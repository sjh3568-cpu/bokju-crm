"""EasyQR 전화상담 접수 → 인박스 브릿지 (옴니채널).

walk.induk.ai.kr 랜딩페이지의 '빠른 전화상담 신청'(consult.php)은 접수를 같은 NAS의
MariaDB(`easyqr_db.consultations`)에 쌓는다. EasyQR은 별도 담당 소관이라 PHP를 고쳐
우리 웹훅으로 쏘게 만들 수 없다. 대신 이 워커가 그 테이블을 **읽기 전용**으로 폴링해
CRM 인박스에 등록한다(채널=웹문의, 인바운드).

SELECT 권한만 있는 계정 하나면 되고 EasyQR 코드는 한 줄도 건드리지 않는다. 같은 NAS
안이라 사내망 밖으로 나가지도, 외부 포트를 열지도 않는다 — CLAUDE.md의 사내망 원칙에 부합.

동작:
  · EASYQR_DB_HOST/USER/PASS 가 모두 설정돼야 활성 (하나라도 없으면 no-op)
  · EASYQR_POLL_SECONDS(기본 180초)마다 `id > last_id` 인 접수만 조회
  · 처리한 마지막 id를 `data/easyqr_sync_status.json`에 기록 → 중복 등록 방지
  · 첫 기동 시에는 현재 최대 id부터 시작한다(옛 접수 수백 건이 한꺼번에 쏟아지지 않게).
    과거분이 필요하면 EASYQR_BACKFILL_FROM=<id> (0이면 전체)
  · 전화번호로 환자 자동 매칭 — 매칭되면 인박스 카드가 그 환자에 붙는다

주의: 실패가 상담 업무를 막지 않도록 예외는 모두 로그만 남기고 다음 주기에 재시도한다.
한 건이 계속 실패하면 5회 뒤 건너뛰고 error 로그를 남긴다(워터마크가 영구히 멈추는 것 방지).

카드 모양은 `/api/webhook/homepage`(views/inbound.py)와 똑같이 맞췄다. 나중에 EasyQR이
직접 쏘는 방식으로 바꾸더라도 상담사가 보는 화면이 달라지지 않게 하기 위해서다.
"""
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import models

logger = logging.getLogger(__name__)

POLL_SECONDS = int(os.getenv("EASYQR_POLL_SECONDS", "180"))
BATCH_LIMIT = 50                    # 한 주기에 가져올 최대 건수 — 밀려 있어도 조금씩 따라잡는다
MAX_STRIKES = 5                     # 같은 건이 이만큼 연속 실패하면 건너뛴다
_DB_NAME = "easyqr_db"
_TABLE = "consultations"
_SUMMARY_PREFIX = "전화상담 신청"

# 웹훅(_HOMEPAGE_EXTRA_FIELDS)과 같은 라벨 — 두 경로가 같은 본문을 만들도록.
_EXTRA_FIELDS = [
    ("available_time", "연락가능시간"),
    ("address", "거주지"),
    ("patient_age", "환자나이"),
]


def _status_path() -> Path:
    env = os.getenv("EASYQR_SYNC_STATUS")
    if env:
        return Path(env)
    db_path = os.getenv("BOKJU_DB_PATH")
    if db_path:
        return Path(db_path).parent / "easyqr_sync_status.json"
    return Path("./data/easyqr_sync_status.json")


STATUS_PATH = _status_path()


def _enabled() -> bool:
    return all(os.getenv(k) for k in ("EASYQR_DB_HOST", "EASYQR_DB_USER", "EASYQR_DB_PASS"))


def status() -> dict:
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_status(**fields):
    """상태 파일 갱신. 여기 실패하면 다음 기동에 같은 건을 다시 가져오므로
    _already_registered()가 2차 방어선이 된다."""
    data = {**status(), **fields, "at": datetime.now().isoformat(timespec="seconds")}
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def _norm_phone(raw: str) -> str:
    """숫자만 남겨 010-XXXX-XXXX 형태로. views/inbound.py의 웹훅과 같은 규칙이어야
    같은 사람이 두 경로로 들어와도 같은 환자에 붙는다."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("01"):
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    if len(digits) == 10 and digits.startswith("01"):
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return (raw or "").strip()


def _connect():
    """EasyQR MariaDB 읽기 전용 접속. pymysql은 여기서만 쓰므로 지연 import한다
    (미설치 환경에서 app import 자체가 깨지지 않도록)."""
    import pymysql
    return pymysql.connect(
        host=os.getenv("EASYQR_DB_HOST"),
        port=int(os.getenv("EASYQR_DB_PORT", "3307")),
        user=os.getenv("EASYQR_DB_USER"),
        password=os.getenv("EASYQR_DB_PASS"),
        database=os.getenv("EASYQR_DB_NAME", _DB_NAME),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        read_timeout=15,
    )


def _summary(receipt_no: int, name: str) -> str:
    head = f"{_SUMMARY_PREFIX} #{receipt_no}"
    return f"{head} · {name}" if name else head


def _build_body(row: dict) -> str:
    body = (row.get("content") or "").strip()[:4000]
    extras = [(label, str(row.get(key) or "").strip()) for key, label in _EXTRA_FIELDS]
    extras = [(label, v) for label, v in extras if v and v != "0"]   # 나이 미입력이 0으로 온다
    if extras:
        body = body + "\n\n" + "\n".join(f"[{label}] {v}" for label, v in extras)
    return body


def _already_registered(receipt_no: int) -> bool:
    """상태 파일이 사라진 뒤 재기동하면 같은 건을 다시 가져올 수 있다. 접수번호가
    요약에 박혀 있으므로 그걸로 한 번 더 막는다(방어선 2중화).
    #12 가 #123 에 걸리지 않도록 '정확히 일치' 또는 '이름 구분자까지 일치'만 본다."""
    head = f"{_SUMMARY_PREFIX} #{receipt_no}"
    conn = models.get_db()
    row = conn.execute(
        "SELECT 1 FROM communications WHERE channel = '웹문의' AND created_by = 'EasyQR' "
        "AND (summary = ? OR summary LIKE ?) LIMIT 1",
        (head, f"{head} · %"),
    ).fetchone()
    conn.close()
    return row is not None


