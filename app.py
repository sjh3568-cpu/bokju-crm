"""Flask 앱 — 복주회복병원 상담실 CRM (bokju-crm).

진입점. 인증·상담 등록/목록/상세·자동완성·통계 API를 한 파일에 모음.
규모가 커지면 Blueprint로 쪼갤 것 (현재는 cafe-helper 스타일 단일 파일).
"""
import csv
import hashlib
import hmac
import time
import io
import json
import logging
import os
import re
import secrets
import threading
import calendar
from contextlib import closing
from functools import lru_cache
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from dotenv import load_dotenv
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash
from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)

import backup
import dashboard_metrics
import models
import release_notes
from auth import (
    admin_required, authenticate, current_user, is_locked_out,
    login_required, login_user, logout_user, menu_level,
)
from config import (
    APP_VERSION, APP_DEVELOPER, CALL_RECORDING_URL,
    ACTIVITY_ACTIVE_OPTIONS, ACTIVITY_DIAPER_OPTIONS, ACTIVITY_OTHERS_OPTIONS,
    ACTIVITY_WHEELCHAIR_OPTIONS, ADMISSION_DOCS, ADMISSION_STATUSES,
    AUDIT_ACTION_LABELS, AUDIT_CATEGORIES, AUDIT_CATEGORY_OTHER,
    AUDIT_CRITICAL_ACTIONS, AUDIT_RETENTION_DAYS,
    ADMISSION_EVENT_TYPES, ATTENDING_DOCTORS, DOCTOR_DEPT_CODES,
    BED_OPTIONS, CAREGIVER_OPTIONS,
    CONSCIOUSNESS_MAIN_OPTIONS, CONSULT_CHANNELS, CONVERSATION_LEVEL_OPTIONS,
    CONSULT_RESULTS, CONSULT_RESULT_REASON_LABELS, REJECTION_REASONS,
    COMM_CHANNELS, COMM_INBOUND_CHANNELS,
    COST_GUIDANCE_OPTIONS, CURRENT_LOCATION_TYPES, DIET_TYPES, DIET_LAYOUT,
    DISEASES_CHECKLIST, DISEASES_GROUPS, GUARDIAN_RELATION_SUGGESTIONS,
    HEARING_OPTIONS, INFO_PROVIDED_OPTIONS,
    COUNSELORS, COUNSELORS_ACTIVE, DISEASES_LAYOUT, OTHERS_LAYOUT, ROOM_CAPACITY,
    ROOM_BED_CAPACITIES,
    WARDS, MGMT_TAG_PRESETS,
    ROLE_LABELS, SEED_USERS,
    MENUS, MENU_KEYS, MENU_MAX_LEVEL, ROLE_PRESETS, role_preset,
    PERM_HIDDEN, PERM_VIEW, PERM_EDIT, PERM_CREATE,
    PERM_LEVELS, PERM_LEVEL_LABELS,
    INSURANCE_TYPES, OTHERS_CHECKLIST, REFERRAL_SOURCE_GROUPS, REFERRAL_TYPES,
    INBOUND_CHANNEL_REFERRAL, INBOUND_CHANNEL_LABELS,
    STAFF_REFERRAL_ORGS, STAFF_REFERRAL_DEPTS,
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
import partnerships
import support_requests
app.register_blueprint(partnerships.bp)
app.register_blueprint(support_requests.bp)
import transport
import homepage_board
app.register_blueprint(transport.bp)
import ward_moves
app.register_blueprint(ward_moves.bp)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(hours=int(os.getenv("SESSION_HOURS", "4")))
_REMEMBER_COOKIE = "bokju_remember"
_REMEMBER_DAYS = max(1, int(os.getenv("AUTO_LOGIN_DAYS", "30")))
_remember_serializer = URLSafeTimedSerializer(app.secret_key, salt="bokju-auto-login-v1")

# 통합 인박스(/inbox) — 2026-09-10 기능 보류로 기본 숨김.
# 끄면 좌측 메뉴·통합검색·시작화면 선택지에서 사라지고 라우트는 404가 된다.
# 데이터(옴니채널 커뮤니케이션)와 대시보드 '미처리 인바운드' 카드는 그대로 살아 있어
# 미처리 문의는 대시보드 오늘 처리 필요(/#action-queue)에서 계속 처리한다. 되살리려면 .env에 INBOX_ENABLED=1.
INBOX_ENABLED = os.getenv("INBOX_ENABLED", "0") == "1"
# 인박스를 숨긴 동안 미처리 배지·알림은 대시보드 인바운드 카드로 보낸다.
INBOX_URL = "/inbox" if INBOX_ENABLED else "/#action-queue"

_db_initialized = False
# 다중 스레드(waitress) 환경에서 첫 요청 여러 건이 동시에 들어오면 init_db()가
# 겹쳐 돌아 ALTER TABLE·1회성 마이그레이션이 중복 실행된다. 락으로 한 번만 돌린다.
_bootstrap_lock = threading.Lock()


def initialize():
    """DB 초기화 + admin 계정 셋업 + 백업 스케줄러 기동. 몇 번 불러도 1회만 실행된다.
    .env의 APP_PASSWORD는 admin·시드 계정의 '초기' 비밀번호다 — 계정이 없을 때만 쓴다.
    admin 비밀번호를 잊었을 때만 .env에 APP_PASSWORD_RESET=1을 넣고 재기동해 되돌린다.

    serve.py(운영)는 기동 시점에, 개발 서버는 첫 요청 시점에 호출한다.
    """
    global _db_initialized
    if _db_initialized:
        return
    with _bootstrap_lock:
        if _db_initialized:
            return
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        transport.init_schema()
        release_notes.publish_release_notes()
        admin_pw = os.getenv("APP_PASSWORD", "").strip()
        if admin_pw:
            # 비상용 break-glass 계정 — 없을 때만 생성. 매 부팅 동기화는 하지 않는다:
            # 그러면 사용자 관리에서 바꾼 비밀번호·표시명이 배포(재기동)마다 초기값으로
            # 되돌아간다. 분실 복구는 APP_PASSWORD_RESET=1로 1회 되돌리고 플래그를 지운다.
            reset = os.getenv("APP_PASSWORD_RESET", "").strip() == "1"
            models.ensure_admin_user("admin", admin_pw, display_name="admin(비상)", force=reset)
            if reset:
                app.logger.warning("APP_PASSWORD_RESET=1 — admin 비밀번호·표시명·권한을 .env 초기값으로 "
                                   "되돌렸습니다. 로그인 후 .env에서 플래그를 지우고 재기동하세요.")
            # 명명된 6개 계정 시드 — 없을 때만 생성, 초기 비번=APP_PASSWORD
            for username, display_name, role in SEED_USERS:
                models.ensure_seed_user(username, display_name, role, admin_pw)
        _db_initialized = True
    if os.getenv("BACKUP_ENABLED", "1") == "1":
        backup.start_scheduler()
    # 심평원 병원 명부 자동 갱신 — HIRA_SERVICE_KEY가 있을 때만 매주 돈다
    try:
        import hira_sync
        hira_sync.start_scheduler()
    except Exception:
        app.logger.exception("심평원 자동 갱신 스케줄러를 시작하지 못했습니다")
    # 운행 시트(구글) 연동 — TRANSPORT_SHEET_URL이 있을 때만 주기 동기화
    try:
        transport.start_scheduler()
    except Exception:
        app.logger.exception("운행 시트 동기화를 시작하지 못했습니다")
    # 홈페이지 문의 메일 브릿지 — IMAP 설정 시에만 활성 (빌더형 홈페이지 대응)
    try:
        import homepage_inbox
        homepage_inbox.start_worker()
    except Exception:
        pass
    # 홈페이지 상담게시판(bokjurh.co.kr) 폴링 — 새 글 → 인박스, 인박스 '답변' → 게시판 등록
    try:
        homepage_board.start_worker()
    except Exception:
        app.logger.exception("홈페이지 상담게시판 연동을 시작하지 못했습니다")


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
    "/api/homepage-board",   # 홈페이지 상담게시판 답변 등록
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
    if path in ("/consultations.csv", "/consultations/inquiries.csv"):
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

    if path.startswith("/partners"):
        return "partners", (PERM_EDIT if is_write else PERM_VIEW)

    # ── 재원 관리 (환자·병동·생애주기·외진) ──
    if path.startswith("/ward") or path.startswith("/patients") \
            or path.startswith("/api/patient") or path.startswith("/api/admission-event") \
            or path.startswith("/lifecycle"):
        return "ward", (PERM_EDIT if is_write else PERM_VIEW)

    # ── 문자 / 커뮤니케이션 ──
    if path.startswith("/sms") or path.startswith("/api/sms") \
            or path.startswith("/api/communication") or path.startswith("/api/webhook") \
            or path.startswith("/api/homepage-board"):
        if not is_write:
            return "sms", PERM_VIEW
        return "sms", PERM_CREATE               # 발송·템플릿·기록 = 생성

    # ── 통계 ──
    if path.startswith("/stats") or path.startswith("/api/stats"):
        return "stats", PERM_VIEW

    # ── 월간보고서 ──
    if path.startswith("/report") or path.startswith("/api/report"):
        return "report", PERM_VIEW

    # ── 대시보드 (루트) · 통합 달력 ──
    if path == "/" or path.startswith("/api/dashboard") or path.startswith("/calendar"):
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
        return redirect(request.referrer or url_for("main.dashboard"))
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
            "notice_url": url_for("notices.notice_required"),
        }), 428
    return redirect(url_for("notices.notice_required", next=request.full_path.rstrip("?")))


