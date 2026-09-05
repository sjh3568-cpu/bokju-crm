"""Flask 앱 — 복주회복병원 상담실 CRM (bokju-crm).

진입점. 인증·상담 등록/목록/상세·자동완성·통계 API를 한 파일에 모음.
규모가 커지면 Blueprint로 쪼갤 것 (현재는 cafe-helper 스타일 단일 파일).
"""
import csv
import hashlib
import io
import json
import logging
import os
import secrets
import threading
import calendar
from functools import lru_cache
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from dotenv import load_dotenv
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)

import backup
import models
from auth import (
    admin_required, authenticate, current_user, is_locked_out,
    login_required, login_user, logout_user, menu_level,
)
from config import (
    ACTIVITY_ACTIVE_OPTIONS, ACTIVITY_DIAPER_OPTIONS, ACTIVITY_OTHERS_OPTIONS,
    ACTIVITY_WHEELCHAIR_OPTIONS, ADMISSION_DOCS, ADMISSION_STATUSES,
    AUDIT_ACTION_LABELS, AUDIT_CATEGORIES, AUDIT_CATEGORY_OTHER,
    AUDIT_CRITICAL_ACTIONS, AUDIT_RETENTION_DAYS,
    ADMISSION_EVENT_TYPES, ATTENDING_DOCTORS,
    BED_OPTIONS, CAREGIVER_OPTIONS,
    CONSCIOUSNESS_MAIN_OPTIONS, CONSULT_CHANNELS, CONVERSATION_LEVEL_OPTIONS,
    CONSULT_RESULTS, CONSULT_RESULT_REASON_LABELS, REJECTION_REASONS,
    COMM_CHANNELS, COMM_INBOUND_CHANNELS,
    COST_GUIDANCE_OPTIONS, CURRENT_LOCATION_TYPES, DIET_TYPES, DIET_LAYOUT,
    DISEASES_CHECKLIST, DISEASES_GROUPS, GUARDIAN_RELATION_SUGGESTIONS,
    HEARING_OPTIONS, INFO_PROVIDED_OPTIONS,
    COUNSELORS, DISEASES_LAYOUT, OTHERS_LAYOUT, ROOM_CAPACITY,
    WARDS, MGMT_TAG_PRESETS,
    ROLE_LABELS, SEED_USERS,
    MENUS, MENU_KEYS, MENU_MAX_LEVEL, ROLE_PRESETS, role_preset,
    PERM_HIDDEN, PERM_VIEW, PERM_EDIT, PERM_CREATE,
    PERM_LEVELS, PERM_LEVEL_LABELS,
    INSURANCE_TYPES, OTHERS_CHECKLIST, REFERRAL_SOURCE_GROUPS, REFERRAL_TYPES,
    LIFECYCLE_STAGES, LIFECYCLE_EVENT_TYPES, LEGACY_STAGE_MAP, CARE_PHASES,
    SIDO_LIST, SIGUNGU_INDEX, SIGUNGU_LIST,
    SMS_TEMPLATE_GROUPS, SMS_PLACEHOLDERS,
    SPECIAL_CARE_OPTIONS, SPECIAL_CARE_NOTE_FIELDS,
    THERAPY_OPTIONS, TRANSPORT_OPTIONS, WOUND_CARE_OPTIONS, WOUND_CARE_NOTE_FIELDS,
)
import sms as sms_gateway

# 할 일 달력 음력·공휴일 (미설치 환경에서도 앱은 동작하도록 방어적 import)
try:
    import holidays as _holidays
except Exception:
    _holidays = None
try:
    from korean_lunar_calendar import KoreanLunarCalendar as _KLC
except Exception:
    _KLC = None


def _lunar_label(d):
    """양력 date → '음 M.D' (윤달이면 '윤' 접두). 라이브러리 없으면 ''."""
    if not _KLC:
        return ""
    try:
        c = _KLC()
        c.setSolarDate(d.year, d.month, d.day)
        lead = "윤" if c.isIntercalation else "음"
        return f"{lead} {c.lunarMonth}.{c.lunarDay}"
    except Exception:
        return ""


@lru_cache(maxsize=8)
def _kr_holidays(years):
    """연도 튜플에 대한 한국 공휴일 dict (캐시). years=(2026, 2027) 등."""
    if not _holidays:
        return {}
    try:
        return dict(_holidays.SouthKorea(years=list(years)))
    except Exception:
        return {}

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(hours=int(os.getenv("SESSION_HOURS", "4")))
_REMEMBER_COOKIE = "bokju_remember"
_REMEMBER_DAYS = max(1, int(os.getenv("AUTO_LOGIN_DAYS", "30")))
_remember_serializer = URLSafeTimedSerializer(app.secret_key, salt="bokju-auto-login-v1")

_db_initialized = False
# 다중 스레드(waitress) 환경에서 첫 요청 여러 건이 동시에 들어오면 init_db()가
# 겹쳐 돌아 ALTER TABLE·1회성 마이그레이션이 중복 실행된다. 락으로 한 번만 돌린다.
_bootstrap_lock = threading.Lock()


def initialize():
    """DB 초기화 + admin 계정 셋업 + 백업 스케줄러 기동. 몇 번 불러도 1회만 실행된다.
    .env의 APP_PASSWORD를 admin 계정 비밀번호로 자동 동기화 (단일 비밀번호 MVP).

    serve.py(운영)는 기동 시점에, 개발 서버는 첫 요청 시점에 호출한다.
    """
    global _db_initialized
    if _db_initialized:
        return
    with _bootstrap_lock:
        if _db_initialized:
            return
        models.init_db()
        admin_pw = os.getenv("APP_PASSWORD", "").strip()
        if admin_pw:
            # 비상용 break-glass 계정 (매 부팅 시 .env 비번으로 동기화)
            models.ensure_admin_user("admin", admin_pw, display_name="admin(비상)")
            # 명명된 6개 계정 시드 — 없을 때만 생성, 초기 비번=APP_PASSWORD
            for username, display_name, role in SEED_USERS:
                models.ensure_seed_user(username, display_name, role, admin_pw)
        _db_initialized = True
    if os.getenv("BACKUP_ENABLED", "1") == "1":
        backup.start_scheduler()


@app.before_request
def _bootstrap():
    initialize()


def _remember_fingerprint(user):
    """비밀번호 변경 시 기존 자동 로그인 토큰이 즉시 무효화되도록 해시 일부를 묶는다."""
    return hashlib.sha256((user.get("password_hash") or "").encode()).hexdigest()[:20]


@app.before_request
def _restore_remembered_login():
    """4시간 세션이 끝난 뒤에도 유효한 자동 로그인 쿠키가 있으면 세션을 복원한다."""
    if current_user() or request.path.startswith(("/logout", "/static/")):
        return
    token = request.cookies.get(_REMEMBER_COOKIE)
    if not token:
        return
    try:
        payload = _remember_serializer.loads(token, max_age=_REMEMBER_DAYS * 86400)
        user = models.get_user_by_id(int(payload.get("uid") or 0))
        if (not user or not user.get("active")
                or payload.get("fp") != _remember_fingerprint(user)):
            raise BadSignature("invalid remembered user")
        login_user(user)
        models.touch_user_login(user["id"])
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        g.clear_remember_cookie = True


# 인증·권한 판정에서 제외하는 경로 (로그인 전이거나 공용 도구)
_PERM_EXEMPT_PREFIXES = (
    "/login", "/logout", "/healthz", "/static/", "/help",
    "/tools/period-calc", "/period-calc", "/notices",
)

# 신규 '등록'으로 취급하는 쓰기 경로 (그 외 쓰기는 '수정' 레벨로 판정)
_CREATE_PATHS = (
    "/api/consult",          # 새 상담 저장 (정확히 이 경로일 때만, 아래에서 검사)
    "/api/sms/send",         # 문자 발송
    "/api/sms/template",     # 문자 템플릿 추가/저장
    "/api/communication",    # 커뮤니케이션(인바운드) 기록
)


def _route_requirement(path: str, method: str):
    """요청 경로·메서드 → (menu_key, 필요_레벨). 판정 대상이 아니면 (None, 0).

    경로 접두어로 메뉴를 정하고, 메서드·세부 경로로 필요 레벨을 정한다.
      · GET 조회 화면 → 조회(1)
      · 새 상담 폼/저장, 문자 발송, CSV 등 '신규 생성' → 등록(3)
      · 상담 수정 폼, 상태 변경, 재원 액션 등 기존 변경 → 수정(2)
    """
    for pref in _PERM_EXEMPT_PREFIXES:
        if path == pref or path.startswith(pref):
            return None, 0

    is_write = method not in ("GET", "HEAD", "OPTIONS")

    # ── 사용자 관리·이력 관리 (users 메뉴 수정↑) ──
    if path.startswith("/admin/"):
        return "users", PERM_EDIT

    # ── 상담 ──
    if (path == "/consultations.csv"):
        return "consult", PERM_CREATE          # 내보내기 = 전체 권한
    if path == "/consult/new":
        return "consult", PERM_CREATE
    if path.startswith("/consult/") and path.endswith("/edit"):
        return "consult", PERM_EDIT
    if path.startswith("/consultations") or path.startswith("/consult/") \
            or path == "/consult" or path.startswith("/api/consult") \
            or path.startswith("/api/quick-filters") or path.startswith("/api/autocomplete"):
        # 재원 관련 상담 액션(입원확정·외진·퇴원)은 재원 메뉴로 분류
        if any(seg in path for seg in ("/admit", "/discharge", "/admission-event")):
            return "ward", (PERM_EDIT if is_write else PERM_VIEW)
        if not is_write:
            return "consult", PERM_VIEW
        if path == "/api/consult":             # 신규 상담 저장
            return "consult", PERM_CREATE
        return "consult", PERM_EDIT

    # ── 재원 관리 (환자·병동·생애주기·외진) ──
    if path.startswith("/ward") or path.startswith("/patients") \
            or path.startswith("/api/patient") or path.startswith("/api/admission-event") \
            or path.startswith("/lifecycle"):
        return "ward", (PERM_EDIT if is_write else PERM_VIEW)

    # ── 문자 / 커뮤니케이션 ──
    if path.startswith("/sms") or path.startswith("/api/sms") \
            or path.startswith("/api/communication") or path.startswith("/api/webhook"):
        if not is_write:
            return "sms", PERM_VIEW
        return "sms", PERM_CREATE               # 발송·템플릿·기록 = 생성

    # ── 통계 ──
    if path.startswith("/stats") or path.startswith("/api/stats"):
        return "stats", PERM_VIEW

    # ── 월간보고서 ──
    if path.startswith("/report") or path.startswith("/api/report"):
        return "report", PERM_VIEW

    # ── 대시보드 (루트) ──
    if path == "/" or path.startswith("/api/dashboard"):
        return "dashboard", PERM_VIEW

    return None, 0


