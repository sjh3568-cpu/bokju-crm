"""홈페이지 문의 → 인박스 이메일 브릿지 (옴니채널).

카페24·아임웹 등 '빌더형' 홈페이지는 서버 코드를 못 건드려 우리 웹훅으로 직접
쏘게 만들 수 없다. 대신 이들 대부분은 **새 문의 접수 시 관리자 이메일 알림**을
보낸다. 이 워커가 그 알림 메일함을 IMAP으로 주기적으로 읽어 CRM 인박스에
자동 등록한다(채널=웹문의, 인바운드).

보안 이점: 사내망 → 메일서버로 '나가서 당겨오는' 아웃바운드 구조라
외부 포트를 열 필요가 없다(역프록시 노출 불필요). CLAUDE.md의 사내망 원칙에 부합.

동작:
  · IMAP_HOST/USER/PASS 가 모두 설정돼야 활성 (하나라도 없으면 no-op)
  · IMAP_POLL_SECONDS(기본 120초)마다 UNSEEN 메일 확인
  · IMAP_FROM_FILTER 가 있으면 그 발신자 메일만 처리(빌더 알림 주소)
  · 처리 성공한 메일만 읽음(Seen) 플래그 → 중복 등록 방지
  · 전화번호/이름을 본문에서 best-effort 추출, 매칭되면 환자 자동 연결

주의: 실패가 상담 업무를 막지 않도록 예외는 모두 로그만 남기고 다음 주기 재시도.
"""
import email
import imaplib
import logging
import os
import re
import threading
import time
from email.header import decode_header, make_header

import models

logger = logging.getLogger(__name__)

POLL_SECONDS = int(os.getenv("IMAP_POLL_SECONDS", "120"))
_PHONE_RE = re.compile(r"01[016-9][-\s.]?\d{3,4}[-\s.]?\d{4}")
_NAME_LABELS = ("이름", "성함", "성명", "고객명", "신청자", "보호자")
_TAG_RE = re.compile(r"<[^>]+>")


def _enabled() -> bool:
    return all(os.getenv(k) for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASS"))


def _decode(value) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _plain_text(msg) -> str:
    """메일 본문을 평문으로. text/plain 우선, 없으면 text/html에서 태그 제거."""
    html = None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if part.get_content_disposition() == "attachment":
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
            except Exception:
                continue
            if ctype == "text/plain":
                return text
            if ctype == "text/html" and html is None:
                html = text
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            text = (payload or b"").decode(charset, errors="replace")
        except Exception:
            text = msg.get_payload() or ""
        if msg.get_content_type() == "text/html":
            html = text
        else:
            return text
    if html:
        return _TAG_RE.sub(" ", html)
    return ""


def _extract_name(body: str) -> str | None:
    for line in body.splitlines():
        for label in _NAME_LABELS:
            if label in line:
                # "이름: 홍길동" / "성함 홍길동" 형태에서 값만
                val = re.split(r"[:：]\s*", line, maxsplit=1)
                cand = (val[1] if len(val) > 1 else line.replace(label, "")).strip()
                cand = cand.strip(" \t-·|")
                if 1 <= len(cand) <= 20:
                    return cand
    return None


def _process_message(raw: bytes) -> int | None:
    """메일 1건 → communications 인바운드 1건. 등록된 id 반환(무시 시 None)."""
    msg = email.message_from_bytes(raw)
    subject = _decode(msg.get("Subject")).strip()
    body = _plain_text(msg).strip()
    combined = f"{subject}\n{body}"
    phone_m = _PHONE_RE.search(combined)
    phone = phone_m.group(0) if phone_m else ""
    phone_norm = re.sub(r"[\s.]", "-", phone) if phone else ""
    name = _extract_name(body)
    if not subject and not body:
        return None
    pid = models.match_patient_by_phone(phone_norm) if phone_norm else None
    summary = (subject or "홈페이지 문의") + (f" · {name}" if name else "")
    comm_id = models.create_communication(
        patient_id=pid, channel="웹문의", direction="in",
        contact=phone_norm or name or None,
        summary=summary[:200], body=body[:4000] or subject[:4000],
        status="open", created_by="홈페이지(메일)",
    )
    try:
        models.log_audit(
            username="homepage-inbox", action="inbound_webhook",
            target_type="communication", target_id=comm_id, detail="웹문의/in(mail)",
        )
    except Exception:
        pass
    return comm_id


def poll_once() -> int:
    """메일함을 1회 확인해 새 문의를 등록. 등록 건수 반환."""
    if not _enabled():
        return 0
    host = os.getenv("IMAP_HOST")
    port = int(os.getenv("IMAP_PORT", "993"))
    user = os.getenv("IMAP_USER")
    pw = os.getenv("IMAP_PASS")
    folder = os.getenv("IMAP_FOLDER", "INBOX")
    from_filter = (os.getenv("IMAP_FROM_FILTER") or "").strip()
    count = 0
    try:
        conn = imaplib.IMAP4_SSL(host, port)
        conn.login(user, pw)
        conn.select(folder)
        criteria = ["UNSEEN"]
        if from_filter:
            criteria = ["UNSEEN", "FROM", from_filter]
        typ, data = conn.search(None, *criteria)
        if typ != "OK":
            conn.logout()
            return 0
        for num in (data[0].split() if data and data[0] else []):
            typ, msg_data = conn.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            try:
                cid = _process_message(msg_data[0][1])
                if cid:
                    count += 1
                    conn.store(num, "+FLAGS", "\\Seen")  # 성공분만 읽음 처리
            except Exception:
                logger.exception("문의 메일 처리 실패 — 다음 주기 재시도(읽음처리 안 함)")
        conn.logout()
    except Exception:
        logger.exception("홈페이지 메일함 폴링 실패 — 다음 주기 재시도")
        return count
    if count:
        logger.info("홈페이지 문의 %d건 인박스 등록", count)
    return count


def _loop():
    while True:
        time.sleep(POLL_SECONDS)
        poll_once()


def start_worker():
    """이메일 브릿지 데몬 스레드 시작. 설정이 없으면 조용히 건너뛴다."""
    if not _enabled():
        logger.info("홈페이지 메일 브릿지 비활성 — .env IMAP_HOST/USER/PASS 미설정")
        return
    poll_once()  # 기동 시 1회
    t = threading.Thread(target=_loop, name="homepage-inbox", daemon=True)
    t.start()
    logger.info("홈페이지 메일 브릿지 시작 — %d초마다 폴링(%s)",
                POLL_SECONDS, os.getenv("IMAP_FOLDER", "INBOX"))
