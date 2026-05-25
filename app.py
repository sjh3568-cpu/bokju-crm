"""Flask 앱 — 복주회복병원 상담실 CRM (bokju-crm).

진입점. 인증·상담 등록/목록/상세·자동완성·통계 API를 한 파일에 모음.
규모가 커지면 Blueprint로 쪼갤 것 (현재는 cafe-helper 스타일 단일 파일).
"""
import csv
import io
import logging
import os
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlsplit

from dotenv import load_dotenv
from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)

import models
from auth import (
    admin_required, authenticate, current_user, is_locked_out,
    login_required, login_user, logout_user,
)
from config import (
    ACTIVITY_ACTIVE_OPTIONS, ACTIVITY_DIAPER_OPTIONS, ACTIVITY_OTHERS_OPTIONS,
    ACTIVITY_WHEELCHAIR_OPTIONS, ADMISSION_DOCS, ADMISSION_STATUSES,
    ADMISSION_EVENT_TYPES, ATTENDING_DOCTORS,
    BED_OPTIONS, CAREGIVER_OPTIONS,
    CONSCIOUSNESS_MAIN_OPTIONS, CONSULT_CHANNELS, CONVERSATION_LEVEL_OPTIONS,
    CONSULT_RESULTS, CONSULT_RESULT_REASON_LABELS, REJECTION_REASONS,
    COMM_CHANNELS, COMM_INBOUND_CHANNELS,
    COST_GUIDANCE_OPTIONS, CURRENT_LOCATION_TYPES, DIET_TYPES, DIET_LAYOUT,
    DISEASES_CHECKLIST, DISEASES_GROUPS, GUARDIAN_RELATION_SUGGESTIONS,
    HEARING_OPTIONS, INFO_PROVIDED_OPTIONS,
    COUNSELORS, DISEASES_LAYOUT, OTHERS_LAYOUT,
    INSURANCE_TYPES, OTHERS_CHECKLIST, REFERRAL_SOURCE_GROUPS, REFERRAL_TYPES,
    LIFECYCLE_STAGES, LIFECYCLE_EVENT_TYPES,
    SIDO_LIST, SIGUNGU_INDEX, SIGUNGU_LIST,
    SMS_TEMPLATE_GROUPS, SMS_PLACEHOLDERS,
    SPECIAL_CARE_OPTIONS, SPECIAL_CARE_NOTE_FIELDS,
    THERAPY_OPTIONS, TRANSPORT_OPTIONS, WOUND_CARE_OPTIONS, WOUND_CARE_NOTE_FIELDS,
)
import sms as sms_gateway

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(hours=int(os.getenv("SESSION_HOURS", "4")))

_db_initialized = False


@app.before_request
def _bootstrap():
    """첫 요청 시 DB 초기화 + admin 계정 셋업.
    .env의 APP_PASSWORD를 admin 계정 비밀번호로 자동 동기화 (단일 비밀번호 MVP).
    """
    global _db_initialized
    if not _db_initialized:
        models.init_db()
        admin_pw = os.getenv("APP_PASSWORD", "").strip()
        if admin_pw:
            models.ensure_admin_user("admin", admin_pw, display_name="관리자")
        _db_initialized = True


@app.after_request
def _no_store(resp):
    """환자 정보 페이지가 브라우저 캐시에 남지 않도록.
    로그아웃 후 뒤로가기로 노출되는 것 방지.
    """
    resp.headers["Cache-Control"] = "no-store, private, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


def _url_with(**overrides):
    """현재 요청의 쿼리스트링을 유지하면서 일부 파라미터만 덮어쓴 URL 반환.
    값이 None이거나 빈 문자열이면 해당 파라미터를 제거한다 (정렬·페이지 링크용).
    """
    args = request.args.to_dict()
    for k, v in overrides.items():
        if v is None or v == "":
            args.pop(k, None)
        else:
            args[k] = str(v)
    qs = urlencode(args)
    return request.path + (("?" + qs) if qs else "")


def _is_safe_next_url(value):
    """Allow only local absolute paths for post-login redirects."""
    value = (value or "").strip()
    if not value or not value.startswith("/"):
        return False
    if value.startswith("//") or value.startswith("/\\"):
        return False
    if any(ord(ch) < 32 for ch in value):
        return False
    parsed = urlsplit(value)
    return not parsed.scheme and not parsed.netloc


@app.context_processor
def _inject_globals():
    return {
        "current_user": current_user(),
        "INSURANCE_TYPES": INSURANCE_TYPES,
        "CONSULT_CHANNELS": CONSULT_CHANNELS,
        "ADMISSION_EVENT_TYPES": ADMISSION_EVENT_TYPES,
        "ATTENDING_DOCTORS": ATTENDING_DOCTORS,
        "ADMISSION_STATUSES": ADMISSION_STATUSES,
        "CONSULT_RESULTS": CONSULT_RESULTS,
        "CONSULT_RESULT_REASON_LABELS": CONSULT_RESULT_REASON_LABELS,
        "LIFECYCLE_STAGES": LIFECYCLE_STAGES,
        "LIFECYCLE_EVENT_TYPES": LIFECYCLE_EVENT_TYPES,
        "SMS_TEMPLATE_GROUPS": SMS_TEMPLATE_GROUPS,
        "COMM_CHANNELS": COMM_CHANNELS,
        "COMM_INBOUND_CHANNELS": COMM_INBOUND_CHANNELS,
        "REJECTION_REASONS": REJECTION_REASONS,
        "GUARDIAN_RELATION_SUGGESTIONS": GUARDIAN_RELATION_SUGGESTIONS,
        "COUNSELORS": COUNSELORS,
        "CURRENT_LOCATION_TYPES": CURRENT_LOCATION_TYPES,
        "CONSCIOUSNESS_MAIN_OPTIONS": CONSCIOUSNESS_MAIN_OPTIONS,
        "CONVERSATION_LEVEL_OPTIONS": CONVERSATION_LEVEL_OPTIONS,
        "HEARING_OPTIONS": HEARING_OPTIONS,
        "ACTIVITY_ACTIVE_OPTIONS": ACTIVITY_ACTIVE_OPTIONS,
        "ACTIVITY_DIAPER_OPTIONS": ACTIVITY_DIAPER_OPTIONS,
        "ACTIVITY_WHEELCHAIR_OPTIONS": ACTIVITY_WHEELCHAIR_OPTIONS,
        "ACTIVITY_OTHERS_OPTIONS": ACTIVITY_OTHERS_OPTIONS,
        "CAREGIVER_OPTIONS": CAREGIVER_OPTIONS,
        "BED_OPTIONS": BED_OPTIONS,
        "DISEASES_CHECKLIST": DISEASES_CHECKLIST,
        "DISEASES_GROUPS": DISEASES_GROUPS,
        "DISEASES_LAYOUT": DISEASES_LAYOUT,
        "OTHERS_CHECKLIST": OTHERS_CHECKLIST,
        "OTHERS_LAYOUT": OTHERS_LAYOUT,
        "DIET_TYPES": DIET_TYPES,
        "DIET_LAYOUT": DIET_LAYOUT,
        "WOUND_CARE_OPTIONS": WOUND_CARE_OPTIONS,
        "WOUND_CARE_NOTE_FIELDS": WOUND_CARE_NOTE_FIELDS,
        "SPECIAL_CARE_OPTIONS": SPECIAL_CARE_OPTIONS,
        "SPECIAL_CARE_NOTE_FIELDS": SPECIAL_CARE_NOTE_FIELDS,
        "THERAPY_OPTIONS": THERAPY_OPTIONS,
        "ADMISSION_DOCS": ADMISSION_DOCS,
        "TRANSPORT_OPTIONS": TRANSPORT_OPTIONS,
        "COST_GUIDANCE_OPTIONS": COST_GUIDANCE_OPTIONS,
        "INFO_PROVIDED_OPTIONS": INFO_PROVIDED_OPTIONS,
        "REFERRAL_SOURCE_GROUPS": REFERRAL_SOURCE_GROUPS,
        "REFERRAL_TYPES": REFERRAL_TYPES,
        "SIDO_LIST": SIDO_LIST,
        "SIGUNGU_LIST": SIGUNGU_LIST,
        "SIGUNGU_INDEX": SIGUNGU_INDEX,
        "now": datetime.now,
        "url_with": _url_with,
    }


@app.template_filter("krdate")
def _krdate(value):
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            return value
    return value.strftime("%Y-%m-%d")


@app.template_filter("krdate_wd")
def _krdate_wd(value):
    """'2026-05-04' → '5/4(월)' (요일 포함)."""
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            return value
    wd = "월화수목금토일"[value.weekday()]
    return f"{value.month}/{value.day}({wd})"


@app.template_filter("krdate_wd_full")
def _krdate_wd_full(value):
    """'2026-05-04' → '2026-05-04(월)' (연-월-일 + 요일)."""
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            return value
    wd = "월화수목금토일"[value.weekday()]
    return f"{value.strftime('%Y-%m-%d')}({wd})"


_SIDO_SHORT = {
    "서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구",
    "인천광역시": "인천", "광주광역시": "광주", "대전광역시": "대전",
    "울산광역시": "울산", "세종특별자치시": "세종",
    "경기도": "경기", "강원특별자치도": "강원", "강원도": "강원",
    "충청북도": "충북", "충청남도": "충남",
    "전북특별자치도": "전북", "전라북도": "전북", "전라남도": "전남",
    "경상북도": "경북", "경상남도": "경남",
    "제주특별자치도": "제주", "제주도": "제주",
}


@app.template_filter("sido_short")
def _sido_short(value):
    if not value:
        return ""
    return _SIDO_SHORT.get(value, value)


# 기저질환 그룹의 부모 라벨 prefix — 병명 셀에서 제외용
_CHRONIC_PREFIXES = ("당뇨", "고혈압", "파킨슨", "희귀성난치질환",
                     "치매", "인지기능저하", "이상행동", "탈출", "암",
                     "마비-편마비", "편마비")


@app.template_filter("hide_chronic")
def _hide_chronic(diseases_list):
    """diseases JSON 리스트에서 기저질환·만성질환 라벨 제거."""
    if not diseases_list:
        return []
    out = []
    for d in diseases_list:
        if d == "기저질환":
            continue
        if any(d == p or d.startswith(p + "-") or d.startswith(p + " ")
               for p in _CHRONIC_PREFIXES):
            continue
        out.append(d)
    return out


