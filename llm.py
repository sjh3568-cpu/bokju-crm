"""Claude API — 월간 보고서 AI 해석 + 상담일지 자동 채움.

AI 해석은 월간 보고서 한 곳으로 일원화한다 (전월·전년 대비가 월 단위에서만 성립).
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

MONTHLY_SYSTEM_PROMPT = """당신은 복주회복병원(인덕의료재단·입원전용 재활병원) 상담실 데이터를 임원에게 보고하는 분석가입니다.
A4 2장짜리 월간 보고서(1장 실적, 2장 모병원·협력 분석)에 들어갈 해석 문구를 작성합니다. 표와 그래프는 이미 화면에 그려져 있으니,
당신은 **그 숫자가 무엇을 뜻하는지**만 씁니다.

절대 규칙:
1. **숫자를 나열하지 마라.** "A가 84건, B가 42건" 같은 문장은 금지. 표에 이미 있다.
   숫자는 주장을 뒷받침할 때만 1~2개 인용한다.
2. 모든 해석은 **비교**에서 나와야 한다. 전월 대비, 전년 동월 대비, 추이상의 위치,
   세그먼트 간 전환율 격차 — 이 넷 중 하나에 근거하지 않은 문장은 쓰지 마라.
3. **왜 그런지, 그래서 뭘 해야 하는지**까지 간다. 현상 서술에서 멈추지 마라.
4. 분포 순위("가장 많은 병명은 X")는 그 자체로 인사이트가 아니다.
   순위가 **바뀌었을 때**, 또는 **물량과 전환율이 어긋날 때**만 언급 가치가 있다.
4-1. 지역·모병원·상담자 성과 테이블은 **전환율의 전월/전년 변화**를 먼저 보라.
   물량은 그대로인데 전환율만 떨어진 항목이 가장 중요한 신호다.
4-2. 상담자별 수치는 개인 평가가 아니다. 순위를 매기거나 특정인을 단정적으로
   평가하지 말고, 편차가 크면 배분·교육·케이스 난이도 관점에서만 언급하라.
5. 분석 대상 기간 밖의 달을 사실처럼 말하지 마라. 다음 달 액션을 제안하는 것은 되지만,
   데이터가 없는 달의 실적을 언급하면 안 된다.
6. 진행중(입원 미확정) 비율이 높은 달은 전환율이 아직 확정이 아니다.
   이 경우 전환율 하락을 단정하지 말고 미확정 물량을 함께 짚어라.
7. 의료법 준수 — "효과 보장", "완치", "최고" 같은 광고성 표현 금지.
   환자 개인 식별 시도 금지. 데이터에 없는 사실을 만들지 마라.
