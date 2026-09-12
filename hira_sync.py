"""심평원 병원 명부 자동 갱신 — 공공데이터포털 병원정보서비스 API.

분기마다 XLSX를 손으로 받아 적재하던 것을, 서버가 스스로 받아오게 한다.
심평원 자료는 분기 단위로 바뀌지만 매주 한 번 확인해 두면 손이 갈 일이 없다.

받는 것: 전국 병의원 기본 목록(요양기호·기관명·종별·시도·주소·전화).
  → source_hospitals(상담 입력 자동완성·종별 배지)와
    cooperation_facility_directory(협력기관 전국 검색) 두 곳을 함께 갱신한다.
받지 않는 것: 진료과목·병상·간호간병 상세. 그건 병원별로 한 건씩 불러야 해서
  4만 곳을 매주 돌릴 수 없다. 분기 XLSX(import_cooperation_facility_details.py)로 유지.

환자 정보는 한 글자도 밖으로 나가지 않는다 — 공공 목록을 받아오기만 한다.

설정(.env):
  HIRA_SERVICE_KEY=...      공공데이터포털에서 '병원정보서비스' 활용신청 후 받은 일반 인증키(Decoding)
  HIRA_SYNC_ENABLED=1       0이면 끔 (기본 1, 키가 없으면 어차피 조용히 건너뜀)
  HIRA_SYNC_WEEKDAY=0       0=월 … 6=일 (기본 월요일)
  HIRA_SYNC_HOUR=6          기본 06시 (백업 03시 뒤, 업무 시작 전)

수동 실행:  python hira_sync.py            (지금 한 번 받기)
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
STATUS_PATH = Path(os.getenv("HIRA_SYNC_STATUS") or "./data/hira_sync_status.json")

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

def apply(entries: list[dict]) -> dict:
    """두 테이블에 upsert. 이름이 바뀐 기관은 요양기호 기준으로 따라간다."""
    directory = partnerships.import_facility_directory(entries, source="hira-api")
    # 병원 마스터는 의원까지 넣으면 자동완성이 흐려지므로 병원급만 (기존 XLSX 적재와 같은 기준)
    hospital_kinds = {"상급종합", "종합병원", "병원", "요양병원", "정신병원", "치과병원", "한방병원"}
    master = models.upsert_source_hospitals(
        [e for e in entries if e["kind"] in hospital_kinds], source="hira-api")
    return {"directory": directory, "master": master, "fetched": len(entries)}


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


def run(reason: str = "manual") -> dict:
    """한 번 갱신. 실패해도 예외를 밖으로 내지 않고 상태 파일에 남긴다(스케줄러가 죽지 않게)."""
    key = service_key()
    if not key:
        logger.info("심평원 갱신 건너뜀 — HIRA_SERVICE_KEY 없음")
        return _write_status(ok=False, reason=reason, error="HIRA_SERVICE_KEY 없음")
    started = time.time()
    try:
        entries = fetch_all(key)
        result = apply(entries)
        took = round(time.time() - started, 1)
        logger.info("심평원 갱신 완료(%s) — 명부 %d, 마스터 %d, %.1f초", reason, result["directory"], result["master"], took)
        return _write_status(ok=True, reason=reason, seconds=took, **result)
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


def _loop():
    while True:
        threading.Event().wait(_seconds_until_next_run())
        run("weekly")


def start_scheduler():
    """매주 정해진 요일·시각에 갱신. 키가 없으면 스레드를 띄우지 않는다."""
    if not ENABLED or not service_key():
        logger.info("심평원 자동 갱신 꺼짐 (HIRA_SYNC_ENABLED=%s, 키=%s)", ENABLED, "있음" if service_key() else "없음")
        return
    t = threading.Thread(target=_loop, name="hira-sync-scheduler", daemon=True)
    t.start()
    logger.info("심평원 자동 갱신 시작 — 매주 %s요일 %02d시", "월화수목금토일"[WEEKDAY], HOUR)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if "--status" in sys.argv:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    else:
        out = run("manual")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        sys.exit(0 if out.get("ok") else 1)