@app.template_filter("simplify_label")
def _simplify_label(label):
    """'마비-편마비 좌' → '마비', '골반-단일 골절' → '골반'. 세부값 제거."""
    if not label:
        return ""
    return label.split("-", 1)[0]


# 회복기 자동 판정 — 의료법(재활의료기관 본지정 고시) 기준
# 진단군별 회복기 인정 기간(일). 가장 긴 매칭값을 채택.
_RECOVERY_RULES = [
    # (키워드 리스트, 인정 기간 일수)
    # 여러 병명이 매칭되면 가장 긴 인정 기간을 적용한다.
    (["뇌출혈", "뇌경색", "뇌손상", "척수손상", "뇌성마비",
      "마비", "편마비", "사지마비", "중추신경계"], 90),
    # 골유합 지연 — 근골격계 골절 중 골유합이 지연되는 경우 인정 기간 연장
    (["골유합 지연", "골유합지연"], 60),
    (["고관절", "대퇴", "대퇴부", "골반", "절단", "하지 부위 절단",
      "슬관절", "근골격계"], 30),
    # 비사용증후군 — 2026-05-20 사용자 확인: 회복기 인정 기간 60일.
    # 파킨슨(신규)·길랑바레증후군도 비사용증후군 기준 동일 적용.
    (["호흡질환", "폐질환", "심장질환", "신생물", "폐렴", "폐수종",
      "패혈증", "농양", "다제내성", "CRE", "VRE",
      "신부전", "동정맥루", "복부대동맥류", "급성복막염", "장폐색",
      "파킨슨(신규)", "길랑바레증후군", "비사용증후군"], 60),
]


