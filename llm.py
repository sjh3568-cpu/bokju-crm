"""Claude API — 통계 인사이트 자동 요약 (Phase 3).

집계 결과 dict를 받아 상담실장에게 도움 되는 한국어 코멘트를 생성한다.
환자 개인정보(이름/연락처)는 절대 전달하지 않는다 — 집계 카운트만.
"""
import json
import logging
import os
import time
from datetime import date

import requests

logger = logging.getLogger(__name__)

CLAUDE_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-4-6"
# 인사이트는 구조화 출력이 필요해 지원 모델을 따로 둔다 (Sonnet 4.6은 미지원)
INSIGHT_MODEL = "claude-sonnet-5"
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


MONTHLY_SYSTEM_PROMPT = """당신은 복주회복병원(인덕의료재단·입원전용 재활병원) 상담실 데이터를 임원에게 보고하는 분석가입니다.
A4 1장짜리 월간 보고서에 들어갈 해석 문구를 작성합니다. 표와 그래프는 이미 화면에 그려져 있으니,
당신은 **그 숫자가 무엇을 뜻하는지**만 씁니다.

절대 규칙:
1. **숫자를 나열하지 마라.** "A가 84건, B가 42건" 같은 문장은 금지. 표에 이미 있다.
   숫자는 주장을 뒷받침할 때만 1~2개 인용한다.
2. 모든 해석은 **비교**에서 나와야 한다. 전월 대비, 전년 동월 대비, 추이상의 위치,
   세그먼트 간 전환율 격차 — 이 넷 중 하나에 근거하지 않은 문장은 쓰지 마라.
3. **왜 그런지, 그래서 뭘 해야 하는지**까지 간다. 현상 서술에서 멈추지 마라.
4. 분포 순위("가장 많은 병명은 X")는 그 자체로 인사이트가 아니다.
   순위가 **바뀌었을 때**, 또는 **물량과 전환율이 어긋날 때**만 언급 가치가 있다.
5. 분석 대상 기간 밖의 달을 사실처럼 말하지 마라. 다음 달 액션을 제안하는 것은 되지만,
   데이터가 없는 달의 실적을 언급하면 안 된다.
6. 진행중(입원 미확정) 비율이 높은 달은 전환율이 아직 확정이 아니다.
   이 경우 전환율 하락을 단정하지 말고 미확정 물량을 함께 짚어라.
7. 의료법 준수 — "효과 보장", "완치", "최고" 같은 광고성 표현 금지.
   환자 개인 식별 시도 금지. 데이터에 없는 사실을 만들지 마라.
8. 가용 데이터 한계(가동률·재원일·매출 부재)는 임원이 이미 안다. 언급하지 마라."""


MONTHLY_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "string",
            "description": "이번 달을 한 문장으로 규정. 25자 내외. 예: '상담 역대 최다, 전환은 반대로 최저'",
        },
        "overview": {
            "type": "string",
            "description": "총평 3~4문장. 이번 달의 핵심 변화와 그 원인 추정, 마지막 문장은 실무 시사점.",
        },
        "trend_comment": {
            "type": "string",
            "description": "15개월 추이 그래프 해석 2~3문장. 이번 달이 추이상 어디에 있는지, 방향성이 무엇인지.",
        },
        "channel_comment": {
            "type": "string",
            "description": "유입경로별 물량과 전환율의 해석 2~3문장. 물량과 전환율이 어긋나는 채널을 반드시 짚을 것.",
        },
        "portfolio_comment": {
            "type": "string",
            "description": "병명그룹·연령 구성 변화 해석 2~3문장. 전년 동월 대비 구성이 어떻게 달라졌는지 중심.",
        },
        "alerts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "즉시 조치가 필요한 이상신호 0~3개. 각 40자 내외. 없으면 빈 배열.",
        },
    },
    "required": ["headline", "overview", "trend_comment", "channel_comment",
                 "portfolio_comment", "alerts"],
    "additionalProperties": False,
}


def summarize_monthly(data: dict) -> dict:
    """월간 보고서 해석 — 섹션별 코멘트 dict.

    data: aggregate_monthly() 반환 dict
    반환: MONTHLY_SCHEMA 형태의 dict (구조화 출력으로 스키마 보장)
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY 미설정")

    # 구조화 출력 지원 모델이어야 한다 (Sonnet 4.6은 미지원)
    model = os.getenv("CLAUDE_MODEL_INSIGHT", INSIGHT_MODEL)
    q = data.get("quality") or {}

    def cmp_rows(rows):
        return [{"항목": r["label"], "이번달": r["cur"], "전월": r["prev"],
                 "전년동월": r["yoy"], "전월대비%": r["delta_pct"],
                 "전년대비%": r["yoy_delta_pct"], "전환율%": r["rate"]}
                for r in rows]

    payload_data = {
        "대상월": f"{data['year']}-{data['month']:02d}",
        "비교기준": {"전월": data["prev_from"][:7], "전년동월": data["yoy_from"][:7]},
        "KPI": [{"항목": k["label"], "이번달": k["value"], "전월": k["prev"],
                 "전년동월": k["yoy"], "전월대비%": k["delta_pct"],
                 "전년대비%": k["yoy_delta_pct"]} for k in data["kpis"]],
        "월별추이_15개월": [{"월": x["label"], "상담": x["consults"],
                        "입원": x["admissions"], "전환율%": x["rate"]}
                       for x in data.get("trend", [])],
        "유입경로_그룹": cmp_rows(data["breakdowns"]["유입경로"]),
        "유입경로_세부_전환율": [{"채널": x["label"], "상담": x["total"],
                          "입원": x["completed"], "전환율%": x["rate"]}
                         for x in (data["channel"]["details"] or [])[:8]],
        "병명그룹": cmp_rows(data["breakdowns"]["병명그룹"]),
        "연령대": cmp_rows(data["breakdowns"]["연령대"]),
        "취소사유": [{"사유": x["label"], "건수": x["count"]}
                 for x in (data["by_rejection_reason"] or [])[:5]],
        "데이터성숙도": {
            "진행중_건수": q.get("pending"),
            "진행중_비율%": q.get("pending_rate"),
            "전환율_미확정": q.get("immature"),
            "취소사유_미입력": q.get("cancel_unlabeled"),
        },
    }

    user_prompt = f"""오늘: {date.today().isoformat()}