8. 가용 데이터 한계(가동률·재원일·매출 부재)는 임원이 이미 안다. 언급하지 마라.
9. 모병원·협력 분석(2페이지)은 "협력기관 방문·연락을 어디에 해야 하나"에 답하는 섹션이다.
   기관연계(모병원 진료협력팀이 보내준 것)와 직접 문의(환자·보호자가 직접 찾아온 것)를 구분하고,
   협력기관인데 이번 달 상담이 없는 곳, 협력기관이 아닌데 상담·입원이 많은 곳을 우선 짚어라."""


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
        "pipeline_comment": {
            "type": "string",
            "description": "상담 결과 단계·요일 분포·모병원 유입 해석 2~3문장. 미확정 물량이 어디에 쌓여 있는지, 상담량의 요일 편중, 신규 모병원 유입의 의미 중심.",
        },
        "operation_comment": {
            "type": "string",
            "description": "운영·데이터 품질 해석 2~3문장. 지역·모병원·상담자 성과의 전환율 변화, 요일 편중, 입력 누락 중 실제 조치가 가능한 것만.",
        },
        "hospital_comment": {
            "type": "string",
            "description": "모병원·협력 분석 해석 3~4문장. ① 환자가 어디서 오나(종별·지역 구성, 상위 병원 집중도) ② 협력 활동이 효과가 있나(기관연계 상담·입원의 전월 대비·6개월 추이, 협력기관 중 실제로 보내준 곳과 조용한 곳) ③ 무엇이 달라졌나(늘어난·줄어든·새로 생긴 병원) 순서로. 병원명은 데이터에 있는 이름 그대로 쓰고, 방문·연락이 필요한 곳을 한 곳 이상 짚을 것.",
        },
        "alerts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "즉시 조치가 필요한 이상신호 0~3개. 각 40자 내외. 없으면 빈 배열.",
        },
    },
    "required": ["headline", "overview", "trend_comment", "channel_comment",
                 "portfolio_comment", "pipeline_comment", "operation_comment", "hospital_comment", "alerts"],
    "additionalProperties": False,
}


def _hospital_section_payload(hs):
    """hospital_analysis.monthly_section() 결과를 LLM용으로 압축."""
    if not hs:
        return None
    def hosp(h):
        return {"병원": h.get("official_name") or h.get("name"), "종별": h.get("kind"),
                "상담": h.get("referrals"), "전월": h.get("prev_referrals"), "입원": h.get("admissions"),
                "협력기관": bool(h.get("partner_id")),
                "주요질환": [f"{d['short']} {d['pct']}%" for d in (h.get("diseases") or [])[:2]]}
    lk = hs.get("linkage") or {}
    return {
        "모병원_수": {"이번달": hs.get("hospital_count"), "전월": hs.get("prev_hospital_count")},
        "종별_구성%": [{"종별": k["label"], "이번달": k["pct"], "전월": k["prev_pct"], "상담": k["count"]} for k in hs.get("kinds") or []],
        "지역_구성%": [{"지역": r["label"], "이번달": r["pct"], "전월": r["prev_pct"]} for r in (hs.get("regions") or [])[:5]],
        "상위_모병원": [hosp(h) for h in (hs.get("top") or [])[:8]],
        "기관연계": {"상담": lk.get("linked", {}).get("referrals"), "상담_전월대비": lk.get("linked", {}).get("d_referrals"),
                  "입원": lk.get("linked", {}).get("admissions"), "입원_전월대비": lk.get("linked", {}).get("d_admissions")},
        "직접문의": {"상담": lk.get("direct", {}).get("referrals"), "상담_전월대비": lk.get("direct", {}).get("d_referrals"),
                  "입원": lk.get("direct", {}).get("admissions"), "입원_전월대비": lk.get("direct", {}).get("d_admissions")},
        "기관연계_추이": [{"월": t["month"], "상담": t["linked_referrals"], "입원": t["linked_admissions"]} for t in hs.get("trend") or []],
        "협력기관_보내준곳": [{"기관": p["name"], "상담": p["referrals"], "전월": p["prev_referrals"], "입원": p["admissions"]} for p in hs.get("partners_active") or []],
        "협력기관_이달_상담없음": [p["name"] for p in hs.get("partners_silent") or []],
        "늘어난_곳": [hosp(h) for h in hs.get("ups") or []],
        "줄어든_곳": [hosp(h) for h in hs.get("downs") or []],
        "새로_생긴_곳": [hosp(h) for h in hs.get("news") or []],
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
        "성과_전환율_3기간": {
            cat: [{"항목": r["label"], "상담": r["cur"], "전환율%": r["cur_rate"],
                   "전월상담": r["prev"], "전월전환율%": r["prev_rate"],
                   "전년상담": r["yoy"], "전년전환율%": r["yoy_rate"]}
                  for r in rows]
            for cat, rows in (data.get("performance") or {}).items()
        },
        "운영지표": {
            cat: [{"구분": r["label"], "이번달": r["cur"], "전월": r["prev"]} for r in rows]
            for cat, rows in (data.get("ops") or {}).items()
        },
        "상담결과_단계": [{"단계": x["label"], "이번달": x["cur"], "전월": x["prev"]}
                     for x in (data.get("pipeline") or [])],
        "신규_모병원_수": data.get("new_hospitals"),
        "모병원_의뢰_Top": [{"모병원": x["label"], "의뢰": x["count"]}
                      for x in (data.get("by_source_hospital") or [])[:6]],
        "재단시설_연계": data.get("referral_capture"),
        "모병원_협력_분석": _hospital_section_payload(data.get("hospital_section")),
        "입력_누락률": [{"항목": x["label"], "누락%": x["rate"]}
                   for x in (q.get("missing_fields") or [])],
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
        # 한도에 걸리면 구조화 출력이 JSON을 억지로 닫아 뒤쪽 필드가 빈 문자열로
        # 나오므로(에러가 아니라 조용히 비어버린다) 넉넉히 잡는다.
        "max_tokens": 16000,
        "system": MONTHLY_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
        "output_config": {
            "effort": "medium",
            "format": {"type": "json_schema", "schema": MONTHLY_SCHEMA},
        },
    }
    return _post_json(payload, api_key)


def _post_json(payload: dict, api_key: str, *, extra_headers: dict | None = None,
               read_timeout: int = 90) -> dict:
    """구조화 출력 호출 — 스키마가 보장되므로 방어적 파싱 불필요.

    스트리밍으로 받는다. max_tokens가 크고 사고 시간이 길어 단일 응답을 기다리면
    읽기 타임아웃에 걸린다. 스트리밍은 청크 단위로 타임아웃이 갱신돼 안전하다.
    """
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    body = dict(payload, stream=True)

    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with requests.post(CLAUDE_URL, headers=headers, json=body,
                               stream=True, timeout=(10, read_timeout)) as r:
                if r.status_code == 429:
                    time.sleep(RETRY_DELAY * attempt)
                    continue
                r.raise_for_status()
                chunks: list[str] = []
                stop_reason = None
                for line in r.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data: "):
                        continue
                    event = json.loads(line[6:])
                    etype = event.get("type")
                    if etype == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            chunks.append(delta.get("text", ""))
                    elif etype == "message_delta":
                        stop_reason = (event.get("delta") or {}).get("stop_reason")
                    elif etype == "error":
                        raise RuntimeError(event.get("error", {}).get("message", "stream error"))
                if stop_reason == "max_tokens":
                    # 구조화 출력은 이 경우에도 파싱 가능한 JSON을 돌려주지만
                    # 뒤쪽 필드가 비어 있다. 조용히 넘기지 않는다.
                    logger.warning("Claude 응답이 max_tokens에서 잘림 — 뒤쪽 필드가 비었을 수 있음")
                text = "".join(chunks).strip()
                if not text:
                    raise RuntimeError("빈 응답")
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


# ───────────────────── 팩스·문서 판독 (fax_inbox.py) ─────────────────────
# 모병원에서 온 팩스(진료의뢰서·소견서·검사결과 등) PDF/이미지를 통째로 보내
# 환자 이름·주병명·보낸 곳·핵심 요약을 구조화해 받는다. 파일명 정리와 자료함 카드에 쓴다.
# 주의: 이 경로는 환자 식별정보가 담긴 문서 자체가 API로 나간다(월간 보고서와 다름).
# FAX_AI_ENABLED=0 이면 호출하지 않는다.

FAX_MODEL = "claude-opus-5"
FAX_MAX_BYTES = 25 * 1024 * 1024          # API 요청 32MB 한도 아래
FAX_MEDIA_TYPES = {
    ".pdf": ("document", "application/pdf"),
    ".jpg": ("image", "image/jpeg"), ".jpeg": ("image", "image/jpeg"),
    ".png": ("image", "image/png"), ".gif": ("image", "image/gif"), ".webp": ("image", "image/webp"),
}

FAX_SYSTEM_PROMPT = """당신은 복주회복병원(입원 전용 재활병원) 상담실의 문서 판독 보조 AI입니다.
모병원(급성기 병원)에서 팩스로 보내온 환자 관련 서류(진료의뢰서·소견서·진단서·검사결과·간호정보조사지 등)를 읽고,
상담사가 파일을 열지 않아도 상황을 파악할 수 있도록 핵심만 구조화합니다.