def compute_recovery_detail(reference_date, disease_onset, diseases):
    """입원(예정)일/상담일 - 발병일 → 회복기 판정 상세.
    Returns: dict(label, days, period, days_left) 또는 None(판정 불가).
      days      = 경과일 (reference - onset)
      period    = 인정 기간 (중추신경계 90일 / 비사용증후군·골유합 지연 60일 / 근골격계 30일)
      days_left = period - days (회복기 잔여일; 양수면 임박, 음수면 초과일)
    """
    if not reference_date or not disease_onset:
        return None
    try:
        rd = datetime.strptime(str(reference_date)[:10], "%Y-%m-%d").date()
        od = datetime.strptime(str(disease_onset)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    days = (rd - od).days
    if days < 0:
        return None
    matched = 0
    for d in diseases or []:
        if not d:
            continue
        d_str = str(d)
        for kws, period in _RECOVERY_RULES:
            if any(kw in d_str for kw in kws):
                if period > matched:
                    matched = period
                break
    if matched == 0:
        return None
    return {
        "label": "회복기" if days <= matched else "비회복기",
        "days": days,
        "period": matched,
        "days_left": matched - days,
    }


def compute_recovery(reference_date, disease_onset, diseases):
    """입원(예정)일 또는 상담일 - 발병일 → 회복기 여부.
    Returns: '회복기' | '비회복기' | None(판정 불가).
    """
    detail = compute_recovery_detail(reference_date, disease_onset, diseases)
    return detail["label"] if detail else None


@app.template_filter("recovery_status")
def _recovery_status(consultation):
    """저장된 admission_purpose 우선, 없으면 발병일 기반 자동 판정.
    Returns: dict(label, source, days_left) — source='manual'|'auto'|None.
      days_left: 회복기 인정기간 잔여일(자동 계산 가능 시). 임박 경고용.
    """
    if not consultation:
        return {"label": None, "source": None, "days_left": None}
    purpose = (consultation.get("admission_purpose") or "").strip()
    # 저장값 매핑 — 자동 기입값이 '회복기재활 및 간호간병 통합서비스' 형태일 수 있어 접두 판정.
    def _purpose_label(p):
        if p.startswith("비회복기재활") or p == "비회복기":
            return "비회복기"
        if p.startswith("회복기재활") or p == "회복기":
            return "회복기"
        if p.startswith("일반재활"):
            return "일반재활"
        if p.startswith("요양"):
            return "요양"
        return None
    manual_label = _purpose_label(purpose)
    # 자동 계산 (입원일 우선, 없으면 상담일) — 저장값과 무관하게 잔여일 산출
    ref = (consultation.get("actual_admission_date")
           or consultation.get("admission_date")
           or consultation.get("planned_admission_date")
           or consultation.get("consult_date"))
    detail = compute_recovery_detail(
        ref, consultation.get("disease_onset"), consultation.get("diseases"),
    )
    days_left = detail["days_left"] if detail else None
    if manual_label:
        return {"label": manual_label, "source": "manual", "days_left": days_left}
    if detail:
        return {"label": detail["label"], "source": "auto", "days_left": days_left}
    if purpose:
        return {"label": "기타", "source": "manual", "days_left": None}
    return {"label": None, "source": None, "days_left": None}


# ── 입원 기간(입원 후 재원 가능 일수) ──
# 중추신경계: 회복기 360일(s005 180 + s006 180) / 비회복기 180일(s006)
# 비사용증후군·하지 부위 절단·골유합 지연: 60일
# 근골격계 골절: 단일부위 30일 / 내고정술·전치환술·다발부위 60일
_ADM_CNS_KW = ("뇌출혈", "뇌경색", "뇌손상", "척수손상", "뇌성마비",
               "마비", "편마비", "사지마비", "중추신경계")
_ADM_DISUSE_KW = ("호흡질환", "폐질환", "심장질환", "신생물", "폐렴", "폐수종",
                  "패혈증", "농양", "다제내성", "CRE", "VRE", "신부전",
                  "동정맥루", "복부대동맥류", "급성복막염", "장폐색",
                  "파킨슨(신규)", "길랑바레증후군", "비사용증후군")
_ADM_MSK_KW = ("고관절", "대퇴", "골반", "근골격계", "슬관절",
               "내고정술", "치환술", "다발")
_ADM_MSK_LONG_KW = ("내고정술", "치환술", "다발")  # 근골격계 단일부위 → 60일 가산


def compute_admission_period(diseases, recovery_label):
    """질환군 + 회복기/비회복기 → 입원 기간(입원 후 재원 가능 일수).
    Returns: dict(total, billing) 또는 None(산정 불가).
      total   = 전체 입원 가능 일수
      billing = 회복기 수가(s005) 인정 기간 — 중추신경계 회복기만 180, 그 외 None
    여러 질환군 중복 시 가장 긴 입원 기간을 적용한다.
    """
    ds = [str(d) for d in (diseases or []) if d]
    if not ds:
        return None

    def has(kws):
        return any(any(kw in d for kw in kws) for d in ds)

    total = 0
    billing = None
    if has(_ADM_CNS_KW):
        if recovery_label == "회복기":
            total = max(total, 360)
            billing = 180
        elif recovery_label == "비회복기":
            total = max(total, 180)
        # 회복기/비회복기 미상이면 중추신경계 입원 기간 산정 불가 → 기여 안 함
    if has(_ADM_DISUSE_KW):
        total = max(total, 60)
    if has(("하지 부위 절단",)):
        total = max(total, 60)
    if has(("골유합 지연", "골유합지연")):
        total = max(total, 60)
    if has(_ADM_MSK_KW):
        total = max(total, 60 if has(_ADM_MSK_LONG_KW) else 30)
    if total == 0:
        return None
    return {"total": total, "billing": billing}


@app.template_filter("admission_expiry")
def _admission_expiry(consultation):
    """입원일 + 입원 기간 → 입원 만료일(퇴원 예정일) 계산.
    Returns: dict 또는 None.
      basis        = 'actual'(실제 입원일) | 'planned'(입원예정일 기준 추정)
      total_days   = 전체 입원 가능 일수, total_date/total_left = 만료일/잔여일
      billing_days = 회복기 수가(s005) 기간, billing_date/billing_left
                     (중추신경계 회복기만, 그 외 None)
    """
    if not consultation:
        return None
    rec = _recovery_status(consultation)
    period = compute_admission_period(
        consultation.get("diseases"), rec.get("label"),
    )
    if not period:
        return None
    actual = (consultation.get("actual_admission_date")
              or consultation.get("admission_date"))
    planned = consultation.get("planned_admission_date")
    adm = actual or planned
    if not adm:
        return None
    try:
        ad = datetime.strptime(str(adm)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    today = datetime.now().date()
    total_d = ad + timedelta(days=period["total"])
    out = {
        "basis": "actual" if actual else "planned",
        "total_days": period["total"],
        "total_date": total_d.isoformat(),
        "total_left": (total_d - today).days,
        "billing_days": period["billing"],
        "billing_date": None,
        "billing_left": None,
    }
    if period["billing"]:
        bd = ad + timedelta(days=period["billing"])
        out["billing_date"] = bd.isoformat()
        out["billing_left"] = (bd - today).days
    return out


@app.template_filter("discharge_watch")
def _discharge_watch(consultation):
    """입원완료 상담의 퇴원 임박 여부 — 상담목록 '퇴원예정' 표기/액션용.
    Returns: dict(state, due_date, days_left) 또는 None.
      state     = '퇴원예정'(유효 퇴원예정일 30일 이내·초과) | None
      due_date  = 유효 퇴원예정일 — 수동 입원연장값(discharge_due_date) 우선,
                  없으면 입원만료일(_admission_expiry total_date) 자동 계산
      days_left = due_date - 오늘 (음수면 초과)
    """
    if not consultation:
        return None
    if (consultation.get("admission_status") or "").strip() != "입원완료":
        return None
    due = (consultation.get("discharge_due_date") or "").strip() or None
    if not due:
        ax = _admission_expiry(consultation)
        due = ax["total_date"] if ax else None
    if not due:
        return None
    try:
        dd = datetime.strptime(str(due)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    days_left = (dd - datetime.now().date()).days
    return {
        "state": "퇴원예정" if days_left <= 30 else None,
        "due_date": dd.isoformat(),
        "days_left": days_left,
    }


def _dashboard_ward_label(room_number):
    room = (room_number or "").strip()
    if not room:
        return "병동 미지정"
    if "병동" in room:
        return room
    digits = ""
    started = False
    for ch in room:
        if ch.isdigit():
            digits += ch
            started = True
        elif started:
            break
    if len(digits) >= 3:
        return f"{digits[0]}병동"
    if digits:
        return f"{digits}병동"
    return room


def _dashboard_disease_labels(record):
    labels = []
    diseases = record.get("diseases") or []
    if isinstance(diseases, list):
        labels.extend(str(v).strip() for v in diseases if str(v).strip())
    elif str(diseases).strip():
        labels.append(str(diseases).strip())
    for key in ("primary_diagnosis", "secondary_diagnosis"):
        value = (record.get(key) or "").strip()
        if value and value not in labels:
            labels.append(value)
    labels = _hide_chronic(labels)
    return labels or ["병명 미지정"]


def _dashboard_groups(items, labels_fn):
    grouped = {}
    for item in items:
        labels = labels_fn(item)
        if isinstance(labels, str):
            labels = [labels]
        for label in labels:
            label = (label or "").strip() or "미지정"
            grouped.setdefault(label, []).append(item)
    return sorted(
        ({"label": label, "count": len(rows), "rows": rows}
         for label, rows in grouped.items()),
        key=lambda g: (-g["count"], g["label"]),
    )


def _dashboard_inbound_bucket(comm):
    channel = (comm.get("channel") or "").strip()
    if "카카오" in channel:
        return "카카오채널"
    if "웹" in channel or "홈" in channel:
        return "홈페이지"
    return channel or "기타"


def _dashboard_parse_datetime(value):
    value = (value or "").strip()
    if not value:
        return None
    formats = (
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%Y-%m-%dT%H:%M:%S", 19),
        ("%Y-%m-%d %H:%M", 16),
        ("%Y-%m-%d", 10),
    )
    for fmt, size in formats:
        try:
            return datetime.strptime(value[:size], fmt)
        except ValueError:
            continue
    return None


def _dashboard_elapsed_label(dt):
    if not dt:
        return ""
    minutes = max(0, int((datetime.now() - dt).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes}분 경과"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}시간 경과"
    days = hours // 24
    rem = hours % 24
    return f"{days}일 {rem}시간 경과" if rem else f"{days}일 경과"


def _dashboard_days_since(value):
    dt = _dashboard_parse_datetime(value)
    if not dt:
        return None
    return (datetime.now().date() - dt.date()).days


def _dashboard_action_queue(data, open_comms, callbacks, recovery_due, discharge_due):
    today = datetime.now().strftime("%Y-%m-%d")
    items = []

    def add(kind, tone, title, detail="", meta="", href=None, sort=50):
        items.append({
            "kind": kind,
            "tone": tone,
            "title": title,
            "detail": detail,
            "meta": meta,
            "href": href,
            "sort": sort,
        })

    for m in open_comms:
        occurred = _dashboard_parse_datetime(m.get("occurred_at") or m.get("created_at"))
        hours = ((datetime.now() - occurred).total_seconds() / 3600) if occurred else 0
        tone = "danger" if hours >= 24 else "warn" if hours >= 2 else "info"
        who = m.get("patient_name") or m.get("contact") or "미연결 문의"
        add(
            _dashboard_inbound_bucket(m),
            tone,
            who,
            (m.get("summary") or m.get("body") or "")[:70],
            _dashboard_elapsed_label(occurred),
            "/inbox",
            0 if tone == "danger" else 15 if tone == "warn" else 45,
        )

    for r in callbacks:
        days = _dashboard_days_since(r.get("consult_date"))
        tone = "danger" if days is not None and days >= 2 else "warn"
        meta = f"{days}일 대기" if days and days > 0 else "오늘 재연락"
        add(
            "재연락",
            tone,
            r.get("patient_name") or "환자 미지정",
            " · ".join(v for v in (
                r.get("counselor") or "상담사 미지정",
                r.get("disease_summary") or "병명 미지정",
                r.get("consult_result_reason") or "",
            ) if v),
            meta,
            f"/consult/{r.get('id')}" if r.get("id") else "/inbox",
            8 if tone == "danger" else 25,
        )

    for r in data.get("admission_by_status", {}).get("planned", []):
        if r.get("admission_display_date") != today:
            continue
        missing = []
        if not (r.get("planned_admission_time") or "").strip():
            missing.append("입원시간")
        if not (r.get("attending_doctor") or "").strip():
            missing.append("주치의")
        if not (r.get("room_number") or "").strip():
            missing.append("병실")
        if not (r.get("counselor") or "").strip():
            missing.append("상담사")
        if missing:
            add(
                "입원준비",
                "danger",
                r.get("patient_name") or "환자 미지정",
                "누락: " + ", ".join(missing),
                "오늘 입원 예정",
                f"/consult/{r.get('id')}" if r.get("id") else None,
                2,
            )

    for r in data.get("today", []):
        if not (r.get("counselor") or "").strip():
            add(
                "담당자",
                "warn",
                r.get("patient_name") or "환자 미지정",
                "오늘 상담의 상담사가 지정되지 않았습니다.",
                r.get("consult_time") or "시간 미지정",
                f"/consult/{r.get('id')}" if r.get("id") else None,
                28,
            )

    for d in recovery_due[:6]:
        left = d["watch"].get("billing_left")
        add(
            "전환체크",
            "danger" if left is not None and left <= 0 else "warn",
            d["con"].get("patient_name") or "환자 미지정",
            "회복기 수가 만료 임박",
            f"{abs(left)}일 초과" if left is not None and left < 0 else f"D-{left}",
            f"/consult/{d['con'].get('id')}" if d["con"].get("id") else None,
            6 if left is not None and left <= 0 else 22,
        )

    for d in discharge_due[:6]:
        left = d["watch"].get("days_left")
        add(
            "퇴원예정",
            "danger" if left is not None and left <= 0 else "warn",
            d["con"].get("patient_name") or "환자 미지정",
            "퇴원 예정일 확인 필요",
            f"{abs(left)}일 초과" if left is not None and left < 0 else f"D-{left}",
            f"/consult/{d['con'].get('id')}" if d["con"].get("id") else None,
            10 if left is not None and left <= 0 else 30,
        )

    for h in data.get("holds", [])[:8]:
        days = _dashboard_days_since(h.get("updated_at") or h.get("consult_date"))
        add(
            h.get("hold_kind") or "보류",
            "danger" if h.get("hold_kind") == "입원보류" else "warn",
            h.get("patient_name") or "환자 미지정",
            h.get("hold_reason_text") or "보류 사유 확인 필요",
            f"{days}일 경과" if days and days > 0 else "보류",
            f"/consult/{h.get('id')}" if h.get("id") else None,
            35,
        )

    tone_rank = {"danger": 0, "warn": 1, "info": 2}
    items.sort(key=lambda x: (tone_rank.get(x["tone"], 9), x["sort"], x["title"]))
    return {
        "items": items[:14],
        "total": len(items),
        "danger": sum(1 for x in items if x["tone"] == "danger"),
        "warn": sum(1 for x in items if x["tone"] == "warn"),
    }


@app.template_filter("agefrom")
def _agefrom(birth_year):
    if not birth_year:
        return ""
    return datetime.now().year - int(birth_year)


# ── 생애주기 단계 자동 동기화 (제안 1 — 상담 결과 → 단계) ──
# 입원완료→입원, 입원보류→입원대기, 퇴원완료→퇴원. 전진만 (수동 지정한 더 앞선
# 단계는 되돌리지 않음). 퇴원은 종료 단계라 항상 적용.
_STATUS_TO_STAGE = {"입원완료": "입원", "입원보류": "입원대기", "퇴원완료": "퇴원"}


def _sync_lifecycle_stage(patient_id, admission_status):
    """상담의 입원 진행 변화에 맞춰 환자 생애주기 단계를 자동 전진시킨다.
    이중 입력 제거 — 상담 결과만 바꾸면 생애주기 보드에도 반영된다.
    """
    target = _STATUS_TO_STAGE.get((admission_status or "").strip())
    if not target:
        return
    p = models.get_patient(patient_id)
    if not p:
        return
    order = {s: i for i, s in enumerate(LIFECYCLE_STAGES)}
    cur_idx = order.get((p.get("lifecycle_stage") or "").strip(), -1)
    tgt_idx = order.get(target, -1)
    if tgt_idx < 0:
        return
    if target == "퇴원" or tgt_idx > cur_idx:
        models.set_patient_stage(patient_id, target)


def _sync_lifecycle_stage_if_unset(patient_id, target_stage):
    """환자 단계가 비어 있을 때만 target_stage로 설정.
    트리거 ① 신규 상담 등록 시 기본 '상담' 단계 자동 부여용 — 이미 단계가 있는
    (입원/회복기/퇴원 등) 환자는 건드리지 않는다."""
    if target_stage not in LIFECYCLE_STAGES:
        return
    p = models.get_patient(patient_id)
    if not p:
        return
    if (p.get("lifecycle_stage") or "").strip():
        return  # 이미 단계 있음 — 손대지 않음
    models.set_patient_stage(patient_id, target_stage)


def _set_lifecycle_stage_clinical(patient_id, target_stage):
    """임상 이벤트 기반 단계 전환 — 응급치료·복귀·회복기·비회복기·퇴원 등.
    의료 사건이 발생하면 단계가 뒤로 갈 수도 있으므로(예: 회복기→응급치료)
    `_sync_lifecycle_stage`의 '앞으로만' 룰을 우회한다. 단, 이미 '퇴원' 상태인
    환자는 더 이상 변동하지 않는다(완료 케이스 보호)."""
    if target_stage not in LIFECYCLE_STAGES:
        return
    p = models.get_patient(patient_id)
    if not p:
        return
    cur = (p.get("lifecycle_stage") or "").strip()
    if cur == "퇴원" and target_stage != "퇴원":
        return  # 퇴원 환자는 다시 끌어내지 않음
    models.set_patient_stage(patient_id, target_stage)


# ───────────────────── 인증 ─────────────────────

@app.route("/login", methods=["GET", "POST"])
def login_view():
    if request.method == "POST":
        ip = request.remote_addr
        if is_locked_out(ip):
            flash("로그인 시도 횟수 초과. 5분 후 다시 시도해주세요.", "error")
            return render_template("login.html"), 429
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = authenticate(username, password, ip=ip)
        if not user:
            flash("아이디 또는 비밀번호가 올바르지 않습니다.", "error")
            return render_template("login.html"), 401
        login_user(user)
        next_url = request.args.get("next") or request.form.get("next") or url_for("dashboard")
        if not _is_safe_next_url(next_url):
            next_url = url_for("dashboard")
        return redirect(next_url)
    if current_user():
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout", methods=["POST", "GET"])
def logout_view():
    logout_user()
    flash("로그아웃되었습니다.", "info")
    return redirect(url_for("login_view"))


# ───────────────────── 메인 ─────────────────────

@app.route("/")
@login_required
def dashboard():
    data = models.dashboard_summary()
    open_comms = models.inbox_open_communications()
    callbacks = models.inbox_callbacks()

    # 입원완료 환자 중 회복기→비회복기 전환 D-15, 퇴원예정 D-30.
    admitted = models.list_consultations(admission_status="입원완료", limit=10000)
    recovery_transition_due = []
    discharge_due = []
    for con in admitted:
        disease_labels = _dashboard_disease_labels(con)
        con["disease_summary"] = "" if disease_labels == ["병명 미지정"] else ", ".join(disease_labels[:3])
        con["ward"] = _dashboard_ward_label(con.get("room_number"))
        ax = _admission_expiry(con)
        if ax and ax.get("billing_left") is not None and ax["billing_left"] <= 15:
            recovery_transition_due.append({"con": con, "watch": ax})
        dw = _discharge_watch(con)
        if dw and dw.get("days_left") is not None and dw["days_left"] <= 30:
            discharge_due.append({"con": con, "watch": dw})
    recovery_transition_due.sort(key=lambda x: x["watch"]["billing_left"])
    discharge_due.sort(key=lambda x: x["watch"]["days_left"])

    for cb in callbacks:
        disease_labels = _dashboard_disease_labels(cb)
        cb["disease_summary"] = "" if disease_labels == ["병명 미지정"] else ", ".join(disease_labels[:3])

    inbound_groups = {
        "all": open_comms,
        "kakao": [m for m in open_comms if _dashboard_inbound_bucket(m) == "카카오채널"],
        "homepage": [m for m in open_comms if _dashboard_inbound_bucket(m) == "홈페이지"],
        "other": [
            m for m in open_comms
            if _dashboard_inbound_bucket(m) not in ("카카오채널", "홈페이지")
        ],
    }
    discharge_groups = {
        "disease": _dashboard_groups(discharge_due, lambda x: _dashboard_disease_labels(x["con"])),
        "ward": _dashboard_groups(discharge_due, lambda x: _dashboard_ward_label(x["con"].get("room_number"))),
        "doctor": _dashboard_groups(
            discharge_due,
            lambda x: (x["con"].get("attending_doctor") or "").strip() or "주치의 미지정",
        ),
    }
    callback_groups = {
        "counselor": _dashboard_groups(
            callbacks,
            lambda x: (x.get("counselor") or "").strip() or "상담사 미지정",
        ),
        "disease": _dashboard_groups(callbacks, _dashboard_disease_labels),
    }
    action_queue = _dashboard_action_queue(
        data, open_comms, callbacks, recovery_transition_due, discharge_due,
    )

    data["open_comms"] = open_comms
    data["inbound_groups"] = inbound_groups
    data["callbacks"] = callbacks
    data["callback_groups"] = callback_groups
    data["recovery_transition_due"] = recovery_transition_due
    data["discharge_due"] = discharge_due
    data["discharge_groups"] = discharge_groups
    data["action_queue"] = action_queue
    data["summary"]["open_inbound"] = len(open_comms)
    data["summary"]["callbacks"] = len(callbacks)
    data["summary"]["recovery_transition_due"] = len(recovery_transition_due)
    data["summary"]["discharge_pending"] = len(discharge_due)
    data["summary"]["action_total"] = action_queue["total"]
    data["summary"]["action_danger"] = action_queue["danger"]
    return render_template("dashboard.html", **data)


@app.route("/healthz")
def healthz():
    return {"ok": True}


@app.route("/help")
@login_required
def help_manual():
    """8개 메뉴 사용 매뉴얼 — 신규 상담사 온보딩·일상 참고용."""
    return render_template("help.html")


# ───────────────────── 통계 (Phase 3) ─────────────────────

@app.route("/stats")
@login_required
def stats_view():
    preset, date_from, date_to = _stats_period_from_request()
    return render_template(
        "stats.html",
        preset=preset, date_from=date_from, date_to=date_to,
        insight_enabled=bool(os.getenv("ANTHROPIC_API_KEY")),
    )


@app.route("/api/stats.json")
@login_required
def api_stats():
    _, date_from, date_to = _stats_period_from_request()
    data = models.aggregate_stats(date_from, date_to)
    return jsonify(data)


@app.route("/report/monthly")
@login_required
def report_monthly():
    """임원용 월간 1페이지 보고서 — 이번 달·전월 KPI + 채널 ROI + 모병원·사유 Top."""
    now = datetime.now()
    try:
        year = int(request.args.get("year") or now.year)
        month = int(request.args.get("month") or now.month)
    except (TypeError, ValueError):
        year, month = now.year, now.month
    if not (1 <= month <= 12):
        year, month = now.year, now.month
    data = models.aggregate_monthly(year, month)
    return render_template(
        "report_monthly.html",
        data=data,
        insight_enabled=bool(os.getenv("ANTHROPIC_API_KEY")),
    )


@app.route("/api/report/monthly/insight", methods=["POST"])
@login_required
def api_report_monthly_insight():
    if not os.getenv("ANTHROPIC_API_KEY"):
        return jsonify({"error": "Claude API 키가 설정되지 않았습니다."}), 503
    payload = request.get_json(silent=True) or {}
    try:
        year = int(payload.get("year"))
        month = int(payload.get("month"))
    except (TypeError, ValueError):
        return jsonify({"error": "year/month 필수"}), 400
    data = models.aggregate_monthly(year, month)
    if not data["this"]["summary"]["total"]:
        return jsonify({"insight": "이번 달 상담 기록이 없어 인사이트를 생성할 수 없습니다."})
    try:
        from llm import summarize_monthly
        text = summarize_monthly(data)
    except Exception as e:
        logger.warning(f"월간 인사이트 실패: {e}")
        return jsonify({"error": f"인사이트 생성 실패: {e}"}), 502
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="report_insight", target_type="monthly_report",
        detail=f"{year}-{month:02d}", ip=request.remote_addr,
    )
    return jsonify({"insight": text})


@app.route("/api/stats/insight", methods=["POST"])
@login_required
def api_stats_insight():
    if not os.getenv("ANTHROPIC_API_KEY"):
        return jsonify({"error": "Claude API 키가 설정되지 않았습니다."}), 503
    payload = request.get_json(silent=True) or {}
    date_from = payload.get("from") or None
    date_to = payload.get("to") or None
    data = models.aggregate_stats(date_from, date_to)
    if not data["summary"]["total"]:
        return jsonify({"insight": "분석할 상담 기록이 없습니다."})
    try:
        from llm import summarize_stats
        text = summarize_stats(data, date_from=date_from, date_to=date_to)
    except Exception as e:
        logger.warning(f"Claude 인사이트 실패: {e}")
        return jsonify({"error": f"인사이트 생성 실패: {e}"}), 502
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="stats_insight", target_type="stats",
        detail=f"{date_from}~{date_to}", ip=request.remote_addr,
    )
    return jsonify({"insight": text})


# ───────────────────── 상담 ─────────────────────

@app.route("/consult/new")
@login_required
def consult_new():
    # 인박스 미처리 인바운드에서 상담 등록을 시작한 경우 — communication 로드 후 prefill
    inbox_comm = None
    patient = None
    try:
        comm_id = int(request.args.get("comm_id") or 0)
    except (ValueError, TypeError):
        comm_id = 0
    if comm_id:
        comm = models.get_communication(comm_id)
        if comm and (comm.get("status") or "") != "done":
            inbox_comm = comm
            if comm.get("patient_id"):
                patient = models.get_patient(comm["patient_id"])
            else:
                # 환자 미연결 — contact(연락처)·body로 보호자 정보 추론하여 가상 patient
                contact = (comm.get("contact") or "").strip()
                patient = {
                    "id": None, "name": "",
                    "guardian_phone": contact if contact else "",
                }
    return render_template("consult_form.html", consultation=None, patient=patient,
                           inbox_comm=inbox_comm,
                           top_hospitals=models.top_source_hospitals())


@app.route("/consult/<int:cid>")
@login_required
def consult_detail(cid):
    c = models.get_consultation(cid)
    if not c:
        abort(404)
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="view_consult", target_type="consultation", target_id=cid,
        ip=request.remote_addr,
    )
    history = models.patient_consultations(c["patient_id"])
    return render_template("consult_detail.html", c=c, history=history,
                           admission_events=models.list_admission_events(cid),
                           LIFECYCLE_EVENT_TYPES=LIFECYCLE_EVENT_TYPES)


@app.route("/consult/<int:cid>/edit")
@login_required
def consult_edit(cid):
    c = models.get_consultation(cid)
    if not c:
        abort(404)
    patient = models.get_patient(c["patient_id"])
    return render_template("consult_form.html", consultation=c, patient=patient,
                           top_hospitals=models.top_source_hospitals())


CONSULT_PAGE_SIZE = 100  # 상담 목록 페이지당 행 수


@app.route("/consultations")
@login_required
def consult_list():
    filters = _list_filters_from_request()
    sort = request.args.get("sort") or "date"
    sort_dir = "asc" if (request.args.get("dir") or "").lower() == "asc" else "desc"
    try:
        page = max(1, int(request.args.get("page") or 1))
    except (ValueError, TypeError):
        page = 1
    total = models.count_consultations(**filters)
    total_pages = max(1, (total + CONSULT_PAGE_SIZE - 1) // CONSULT_PAGE_SIZE)
    page = min(page, total_pages)
    offset = (page - 1) * CONSULT_PAGE_SIZE
    rows = models.list_consultations(
        **filters, sort=sort, sort_dir=sort_dir,
        limit=CONSULT_PAGE_SIZE, offset=offset,
    )
    return render_template(
        "consult_list.html", rows=rows, filters=filters,
        sort=sort, sort_dir=sort_dir,
        page=page, total_pages=total_pages, total=total,
        page_size=CONSULT_PAGE_SIZE, page_start=offset,
        COUNSELORS=COUNSELORS,
        ADMISSION_STATUSES=ADMISSION_STATUSES,
        DISEASE_GROUPS=list(DISEASES_GROUPS.keys()),
        SIDO_LIST=SIDO_LIST,
        REFERRAL_TYPES=REFERRAL_TYPES,
        CONSULT_CHANNELS=CONSULT_CHANNELS,
        RECOVERY_OPTIONS=["회복기", "비회복기", "일반재활", "요양"],
    )


@app.route("/consultations.csv")
@admin_required
def consult_csv():
    filters = _list_filters_from_request()
    rows = models.list_consultations(**filters, limit=10000)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "상담일", "상담시각", "환자명", "성별", "나이", "블랙리스트",
        "거주시도", "거주시군구", "주소", "보험유형",
        "보호자", "관계", "연락처",
        "상담방법", "유입경로(상위)", "세부경로",
        "모병원", "병명", "발병일", "회복기",
        "상담결과", "상담결과사유", "입원진행",
        "주치의", "호실", "입원예정일", "입원완료일", "상담자",
    ])
    for r in rows:
        rec = _recovery_status(r)
        recovery_label = rec["label"] or ""
        if recovery_label and rec["source"] == "auto":
            recovery_label += "(자동)"
        writer.writerow([
            r.get("consult_date") or "",
            r.get("consult_time") or "",
            r.get("patient_name") or "",
            r.get("gender") or "",
            r.get("patient_age") if r.get("patient_age") is not None else "",
            "블랙리스트" if r.get("blacklist") else "",
            r.get("residence_sido") or "",
            r.get("residence_sigungu") or "",
            r.get("address_full") or "",
            r.get("insurance_type") or "",
            r.get("guardian_name") or "",
            r.get("guardian_relation") or "",
            r.get("guardian_phone") or "",
            r.get("consult_channel") or "",
            _csv_list(r.get("referral_source_type")),
            _csv_list(r.get("referral_source_detail")),
            r.get("source_hospital") or "",
            _csv_list(r.get("diseases")),
            r.get("disease_onset") or "",
            recovery_label,
            r.get("consult_result") or "상담완료",
            r.get("consult_result_reason") or "",
            r.get("admission_status") or "미정",
            r.get("attending_doctor") or "",
            r.get("room_number") or "",
            r.get("planned_admission_date") or "",
            r.get("actual_admission_date") or "",
            r.get("counselor") or "",
        ])
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="export_csv", target_type="consultations",
        detail=str(len(rows)), ip=request.remote_addr,
    )
    data = buf.getvalue().encode("utf-8-sig")  # Excel 한글 깨짐 방지
    return send_file(
        io.BytesIO(data), mimetype="text/csv",
        as_attachment=True,
        download_name=f"consultations_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
    )