@app.before_request
def _enforce_menu_permissions():
    """계정별 메뉴 권한 매트릭스로 접근을 일괄 판정.
    로그인 안 됐으면 통과(각 뷰의 login_required가 처리). 권한 부족 시 403(또는 안내 후 되돌림).
    """
    user = current_user()
    if not user:
        return
    menu, required = _route_requirement(request.path, request.method)
    if menu is None:
        return
    if menu_level(user, menu) >= required:
        return
    if request.path.startswith("/api/"):
        abort(403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        flash("이 작업을 수행할 권한이 없습니다.", "error")
        return redirect(request.referrer or url_for("dashboard"))
    abort(403)


@app.before_request
def _require_announcement_acknowledgement():
    """필수 공지를 확인하기 전에는 다른 업무 화면으로 이동할 수 없게 한다."""
    user = current_user()
    if not user or request.path.startswith(("/login", "/logout", "/static/", "/notices")):
        return
    pending = models.first_unread_required_announcement(user["id"], user.get("role", "staff"))
    if not pending:
        return
    if request.path.startswith("/api/"):
        return jsonify({
            "error": "필수 공지를 먼저 확인해주세요.",
            "notice_id": pending["id"],
            "notice_url": url_for("notice_required"),
        }), 428
    return redirect(url_for("notice_required", next=request.full_path.rstrip("?")))


@app.after_request
def _no_store(resp):
    """환자 정보 페이지가 브라우저 캐시에 남지 않도록.
    로그아웃 후 뒤로가기로 노출되는 것 방지.
    """
    resp.headers["Cache-Control"] = "no-store, private, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    if getattr(g, "clear_remember_cookie", False):
        resp.delete_cookie(_REMEMBER_COOKIE, path="/", samesite="Lax")
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
    _u = current_user()
    # 나의 할 일 리마인드 배지 — 오늘+지난 미완료 개수 (로그인 시에만 조회)
    todo_badge = 0
    if _u:
        try:
            todo_badge = models.todo_badge_count(_u["id"], date.today().isoformat())
        except Exception:
            todo_badge = 0
    pending_notice = (models.first_unread_required_announcement(
        _u["id"], _u.get("role", "staff")) if _u else None)
    password_reset_badge = 0
    if _u and menu_level(_u, "users") >= PERM_EDIT:
        try:
            password_reset_badge = models.pending_password_reset_count()
        except Exception:
            password_reset_badge = 0
    return {
        "current_user": _u,
        "todo_badge": todo_badge,
        "has_unread_required_notice": bool(pending_notice),
        "password_reset_badge": password_reset_badge,
        "today_str": date.today().isoformat(),   # 날짜 입력 기본값(외진 기록 등)
        "INSURANCE_TYPES": INSURANCE_TYPES,
        "CONSULT_CHANNELS": CONSULT_CHANNELS,
        "ADMISSION_EVENT_TYPES": ADMISSION_EVENT_TYPES,
        "ATTENDING_DOCTORS": ATTENDING_DOCTORS,
        "ADMISSION_STATUSES": ADMISSION_STATUSES,
        "CONSULT_RESULTS": CONSULT_RESULTS,
        "CONSULT_RESULT_REASON_LABELS": CONSULT_RESULT_REASON_LABELS,
        "LIFECYCLE_STAGES": LIFECYCLE_STAGES,
        "CARE_PHASES": CARE_PHASES,
        "LIFECYCLE_EVENT_TYPES": LIFECYCLE_EVENT_TYPES,
        "SMS_TEMPLATE_GROUPS": SMS_TEMPLATE_GROUPS,
        "COMM_CHANNELS": COMM_CHANNELS,
        "COMM_INBOUND_CHANNELS": COMM_INBOUND_CHANNELS,
        "REJECTION_REASONS": REJECTION_REASONS,
        "GUARDIAN_RELATION_SUGGESTIONS": GUARDIAN_RELATION_SUGGESTIONS,
        "COUNSELORS": COUNSELORS,
        "ROLE_LABELS": ROLE_LABELS,
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


@app.template_filter("krdate_short_wd")
def _krdate_short_wd(value):
    """'2026-09-03' → '26.09.03(목)' (목록용 한 줄 축약 날짜)."""
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            return value
    wd = "월화수목금토일"[value.weekday()]
    return f"{value.strftime('%y.%m.%d')}({wd})"


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


def recovery_window_days(diseases):
    """진단군별 회복기 인정 기간(일). 매칭 없으면 0.
    중추신경계 90 / 비사용증후군·골유합 지연 60 / 근골격계·절단 30.
    """
    matched = 0
    for d in diseases or []:
        if not d:
            continue
        d_str = str(d)
        for kws, period in _RECOVERY_RULES:
            if any(kw in d_str for kw in kws):
                matched = max(matched, period)
                break
    return matched


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
    # 발병일이 1일째 (입원 당일 포함)
    days = (rd - od).days + 1
    if days < 1:
        return None
    matched = recovery_window_days(diseases)
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


def _purpose_to_category(admission_purpose: str | None) -> str | None:
    """admission_purpose 저장값 → 정규화된 category 4종.
    Returns: '회복기' | '비회복기' | '일반재활' | '요양' | None
    """
    p = (admission_purpose or "").strip()
    if p.startswith("비회복기재활") or p == "비회복기":
        return "비회복기"
    if p.startswith("회복기재활") or p == "회복기":
        return "회복기"
    if p.startswith("일반재활"):
        return "일반재활"
    if p.startswith("요양"):
        return "요양"
    return None


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
# 2026-08-21 사용자 확인 — 중추신경계와 그 외가 완전히 다른 구조다.
#   중추신경계: S005 회복기 180일 → S006 비회복기 → 입원일 + 1년(365일)
#               (90일을 넘겨 입원해도 급성기 치료 사유면 S044로 일부 인정)
#   그 외     : S005 밖에 없다. 재원 기간이 진단군별로 고정되어 있고
#               그 기간 안에 반드시 퇴원해야 한다 (S006 연장 구간 없음).
#               근골격계 단일 30일 / 다발·내고정술·치환술 60일 /
#               비사용증후군군·골유합 지연·하지 부위 절단 60일
RECOVERY_STAY_DAYS = 180   # 중추신경계 S005 산정 일수
TOTAL_STAY_DAYS = 365      # 중추신경계 총 재원 = 입원일 + 1년

_CNS_KW = ("뇌출혈", "뇌경색", "뇌손상", "척수손상", "뇌성마비",
           "마비", "편마비", "사지마비", "중추신경계")
# 비중추신경계 재원 일수 — 여러 개 매칭되면 가장 긴 값 적용
_NONCNS_STAY_RULES = [
    (("내고정술", "치환술", "다발"), 60),
    (("호흡질환", "폐질환", "심장질환", "신생물", "폐렴", "폐수종",
      "패혈증", "농양", "다제내성", "CRE", "VRE", "신부전",
      "동정맥루", "복부대동맥류", "급성복막염", "장폐색",
      "파킨슨(신규)", "길랑바레증후군", "비사용증후군"), 60),
    (("골유합 지연", "골유합지연"), 60),
    (("하지 부위 절단", "절단"), 60),
    (("고관절", "대퇴", "골반", "근골격계", "슬관절"), 30),
]


def is_cns_diseases(diseases):
    """중추신경계 진단군인지."""
    return any(any(kw in str(d) for kw in _CNS_KW) for d in (diseases or []) if d)


def noncns_stay_days(diseases):
    """비중추신경계 재원 일수. 매칭 없으면 0."""
    days = 0
    for d in (diseases or []):
        if not d:
            continue
        for kws, n in _NONCNS_STAY_RULES:
            if any(kw in str(d) for kw in kws):
                days = max(days, n)
                break
    return days


def compute_admission_period(diseases, recovery_label):
    """질환군 + 회복기/비회복기 → 입원 기간(입원 후 재원 가능 일수).
    Returns: dict(total, billing, mandatory) 또는 None(산정 불가).
      total     = 전체 입원 가능 일수
      billing   = 회복기 수가(S005) 인정 기간 — 중추신경계 회복기만 180, 그 외 None
      mandatory = 이 기간 안에 반드시 퇴원해야 하는지 (비중추신경계는 True)
    """
    if is_cns_diseases(diseases):
        if recovery_label == "회복기":
            return {"total": TOTAL_STAY_DAYS, "billing": RECOVERY_STAY_DAYS,
                    "mandatory": False}
        if recovery_label == "비회복기":
            return {"total": TOTAL_STAY_DAYS, "billing": None, "mandatory": False}
        # 회복기/비회복기 미상이면 중추신경계 입원 기간 산정 불가
        return None
    days = noncns_stay_days(diseases)
    if not days:
        return None
    # 비중추신경계는 전원 S005. 수가 구간 = 재원 기간 전체라 '전환' 개념이 없어
    # billing(전환 임박 경고용)은 비워두고 mandatory로 필수 퇴원을 알린다.
    return {"total": days, "billing": None, "mandatory": True}


@app.template_filter("admission_expiry")
def _admission_expiry(consultation):
    """입원일 + 입원 기간 → 입원 만료일(퇴원 예정일) 계산.

    ※ 외진(응급전원·모병원 외래치료) 기간은 차감하지 않는다 — 2026-08-25 사용자 확인.
      병상을 유지한 채 나갔다 오는 것이라 회복기(S005)·비회복기 수가 기간과
      입원 경과일이 그대로 흘러간다. 여기서 외진 일수를 빼면 실제 만료일보다
      늦게 계산돼 퇴원 시점을 놓친다.

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
    total_d = _day_of(ad, period["total"])
    out = {
        "basis": "actual" if actual else "planned",
        "mandatory": period.get("mandatory", False),
        "total_days": period["total"],
        "total_date": total_d.isoformat(),
        "total_left": (total_d - today).days,
        "billing_days": period["billing"],
        "billing_date": None,
        "billing_left": None,
    }
    if period["billing"]:
        bd = _day_of(ad, period["billing"])
        out["billing_date"] = bd.isoformat()
        out["billing_left"] = (bd - today).days
    extension_d = (total_d + timedelta(days=180)
                   if period["total"] == TOTAL_STAY_DAYS else None)
    out["extension_date"] = extension_d.isoformat() if extension_d else None
    out["extension_left"] = (extension_d - today).days if extension_d else None
    out["is_extended_6m"] = bool(
        period["total"] == TOTAL_STAY_DAYS
        and out["total_left"] < 0
        and out["extension_left"] is not None
        and out["extension_left"] >= 0
        and (consultation.get("admission_status") or "").strip() == "입원완료"
        and not (consultation.get("discharge_date") or "").strip()
    )
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
    ax_flag = _admission_expiry(consultation) or {}
    return {
        "mandatory": bool(ax_flag.get("mandatory")),
        "state": "퇴원예정" if days_left <= 30 else None,
        "due_date": dd.isoformat(),
        "days_left": days_left,
    }


def _care_phase(consultation):
    """입원 환자의 수가 구간 — 생애주기 '입원' 컬럼 내부 레인 값.
    단계(lifecycle_stage)가 아니라 발병일+진단군에서 자동 판정한다(이중 입력 없음).
    Returns: dict(care_phase, phase_dday, phase_label)
      care_phase = '회복기' | '비회복기' | '단일구간'(비중추신경계 — 전환 개념 없음)
                   | '미판정'(발병일 없음 / 입원목적이 일반재활·요양·기타)
      phase_dday = 회복기는 S005 수가 만료까지, 그 외는 입원 만료까지 남은 일수
    """
    rec = _recovery_status(consultation) or {}
    label = (rec.get("label") or "").strip()
    ax = _admission_expiry(consultation) or {}
    if not label:
        phase = "미판정"
    elif not is_cns_diseases(consultation.get("diseases")):
        # 비중추신경계는 S005 하나뿐 — 회복기→비회복기 '전환' 자체가 없다.
        # 재원 기간이 진단군별로 고정이라 별도 레인(단일구간)으로 묶는다.
        phase = "단일구간"
    elif label == "비회복기":
        phase = "비회복기"
    elif label == "회복기":
        phase = "회복기"
    else:
        # 일반재활·요양 — 중추신경계라도 회복기/비회복기 '구간' 밖이다.
        # 회복기로 뭉뚱그리면 재원 카드에 엉뚱한 구간이 찍히고, recovery_due
        # ('회복기 전환 임박')가 대상 아닌 환자까지 잡아 알림이 부푼다.
        phase = "미판정"
    # 외진 기간을 빼지 않은 값 그대로 쓴다 (_admission_expiry 주석 참고)
    dday = ax.get("billing_left") if phase == "회복기" else ax.get("total_left")
    end_date = ax.get("billing_date") if phase == "회복기" else ax.get("total_date")
    return {"care_phase": phase, "phase_dday": dday, "phase_end_date": end_date,
            "phase_mandatory": bool(ax.get("mandatory"))}


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
    # 4자리(1201·1305 등)=앞 2자리 병동(10~13), 3자리(502)=앞 1자리 병동(2~9)
    if len(digits) >= 4:
        return f"{int(digits[:2])}병동"
    if len(digits) == 3:
        return f"{int(digits[0])}병동"
    if digits:
        return f"{int(digits)}병동"
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


# 액션큐 카드 종류 → KPI 히어로에 표시할 묶음. 인바운드는 채널명이 그대로 kind로
# 들어오므로(카카오채널/홈페이지/기타) 아래 표에 없는 kind는 전부 '문의'로 본다.
_ACTION_GROUP_MAP = {
    "재연락": "재연락",
    "입원준비": "입원준비",
    "담당자": "담당자",
    "전환체크": "전환체크",
    "퇴원예정": "퇴원예정",
    "입원보류": "보류",
    "상담보류": "보류",
    "보류": "보류",
    "입원예정": "입원예정일",
}
# 순서·묶음은 아래 KPI 카드 줄과 맞춘다 — 오늘(파랑) → 기한(주황) → 대기(회색).
_ACTION_GROUP_ORDER = ("입원준비", "담당자", "전환체크", "퇴원예정",
                       "문의", "재연락", "보류", "입원예정일")
_ACTION_GROUP_BAND = {
    "입원준비": "today", "담당자": "today",
    "전환체크": "due", "퇴원예정": "due",
}


def _dashboard_action_group(kind):
    return _ACTION_GROUP_MAP.get(kind, "문의")


def _dashboard_action_queue(data, open_comms, callbacks, recovery_due, discharge_due,
                            planned_missing_date=None):
    """대시보드 액션큐 — 처리 필요 카드 목록.
    age_days 기준으로 ① '오늘 처리 필요'(0~7일)와 ② '오래 방치'(8일+)로 분리한다.
    "오늘 처리 필요" 섹션 라벨과 묵은 카드(20일+ 등)의 모순을 해소.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    items = []
    planned_missing_date = planned_missing_date or []

    STALE_THRESHOLD = 8  # 일. 이 이상 방치된 건은 '오래 방치' 섹션으로 분리.

    def add(kind, tone, title, detail="", meta="", href=None, sort=50, age_days=0):
        items.append({
            "kind": kind,
            "tone": tone,
            "title": title,
            "detail": detail,
            "meta": meta,
            "href": href,
            "sort": sort,
            "age_days": age_days or 0,
            "is_stale": (age_days or 0) >= STALE_THRESHOLD,
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
            "/#inbound",
            0 if tone == "danger" else 15 if tone == "warn" else 45,
            age_days=int(hours // 24),
        )

    for r in callbacks:
        days = _dashboard_days_since(r.get("consult_date")) or 0
        tone = "danger" if days >= 2 else "warn"
        meta = f"{days}일 대기" if days > 0 else "오늘 재연락"
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
            f"/consult/{r.get('id')}" if r.get("id") else "/#inbound",
            8 if tone == "danger" else 25,
            age_days=days,
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
                age_days=0,
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
                age_days=0,
            )

    for d in recovery_due:
        left = d["watch"].get("billing_left")
        # left가 음수면 만료 후 경과일수 → 방치 판단 기준
        age = -left if (left is not None and left < 0) else 0
        add(
            "전환체크",
            "danger" if left is not None and left <= 0 else "warn",
            d["con"].get("patient_name") or "환자 미지정",
            "회복기 수가 만료 임박",
            f"{abs(left)}일 초과" if left is not None and left < 0 else f"D-{left}",
            f"/consult/{d['con'].get('id')}" if d["con"].get("id") else None,
            6 if left is not None and left <= 0 else 22,
            age_days=age,
        )

    for d in discharge_due:
        left = d["watch"].get("days_left")
        age = -left if (left is not None and left < 0) else 0
        add(
            "퇴원예정",
            "danger" if left is not None and left <= 0 else "warn",
            d["con"].get("patient_name") or "환자 미지정",
            "퇴원 예정일 확인 필요",
            f"{abs(left)}일 초과" if left is not None and left < 0 else f"D-{left}",
            f"/consult/{d['con'].get('id')}" if d["con"].get("id") else None,
            10 if left is not None and left <= 0 else 30,
            age_days=age,
        )

    for h in data.get("holds", []):
        days = _dashboard_days_since(h.get("updated_at") or h.get("consult_date")) or 0
        add(
            h.get("hold_kind") or "보류",
            "danger" if h.get("hold_kind") == "입원보류" else "warn",
            h.get("patient_name") or "환자 미지정",
            h.get("hold_reason_text") or "보류 사유 확인 필요",
            f"{days}일 경과" if days > 0 else "보류",
            f"/consult/{h.get('id')}" if h.get("id") else None,
            35,
            age_days=days,
        )

    # 입원예정 상태인데 planned_admission_date가 비어 있는 상담 — 날짜 지정 필요.
    # 오래 방치될수록 우선순위 상승 (consult_date 기준 경과일).
    for r in planned_missing_date:
        days = _dashboard_days_since(r.get("consult_date")) or 0
        meta = f"{days}일 경과" if days > 0 else "오늘 등록"
        add(
            "입원예정",
            "danger" if days >= 3 else "warn",
            r.get("patient_name") or "환자 미지정",
            "입원예정일 미지정 — 상단 '입원예정 (월/일)' 칸을 채워주세요",
            meta,
            f"/consult/{r.get('id')}/edit" if r.get("id") else None,
            12 if days >= 3 else 32,
            age_days=days,
        )

    tone_rank = {"danger": 0, "warn": 1, "info": 2}
    items.sort(key=lambda x: (tone_rank.get(x["tone"], 9), x["sort"], x["title"]))

    today_items = [x for x in items if not x["is_stale"]]
    stale_items = sorted(
        [x for x in items if x["is_stale"]],
        key=lambda x: (-x["age_days"], tone_rank.get(x["tone"], 9), x["title"]),
    )
    groups = []
    for label in _ACTION_GROUP_ORDER:
        rows = [x for x in today_items if _dashboard_action_group(x["kind"]) == label]
        groups.append({
            "label": label,
            "band": _ACTION_GROUP_BAND.get(label, "wait"),
            "count": len(rows),
            "danger": sum(1 for x in rows if x["tone"] == "danger"),
            "stale": sum(1 for x in stale_items if _dashboard_action_group(x["kind"]) == label),
        })

    return {
        "items": today_items[:14],
        "stale_items": stale_items[:30],
        "groups": groups,
        "today_total": len(today_items),
        "stale_total": len(stale_items),
        "total": len(items),
        "danger": sum(1 for x in today_items if x["tone"] == "danger"),
        "warn": sum(1 for x in today_items if x["tone"] == "warn"),
        "stale_danger": sum(1 for x in stale_items if x["tone"] == "danger"),
    }


@app.template_filter("agefrom")
def _agefrom(birth_year):
    if not birth_year:
        return ""
    return datetime.now().year - int(birth_year)


# ── 생애주기 단계 자동 동기화 (제안 1 — 상담 결과 → 단계) ──
# 입원예정·입원보류→입원대기, 입원완료→입원, 퇴원완료→퇴원. 전진만 (수동 지정한
# 더 앞선 단계는 되돌리지 않음). 퇴원은 종료 단계라 항상 적용.
_STATUS_TO_STAGE = {
    "입원예정": "입원대기",
    "입원보류": "입원대기",
    "입원완료": "입원",
    "퇴원완료": "퇴원",
}


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
    """임상 이벤트 기반 단계 전환 — 복귀·퇴원 등.
    의료 사건이 발생하면 단계가 뒤로 갈 수도 있으므로(예: 퇴원 취소 → 입원)
    `_sync_lifecycle_stage`의 '앞으로만' 룰을 우회한다. 단, 이미 '퇴원' 상태인
    환자는 더 이상 변동하지 않는다(완료 케이스 보호).
    폐지된 단계값(응급치료·회복기·비회복기)이 들어오면 '입원'으로 접어 받는다."""
    target_stage = LEGACY_STAGE_MAP.get(target_stage, target_stage)
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
        destination = (url_for("notice_required", next=next_url)
                       if models.first_unread_required_announcement(
                           user["id"], user.get("role", "staff")) else next_url)
        response = redirect(destination)
        if request.form.get("auto_login") == "1":
            token = _remember_serializer.dumps({
                "uid": user["id"], "fp": _remember_fingerprint(user),
            })
            response.set_cookie(
                _REMEMBER_COOKIE, token, max_age=_REMEMBER_DAYS * 86400,
                httponly=True, secure=request.is_secure, samesite="Lax", path="/",
            )
        else:
            response.delete_cookie(_REMEMBER_COOKIE, path="/", samesite="Lax")
        return response
    if current_user():
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout", methods=["POST", "GET"])
def logout_view():
    logout_user()
    flash("로그아웃되었습니다.", "info")
    response = redirect(url_for("login_view"))
    response.delete_cookie(_REMEMBER_COOKIE, path="/", samesite="Lax")
    return response


@app.route("/password-reset/request", methods=["POST"])
def password_reset_request():
    """로그인 전 초기화 요청. 계정 존재 여부는 응답에서 구분하지 않는다."""
    username = (request.form.get("username") or "").strip()
    if username:
        created = models.create_password_reset_request(username, request.remote_addr)
        if created:
            models.log_audit(
                username=username, action="request_password_reset",
                target_type="user", detail="비밀번호 초기화 요청", ip=request.remote_addr,
            )
    flash("등록된 계정인 경우 관리자에게 비밀번호 초기화 요청을 전달했습니다.", "info")
    return redirect(url_for("login_view"))


# ───────────────────── 공지사항 ─────────────────────

@app.route("/notices")
@login_required
def notices_view():
    user = current_user()
    is_admin = user.get("role") == "admin"
    notices = models.list_announcements(
        user["id"], user.get("role", "staff"), include_inactive=is_admin)
    return render_template("notices.html", notices=notices, is_admin=is_admin)


@app.route("/notices/required")
@login_required
def notice_required():
    user = current_user()
    notice = models.first_unread_required_announcement(
        user["id"], user.get("role", "staff"))
    next_url = request.args.get("next") or url_for("dashboard")
    if not _is_safe_next_url(next_url):
        next_url = url_for("dashboard")
    if not notice:
        return redirect(next_url)
    return render_template("notice_required.html", notice=notice, next_url=next_url)


@app.route("/notices/<int:notice_id>/ack", methods=["POST"])
@login_required
def notice_acknowledge(notice_id):
    user = current_user()
    if not models.acknowledge_announcement(
            notice_id, user["id"], user.get("role", "staff")):
        abort(404)
    models.log_audit(
        user_id=user["id"], username=user["username"], action="ack_notice",
        target_type="announcement", target_id=notice_id, ip=request.remote_addr,
    )
    next_url = request.form.get("next") or url_for("dashboard")
    if not _is_safe_next_url(next_url):
        next_url = url_for("dashboard")
    if models.first_unread_required_announcement(user["id"], user.get("role", "staff")):
        return redirect(url_for("notice_required", next=next_url))
    flash("공지사항을 확인했습니다.", "success")
    return redirect(next_url)


@app.route("/notices/create", methods=["POST"])
@login_required
def notice_create():
    user = current_user()
    if user.get("role") != "admin":
        abort(403)
    title = (request.form.get("title") or "").strip()
    body = (request.form.get("body") or "").strip()
    if not title or not body:
        flash("공지 제목과 내용을 모두 입력해주세요.", "error")
        return redirect(url_for("notices_view"))
    target_role = request.form.get("target_role")
    if target_role not in ("staff", "viewer", "all"):
        target_role = "staff"
    notice_id = models.create_announcement(
        title=title, body=body, target_role=target_role,
        requires_ack=request.form.get("requires_ack") == "1",
        expires_at=(request.form.get("expires_at") or "").strip() or None,
        created_by=user["id"], created_by_name=user.get("display_name") or user["username"],
    )
    models.log_audit(
        user_id=user["id"], username=user["username"], action="create_notice",
        target_type="announcement", target_id=notice_id, detail=title,
        ip=request.remote_addr,
    )
    flash("공지사항을 게시했습니다.", "success")
    return redirect(url_for("notices_view"))


@app.route("/notices/<int:notice_id>/active", methods=["POST"])
@login_required
def notice_set_active(notice_id):
    user = current_user()
    if user.get("role") != "admin":
        abort(403)
    active = request.form.get("active") == "1"
    if not models.set_announcement_active(notice_id, active):
        abort(404)
    models.log_audit(
        user_id=user["id"], username=user["username"], action="update_notice",
        target_type="announcement", target_id=notice_id,
        detail="게시" if active else "게시 종료", ip=request.remote_addr,
    )
    flash("공지 상태를 변경했습니다.", "success")
    return redirect(url_for("notices_view"))


# ───────────────────── 사용자 관리 (어드민 전용) ─────────────────────

_VALID_ROLES = {"admin", "staff", "viewer"}
_MIN_PW_LEN = 4


AUDIT_PAGE_SIZE = 100  # 이력 관리 페이지당 행 수
AUDIT_PAGE_SIZE_OPTIONS = (50, 100, 200, 500)
AUDIT_EXPORT_LIMIT = 20000  # CSV 한 번에 내보내는 최대 행 수


def _audit_filters_from_request():
    """이력 관리 화면의 필터를 요청에서 읽어 models 인자 형태로 정리."""
    category = (request.args.get("category") or "").strip()
    action = (request.args.get("action") or "").strip()
    known = [a for _, acts in AUDIT_CATEGORIES.values() for a in acts]
    if action:
        actions, exclude = [action], None
    elif category == AUDIT_CATEGORY_OTHER:
        actions, exclude = None, known          # 어느 분류에도 없는 action
    elif category in AUDIT_CATEGORIES:
        actions, exclude = AUDIT_CATEGORIES[category][1], None
    else:
        category, actions, exclude = "", None, None
    return {
        "date_from": (request.args.get("from") or "").strip() or None,
        "date_to": (request.args.get("to") or "").strip() or None,
        "username": (request.args.get("user") or "").strip() or None,
        "target_type": (request.args.get("target") or "").strip() or None,
        "q": (request.args.get("q") or "").strip() or None,
        "actions": actions,
        "exclude_actions": exclude,
    }, category, action


@app.route("/admin/audit")
@admin_required
def admin_audit():
    """이력 관리 — 누가·언제·무엇을 조회/입력/수정/삭제했는지 (audit_log)."""
    filters, category, action = _audit_filters_from_request()
    try:
        page = max(1, int(request.args.get("page") or 1))
    except (ValueError, TypeError):
        page = 1
    try:
        requested = int(request.args.get("page_size") or AUDIT_PAGE_SIZE)
    except (ValueError, TypeError):
        requested = AUDIT_PAGE_SIZE
    page_size = requested if requested in AUDIT_PAGE_SIZE_OPTIONS else AUDIT_PAGE_SIZE

    total = models.count_audit_logs(**filters)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    offset = (page - 1) * page_size
    rows = models.list_audit_logs(**filters, limit=page_size, offset=offset)
    # CSV 내보내기 링크 — 현재 검색 조건만 유지 (페이지·보기개수는 의미 없음)
    export_args = {k: v for k, v in request.args.items()
                   if k not in ("page", "page_size") and v}
    export_qs = ("?" + urlencode(export_args)) if export_args else ""
    return render_template(
        "audit.html", rows=rows, total=total, page=page, total_pages=total_pages,
        export_qs=export_qs,
        page_size=page_size, page_size_options=AUDIT_PAGE_SIZE_OPTIONS,
        page_start=offset,
        category=category, action=action,
        action_counts=models.audit_action_counts(**filters),
        users=models.audit_usernames(),
        target_types=models.audit_target_types(),
        span=models.audit_log_span(),
        AUDIT_ACTION_LABELS=AUDIT_ACTION_LABELS,
        AUDIT_CATEGORIES=AUDIT_CATEGORIES,
        AUDIT_CATEGORY_OTHER=AUDIT_CATEGORY_OTHER,
        AUDIT_CRITICAL_ACTIONS=AUDIT_CRITICAL_ACTIONS,
        AUDIT_RETENTION_DAYS=AUDIT_RETENTION_DAYS,
    )


@app.route("/admin/audit/export")
@admin_required
def admin_audit_export():
    """현재 필터 조건의 이력을 CSV로 — 개인정보 열람기록 제출·내부 감사용."""
    filters, _category, _action = _audit_filters_from_request()
    rows = models.list_audit_logs(**filters, limit=AUDIT_EXPORT_LIMIT, offset=0)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["일시", "사용자ID", "아이디", "이름", "행위", "행위(코드)",
                "대상종류", "대상ID", "상세", "IP"])
    for r in rows:
        w.writerow([
            r.get("created_at") or "",
            r.get("user_id") if r.get("user_id") is not None else "",
            r.get("username") or "",
            r.get("display_name") or "",
            AUDIT_ACTION_LABELS.get(r.get("action"), r.get("action") or ""),
            r.get("action") or "",
            r.get("target_type") or "",
            r.get("target_id") if r.get("target_id") is not None else "",
            r.get("detail") or "",
            r.get("ip") or "",
        ])
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="export_csv", target_type="audit_log",
        detail=f"이력 {len(rows)}건", ip=request.remote_addr,
    )
    data = buf.getvalue().encode("utf-8-sig")  # Excel 한글 깨짐 방지
    return send_file(
        io.BytesIO(data), mimetype="text/csv", as_attachment=True,
        download_name=f"audit_log_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
    )


def _parse_perms_form(form):
    """폼의 perm_<menu> 값 → {menu: level} (메뉴별 지원 최대 레벨로 클램프)."""
    perms = {}
    for k in MENU_KEYS:
        try:
            lvl = int(form.get(f"perm_{k}", 0))
        except (TypeError, ValueError):
            lvl = 0
        perms[k] = max(0, min(MENU_MAX_LEVEL[k], lvl))
    return perms


def _count_user_managers(exclude_id=None):
    """사용자 관리(users) 권한이 '수정' 이상인 활성 계정 수. 마지막 관리자 보호용."""
    n = 0
    for u in models.list_users():
        if exclude_id is not None and u["id"] == exclude_id:
            continue
        if u["active"] and u["perms"].get("users", 0) >= PERM_EDIT:
            n += 1
    return n


@app.route("/admin/users")
@admin_required
def admin_users():
    users = models.list_users()
    return render_template(
        "users.html", users=users, menus=MENUS, perm_labels=PERM_LEVEL_LABELS,
        role_presets=ROLE_PRESETS, menu_max=MENU_MAX_LEVEL,
        password_reset_requests=models.list_pending_password_reset_requests(),
    )


@app.route("/admin/users/create", methods=["POST"])
@admin_required
def admin_users_create():
    username = (request.form.get("username") or "").strip()
    display_name = (request.form.get("display_name") or "").strip()
    role = (request.form.get("role") or "staff").strip()
    password = request.form.get("password") or ""
    if not username or role not in _VALID_ROLES:
        flash("아이디와 역할을 올바르게 입력하세요.", "error")
        return redirect(url_for("admin_users"))
    if len(password) < _MIN_PW_LEN:
        flash(f"비밀번호는 최소 {_MIN_PW_LEN}자 이상이어야 합니다.", "error")
        return redirect(url_for("admin_users"))
    # 권한: 폼에 perm_* 가 오면 그 값, 없으면 역할 프리셋
    perms = _parse_perms_form(request.form) if any(
        k.startswith("perm_") for k in request.form) else role_preset(role)
    try:
        models.create_user(username, display_name, role, password, permissions=perms)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("admin_users"))
    models.log_audit(user_id=g.user["id"], username=g.user["username"],
                     action="create_user", target_type="user",
                     detail=f"{username} ({role})", ip=request.remote_addr)
    flash(f"'{display_name or username}' 계정을 추가했습니다.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:uid>/update", methods=["POST"])