@app.after_request
def _no_store(resp):
    """환자 정보 페이지가 브라우저 캐시에 남지 않도록.
    로그아웃 후 뒤로가기로 노출되는 것 방지.
    """
    resp.headers["Cache-Control"] = "no-store, private, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    if getattr(g, "clear_remember_cookie", False):
        resp.delete_cookie(_REMEMBER_COOKIE, path="/", samesite="Lax")
    return _gzip_response(resp)


_GZIP_TYPES = ("text/html", "application/json", "text/css", "application/javascript", "text/javascript", "text/csv", "image/svg+xml")
_GZIP_MIN_BYTES = 4096


def _gzip_response(resp):
    """텍스트 응답 gzip — 재원 관리(약 600KB)·상담목록(600KB)·대시보드(270KB)가 8~10배 줄어든다.
    waitress 앞에 역프록시가 없어 앱이 직접 압축한다. 스트리밍(파일 전송)·작은 응답·이미 인코딩된 응답은 건너뛴다."""
    try:
        if resp.direct_passthrough or resp.status_code < 200 or resp.status_code >= 300 or resp.status_code == 204:
            return resp
        if "gzip" not in (request.headers.get("Accept-Encoding") or "").lower():
            return resp
        if resp.headers.get("Content-Encoding") or not (resp.mimetype or "").startswith(_GZIP_TYPES):
            return resp
        body = resp.get_data()
        if len(body) < _GZIP_MIN_BYTES:
            return resp
        import gzip as _gz
        resp.set_data(_gz.compress(body, compresslevel=5))
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers.add("Vary", "Accept-Encoding")
    except Exception:
        app.logger.exception("응답 압축 실패 — 압축 없이 보냅니다")
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