@app.route("/patients/<int:pid>")
@login_required
def patient_detail(pid):
    p = models.get_patient(pid)
    if not p:
        abort(404)
    history = models.patient_consultations(pid)
    timeline = models.patient_timeline(pid)
    # 생애주기 보드 검색어(q) 보존 — 보드 → 환자상세 → 보드 복귀 시 검색 상태 유지
    lc_back_q = (request.args.get("q") or "").strip()
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="view_patient", target_type="patient", target_id=pid,
        ip=request.remote_addr,
    )
    return render_template("patient_detail.html", p=p, history=history,
                           timeline=timeline, lc_back_q=lc_back_q)


# ───────────────────── API: 상담 CRUD ─────────────────────

@app.route("/api/consult", methods=["POST"])
@login_required
def api_consult_create():
    payload = request.get_json(silent=True) or {}
    err = _validate_consult_payload(payload, require_patient=True)
    if err:
        return jsonify({"error": err}), 400

    p = payload["patient"]
    pid = models.find_or_create_patient(
        name=p["name"].strip(),
        guardian_phone=(p.get("guardian_phone") or "").strip() or None,
        gender=p.get("gender") or "U",
        address_full=p.get("address_full"),
        residence_sido=p.get("residence_sido"),
        residence_sigungu=p.get("residence_sigungu"),
        insurance_type=p.get("insurance_type"),
        guardian_name=p.get("guardian_name"),
        guardian_relation=p.get("guardian_relation"),
        family_info=p.get("family_info"),
    )
    # 블랙리스트 (4번) — 폼 체크 상태 반영
    if "blacklist" in p:
        models.set_patient_blacklist(
            pid, bool(p.get("blacklist")),
            (p.get("blacklist_reason") or "").strip() or None)

    c = payload.get("consultation", {})
    cfields = _consult_fields_from_payload(c)
    cfields.setdefault("consult_date", datetime.now().strftime("%Y-%m-%d"))
    cfields.setdefault("counselor", g.user.get("display_name"))
    cid = models.create_consultation(patient_id=pid, **cfields)
    if cfields.get("admission_status"):
        _sync_lifecycle_stage(pid, cfields["admission_status"])
    else:
        # 트리거 ① — 신규 상담은 기본 '상담' 단계 자동 부여 (단계 미지정 환자 한정)
        # 이미 입원/회복기 등 더 뒤 단계인 환자는 룰 A로 후진 안 함
        _sync_lifecycle_stage_if_unset(pid, "상담")
    # 인박스에서 등록 → 해당 communication을 'done' + consultation_id/patient_id 연결
    try:
        comm_id = int(request.args.get("comm_id") or 0)
    except (ValueError, TypeError):
        comm_id = 0
    if comm_id and models.get_communication(comm_id):
        models.update_communication(
            comm_id, status="done", consultation_id=cid, patient_id=pid,
        )
        models.log_audit(
            user_id=g.user["id"], username=g.user["username"],
            action="close_communication", target_type="communication",
            target_id=comm_id, detail=f"→ consult #{cid}", ip=request.remote_addr,
        )
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="create_consult", target_type="consultation", target_id=cid,
        ip=request.remote_addr,
    )
    return jsonify({"ok": True, "id": cid, "patient_id": pid})