@admin_required
def admin_users_update(uid):
    target = models.get_user_by_id(uid)
    if not target:
        abort(404)
    display_name = (request.form.get("display_name") or "").strip() or target["username"]
    role = (request.form.get("role") or target["role"]).strip()
    if role not in _VALID_ROLES:
        flash("올바른 역할이 아닙니다.", "error")
        return redirect(url_for("admin_users"))
    perms = _parse_perms_form(request.form)
    # 사용자 관리 권한을 잃게 되는 변경이면, 다른 관리자가 최소 1명 남아야 함
    if perms.get("users", 0) < PERM_EDIT and target["perms"].get("users", 0) >= PERM_EDIT \
            and _count_user_managers(exclude_id=uid) < 1:
        flash("사용자 관리 권한을 가진 계정이 최소 1개는 있어야 합니다.", "error")
        return redirect(url_for("admin_users"))
    models.update_user(uid, display_name, role, permissions=perms)
    models.log_audit(user_id=g.user["id"], username=g.user["username"],
                     action="update_user", target_type="user", target_id=uid,
                     detail=f"{target['username']} → {role} perms={perms}",
                     ip=request.remote_addr)
    flash("계정 정보·권한을 저장했습니다.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:uid>/password", methods=["POST"])
@admin_required
def admin_users_password(uid):
    target = models.get_user_by_id(uid)
    if not target:
        abort(404)
    password = request.form.get("password") or ""
    if len(password) < _MIN_PW_LEN:
        flash(f"비밀번호는 최소 {_MIN_PW_LEN}자 이상이어야 합니다.", "error")
        return redirect(url_for("admin_users"))
    models.set_user_password(uid, password)
    models.resolve_password_reset_requests(uid, g.user["id"])
    models.log_audit(user_id=g.user["id"], username=g.user["username"],
                     action="reset_password", target_type="user", target_id=uid,
                     detail=target["username"], ip=request.remote_addr)
    flash(f"'{target['display_name'] or target['username']}' 비밀번호를 변경했습니다.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/password-reset/<int:request_id>/resolve", methods=["POST"])
@admin_required
def admin_password_reset_resolve(request_id):
    if not models.resolve_password_reset_request(request_id, g.user["id"]):
        abort(404)
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="resolve_password_reset", target_type="password_reset_request",
        target_id=request_id, ip=request.remote_addr,
    )
    flash("비밀번호 초기화 요청을 처리 완료로 표시했습니다.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:uid>/active", methods=["POST"])
@admin_required
def admin_users_active(uid):
    target = models.get_user_by_id(uid)
    if not target:
        abort(404)
    activate = (request.form.get("active") == "1")
    if not activate and uid == g.user["id"]:
        flash("본인 계정은 비활성화할 수 없습니다.", "error")
        return redirect(url_for("admin_users"))
    if not activate and target["perms"].get("users", 0) >= PERM_EDIT \
            and _count_user_managers(exclude_id=uid) < 1:
        flash("사용자 관리 권한을 가진 계정이 최소 1개는 있어야 합니다.", "error")
        return redirect(url_for("admin_users"))
    models.set_user_active(uid, activate)
    models.log_audit(user_id=g.user["id"], username=g.user["username"],
                     action="toggle_user_active", target_type="user", target_id=uid,
                     detail=f"{target['username']} active={activate}", ip=request.remote_addr)
    flash(("활성화" if activate else "비활성화") + "했습니다.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:uid>/delete", methods=["POST"])
@admin_required
def admin_users_delete(uid):
    target = models.get_user_by_id(uid)
    if not target:
        abort(404)
    if uid == g.user["id"]:
        flash("본인 계정은 삭제할 수 없습니다.", "error")
        return redirect(url_for("admin_users"))
    if target["perms"].get("users", 0) >= PERM_EDIT \
            and _count_user_managers(exclude_id=uid) < 1:
        flash("사용자 관리 권한을 가진 계정이 최소 1개는 있어야 합니다.", "error")
        return redirect(url_for("admin_users"))
    models.delete_user(uid)
    models.log_audit(user_id=g.user["id"], username=g.user["username"],
                     action="delete_user", target_type="user", target_id=uid,
                     detail=target["username"], ip=request.remote_addr)
    flash(f"'{target['display_name'] or target['username']}' 계정을 삭제했습니다.", "success")
    return redirect(url_for("admin_users"))


# ───────────────────── 메인 ─────────────────────

def _dashboard_calendar_context(uid, year, month, counselor=None):
    """상담·입퇴원 일정과 개인/공유 ToDo를 합친 월간 달력.
    counselor 지정 시 상담·입퇴원 일정은 그 상담사 담당 건만 (내 담당만 보기)."""
    first = date(year, month, 1)
    start = first - timedelta(days=(first.weekday() + 1) % 7)
    days = [start + timedelta(days=i) for i in range(42)]
    last = days[-1]
    buckets = {d.isoformat(): [] for d in days}

    def add(day, kind, title, meta="", href="#", time="", done=False):
        key = (day or "")[:10]
        if key not in buckets:
            return
        buckets[key].append({"kind": kind, "title": title, "meta": meta,
                             "href": href, "time": (time or "")[:5], "done": done})

    for row in models.dashboard_calendar_rows(start.isoformat(), last.isoformat(), counselor):
        name = row.get("patient_name") or "환자 미지정"
        href = f"/consult/{row['id']}"
        add(row.get("consult_date"), "consult", name,
            "상담" + (f" · {row['counselor']}" if row.get("counselor") else ""),
            href, row.get("consult_time"))
        actual = row.get("actual_admission_date") or row.get("admission_date")
        planned = row.get("planned_admission_date")
        if actual:
            add(actual, "admitted", name, "입원", href, row.get("planned_admission_time"))
        if planned and (not actual or planned != actual):
            add(planned, "admission", name, "입원예정", href, row.get("planned_admission_time"))
        discharged = row.get("discharge_date")
        discharge_due = row.get("discharge_due_date")
        if discharged:
            add(discharged, "discharged", name, "퇴원", href)
        elif discharge_due:
            add(discharge_due, "discharge", name, "퇴원예정", href)

    todos = models.list_todos_range(uid, start.isoformat(), last.isoformat())
    for todo in todos:
        begin = date.fromisoformat(todo["due_date"])
        end = date.fromisoformat(todo["end_date"]) if todo.get("end_date") else begin
        cursor = max(begin, start)
        while cursor <= min(end, last):
            shared = not bool(todo.get("is_owner"))
            meta = ((todo.get("owner_name") or "다른 상담사") + " 공유"
                    if shared else "ToDo")
            add(cursor.isoformat(), "shared" if shared else "todo", todo["title"],
                meta, f"/todos?view=list&date={cursor.isoformat()}", todo.get("start_time"),
                done=bool(todo.get("done")))
            cursor += timedelta(days=1)

    order = {"admission": 0, "admitted": 0, "discharge": 1, "discharged": 1,
             "consult": 2, "shared": 3, "todo": 4}
    kr_holidays = _kr_holidays(tuple(sorted({start.year, last.year})))
    weeks = []
    for w in range(6):
        week = []
        for d in days[w * 7:(w + 1) * 7]:
            events = sorted(buckets[d.isoformat()],
                            key=lambda x: (order.get(x["kind"], 9), x["time"], x["title"]))
            holiday = kr_holidays.get(d)
            weekday = (d.weekday() + 1) % 7
            week.append({"date": d.isoformat(), "day": d.day,
                         "in_month": d.month == month, "is_today": d == date.today(),
                         "weekday": weekday, "lunar": _lunar_label(d),
                         "holiday": holiday,
                         "is_holiday": bool(holiday) or weekday == 0,
                         "events": events})
        weeks.append(week)
    prev_m = (first - timedelta(days=1)).replace(day=1)
    next_m = (first + timedelta(days=31)).replace(day=1)
    return {"dashboard_calendar_weeks": weeks, "cal_year": year, "cal_month": month,
            "cal_label": f"{year}.{month:02d}",
            "cal_prev_year": prev_m.year, "cal_prev_month": prev_m.month,
            "cal_next_year": next_m.year, "cal_next_month": next_m.month,
            "cal_mine": bool(counselor)}