분석 대상: {data['year']}년 {data['month']}월 ({data['from']} ~ {data['to']})

{json.dumps(payload_data, ensure_ascii=False, indent=1)}

위 데이터를 해석해 월간 보고서 문구를 작성하세요."""

    payload = {
        "model": model,
        # Sonnet 5는 적응형 사고가 기본이라 사고 토큰이 max_tokens를 함께 소비한다.
        # 한도가 빠듯하면 JSON이 중간에 잘리므로 여유를 둔다.
        "max_tokens": 8000,
        "system": MONTHLY_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
        "output_config": {
            "effort": "medium",
            "format": {"type": "json_schema", "schema": MONTHLY_SCHEMA},
        },
    }
    return _post_json(payload, api_key)


def _post_json(payload: dict, api_key: str) -> dict:
    """구조화 출력 호출 — 스키마가 보장되므로 방어적 파싱 불필요."""
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(CLAUDE_URL, headers=headers, json=payload, timeout=90)
            if r.status_code == 429:
                time.sleep(RETRY_DELAY * attempt)
                continue
            r.raise_for_status()
            body = r.json()
            text = next(b["text"] for b in body["content"] if b["type"] == "text")
            return json.loads(text)
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
- hearing: 청력 상태 배열 (아래 [청력 보기] 중)
- diaper: 기저귀 사용 ("유" 또는 "무")
- wheelchair: 휠체어 이동 ("스스로" 또는 "도움")
- mobility_active: 화장실 등 능동 이동 배열 (아래 [능동이동 보기] 중, 예: ["워커"])
- activity_others: 기타 활동 상태 배열 (아래 [기타활동 보기] 중, 예: ["와상"])
- caregiver: 간병 형태 ("간병" 또는 "비간병")
- bed: 침상 형태 ("침대" 또는 "바닥생활")
- diet: 식이 형태 배열 (아래 [식이 보기] 중, 예: ["비강영양(L-tube)"])
- swallow_test: 연하검사 시행 여부 ("유"/"무", 메모에 분명할 때만)
- wound_care: 창상·피부 처치 배열 (아래 [창상 보기] 중, 예: ["욕창"])
- special_care: 특수처치·내성균 배열 (아래 [특수처치 보기] 중, 예: ["산소요법","MRSA"])
- therapy: 재활치료 종류 배열 (아래 [재활치료 보기] 중)
- arrange: 사전연명의료 등 배열 (아래 [기타확인 보기] 중, 예: ["DNR(consult)"])
- referral_detail: 유입경로 세부 배열 (아래 [유입경로 보기] 중 — 어떻게 알고 연락했는지)
- referrer_person: 소개해 준 사람 이름(문자열, 지인추천·직원소개 등)
- referrer_institution: 추천·의뢰한 기관/병원 이름(문자열, 들은 그대로 — 정식명 몰라도 됨)
- current_hospital: 현재 입원/입소 중인 병원·요양원 이름(전원·의뢰 오는 경우, 문자열)
- current_facility_type: 현재 위치 ("입원중"=병원 / "입소중"=요양원 / "집"=자택)
- cancer_site: 암 부위(문자열, 암 환자인 경우만)
- transport: 내원 교통수단 (아래 [교통 보기] 중, 명시된 경우만)
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
        f"[청력 보기] {', '.join(enums.get('hearing', []))}\n"
        f"[능동이동 보기] {', '.join(enums.get('mobility_active', []))}\n"
        f"[기타활동 보기] {', '.join(enums.get('activity_others', []))}\n"
        f"[식이 보기] {', '.join(enums.get('diet', []))}\n"
        f"[창상 보기] {', '.join(enums.get('wound_care', []))}\n"
        f"[특수처치 보기] {', '.join(enums.get('special_care', []))}\n"
        f"[재활치료 보기] {', '.join(enums.get('therapy', []))}\n"
        f"[기타확인 보기] {', '.join(enums.get('arrange', []))}\n"
        f"[유입경로 보기] {', '.join(enums.get('referral_detail', []))}\n"
        f"[교통 보기] {', '.join(enums.get('transport', []))}"
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
