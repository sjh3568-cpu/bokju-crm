"""문자 발송 게이트웨이 — 5번 요청.

사용자 결정(2026-05-22): 환자군별 정형 문구를 클릭 한 번으로 보내는 구조를
먼저 갖추고, 실제 발송사(알리고/솔라피 등)는 추후 결정한다.
2026-09-11: 발송사 무관한 공통부(SMS/LMS 판별·테스트 전환·이력 필드)와
알리고 어댑터를 먼저 구현. 다른 발송사는 `_PROVIDERS`에 함수 하나만 추가한다.

.env
  SMS_PROVIDER   발송사 키 (`ppurio` | `aligo`). 비우면 manual 모드.
  SMS_API_KEY    발송사 API 키 (뿌리오: 연동관리의 '연동 개발 인증키')
  SMS_API_USER   발송사 계정 ID (뿌리오 계정, 알리고 user_id)
  SMS_SENDER     사전 등록된 발신번호 (병원 대표번호 등)
  SMS_API_BASE   발송사 API 호스트 재정의 (선택. 뿌리오 기본 https://message.ppurio.com)
  SMS_TEST_TO    테스트 전환 번호 — 채워져 있으면 **모든 문자가 이 번호로만** 간다.
                 실제 보호자에게 나가지 않으니 연동 검증 중에는 반드시 채울 것.

manual 모드(`gateway_configured()`가 False)에서는 `/api/sms/send`가 이력만 남기고
직원 휴대폰 문자앱을 `sms:` 링크로 연다(본문 채워진 채 직원이 전송만 누름).

보안 원칙(의료기관): 게이트웨이 연동 시에도 외부로 보내는 데이터는
수신 번호와 본문으로 최소화한다. 환자 식별정보를 본문에 과도하게 넣지 않는다.
"""
import os
import re

import requests

# 국내 발송사는 EUC-KR 바이트로 요금 단위를 나눈다 — 한글 2바이트, 영숫자 1바이트.
SMS_MAX_BYTES = 90
LMS_MAX_BYTES = 2000
_TIMEOUT = 10


# ─── 발송사 무관 공통부 ───

def body_bytes(body: str) -> int:
    """발송사 기준(EUC-KR) 바이트 수. EUC-KR에 없는 글자(이모지 등)는 2바이트로 센다."""
    n = 0
    for ch in body:
        try:
            n += len(ch.encode("euc-kr"))
        except UnicodeEncodeError:
            n += 2
    return n


def message_type(body: str) -> tuple[str, int]:
    """(msg_type, bytes) — 'SMS' | 'LMS' | 'TOO_LONG'. UI의 자수 표시와 발송 요청이 같이 쓴다."""
    n = body_bytes(body)
    if n <= SMS_MAX_BYTES:
        return "SMS", n
    if n <= LMS_MAX_BYTES:
        return "LMS", n
    return "TOO_LONG", n


def normalize_phone(phone: str) -> str:
    """'010-1234-5678' → '01012345678'. 발송사는 숫자만 받는다."""
    return re.sub(r"\D", "", phone or "")


def provider_name() -> str:
    return (os.getenv("SMS_PROVIDER") or "").strip().lower()


def test_to() -> str:
    return normalize_phone(os.getenv("SMS_TEST_TO") or "")


def gateway_configured() -> bool:
    """발송사 자격증명이 .env에 갖춰졌는지. 아니면 manual 모드."""
    return bool(provider_name() in _PROVIDERS
                and os.getenv("SMS_API_KEY") and os.getenv("SMS_SENDER"))


def gateway_info() -> dict:
    """화면 안내용 — 어느 발송사·발신번호·테스트 전환인지."""
    return {
        "ready": gateway_configured(),
        "provider": provider_name(),
        "sender": (os.getenv("SMS_SENDER") or "").strip(),
        "test_to": test_to(),
    }


