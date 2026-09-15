"""심평원 병원 명부 자동 갱신 — 공공데이터포털 병원정보서비스 API.

분기마다 XLSX를 손으로 받아 적재하던 것을, 서버가 스스로 받아오게 한다.
심평원 자료는 분기 단위로 바뀌지만 매주 한 번 확인해 두면 손이 갈 일이 없다.

받는 것: 전국 병의원 기본 목록(요양기호·기관명·종별·시도·주소·전화).
  → source_hospitals(상담 입력 자동완성·종별 배지)와
    cooperation_facility_directory(협력기관 전국 검색) 두 곳을 함께 갱신한다.
상세(진료과목·병상·간호간병통합)는 병원별로 한 건씩 불러야 해서 4만 곳 전부는 무리다.
  실제로 화면에 나오는 곳 — 최근 1년 상담에 등장한 모병원 + 협력기관 — 만 받는다(수백 곳, 몇 분).

환자 정보는 한 글자도 밖으로 나가지 않는다 — 공공 목록을 받아오기만 한다.

설정(.env):
  HIRA_SERVICE_KEY=...      공공데이터포털에서 '병원정보서비스' 활용신청 후 받은 일반 인증키(Decoding)
  HIRA_SYNC_ENABLED=1       0이면 끔 (기본 1, 키가 없으면 어차피 조용히 건너뜀)
  HIRA_SYNC_WEEKDAY=0       0=월 … 6=일 (기본 월요일)
  HIRA_SYNC_HOUR=6          기본 06시 (백업 03시 뒤, 업무 시작 전)
  HIRA_SYNC_STATUS=...      상태 파일 경로 (기본: DB 옆 hira_sync_status.json → 운영은 /data)

기동 시 마스터에 심평원 명부가 한 건도 없거나 마지막 갱신이 실패였으면 30초 뒤 바로 한 번 받는다.
그 뒤 매주. 실패하면 6시간 뒤 재시도. 기관협력 화면 상단에서 상태를 보고 [지금 갱신]도 할 수 있다.

수동 실행:  python hira_sync.py            (기본 목록 + 상세, 지금 한 번)
            python hira_sync.py --details  (상세만 다시)
            python hira_sync.py --status   (마지막 갱신 결과)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import models
import partnerships

logger = logging.getLogger(__name__)

API_ENDPOINT = "https://apis.data.go.kr/B551182/hospInfoServicev2/getHospBasisList"
ROWS_PER_PAGE = 1000
def _default_status_path() -> Path:
    """상태 파일은 DB와 같은 폴더(운영: /data 볼륨)에 둔다.
    2026-09-15 교훈: ./data/ 기본값은 컨테이너 안의 /app/data(이미지 계층)라 재빌드마다 사라지고
    NAS 쪽에서도 보이지 않아, 갱신이 한 번도 안 돌았다는 사실을 아무도 알 수 없었다."""
    env = os.getenv("HIRA_SYNC_STATUS")
    if env:
        return Path(env)
    db_path = os.getenv("BOKJU_DB_PATH")
    if db_path:
        return Path(db_path).parent / "hira_sync_status.json"
    return Path("./data/hira_sync_status.json")


STATUS_PATH = _default_status_path()
# 기동 직후 첫 갱신까지 기다리는 시간(초) — 앱이 다 뜬 뒤에 시작
BOOTSTRAP_DELAY = int(os.getenv("HIRA_SYNC_BOOTSTRAP_DELAY", "30"))
# 실패했을 때 다음 주까지 기다리지 않고 다시 시도하는 간격(초)
RETRY_AFTER_FAILURE = 6 * 3600

ENABLED = os.getenv("HIRA_SYNC_ENABLED", "1") == "1"
WEEKDAY = int(os.getenv("HIRA_SYNC_WEEKDAY", "0"))
HOUR = int(os.getenv("HIRA_SYNC_HOUR", "6"))


def service_key() -> str:
    return (os.getenv("HIRA_SERVICE_KEY") or os.getenv("DATA_GO_KR_SERVICE_KEY") or "").strip()


# ── API ──

def _fetch_page(key: str, page: int) -> tuple[list[dict], int]:
    """한 페이지. (items, totalCount). 키 미등록·서비스 오류는 그대로 올린다."""
    url = f"{API_ENDPOINT}?{urlencode({'serviceKey': key, 'pageNo': page, 'numOfRows': ROWS_PER_PAGE, '_type': 'json'})}"
    with urlopen(url, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    header = ((payload.get("response") or {}).get("header") or {})
    if str(header.get("resultCode", "00")) not in ("00", "0"):
        raise RuntimeError(f"API 오류 {header.get('resultCode')}: {header.get('resultMsg')}")
    body = ((payload.get("response") or {}).get("body") or {})
    items = (body.get("items") or {}).get("item") or []
    if isinstance(items, dict):
        items = [items]
    return items, int(body.get("totalCount") or 0)


def _entry(row: dict) -> dict:
    return {
        "official_code": (row.get("ykiho") or "").strip(),
        "name": (row.get("yadmNm") or "").strip(),
        "kind": (row.get("clCdNm") or "").strip() or None,
        "region": (row.get("sidoCdNm") or "").strip() or None,
        "address": (row.get("addr") or "").strip() or None,
        "phone": (row.get("telno") or "").strip() or None,
    }


def fetch_all(key: str, *, progress=None) -> list[dict]:
    """전국 목록 전체. 약 8만 건(의원 포함), 1000건씩 80여 번."""
    entries, page, total, received = [], 1, None, 0
    # 받은 행 수로 끝을 판단한다(빈 페이지면 즉시 중단). 필터로 걸러진 건수와 섞지 않는다.
    while total is None or received < total:
        items, total = _fetch_page(key, page)
        if not items:
            break
        received += len(items)
        entries.extend(e for e in (_entry(r) for r in items) if e["name"] and e["official_code"])
        if progress:
            progress(len(entries), total)
        page += 1
        time.sleep(0.1)  # 공공 API 예의 — 초당 수십 회 두드리지 않는다
    return entries


# ── 적재 ──

# 협력기관 검색 명부에 넣는 종별. 치과의원·한의원·보건소·조산원까지 넣으면 4만→8만으로
# 불어나 검색이 지저분해진다. 환자가 실제로 오는 급성기·요양·한방·정신 병원과 의원까지.
DIRECTORY_KINDS = {"상급종합", "종합병원", "병원", "의원", "요양병원", "한방병원", "정신병원"}
# 상담 입력 자동완성 마스터도 같은 범위. 자동완성은 입력한 글자로 좁혀지므로 의원이 있어도 무방하다.
MASTER_KINDS = DIRECTORY_KINDS  # 기존 마스터에 의원이 이미 들어 있어 같은 범위로 맞춘다


def apply(entries: list[dict]) -> dict:
    """두 테이블에 upsert. 이름이 바뀐 기관은 요양기호 기준으로 따라간다."""
    directory = partnerships.import_facility_directory(
        [e for e in entries if e["kind"] in DIRECTORY_KINDS], source="hira-api")
    master = models.upsert_source_hospitals(
        [e for e in entries if e["kind"] in MASTER_KINDS], source="hira-api")
    return {"directory": directory, "master": master, "fetched": len(entries)}


# ── 상세(진료과목·병상·간호간병) ──

DETAIL_BASE = "https://apis.data.go.kr/B551182/MadmDtlInfoService2.8"
# 병상 합계 — 분기 XLSX 적재(import_cooperation_facility_details.BED_COLUMNS)와 같은 조합.
# 실측: 의료법인안동병원 850+68+76+10+4+34+7 = 1,049 = XLSX 값과 일치.
BED_FIELDS = ("stdSickbdCnt", "hghrSickbdCnt", "aduChldSprmCnt", "chldSprmCnt", "nbySprmCnt",
              "partumCnt", "psydeptClsHghrSbdCnt", "psydeptClsGnlSbdCnt",
              "psydeptOpenHghrSbdCnt", "psydeptOpenGnlSbdCnt", "isnrSbdCnt", "anvirTrrmSbdCnt")
DETAIL_WINDOW_DAYS = 365


def _detail_items(key: str, op: str, ykiho: str) -> list[dict]:
    url = f"{DETAIL_BASE}/{op}?{urlencode({'serviceKey': key, 'ykiho': ykiho, 'pageNo': 1, 'numOfRows': 100, '_type': 'json'})}"
    with urlopen(url, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    body = ((payload.get("response") or {}).get("body") or {})
    items = body.get("items") or {}
    items = items.get("item", []) if isinstance(items, dict) else []
    return [items] if isinstance(items, dict) else (items or [])


def fetch_detail(key: str, ykiho: str) -> dict:
    """한 기관의 진료과목·병상·간호간병통합(특수진료 KH)."""
    # API가 코드를 숫자로 주는 경우가 있다(srchCd 등). 전부 문자열로 다룬다.
    departments = {str(it.get("dgsbjtCdNm") or "").strip() for it in _detail_items(key, "getDgsbjtInfo2.8", ykiho)}
    special = {str(it.get("srchCd") or "").strip() for it in _detail_items(key, "getSpclDiagInfo2.8", ykiho)}
    beds = None
    for it in _detail_items(key, "getEqpInfo2.8", ykiho)[:1]:
        beds = sum(int(it.get(f) or 0) for f in BED_FIELDS)
    return {"departments": {d for d in departments if d}, "integrated": "KH" in special, "beds": beds}


def detail_targets() -> dict[str, str]:
    """상세를 받을 기관 — 최근 1년 상담에 나온 모병원 + 협력기관. {요양기호: 이름}.

    4만 곳 전부는 병원별 호출이라 무리이고, 화면에 실제로 나오는 곳만 받으면 충분하다.
    """
    since = (datetime.now() - timedelta(days=DETAIL_WINDOW_DAYS)).date().isoformat()
    targets = {}
    overview = models.hospital_referral_overview(since, datetime.now().date().isoformat())
    idx = models._hospital_kind_index()
    for h in overview["hospitals"]:
        code = models.hospital_official_code(h["name"], idx)
        if code is None:
            code = next((c for c in (models.hospital_official_code(v["name"], idx) for v in h["variants"]) if c), None)
        if code:
            targets.setdefault(code, h["name"])
    conn = models.get_db()
    for r in conn.execute("""SELECT d.official_code, COALESCE(p.official_name, h.name) name
                             FROM cooperation_partners p JOIN source_hospitals h ON h.id=p.hospital_id
                             LEFT JOIN cooperation_facility_directory d ON d.id=p.directory_id
                             WHERE d.official_code IS NOT NULL"""):
        targets.setdefault(r["official_code"], r["name"])
    conn.close()
    return targets


def sync_details(key: str, *, targets: dict[str, str] | None = None, progress=None) -> dict:
    """대상 기관의 상세를 받아 명부에 반영. 기관당 3회 호출, 0.1초 간격."""
    targets = detail_targets() if targets is None else targets
    departments, beds, integrated, failed = {}, {}, set(), []
    for i, (code, name) in enumerate(targets.items(), 1):
        try:
            d = fetch_detail(key, code)
        except Exception as e:  # noqa: BLE001 — 한 기관이 어떤 이유로 깨져도 나머지는 계속 받는다
            failed.append(name)
            logger.warning("상세 실패 %s: %s", name, e)
            continue
        if d["departments"]:
            departments[code] = d["departments"]
        if d["beds"] is not None:
            beds[code] = d["beds"]
        if d["integrated"]:
            integrated.add(code)
        if progress:
            progress(i, len(targets), name)
        time.sleep(0.1)
    updated = partnerships.import_facility_details(
        departments=departments, beds=beds, integrated_codes=integrated,
        updated_at=datetime.now().strftime("%Y-%m-%d"))
    return {"targets": len(targets), "updated": updated, "failed": failed[:20], "failed_count": len(failed)}


def _master_total() -> int:
    conn = models.get_db()
    try:
        return conn.execute("SELECT COUNT(*) FROM source_hospitals WHERE active=1").fetchone()[0]
    finally:
        conn.close()


def _write_status(**fields):
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {"at": datetime.now().isoformat(timespec="seconds"), **fields}
    STATUS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def status() -> dict | None:
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def master_synced() -> bool:
    """전국 명부(심평원 API)가 한 번이라도 마스터에 들어갔는가.
    운영 DB는 개발 DB와 별개라 코드를 배포해도 명부는 따라오지 않는다 — DB에서 직접 확인한다."""
    conn = models.get_db()
    try:
        n = conn.execute("SELECT COUNT(*) FROM source_hospitals WHERE source='hira-api'").fetchone()[0]
    except Exception:  # noqa: BLE001 — 아직 스키마가 없으면 '안 됨'으로 본다
        n = 0
    finally:
        conn.close()
    return n > 0


def needs_bootstrap() -> bool:
    """기동 직후 바로 갱신해야 하는가 — 명부가 한 번도 안 들어갔거나 마지막 갱신이 실패했을 때.
    매주 월요일 06시만 기다리면 키를 넣은 뒤 최대 일주일 동안 자동완성이 13곳짜리 마스터로 돈다(2026-09-15 실제 발생)."""
    if not master_synced():
        return True
    last = status() or {}
    return last.get("ok") is not True


_run_lock = threading.Lock()


def is_running() -> bool:
    return _run_lock.locked()


def run(reason: str = "manual") -> dict:
    """한 번 갱신. 실패해도 예외를 밖으로 내지 않고 상태 파일에 남긴다(스케줄러가 죽지 않게).
    이미 도는 중이면 새로 시작하지 않는다(스케줄·기동·수동 버튼이 겹칠 수 있다)."""
    if not _run_lock.acquire(blocking=False):
        logger.info("심평원 갱신 건너뜀(%s) — 이미 실행 중", reason)
        return {"ok": False, "reason": reason, "error": "이미 갱신 중", "skipped": True}
    try:
        return _run_locked(reason)
    finally:
        _run_lock.release()


def run_in_background(reason: str = "manual") -> bool:
    """수동 버튼용. 시작했으면 True, 이미 도는 중이면 False."""
    if is_running():
        return False
    threading.Thread(target=run, args=(reason,), name="hira-sync-manual", daemon=True).start()
    return True


def _run_locked(reason: str) -> dict:
    key = service_key()
    if not key:
        logger.info("심평원 갱신 건너뜀 — HIRA_SERVICE_KEY 없음")
        return _write_status(ok=False, reason=reason, error="HIRA_SERVICE_KEY 없음")
    started = time.time()
    try:
        entries = fetch_all(key)
        result = apply(entries)
        # 기본 목록이 갱신된 뒤에 상세를 받아야 새로 생긴 기관도 대상에 든다.
        detail = sync_details(key)
        result["details"] = detail
        took = round(time.time() - started, 1)
        logger.info("심평원 갱신 완료(%s) — 명부 %d, 마스터 %d, 상세 %d/%d곳, %.1f초",
                    reason, result["directory"], result["master"], detail["updated"], detail["targets"], took)
        return _write_status(ok=True, reason=reason, seconds=took, master_total=_master_total(), **result)
    except HTTPError as e:
        msg = e.read().decode("utf-8", "ignore")[:300]
        hint = " — 공공데이터포털에서 '병원정보서비스' 활용신청이 되어 있는지 확인" if "NOT_REGISTERED" in msg else ""
        logger.warning("심평원 갱신 실패 HTTP %s%s", e.code, hint)
        return _write_status(ok=False, reason=reason, error=f"HTTP {e.code}{hint}")
    except (URLError, RuntimeError, ValueError, OSError) as e:
        logger.warning("심평원 갱신 실패: %s", e)
        return _write_status(ok=False, reason=reason, error=str(e)[:300])


# ── 스케줄 ──

def _seconds_until_next_run() -> float:
    now = datetime.now()
    target = now.replace(hour=HOUR, minute=0, second=0, microsecond=0)
    days_ahead = (WEEKDAY - now.weekday()) % 7
    target += timedelta(days=days_ahead)
    if target <= now:
        target += timedelta(days=7)
    return (target - now).total_seconds()


def _next_wait(last: dict | None) -> float:
    """다음 실행까지 대기 — 평소엔 주간 시각, 마지막이 실패였으면 6시간 뒤 재시도(주간 시각이 더 이르면 그때)."""
    weekly = _seconds_until_next_run()
    if last and last.get("ok") is not True:
        return min(weekly, RETRY_AFTER_FAILURE)
    return weekly


def _loop():
    last = None
    if needs_bootstrap():
        logger.info("심평원 명부가 비어 있거나 마지막 갱신이 실패 — %d초 뒤 바로 갱신", BOOTSTRAP_DELAY)
        threading.Event().wait(BOOTSTRAP_DELAY)
        last = run("startup")
    while True:
        threading.Event().wait(_next_wait(last))
        last = run("weekly")


def start_scheduler():
    """매주 정해진 요일·시각에 갱신. 키가 없으면 스레드를 띄우지 않는다."""
    if not ENABLED or not service_key():
        logger.info("심평원 자동 갱신 꺼짐 (HIRA_SYNC_ENABLED=%s, 키=%s)", ENABLED, "있음" if service_key() else "없음")
        return
    t = threading.Thread(target=_loop, name="hira-sync-scheduler", daemon=True)
    t.start()
    logger.info("심평원 자동 갱신 시작 — 매주 %s요일 %02d시 (명부 없으면 기동 직후 1회), 상태 파일 %s",
                "월화수목금토일"[WEEKDAY], HOUR, STATUS_PATH)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if "--status" in sys.argv:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    elif "--details" in sys.argv:
        # 기본 목록은 두고 상세만 다시 (대상 확인·재시도용)
        out = sync_details(service_key(), progress=lambda i, n, name: print(f"  {i}/{n} {name}", end="\r"))
        print(); print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        out = run("manual")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        sys.exit(0 if out.get("ok") else 1)