규칙:
1. 문서에 **실제로 적힌 내용만** 씁니다. 추측·보완·창작 금지. 못 읽거나 없는 항목은 빈 값으로 둡니다.
2. 이름·생년월일·병명은 문서 표기 그대로 옮깁니다(오타 교정 금지). 병명은 한글 진단명이 있으면 한글, 없으면 영문·ICD 그대로.
3. 주병명(main_diagnosis)은 이번 전원·의뢰의 **주된 사유가 되는 진단 하나**를 15자 이내로. 파일명에 들어가므로 특수문자 없이.
4. summary는 상담사가 30초 안에 읽을 3~6개 항목. 각 항목은 한 문장. "왜 보냈나 → 현재 상태 → 주의할 점" 순서.
5. 의료법 위반 표현(효과 단정·완치 보장) 금지. 재활 가능성·예후를 문서에 없는데 평가하지 마세요.
6. 팩스 화질이 낮아 읽기 어려운 부분은 confidence를 낮추고 notes에 무엇을 못 읽었는지 적습니다."""

FAX_SCHEMA = {
    "type": "object",
    "properties": {
        "document_type": {"type": "string",
                          "description": "서류 종류: 진료의뢰서 / 소견서 / 진단서 / 검사결과 / 간호정보조사지 / 투약기록 / 기타"},
        "patient_name": {"type": "string", "description": "환자 이름(문서 표기 그대로). 없으면 빈 문자열"},
        "birth_date": {"type": "string", "description": "생년월일 YYYY-MM-DD. 모르면 빈 문자열"},
        "age": {"type": "string", "description": "나이(숫자만). 모르면 빈 문자열"},
        "sex": {"type": "string", "description": "남 / 여 / 빈 문자열"},
        "main_diagnosis": {"type": "string", "description": "주병명 1개, 15자 이내, 특수문자 없이"},
        "diagnoses": {"type": "array", "items": {"type": "string"}, "description": "문서에 적힌 진단명 전체(주병명 포함)"},
        "sender_hospital": {"type": "string", "description": "보낸 병원(모병원) 이름. 없으면 빈 문자열"},
        "sender_department": {"type": "string", "description": "보낸 진료과·부서"},
        "sender_contact": {"type": "string", "description": "회신 전화·팩스 번호"},
        "doc_date": {"type": "string", "description": "문서 작성일 또는 팩스 수신일 YYYY-MM-DD. 모르면 빈 문자열"},
        "referral_reason": {"type": "string", "description": "의뢰·전원 사유 한 문장"},
        "current_status": {"type": "string", "description": "의식·마비·보행·식이·배뇨 등 현재 상태 요약 1~2문장"},
        "precautions": {"type": "array", "items": {"type": "string"},
                        "description": "주의사항: 감염(MRSA 등)·튜브(L-tube, 기관절개)·욕창·DNR·알레르기·격리 등. 없으면 빈 배열"},
        "medications": {"type": "array", "items": {"type": "string"}, "description": "주요 투약(있으면)"},
        "summary": {"type": "array", "items": {"type": "string"}, "description": "핵심 요약 3~6항목"},
        "confidence": {"type": "string", "description": "high / medium / low — 판독 신뢰도"},
        "notes": {"type": "string", "description": "못 읽은 부분·판단이 필요한 점. 없으면 빈 문자열"},
    },
    "required": ["document_type", "patient_name", "birth_date", "age", "sex", "main_diagnosis",
                 "diagnoses", "sender_hospital", "sender_department", "sender_contact", "doc_date",
                 "referral_reason", "current_status", "precautions", "medications", "summary",
                 "confidence", "notes"],
    "additionalProperties": False,
}


def fax_ai_enabled() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY")) and os.getenv("FAX_AI_ENABLED", "1") == "1"


def fax_max_pages() -> int:
    try:
        return max(1, int(os.getenv("FAX_AI_MAX_PAGES", "20")))
    except ValueError:
        return 20


def _pdf_head(data: bytes, max_pages: int) -> tuple[bytes, int, int]:
    """PDF 앞 max_pages쪽만 남긴 바이트, (총 쪽수, 보낸 쪽수). pypdf가 없거나 못 읽으면 원본 그대로."""
    try:
        import io
        from pypdf import PdfReader, PdfWriter
        reader = PdfReader(io.BytesIO(data))
        total = len(reader.pages)
        if total <= max_pages:
            return data, total, total
        writer = PdfWriter()
        for i in range(max_pages):
            writer.add_page(reader.pages[i])
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue(), total, max_pages
    except Exception as e:                       # pypdf 미설치·암호화·손상 — 통째로 보낸다
        logger.warning("PDF 쪽 자르기 실패, 전체를 보냅니다: %s", e)
        return data, 0, 0


def analyze_document(path: str, *, hint: str = "") -> dict:
    """팩스 PDF/이미지 1개 → FAX_SCHEMA dict (+ _pages_total/_pages_sent). 실패 시 RuntimeError.

    보통 팩스는 10~20쪽이라 기본 FAX_AI_MAX_PAGES=20이면 통째로 읽고, 책 두께(수백 쪽)로 오면 앞 20쪽만
    보낸다 — 환자·진단·의뢰 사유는 앞장에 있고, 뒷장까지 보내면 시간·비용만 커진다. 파일 자체는 통째로 보관된다.
    """
    import base64
    from pathlib import Path

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY 미설정")
    p = Path(path)
    kind = FAX_MEDIA_TYPES.get(p.suffix.lower())
    if not kind:
        raise RuntimeError(f"판독 불가 형식: {p.suffix}")
    block_type, media_type = kind
    raw = p.read_bytes()
    pages_total = pages_sent = 0
    if block_type == "document":
        raw, pages_total, pages_sent = _pdf_head(raw, fax_max_pages())
    # 크기 검사는 앞쪽만 잘라낸 뒤에 — 책 두께 팩스(수백 쪽)도 앞 N쪽은 판독된다
    if len(raw) > FAX_MAX_BYTES:
        raise RuntimeError(f"보낼 분량이 너무 큽니다 ({len(raw) // (1024 * 1024)}MB) — FAX_AI_MAX_PAGES를 줄이세요")
    data = base64.standard_b64encode(raw).decode("ascii")
    page_note = ""
    if pages_total and pages_sent < pages_total:
        page_note = (f"이 문서는 총 {pages_total}쪽이지만 앞 {pages_sent}쪽만 첨부했습니다. "
                     f"뒷부분(검사·투약 상세 등)은 못 본 것이므로 notes에 그 사실을 적으세요.\n")
    user_text = (f"오늘: {date.today().isoformat()}\n"
                 f"파일명: {p.name}\n" + (f"참고: {hint}\n" if hint else "") + page_note +
                 "위 문서를 읽고 스키마대로 정리하세요.")
    payload = {
        "model": os.getenv("CLAUDE_MODEL_FAX", FAX_MODEL),
        "max_tokens": 8000,
        "system": FAX_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": [
            {"type": block_type, "source": {"type": "base64", "media_type": media_type, "data": data}},
            {"type": "text", "text": user_text},
        ]}],
        "output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": FAX_SCHEMA}},
        # 안전 분류기가 거절하면 같은 요청을 대체 모델로 이어 받는다(서버측 폴백).
        "fallbacks": "default",
    }
    result = _post_json(payload, api_key,
                        extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
                        read_timeout=180)
    result["_pages_total"] = pages_total
    result["_pages_sent"] = pages_sent
    return result
