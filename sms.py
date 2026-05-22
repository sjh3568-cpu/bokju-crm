"""문자 발송 게이트웨이 — 5번 요청. 현재는 구조만 (발송사 미정).

사용자 결정(2026-05-22): 환자군별 정형 문구를 클릭 한 번으로 보내는 구조를
먼저 갖추고, 실제 발송사(알리고/솔라피 등)는 추후 결정한다.

발송사 결정 시 `send_sms()` 한 함수만 구현하면 전체 문자 기능이 작동한다.
그 전까지 `/api/sms/send`는 'manual' 모드로 동작 — 직원 휴대폰 문자앱을
`sms:` 링크로 열어 본문이 채워진 채 직원이 전송 버튼만 누른다.

보안 원칙(의료기관): 게이트웨이 연동 시에도 외부로 보내는 데이터는
수신 번호와 본문으로 최소화한다. 환자 식별정보를 본문에 과도하게 넣지 않는다.
"""
import os


def gateway_configured() -> bool:
    """문자 발송 게이트웨이 자격증명이 .env에 설정되어 있는지.

    발송사 연동 시 .env에 SMS_API_KEY / SMS_SENDER(발신번호) 등을 채운다.
    """
    return bool(os.getenv("SMS_API_KEY") and os.getenv("SMS_SENDER"))


def send_sms(to_phone: str, body: str) -> dict:
    """문자 1건 발송.

    Returns: {"ok": bool, "status": str, "error": str | None}

    발송사 미정 단계에서는 항상 미설정으로 반환한다.
    발송사 결정 후 이 함수 안에서 REST API를 호출하도록 구현하면 된다.
    예) 알리고: POST https://apis.aligo.in/send/  (key, user_id, sender, receiver, msg)
    """
    if not gateway_configured():
        return {
            "ok": False,
            "status": "not_configured",
            "error": "문자 발송사 미설정 — .env에 SMS_API_KEY/SMS_SENDER 입력 후 send_sms() 구현 필요",
        }
    # TODO: 발송사 결정 후 실제 REST 호출 구현.
    return {
        "ok": False,
        "status": "not_implemented",
        "error": "문자 발송 게이트웨이 미구현 — sms.py send_sms() 참고",
    }
