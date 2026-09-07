"""Claude API — 통계 인사이트 자동 요약 (Phase 3).

집계 결과 dict를 받아 상담실장에게 도움 되는 한국어 코멘트를 생성한다.
환자 개인정보(이름/연락처)는 절대 전달하지 않는다 — 집계 카운트만.
"""
import json
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

CLAUDE_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_RETRIES = 2
RETRY_DELAY = 4

SYSTEM_PROMPT = """당신은 재활병원 상담실의 통계 분석가입니다.
복주회복병원(인덕의료재단) 상담실은 입원 전용 재활병원으로, 입원경로/거주지/병명/보험/연령 분포를 본다.

다음 원칙으로 응답:
1. **3~5문장**의 자연스러운 한국어 단락 형식
2. 가장 눈에 띄는 변화·비율·분포만 짚는다 (모든 항목 나열 X)
3. 입원경로/병명/거주지/연령 중 가장 의미 있는 1~2개 인사이트에 집중
4. **숫자는 정확히 인용** (예: "온라인 유입이 12건으로 전체의 40%")
5. 마지막 1문장은 **실무 시사점** (예: "온라인 카페 활동을 강화할 시점", "고관절 골절 환자가 증가 추세")
6. 의료법 준수 — "효과 보장", "완치", "최고" 같은 광고성 표현 금지
7. 환자 개인 식별 시도 금지, 추측·과장 금지, 데이터에 없는 정보 만들지 말 것
8. JSON·마크다운 X. 줄글로만."""