def _call_recording_login(user):
    """사이드바 '통화 녹음' 자동 로그인 정보. .env의 CALL_RECORDING_ID/PASSWORD가 둘 다 있고
    조회 전용(viewer)이 아닐 때만 {userid, pwd_hash}를 준다. 헬로비전 biz070 녹음 사이트는
    비밀번호를 SHA-256(대문자 hex)로 보내므로 원문 대신 해시만 브라우저로 내려간다."""
    if not user or user.get("role") == "viewer":
        return None
    uid = os.getenv("CALL_RECORDING_ID", "").strip()
    pw = os.getenv("CALL_RECORDING_PASSWORD", "").strip()
    if not uid or not pw:
        return None
    return {"userid": uid, "pwd_hash": hashlib.sha256(pw.encode("utf-8")).hexdigest().upper()}


@app.context_processor
def _inject_globals():
    _u = current_user()
    account_preferences={}
    if _u:
        try: account_preferences=models.get_user_by_id(_u['id']).get('preferences_data',{})
        except Exception: account_preferences={}
    # 나의 할 일 리마인드 배지 — 오늘+지난 미완료 개수 (로그인 시에만 조회)
    todo_badge = 0
    if _u:
        try:
            todo_badge = models.todo_badge_count(_u["id"], date.today().isoformat())
        except Exception:
            todo_badge = 0
    # 통합 달력 배지 — 오늘 잡힌 상담·입원·퇴원 일정 + 오늘 ToDo(나의·공유, 미완료) 건수
    calendar_badge = 0
    if _u:
        try:
            _t = date.today().isoformat()
            for _r in models.dashboard_calendar_rows(_t, _t, None):
                if any((_r.get(k) or "")[:10] == _t for k in (
                        "consult_date", "planned_admission_date", "actual_admission_date",
                        "admission_date", "discharge_due_date", "discharge_date")):
                    calendar_badge += 1
            calendar_badge += sum(1 for t in models.list_todos_range(_u["id"], _t, _t) if not t.get("done"))
        except Exception:
            calendar_badge = 0
    pending_notice = (models.first_unread_required_announcement(
        _u["id"], _u.get("role", "staff")) if _u else None)
    password_reset_badge = 0
    if _u and menu_level(_u, "users") >= PERM_EDIT:
        try:
            password_reset_badge = models.pending_password_reset_count()
        except Exception:
            password_reset_badge = 0
    # 미처리 인바운드 배지 — 홈페이지·카카오 등 채널 문의 대기 건수 (로그인 시)
    inbound_badge = 0
    command_metrics = {"admit": 0, "discharge": 0, "pending": 0}
    if _u:
        try:
            inbound_badge = models.open_inbound_count()
        except Exception:
            inbound_badge = 0
        try:
            with closing(models.get_db()) as _db:
                _today=date.today().isoformat()
                # 오늘 실입원 + 오늘 외진 복귀(타 병원 전원 종결 제외) — 대시보드 '완료'와 같은 정의
                command_metrics["admit"]=_db.execute("""SELECT COUNT(DISTINCT patient_id) FROM (
                    SELECT patient_id FROM consultations
                     WHERE COALESCE(actual_admission_date,admission_date)=? AND admission_status IN ('입원완료','퇴원완료')
                    UNION SELECT c.patient_id FROM admission_events ae JOIN consultations c ON c.id=ae.consultation_id
                     WHERE ae.event_type IN ('응급전원','모병원 외래치료') AND ae.returned_at=?
                       AND COALESCE(ae.return_outcome,'복귀')='복귀')""",(_today,_today)).fetchone()[0]
                command_metrics["discharge"]=_db.execute("SELECT COUNT(DISTINCT patient_id) FROM consultations WHERE discharge_date=?",(_today,)).fetchone()[0]
            command_metrics["pending"]=inbound_badge
        except Exception:
            pass
    return {
        "current_user": _u,
        "app_version": APP_VERSION,
        "app_developer": APP_DEVELOPER,
        "call_recording_url": CALL_RECORDING_URL,
        "call_recording_login": _call_recording_login(_u),
        "todo_badge": todo_badge,
        "calendar_badge": calendar_badge,
        "has_unread_required_notice": bool(pending_notice),
        "password_reset_badge": password_reset_badge,
        "inbound_badge": inbound_badge,
        "inbox_enabled": INBOX_ENABLED,
        "inbox_url": INBOX_URL,
        "command_metrics": command_metrics,
        "account_preferences": account_preferences,
        "today_str": date.today().isoformat(),   # 날짜 입력 기본값(외진 기록 등)
        "today_header": date.today().strftime('%Y.%m.%d') + f" ({'월화수목금토일'[date.today().weekday()]})",
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
        "COUNSELORS_ACTIVE": COUNSELORS_ACTIVE,
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
        "STAFF_REFERRAL_ORGS": STAFF_REFERRAL_ORGS,
        "STAFF_REFERRAL_DEPTS": STAFF_REFERRAL_DEPTS,
        "SIDO_LIST": SIDO_LIST,
        "SIGUNGU_LIST": SIGUNGU_LIST,
        "SIGUNGU_INDEX": SIGUNGU_INDEX,
        "now": datetime.now,
        "url_with": _url_with,
    }