@app.route("/")
@login_required
def dashboard():
    today_d = date.today()
    legacy_admission_date = request.args.get("admission_date")
    admission_from = _valid_date(
        request.args.get("admission_from") or legacy_admission_date, today_d.isoformat())
    admission_to = _valid_date(
        request.args.get("admission_to") or legacy_admission_date, admission_from)
    if admission_from > admission_to:
        admission_from, admission_to = admission_to, admission_from
    admission_weekdays = "월화수목금토일"
    def admission_date_label(value):
        parsed = date.fromisoformat(value)
        return parsed.strftime("%Y.%m.%d") + f"({admission_weekdays[parsed.weekday()]})"
    data = models.dashboard_summary(admission_from, admission_to)
    data.update({
        "admission_lookup_from": admission_from,
        "admission_lookup_to": admission_to,
        "admission_lookup_label": (admission_date_label(admission_from)
                                   if admission_from == admission_to else
                                   f"{admission_date_label(admission_from)} ~ {admission_date_label(admission_to)}"),
        "admission_quick_dates": [
            {"label": "오늘", "from": today_d.isoformat(), "to": today_d.isoformat()},
            {"label": "어제", "from": (today_d - timedelta(days=1)).isoformat(), "to": (today_d - timedelta(days=1)).isoformat()},
            {"label": "그저께", "from": (today_d - timedelta(days=2)).isoformat(), "to": (today_d - timedelta(days=2)).isoformat()},
            {"label": "최근 7일", "from": (today_d - timedelta(days=6)).isoformat(), "to": today_d.isoformat()},
            {"label": "최근 30일", "from": (today_d - timedelta(days=29)).isoformat(), "to": today_d.isoformat()},
        ],
    })
    open_comms = models.inbox_open_communications()
    callbacks = models.inbox_callbacks()

    # 입원예정 상태인데 planned_admission_date가 비어 있는 상담 — 액션큐에 표시.
    planned_consults = models.list_consultations(admission_status="입원예정", limit=10000)
    planned_missing_date = [
        c for c in planned_consults
        if not (c.get("planned_admission_date") or "").strip()
    ]

    # 입원완료 환자 중 회복기→비회복기 전환 D-30, 퇴원예정 D-30.
    admitted = models.list_consultations(admission_status="입원완료", limit=10000)
    recovery_transition_due = []
    discharge_due = []
    for con in admitted:
        disease_labels = _dashboard_disease_labels(con)
        con["disease_summary"] = "" if disease_labels == ["병명 미지정"] else ", ".join(disease_labels[:3])
        con["ward"] = _dashboard_ward_label(con.get("room_number"))
        ax = _admission_expiry(con)
        if ax and ax.get("billing_left") is not None and ax["billing_left"] <= 30:
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
        planned_missing_date=planned_missing_date,
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
    # 비중추신경계 — S005 재원 기간 안에 반드시 퇴원해야 하는 건수
    data["summary"]["discharge_mandatory"] = sum(
        1 for d in discharge_due if d["watch"].get("mandatory")
    )
    # KPI 카운터 — "처리 필요"는 오늘 큐 기준 (8일+ stale은 별도 표시).
    data["summary"]["action_total"] = action_queue["today_total"]
    data["summary"]["action_danger"] = action_queue["danger"]
    data["summary"]["action_stale"] = action_queue["stale_total"]
    try:
        cal_year = int(request.args.get("cal_year") or date.today().year)
        cal_month = int(request.args.get("cal_month") or date.today().month)
        if not 2000 <= cal_year <= 2100:
            raise ValueError
        date(cal_year, cal_month, 1)
    except (TypeError, ValueError):
        cal_year, cal_month = date.today().year, date.today().month
    # 통합 달력 '내 담당만/전체' — 기본 내 담당만. cal_mine=0이면 전체.
    cal_mine = request.args.get("cal_mine", "1") != "0"
    cal_counselor = g.user.get("display_name") if cal_mine else None
    data.update(_dashboard_calendar_context(g.user["id"], cal_year, cal_month, cal_counselor))
    return render_template("dashboard.html", **data)


@app.route("/healthz")
def healthz():
    return {"ok": True}


@app.route("/help")
@login_required
def help_manual():
    """8개 메뉴 사용 매뉴얼 — 신규 상담사 온보딩·일상 참고용."""
    return render_template("help.html")


# ───────────────────── 상담사 개인 할 일(To-Do) ─────────────────────
# 계정별 개인 기능 — 권한 매트릭스와 무관, 모든 로그인 사용자가 사용.

def _valid_date(s, default=None):
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d").date().isoformat()
    except (ValueError, AttributeError):
        return default


def _valid_time(s, default=None):
    """'HH:MM' 만 허용. 빈 값/형식 오류면 default(기본 None)."""
    try:
        return datetime.strptime((s or "").strip(), "%H:%M").strftime("%H:%M")
    except (ValueError, AttributeError):
        return default


def _add_months(d, n):
    """월 더하기 — 말일 넘침은 해당 월 말일로 보정 (1/31 + 1개월 = 2/28/29)."""
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _repeat_dates(start_iso, freq, until_iso, cap=200):
    """반복 시작일~종료일 사이의 발생일 목록(ISO). 반복 없음/종료일 없음이면 [start]만."""
    start = _valid_date(start_iso)
    until = _valid_date(until_iso)
    if not start or freq not in ("daily", "weekly", "monthly") or not until:
        return [start] if start else []
    s = date.fromisoformat(start)
    u = date.fromisoformat(until)
    if u < s:
        return [start]
    out, cur = [s], s
    while len(out) < cap:
        if freq == "daily":
            cur = cur + timedelta(days=1)
        elif freq == "weekly":
            cur = cur + timedelta(weeks=1)
        else:
            cur = _add_months(cur, 1)
        if cur > u:
            break
        out.append(cur)
    return [d.isoformat() for d in out]


def _annotate_todos(todos, today):
    """할 일에 dday_label(마감까지 D-표기)을 붙인다 (dday 사용 항목만)."""
    for t in todos:
        t["share_user_ids"] = (models.todo_share_user_ids(t["id"])
                               if t.get("is_owner", True) else [])
        t["dday_label"] = ""
        if t.get("dday"):
            anchor = t.get("end_date") or t.get("due_date")
            try:
                days = (date.fromisoformat(anchor) - today).days
                t["dday_label"] = ("D-DAY" if days == 0
                                   else (f"D-{days}" if days > 0 else f"D+{-days}"))
            except (ValueError, TypeError):
                t["dday_label"] = ""
    return todos


def _todo_calendar_context(uid, year, month, today):
    """월 달력 그리드(6주 x 7일) + 각 날짜의 할 일 버킷."""
    first = date(year, month, 1)
    start = first - timedelta(days=(first.weekday() + 1) % 7)   # 그 주 일요일부터
    grid = [start + timedelta(days=i) for i in range(42)]
    todos = _annotate_todos(
        models.list_todos_range(uid, grid[0].isoformat(), grid[-1].isoformat()), today)
    buckets = {}
    for t in todos:
        s = date.fromisoformat(t["due_date"])
        e = date.fromisoformat(t["end_date"]) if t.get("end_date") else s
        d, last = max(s, grid[0]), min(e, grid[-1])
        while d <= last:
            buckets.setdefault(d.isoformat(), []).append(t)
            d += timedelta(days=1)
    kr_hol = _kr_holidays(tuple(sorted({grid[0].year, grid[-1].year})))
    weeks = []
    for w in range(6):
        week = []
        for dc in grid[w * 7:(w + 1) * 7]:
            wd = (dc.weekday() + 1) % 7            # 0=일 .. 6=토
            holiday = kr_hol.get(dc)
            week.append({
                "date": dc.isoformat(), "day": dc.day,
                "in_month": dc.month == month, "is_today": dc == today,
                "weekday": wd,
                "lunar": _lunar_label(dc),
                "holiday": holiday,                 # 공휴일명 또는 None
                "is_holiday": bool(holiday) or wd == 0,   # 일요일·공휴일=빨강
                "todos": buckets.get(dc.isoformat(), []),
            })
        weeks.append(week)
    prev_m = (first - timedelta(days=1)).replace(day=1)
    next_m = (first + timedelta(days=31)).replace(day=1)
    return {
        "weeks": weeks, "year": year, "month": month,
        "month_label": f"{year}.{month:02d}",
        "prev_year": prev_m.year, "prev_month": prev_m.month,
        "next_year": next_m.year, "next_month": next_m.month,
    }


@app.route("/todos")
@login_required
def todos_view():
    """개인 할 일 — 달력(월) 뷰가 기본, 목록(일자별) 뷰 선택 가능."""
    uid = g.user["id"]
    today = date.today()
    embed = request.args.get("embed") == "1"   # 팝업(iframe)용 — 헤더/네비 없이 본문만
    view = "list" if request.args.get("view") == "list" else "calendar"
    selected_date = _valid_date(request.args.get("date"), today.isoformat())
    share_users = [u for u in models.list_users()
                   if u.get("active") and u["id"] != uid
                   and u.get("role") in ("admin", "staff")]
    ctx = {"view": view, "embed": embed, "today": today.isoformat(),
           "auto_new": request.args.get("new") == "1",
           "selected_date": selected_date,
           "share_users": share_users}
    if view == "list":
        day = _valid_date(request.args.get("date"), today.isoformat())
        ctx.update(
            day=day, items=_annotate_todos(models.list_todos(uid, day), today),
            overdue=_annotate_todos(
                models.list_overdue_todos(uid, today.isoformat())
                if day == today.isoformat() else [], today),
            prev_day=(date.fromisoformat(day) - timedelta(days=1)).isoformat(),
            next_day=(date.fromisoformat(day) + timedelta(days=1)).isoformat(),
        )
    else:
        selected_day = date.fromisoformat(selected_date)
        try:
            year = int(request.args.get("year") or selected_day.year)
            month = int(request.args.get("month") or selected_day.month)
            date(year, month, 1)
        except (TypeError, ValueError):
            year, month = today.year, today.month
        ctx.update(_todo_calendar_context(uid, year, month, today))
    rendered = render_template("todos_embed.html" if embed else "todos.html", **ctx)
    models.mark_todo_shares_seen(uid)
    return rendered


@app.route("/api/todos", methods=["POST"])
@login_required
def api_todo_create():
    data = request.get_json(silent=True) or request.form
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "할 일 내용을 입력하세요."}), 400
    day = _valid_date(data.get("due_date"), date.today().isoformat())
    end = _valid_date(data.get("end_date"))
    if end and end < day:          # 종료일이 시작일보다 앞서면 무시
        end = None
    # 환자 연결(선택) — 상담/환자 화면에서 만든 경우. patient_name은 표시용 스냅샷.
    try:
        pid = int(data.get("patient_id")) if data.get("patient_id") else None
    except (TypeError, ValueError):
        pid = None
    pname = (data.get("patient_name") or "").strip() or None
    if pid and not pname:
        pat = models.get_patient(pid)
        pname = pat.get("name") if pat else None
    uid = g.user["id"]
    shares = data.get("share_user_ids") or []
    st = _valid_time(data.get("start_time"))
    et = _valid_time(data.get("end_time"))
    note = (data.get("note") or "").strip()
    dday = str(data.get("dday", "")) in ("1", "true", "True", "on")

    # 반복 일정 — repeat(daily/weekly/monthly) + repeat_until 지정 시 각 날짜로 실제 생성.
    repeat = (data.get("repeat") or "none").strip()
    repeat_until = _valid_date(data.get("repeat_until"))
    dates = _repeat_dates(day, repeat, repeat_until)
    if len(dates) > 1:
        grp = secrets.token_hex(8)
        delta_end = (date.fromisoformat(end) - date.fromisoformat(day)).days if end else None
        first_id = None
        for i, d0 in enumerate(dates):
            e0 = (date.fromisoformat(d0) + timedelta(days=delta_end)).isoformat() if delta_end is not None else None
            tid = models.create_todo(
                uid, title, d0, end_date=e0, start_time=st, end_time=et,
                note=note, dday=dday, patient_id=pid, patient_name=pname, repeat_group=grp)
            if shares:
                models.sync_todo_shares(tid, uid, shares)
            if i == 0:
                first_id = tid
        return jsonify({"ok": True, "id": first_id, "count": len(dates)})

    # 단일 일정
    tid = models.create_todo(
        uid, title, day, end_date=end, start_time=st, end_time=et, note=note,
        remind_at=(data.get("remind_at") or "").strip() or None,
        progress=data.get("progress") or 0, dday=dday,
        patient_id=pid, patient_name=pname,
    )
    models.sync_todo_shares(tid, uid, shares)
    return jsonify({"ok": True, "id": tid})


@app.route("/api/todos/<int:tid>", methods=["POST"])
@login_required
def api_todo_update(tid):
    if not models.get_todo(tid, g.user["id"]):
        abort(404)
    data = request.get_json(silent=True) or request.form
    fields = {}
    if "title" in data:
        t = (data.get("title") or "").strip()
        if not t:
            return jsonify({"error": "할 일 내용을 입력하세요."}), 400
        fields["title"] = t
    if "note" in data:
        fields["note"] = (data.get("note") or "").strip()
    if "remind_at" in data:
        fields["remind_at"] = (data.get("remind_at") or "").strip()
    if "due_date" in data:
        fields["due_date"] = _valid_date(data.get("due_date"))
    if "end_date" in data:
        fields["end_date"] = _valid_date(data.get("end_date")) or ""
    if "start_time" in data:
        fields["start_time"] = _valid_time(data.get("start_time")) or ""
    if "end_time" in data:
        fields["end_time"] = _valid_time(data.get("end_time")) or ""
    if "progress" in data:
        fields["progress"] = data.get("progress") or 0
    if "dday" in data:
        fields["dday"] = str(data.get("dday", "")) in ("1", "true", "True", "on")
    models.update_todo(tid, g.user["id"], **fields)
    if "share_user_ids" in data:
        models.sync_todo_shares(tid, g.user["id"], data.get("share_user_ids") or [])
    return jsonify({"ok": True})


@app.route("/api/todos/<int:tid>/toggle", methods=["POST"])
@login_required
def api_todo_toggle(tid):
    data = request.get_json(silent=True) or request.form
    done = str(data.get("done", "1")) in ("1", "true", "True", "on")
    uid = g.user["id"]
    # 소유자뿐 아니라 공유받은 사람도 완료 처리 가능 (위임 흐름)
    t = models.get_todo_access(tid, uid)
    if not t:
        abort(404)
    models.set_todo_done_any(tid, done)
    # 공유된 할 일을 완료하면 다른 참여자(소유자·공유대상)에게 '완료 알림'
    if done:
        participants = models.todo_participants(tid)
        if len(participants) > 1:
            actor = g.user.get("display_name") or g.user["username"]
            for p in participants:
                if p != uid:
                    models.add_todo_notification(p, tid, actor, t.get("title") or "할 일")
    return jsonify({"ok": True, "done": done})