def _register(row: dict) -> int | None:
    """접수 1건 → communications. 등록했으면 comm id, 이미 있으면 None."""
    receipt_no = int(row["id"])
    if _already_registered(receipt_no):
        logger.info("EasyQR 접수 #%d 이미 등록됨 — 건너뜀", receipt_no)
        return None
    name = (row.get("name") or "").strip()
    phone = _norm_phone(row.get("phone") or "")
    occurred = row.get("created_at")
    return models.create_communication(
        patient_id=models.match_patient_by_phone(phone),
        channel="웹문의", direction="in",
        contact=phone or name or None,
        summary=_summary(receipt_no, name), body=_build_body(row),
        status="open", created_by="EasyQR",
        occurred_at=occurred.strftime("%Y-%m-%d %H:%M:%S")
                    if isinstance(occurred, datetime) else (occurred or None),
    )


def _initial_last_id(cur) -> int:
    """첫 기동 기준점. 기본은 '지금부터' — 옛 접수가 인박스를 덮지 않게 한다."""
    backfill = (os.getenv("EASYQR_BACKFILL_FROM") or "").strip()
    if backfill.isdigit():
        logger.info("EasyQR 백필 지정 — id > %s 부터 가져온다", backfill)
        return int(backfill)
    cur.execute(f"SELECT COALESCE(MAX(id), 0) AS m FROM {_TABLE}")
    last = int(cur.fetchone()["m"])
    logger.info("EasyQR 첫 동기화 — 현재 최대 id %d 이후 접수부터 등록한다", last)
    return last


def _fetch_new(last_id):
    """(rows, last_id) 반환. 접속·조회 실패는 여기서 삼키고 (None, last_id)."""
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            if last_id is None:
                last_id = _initial_last_id(cur)
                _write_status(last_id=last_id, ok=True, reason="bootstrap", registered=0)
            cur.execute(
                f"SELECT id, name, phone, available_time, address, patient_age, "
                f"content, created_at FROM {_TABLE} "
                f"WHERE id > %s ORDER BY id LIMIT %s",
                (int(last_id), BATCH_LIMIT),
            )
            return cur.fetchall(), last_id
    except Exception:
        logger.exception("EasyQR 접수 조회 실패 — 다음 주기 재시도")
        _write_status(ok=False, error="connect_or_query")
        return None, last_id
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def poll_once() -> int:
    """접수 테이블을 1회 확인해 새 건을 등록. 등록 건수 반환."""
    if not _enabled():
        return 0
    st = status()
    rows, last_id = _fetch_new(st.get("last_id"))
    if not rows:
        return 0

    count = 0
    strikes = int(st.get("strikes") or 0)
    stuck_id = st.get("stuck_id")
    for row in rows:
        rid = int(row["id"])
        try:
            if _register(row):
                count += 1
            last_id, strikes, stuck_id = rid, 0, None
        except Exception:
            # 이 건에서 막혔다. 워터마크를 넘기지 않고 다음 주기에 다시 시도하되,
            # 같은 건이 계속 실패하면 뒤에 쌓인 접수까지 영영 막히므로 5회 뒤 건너뛴다.
            strikes = strikes + 1 if stuck_id == rid else 1
            stuck_id = rid
            if strikes >= MAX_STRIKES:
                logger.error("EasyQR 접수 #%d를 %d회 등록 실패 — 건너뛴다. "
                             "easyqr_db.consultations에서 직접 확인 필요", rid, strikes)
                last_id, strikes, stuck_id = rid, 0, None
                continue
            logger.exception("EasyQR 접수 #%d 등록 실패(%d/%d) — 다음 주기 재시도",
                             rid, strikes, MAX_STRIKES)
            break
        finally:
            _write_status(last_id=last_id, ok=True, registered=count,
                          strikes=strikes, stuck_id=stuck_id)
    if count:
        logger.info("EasyQR 전화상담 접수 %d건 인박스 등록", count)
    return count


def _loop():
    while True:
        time.sleep(POLL_SECONDS)
        try:
            poll_once()
        except Exception:                # 루프는 어떤 일이 있어도 죽지 않는다
            logger.exception("EasyQR 폴링 주기 실패")


def start_worker():
    """EasyQR 접수 브릿지 데몬 스레드 시작. 설정이 없으면 조용히 건너뛴다."""
    if not _enabled():
        logger.info("EasyQR 접수 연동 비활성 — .env EASYQR_DB_HOST/USER/PASS 미설정")
        return
    poll_once()                          # 기동 시 1회
    t = threading.Thread(target=_loop, name="easyqr-inbox", daemon=True)
    t.start()
    logger.info("EasyQR 접수 연동 시작 — %d초마다 %s.%s 폴링",
                POLL_SECONDS, os.getenv("EASYQR_DB_NAME", _DB_NAME), _TABLE)