@app.route("/api/consult/<int:cid>", methods=["POST"])
@login_required
def api_consult_update(cid):
    existing = models.get_consultation(cid)
    if not existing:
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    err = _validate_consult_payload(payload, require_patient=False)
    if err:
        return jsonify({"error": err}), 400

    p = payload.get("patient") or {}
    if p:
        patient_cols = (
            "gender", "address_full", "residence_sido", "residence_sigungu",
            "insurance_type", "guardian_name", "guardian_relation",
            "guardian_phone", "family_info",
        )
        valid = {}
        for k, v in p.items():
            if k not in patient_cols:
                continue
            valid[k] = (v.strip() or None) if isinstance(v, str) else v
        if valid:
            models.update_patient(existing["patient_id"], **valid)
        if "blacklist" in p:
            models.set_patient_blacklist(
                existing["patient_id"], bool(p.get("blacklist")),
                (p.get("blacklist_reason") or "").strip() or None)

    c = payload.get("consultation") or {}
    update_fields = _consult_fields_from_payload(c)
    if update_fields:
        models.update_consultation(cid, **update_fields)
        if "admission_status" in update_fields:
            _sync_lifecycle_stage(existing["patient_id"], update_fields["admission_status"])

    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="update_consult", target_type="consultation", target_id=cid,
        ip=request.remote_addr,
    )
    return jsonify({"ok": True})


# ───────────────────── API: 결과(입원 진행 단계) 변경 ─────────────────────

@app.route("/api/consult/<int:cid>/status", methods=["POST"])
@login_required
def api_consult_status(cid):
    """상담 결과 2단계 변경 — ① consult_result(상담 진행) ② admission_status(입원 진행).
    payload에 들어온 단계만 변경. 두 단계 모두 한 번에 보낼 수도 있다.
    """
    existing = models.get_consultation(cid)
    if not existing:
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    fields = {}
    audit = []

    # ① 상담 진행 (Tier 1)
    if "consult_result" in payload:
        cr = (payload.get("consult_result") or "").strip()
        if cr and cr not in CONSULT_RESULTS:
            return jsonify({"error": "허용되지 않은 상담 결과값"}), 400
        if cr:
            reason = (payload.get("consult_result_reason") or "").strip()
            if cr in CONSULT_RESULT_REASON_LABELS and not reason:
                return jsonify(
                    {"error": f"{CONSULT_RESULT_REASON_LABELS[cr]}을(를) 입력하세요."}), 400
            fields["consult_result"] = cr
            fields["consult_result_reason"] = reason or None
            audit.append(f"상담:{cr}")

    # ② 입원 진행 (Tier 2) — 빈값은 '미정'(입원 단계 미진입)
    if "admission_status" in payload:
        status = (payload.get("admission_status") or "").strip()
        if status and status not in ADMISSION_STATUSES:
            return jsonify({"error": "허용되지 않은 입원 진행값"}), 400
        fields["admission_status"] = status or None
        audit.append(f"입원:{status or '미정'}")
        if status == "입원완료":
            adate = (payload.get("admission_date") or "").strip()
            if adate:
                try:
                    datetime.strptime(adate, "%Y-%m-%d")
                    fields["admission_date"] = adate
                except ValueError:
                    return jsonify({"error": "입원일자 형식 오류"}), 400
        elif status == "입원보류":
            hold_reason = (payload.get("hold_reason") or "").strip()
            if not hold_reason:
                return jsonify({"error": "입원보류 사유를 입력하세요."}), 400
            fields["hold_reason"] = hold_reason
        elif status == "입원취소":
            reason = (payload.get("rejection_reason") or "").strip()
            reason_detail = (payload.get("rejection_reason_detail") or "").strip()
            if reason and reason not in REJECTION_REASONS:
                return jsonify({"error": "허용되지 않은 취소 사유"}), 400
            if not reason and not reason_detail:
                return jsonify({"error": "입원취소 사유를 입력하세요."}), 400
            if reason:
                fields["rejection_reason"] = reason
            if reason_detail:
                fields["rejection_reason_detail"] = reason_detail

    if not fields:
        return jsonify({"error": "변경할 값이 없습니다."}), 400

    models.update_consultation_meta(cid, **fields)
    if "admission_status" in fields:
        _sync_lifecycle_stage(existing["patient_id"], fields["admission_status"])
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="update_status", target_type="consultation", target_id=cid,
        detail=" / ".join(audit), ip=request.remote_addr,
    )
    return jsonify({"ok": True, **fields})