@app.route("/api/todos/<int:tid>/carry", methods=["POST"])
@login_required
def api_todo_carry(tid):
    if not models.carry_todo_to(tid, g.user["id"], date.today().isoformat()):
        abort(404)
    return jsonify({"ok": True})


@app.route("/api/todos/<int:tid>/delete", methods=["POST"])
@login_required
def api_todo_delete(tid):
    data = request.get_json(silent=True) or request.form
    # series=1 이면 같은 반복 그룹 전체 삭제
    if str(data.get("series", "")) in ("1", "true", "True", "on"):
        t = models.get_todo(tid, g.user["id"])
        if t and t.get("repeat_group"):
            n = models.delete_todo_series(g.user["id"], t["repeat_group"])
            return jsonify({"ok": True, "deleted": n})
    if not models.delete_todo(tid, g.user["id"]):
        abort(404)
    return jsonify({"ok": True})


@app.route("/api/todos/<int:tid>/move", methods=["POST"])
@login_required
def api_todo_move(tid):
    """달력 드래그 이동 — 시작일을 new_date로, 기간(end_date)은 같은 간격 유지."""
    t = models.get_todo(tid, g.user["id"])
    if not t:
        abort(404)
    new_day = _valid_date((request.get_json(silent=True) or request.form).get("new_date"))
    if not new_day:
        return jsonify({"error": "날짜가 올바르지 않습니다."}), 400
    fields = {"due_date": new_day}
    if t.get("end_date"):
        delta = (date.fromisoformat(new_day) - date.fromisoformat(t["due_date"])).days
        fields["end_date"] = (date.fromisoformat(t["end_date"]) + timedelta(days=delta)).isoformat()
    models.update_todo(tid, g.user["id"], **fields)
    return jsonify({"ok": True})


@app.route("/api/todos/reminders")
@login_required
def api_todo_reminders():
    """리마인드 시각이 지난 미완료 할 일 — 브라우저 알림 폴링용."""
    now_iso = datetime.now().isoformat(timespec="seconds")
    uid = g.user["id"]
    items = []
    for t in models.due_reminder_todos(uid, now_iso):
        items.append({"id": f"remind-{t['id']}", "kind": "remind",
                      "label": "할 일 리마인드", "body": t["title"]})
    for item in models.unread_shared_todos(uid):
        items.append({"id": f"shared-{item['id']}", "kind": "shared",
                      "label": "할 일 공유됨",
                      "body": (item.get("owner_name") or "동료") + "님이 공유: " + item["title"]})
    # 완료 알림 — 공유 할 일을 다른 사람이 완료함 (한 번만 전달 후 확인 처리)
    for n in models.pop_unseen_todo_notifications(uid):
        items.append({"id": f"done-{n['id']}", "kind": "done",
                      "label": "할 일 완료됨",
                      "body": (n.get("actor_name") or "동료") + "님이 완료: " + (n.get("todo_title") or "할 일")})
    return jsonify({"items": items})


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


@app.route("/stats/hospitals")
@login_required
def hospital_stats_view():
    """모병원별 입원 환자 집계와 환자 단위 상세 목록."""
    preset, date_from, date_to = _stats_period_from_request()
    hospital = (request.args.get("hospital") or "").strip()
    q = (request.args.get("q") or "").strip()
    overall = models.hospital_admission_analysis(date_from, date_to)
    filtered = models.hospital_admission_analysis(
        date_from, date_to, hospital=hospital or None, q=q or None)
    return render_template(
        "stats_hospitals.html", preset=preset, date_from=date_from, date_to=date_to,
        hospital=hospital, q=q, hospitals=overall["hospitals"], data=filtered,
    )


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

# ── 기간 계산기 ──
# 상담 중 "이 환자, 회복기 되나? 언제까지 있을 수 있나?"를 즉답하기 위한 도구.
# 판정 규칙은 상담일지와 같은 함수(recovery_window_days / noncns_stay_days)를 쓰고,
# 계산기는 진단군 → 대표 병명값 변환만 담당한다.
# 진단군: (표시명, 대표 병명값, 설명)
PERIOD_CALC_GROUPS = [
    ("중추신경계", ["뇌출혈"], "뇌출혈·뇌경색·뇌손상·척수손상·뇌성마비·마비 — 90일 이내 입원 시 S005"),
    ("근골격계 (단일부위)", ["고관절 골절"], "고관절·대퇴부·골반 골절 중 한 부위 — 재원 30일"),
    ("근골격계 (다발·내고정술·치환술)", ["다발부위-고관절 골절"], "두 부위 이상 또는 내고정술·전치환술 — 재원 60일"),
    ("비사용증후군군", ["비사용증후군"], "호흡·심장·신생물·패혈증·신부전·파킨슨(신규)·길랑바레 등 — 재원 60일"),
    ("골유합 지연", ["골유합 지연"], "골절 후 골유합이 지연된 경우 — 재원 60일"),
    ("하지 부위 절단", ["하지 부위 절단"], "재원 60일"),
]
PERIOD_CALC_GROUP_MAP = {name: diseases for name, diseases, _d in PERIOD_CALC_GROUPS}

# 발병일/수술일 기준 최대 재활 인정 한도. 입원 가능 일수를 다 채우지 못하는 상한.
PERIOD_CALC_CAP_YEARS = 2


def _add_months(d, months):
    """date + 개월. 말일 보정(1/31 + 1개월 = 2/28)."""
    y, m = divmod((d.month - 1) + months, 12)
    y, m = d.year + y, m + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def _day_of(base, n):
    """base를 1일째로 셀 때 n일째에 해당하는 날짜. (n일 기간의 종료일)"""
    return base + timedelta(days=n - 1)


def _parse_date(value):
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _cns_stay_plan(elapsed, window, delayed):
    """중추신경계 — 경과일 → S005 / S044 / S006 판정과 회복기 일수.
    S044 회복기 일수 = (인정 기간 + 180) − 경과일수. 0 이하면 대상 아님.
    """
    if elapsed <= window:
        return {"code": "S005", "label": "회복기",
                "recovery_days": RECOVERY_STAY_DAYS, "note": ""}
    limit = window + RECOVERY_STAY_DAYS
    over = elapsed - window
    if delayed:
        remain = limit - elapsed
        if remain > 0:
            return {"code": "S044", "label": "지연된 회복기", "recovery_days": remain,
                    "note": f"{window}일을 {over}일 초과 → {limit} − {elapsed} = {remain}일"}
        return {"code": "S006", "label": "비회복기", "recovery_days": 0,
                "note": (f"발병/수술 후 {elapsed}일째 — {limit}일을 넘겨 "
                         f"지연된 회복기(S044) 인정 불가")}
    return {"code": "S006", "label": "비회복기", "recovery_days": 0,
            "note": f"{window}일을 {over}일 초과"}