@app.route('/api/global-search')
@login_required
def global_search():
    q=(request.args.get('q') or '').strip()[:80]
    if len(q)<2:
        return jsonify(items=[])
    items=[]
    menu_items=[
        ('dashboard','대시보드','오늘 브리핑·통합 달력·입원 현황','/'),
        ('consult','상담목록','환자 상담 검색·조회','/consultations'),
        ('consult','새 상담 등록','신규 환자 상담 접수','/consult/new'),
        ('ward','재원 관리','재원 현황·입원 대기·회복기 관리','/ward'),
        ('ward','입원 대기','입원 예정·병상 대기 환자','/ward?tab=waiting'),
        ('partners','기관협력','협력기관·방문·연락·업무협약','/partners'),
        ('sms','문자','문자 발송·템플릿·발송 이력','/sms'),
        ('stats','통계 대시보드','상담·입원 핵심 통계','/stats'),
        ('stats','모병원 분석','모병원별 상담의뢰·입원완료 현황','/stats/hospitals'),
        ('stats','직원소개 분석','소개한 직원별 상담·입원 성과','/stats/staff'),
        ('report','월간보고서','월별 운영 성과 보고','/report/monthly'),
        ('dashboard','공지사항','공지·필수 확인','/notices'),
        ('dashboard','내 할 일','개인 일정·공유 업무','/todos'),
        ('dashboard','내 계정 설정','개인화·권한 요청·비밀번호','/account'),
    ]
    q_lower=q.lower()
    for permission,title,meta,url in menu_items:
        required=PERM_CREATE if url=='/consult/new' else PERM_VIEW
        if menu_level(current_user(),permission)>=required and q_lower in f'{title} {meta}'.lower():
            items.append({'kind':'메뉴','title':title,'meta':meta,'url':url})
    if menu_level(current_user(),'consult')>=PERM_VIEW:
        for row in models.list_consultations(q=q,limit=6):
            items.append({'kind':'환자·상담','title':row.get('patient_name') or '이름 없음',
                          'meta':' · '.join(filter(None,[row.get('guardian_phone'),row.get('consult_date'),row.get('admission_status')])),
                          'url':url_for('consult.consult_detail',cid=row['id'])})
    if menu_level(current_user(),'partners')>=PERM_VIEW:
        with closing(models.get_db()) as db:
            rows=db.execute('''SELECT p.id,COALESCE(p.official_name,h.name) name,
                COALESCE(d.kind,h.kind) kind,COALESCE(d.address,h.address) address
                FROM cooperation_partners p JOIN source_hospitals h ON h.id=p.hospital_id
                LEFT JOIN cooperation_facility_directory d ON d.id=p.directory_id
                WHERE COALESCE(p.official_name,h.name) LIKE ? ORDER BY p.important DESC,name LIMIT 5''',('%'+q+'%',)).fetchall()
        items.extend({'kind':'협력기관','title':r['name'],'meta':' · '.join(filter(None,[r['kind'],r['address']])),
                      'url':url_for('partners.detail',pid=r['id'])} for r in rows)
    return jsonify(items=items[:12])


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