# ───────────────────── API: 퇴원 워크플로 (상담목록) ─────────────────────

@app.route("/api/consult/<int:cid>/discharge", methods=["POST"])
@login_required
def api_consult_discharge(cid):
    """입원완료 상담의 퇴원 처리 — action=complete(퇴원완료) | extend(입원연장).
    complete: admission_status='퇴원완료' + discharge_date 저장.
    extend:   discharge_due_date(새 퇴원예정일) 저장. 상태는 입원완료 유지.
    """
    existing = models.get_consultation(cid)
    if not existing:
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    action = (payload.get("action") or "").strip()
    fields = {}
    if action == "complete":
        ddate = (payload.get("discharge_date") or "").strip()
        if not ddate:
            return jsonify({"error": "퇴원일자를 입력하세요."}), 400
        try:
            datetime.strptime(ddate, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "퇴원일자 형식 오류 (YYYY-MM-DD)"}), 400
        fields["admission_status"] = "퇴원완료"
        fields["discharge_date"] = ddate
    elif action == "extend":
        due = (payload.get("discharge_due_date") or "").strip()
        if not due:
            return jsonify({"error": "새 퇴원예정일을 입력하세요."}), 400
        try:
            datetime.strptime(due, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "퇴원예정일 형식 오류 (YYYY-MM-DD)"}), 400
        fields["discharge_due_date"] = due
    else:
        return jsonify({"error": "허용되지 않은 동작"}), 400

    models.update_consultation_meta(cid, **fields)
    if action == "complete":
        _sync_lifecycle_stage(existing["patient_id"], "퇴원완료")
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="update_discharge", target_type="consultation", target_id=cid,
        detail=action, ip=request.remote_addr,
    )
    return jsonify({"ok": True, **fields})


@app.route("/api/consult/<int:cid>", methods=["DELETE"])
@login_required
def api_consult_delete(cid):
    """상담 1건 삭제. 감사 로그에 환자명·날짜 기록."""
    existing = models.get_consultation(cid)
    if not existing:
        return jsonify({"error": "not found"}), 404
    detail = f"{existing.get('patient_name', '')} / {existing.get('consult_date', '')}"
    models.delete_consultation(cid)
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="delete_consult", target_type="consultation", target_id=cid,
        detail=detail, ip=request.remote_addr,
    )
    return jsonify({"ok": True})


# ───────────────────── 생애주기 (3번 요청) ─────────────────────

@app.route("/lifecycle")
@login_required
def lifecycle_board():
    """환자 생애주기 관리 보드 — 단계별 컬럼에 환자 카드 배치.
    필터: q(검색) / period(기간) / stages[](단계) / dx(병명그룹) / doctor / archived(아카이브 포함)
    """
    q = (request.args.get("q") or "").strip() or None
    # 기간 — 기본 90일, '0'/'all'은 전체
    period_raw = request.args.get("period", "90")
    try:
        period_days = None if period_raw in ("0", "all", "") else int(period_raw)
    except (ValueError, TypeError):
        period_days = 90
    # 단계 필터 — 여러 단계 체크박스
    stage_filter = request.args.getlist("stage") or []
    stage_filter = [s for s in stage_filter if s in LIFECYCLE_STAGES] or None
    # 병명 그룹·주치의·모병원
    dx = (request.args.get("dx") or "").strip() or None
    doctor = (request.args.get("doctor") or "").strip() or None
    hospital = (request.args.get("hospital") or "").strip() or None
    # 아카이브 포함 (자동 정리 룰 우회)
    include_archived = request.args.get("archived") in ("1", "true", "yes")
    # KPI 클릭 필터·프리셋
    stale_only = request.args.get("stale") in ("1", "true", "yes")
    new_30d_only = request.args.get("new30") in ("1", "true", "yes")
    discharge_imminent_only = request.args.get("discharge_imminent") in ("1", "true", "yes")
    emergency_overdue_only = request.args.get("emergency_overdue") in ("1", "true", "yes")
    # 기본 보기 — view=all이 아니면 입원·응급치료·입원대기 자동 필터 (시급 환자)
    view = request.args.get("view") or ""
    any_explicit_filter = (q or stage_filter or dx or doctor or hospital or
                           stale_only or new_30d_only or
                           discharge_imminent_only or emergency_overdue_only or
                           include_archived)
    if view != "all" and not any_explicit_filter:
        stage_filter = ["입원", "응급치료", "입원대기"]
    # 액션 필터(퇴원임박·응급복귀)는 단계 무관 (모든 단계에서 적용)
    if discharge_imminent_only or emergency_overdue_only:
        stage_filter = None

    patients = models.lifecycle_board(
        q=q, period_days=period_days, stages=stage_filter,
        disease_group=dx, doctor=doctor, include_archived=include_archived,
        stale_only=stale_only, new_30d_only=new_30d_only,
    )
    # 모병원 필터 (post-filter)
    if hospital:
        pid_set = set()
        if patients:
            conn = models.get_db()
            placeholders = ",".join("?" * len(patients))
            rows = conn.execute(
                f"SELECT DISTINCT patient_id FROM consultations "
                f"WHERE patient_id IN ({placeholders}) AND source_hospital = ?",
                [p["id"] for p in patients] + [hospital],
            ).fetchall()
            pid_set = {r["patient_id"] for r in rows}
            conn.close()
        patients = [p for p in patients if p["id"] in pid_set]
    # 입원중 단계 환자의 퇴원 D-day 계산 (의료법 입원기간 룰 기반)
    for pt in patients:
        if pt.get("last_admission_status") == "입원완료" and pt.get("last_consult_id"):
            con = models.get_consultation(pt["last_consult_id"])
            if con:
                dw = _discharge_watch(con)
                if dw:
                    pt["discharge_dday"] = dw["days_left"]
                    pt["discharge_due_date"] = dw["due_date"]
    # KPI 계산은 액션 필터 적용 전 patients 기준 (전체 카운트 정확)
    kpis = models.lifecycle_board_kpis(patients)
    # 액션 필터 (퇴원 임박·응급 복귀 미기록) — KPI 계산 후 post-filter
    if discharge_imminent_only:
        patients = [p for p in patients
                    if p.get("discharge_dday") is not None and p["discharge_dday"] <= 3]
    if emergency_overdue_only:
        patients = [p for p in patients
                    if p.get("lifecycle_stage") == "응급치료"
                    and (p.get("stage_days_int") or 0) >= 3]
    board = {s: [] for s in LIFECYCLE_STAGES}
    board["기타"] = []
    for pt in patients:
        st = pt.get("lifecycle_stage") or "기타"
        board.setdefault(st if st in board else "기타", []).append(pt)
    if not board["기타"]:
        board.pop("기타")
    # 단계별 카운트는 KPI 필터 영향 받음 — 카테고리 카드는 전체 환자 기준으로 별도 조회가 필요할 수도 있으나
    # 일단 보드와 동기화된 값으로 표시 (현재 상황 = 활성 단계만)
    # 사이드 패널 데이터 (모병원·응급전원)
    side = models.lifecycle_board_side(patients)
    # 주치의 옵션 — 최근 상담에서 추출 (config 5명 + 자유 입력 환자가 있을 수 있음)
    doctor_options = sorted(set(filter(None,
        (p.get("last_doctor") for p in patients))))
    return render_template(
        "lifecycle.html", board=board, q=q or "", total=len(patients),
        kpis=kpis, side=side,
        filters={
            "period": period_raw, "stages": stage_filter or [],
            "dx": dx or "", "doctor": doctor or "", "hospital": hospital or "",
            "archived": include_archived,
            "stale": stale_only, "new30": new_30d_only,
            "discharge_imminent": discharge_imminent_only,
            "emergency_overdue": emergency_overdue_only,
            "view": view,
        },
        LIFECYCLE_STAGES=LIFECYCLE_STAGES,
        doctor_options=doctor_options,
        DISEASE_GROUPS=list(DISEASES_GROUPS.keys()),
    )


@app.route("/api/patient/<int:pid>/stage", methods=["POST"])
@login_required
def api_patient_stage(pid):
    if not models.get_patient(pid):
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    stage = (payload.get("stage") or "").strip()
    if stage and stage not in LIFECYCLE_STAGES:
        return jsonify({"error": "허용되지 않은 단계값"}), 400
    models.set_patient_stage(pid, stage or None)
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="update_stage", target_type="patient", target_id=pid,
        detail=stage or "(미설정)", ip=request.remote_addr,
    )
    return jsonify({"ok": True, "stage": stage})


@app.route("/api/patient/<int:pid>/lifecycle/event", methods=["POST"])
@login_required
def api_lifecycle_event_add(pid):
    if not models.get_patient(pid):
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    event_type = (payload.get("event_type") or "").strip()
    if event_type not in LIFECYCLE_EVENT_TYPES:
        return jsonify({"error": "이벤트 유형을 선택하세요."}), 400
    event_date = (payload.get("event_date") or "").strip() or None
    if event_date:
        try:
            datetime.strptime(event_date, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "이벤트 일자 형식 오류 (YYYY-MM-DD)"}), 400
    eid = models.add_lifecycle_event(
        patient_id=pid, event_type=event_type, event_date=event_date,
        title=(payload.get("title") or "").strip() or None,
        detail=(payload.get("detail") or "").strip() or None,
        created_by=g.user.get("display_name"),
    )
    # 트리거 ③ — 생애주기 이벤트가 단계 전환 신호인 경우 환자 단계 자동 변경
    stage_map = {
        "회복기 전환": "회복기",
        "비회복기 전환": "비회복기",
        "응급치료": "응급치료",
        "복귀": "입원",
        "퇴원": "퇴원",
    }
    target = stage_map.get(event_type)
    if target:
        _set_lifecycle_stage_clinical(pid, target)
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="add_lifecycle_event", target_type="patient", target_id=pid,
        detail=event_type, ip=request.remote_addr,
    )
    return jsonify({"ok": True, "id": eid})


@app.route("/api/lifecycle/event/<int:eid>", methods=["DELETE"])
@login_required
def api_lifecycle_event_delete(eid):
    ev = models.get_lifecycle_event(eid)
    if not ev:
        return jsonify({"error": "not found"}), 404
    models.delete_lifecycle_event(eid)
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="delete_lifecycle_event", target_type="patient",
        target_id=ev["patient_id"], detail=ev.get("event_type"),
        ip=request.remote_addr,
    )
    return jsonify({"ok": True})