def send_sms(to_phone: str, body: str, *, title: str | None = None) -> dict:
    """문자 1건 발송.

    Returns: {"ok": bool, "status": "sent"|"test"|"failed"|"not_configured",
              "msg_type": "SMS"|"LMS", "error": str|None,
              "provider_msg_id": str|None, "sent_to": str}
    `sent_to`는 실제로 나간 번호 — SMS_TEST_TO가 있으면 원래 번호 대신 그 번호.
    """
    msg_type, n = message_type(body)
    base = {"ok": False, "msg_type": msg_type, "error": None,
            "provider_msg_id": None, "sent_to": normalize_phone(to_phone)}
    if not gateway_configured():
        return {**base, "status": "not_configured",
                "error": "문자 발송사 미설정 — .env의 SMS_PROVIDER/SMS_API_KEY/SMS_SENDER"}
    if msg_type == "TOO_LONG":
        return {**base, "status": "failed",
                "error": f"본문이 {n}바이트 — LMS 한도 {LMS_MAX_BYTES}바이트 초과"}
    receiver = normalize_phone(to_phone)
    if not re.fullmatch(r"01[016789]\d{7,8}", receiver):
        return {**base, "status": "failed", "error": f"휴대폰 번호 형식이 아닙니다: {to_phone}"}

    redirected = test_to()
    if redirected and redirected != receiver:
        # 테스트 전환 — 원래 번호는 본문 머리에만 남기고 실제 전송은 테스트 번호로.
        body = f"[테스트 → {receiver}]\n{body}"
        msg_type, _ = message_type(body)
        if msg_type == "TOO_LONG":
            return {**base, "status": "failed", "error": "테스트 머리말을 붙이니 LMS 한도 초과"}
        receiver = redirected

    try:
        result = _PROVIDERS[provider_name()](receiver, body, msg_type, title)
    except requests.RequestException as e:
        return {**base, "status": "failed", "error": f"발송사 연결 실패: {e}"}
    except Exception as e:  # 응답 파싱 실패 등 — 이력에 남기고 화면에 보여준다
        return {**base, "status": "failed", "error": f"발송사 응답 처리 실패: {e}"}

    ok = bool(result.get("ok"))
    return {
        **base, "ok": ok, "msg_type": msg_type, "sent_to": receiver,
        "status": ("test" if redirected else "sent") if ok else "failed",
        "error": None if ok else (result.get("error") or "발송사 오류"),
        "provider_msg_id": result.get("provider_msg_id"),
    }


# ─── 발송사 어댑터 — (receiver, body, msg_type, title) → {"ok", "error", "provider_msg_id"} ───

def _send_aligo(receiver: str, body: str, msg_type: str, title: str | None) -> dict:
    """알리고 https://smartsms.aligo.in/smsapi.html
    POST https://apis.aligo.in/send/ (form) — result_code '1'이 성공, 음수는 오류.
    """
    data = {
        "key": os.getenv("SMS_API_KEY"),
        "user_id": os.getenv("SMS_API_USER") or "",
        "sender": normalize_phone(os.getenv("SMS_SENDER")),
        "receiver": receiver,
        "msg": body,
        "msg_type": msg_type,
    }
    if msg_type == "LMS":
        data["title"] = (title or "복주회복병원 안내")[:44]
    r = requests.post("https://apis.aligo.in/send/", data=data, timeout=_TIMEOUT)
    r.raise_for_status()
    res = r.json()
    ok = str(res.get("result_code")) == "1" and int(res.get("error_cnt") or 0) == 0
    return {
        "ok": ok,
        "error": None if ok else f"알리고 {res.get('result_code')}: {res.get('message')}",
        "provider_msg_id": str(res.get("msg_id")) if res.get("msg_id") else None,
    }


def _send_ppurio(receiver: str, body: str, msg_type: str, title: str | None) -> dict:
    """뿌리오(다우기술) 문자 API https://www.ppurio.com/send-api/guide
    1) POST /v1/token — Authorization: Basic base64("계정:인증키") → {"token","type","expired"}
    2) POST /v1/message — Bearer 토큰, JSON → {"code":1000,"description","messageKey"}
    토큰은 24시간 유효하지만 발송 빈도가 낮아 매번 새로 받는다(캐시 없음).
    사전 조건: 기업회원 전환, 발신번호 등록, **연동 IP 등록**(NAS 공인 IP), 초당 15회 제한.
    """
    import base64
    base = (os.getenv("SMS_API_BASE") or "https://message.ppurio.com").rstrip("/")
    account = os.getenv("SMS_API_USER") or ""
    basic = base64.b64encode(f"{account}:{os.getenv('SMS_API_KEY')}".encode()).decode()
    t = requests.post(f"{base}/v1/token", headers={"Authorization": f"Basic {basic}"},
                      timeout=_TIMEOUT)
    t.raise_for_status()
    token = t.json().get("token")
    if not token:
        return {"ok": False, "error": f"뿌리오 토큰 발급 실패: {t.text[:200]}", "provider_msg_id": None}

    payload = {
        "account": account,
        "messageType": msg_type,
        "content": body,
        "from": normalize_phone(os.getenv("SMS_SENDER")),
        "duplicateFlag": "N",
        "targetCount": 1,
        "targets": [{"to": receiver}],
        "refKey": f"bokju-{os.urandom(6).hex()}",
    }
    if msg_type == "LMS":
        payload["subject"] = (title or "복주회복병원 안내")[:30]
    r = requests.post(f"{base}/v1/message", json=payload,
                      headers={"Authorization": f"Bearer {token}"}, timeout=_TIMEOUT)
    # 4xx도 본문에 code/description이 실려 오므로 raise 대신 파싱한다.
    try:
        res = r.json()
    except ValueError:
        return {"ok": False, "error": f"뿌리오 HTTP {r.status_code}: {r.text[:200]}",
                "provider_msg_id": None}
    ok = str(res.get("code")) == "1000"
    return {
        "ok": ok,
        "error": None if ok else f"뿌리오 {res.get('code')}: {res.get('description')}",
        "provider_msg_id": res.get("messageKey"),
    }


_PROVIDERS = {
    "ppurio": _send_ppurio,
    "aligo": _send_aligo,
}