def compute_period_plan(onset, planned, group_name, delayed=False):
    """발병일/수술일 + 입원예정일 + 진단군 → 회복기 여부·기간·도래 일자.
    delayed: 급성기 치료로 입원이 지연됨(S044 검토 대상) 체크 여부.
    Returns: dict(ok=False, error=...) 또는 계산 결과 dict.
    """
    od, pd = _parse_date(onset), _parse_date(planned)
    diseases = PERIOD_CALC_GROUP_MAP.get(group_name)
    if not od or not pd:
        return {"ok": False, "error": "발병일/수술일과 입원예정일을 모두 입력해주세요."}
    if not diseases:
        return {"ok": False, "error": "진단군을 선택해주세요."}
    if pd < od:
        return {"ok": False, "error": "입원예정일이 발병일/수술일보다 빠릅니다."}

    # 발병/수술일을 1일째로 세어 입원일이 며칠째인지 (입원 당일 포함)
    elapsed = (pd - od).days + 1
    window = recovery_window_days(diseases)
    if not window:
        return {"ok": False, "error": "이 진단군은 회복기 판정 기준이 없습니다."}
    is_cns = is_cns_diseases(diseases)

    if is_cns:
        stay = _cns_stay_plan(elapsed, window, delayed)
        code, label, code_note = stay["code"], stay["label"], stay["note"]
        recovery_days = stay["recovery_days"]
        delayed_limit = window + RECOVERY_STAY_DAYS
        # S044는 차감 후 1일이라도 남아야 하므로 마지막 인정일은 (한계 - 1)일째
        delayed_deadline = _day_of(od, delayed_limit - 1)
        # 체크 안 했지만 S044로 인정될 수 있는 구간이면 안내
        delayed_hint = not delayed and window < elapsed < delayed_limit
        total = TOTAL_STAY_DAYS
        mandatory = False
    else:
        # 비중추신경계는 S005 하나뿐 — S044(지연)도 S006(연장)도 없다.
        # 재원 기간이 진단군별로 고정이고 그 안에 반드시 퇴원해야 한다.
        delayed_limit = delayed_deadline = None
        delayed_hint = False
        mandatory = True
        stay_days = noncns_stay_days(diseases)
        if elapsed <= window:
            code, label = "S005", "회복기"
            recovery_days = total = stay_days
            code_note = f"재원 {stay_days}일 — 이 기간 안에 반드시 퇴원"
        else:
            code, label = None, "대상 아님"
            recovery_days = total = 0
            code_note = (f"{window}일을 {elapsed - window}일 초과 — "
                         f"이 진단군은 S005만 가능해 수가 산정 불가")

    noncovered_days = max(total - recovery_days, 0)
    recovery_end = _day_of(pd, recovery_days) if recovery_days else None
    admission_end = _day_of(pd, total) if total else None
    # 회복기(S005) 인정 마감일 — 이 날짜까지 입원해야 S005 (발병일이 1일째)
    recovery_deadline = _day_of(od, window)

    if not total:
        segments = []
    elif not is_cns:
        segments = [{"kind": "rec", "name": "회복기(S005) 재원", "days": total,
                     "start": pd, "end": admission_end}]
    elif recovery_days:
        segments = [
            {"kind": "rec", "name": f"{label}({code})", "days": recovery_days,
             "start": pd, "end": recovery_end},
            {"kind": "non", "name": "비회복기(S006)", "days": noncovered_days,
             "start": recovery_end, "end": admission_end},
        ]
    else:
        segments = [{"kind": "non", "name": "비회복기(S006)", "days": total,
                     "start": pd, "end": admission_end}]
    segments = [x for x in segments if x["days"]]

    # 막대 눈금 — 90일 이상이면 개월, 그 미만이면 주 단위로 촘촘하게
    bar = None
    if total and admission_end:
        for seg in segments:
            seg["pct"] = round(seg["days"] / total * 100, 2)
        ticks = []
        if total >= 90:
            unit = "개월"
            n = 1
            while True:
                d = _add_months(pd, n)
                days = (d - pd).days
                if days > total:
                    break
                ticks.append({"n": n, "date": d, "days": days,
                              "pct": round(days / total * 100, 2)})
                n += 1
        else:
            unit = "주"
            for n in range(1, total // 7 + 1):
                days = n * 7
                ticks.append({"n": n, "date": pd + timedelta(days=days), "days": days,
                              "pct": round(days / total * 100, 2)})
        bar = {"unit": unit, "ticks": ticks, "total": total}

    # 도래일 = 달력 n개월째의 마지막 날 (시작일이 1일째이므로 하루 뺀다)
    cap_date = _add_months(od, 12 * PERIOD_CALC_CAP_YEARS) - timedelta(days=1)
    milestones = [
        {"label": "1년", "basis": "입원예정일",
         "date": _add_months(pd, 12) - timedelta(days=1)},
        {"label": "1년 6개월", "basis": "입원예정일",
         "date": _add_months(pd, 18) - timedelta(days=1)},
        {"label": f"{PERIOD_CALC_CAP_YEARS}년", "basis": "발병일/수술일",
         "date": cap_date},
    ]
    for m in milestones:
        m["over_cap"] = m["date"] > cap_date
        m["days_from_planned"] = (m["date"] - pd).days

    # 일정 — 입원 후 항목은 회복기 종료 / 1년 / 1년 6개월 / 2년 네 가지.
    # 같은 날짜에 겹치면(중추신경계는 재원 종료 = 1년) 한 줄로 묶는다.
    events = {}

    def add_event(d, text, note="", kind="main"):
        if not d:
            return
        row = events.setdefault(d, {"date": d, "items": [], "kind": kind})
        row["items"].append({"text": text, "note": note})
        if kind == "main":
            row["kind"] = "main"

    add_event(pd, "입원", f"발병/수술 {elapsed}일째")
    if recovery_end:
        add_event(recovery_end,
                  "회복기 종료" + ("" if is_cns else " — 반드시 퇴원"),
                  f"입원 {recovery_days}일째"
                  + ("" if is_cns else " · 재원 종료"))
    elif admission_end:
        add_event(admission_end, "재원 종료", f"입원 {total}일째")
    if is_cns:
        for m in milestones:
            note = f"{m['basis']} 기준"
            if m["label"] == "1년":
                note += " · 입원 만료"
            add_event(m["date"], m["label"], note,
                      kind="main" if m["label"] == "1년" else "ref")
    timeline = sorted(events.values(), key=lambda x: x["date"])
    for row in timeline:
        # 입원 당일이 1일째
        row["day_index"] = (row["date"] - pd).days + 1
        row["over_cap"] = row["date"] > cap_date

    # 실제 종료일 — 입원 가능 일수를 다 못 채우는 경우가 많아 상한과 비교한다.
    effective_end = min(admission_end, cap_date) if admission_end else None
    return {
        "ok": True,
        "onset": od, "planned": pd,
        "group": group_name, "diseases": diseases,
        "is_cns": is_cns, "mandatory": mandatory,
        "label": label, "code": code, "code_note": code_note,
        "delayed": bool(delayed), "delayed_hint": delayed_hint,
        "delayed_deadline": delayed_deadline, "delayed_limit_days": delayed_limit,
        "elapsed_days": elapsed,
        "recovery_period": window,
        "recovery_days_left": window - elapsed,
        "recovery_deadline": recovery_deadline,
        "total_days": total,
        "recovery_stay_days": recovery_days,
        "noncovered_stay_days": noncovered_days,
        "recovery_end": recovery_end,
        "segments": segments, "bar": bar, "timeline": timeline,
        "billing_applies": bool(recovery_days),
        "admission_end": admission_end,
        "cap_date": cap_date,
        "cap_years": PERIOD_CALC_CAP_YEARS,
        "effective_end": effective_end,
        "capped": bool(admission_end and admission_end > cap_date),
        "capped_lost_days": (max((admission_end - cap_date).days, 0)
                             if admission_end else 0),
        "milestones": milestones,
    }


_KRPG_DATA_PATH = Path(__file__).with_name("data") / "krpg_v22.json"


@lru_cache(maxsize=1)
def _krpg_data():
    with _KRPG_DATA_PATH.open(encoding="utf-8") as fp:
        return json.load(fp)


def _normalize_kcd(value):
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


@app.route("/tools/krpg")
@login_required
def krpg_lookup():
    """KRPG 2.2 사업대상 1,477개 KCD 코드 즉시 조회."""
    template = ("krpg_lookup_embed.html" if request.args.get("embed") == "1"
                else "krpg_lookup.html")
    return render_template(template, krpg_meta=_krpg_data())


@app.route("/api/krpg/search")
@login_required
def api_krpg_search():
    query = (request.args.get("q") or "").strip()
    scope = (request.args.get("scope") or "business").strip()
    try:
        page = max(int(request.args.get("page") or 1), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        requested_page_size = int(request.args.get("page_size") or 30)
    except (TypeError, ValueError):
        requested_page_size = 30
    page_size = requested_page_size if requested_page_size in (30, 50, 100, 200) else 30
    if scope not in ("business", "all", "changes"):
        scope = "business"
    normalized = _normalize_kcd(query)
    lowered = query.casefold()
    data = _krpg_data()
    items = data["datasets"][scope]
    business_keys = {(_normalize_kcd(x["kcd"]), x["kric"])
                     for x in data["datasets"]["business"]}
    business_codes = {_normalize_kcd(x["kcd"])
                      for x in data["datasets"]["business"]}
    change_by_key = {
        (_normalize_kcd(x["kcd"]), x["kric"]): x.get("note", "")
        for x in data["datasets"]["changes"]
    }

    exact, prefix, text_matches = [], [], []
    looks_like_code = bool(normalized) and any(ch.isdigit() for ch in normalized) \
        and not any("가" <= ch <= "힣" for ch in query)
    if query:
        for item in items:
            item_code = _normalize_kcd(item["kcd"])
            if looks_like_code and item_code == normalized:
                exact.append(item)
            elif looks_like_code and item_code.startswith(normalized):
                prefix.append(item)
            elif (lowered in item["name_ko"].casefold()
                  or lowered in item["name_en"].casefold()):
                text_matches.append(item)
        matched = exact + prefix + text_matches
    else:
        matched = items
    # 같은 KRIC·KCD 조합은 한 번만 보여준다. 동일 KCD의 다른 KRIC 분류는 유지한다.
    unique, seen = [], set()
    for item in matched:
        display_key = (item["kric"], item["kcd"])
        if display_key not in seen:
            seen.add(display_key)
            unique.append(item)
    total_matches = len(unique)
    total_pages = max((total_matches + page_size - 1) // page_size, 1)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    page_items = []
    for item in unique[start:start + page_size]:
        lookup_key = (_normalize_kcd(item["kcd"]), item["kric"])
        enriched = dict(item)
        enriched["business_target"] = lookup_key in business_keys
        enriched["change"] = change_by_key.get(lookup_key, "")
        page_items.append(enriched)
    return jsonify({
        "query": query,
        "scope": scope,
        "normalized": normalized if looks_like_code else "",
        "eligible": bool(looks_like_code and normalized in business_codes),
        "exact": bool(exact),
        "exact_count": len(exact),
        "total_matches": total_matches,
        "items": page_items,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "version": data["version"],
        "counts": data["counts"],
    })


@app.route("/tools/period-calc")
@login_required
def period_calc():
    """발병일/수술일 + 입원예정일 → 회복기 여부·기간·도래 일자 계산기."""
    onset = (request.args.get("onset") or "").strip()
    planned = (request.args.get("planned") or "").strip()
    group = (request.args.get("group") or "").strip()
    delayed = request.args.get("delayed") == "1"
    plan = None
    if onset or planned or group:
        plan = compute_period_plan(onset, planned, group, delayed=delayed)
    # embed=1 — 어느 페이지에서든 띄우는 팝업(iframe)용. 헤더/네비 없이 본문만.
    template = ("period_calc_embed.html" if request.args.get("embed") == "1"
                else "period_calc.html")
    return render_template(
        template,
        groups=PERIOD_CALC_GROUPS,
        onset=onset, planned=planned, group=group, delayed=delayed,
        plan=plan,
        today=datetime.now().strftime("%Y-%m-%d"),
    )


@app.route("/consult/new")
@login_required
def consult_new():
    # 인박스 미처리 인바운드에서 상담 등록을 시작한 경우 — communication 로드 후 prefill
    inbox_comm = None
    patient = None
    prefill_consult = None  # 재상담: 같은 환자의 가장 최근 상담에서 일부 필드 prefill
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

    # 미니카드 '새 상담 등록' / 환자 상세 '재상담' 진입 — 환자 정보 + 가장 최근 상담 일부 필드 prefill
    if not patient:
        try:
            pid_arg = int(request.args.get("patient_id") or 0)
        except (ValueError, TypeError):
            pid_arg = 0
        if pid_arg:
            patient = models.get_patient(pid_arg)
            if patient:
                history = models.patient_consultations(pid_arg)
                if history:
                    last = history[0]  # 가장 최근 상담 (consult_date DESC)
                    # 환자 단위로 거의 변하지 않는 필드만 prefill — 매 상담마다 새로 입력해야 하는
                    # 발병일·의식·활동·병명·입원예정일 등은 제외. source_hospital은 저장 시
                    # current_location_name에서 자동 매핑되므로 둘 다 채울 필요는 없음.
                    SAFE_PREFILL = (
                        "current_location_type", "current_location_name",
                        "referral_source_detail",
                        "referrer_person", "referrer_institution",
                        "attending_doctor",
                    )
                    prefill_consult = {k: last.get(k) for k in SAFE_PREFILL if last.get(k)}

    return render_template("consult_form.html", consultation=None, patient=patient,
                           inbox_comm=inbox_comm, prefill=prefill_consult,
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
    patient_todos = _annotate_todos(
        models.list_todos_for_patient(g.user["id"], c["patient_id"]), date.today())
    return render_template("consult_detail.html", c=c, history=history,
                           admission_events=models.list_admission_events(cid),
                           patient_todos=patient_todos, today_str=date.today().isoformat(),
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
CONSULT_PAGE_SIZE_OPTIONS = (30, 50, 100, 200)


@app.route("/consultations")
@login_required
def consult_list():
    filters = _list_filters_from_request()
    quick_filters = models.list_quick_filters()
    quick_filter_editor = models.list_quick_filters(include_inactive=True)
    sort = request.args.get("sort") or "date"
    sort_dir = "asc" if (request.args.get("dir") or "").lower() == "asc" else "desc"
    try:
        page = max(1, int(request.args.get("page") or 1))
    except (ValueError, TypeError):
        page = 1
    try:
        requested_page_size = int(request.args.get("page_size") or CONSULT_PAGE_SIZE)
    except (ValueError, TypeError):
        requested_page_size = CONSULT_PAGE_SIZE
    page_size = (requested_page_size if requested_page_size in CONSULT_PAGE_SIZE_OPTIONS
                 else CONSULT_PAGE_SIZE)
    total = models.count_consultations(**filters)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    offset = (page - 1) * page_size
    rows = models.list_consultations(
        **filters, sort=sort, sort_dir=sort_dir,
        limit=page_size, offset=offset,
    )
    return render_template(
        "consult_list.html", rows=rows, filters=filters,
        sort=sort, sort_dir=sort_dir,
        page=page, total_pages=total_pages, total=total,
        page_size=page_size, page_size_options=CONSULT_PAGE_SIZE_OPTIONS,
        page_start=offset,
        COUNSELORS=COUNSELORS,
        ADMISSION_STATUSES=ADMISSION_STATUSES,
        DISEASE_GROUPS=list(DISEASES_GROUPS.keys()),
        SIDO_LIST=SIDO_LIST,
        REFERRAL_TYPES=REFERRAL_TYPES,
        CONSULT_CHANNELS=CONSULT_CHANNELS,
        quick_filters=quick_filters,
        quick_filter_editor=quick_filter_editor,
        RECOVERY_OPTIONS=["회복기", "비회복기", "일반재활", "요양"],
    )


@app.route("/api/quick-filters", methods=["POST"])
@admin_required
def api_quick_filters():
    payload = request.get_json(silent=True) or {}
    items = payload.get("filters") or []
    if not isinstance(items, list):
        return jsonify({"error": "filters must be a list"}), 400

    allowed_keys = {
        "from", "to", "q", "insurance", "counselor", "admission_status",
        "consult_result", "blacklist", "disease_group", "residence_sido",
        "recovery", "consult_channel", "referral_type", "gender", "age_min",
        "age_max", "guardian", "hospital",
    }
    cleaned = []
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        label = (item.get("label") or "").strip()
        if not label:
            continue
        filter_def = item.get("filter") or {}
        if not isinstance(filter_def, dict):
            continue
        clean_filter = {}
        preset = (filter_def.get("preset") or "").strip()
        if preset:
            if preset != "today":
                return jsonify({"error": f"지원하지 않는 preset: {preset}"}), 400
            clean_filter["preset"] = preset
        params = filter_def.get("params") or {}
        if params:
            if not isinstance(params, dict):
                return jsonify({"error": "params must be an object"}), 400
            clean_params = {}
            for key, value in params.items():
                key = (key or "").strip()
                if key not in allowed_keys:
                    return jsonify({"error": f"지원하지 않는 필터 항목: {key}"}), 400
                value = str(value or "").strip()
                if value:
                    clean_params[key] = value
            if clean_params:
                clean_filter["params"] = clean_params
        if not clean_filter:
            continue
        cleaned.append({
            "label": label[:40],
            "filter": clean_filter,
            "sort_order": idx,
            "active": bool(item.get("active", True)),
        })

    if not cleaned:
        return jsonify({"error": "저장할 빠른필터가 없습니다."}), 400
    models.replace_quick_filters(cleaned)
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="quick_filters_update", target_type="quick_filters",
        detail=json.dumps({"count": len(cleaned)}, ensure_ascii=False),
        ip=request.remote_addr,
    )
    return jsonify({"ok": True, "filters": models.list_quick_filters(include_inactive=True)})


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
    # 구 생애주기 이벤트는 재원 관리 전환 후 화면에서 제외한다.
    timeline = [e for e in models.patient_timeline(pid, viewer_id=g.user["id"])
                if e.get("kind") != "lifecycle"]
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="view_patient", target_type="patient", target_id=pid,
        ip=request.remote_addr,
    )
    patient_todos = _annotate_todos(
        models.list_todos_for_patient(g.user["id"], pid), date.today())
    return render_template("patient_detail.html", p=p, history=history,
                           timeline=timeline, patient_todos=patient_todos,
                           today_str=date.today().isoformat(),
                           MGMT_TAG_PRESETS=MGMT_TAG_PRESETS)


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
    # 자동완성 ranking — 신규 등록 시 사용한 마스터 row의 use_count + 1.
    # update 시엔 안 함 (재저장으로 인플레이션 방지).
    _bump_master_use_counts(cfields)
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
        # 외진 나가 있는 상태에서 퇴원하면 미복귀 기록이 영원히 남는다.
        if models.open_away_event(cid):
            return jsonify({"error": "외진 중인 환자입니다. 복귀 처리 후 퇴원하세요."}), 400
        ddate = (payload.get("discharge_date") or "").strip() or date.today().isoformat()
        if not ddate:
            return jsonify({"error": "퇴원일자를 입력하세요."}), 400
        try:
            datetime.strptime(ddate, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "퇴원일자 형식 오류 (YYYY-MM-DD)"}), 400
        fields["admission_status"] = "퇴원완료"
        fields["discharge_date"] = ddate
        fields["discharge_destination"] = (payload.get("discharge_destination") or "").strip()[:120]
        fields["discharge_reason"] = (payload.get("discharge_reason") or "").strip()[:500]
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


# ──── 대시보드 follow-up 토글 (회복기 전환 보호자 연락 / 퇴원 1차 면담) ────

def _toggle_follow_up(cid, field, audit_action):
    """consultations[field](DATETIME)를 토글 — 비어있으면 현재 시각, 있으면 NULL."""
    existing = models.get_consultation(cid)
    if not existing:
        return jsonify({"error": "not found"}), 404
    cur = existing.get(field)
    new_val = None if cur else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    models.update_consultation_meta(cid, **{field: new_val})
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action=audit_action, target_type="consultation", target_id=cid,
        detail="set" if new_val else "unset", ip=request.remote_addr,
    )
    return jsonify({"ok": True, field: new_val})


@app.route("/api/consult/<int:cid>/recovery-call", methods=["POST"])
@login_required
def api_consult_recovery_call(cid):
    """회복기→비회복기 전환 D-15 환자의 보호자 전화 완료 마킹 토글."""
    return _toggle_follow_up(cid, "recovery_call_at", "recovery_call")


@app.route("/api/consult/<int:cid>/discharge-interview", methods=["POST"])
@login_required
def api_consult_discharge_interview(cid):
    """퇴원예정 D-30 환자의 1차 병동 면담 완료 마킹 토글."""
    return _toggle_follow_up(cid, "discharge_interview_at", "discharge_interview")


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
def lifecycle_board_legacy():
    """구 생애주기 보드 — 재원 관리(/ward)로 대체됐다. 북마크·외부 링크 보호용 리다이렉트."""
    return redirect(url_for("ward_view", q=request.args.get("q") or None))


@app.route("/lifecycle/board")
@login_required
def lifecycle_board():
    """환자 생애주기 관리 보드 — 단계별 컬럼에 환자 카드 배치.
    필터: q(검색) / period(기간) / stages[](단계) / dx(병명그룹) / doctor / archived(아카이브 포함)
    """
    # 구 주소로 접근해도 새 재원 관리 화면으로 일관되게 연결한다.
    return redirect(url_for("ward_view", q=request.args.get("q") or None))

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
    away_flag = request.args.get("away") in ("1", "true", "yes")
    away_overdue_flag = request.args.get("away_overdue") in ("1", "true", "yes")
    recovery_due_flag = request.args.get("recovery_due") in ("1", "true", "yes")
    # 기본 보기 — view=all이 아니면 입원·입원대기 자동 필터 (시급 환자)
    view = request.args.get("view") or ""
    any_explicit_filter = (q or stage_filter or dx or doctor or hospital or
                           stale_only or new_30d_only or
                           discharge_imminent_only or recovery_due_flag or
                           away_flag or away_overdue_flag or include_archived)
    if view != "all" and not any_explicit_filter:
        stage_filter = ["입원", "입원대기"]
    # 액션 필터(퇴원임박)는 단계 무관 (모든 단계에서 적용)
    if discharge_imminent_only:
        stage_filter = None

    # ── 작업 집합을 한 번만 만든다 ──
    # 단계·액션 필터를 SQL이 아니라 파이썬에서 걸어, KPI와 현황 스트립이 '무엇을
    # 보고 있든' 같은 전체 기준으로 계산되게 한다. 필터를 걸 때마다 숫자가
    # 흔들리면 "상담 0명"처럼 오해를 부른다.
    all_rows = models.lifecycle_board(
        q=q, period_days=period_days, stages=None,
        disease_group=dx, doctor=doctor, include_archived=include_archived,
    )
    # 모병원 필터 (post-filter)
    if hospital:
        pid_set = set()
        if all_rows:
            conn = models.get_db()
            placeholders = ",".join("?" * len(all_rows))
            rows = conn.execute(
                f"SELECT DISTINCT patient_id FROM consultations "
                f"WHERE patient_id IN ({placeholders}) AND source_hospital = ?",
                [p["id"] for p in all_rows] + [hospital],
            ).fetchall()
            pid_set = {r["patient_id"] for r in rows}
            conn.close()
        all_rows = [p for p in all_rows if p["id"] in pid_set]

    # 입원 환자 부가정보 — 퇴원 D-day(의료법 입원기간 룰) + 수가 구간
    for pt in all_rows:
        if pt.get("last_admission_status") == "입원완료" and pt.get("last_consult_id"):
            con = models.get_consultation(pt["last_consult_id"])
            if con:
                dw = _discharge_watch(con)
                if dw:
                    pt["discharge_dday"] = dw["days_left"]
                    pt["discharge_due_date"] = dw["due_date"]
                    pt["discharge_mandatory"] = dw["mandatory"]
                # 수가 구간 — 발병일+진단군 자동 판정. 컬럼이 아니라 '입원' 안의 레인.
                pt.update(_care_phase(con))
                pt["recovery_due"] = (pt.get("care_phase") == "회복기"
                                      and pt.get("phase_dday") is not None
                                      and pt["phase_dday"] <= 15)
    # ── 현재 외진 중(미복귀) 정보 부착 ──
    # 단계값이 아니라 admission_events.returned_at IS NULL 이 유일한 판정 근거다.
    away_by_pid = {}
    for a in models.away_now([p["id"] for p in all_rows]):
        away_by_pid.setdefault(a["pid"], a)   # 환자당 가장 오래된 미복귀 1건
    for pt in all_rows:
        pt["away"] = away_by_pid.get(pt["id"])

    # KPI — 전부 all_rows 기준. 카드에 뜬 숫자와 클릭 후 개수가 항상 일치한다.
    kpis = models.lifecycle_board_kpis(all_rows)
    kpis["away_now"] = len(away_by_pid)
    kpis["away_overdue"] = sum(1 for a in away_by_pid.values() if a.get("overdue"))
    # 회복기 수가(S005) 만료 D-15 — 비회복기 전환 안내 대상 (초과분 포함)
    kpis["recovery_due"] = sum(1 for p in all_rows if p.get("recovery_due"))

    # ── 표시 대상 추리기 ──
    patients = all_rows
    if stage_filter:
        patients = [p for p in patients if p.get("lifecycle_stage") in stage_filter]
    if stale_only:
        patients = [p for p in patients if p.get("is_stale")]
    if new_30d_only:
        patients = [p for p in patients
                    if p.get("stage_days_int") is not None and p["stage_days_int"] <= 30]
    if recovery_due_flag:
        patients = [p for p in patients if p.get("recovery_due")]
    if away_flag:
        patients = [p for p in patients if p.get("away")]
    if away_overdue_flag:
        patients = [p for p in patients if (p.get("away") or {}).get("overdue")]
    if discharge_imminent_only:
        patients = [p for p in patients
                    if p.get("discharge_dday") is not None and p["discharge_dday"] <= 3]

    board = {s: [] for s in LIFECYCLE_STAGES}
    board["기타"] = []
    for pt in patients:
        st = pt.get("lifecycle_stage") or "기타"
        board.setdefault(st if st in board else "기타", []).append(pt)
    if not board["기타"]:
        board.pop("기타")
    # 정렬 — 카드가 수백 장이라 '무엇부터 처리해야 하는가' 순으로 고정한다.
    #   입원: 기한 임박 순(외진 중 > 구간·퇴원 D-day 작은 순)
    #   그 외: 오래 방치된 순(단계 진입 후 경과일 내림차순)
    def _admit_key(p):
        ddays = [d for d in (p.get("phase_dday"), p.get("discharge_dday"))
                 if d is not None]
        return (0 if p.get("away") else 1,
                min(ddays) if ddays else 10 ** 6,
                -(p.get("stage_days_int") or 0))

    for stage_name, cards in board.items():
        if stage_name == "입원":
            cards.sort(key=_admit_key)
        else:
            cards.sort(key=lambda p: -(p.get("stage_days_int") or 0))
    # 단계별 카운트는 KPI 필터 영향 받음 — 카테고리 카드는 전체 환자 기준으로 별도 조회가 필요할 수도 있으나
    # 일단 보드와 동기화된 값으로 표시 (현재 상황 = 활성 단계만)
    # 사이드 패널 데이터 (모병원·응급전원)
    # 사이드(모병원·외진)는 필터와 무관하게 전체 기준 — 드릴다운 입구 역할이라
    # 필터를 걸 때마다 목록이 사라지면 못 쓴다.
    side = models.lifecycle_board_side(all_rows)
    side["away"] = sorted(
        away_by_pid.values(),
        key=lambda a: (not a.get("overdue"), -(a.get("days_out") or 0)),
    )
    # 주치의 옵션 — 최근 상담에서 추출 (config 5명 + 자유 입력 환자가 있을 수 있음)
    doctor_options = sorted(set(filter(None,
        (p.get("last_doctor") for p in all_rows))))
    return render_template(
        "lifecycle.html", board=board, q=q or "", total=len(patients),
        kpis=kpis, side=side,
        filters={
            "period": period_raw, "stages": stage_filter or [],
            "dx": dx or "", "doctor": doctor or "", "hospital": hospital or "",
            "archived": include_archived,
            "stale": stale_only, "new30": new_30d_only,
            "discharge_imminent": discharge_imminent_only,
            "away": away_flag, "away_overdue": away_overdue_flag,
            "recovery_due": recovery_due_flag,
            "view": view,
        },
        LIFECYCLE_STAGES=LIFECYCLE_STAGES,
        doctor_options=doctor_options,
        DISEASE_GROUPS=list(DISEASES_GROUPS.keys()),
    )


@app.route("/ward")
@login_required
def ward_view():
    """재원 관리 — 지금 병원 안에 누가 있고, 누가 외진 나가 있는가.

    별도의 단계 필드를 두지 않는다. 화면의 모든 구분이 실제 사실에서 파생된다:
      · 재원      = 입원완료 + 실제 입원일 있음 + 퇴원일 없음
      · 외진 중   = 그 중 admission_events.returned_at IS NULL 인 건이 있는 환자
      · 입원 미확정 = 입원완료인데 실제 입원일이 아직 없는 건 (입력 큐)
    유지해야 할 상태값이 없으므로 아무도 손대지 않아도 명부가 어긋나지 않는다.
    """
    q = (request.args.get("q") or "").strip() or None
    doctor = (request.args.get("doctor") or "").strip() or None
    sort = request.args.get("sort") or "dday"
    show_old = request.args.get("old") in ("1", "true", "yes")
    # KPI 카드별 세부 내역 필터 — 재원 목록을 해당 항목으로 좁혀 본다.
    filt = (request.args.get("filt") or "").strip() or None
    ward_f = (request.args.get("ward") or "").strip() or None      # 병동 빠른 조회
    tag_f = (request.args.get("tag") or "").strip() or None        # 관리 태그
    organism_f = (request.args.get("organism") or "").strip() or None  # 내성균 보유

    rows = models.list_consultations(admission_status="입원완료", q=q,
                                     q_scope="ward", limit=10000)
    if doctor:
        rows = [c for c in rows if (c.get("attending_doctor") or "") == doctor]
    # 퇴원 기록이 있으면 명부에서 빠진다
    rows = [c for c in rows if not (c.get("discharge_date") or "").strip()]

    away_by_cid = {a["consultation_id"]: a for a in models.away_now()}
    admitted, pending = [], []
    for c in rows:
        adm = (c.get("actual_admission_date") or c.get("admission_date") or "").strip()
        c["admitted_on"] = adm or None
        if not adm:
            pending.append(c)
            continue
        c["away"] = away_by_cid.get(c["id"])
        c["stay_days"] = _days_since(adm)
        c.update(_care_phase(c))
        c["recovery_due"] = (c.get("care_phase") == "회복기"
                             and c.get("phase_dday") is not None
                             and c["phase_dday"] <= 30)
        dw = _discharge_watch(c)
        if dw:
            c["discharge_dday"] = dw["days_left"]
            c["discharge_due"] = dw["due_date"]
        c.update(_extension_tier(c))
        c.update(_split_diagnosis(c))
        admitted.append(c)

    # 외진 이력 — 참고 정보. 재원 경과일·수가 D-day에서 외진 기간을 빼지 않는다.
    # 관리 태그 + 병동 라벨 부착
    tag_map = models.patient_tags_map([c.get("patient_id") for c in admitted])
    for c in admitted:
        c["mgmt_tags"] = tag_map.get(c.get("patient_id"), [])
        c["ward_label"] = _dashboard_ward_label(c.get("room_number"))
    hist = models.away_history([c["id"] for c in admitted])
    for c in admitted:
        c["away_hist"] = hist.get(c["id"])

    away = [c for c in admitted if c.get("away")]
    away.sort(key=lambda c: -((c["away"].get("days_out")) or 0))

    # ── 병실 뷰 — 병동 → 호실 → 침상 ──
    rooms, unassigned = {}, []
    for c in admitted:
        room = (c.get("room_number") or "").strip()
        if not room:
            unassigned.append(c)
            continue
        ward = _dashboard_ward_label(room)
        rooms.setdefault(ward, {}).setdefault(room, []).append(c)
    room_view = []
    for ward in sorted(rooms, key=lambda w: (_room_sort_key(w), w)):
        beds = [
            {"room": r, "patients": sorted(rooms[ward][r],
                                           key=lambda c: c.get("patient_name") or ""),
             "empty": max(0, max(ROOM_CAPACITY, len(rooms[ward][r])) - len(rooms[ward][r]))}
            for r in sorted(rooms[ward], key=_room_sort_key)
        ]
        room_view.append({"ward": ward, "rooms": beds,
                          "n": sum(len(b["patients"]) for b in beds)})

    def _dday(c):
        vals = [d for d in (c.get("phase_dday"), c.get("discharge_dday")) if d is not None]
        return min(vals) if vals else 10 ** 6
    sorters = {
        "dday": lambda c: (_dday(c), c.get("patient_name") or ""),
        "room": lambda c: (_room_sort_key(c.get("room_number")), c.get("patient_name") or ""),
        "stay": lambda c: -(c.get("stay_days") or 0),
        "name": lambda c: c.get("patient_name") or "",
    }
    admitted.sort(key=sorters.get(sort, sorters["dday"]))

    # 입력 큐 — 오래된 건은 접어둔다. 상담사가 기억하는 최근 건부터 채우게 한다.
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    def _pend_basis(c):
        return (c.get("planned_admission_date") or c.get("consult_date") or "")
    pending.sort(key=_pend_basis, reverse=True)
    pending_recent = [c for c in pending if _pend_basis(c) >= cutoff]
    pending_old = [c for c in pending if _pend_basis(c) < cutoff]

    recovery_n = sum(1 for c in admitted if c.get("care_phase") == "회복기")
    nonrecovery_n = sum(1 for c in admitted if c.get("care_phase") == "비회복기")
    total_n = len(admitted)
    recovery_ratio = round(recovery_n / total_n * 100) if total_n else 0
    kpis = {
        "admitted": total_n,
        "recovery": recovery_n,
        "nonrecovery": nonrecovery_n,
        "recovery_ratio": recovery_ratio,
        "recovery_ratio_ok": recovery_ratio >= 40,
        "away": len(away),
        "away_overdue": sum(1 for c in away if c["away"].get("overdue")),
        "recovery_due": sum(1 for c in admitted if c.get("recovery_due")),
        "discharge_soon": sum(1 for c in admitted
                              if c.get("discharge_dday") is not None
                              and c["discharge_dday"] <= 7),
        "discharge_due_30": sum(1 for c in admitted
                                if c.get("discharge_dday") is not None
                                and c["discharge_dday"] <= 30),
        "ext1": sum(1 for c in admitted if c.get("ext_tier") == 1),
        "ext2": sum(1 for c in admitted if c.get("ext_tier") == 2),
        "pending": len(pending),
        "pending_recent": len(pending_recent),
        "unassigned": len(unassigned),
    }
    recovery_due_list = sorted(
        [c for c in admitted if c.get("recovery_due")],
        key=lambda c: (c.get("phase_dday") if c.get("phase_dday") is not None else 10 ** 6,
                       c.get("patient_name") or ""),
    )
    discharge_due_list = sorted(
        [c for c in admitted if c.get("discharge_dday") is not None
         and c["discharge_dday"] <= 30],
        key=lambda c: (c.get("discharge_dday"), c.get("patient_name") or ""),
    )

    # 각 날짜의 재원 명부를 복원해 회복기 환자 비율을 계산한다.
    trend_rows = models.list_consultations(limit=10000)
    def _ratio_at(snapshot):
        census = []
        snapshot_iso = snapshot.isoformat()
        for con in trend_rows:
            admitted_on = (con.get("actual_admission_date") or con.get("admission_date") or "")[:10]
            discharged_on = (con.get("discharge_date") or "")[:10]
            if not admitted_on or admitted_on > snapshot_iso:
                continue
            if discharged_on and discharged_on <= snapshot_iso:
                continue
            census.append(con)
        recovery_count = 0
        for con in census:
            if _recovery_status(con).get("label") != "회복기":
                continue
            period = compute_admission_period(con.get("diseases"), "회복기")
            try:
                admitted_on = date.fromisoformat(
                    (con.get("actual_admission_date") or con.get("admission_date"))[:10])
            except (TypeError, ValueError):
                continue
            billing_end = (_day_of(admitted_on, period["billing"])
                           if period and period.get("billing") else None)
            if billing_end is None or snapshot <= billing_end:
                recovery_count += 1
        total = len(census)
        return {"total": total, "recovery": recovery_count,
                "ratio": round(recovery_count * 100 / total, 1) if total else 0}

    daily_ratio_trend = []
    for offset in range(29, -1, -1):
        snapshot = date.today() - timedelta(days=offset)
        daily_ratio_trend.append({"label": snapshot.strftime("%m.%d"),
                                  "date": snapshot.isoformat(), **_ratio_at(snapshot)})
    monthly_ratio_trend = []
    current_month = date.today().replace(day=1)
    for offset in range(11, -1, -1):
        month_index = current_month.year * 12 + current_month.month - 1 - offset
        year, month0 = divmod(month_index, 12)
        month = month0 + 1
        snapshot = min(date(year, month, calendar.monthrange(year, month)[1]), date.today())
        monthly_ratio_trend.append({"label": f"{str(year)[2:]}.{month:02d}",
                                    "date": snapshot.isoformat(), **_ratio_at(snapshot)})

    discharged = models.list_consultations(admission_status="퇴원완료", limit=10000)
    discharged = sorted(
        [c for c in discharged if (c.get("discharge_date") or "").strip()],
        key=lambda c: (c.get("discharge_date") or "", c.get("patient_name") or ""),
        reverse=True,
    )[:100]
    doctor_options = sorted({c.get("attending_doctor") for c in rows
                             if (c.get("attending_doctor") or "").strip()})

    # 관리 태그 카운트 (필터 칩용)
    tag_counts = {}
    for c in admitted:
        for t in c.get("mgmt_tags", []):
            tag_counts[t] = tag_counts.get(t, 0) + 1

    # 병동 빠른 조회 — 병실 뷰(배치도)와 목록 모두 적용
    if ward_f:
        room_view = [w for w in room_view if w["ward"] == ward_f]

    # KPI/태그/균 세부 필터 — 목록 표시에 적용
    filt_label = _WARD_FILTS[filt][0] if filt in _WARD_FILTS else None
    admitted_list = _apply_ward_filters(admitted, filt, ward_f, tag_f, organism_f)
    view = request.args.get("view") or "room"
    if filt in _WARD_FILTS or tag_f or organism_f:
        view = "list"   # 세부 내역을 볼 땐 목록 뷰로
    any_filter = bool(filt in _WARD_FILTS or ward_f or tag_f or organism_f)
    return render_template(
        "ward.html", away=away, admitted=admitted_list,
        room_view=room_view, unassigned=unassigned,
        view=view,
        pending=pending_recent, pending_old=pending_old, show_old=show_old,
        kpis=kpis, q=q or "", doctor=doctor or "", sort=sort,
        filt=filt, filt_label=filt_label,
        ward_f=ward_f, tag_f=tag_f, organism_f=organism_f, any_filter=any_filter,
        WARDS=WARDS, MGMT_TAG_PRESETS=MGMT_TAG_PRESETS, tag_counts=tag_counts,
        doctor_options=doctor_options,
        roster_open=bool(q or doctor or any_filter),
        recovery_due_list=recovery_due_list, discharge_due_list=discharge_due_list,
        daily_ratio_trend=daily_ratio_trend, monthly_ratio_trend=monthly_ratio_trend,
        discharged=discharged,
    )


def _ward_admitted_roster(q, doctor):
    """재원(입원완료·미퇴원·입원일 있음) 환자 목록 — 파생 필드 모두 부착. ward_view/CSV 공유."""
    rows = models.list_consultations(admission_status="입원완료", q=q,
                                     q_scope="ward", limit=10000)
    if doctor:
        rows = [c for c in rows if (c.get("attending_doctor") or "") == doctor]
    rows = [c for c in rows if not (c.get("discharge_date") or "").strip()]
    away_by_cid = {a["consultation_id"]: a for a in models.away_now()}
    admitted = []
    for c in rows:
        adm = (c.get("actual_admission_date") or c.get("admission_date") or "").strip()
        if not adm:
            continue
        c["admitted_on"] = adm
        c["away"] = away_by_cid.get(c["id"])
        c["stay_days"] = _days_since(adm)
        c.update(_care_phase(c))
        c["recovery_due"] = (c.get("care_phase") == "회복기"
                             and c.get("phase_dday") is not None and c["phase_dday"] <= 30)
        dw = _discharge_watch(c)
        if dw:
            c["discharge_dday"] = dw["days_left"]
            c["discharge_due"] = dw["due_date"]
        c.update(_extension_tier(c))
        c.update(_split_diagnosis(c))
        c["ward_label"] = _dashboard_ward_label(c.get("room_number"))
        admitted.append(c)
    tag_map = models.patient_tags_map([c.get("patient_id") for c in admitted])
    for c in admitted:
        c["mgmt_tags"] = tag_map.get(c.get("patient_id"), [])
    return admitted


@app.route("/ward.csv")
@admin_required
def ward_csv():
    """현재 필터 조건의 재원 명부를 CSV로 — 병동·주치의·수가구간·연장·균·태그 반영."""
    q = (request.args.get("q") or "").strip() or None
    doctor = (request.args.get("doctor") or "").strip() or None
    admitted = _ward_admitted_roster(q, doctor)
    admitted = _apply_ward_filters(
        admitted, (request.args.get("filt") or "").strip() or None,
        (request.args.get("ward") or "").strip() or None,
        (request.args.get("tag") or "").strip() or None,
        (request.args.get("organism") or "").strip() or None)
    admitted.sort(key=lambda c: (_room_sort_key(c.get("room_number")), c.get("patient_name") or ""))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["병동", "호실", "환자", "나이", "성별", "주치의", "주진단", "부진단",
                "내성균", "입원일", "재원일수", "수가구간", "D-day", "회복기종료/만료일",
                "퇴원예정", "연장", "관리태그"])
    for c in admitted:
        pd = c.get("phase_dday")
        dday = ("" if pd is None else (f"D-{pd}" if pd >= 0 else f"{-pd}일초과"))
        w.writerow([
            c.get("ward_label") or "", c.get("room_number") or "", c.get("patient_name") or "",
            c.get("patient_age") if c.get("patient_age") is not None else "",
            {"M": "남", "F": "여"}.get(c.get("gender"), ""),
            c.get("attending_doctor") or "",
            ", ".join(c.get("dx_primary") or []), ", ".join(c.get("dx_secondary") or []),
            ", ".join(c.get("organisms") or []),
            c.get("admitted_on") or "",
            c.get("stay_days") if c.get("stay_days") is not None else "",
            c.get("care_phase") or "", dday, c.get("phase_end_date") or "",
            c.get("discharge_due") or "", c.get("ext_label") or "",
            ", ".join(c.get("mgmt_tags") or []),
        ])
    models.log_audit(user_id=g.user["id"], username=g.user["username"],
                     action="export_csv", target_type="ward",
                     detail=f"재원 {len(admitted)}건", ip=request.remote_addr)
    data = buf.getvalue().encode("utf-8-sig")
    return send_file(io.BytesIO(data), mimetype="text/csv", as_attachment=True,
                     download_name=f"ward_{datetime.now().strftime('%Y%m%d_%H%M')}.csv")