_DOCTOR_CODES = {}
for _d in ATTENDING_DOCTORS:            # "IM1 정기천 원장" → {"정기천": "IM1"}
    _parts = _d.split()
    if len(_parts) >= 2:
        _DOCTOR_CODES[_parts[1]] = _parts[0]
_DOCTOR_CODES.update(DOCTOR_DEPT_CODES)  # 명부에만 나오는 의사(허남연 RM3·김우근 FM 등)까지


@app.template_filter("doctor_short")
def _doctor_short(value):
    """주치의를 '정기천(IM1)'로 — 명부 값(이름만)과 상담 값('IM1 정기천 원장') 모두 받는다.
    config.ATTENDING_DOCTORS에 없는 의사는 이름만."""
    if not value:
        return ""
    name = str(value).strip()
    parts = name.split()
    if len(parts) >= 2 and parts[0] in _DOCTOR_CODES.values():
        name = parts[1]
    code = _DOCTOR_CODES.get(name)
    return f"{name}({code})" if code else name


@app.template_filter("krdate_short")
def _krdate_short(value):
    """'2026-09-03' → '26.09.03' (요일 없는 목록용 축약 날짜)."""
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            return value
    return value.strftime('%y.%m.%d')


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


@app.template_filter("pct2")
def _pct2(value):
    """회복기 비율 표기 — 소수 둘째자리 고정(40 → 40.00, 42.5 → 42.50).

    round()는 40.0을 '40.0'으로 찍어 화면마다 자릿수가 들쭉날쭉해진다. 40% 기준선을
    눈으로 비교하는 숫자라 표기 자릿수를 고정한다.
    """
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return value


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
    # 중추신경계 판정은 상담 병명 우선, 없으면 명부 주상병(진단명)으로 보완한다.
    # 상담 병명칸에 뇌졸중을 안 적어(주상병엔 있는데) 비중추로 새면 퇴원예정이
    # 입원일+1년이 아니라 회복기 종료로 짧게 잡힌다.
    diseases = consultation.get("diseases")
    dx = str(consultation.get("roster_diagnosis") or consultation.get("primary_diagnosis") or "")
    if dx and not is_cns_diseases(diseases) and any(kw in dx for kw in _CNS_KW):
        diseases = list(diseases or []) + ["중추신경계"]
    period = compute_admission_period(
        diseases,
        consultation.get("roster_care_phase") or rec.get("label"),
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
    cns = is_cns_diseases(diseases)
    onset = _parse_date(consultation.get("onset_date"))
    rehab_end = (_parse_date(consultation.get("rehab_end_date"))
                 if consultation.get("rehab_end_imported") else None)

    # ── 총 입원 만료일(퇴원 예정) ──
    if cns:
        # 뇌졸중 등 중추 — 입원일 + 1년. 단 발병일 + 2년을 넘길 수 없다(상한).
        # 연장(6개월×최대2회)은 수동으로 discharge_due_date에 반영한다.
        total_d = _day_of(ad, period["total"])
        if onset:
            cap = _day_of(onset, PERIOD_CALC_CAP_YEARS * 365)
            if cap < total_d:
                total_d = cap
    elif rehab_end:
        # 비중추 — 회복기 기간이 곧 입원 기간(무조건 종료). 명부 재활종료일을 만료로 쓴다.
        total_d = rehab_end
    else:
        total_d = _day_of(ad, period["total"])

    # ── 회복기 수가(S005) 종료일 — 중추만 '전환'이 있다. 명부 실제 재활종료일 우선,
    #    없으면 입원일+180 추정. 비중추는 전환 개념이 없어 billing 없음(퇴원=회복기 종료). ──
    if cns and rehab_end:
        billing_d = rehab_end
    elif period["billing"]:
        billing_d = _day_of(ad, period["billing"])
    else:
        billing_d = None

    out = {
        "basis": "actual" if actual else "planned",
        "mandatory": period.get("mandatory", False),
        "total_days": period["total"],
        "total_date": total_d.isoformat(),
        "total_left": (total_d - today).days,
        "billing_days": period["billing"],
        "billing_date": billing_d.isoformat() if billing_d else None,
        "billing_left": (billing_d - today).days if billing_d else None,
    }
    extension_d = (total_d + timedelta(days=180)
                   if cns else None)
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
    if consultation.get("rehab_end_imported"):
        end = consultation.get("rehab_end_date")
        phase = _effective_roster_care_phase(
            None, None, None, date.today(), end, True)
        end_date = date.fromisoformat(end) if end else None
        return {"care_phase": phase,
                "phase_dday": (end_date - date.today()).days if end_date else None,
                "phase_end_date": end, "phase_end_kind": "rehab",
                "phase_mandatory": False}
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
        # S005 수가 기간이 끝났으면 더 이상 회복기 환자가 아니다. label은 발병일
        # 기준으로 '회복기로 입원했는가'를 말할 뿐이라, 입원일부터 흘러간 수가
        # 기간은 여기서 따로 봐야 한다. 이걸 안 보면 D+504인 환자까지 회복기로
        # 세어 비율이 부푼다 — 재원 260명 기준 167명(64%)으로 나왔는데 만료분
        # 85명을 빼면 82명(32%)이다. 월별 추이는 원래 만료를 반영하고 있어서
        # 같은 화면 안에서 KPI와 추이가 서로 달랐다.
        left = ax.get("billing_left")
        phase = "비회복기" if left is not None and left < 0 else "회복기"
    else:
        # 일반재활·요양 — 중추신경계라도 회복기/비회복기 '구간' 밖이다.
        # 회복기로 뭉뚱그리면 재원 카드에 엉뚱한 구간이 찍히고, recovery_due
        # ('회복기 전환 임박')가 대상 아닌 환자까지 잡아 알림이 부푼다.
        phase = "미판정"
    roster_phase = consultation.get("roster_care_phase")
    if roster_phase:
        phase = _effective_roster_care_phase(
            roster_phase, consultation.get("diseases"),
            consultation.get("actual_admission_date") or consultation.get("admission_date"),
            date.today(),
        )
    # 명부의 회복기 대상 표시는 종료된 수가 기간을 다시 열지 않는다.
    billing_phase = phase == "회복기" and ax.get("billing_left") is not None
    dday = ax.get("billing_left") if billing_phase else ax.get("total_left")
    end_date = ax.get("billing_date") if billing_phase else ax.get("total_date")
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
    "운행": "운행",
}
# 순서·묶음은 아래 KPI 카드 줄과 맞춘다 — 오늘(파랑) → 기한(주황) → 대기(회색).
_ACTION_GROUP_ORDER = ("입원준비", "운행", "담당자", "퇴원예정",
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

    def add(kind, tone, title, detail="", meta="", href=None, sort=50, age_days=0, action=None):
        # action = {"type": "comm"|"callback", "id": n} — 행에서 바로 완료/상담 등록을 누를 수 있게.
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
            "action": action,
            "group": _dashboard_action_group(kind),   # 카드의 세부 탭 이름
        })

    for m in open_comms:
        occurred = _dashboard_parse_datetime(m.get("occurred_at") or m.get("created_at"))
        hours = ((datetime.now() - occurred).total_seconds() / 3600) if occurred else 0
        tone = "danger" if hours >= 24 else "warn" if hours >= 2 else "info"
        who = m.get("patient_name") or m.get("contact") or "미연결 문의"
        kind = _dashboard_inbound_bucket(m)
        meta = _dashboard_elapsed_label(occurred)
        if m.get("status") == "waiting":
            # 부재중 → 재연락 예약: 시각 전엔 처리 대상이 아니므로 큐에서 뺀다.
            if not m.get("callback_due"):
                continue
            due = _dashboard_parse_datetime(m.get("follow_up_at"))
            late_h = ((datetime.now() - due).total_seconds() / 3600) if due else 0
            tone = "danger" if late_h >= 2 else "warn"
            kind = "재연락"
            meta = f"부재 {m.get('missed_count') or 1}회 · 재연락 {(m.get('follow_up_at') or '')[5:16]}"
        add(
            kind,
            tone,
            who,
            (m.get("summary") or m.get("body") or "")[:70],
            meta,
            f"/consult/new?comm_id={m.get('id')}" if m.get("id") else "/consultations",
            0 if tone == "danger" else 15 if tone == "warn" else 45,
            age_days=int(hours // 24),
            action={"type": "comm", "id": m.get("id"),
                    "homepage_idx": m.get("homepage_idx")} if m.get("id") else None,
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
            f"/consult/{r.get('id')}" if r.get("id") else "/consultations",
            8 if tone == "danger" else 25,
            age_days=days,
            action={"type": "callback", "id": r.get("id")} if r.get("id") else None,
        )

    for r in data.get("admission_by_status", {}).get("planned", []):
        if r.get("admission_display_date") != today:
            continue
        if r.get("admission_kind") == "return":
            continue   # 외진 복귀 예정 — 입원시간·주치의 등 입원 준비 항목이 아니다
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

    # 회복기 전환(전환체크)은 아래 '기한 임박' 카드와 겹치므로 큐에 넣지 않는다 (2026-09-13).

    # 퇴원예정: 예정일이 지났는데 아직 재원인 환자만(확인·연장 필요). 앞으로 올 D-30은 '기한 임박' 카드가 맡는다.
    for d in discharge_due:
        left = d["watch"].get("days_left")
        if left is None or left >= 0:
            continue
        add(
            "퇴원예정",
            "danger",
            d["con"].get("patient_name") or "환자 미지정",
            "퇴원 예정일이 지남 — 퇴원·연장 확인",
            f"{-left}일 초과",
            f"/consult/{d['con'].get('id')}" if d["con"].get("id") else None,
            10,
            age_days=-left,
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

    # 차량 운행(픽업) — 오늘·내일 입원인데 운행 여부 미정 / 시트 전송 안 됨 / 배정 대기
    try:
        for t in transport.dashboard_alerts():
            add(t["kind"], t["tone"], t["title"], t["detail"], t["meta"], t["href"], t["sort"], age_days=0)
    except Exception:
        app.logger.exception("운행 경고 계산 실패")

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
        "items_all": today_items,
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
    "입원대기": "입원대기",
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


# ───────────────────── 사용자 관리 (어드민 전용) ─────────────────────

_MIN_PW_LEN = 4


# ───────────────────── 메인 ─────────────────────


# 허가병상 — /ward의 병상 가동률과 같은 기준(현재 355병상).
WARD_BED_CAPACITY = 355
# 대시보드 회복기 만료·퇴원 예정 큐의 창(일). 만료일 기준 ±이 범위 안만 담는다 —
# 30일 전 D-30부터 30일 초과까지. 그보다 오래 지난 건은 입원연장을 안 적은 옛 상담이라
# 큐를 채우기만 하고 오늘 할 일이 아니다.
DASHBOARD_DUE_WINDOW_DAYS = 30


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


# ───────────────────── 상담 ─────────────────────


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


# ───────────────────── 생애주기 (3번 요청) ─────────────────────


def _effective_roster_care_phase(phase, diseases, admitted_at, snapshot,
                                 rehab_end_date=None, rehab_end_imported=False):
    """Q열이 적재됐으면 실제 종료일이 진단군 추정보다 우선한다."""
    if rehab_end_imported:
        if not rehab_end_date:
            return "비회복기"
        return "회복기" if snapshot <= date.fromisoformat(rehab_end_date) else "비회복기"
    if phase != "회복기":
        return phase
    period = compute_admission_period(diseases, "회복기")
    if not period:
        return phase
    days = period.get("billing") or period.get("total")
    try:
        end = _day_of(date.fromisoformat(str(admitted_at)[:10]), days)
    except (TypeError, ValueError):
        return phase
    if snapshot <= end:
        return phase
    # 단일 수가 질환은 S006 연장 대상이 아니므로 기존 단일구간을 유지한다.
    return "단일구간" if period.get("mandatory") else "비회복기"


# (구 /inbox 라우트는 2026-05-25 대시보드로 통합되어 제거됨.
#  models.inbox_callbacks·inbox_open_communications 등 함수는 대시보드가 사용 중)


# ───────────────────── 인바운드 webhook (옴니채널 직수신) ─────────────────────
# 카카오 비즈채널·홈페이지 문의폼이 사내망 CRM으로 보내는 유일한 외부 노출 경로.
# 보안: 역프록시에서 /api/webhook/* 만 외부로 열고 나머지는 사내망 유지.
#  ① 채널별 토큰(hmac.compare_digest, 상수시간 비교) ② 선택적 IP 화이트리스트
#  ③ 요청 크기 제한 ④ IP당 rate limit ⑤ 감사로그(식별정보 평문 미기록).



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



# ───────────────────── 화면·API Blueprint 등록 (views/) ─────────────────────
# 분리된 라우트 모듈은 여기서(모든 공용 헬퍼·필터가 정의된 뒤) import·등록한다.
# 각 모듈은 `from app import …`로 공용 헬퍼를 가져오므로, 이 블록보다 위에 있어야 하는 이름을 아래에 두지 말 것.
from views import account, admin, consult, inbound, main, notices, sms_views, stats, todos, ward  # noqa: E402
for _bp_module in (account, admin, consult, inbound, main, notices, sms_views, stats, todos, ward):
    app.register_blueprint(_bp_module.bp)
# app.py에 남은 코드·테스트(tests/*.py의 main._x)가 쓰는 이름 재수출
from views.main import _ward_status_strip  # noqa: E402,F401
from views.ward import _away_insights, _orphan_matches, _ratio_insight, _roster_care_phase, _trend_flow, _trend_summary, _ward_admitted_roster, _ward_away_panel, _ward_away_report, _ward_current_away  # noqa: E402,F401

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8003"))
    host = "0.0.0.0" if os.getenv("ALLOW_LAN", "0") == "1" else "127.0.0.1"
    app.run(host=host, port=port, debug=os.getenv("FLASK_DEBUG") == "1")