@app.route("/api/patient/<int:pid>/blacklist", methods=["POST"])
@login_required
def api_patient_blacklist(pid):
    """블랙리스트 지정/해제 (4번 요청)."""
    if not models.get_patient(pid):
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    on = bool(payload.get("blacklist"))
    reason = (payload.get("blacklist_reason") or "").strip() or None
    if on and not reason:
        return jsonify({"error": "블랙리스트 지정 사유를 입력하세요."}), 400
    models.set_patient_blacklist(pid, on, reason)
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="update_blacklist", target_type="patient", target_id=pid,
        detail=("ON: " + (reason or "")) if on else "OFF", ip=request.remote_addr,
    )
    return jsonify({"ok": True, "blacklist": on})


@app.route("/api/patient/blacklist-check")
@login_required
def api_blacklist_check():
    """이름·연락처로 블랙리스트 환자 여부 조회 (신규 상담 등록 전 경고용)."""
    name = (request.args.get("name") or "").strip()
    phone = (request.args.get("phone") or "").strip()
    hit = models.find_blacklisted(name=name, phone=phone)
    if hit:
        return jsonify({"blacklisted": True, "name": hit.get("name"),
                        "reason": hit.get("blacklist_reason") or ""})
    return jsonify({"blacklisted": False})


# ───────────────────── 옴니채널 — 인박스·커뮤니케이션 ─────────────────────

@app.route("/inbox")
@login_required
def inbox_view():
    """통합 인박스 — 재연락 대기 + 미처리 인바운드 + 입원안내 예정 + 퇴원예정.
    상담사의 '오늘 처리할 일'을 채널 무관하게 한곳에 모은다.
    """
    callbacks = models.inbox_callbacks()
    open_comms = models.inbox_open_communications()
    upcoming = models.inbox_upcoming_admissions(within_days=3)
    # 퇴원예정 — 입원완료 상담 중 퇴원 임박
    discharge_due = []
    for con in models.list_consultations(admission_status="입원완료", limit=10000):
        dw = _discharge_watch(con)
        if dw and dw.get("state"):
            discharge_due.append({"con": con, "watch": dw})
    discharge_due.sort(key=lambda x: x["watch"]["days_left"])
    return render_template(
        "inbox.html", callbacks=callbacks, open_comms=open_comms,
        upcoming=upcoming, discharge_due=discharge_due,
    )


@app.route("/api/communication", methods=["POST"])
@login_required
def api_communication_create():
    """인바운드/기타 커뮤니케이션 1건 기록 (받은 문자·카톡·웹문의·부재중 등)."""
    payload = request.get_json(silent=True) or {}
    channel = (payload.get("channel") or "").strip()
    if channel not in COMM_CHANNELS:
        return jsonify({"error": "채널을 선택하세요."}), 400
    direction = "out" if payload.get("direction") == "out" else "in"
    summary = (payload.get("summary") or "").strip()
    body = (payload.get("body") or "").strip()
    if not summary and not body:
        return jsonify({"error": "요약 또는 내용을 입력하세요."}), 400
    pid = payload.get("patient_id")
    cid = models.create_communication(
        patient_id=pid, consultation_id=payload.get("consultation_id"),
        channel=channel, direction=direction,
        contact=(payload.get("contact") or "").strip() or None,
        summary=summary or None, body=body or None,
        follow_up_at=(payload.get("follow_up_at") or "").strip() or None,
        occurred_at=(payload.get("occurred_at") or "").strip() or None,
        created_by=g.user.get("display_name"),
        status="open" if direction == "in" else "done",
    )
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="add_communication", target_type="patient", target_id=pid,
        detail=f"{channel}/{direction}", ip=request.remote_addr,
    )
    return jsonify({"ok": True, "id": cid})


@app.route("/api/communication/<int:comm_id>/done", methods=["POST"])
@login_required
def api_communication_done(comm_id):
    if not models.get_communication(comm_id):
        return jsonify({"error": "not found"}), 404
    models.update_communication(comm_id, status="done")
    return jsonify({"ok": True})


@app.route("/api/communication/<int:comm_id>", methods=["DELETE"])
@login_required
def api_communication_delete(comm_id):
    if not models.get_communication(comm_id):
        return jsonify({"error": "not found"}), 404
    models.delete_communication(comm_id)
    return jsonify({"ok": True})


@app.route("/api/consult/<int:cid>/admission-event", methods=["POST"])
@login_required
def api_admission_event_create(cid):
    """입원 중 이벤트 1건 추가 (응급전원·모병원 외래치료 등)."""
    if not models.get_consultation(cid):
        return jsonify({"error": "상담을 찾을 수 없습니다."}), 404
    payload = request.get_json(silent=True) or {}
    event_type = (payload.get("event_type") or "").strip()
    if event_type not in ADMISSION_EVENT_TYPES:
        return jsonify({"error": "이벤트 유형을 선택하세요."}), 400
    event_date = (payload.get("event_date") or "").strip()
    if event_date:
        try:
            datetime.strptime(event_date, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "발생일 형식 오류"}), 400
    eid = models.create_admission_event(
        consultation_id=cid, event_type=event_type,
        event_date=event_date or None,
        hospital=(payload.get("hospital") or "").strip() or None,
        memo=(payload.get("memo") or "").strip() or None,
        created_by=g.user.get("display_name"),
    )
    # 트리거 ② — 입원 중 이벤트가 단계 전환 신호인 경우 환자 단계 자동 변경
    con = models.get_consultation(cid)
    if con:
        stage_map = {"응급전원": "응급치료", "모병원 외래치료": "응급치료", "복귀": "입원"}
        target = stage_map.get(event_type)
        if target:
            _set_lifecycle_stage_clinical(con["patient_id"], target)
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="add_admission_event", target_type="consultation", target_id=cid,
        detail=event_type, ip=request.remote_addr,
    )
    return jsonify({"ok": True, "id": eid})


@app.route("/api/admission-event/<int:event_id>", methods=["DELETE"])
@login_required
def api_admission_event_delete(event_id):
    if not models.get_admission_event(event_id):
        return jsonify({"error": "not found"}), 404
    models.delete_admission_event(event_id)
    return jsonify({"ok": True})