def summarize_stats(data: dict, *, date_from: str | None, date_to: str | None) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY 미설정")

    model = os.getenv("CLAUDE_MODEL", DEFAULT_MODEL)
    summary_payload = _compact(data)

    user_prompt = f"""기간: {date_from or '전체'} ~ {date_to or '전체'}
총 상담: {data['summary']['total']}건, 입원예정 등록: {data['summary']['planned']}건 ({data['summary']['plan_rate']}%)

집계 데이터:
{json.dumps(summary_payload, ensure_ascii=False, indent=2)}

위 데이터를 기반으로 상담실장이 보기 좋은 인사이트 단락(3~5문장)을 작성하세요."""

    payload = {
        "model": model,
        "max_tokens": 700,
        "temperature": 0.3,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(CLAUDE_URL, headers=headers, json=payload, timeout=60)
            if r.status_code == 429:
                time.sleep(RETRY_DELAY * attempt)
                continue
            r.raise_for_status()
            data = r.json()
            return data["content"][0]["text"].strip()
        except Exception as e:
            last_err = e
            logger.warning(f"Claude 호출 실패 (시도 {attempt}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"Claude 호출 실패: {last_err}")


MONTHLY_SYSTEM_PROMPT = """당신은 복주회복병원(인덕의료재단·입원전용 재활병원) 상담실 데이터를 보고 임원에게 1페이지 월간 인사이트를 보고하는 분석가입니다.

원칙:
1. **3~5문장**의 단락 형식. 줄글로만 (마크다운/JSON 금지).
2. CEO가 5초 안에 상황을 이해할 수 있도록 핵심만:
   - 가장 큰 변화(전월 대비 ±%) 1~2개
   - 주목할 이상신호 또는 기회
   - 마지막 1문장은 **실무 시사점** (예: "○○ 채널 강화 필요", "고비용 사유가 X건으로 1순위 — 진료비 안내 강화")
3. 숫자는 **정확히** 인용. 추측·과장 금지.
4. 의료법 준수 — "효과 보장", "완치" 같은 광고성 표현 금지.
5. 환자 개인 식별 시도 금지. 데이터에 없는 정보 만들지 말 것.
6. 가용 데이터 한계(가동률·재원일·매출 부재)는 임원이 이미 알고 있으니 구태여 언급 X."""


def summarize_monthly(data: dict) -> str:
    """월간 보고서 인사이트 — 이번 달·전월 비교 + 채널 ROI + 사유 Top.

    data: aggregate_monthly() 반환 dict
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY 미설정")

    model = os.getenv("CLAUDE_MODEL", DEFAULT_MODEL)

    def top(arr, n=5):
        return [{"label": x["label"], "count": x["count"]} for x in (arr or [])[:n] if x.get("count")]

    summary_payload = {
        "이번달": f"{data['year']}-{data['month']:02d}",
        "KPI": [
            {"항목": k["label"], "현재": k["value"], "전월": k["prev"], "변화율%": k["delta_pct"]}
            for k in data["kpis"]
        ],
        "채널_그룹_ROI": [
            {"채널": x["label"], "상담": x["total"], "입원완료": x["completed"], "전환율%": x["rate"]}
            for x in (data["channel"]["groups"] or [])[:5]
        ],
        "채널_세부_ROI": [
            {"채널": x["label"], "상담": x["total"], "입원완료": x["completed"], "전환율%": x["rate"]}
            for x in (data["channel"]["details"] or [])[:8]
        ],
        "모병원_Top": top(data["by_source_hospital"], 8),
        "취소사유_Top": top(data["by_rejection_reason"], 8),
        "병명그룹": top(data["by_disease_group"], 5),
        "보험유형": top(data["by_insurance"], 5),
        "연령대": top(data["by_age"], 8),
    }

    user_prompt = f"""다음은 {data['year']}년 {data['month']}월 상담실 데이터 요약(전월 대비 포함)입니다.
임원 보고용 월간 인사이트 단락(3~5문장)을 작성하세요.

{json.dumps(summary_payload, ensure_ascii=False, indent=2)}"""

    payload = {
        "model": model,
        "max_tokens": 800,
        "temperature": 0.3,
        "system": MONTHLY_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(CLAUDE_URL, headers=headers, json=payload, timeout=60)
            if r.status_code == 429:
                time.sleep(RETRY_DELAY * attempt)
                continue
            r.raise_for_status()
            d = r.json()
            return d["content"][0]["text"].strip()
        except Exception as e:
            last_err = e
            logger.warning(f"Claude 호출 실패 (시도 {attempt}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"Claude 호출 실패: {last_err}")


EXTRACT_SYSTEM_PROMPT = """당신은 복주회복병원(입원 전용 재활병원) 상담실의 입력 보조 AI입니다.
상담사가 통화하며 급히 적은 '통화 메모'를 읽고, 상담일지 양식의 해당 칸에 들어갈 값을
구조화해 뽑아냅니다.

절대 규칙:
1. **오직 JSON 객체 하나만** 출력. 마크다운·설명·코드펜스 금지.
2. 메모에서 **명시적으로 확인되는 값만** 채운다. 추측·창작 금지. 모르면 그 키를 아예 넣지 않는다.
3. 열거형은 반드시 주어진 보기 중 하나와 **정확히 일치**하는 문자열만. 애매하면 생략.
4. 개인정보를 지어내지 말 것. 메모에 없는 이름·번호·주소를 만들지 말 것.
5. 의료법 위반 표현(효과 단정·완치 보장 등) 금지.

출력 가능한 키와 형식(모르는 키는 생략):
- name: 환자 이름(문자열)
- gender: "남" 또는 "여"
- age: 환자 나이(정수, 만나이 기준 숫자만)
- guardian_name: 보호자 이름
- guardian_relation: 보호자 관계(예: 배우자/자녀/형제/부모)
- guardian_phone: 연락처(010-0000-0000 형식으로 정규화)
- residence_sido: 거주 시/도 (아래 시도 보기 중 하나)
- residence_sigungu: 거주 시/군/구 (문자열)
- insurance_type: 보험유형 (아래 보기 중 하나)
- consult_channel: 상담방법 (아래 보기 중 하나)
- attending_doctor: 희망/담당 주치의 (아래 보기 중 하나, 메모에 명시된 경우만)
- disease_onset: 발병일 (YYYY-MM-DD, 명확할 때만) 또는 발병 시점 설명(문자열, 예: "3주 전")
- planned_admission_date: 입원예정일 (YYYY-MM-DD, 명확할 때만)
- diseases: 해당하는 병명들의 배열 — 반드시 아래 [병명 보기] 중 정확히 일치하는 값만.
  (예: 뇌경색+우측 편마비 → ["뇌경색", "마비-편마비 우"]) 해당 없으면 생략.
- diagnosis: 병명 보기에 없는 추가 진단·상세(문자열) — 상담일지 병명 상세칸용 (예: 연하장애, 욕창)
- consciousness: 의식 상태 (아래 [의식 보기] 중 하나)
- conversation: 대화 수준 (아래 [대화 보기] 중 하나)
- diaper: 기저귀 사용 ("유" 또는 "무")
- wheelchair: 휠체어 이동 ("스스로" 또는 "도움")
- activity_others: 기타 활동 상태 배열 (아래 [기타활동 보기] 중, 예: ["와상"])
- admission_purpose: 입원 목적/주요 재활 목표(문자열)
- consult_result: 상담 결과 (아래 보기 중 하나, 메모에 분명할 때만)
- summary: 통화 핵심 요약 2~4문장(문자열). 환자 상태·요청사항·다음 조치 중심."""


def extract_consultation(memo: str, *, enums: dict) -> dict:
    """통화 메모 → 상담일지 필드 초안(dict). 값 검증은 호출측(app)이 config로 수행.

    enums: {"insurance": [...], "channel": [...], "doctor": [...],
            "result": [...], "sido": [...]} — 프롬프트에 보기로 제시.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY 미설정")
    memo = (memo or "").strip()
    if not memo:
        return {}

    model = os.getenv("CLAUDE_MODEL", DEFAULT_MODEL)
    guide = (
        f"[보험유형 보기] {', '.join(enums.get('insurance', []))}\n"
        f"[상담방법 보기] {', '.join(enums.get('channel', []))}\n"
        f"[주치의 보기] {', '.join(enums.get('doctor', []))}\n"
        f"[상담결과 보기] {', '.join(enums.get('result', []))}\n"
        f"[시도 보기] {', '.join(enums.get('sido', []))}\n"
        f"[병명 보기] {', '.join(enums.get('diseases', []))}\n"
        f"[의식 보기] {', '.join(enums.get('consciousness', []))}\n"
        f"[대화 보기] {', '.join(enums.get('conversation', []))}\n"
        f"[기타활동 보기] {', '.join(enums.get('activity_others', []))}"
    )
    user_prompt = f"{guide}\n\n[통화 메모]\n{memo}\n\n위 메모에서 확인되는 값만 JSON으로 출력하세요."

    payload = {
        "model": model,
        "max_tokens": 1200,
        "temperature": 0,
        "system": EXTRACT_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(CLAUDE_URL, headers=headers, json=payload, timeout=60)
            if r.status_code == 429:
                time.sleep(RETRY_DELAY * attempt)
                continue
            r.raise_for_status()
            text = r.json()["content"][0]["text"].strip()
            return _parse_json_object(text)
        except Exception as e:
            last_err = e
            logger.warning(f"Claude 추출 실패 (시도 {attempt}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"Claude 호출 실패: {last_err}")


def _parse_json_object(text: str) -> dict:
    """LLM 응답에서 JSON 객체를 방어적으로 추출 (코드펜스·앞뒤 잡텍스트 제거)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if "```" in text[3:] else text.strip("`")
        text = text.split("\n", 1)[-1] if text.lower().startswith("json") else text
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError):
        logger.warning("LLM JSON 파싱 실패")
        return {}


def _compact(data: dict) -> dict:
    """프롬프트 토큰 절약 — 카운트가 0인 라벨/긴 꼬리 제거."""
    def top(arr, n=8):
        return [{"label": x["label"], "count": x["count"]} for x in (arr or [])[:n] if x.get("count")]
    return {
        "입원경로_그룹": top(data.get("by_referral_type"), 5),
        "입원경로_세부": top(data.get("by_referral_detail"), 8),
        "병명_그룹": top(data.get("by_disease_group"), 5),
        "병명_상위": top(data.get("by_disease"), 10),
        "거주지_시도": top(data.get("by_sido"), 8),
        "거주지_시군구": top(data.get("by_sigungu_top"), 8),
        "보험유형": top(data.get("by_insurance"), 8),
        "연령대": top(data.get("by_age"), 8),
        "상담방법": top(data.get("by_channel"), 5),
        "상담자별": top(data.get("by_counselor"), 8),
        "성별": top(data.get("by_gender"), 5),
    }
