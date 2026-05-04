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