@app.route("/api/patient/<int:pid>/tags", methods=["POST"])
@login_required
def api_patient_tags(pid):
    if not models.get_patient(pid):
        abort(404)
    data = request.get_json(silent=True) or request.form
    tags = data.get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    models.set_patient_tags(pid, tags or [])
    models.log_audit(user_id=g.user["id"], username=g.user["username"],
                     action="update_patient", target_type="patient", target_id=pid,
                     detail="관리 태그 변경", ip=request.remote_addr)
    return jsonify({"ok": True, "tags": models.get_patient(pid)["mgmt_tags"]})


def _days_since(datestr):
    try:
        d = datetime.strptime(str(datestr)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return (date.today() - d).days


_ORGANISMS = ("CRE", "VRE", "MRSA")


def _split_diagnosis(c):
    """diseases를 주 진단(회복기재활 입원 질환)·부 진단(기저·기타)으로 분리하고,
    내성균(CRE/VRE/MRSA) 목록을 뽑는다.
    Returns dict(dx_primary, dx_secondary, organisms)."""
    base = set(DISEASES_GROUPS.get("기저질환", []))
    primary, secondary = [], []
    for d in (c.get("diseases") or []):
        s = str(d).strip()
        if not s:
            continue
        if any(kw and (kw in s or s in kw) for kw in base):
            secondary.append(s)
        else:
            primary.append(s)
    # 폼에 별도 주/부 진단 텍스트가 있으면 앞에 반영
    pd = (c.get("primary_diagnosis") or "").strip()
    if pd and pd not in primary:
        primary.insert(0, pd)
    sd = (c.get("secondary_diagnosis") or "").strip()
    if sd and sd not in secondary:
        secondary.append(sd)
    sc = c.get("special_care") or []
    organisms = [x for x in _ORGANISMS if x in sc]
    return {"dx_primary": primary, "dx_secondary": secondary, "organisms": organisms}


# 재원 목록 세부 필터 — (라벨, 판정함수). KPI 카드/칩과 CSV가 공유.
_WARD_FILTS = {
    "recovery":    ("회복기 (S005)",   lambda c: c.get("care_phase") == "회복기"),
    "nonrecovery": ("비회복기 (S006)", lambda c: c.get("care_phase") == "비회복기"),
    "recdue":      ("회복기 종료 D-30", lambda c: c.get("recovery_due")),
    "dis30":       ("퇴원 예정 D-30",   lambda c: c.get("discharge_dday") is not None and c["discharge_dday"] <= 30),
    "ext1":        ("연장 1회 (1.5년)", lambda c: c.get("ext_tier") == 1),
    "ext2":        ("연장 2회 (2년)",   lambda c: c.get("ext_tier") == 2),
}


def _apply_ward_filters(admitted, filt=None, ward_f=None, tag_f=None, organism_f=None):
    """재원 목록에 병동·KPI구분·태그·내성균 필터를 순차 적용."""
    out = admitted
    if ward_f:
        out = [c for c in out if c.get("ward_label") == ward_f]
    if filt in _WARD_FILTS:
        pred = _WARD_FILTS[filt][1]
        out = [c for c in out if pred(c)]
    if tag_f:
        out = [c for c in out if tag_f in (c.get("mgmt_tags") or [])]
    if organism_f:
        out = [c for c in out if c.get("organisms")]
    return out


# 입원 연장 분류 — 기본 1년(TOTAL_STAY_DAYS) + 6개월(EXTENSION_DAYS) 연장 최대 2회(≈2년).
EXTENSION_DAYS = 180


def _extension_tier(c):
    """discharge_due_date(입원연장 시 새 퇴원예정일)가 기본 만료(입원일+1년)를 얼마나
    넘는지로 연장 횟수 판정. 발병일+2년을 최종 한도로 함께 계산.
    Returns dict(ext_tier 0/1/2, ext_label, ext_extra_days, ext_cap_date, ext_cap_left)."""
    out = {"ext_tier": 0, "ext_label": "기본 (1년)", "ext_extra_days": 0,
           "ext_cap_date": None, "ext_cap_left": None}
    adm = c.get("admitted_on")
    dd = (c.get("discharge_due_date") or "").strip()
    # 최종 진단일(발병일) + 2년 = 절대 한도
    onset = (c.get("disease_onset") or "").strip()
    if onset:
        try:
            od = datetime.strptime(onset[:10], "%Y-%m-%d").date()
            cap = od + timedelta(days=730)
            out["ext_cap_date"] = cap.isoformat()
            out["ext_cap_left"] = (cap - date.today()).days
        except (ValueError, TypeError):
            pass
    if not adm or not dd:
        return out
    try:
        ad = datetime.strptime(adm[:10], "%Y-%m-%d").date()
        dr = datetime.strptime(dd[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return out
    extra = (dr - ad).days - TOTAL_STAY_DAYS      # 기본 1년 만료 대비 초과일
    out["ext_extra_days"] = max(0, extra)
    if extra <= 30:                                # 소폭 조정은 기본으로 흡수
        out["ext_tier"] = 0
    elif extra <= EXTENSION_DAYS + 90:             # ≈ +180 → 1회
        out["ext_tier"] = 1
        out["ext_label"] = "연장 1회 (1.5년)"
    else:                                          # ≈ +360 → 2회
        out["ext_tier"] = 2
        out["ext_label"] = "연장 2회 (2년)"
    return out


def _room_sort_key(room):
    """호실 정렬 — '502', '502-1', 'A동 3층' 등 섞여 있어 숫자 우선으로 정렬."""
    r = (room or "").strip()
    if not r:
        return (1, "")
    digits = "".join(ch for ch in r if ch.isdigit())
    return (0, int(digits)) if digits else (1, r)


@app.route("/api/consult/<int:cid>/room", methods=["POST"])
@login_required
def api_consult_room(cid):
    """호실 지정·변경 — 병실 뷰에서 침상 배치를 바로 잡는다."""
    if not models.get_consultation(cid):
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    room = (payload.get("room_number") or "").strip()
    models.update_consultation(cid, room_number=room or None)
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="update_room", target_type="consultation", target_id=cid,
        detail=room or "(호실 해제)", ip=request.remote_addr,
    )
    return jsonify({"ok": True, "room_number": room or None})


@app.route("/api/consult/<int:cid>/admit", methods=["POST"])
@login_required
def api_consult_admit(cid):
    """입원일 확정 — 이 시점부터 재원 명부에 오르고 D-day가 돌기 시작한다."""
    con = models.get_consultation(cid)
    if not con:
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    adate = (payload.get("admission_date") or "").strip()
    if not adate:
        adate = (con.get("planned_admission_date") or "").strip()
    if not adate:
        return jsonify({"error": "입원일을 입력하세요."}), 400
    try:
        datetime.strptime(adate, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "입원일 형식 오류 (YYYY-MM-DD)"}), 400
    fields = {"actual_admission_date": adate}
    room = (payload.get("room_number") or "").strip()
    if room:
        fields["room_number"] = room
    models.update_consultation(cid, **fields)
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="confirm_admission", target_type="consultation", target_id=cid,
        detail=f"입원일 {adate}" + (f" · {room}호" if room else ""),
        ip=request.remote_addr,
    )
    return jsonify({"ok": True, "admission_date": adate, "room_number": room or None})


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
    # 회복기·비회복기 전환은 더 이상 단계가 아니다 (발병일+진단군 자동 판정) —
    # 타임라인 이벤트로만 남기고 단계는 '입원' 유지.
    stage_map = {
        "회복기 전환": "입원",
        "비회복기 전환": "입원",
        "응급치료": "입원",
        "복귀": "입원",
        "입원": "입원",
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


# ───────────────────── 옴니채널 — 커뮤니케이션 ─────────────────────
# (구 /inbox 라우트는 2026-05-25 대시보드로 통합되어 제거됨.
#  models.inbox_callbacks·inbox_open_communications 등 함수는 대시보드가 사용 중)


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
    event_time = _valid_time(payload.get("event_time"))
    if payload.get("event_time") and not event_time:
        return jsonify({"error": "이송 시각 형식 오류"}), 400
    con = models.get_consultation(cid)
    pid = con["patient_id"] if con else None
    cur_stage = None
    if pid:
        p = models.get_patient(pid)
        cur_stage = (p.get("lifecycle_stage") or "").strip() if p else None

    # ── 복귀는 새 행이 아니라 '나감' 행을 닫는다 (出/歸 페어링) ──
    if event_type == "복귀":
        open_ev = models.open_away_event(cid)
        if open_ev:
            models.mark_admission_event_returned(
                open_ev["id"], return_date=event_date or None,
                returned_by=g.user.get("display_name"),
            )
        # 복귀 시 단계는 '입원' 고정이 아니라 나가기 직전 단계로 원상복구
        # (회복기 환자가 외진 다녀와서 입원으로 강등되던 문제)
        if pid:
            back_to = (open_ev or {}).get("stage_before") or "입원"
            _set_lifecycle_stage_clinical(pid, back_to)

    # 나가기 직전 단계 보존 — 레거시 '응급치료' 값은 복구 대상으로 삼지 않는다
    stage_before = None
    if event_type in models.AWAY_EVENT_TYPES:
        stage_before = cur_stage if cur_stage and cur_stage != "응급치료" else "입원"
    eid = models.create_admission_event(
        consultation_id=cid, event_type=event_type,
        event_date=event_date or None,
        event_time=event_time,
        hospital=(payload.get("hospital") or "").strip() or None,
        memo=(payload.get("memo") or "").strip() or None,
        created_by=g.user.get("display_name"),
        stage_before=stage_before,
    )
    # 외진은 단계를 바꾸지 않는다 — 병상을 유지한 일시 이탈이므로 '입원'에 그대로
    # 머물고, 미복귀 플래그(returned_at IS NULL)와 카드 배지로만 표시한다.
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="add_admission_event", target_type="consultation", target_id=cid,
        detail=event_type, ip=request.remote_addr,
    )
    return jsonify({"ok": True, "id": eid})


