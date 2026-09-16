"""국민건강보험공단 장기요양기관(요양원) 명부 자동 갱신 — 공공데이터포털 '장기요양기관 검색 서비스' API.

병원은 심평원(hira_sync.py)에서 받지만 요양원은 의료기관이 아니라 심평원 명부에 없다.
요양원(노인요양시설·노인요양공동생활가정)은 공단 장기요양기관 명부가 원본이다.
상담일지 '환자상태 → 요양원' 칸도 병원처럼 전국 공식 명칭에서 찾게 하려고 만들었다(2026-09-16).

받는 것: 전국 입소시설(기관유형 A03 노인요양시설·A04 노인요양공동생활가정, 약 6천 곳) — 장기요양기관기호·기관명·기관유형·시도.
  → source_nursing_homes(상담 입력 자동완성) 한 곳만 갱신한다.
  이 API는 주소·전화를 주지 않는다(법정동 코드만). 자동완성엔 이름·시도·유형이면 충분하다.
  재가(방문요양·주야간보호, B·C 유형)는 요양원이 아니므로 넣지 않는다 — 2만 곳이 섞이면 검색이 지저분해진다.

API 특성(2026-09-16 포털 Swagger 명세로 확인):
  - 시도코드(siDoCd)가 필수라 전국은 시도 16코드 × 유형 2개를 돌아 받는다(40회 남짓, 20초 안쪽).
  - 응답은 XML 기본이지만 _type=json도 된다(실측). 둘 다 읽는다. 한도는 일 10,000회·초당 10회.
  - 같은 이름 요양원이 전국에 여럿이라(예: 행복요양원) 마스터의 이름 UNIQUE와 충돌한다
    → 겹치면 '행복요양원 (경북)'처럼 시도를, 같은 시도 안에서도 겹치면 번호를 붙여 구분한다.

환자 정보는 한 글자도 밖으로 나가지 않는다 — 공공 목록을 받아오기만 한다.

설정(.env):
  LTCI_SERVICE_KEY=...      공공데이터포털에서 '국민건강보험공단_장기요양기관 검색 서비스' 활용신청 후 받은 인증키(Decoding).
                            비워 두면 HIRA_SERVICE_KEY를 같이 쓴다(같은 포털 키 — 단, 이 서비스도 활용신청이 돼 있어야 한다).
  LTCI_SYNC_ENABLED=1       0이면 끔
  LTCI_SYNC_WEEKDAY=0       0=월 … 6=일
  LTCI_SYNC_HOUR=7          기본 07시 (심평원 06시 다음)
  LTCI_SYNC_STATUS=...      상태 파일 경로 (기본: DB 옆 ltci_sync_status.json)

기동 시 마스터에 공단 명부가 한 건도 없거나 마지막 갱신이 실패였으면 60초 뒤 바로 한 번 받는다.
그 뒤 매주. 실패하면 6시간 뒤 재시도. 기관협력 화면에서 상태를 보고 [지금 갱신]도 할 수 있다.

수동 실행:  python ltci_sync.py            (지금 한 번)
            python ltci_sync.py --status   (마지막 갱신 결과)
            python ltci_sync.py --lookup 이름  (이름으로 바로 조회)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import models

logger = logging.getLogger(__name__)

API_BASE = "https://apis.data.go.kr/B550928/searchLtcInsttService02"
LIST_OP = "getLtcInsttSeachList02"
ROWS_PER_PAGE = 1000

# 기관유형코드(adminPttnCd) — 공단 장기요양 홈페이지 기관 상세 화면의 라벨로 전수 확인(2026-09-16).
# A=입소시설(요양원), B=재가노인복지시설, C=재가장기요양기관.
ADMIN_KINDS = {
    "A01": "노인요양시설", "A02": "노인전문요양시설", "A03": "노인요양시설",
    "A04": "노인요양공동생활가정", "A05": "노인요양시설",
    "B01": "재가 방문요양", "B02": "재가 방문목욕", "B03": "재가 주야간보호",
    "B04": "재가 단기보호", "B05": "재가 방문간호", "B06": "재가 복지용구",
    "C01": "재가 방문요양", "C02": "재가 방문목욕", "C03": "재가 주야간보호",
    "C04": "재가 단기보호", "C05": "재가 방문간호", "C06": "재가 복지용구",
}
# 실제로 건수가 있는 입소 유형은 A03(노인요양시설, 개정법)·A04(공동생활가정)뿐이다(2026-09-16 전수 조회: A01·A02·A05는 전국 0건).
# 옛 코드까지 매번 묻으면 갱신 한 번에 헛호출 57회라 뺐다. 라벨 표는 위에 남겨 둔다.
RESIDENTIAL_CODES = ("A03", "A04")

# 시도코드 → 마스터 region 표기(SOURCE_HOSPITAL_SEED·import_nursing_master와 동일한 짧은 이름).
# 2026-09-16 API에 10~99를 전수 조회해 건수가 있는 코드만 남겼다. 강원 42→51, 전북 45→52는 특별자치도 전환으로 옛 코드가 0건,
# 광주(29)·전남(46)은 2026년 '전남광주통합특별시' 출범으로 **12**로 합쳐졌다. 코드가 또 바뀌면 이 표만 고친다.
SIDO_CODES = [
    ("11", "서울"), ("12", "전남광주"), ("26", "부산"), ("27", "대구"), ("28", "인천"),
    ("30", "대전"), ("31", "울산"), ("36", "세종시"), ("41", "경기"), ("51", "강원"),
    ("43", "충북"), ("44", "충남"), ("52", "전북"), ("47", "경북"), ("48", "경남"), ("50", "제주"),
]


def _default_status_path() -> Path:
    env = os.getenv("LTCI_SYNC_STATUS")
    if env:
        return Path(env)
    db_path = os.getenv("BOKJU_DB_PATH")
    if db_path:
        return Path(db_path).parent / "ltci_sync_status.json"
    return Path("./data/ltci_sync_status.json")


STATUS_PATH = _default_status_path()
BOOTSTRAP_DELAY = int(os.getenv("LTCI_SYNC_BOOTSTRAP_DELAY", "60"))
RETRY_AFTER_FAILURE = 6 * 3600
ENABLED = os.getenv("LTCI_SYNC_ENABLED", "1") == "1"
WEEKDAY = int(os.getenv("LTCI_SYNC_WEEKDAY", "0"))
HOUR = int(os.getenv("LTCI_SYNC_HOUR", "7"))


def service_key() -> str:
    return (os.getenv("LTCI_SERVICE_KEY") or os.getenv("HIRA_SERVICE_KEY")
            or os.getenv("DATA_GO_KR_SERVICE_KEY") or "").strip()


# ── API ──

def _parse_response(raw: bytes) -> tuple[list[dict], int]:
    """XML(기본) 또는 JSON 응답 → (items, totalCount). 게이트웨이 오류(키 미등록 등)는 RuntimeError."""
    text = raw.decode("utf-8", "ignore").strip()
    if text.startswith("{"):
        payload = json.loads(text)
        header = ((payload.get("response") or {}).get("header") or {})
        if str(header.get("resultCode", "00")) not in ("00", "0"):
            raise RuntimeError(f"API 오류 {header.get('resultCode')}: {header.get('resultMsg')}")
        body = ((payload.get("response") or {}).get("body") or {})
        items = (body.get("items") or {}).get("item") or []
        if isinstance(items, dict):
            items = [items]
        return [{k: (str(v) if v is not None else "") for k, v in it.items()} for it in items], int(body.get("totalCount") or 0)
    root = ET.fromstring(text)
    if root.tag == "OpenAPI_ServiceResponse":  # 공공데이터포털 게이트웨이 오류 (인증키·활용신청·트래픽)
        code = (root.findtext(".//returnReasonCode") or "").strip()
        msg = (root.findtext(".//returnAuthMsg") or root.findtext(".//errMsg") or "").strip()
        raise RuntimeError(f"API 오류 {code}: {msg}")
    rc = (root.findtext("./header/resultCode") or "00").strip()
    if rc not in ("00", "0"):
        raise RuntimeError(f"API 오류 {rc}: {(root.findtext('./header/resultMsg') or '').strip()}")
    items = [{child.tag: (child.text or "").strip() for child in item} for item in root.iter("item")]
    return items, int((root.findtext("./body/totalCount") or "0").strip() or 0)


# 포털 게이트웨이 한도(2026-09-16 실측 헤더): 일 10,000회 · **초당 10회**. 넘으면 HTTP 429(코드 23)가 잠시 계속된다.
CALL_PAUSE = 0.15          # 호출 사이 최소 간격 — 순차로 돌리면 초당 6~7회
RETRY_429_WAITS = (1.5, 3.0, 6.0)


def _call(key: str, params: dict, timeout: int) -> tuple[list[dict], int]:
    url = f"{API_BASE}/{LIST_OP}?{urlencode({'serviceKey': key, **params})}"
    for attempt, wait in enumerate(RETRY_429_WAITS + (None,)):
        try:
            with urlopen(url, timeout=timeout) as resp:
                return _parse_response(resp.read())
        except HTTPError as e:
            if e.code != 429 or wait is None:
                raise
            logger.info("공단 API 초당 한도 초과(429) — %.1f초 뒤 재시도(%d)", wait, attempt + 1)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def _fetch_page(key: str, sido: str, kind_code: str, page: int) -> tuple[list[dict], int]:
    """시도·유형 한 조합의 한 페이지. (items, totalCount)."""
    return _call(key, {"siDoCd": sido, "adminPttnCd": kind_code, "pageNo": page,
                       "numOfRows": ROWS_PER_PAGE, "_type": "json"}, timeout=60)


def _entry(row: dict, region: str | None) -> dict:
    code = (row.get("adminPttnCd") or "").strip()
    return {
        "official_code": (row.get("longTermAdminSym") or "").strip(),
        "name": " ".join((row.get("adminNm") or "").split()),
        "kind": ADMIN_KINDS.get(code, code or None),
        "type_code": code,
        "region": region,
        "address": None,
        "phone": None,
    }


def fetch_all(key: str, *, progress=None) -> list[dict]:
    """전국 입소시설 전체(6천 곳 남짓). 시도 × 유형별로 받고 기관기호로 중복을 걷어낸다."""
    seen: dict[str, dict] = {}
    for sido, region in SIDO_CODES:
        for code in RESIDENTIAL_CODES:
            page, total, received = 1, None, 0
            while total is None or received < total:
                items, total = _fetch_page(key, sido, code, page)
                if not items:
                    break
                received += len(items)
                for e in (_entry(r, region) for r in items):
                    if e["name"] and e["official_code"]:
                        seen.setdefault(e["official_code"], e)
                page += 1
                time.sleep(CALL_PAUSE)  # 초당 10회 한도
            if progress:
                progress(len(seen), region, code)
    return list(seen.values())


def disambiguate(entries: list[dict]) -> list[dict]:
    """같은 이름이 여럿이면 '이름 (시도)', 같은 시도 안에서도 겹치면 '이름 (시도 2)'.
    마스터는 이름이 UNIQUE라 그대로 넣으면 한 곳으로 합쳐져 나머지가 사라진다.
    번호는 기관기호 순이라 갱신할 때마다 같은 기관이 같은 이름을 받는다."""
    by_name: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by_name[e["name"]].append(e)
    out = []
    for name, group in by_name.items():
        if len(group) == 1:
            out.extend(group)
            continue
        by_region: dict[str, list[dict]] = defaultdict(list)
        for e in sorted(group, key=lambda x: x["official_code"]):
            by_region[e.get("region") or ""].append(e)
        for region, sub in by_region.items():
            for i, e in enumerate(sub, 1):
                suffix = region if len(sub) == 1 else f"{region} {i}".strip()
                out.append(dict(e, name=f"{name} ({suffix})" if suffix else name))
    return out


def lookup(key: str, q: str, limit: int = 20, timeout: int = 20) -> list[dict]:
    """이름으로 공단에서 바로 찾기 — 상담일지에서 마스터에 없는 요양원을 그 자리에서 등록할 때.
    시도코드가 필수라 시도 19코드를 묻고, 입소시설(A 유형)만 남긴다.
    초당 10회 한도 때문에 3개 스레드 + 호출당 0.3초 쉼(≈초당 5~6회, 전체 3~4초). 6개 병렬로 돌리자 첫 호출부터 429가 났다."""
    q = (q or "").strip()
    if not q or not key:
        return []

    def one(sido_region):
        sido, region = sido_region
        try:
            time.sleep(0.3)
            items, _ = _call(key, {"siDoCd": sido, "adminNm": q, "pageNo": 1, "numOfRows": 100, "_type": "json"}, timeout=timeout)
        except (HTTPError, URLError, RuntimeError, ValueError, OSError, ET.ParseError) as e:
            return region, e
        return region, [_entry(r, region) for r in items]

    found: dict[str, dict] = {}
    errors = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for region, result in pool.map(one, SIDO_CODES):
            if isinstance(result, Exception):
                errors.append(result)
                continue
            for e in result:
                if e["name"] and e["official_code"] and e["type_code"] in RESIDENTIAL_CODES:
                    found.setdefault(e["official_code"], e)
    if not found and errors:
        raise errors[0]
    items = sorted(found.values(), key=lambda e: (len(e["name"]), e["name"]))
    return items[:limit]


def register_one(entry: dict, *, source: str) -> str:
    """요양원 한 곳을 마스터에 넣고 저장된 이름을 돌려준다."""
    name = models.canonical_nursing_name(entry.get("name")) or (entry.get("name") or "").strip()
    models.upsert_source_nursing_homes([entry], source=source)
    return name


# ── 적재 ──

def apply(entries: list[dict]) -> dict:
    """마스터 upsert + 이번 명부에 없는 공단 행 비활성화.
    폐업했거나, 동명 기관이 새로 생겨 '이름'이 '이름 (시도)'로 바뀐 옛 행이 자동완성에 남지 않게 한다
    (2026-09-16 첫 두 번의 갱신 사이에 31곳이 그렇게 남았다). 상담에 적힌 이름은 건드리지 않는다."""
    named = disambiguate(entries)
    master = models.upsert_source_nursing_homes(named, source="ltci-api")
    current = {e["name"] for e in named}
    conn = models.get_db()
    try:
        rows = conn.execute("SELECT id, name FROM source_nursing_homes WHERE source='ltci-api' AND active=1").fetchall()
        stale = [r["id"] for r in rows if r["name"] not in current]
        for i in range(0, len(stale), 500):
            chunk = stale[i:i + 500]
            conn.execute(f"UPDATE source_nursing_homes SET active=0, updated_at=CURRENT_TIMESTAMP "
                         f"WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        conn.commit()
    finally:
        conn.close()
    return {"master": master, "fetched": len(entries), "deactivated": len(stale)}


def _master_total() -> int:
    conn = models.get_db()
    try:
        return conn.execute("SELECT COUNT(*) FROM source_nursing_homes WHERE active=1").fetchone()[0]
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
    """공단 명부가 한 번이라도 마스터에 들어갔는가 — DB에서 직접 확인(운영 DB는 코드 배포와 별개)."""
    conn = models.get_db()
    try:
        n = conn.execute("SELECT COUNT(*) FROM source_nursing_homes WHERE source='ltci-api'").fetchone()[0]
    except Exception:  # noqa: BLE001
        n = 0
    finally:
        conn.close()
    return n > 0


def needs_bootstrap() -> bool:
    if not master_synced():
        return True
    last = status() or {}
    return last.get("ok") is not True


_run_lock = threading.Lock()


def is_running() -> bool:
    return _run_lock.locked()


def run(reason: str = "manual") -> dict:
    if not _run_lock.acquire(blocking=False):
        logger.info("공단 요양원 명부 갱신 건너뜀(%s) — 이미 실행 중", reason)
        return {"ok": False, "reason": reason, "error": "이미 갱신 중", "skipped": True}
    try:
        return _run_locked(reason)
    finally:
        _run_lock.release()


def run_in_background(reason: str = "manual") -> bool:
    if is_running():
        return False
    threading.Thread(target=run, args=(reason,), name="ltci-sync-manual", daemon=True).start()
    return True


def _run_locked(reason: str) -> dict:
    key = service_key()
    if not key:
        logger.info("공단 요양원 명부 갱신 건너뜀 — LTCI_SERVICE_KEY 없음")
        return _write_status(ok=False, reason=reason, error="LTCI_SERVICE_KEY 없음")
    started = time.time()
    try:
        entries = fetch_all(key)
        if not entries:
            raise RuntimeError("공단 응답에 요양원이 한 곳도 없음 — 활용신청·시도코드 확인")
        result = apply(entries)
        took = round(time.time() - started, 1)
        logger.info("공단 요양원 명부 갱신 완료(%s) — 받은 %d곳, 마스터 %d곳, %.1f초",
                    reason, result["fetched"], result["master"], took)
        return _write_status(ok=True, reason=reason, seconds=took, master_total=_master_total(), **result)
    except HTTPError as e:
        msg = e.read().decode("utf-8", "ignore")[:300]
        hint = " — 공공데이터포털에서 '장기요양기관 검색 서비스' 활용신청이 되어 있는지 확인" if "NOT_REGISTERED" in msg or "DENIED" in msg else ""
        logger.warning("공단 요양원 명부 갱신 실패 HTTP %s%s", e.code, hint)
        return _write_status(ok=False, reason=reason, error=f"HTTP {e.code}{hint}")
    except (URLError, RuntimeError, ValueError, OSError, ET.ParseError) as e:
        msg = str(e)[:300]
        if any(k in msg.upper() for k in ("REGISTERED", "PERMISSION", "ACCESS_DENIED")):
            msg += " — 공공데이터포털에서 '장기요양기관 검색 서비스' 활용신청이 되어 있는지 확인"
        logger.warning("공단 요양원 명부 갱신 실패: %s", msg)
        return _write_status(ok=False, reason=reason, error=msg)


# ── 스케줄 ──

def _seconds_until_next_run() -> float:
    now = datetime.now()
    target = now.replace(hour=HOUR, minute=0, second=0, microsecond=0)
    target += timedelta(days=(WEEKDAY - now.weekday()) % 7)
    if target <= now:
        target += timedelta(days=7)
    return (target - now).total_seconds()


def _next_wait(last: dict | None) -> float:
    weekly = _seconds_until_next_run()
    if last and last.get("ok") is not True:
        return min(weekly, RETRY_AFTER_FAILURE)
    return weekly


def _loop():
    last = None
    if needs_bootstrap():
        logger.info("공단 요양원 명부가 비어 있거나 마지막 갱신이 실패 — %d초 뒤 바로 갱신", BOOTSTRAP_DELAY)
        threading.Event().wait(BOOTSTRAP_DELAY)
        last = run("startup")
    while True:
        threading.Event().wait(_next_wait(last))
        last = run("weekly")


def start_scheduler():
    if not ENABLED or not service_key():
        logger.info("공단 요양원 명부 자동 갱신 꺼짐 (LTCI_SYNC_ENABLED=%s, 키=%s)", ENABLED, "있음" if service_key() else "없음")
        return
    threading.Thread(target=_loop, name="ltci-sync-scheduler", daemon=True).start()
    logger.info("공단 요양원 명부 자동 갱신 시작 — 매주 %s요일 %02d시 (명부 없으면 기동 직후 1회), 상태 파일 %s",
                "월화수목금토일"[WEEKDAY], HOUR, STATUS_PATH)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if "--status" in sys.argv:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    elif "--lookup" in sys.argv:
        q = sys.argv[sys.argv.index("--lookup") + 1]
        print(json.dumps(lookup(service_key(), q), ensure_ascii=False, indent=2))
    else:
        out = run("manual")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        sys.exit(0 if out.get("ok") else 1)