@app.route("/api/webhook/kakao", methods=["POST"])
def api_webhook_kakao():
    """카카오 비즈채널 인바운드 webhook 수신 자리 — 구조만 (비즈채널 연동 시 작동).
    .env의 KAKAO_WEBHOOK_TOKEN으로 호출자 검증. 보호자 번호로 환자 자동 매칭해
    communications에 인바운드로 기록 → 인박스에 노출.
    실제 카카오 페이로드 형식은 채널 연동 시 확정 (현재는 범용 형태 수신).
    """
    expected = os.getenv("KAKAO_WEBHOOK_TOKEN", "").strip()
    if not expected:
        return jsonify({"error": "webhook 미설정 — .env KAKAO_WEBHOOK_TOKEN 필요"}), 503
    token = request.headers.get("X-Webhook-Token") or request.args.get("token") or ""
    if token != expected:
        return jsonify({"error": "unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    phone = (payload.get("phone") or "").strip()
    name = (payload.get("name") or "").strip()
    message = (payload.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message 필수"}), 400
    pid = models.match_patient_by_phone(phone)
    comm_id = models.create_communication(
        patient_id=pid, channel="카카오", direction="in",
        contact=phone or name or None,
        summary="카카오 메시지" + (f" · {name}" if name else ""),
        body=message, status="open", created_by="카카오봇",
    )
    return jsonify({"ok": True, "id": comm_id, "matched_patient": pid})


# ───────────────────── 문자 발송 (5번 요청) ─────────────────────

@app.route("/sms")
@login_required
def sms_compose():
    """문자 전송 — 최근 상담에서 보호자 선택 → 환자군 템플릿 → 발송."""
    recent = models.list_consultations(limit=200)
    cid = request.args.get("cid", type=int)
    pid = request.args.get("pid", type=int)
    preselect = models.get_consultation(cid) if cid else None
    # 선택 대상이 최근 200건 밖이면 드롭다운에 옵션이 없어 프리필이 안 된다
    # (인박스 퇴원예정 등 오래된 상담의 '문자' 버튼 진입 케이스) → 목록 맨 앞에 보강.
    if preselect and not any(r["id"] == preselect["id"] for r in recent):
        recent = [preselect] + recent
    # ← 돌아가기 — 진입 경로 추론 (cid → 상담상세 / pid → 환자상세 / 그 외 → 인박스)
    if cid and preselect:
        back_url, back_label = f"/consult/{cid}", "← 상담 상세"
    elif pid:
        back_url, back_label = f"/patients/{pid}", "← 환자 상세"
    else:
        back_url, back_label = "/inbox", "← 인박스"
    return render_template(
        "sms.html", recent=recent, templates=models.list_sms_templates(),
        preselect=preselect, log=models.list_sms_log(30),
        placeholders=SMS_PLACEHOLDERS,
        gateway_ready=sms_gateway.gateway_configured(),
        back_url=back_url, back_label=back_label,
    )


@app.route("/sms/templates")
@login_required
def sms_templates_view():
    return render_template(
        "sms_templates.html",
        templates=models.list_sms_templates(active_only=False),
    )


@app.route("/api/sms/template", methods=["POST"])
@login_required
def api_sms_template_create():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    body = (payload.get("body") or "").strip()
    group = (payload.get("template_group") or "공통").strip()
    if not name or not body:
        return jsonify({"error": "템플릿 이름과 본문을 입력하세요."}), 400
    tid = models.create_sms_template(
        name=name, body=body,
        template_group=group if group in SMS_TEMPLATE_GROUPS else "공통",
    )
    return jsonify({"ok": True, "id": tid})


@app.route("/api/sms/template/<int:tid>", methods=["POST"])
@login_required
def api_sms_template_update(tid):
    if not models.get_sms_template(tid):
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    fields = {}
    if "name" in payload:
        fields["name"] = (payload.get("name") or "").strip()
    if "body" in payload:
        fields["body"] = (payload.get("body") or "").strip()
    if "template_group" in payload:
        gr = (payload.get("template_group") or "공통").strip()
        fields["template_group"] = gr if gr in SMS_TEMPLATE_GROUPS else "공통"
    if "active" in payload:
        fields["active"] = 1 if payload.get("active") else 0
    if fields.get("name") == "" or fields.get("body") == "":
        return jsonify({"error": "이름·본문은 비울 수 없습니다."}), 400
    models.update_sms_template(tid, **fields)
    return jsonify({"ok": True})


@app.route("/api/sms/template/<int:tid>", methods=["DELETE"])
@login_required
def api_sms_template_delete(tid):
    if not models.get_sms_template(tid):
        return jsonify({"error": "not found"}), 404
    models.delete_sms_template(tid)
    return jsonify({"ok": True})


@app.route("/api/sms/send", methods=["POST"])
@login_required
def api_sms_send():
    """문자 발송 — 게이트웨이 설정 시 직접 발송, 미설정 시 'manual'(휴대폰 문자앱).
    어느 쪽이든 sms_log에 이력을 남긴다. 환자정보 보호: 외부 전송은 수신번호·본문 한정.
    """
    payload = request.get_json(silent=True) or {}
    to_phone = (payload.get("to_phone") or "").strip()
    body = (payload.get("body") or "").strip()
    if not to_phone or not body:
        return jsonify({"error": "수신 번호와 본문이 필요합니다."}), 400

    status, error = "manual", None
    if sms_gateway.gateway_configured():
        result = sms_gateway.send_sms(to_phone, body)
        status = "sent" if result.get("ok") else "failed"
        error = result.get("error")

    sid = models.log_sms(
        consultation_id=payload.get("consultation_id"),
        patient_id=payload.get("patient_id"),
        template_id=payload.get("template_id"),
        to_name=(payload.get("to_name") or "").strip() or None,
        to_phone=to_phone, body=body, status=status,
        sent_by=g.user.get("display_name"),
    )
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="send_sms", target_type="consultation",
        target_id=payload.get("consultation_id"),
        detail=f"{to_phone} [{status}]", ip=request.remote_addr,
    )
    return jsonify({"ok": True, "id": sid, "status": status, "error": error})


# ───────────────────── API: 자동완성 ─────────────────────

@app.route("/api/autocomplete/hospital")
@login_required
def api_ac_hospital():
    q = (request.args.get("q") or "").strip()
    if len(q) < 1:
        return jsonify({"items": []})
    return jsonify({"items": models.autocomplete_hospitals(q, limit=10)})


@app.route("/api/autocomplete/diagnosis")
@login_required
def api_ac_diagnosis():
    q = (request.args.get("q") or "").strip()
    if len(q) < 1:
        return jsonify({"items": []})
    return jsonify({"items": models.autocomplete_diagnoses(q, limit=10)})


@app.route("/api/autocomplete/patient")
@login_required
def api_ac_patient():
    q = (request.args.get("q") or "").strip()
    if len(q) < 1:
        return jsonify({"items": []})
    return jsonify({"items": models.autocomplete_patients(q, limit=10)})


# ───────────────────── helpers ─────────────────────

def _list_filters_from_request():
    def _int_or_none(v):
        try:
            return int(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None
    return {
        "date_from": request.args.get("from") or None,
        "date_to": request.args.get("to") or None,
        "insurance": request.args.get("insurance") or None,
        "counselor": request.args.get("counselor") or None,
        "admission_status": request.args.get("admission_status") or None,
        "consult_result": request.args.get("consult_result") or None,
        "blacklist": "1" if request.args.get("blacklist") else None,
        "disease_group": request.args.get("disease_group") or None,
        "residence_sido": request.args.get("residence_sido") or None,
        "recovery": request.args.get("recovery") or None,
        "consult_channel": request.args.get("consult_channel") or None,
        "referral_type": request.args.get("referral_type") or None,
        "q": request.args.get("q") or None,
        # 컬럼별 필터 — 성별·나이 범위·보호자·모병원
        "gender": request.args.get("gender") or None,
        "age_min": _int_or_none(request.args.get("age_min")),
        "age_max": _int_or_none(request.args.get("age_max")),
        "guardian": request.args.get("guardian") or None,
        "hospital": request.args.get("hospital") or None,
    }


def _stats_period_from_request():
    """preset(this_month/last_month/this_quarter/last_quarter/ytd/custom) → (preset, from, to).
    custom일 때만 from/to 쿼리스트링을 사용. 기본은 this_month.
    """
    preset = (request.args.get("preset") or "this_month").strip()
    today = datetime.now().date()
    y, m = today.year, today.month

    def _q_range(year, q_idx):
        start_m = (q_idx - 1) * 3 + 1
        end_m = start_m + 2
        from datetime import date
        from calendar import monthrange
        start = date(year, start_m, 1)
        end = date(year, end_m, monthrange(year, end_m)[1])
        return start.isoformat(), end.isoformat()

    if preset == "this_month":
        from calendar import monthrange
        date_from = today.replace(day=1).isoformat()
        date_to = today.replace(day=monthrange(y, m)[1]).isoformat()
    elif preset == "last_month":
        from calendar import monthrange
        from datetime import date
        if m == 1:
            ly, lm = y - 1, 12
        else:
            ly, lm = y, m - 1
        date_from = date(ly, lm, 1).isoformat()
        date_to = date(ly, lm, monthrange(ly, lm)[1]).isoformat()
    elif preset == "this_quarter":
        date_from, date_to = _q_range(y, (m - 1) // 3 + 1)
    elif preset == "last_quarter":
        cq = (m - 1) // 3 + 1
        if cq == 1:
            date_from, date_to = _q_range(y - 1, 4)
        else:
            date_from, date_to = _q_range(y, cq - 1)
    elif preset == "ytd":
        from datetime import date
        date_from = date(y, 1, 1).isoformat()
        date_to = today.isoformat()
    elif preset == "custom":
        date_from = request.args.get("from") or None
        date_to = request.args.get("to") or None
    else:
        preset = "this_month"
        from calendar import monthrange
        date_from = today.replace(day=1).isoformat()
        date_to = today.replace(day=monthrange(y, m)[1]).isoformat()
    return preset, date_from, date_to


def _int(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


_KR_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def _weekday_kr(date_str):
    if not date_str:
        return ""
    try:
        return _KR_WEEKDAYS[datetime.strptime(date_str[:10], "%Y-%m-%d").weekday()]
    except (ValueError, TypeError):
        return ""


def _csv_list(v):
    if isinstance(v, list):
        return ", ".join(v)
    return v or ""


# 상담 페이로드에서 모델 필드만 골라내고 적절히 캐스팅한다.
# JSON 배열 필드(diseases 등)는 models 쪽에서 직렬화하므로 list 그대로 전달.
def _consult_fields_from_payload(c: dict) -> dict:
    out = {}
    for key in models.CONSULT_FIELDS:
        if key not in c:
            continue
        v = c[key]
        if key == "patient_age":
            out[key] = _int(v)
        elif isinstance(v, str):
            out[key] = v.strip() or None
        else:
            out[key] = v
    # 모병원 자동 매핑: '현재' 라디오가 입원중이면 병원명, 입소중이면 요양원명을
    # source_hospital 컬럼에 함께 기록. 자택 거주는 모병원 없음(통계 분석에서 제외).
    loc_type = out.get("current_location_type")
    if loc_type == "입원중" and out.get("current_location_name"):
        out["source_hospital"] = out["current_location_name"]
    elif loc_type == "입소중" and out.get("current_nursing_name"):
        out["source_hospital"] = out["current_nursing_name"]
    # 입원경로(다중) — 선택된 항목들로부터 상위 그룹(온라인/소개/기타)을 중복없이 도출
    detail = out.get("referral_source_detail")
    if isinstance(detail, list) and detail:
        types = []
        for d in detail:
            for group_name, options in REFERRAL_SOURCE_GROUPS.items():
                if d in options and group_name not in types:
                    types.append(group_name)
        out["referral_source_type"] = types
    elif isinstance(detail, str) and detail:
        # 과거 단일값 호환
        for group_name, options in REFERRAL_SOURCE_GROUPS.items():
            if detail in options:
                out["referral_source_detail"] = [detail]
                out["referral_source_type"] = [group_name]
                break
    return out


def _validate_consult_payload(payload, *, require_patient):
    if not isinstance(payload, dict):
        return "잘못된 요청 형식"
    if require_patient:
        p = payload.get("patient") or {}
        if not (p.get("name") or "").strip():
            return "환자 이름이 필요합니다."
    c = payload.get("consultation") or {}
    cd = c.get("consult_date")
    if cd:
        try:
            datetime.strptime(cd, "%Y-%m-%d")
        except ValueError:
            return "상담일자 형식이 올바르지 않습니다 (YYYY-MM-DD)."
    # 상담 결과 ① 상담 진행 — 화이트리스트 + 사유 필수 (재입원/요청/보류/취소)
    cr = (c.get("consult_result") or "").strip()
    if cr and cr not in CONSULT_RESULTS:
        return "허용되지 않은 상담 결과값입니다."
    if cr in CONSULT_RESULT_REASON_LABELS and not (c.get("consult_result_reason") or "").strip():
        return f"{CONSULT_RESULT_REASON_LABELS[cr]}을(를) 입력하세요."
    # 상담 결과 ② 입원 진행 — 화이트리스트 + 보류/취소 사유 필수
    status = (c.get("admission_status") or "").strip()
    if status and status not in ADMISSION_STATUSES:
        return "허용되지 않은 입원 진행값입니다."
    if status == "입원보류" and not (c.get("hold_reason") or "").strip():
        return "입원보류 사유를 입력하세요."
    if status == "입원취소":
        reason = (c.get("rejection_reason") or "").strip()
        detail = (c.get("rejection_reason_detail") or "").strip()
        if reason and reason not in REJECTION_REASONS:
            return "허용되지 않은 입원취소 사유입니다."
        if not reason and not detail:
            return "입원취소 사유를 입력하세요."
    return None


# ───────────────────── 에러 ─────────────────────

@app.errorhandler(404)
def _404(_):
    return render_template("error.html", code=404, msg="페이지를 찾을 수 없습니다."), 404


@app.errorhandler(403)
def _403(_):
    return render_template("error.html", code=403, msg="접근 권한이 없습니다."), 403


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8003"))
    host = "0.0.0.0" if os.getenv("ALLOW_LAN", "0") == "1" else "127.0.0.1"
    app.run(host=host, port=port, debug=os.getenv("FLASK_DEBUG") == "1")