@app.route("/api/admission-event/<int:event_id>/return", methods=["POST"])
@login_required
def api_admission_event_return(event_id):
    """외진 복귀 처리 — 나감 이벤트에 복귀일을 찍고 단계를 원래대로 되돌린다.
    보드 카드의 [↩ 복귀] 1클릭과 상담 상세의 복귀 버튼이 함께 쓴다.
    """
    ev = models.get_admission_event(event_id)
    if not ev:
        return jsonify({"error": "not found"}), 404
    if ev.get("returned_at"):
        return jsonify({"error": "이미 복귀 처리된 외진입니다."}), 400
    payload = request.get_json(silent=True) or {}
    return_date = (payload.get("return_date") or "").strip() or None
    if return_date:
        try:
            rd = datetime.strptime(return_date, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "복귀일 형식 오류 (YYYY-MM-DD)"}), 400
        if rd > date.today():
            return jsonify({"error": "복귀일이 미래입니다."}), 400
        out = (ev.get("event_date") or "").strip()
        if out:
            try:
                if rd < datetime.strptime(out[:10], "%Y-%m-%d").date():
                    return jsonify({"error": f"복귀일이 외진 나간 날({out})보다 빠릅니다."}), 400
            except ValueError:
                pass
    models.mark_admission_event_returned(
        event_id, return_date=return_date,
        returned_by=g.user.get("display_name"),
    )
    con = models.get_consultation(ev["consultation_id"])
    back_to = (ev.get("stage_before") or "입원").strip()
    if con:
        _set_lifecycle_stage_clinical(con["patient_id"], back_to)
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="return_admission_event", target_type="consultation",
        target_id=ev["consultation_id"],
        detail=f"{ev.get('event_type')} 복귀 → {back_to}", ip=request.remote_addr,
    )
    return jsonify({"ok": True, "stage": back_to,
                    "return_date": return_date or date.today().isoformat()})


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
    # ← 돌아가기 — 진입 경로 추론 (cid → 상담상세 / pid → 환자상세 / 그 외 → 대시보드)
    if cid and preselect:
        back_url, back_label = f"/consult/{cid}", "← 상담 상세"
    elif pid:
        back_url, back_label = f"/patients/{pid}", "← 환자 상세"
    else:
        back_url, back_label = "/", "← 대시보드"
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
        return jsonify({"items": [], "master_size": 0})
    # autocomplete_hospitals는 {items, master_size} 반환
    return jsonify(models.autocomplete_hospitals(q, limit=50))


@app.route("/api/autocomplete/nursing")
@login_required
def api_ac_nursing():
    q = (request.args.get("q") or "").strip()
    if len(q) < 1:
        return jsonify({"items": [], "master_size": 0})
    return jsonify(models.autocomplete_nursing_homes(q, limit=50))


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


@app.route("/api/patient/<int:pid>/minicard")
@login_required
def api_patient_minicard(pid):
    info = models.patient_minicard(pid)
    if not info:
        return jsonify({"error": "not_found"}), 404
    return jsonify(info)


@app.route("/api/patient/<int:pid>/blacklist-info")
@login_required
def api_patient_blacklist_info(pid):
    info = models.patient_blacklist_info(pid)
    if not info:
        return jsonify({"error": "not_found"}), 404
    return jsonify(info)


@app.route("/api/patients/by-name")
@login_required
def api_patients_by_name():
    name = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"items": []})
    return jsonify({"items": models.patients_by_name(name)})


@app.route("/api/patient/merge", methods=["POST"])
@login_required
@admin_required
def api_patient_merge():
    payload = request.get_json(silent=True) or {}
    try:
        source_id = int(payload.get("source_id"))
        target_id = int(payload.get("target_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "source_id·target_id 필수"}), 400
    try:
        result = models.merge_patients(source_id, target_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"병합 실패: {e}"}), 500
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="merge_patient", target_type="patient", target_id=target_id,
        detail=f"merged #{source_id} → #{target_id}: "
               f"{result['moved']} fields_filled={result['filled_fields']}",
        ip=request.remote_addr,
    )
    return jsonify({"ok": True, **result})


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
        "stay_period": request.args.get("stay_period") or None,
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
        out["current_location_name"] = models.canonical_hospital_name(out["current_location_name"])
        out["source_hospital"] = out["current_location_name"]
    elif loc_type == "입소중" and out.get("current_nursing_name"):
        out["current_nursing_name"] = models.canonical_nursing_name(out["current_nursing_name"])
        out["source_hospital"] = out["current_nursing_name"]
    # 추천 기관도 모병원과 동일하게 별칭→공식명 정규화 (마스터 미일치 자유 텍스트는
    # 클라이언트에서 차단되지만 서버에서도 보수적으로 정규화).
    if out.get("referrer_institution"):
        out["referrer_institution"] = models.canonical_hospital_name(out["referrer_institution"])
    # admission_purpose_category 자동 산출 — admission_purpose 저장값 우선,
    # 없으면 disease_onset + diseases 기반 자동 판정
    cat = _purpose_to_category(out.get("admission_purpose"))
    if cat is None and out.get("disease_onset"):
        ref = (out.get("actual_admission_date")
               or out.get("planned_admission_date")
               or out.get("consult_date"))
        cat = compute_recovery(ref, out.get("disease_onset"), out.get("diseases"))
    out["admission_purpose_category"] = cat

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


def _bump_master_use_counts(fields):
    """신규 상담 등록 후 사용된 마스터의 use_count + 1. 자동완성 ranking용."""
    loc_type = (fields.get("current_location_type") or "")
    if loc_type == "입원중" and fields.get("current_location_name"):
        models.bump_facility_use_count(fields["current_location_name"], table="source_hospitals")
    elif loc_type == "입소중" and fields.get("current_nursing_name"):
        models.bump_facility_use_count(fields["current_nursing_name"], table="source_nursing_homes")
    if fields.get("referrer_institution"):
        models.bump_facility_use_count(fields["referrer_institution"], table="source_hospitals")


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
