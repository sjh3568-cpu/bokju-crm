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
import models
import release_notes
from auth import (
    admin_required, authenticate, current_user, is_locked_out,
    login_required, login_user, logout_user, menu_level,
)
from config import (
    APP_VERSION, APP_DEVELOPER,
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
    COUNSELORS, COUNSELORS_ACTIVE, DISEASES_LAYOUT, OTHERS_LAYOUT, ROOM_CAPACITY,
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
import partnerships
import support_requests
app.register_blueprint(partnerships.bp)
app.register_blueprint(support_requests.bp)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(hours=int(os.getenv("SESSION_HOURS", "4")))
_REMEMBER_COOKIE = "bokju_remember"
_REMEMBER_DAYS = max(1, int(os.getenv("AUTO_LOGIN_DAYS", "30")))
_remember_serializer = URLSafeTimedSerializer(app.secret_key, salt="bokju-auto-login-v1")

# 통합 인박스(/inbox) — 2026-09-10 기능 보류로 기본 숨김.
# 끄면 좌측 메뉴·통합검색·시작화면 선택지에서 사라지고 라우트는 404가 된다.
# 데이터(옴니채널 커뮤니케이션)와 대시보드 '미처리 인바운드' 카드는 그대로 살아 있어
# 미처리 문의는 대시보드(/#inbound)에서 계속 처리한다. 되살리려면 .env에 INBOX_ENABLED=1.
INBOX_ENABLED = os.getenv("INBOX_ENABLED", "0") == "1"
# 인박스를 숨긴 동안 미처리 배지·알림은 대시보드 인바운드 카드로 보낸다.
INBOX_URL = "/inbox" if INBOX_ENABLED else "/#inbound"

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
        partnerships.init_schema()
        support_requests.init_schema()
        release_notes.publish_release_notes()
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
    # 홈페이지 문의 메일 브릿지 — IMAP 설정 시에만 활성 (빌더형 홈페이지 대응)
    try:
        import homepage_inbox
        homepage_inbox.start_worker()
    except Exception:
        pass


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

    if path.startswith("/partners"):
        return "partners", (PERM_EDIT if is_write else PERM_VIEW)

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
                command_metrics["admit"]=_db.execute("""SELECT COUNT(DISTINCT patient_id) FROM consultations
                    WHERE COALESCE(actual_admission_date,admission_date)=? AND admission_status IN ('입원완료','퇴원완료')""",(_today,)).fetchone()[0]
                command_metrics["discharge"]=_db.execute("SELECT COUNT(DISTINCT patient_id) FROM consultations WHERE discharge_date=?",(_today,)).fetchone()[0]
            command_metrics["pending"]=inbound_badge
        except Exception:
            pass
    return {
        "current_user": _u,
        "app_version": APP_VERSION,
        "app_developer": APP_DEVELOPER,
        "todo_badge": todo_badge,
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
                          'url':url_for('consult_detail',cid=row['id'])})
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
        consultation.get("diseases"),
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
        requested_next = request.args.get("next") or request.form.get("next")
        start_paths = {'default':'/','dashboard':'/','consultations':'/consultations','ward':'/ward','partners':'/partners',
                       'inbox':'/inbox','consult_new':'/consult/new','ward_waiting':'/ward?tab=waiting','ward_trend':'/ward?tab=trend',
                       'partners_feed':'/partners?view=feed','stats_hospitals':'/stats/hospitals',
                       'todos':'/todos','sms':'/sms','stats':'/stats','report':'/report/monthly','notices':'/notices'}
        start_key = user.get('start_page') or 'dashboard'
        if start_key == 'inbox' and not INBOX_ENABLED:
            start_key = 'dashboard'
        allowed = {'default':'dashboard','dashboard':'dashboard','consultations':'consult','ward':'ward','partners':'partners',
                   'inbox':'dashboard','consult_new':'consult','ward_waiting':'ward','ward_trend':'ward','partners_feed':'partners',
                   'stats_hospitals':'stats','todos':'dashboard','sms':'sms','stats':'stats','report':'report','notices':'dashboard'}
        if menu_level(user,allowed.get(start_key,'dashboard')) < PERM_VIEW:
            start_key='dashboard'
        next_url = requested_next or start_paths.get(start_key,'/')
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


@app.route('/settings/start-page',methods=['POST'])
@login_required
def set_start_page():
    if not secrets.compare_digest(session.get('start_page_csrf',''),request.form.get('csrf','')):
        abort(400)
    key=(request.form.get('start_page') or '').strip()
    options={'default':('dashboard','/'),'dashboard':('dashboard','/'),'consultations':('consult','/consultations'),
             'inbox':('dashboard','/inbox'),'consult_new':('consult','/consult/new'),'ward':('ward','/ward'),
             'ward_waiting':('ward','/ward?tab=waiting'),'ward_trend':('ward','/ward?tab=trend'),
             'partners':('partners','/partners'),'partners_feed':('partners','/partners?view=feed'),
             'stats_hospitals':('stats','/stats/hospitals'),'todos':('dashboard','/todos'),
             'sms':('sms','/sms'),'stats':('stats','/stats'),'report':('report','/report/monthly'),
             'notices':('dashboard','/notices')}
    if not INBOX_ENABLED:
        options.pop('inbox',None)
    required_level=PERM_CREATE if key=='consult_new' else PERM_VIEW
    if key not in options or menu_level(current_user(),options[key][0])<required_level:
        abort(400)
    models.set_user_start_page(g.user['id'],key)
    flash('로그인 후 시작 화면을 저장했습니다.','success')
    return redirect(request.referrer or url_for('dashboard'))


@app.route('/account',methods=['GET','POST'])
@login_required
def account_settings():
    token=session.setdefault('account_csrf',secrets.token_hex(32))
    if request.method=='POST':
        if not secrets.compare_digest(token,request.form.get('csrf','')):
            abort(400)
        user=models.get_user_by_id(g.user['id'])
        action=(request.form.get('action') or 'password').strip()
        if action=='profile':
            values=[(request.form.get(k) or '').strip() for k in ('department','position','extension','work_phone')]
            if any(len(v)>100 for v in values):
                flash('입력 길이를 확인해주세요.','error')
            else:
                models.update_own_profile(g.user['id'],*values)
                models.log_audit(user_id=g.user['id'],username=g.user['username'],action='update_own_profile',target_type='user',target_id=g.user['id'],ip=request.remote_addr)
                flash('내 업무 정보를 저장했습니다.','success')
            return redirect(url_for('account_settings'))
        if action=='permission_request':
            menu_key=(request.form.get('menu_key') or '').strip()
            try: level=int(request.form.get('requested_level') or 0)
            except ValueError: level=0
            reason=(request.form.get('reason') or '').strip()[:500]
            if menu_key not in MENU_KEYS or level not in (1,2,3) or level>MENU_MAX_LEVEL.get(menu_key,0) or level<=int(user['perms'].get(menu_key,0)):
                flash('현재 권한보다 높은 권한을 선택해주세요.','error')
            else:
                try:
                    models.create_permission_request(g.user['id'],menu_key,level,reason)
                    models.log_audit(user_id=g.user['id'],username=g.user['username'],action='request_permission',target_type='user',target_id=g.user['id'],detail=f'{menu_key}:{level}',ip=request.remote_addr)
                    flash('권한 요청을 관리자에게 전달했습니다.','success')
                except ValueError as e: flash(str(e),'error')
            return redirect(url_for('account_settings'))
        if action=='preferences':
            prefs=dict(user.get('preferences_data',{}));mode=request.form.get('mode')
            if mode=='alerts':
                for key in ('notify_todo','notify_partner','notify_recovery','notify_discharge','notify_inbound'):
                    prefs[key]=request.form.get(key)=='1'
            elif mode=='menus':
                for key in ('show_sms','show_stats','show_partners','show_notices'):
                    prefs[key]=request.form.get(key)=='1'
            else:
                calendar_mine=request.form.get('calendar_mine','')
                partner_view=request.form.get('partner_view','')
                ward_tab=request.form.get('ward_tab','')
                if calendar_mine in ('0','1'): prefs['calendar_mine']=calendar_mine=='1'
                else: prefs.pop('calendar_mine',None)
                if partner_view in ('list','feed'): prefs['partner_view']=partner_view
                else: prefs.pop('partner_view',None)
                if ward_tab in ('status','away','waiting','trend','blacklist','quality'): prefs['ward_tab']=ward_tab
                else: prefs.pop('ward_tab',None)
            models.set_user_preferences(g.user['id'],prefs);flash('개인 알림과 기본 보기를 저장했습니다.','success')
            return redirect(url_for('account_settings'))
        current=request.form.get('current_password') or ''
        new=request.form.get('new_password') or ''
        confirm=request.form.get('confirm_password') or ''
        if not user or not check_password_hash(user['password_hash'],current):
            models.log_audit(user_id=g.user['id'],username=g.user['username'],action='password_change_fail',
                             target_type='user',target_id=g.user['id'],detail='현재 비밀번호 불일치',ip=request.remote_addr)
            flash('현재 비밀번호가 올바르지 않습니다.','error')
        elif len(new)<_MIN_PW_LEN:
            flash(f'새 비밀번호는 최소 {_MIN_PW_LEN}자 이상이어야 합니다.','error')
        elif new!=confirm:
            flash('새 비밀번호 확인이 일치하지 않습니다.','error')
        elif check_password_hash(user['password_hash'],new):
            flash('현재 비밀번호와 다른 새 비밀번호를 입력해주세요.','error')
        else:
            models.set_user_password(g.user['id'],new)
            models.resolve_password_reset_requests(g.user['id'],g.user['id'])
            models.log_audit(user_id=g.user['id'],username=g.user['username'],action='change_own_password',
                             target_type='user',target_id=g.user['id'],detail='본인 비밀번호 변경',ip=request.remote_addr)
            session['account_csrf']=secrets.token_hex(32)
            flash('비밀번호를 변경했습니다. 다음 로그인부터 새 비밀번호를 사용하세요.','success')
            return redirect(url_for('account_settings'))
    user=models.get_user_by_id(g.user['id'])
    with closing(models.get_db()) as db:
        recent_logins=[dict(r) for r in db.execute("SELECT created_at,ip FROM audit_log WHERE user_id=? AND action='login' ORDER BY id DESC LIMIT 5",(g.user['id'],))]
    return render_template('account.html',csrf=session['account_csrf'],min_password_length=_MIN_PW_LEN,
                           account=user,recent_logins=recent_logins,menus=MENUS,
                           permission_requests=models.list_permission_requests(g.user['id']))


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
    date_from = _valid_date(request.args.get("from"))
    date_to = _valid_date(request.args.get("to"))
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
    notices = models.list_announcements(
        user["id"], user.get("role", "staff"), include_inactive=is_admin,
        date_from=date_from, date_to=date_to)
    return render_template("notices.html", notices=notices, is_admin=is_admin,
                           date_from=date_from, date_to=date_to)


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
        permission_requests=models.list_permission_requests(pending_only=True),
    )


@app.route('/admin/permission-requests/<int:request_id>',methods=['POST'])
@admin_required
def admin_permission_request_resolve(request_id):
    status=(request.form.get('status') or '').strip()
    if status not in ('승인','반려'): abort(400)
    models.resolve_permission_request(request_id,g.user['id'],status)
    models.log_audit(user_id=g.user['id'],username=g.user['username'],action='resolve_permission_request',target_type='permission_request',target_id=request_id,detail=status,ip=request.remote_addr)
    flash('권한 요청을 처리했습니다. 권한 변경은 사용자 행에서 별도로 저장해주세요.','success')
    return redirect(url_for('admin_users'))


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
    admission_scope = (request.args.get("admission_scope") or "all").strip()
    if admission_scope not in ("all", "planned", "completed"):
        admission_scope = "all"
    admission_weekdays = "월화수목금토일"
    def admission_date_label(value):
        parsed = date.fromisoformat(value)
        return parsed.strftime("%Y.%m.%d") + f"({admission_weekdays[parsed.weekday()]})"
    data = models.dashboard_summary(admission_from, admission_to, admission_scope)
    data.update({
        "admission_lookup_from": admission_from,
        "admission_lookup_to": admission_to,
        "admission_lookup_scope": admission_scope,
        "admission_lookup_label": (admission_date_label(admission_from)
                                   if admission_from == admission_to else
                                   f"{admission_date_label(admission_from)} ~ {admission_date_label(admission_to)}"),
        "admission_quick_dates": [
            {"group": "today", "label": "오늘", "from": today_d.isoformat(), "to": today_d.isoformat(), "scope": "all"},
            {"group": "past", "label": "15일 전", "from": (today_d - timedelta(days=15)).isoformat(), "to": (today_d - timedelta(days=15)).isoformat(), "scope": "completed"},
            {"group": "past", "label": "7일 전", "from": (today_d - timedelta(days=7)).isoformat(), "to": (today_d - timedelta(days=7)).isoformat(), "scope": "completed"},
            {"group": "past", "label": "3일 전", "from": (today_d - timedelta(days=3)).isoformat(), "to": (today_d - timedelta(days=3)).isoformat(), "scope": "completed"},
            {"group": "future", "label": "3일 후", "from": (today_d + timedelta(days=1)).isoformat(), "to": (today_d + timedelta(days=3)).isoformat(), "scope": "planned"},
            {"group": "future", "label": "7일 후", "from": (today_d + timedelta(days=1)).isoformat(), "to": (today_d + timedelta(days=7)).isoformat(), "scope": "planned"},
            {"group": "future", "label": "15일 후", "from": (today_d + timedelta(days=1)).isoformat(), "to": (today_d + timedelta(days=15)).isoformat(), "scope": "planned"},
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
    my_name=(g.user.get('display_name') or '').strip()
    personal_admitted=[c for c in admitted if (c.get('counselor') or '').strip()==my_name]
    # 내 담당 입원예정 — 예정일 빠른 순. 예정일 미정은 뒤로.
    my_planned=[c for c in planned_consults if (c.get('counselor') or '').strip()==my_name]
    my_planned.sort(key=lambda c: (c.get('planned_admission_date') or '9999-99-99'))
    # 재연락 대기(상담요청) 중 내 담당 — 클릭 시 상담 상세로 이어짐
    my_callbacks=[c for c in callbacks if (c.get('counselor') or '').strip()==my_name]
    # 오늘 내 할 일(내 것 + 공유받은 것) — 상단에서 바로 목록 확인
    try:
        my_todos=models.list_todos(g.user['id'], today_d.isoformat())
    except Exception:
        my_todos=[]
    data['personal_briefing']={
        'name':my_name or g.user.get('username'),
        'planned':len(my_planned),
        'planned_list':my_planned[:5],
        'admitted':len(personal_admitted),
        'admitted_list':personal_admitted[:5],
        'callbacks':len(my_callbacks),
        'callback_list':my_callbacks[:6],
        'todos':[t for t in my_todos if not t.get('done')],
        'todo_done':sum(1 for t in my_todos if t.get('done')),
    }
    data['start_page_csrf']=session.setdefault('start_page_csrf',secrets.token_hex(32))
    data['start_page']=models.get_user_by_id(g.user['id']).get('start_page') or 'dashboard'

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
    prefs=models.get_user_by_id(g.user['id']).get('preferences_data',{})
    cal_default='1' if prefs.get('calendar_mine',True) else '0'
    cal_mine = request.args.get("cal_mine", cal_default) != "0"
    cal_counselor = g.user.get("display_name") if cal_mine else None
    data.update(_dashboard_calendar_context(g.user["id"], cal_year, cal_month, cal_counselor))
    return render_template("dashboard.html", **data)


@app.route("/healthz")
def healthz():
    return {"ok": True}


@app.route("/help")
@login_required
def help_manual():
    """상담 표준 지침 + 현재 CRM 메뉴별 작성·등록·관리 매뉴얼."""
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
    """모병원 전체의 상담의뢰·입원완료 성과를 상담의뢰 순으로 표시."""
    preset, date_from, date_to = _stats_period_from_request()
    q = (request.args.get("q") or "").strip()
    sort = request.args.get("sort") or "referrals"
    data=models.hospital_referral_overview(date_from,date_to,q=q or None)
    # 기관연계 순은 협력 성과, 상담·입원 순은 유입 규모를 본다.
    keys={"referrals":lambda h:(-h["referrals"],-h["admissions"]),
          "admissions":lambda h:(-h["admissions"],-h["referrals"]),
          "linked":lambda h:(-h["linked_admissions"],-h["linked_referrals"],-h["admissions"])}
    data["hospitals"]=sorted(data["hospitals"],key=lambda h:(*keys.get(sort,keys["referrals"])(h),h["name"]))
    return render_template(
        "stats_hospitals.html", preset=preset, date_from=date_from, date_to=date_to,
        q=q, sort=sort, data=data,
    )


@app.route("/stats/staff")
@login_required
def staff_referral_view():
    """직원소개를 소개자별로 집계. 전환율이 가장 높은 유입 경로라 따로 관리한다."""
    preset, date_from, date_to = _stats_period_from_request()
    q = (request.args.get("q") or "").strip()
    sort = request.args.get("sort") or "admissions"
    data = models.staff_referral_overview(date_from, date_to, q=q or None)
    keys = {"admissions": lambda r: (-r["admissions"], -r["referrals"]),
            "referrals": lambda r: (-r["referrals"], -r["admissions"]),
            "conversion": lambda r: (-r["conversion"], -r["admissions"])}
    data["referrers"] = sorted(data["referrers"],
                               key=lambda r: (*keys.get(sort, keys["admissions"])(r), r["name"]))
    return render_template(
        "stats_staff.html", preset=preset, date_from=date_from, date_to=date_to,
        q=q, sort=sort, data=data,
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
        return jsonify({"insight": {
            "headline": "", "overview": "이번 달 상담 기록이 없어 인사이트를 생성할 수 없습니다.",
            "trend_comment": "", "channel_comment": "", "portfolio_comment": "",
            "pipeline_comment": "", "operation_comment": "", "alerts": [],
        }})
    try:
        from llm import summarize_monthly
        insight = summarize_monthly(data)
    except Exception as e:
        logger.warning(f"월간 인사이트 실패: {e}")
        return jsonify({"error": f"인사이트 생성 실패: {e}"}), 502
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="report_insight", target_type="monthly_report",
        detail=f"{year}-{month:02d}", ip=request.remote_addr,
    )
    return jsonify({"insight": insight})


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
                           admission_episodes=models.list_admission_episodes(c["patient_id"]),
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

@app.route("/api/consult-drafts", methods=["GET", "POST"])
@login_required
def api_consult_drafts():
    """상담사 개인별 새 상담 임시저장 목록과 저장."""
    if request.method == "GET":
        rows = models.list_consultation_drafts(g.user["id"])
        for row in rows:
            try:
                row["payload"] = json.loads(row.pop("payload_json") or "[]")
            except (TypeError, ValueError):
                row["payload"] = []
        return jsonify({"drafts": rows})
    data = request.get_json(silent=True) or {}
    fields = data.get("fields")
    if not isinstance(fields, list) or len(fields) > 500:
        return jsonify({"error": "임시저장할 상담 내용이 올바르지 않습니다."}), 400
    draft_id = data.get("id")
    try:
        draft_id = int(draft_id) if draft_id else None
    except (TypeError, ValueError):
        draft_id = None
    new_id = models.save_consultation_draft(
        g.user["id"], fields, draft_id=draft_id,
        title=(data.get("title") or "")[:100],
        patient_name=(data.get("patient_name") or "")[:100],
        guardian_phone=(data.get("guardian_phone") or "")[:50],
    )
    return jsonify({"ok": True, "id": new_id, "saved_at": datetime.now().isoformat()})


@app.route("/api/consult-drafts/<int:draft_id>", methods=["DELETE"])
@login_required
def api_consult_draft_delete(draft_id):
    if not models.delete_consultation_draft(g.user["id"], draft_id):
        return jsonify({"error": "임시저장본을 찾을 수 없습니다."}), 404
    return jsonify({"ok": True})

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


# AI 자동채움 화이트리스트 — 사전연명의료(ARRANGE)·유입경로 세부(그룹 평탄화)
_AI_ARRANGE_OPTS = ["DNR(agree)", "DNR(consult)", "hopeless 확인"]
_AI_REFERRAL_FLAT = [v for _grp in REFERRAL_SOURCE_GROUPS.values() for v in _grp]


def _resolve_facility_master(name, *, nursing=False):
    """AI가 들은 병원/기관명을 마스터와 대조.
    반환 (matched_official|None, candidates[list]). 자유텍스트 저장 방지 —
    정확히 일치할 때만 정식명 확정, 아니면 후보만 제안(상담사가 선택).
    """
    name = (name or "").strip()
    if not name:
        return None, []
    lookup = models.autocomplete_nursing_homes if nursing else models.autocomplete_hospitals
    try:
        items = (lookup(name, limit=5) or {}).get("items", [])
    except Exception:
        items = []
    if not items:
        return None, []
    key = models._hospital_search_key(name)
    exact = [it["name"] for it in items if models._hospital_search_key(it["name"]) == key]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, exact[:3]
    return None, [it["name"] for it in items[:3]]


@app.route("/api/consult/ai-fill", methods=["POST"])
@login_required
def api_consult_ai_fill():
    """통화 메모 → 상담일지 필드 초안. Claude가 뽑은 값을 config로 검증·화이트리스트해
    폼 필드명(dotted)으로 돌려준다. 자동 저장 없음 — 상담사가 검토·수정 후 저장.
    개인정보(메모 원문)는 감사로그에 남기지 않는다."""
    payload = request.get_json(silent=True) or {}
    memo = (payload.get("memo") or "").strip()
    if not memo:
        return jsonify({"error": "통화 메모를 입력하세요."}), 400
    if len(memo) > 6000:
        memo = memo[:6000]
    if not os.getenv("ANTHROPIC_API_KEY"):
        return jsonify({"error": "AI 기능 미설정 (ANTHROPIC_API_KEY 없음)"}), 503
    try:
        import llm
        raw = llm.extract_consultation(memo, enums={
            "insurance": INSURANCE_TYPES, "channel": CONSULT_CHANNELS,
            "doctor": ATTENDING_DOCTORS, "result": CONSULT_RESULTS,
            "sido": SIDO_LIST, "diseases": DISEASES_CHECKLIST,
            "consciousness": CONSCIOUSNESS_MAIN_OPTIONS,
            "conversation": CONVERSATION_LEVEL_OPTIONS,
            "activity_others": ACTIVITY_OTHERS_OPTIONS,
            "hearing": HEARING_OPTIONS,
            "mobility_active": ACTIVITY_ACTIVE_OPTIONS,
            "diet": DIET_TYPES, "wound_care": WOUND_CARE_OPTIONS,
            "special_care": SPECIAL_CARE_OPTIONS, "therapy": THERAPY_OPTIONS,
            "arrange": _AI_ARRANGE_OPTS, "transport": TRANSPORT_OPTIONS,
            "referral_detail": _AI_REFERRAL_FLAT,
        })
    except Exception as e:
        logger.warning("AI 상담 추출 실패: %s", e)
        return jsonify({"error": "AI 처리에 실패했습니다. 잠시 후 다시 시도하세요."}), 502

    fields, labels = {}, {}

    def put(name, value, label):
        fields[name] = value
        labels[name] = label

    def text_of(key, cap=500):
        v = raw.get(key)
        return (str(v).strip()[:cap]) if v not in (None, "") else None

    # ── 환자 기본 ──
    if (v := text_of("name", 40)):
        put("patient.name", v, "환자명")
    g_ = (raw.get("gender") or "").strip()
    if g_ in ("남", "M", "남자"):
        put("patient.gender", "M", "성별")
    elif g_ in ("여", "F", "여자"):
        put("patient.gender", "F", "성별")
    if (v := text_of("guardian_name", 40)):
        put("patient.guardian_name", v, "보호자명")
    if (v := text_of("guardian_relation", 20)):
        put("patient.guardian_relation", v, "보호자 관계")
    if raw.get("guardian_phone"):
        put("patient.guardian_phone", _norm_phone(str(raw["guardian_phone"])), "연락처")
    if (v := raw.get("residence_sido")) and v in SIDO_LIST:
        put("patient.residence_sido", v, "거주 시/도")
        if (sg := text_of("residence_sigungu", 30)):
            put("patient.residence_sigungu", sg, "거주 시/군/구")
    if (v := raw.get("insurance_type")) and v in INSURANCE_TYPES:
        put("patient.insurance_type", v, "보험유형")

    # ── 상담 ──
    age = raw.get("age")
    try:
        if age is not None and 0 <= int(age) <= 120:
            put("consultation.patient_age", int(age), "나이")
    except (ValueError, TypeError):
        pass
    if (v := raw.get("consult_channel")) and v in CONSULT_CHANNELS:
        put("consultation.consult_channel", v, "상담방법")
    if (v := raw.get("attending_doctor")) and v in ATTENDING_DOCTORS:
        put("consultation.attending_doctor", v, "주치의")
    if (v := text_of("disease_onset", 60)):
        put("consultation.disease_onset", v, "발병일")
    if (v := raw.get("planned_admission_date")) and _valid_date(str(v)[:10], None):
        put("consultation.planned_admission_date", str(v)[:10], "입원예정일")
    if (v := text_of("diagnosis", 800)):
        put("consultation.disease_detail", v, "병명·진단 상세")
    if (v := text_of("admission_purpose", 500)):
        put("consultation.admission_purpose", v, "입원 목적")
    if (v := raw.get("consult_result")) and v in CONSULT_RESULTS:
        put("consultation.consult_result", v, "상담 결과")

    # ── 병명 체크리스트 (배열, 화이트리스트) ──
    dz = raw.get("diseases")
    if isinstance(dz, list):
        picked_dz = [x for x in dz if x in DISEASES_CHECKLIST]
        if picked_dz:
            put("consultation.diseases[]", picked_dz, "병명")

    # ── 환자 상태 (라디오·체크박스, 화이트리스트) ──
    if (v := raw.get("consciousness")) and v in CONSCIOUSNESS_MAIN_OPTIONS:
        put("consultation.consciousness_main", v, "의식")
    if (v := raw.get("conversation")) and v in CONVERSATION_LEVEL_OPTIONS:
        put("consultation.conversation_level", v, "대화")
    if (v := raw.get("diaper")) and v in ACTIVITY_DIAPER_OPTIONS:
        put("consultation.activity_diaper", v, "기저귀")
    if (v := raw.get("wheelchair")) and v in ACTIVITY_WHEELCHAIR_OPTIONS:
        put("consultation.activity_wheelchair", v, "휠체어")
    ao = raw.get("activity_others")
    if isinstance(ao, list):
        picked_ao = [x for x in ao if x in ACTIVITY_OTHERS_OPTIONS]
        if picked_ao:
            put("consultation.activity_others[]", picked_ao, "기타 활동")

    # ── 추가 상태·처치·유입경로 (배열/단일, 화이트리스트) ──
    def put_list(key, name, allowed, label):
        vals = raw.get(key)
        if isinstance(vals, list):
            picked = [x for x in vals if x in allowed]
            if picked:
                put(name, picked, label)

    def put_one(key, name, allowed, label):
        v = raw.get(key)
        if v in allowed:
            put(name, v, label)

    put_list("hearing", "consultation.hearing_options[]", HEARING_OPTIONS, "청력")
    put_list("mobility_active", "consultation.activity_active[]", ACTIVITY_ACTIVE_OPTIONS, "능동 이동")
    put_one("caregiver", "consultation.caregiver_status", CAREGIVER_OPTIONS, "간병")
    put_one("bed", "consultation.bed_type", BED_OPTIONS, "침상")
    put_list("diet", "consultation.diet_types[]", DIET_TYPES, "식이")
    put_one("swallow_test", "consultation.swallow_test", ("유", "무"), "연하검사")
    put_list("wound_care", "consultation.wound_care[]", WOUND_CARE_OPTIONS, "창상 처치")
    put_list("special_care", "consultation.special_care[]", SPECIAL_CARE_OPTIONS, "특수 처치")
    put_list("therapy", "consultation.therapy[]", THERAPY_OPTIONS, "재활치료")
    put_list("arrange", "consultation.arrange_items[]", _AI_ARRANGE_OPTS, "기타 확인")
    put_list("referral_detail", "consultation.referral_source_detail[]", _AI_REFERRAL_FLAT, "유입경로")
    if (v := raw.get("transport")) and v in TRANSPORT_OPTIONS:
        put("consultation.transport_method[]", [v], "교통수단")
    if (v := text_of("referrer_person", 40)):
        put("consultation.referrer_person", v, "소개자")
    if (v := text_of("cancer_site", 60)):
        put("consultation.cancer_site", v, "암 부위")

    # ── 모병원·추천기관: 마스터 대조 (자유텍스트 저장 금지 — 정식명만) ──
    suggestions = []

    def resolve_into(raw_name, field, label, *, nursing=False, type_field=None, type_value=None):
        raw_name = (raw_name or "").strip()
        if not raw_name:
            return
        official, cands = _resolve_facility_master(raw_name, nursing=nursing)
        if official:                       # 정확 매칭 → 정식명으로 확정
            put(field, official, label)
            if type_field and type_value:
                put(type_field, type_value, "현재 위치")
        else:                              # 후보 제안(또는 미등록) — 자동 저장 안 함
            suggestions.append({
                "label": label, "raw": raw_name, "field": field,
                "candidates": cands,
                "type_field": type_field, "type_value": type_value,
            })

    fac_type = (raw.get("current_facility_type") or "").strip()
    if (v := text_of("current_hospital", 60)):
        if fac_type == "입소중":
            resolve_into(v, "consultation.current_nursing_name", "현재 요양원",
                         nursing=True, type_field="consultation.current_location_type",
                         type_value="입소중")
        elif fac_type in ("입원중", ""):
            resolve_into(v, "consultation.current_location_name", "현재 병원(모병원)",
                         type_field="consultation.current_location_type",
                         type_value="입원중")
    if (v := text_of("referrer_institution", 60)):
        resolve_into(v, "consultation.referrer_institution", "추천기관")

    summary = text_of("summary", 1000) or ""
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="ai_fill_consult", target_type="consultation",
        detail=f"{len(fields)}개 필드", ip=request.remote_addr,
    )
    return jsonify({"ok": True, "fields": fields, "labels": labels,
                    "summary": summary, "suggestions": suggestions})


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
        # 재원 명단은 원무 명부 회차로 세므로, 이 상담에 붙은 명부 회차도 닫아야
        # 화면에서 빠진다. 상담에만 퇴원일을 적으면 '반영이 안 되는' 것처럼 보였다.
        ep = models.current_admission_census()["by_consultation"].get(cid)
        if ep:
            models.close_roster_episode(ep["id"], discharged_at=fields["discharge_date"],
                                        destination=fields["discharge_destination"] or None,
                                        reason=fields["discharge_reason"] or None)
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="update_discharge", target_type="consultation", target_id=cid,
        detail=action, ip=request.remote_addr,
    )
    return jsonify({"ok": True, **fields})


# ──── 대시보드 follow-up 토글 (회복기 전환 보호자 연락 / 퇴원 1차 면담) ────

def _toggle_follow_up(cid, field, audit_action, by_field=None):
    """consultations[field](DATETIME)를 토글 — 비어있으면 현재 시각, 있으면 NULL."""
    existing = models.get_consultation(cid)
    if not existing:
        return jsonify({"error": "not found"}), 404
    cur = existing.get(field)
    new_val = None if cur else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updates = {field: new_val}
    if by_field:
        updates[by_field] = (g.user.get("display_name") or g.user.get("username")) if new_val else None
    models.update_consultation_meta(cid, **updates)
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action=audit_action, target_type="consultation", target_id=cid,
        detail="set" if new_val else "unset", ip=request.remote_addr,
    )
    return jsonify({"ok": True, field: new_val})


@app.route("/api/consult/<int:cid>/recovery-call", methods=["POST"])
@login_required
def api_consult_recovery_call(cid):
    """회복기→비회복기 전환 D-30 환자의 보호자 재상담 완료 마킹 토글."""
    return _toggle_follow_up(cid, "recovery_call_at", "recovery_call", "recovery_call_by")


@app.route("/api/consult/<int:cid>/discharge-sms", methods=["POST"])
@login_required
def api_consult_discharge_sms(cid):
    """퇴원예정 D-30 환자의 보호자 퇴원 안내 문자 발송 확인 토글."""
    return _toggle_follow_up(cid, "discharge_sms_at", "discharge_sms", "discharge_sms_by")


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
            # 표기 변형('대구 굿모닝병원'·'대구굿모닝')을 모두 포함 — 순위표 건수와 목록이 어긋나지 않게.
            variants = models.hospital_name_variants(hospital)
            conn = models.get_db()
            placeholders = ",".join("?" * len(all_rows))
            marks = ",".join("?" * len(variants))
            rows = conn.execute(
                f"SELECT DISTINCT patient_id FROM consultations "
                f"WHERE patient_id IN ({placeholders}) AND TRIM(source_hospital) IN ({marks})",
                [p["id"] for p in all_rows] + variants,
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


def _away_insights(rows, top=6):
    """외진·전원 기록의 특성 — 무엇 때문에, 어디로, 얼마나 나갔다가 돌아오는가.

    기준은 KPI와 같은 '기간·유형' 필터 결과(rows)다. 복귀 소요일은 나간 날을
    1일째로 세고(외진 일수와 같은 셈법), 타 병원 전원으로 끝난 기록은 복귀가
    아니므로 소요일 평균에서 뺀다. 사유는 자유 입력이라 '|' '/' '(' 앞 첫 구절만
    떼어 묶는다 — '폐렴 | 명부: …' 식으로 뒤에 출처 메모가 붙는 경우가 많다.
    """
    if not rows:
        return None

    def days_out(r):
        if not r.get("returned_at") or models.is_away_transferred(r):
            return None
        try:
            d = (date.fromisoformat(str(r["returned_at"])[:10])
                 - date.fromisoformat(str(r.get("event_date"))[:10])).days
        except (TypeError, ValueError):
            return None
        return d + 1 if d >= 0 else None

    def reason_of(r):
        memo = (r.get("memo") or "").strip()
        head = re.split(r"[|/(（\n]", memo, 1)[0].strip(" ·-–,.")
        return head[:20] or "사유 미기재"

    def group(rows, key):
        buckets = {}
        for r in rows:
            k = key(r)
            if not k:
                continue
            b = buckets.setdefault(k, {"label": k, "events": 0, "patients": set(),
                                        "returned": 0, "transferred": 0, "days": []})
            b["events"] += 1
            b["patients"].add(r["patient_id"])
            if models.is_away_transferred(r):
                b["transferred"] += 1
            elif r.get("returned_at"):
                b["returned"] += 1
            d = days_out(r)
            if d is not None:
                b["days"].append(d)
        out = []
        for b in buckets.values():
            closed = b["returned"] + b["transferred"]
            out.append({
                "label": b["label"], "events": b["events"], "patients": len(b["patients"]),
                "returned": b["returned"], "transferred": b["transferred"],
                "open": b["events"] - closed,
                "return_rate": round(b["returned"] * 100 / b["events"], 1) if b["events"] else 0,
                "avg_days": round(sum(b["days"]) / len(b["days"]), 1) if b["days"] else None,
            })
        out.sort(key=lambda b: (-b["events"], b["label"]))
        return out

    for r in rows:
        r.update(_split_diagnosis(r))
    all_days = sorted(d for d in (days_out(r) for r in rows) if d is not None)
    open_rows = [r for r in rows if not r.get("returned_at")]
    open_days = []
    for r in open_rows:
        try:
            open_days.append((date.today() - date.fromisoformat(str(r.get("event_date"))[:10])).days + 1)
        except (TypeError, ValueError):
            pass
    dist_bins = (("1~3일", 1, 3), ("4~7일", 4, 7), ("8~14일", 8, 14), ("15일 이상", 15, 10 ** 6))
    distribution = [{"label": lb, "count": sum(1 for d in all_days if lo <= d <= hi)}
                    for lb, lo, hi in dist_bins]
    per_patient = {}
    for r in rows:
        per_patient.setdefault(r["patient_id"], []).append(r)
    repeaters = sorted(
        [{"patient_id": pid, "name": items[0].get("patient_name"), "id": items[0].get("id"),
          "events": len(items),
          "hospitals": sorted({(i.get("hospital") or "").strip() for i in items if (i.get("hospital") or "").strip()})}
         for pid, items in per_patient.items() if len(items) >= 2],
        key=lambda x: (-x["events"], x["name"] or ""))

    def age_band(r):
        age = r.get("patient_age")
        if age is None:
            return None
        return f"{min(int(age) // 10 * 10, 90)}대"

    return {
        "events": len(rows), "patients": len(per_patient),
        "avg_days": round(sum(all_days) / len(all_days), 1) if all_days else None,
        "median_days": all_days[len(all_days) // 2] if all_days else None,
        "max_days": all_days[-1] if all_days else None,
        "returned_n": len(all_days),
        "distribution": distribution,
        "open_n": len(open_rows), "open_long": sum(1 for d in open_days if d >= 7),
        "open_avg_days": round(sum(open_days) / len(open_days), 1) if open_days else None,
        "by_type": group(rows, lambda r: r.get("event_type")),
        "by_reason": group(rows, reason_of)[:top],
        "by_dx": group(rows, lambda r: (r.get("dx_primary") or [None])[0])[:top],
        "by_hospital": group(rows, lambda r: (r.get("hospital") or "").strip() or None)[:top],
        "by_age": sorted(group(rows, age_band), key=lambda b: b["label"]),
        "repeaters": repeaters[:top], "repeater_n": len(repeaters),
    }


def _ward_away_report():
    filters = {key: (request.args.get(key) or "").strip()
               for key in ("away_from", "away_to", "away_type", "away_status", "away_q",
                           "away_gender", "away_age_min", "away_age_max", "away_hospital",
                           "away_dx", "away_number", "away_days_min", "away_dday_max")}
    for key in ("away_from", "away_to"):
        if filters[key]:
            try:
                filters[key] = date.fromisoformat(filters[key]).isoformat()
            except ValueError:
                abort(400, description="조회 기간은 YYYY-MM-DD 형식으로 입력하세요.")
    if filters["away_from"] and filters["away_to"] and filters["away_from"] > filters["away_to"]:
        abort(400, description="조회 시작일이 종료일보다 늦습니다.")
    if filters["away_type"] not in ("", *models.AWAY_EVENT_TYPES):
        abort(400)
    if filters["away_status"] not in ("", "open", "returned", "transferred"):
        abort(400)
    if filters["away_gender"] not in ("", "M", "F", "U"):
        abort(400)
    numbers = {}
    for key in ("away_age_min", "away_age_max", "away_number", "away_days_min", "away_dday_max"):
        try:
            numbers[key] = int(filters[key]) if filters[key] else None
        except ValueError:
            abort(400, description="나이·차수·기간은 정수로 입력하세요.")
        if key != "away_dday_max" and numbers[key] is not None and numbers[key] < (1 if key == "away_number" else 0):
            abort(400, description="나이·기간은 0 이상, 외진 차수는 1 이상으로 입력하세요.")
    if numbers["away_age_min"] is not None and numbers["away_age_max"] is not None and numbers["away_age_min"] > numbers["away_age_max"]:
        abort(400, description="최소 나이가 최대 나이보다 큽니다.")

    def inclusive_days(start, end):
        try:
            days = (date.fromisoformat(str(end)[:10]) - date.fromisoformat(str(start)[:10])).days
            return days + 1 if days >= 0 else None
        except ValueError:
            return None

    rows = models.list_away_records(date_from=filters["away_from"], date_to=filters["away_to"],
                                   event_type=filters["away_type"])
    # 검색/복귀 상태로 분모가 달라지지 않도록 기간·유형 기준 통계를 먼저 계산한다.
    stats = models.away_record_stats(rows)
    transfers = models.away_record_stats([r for r in rows if r["event_type"] == "응급전원"])
    monthly = {}
    for row in rows:
        month = (row.get("event_date") or "")[:7] or "일자 미기재"
        monthly.setdefault(month, []).append(row)
    monthly = [(month, models.away_record_stats(items))
               for month, items in sorted(monthly.items(), reverse=True)]
    selected = []
    for row in rows:
        if filters["away_status"] == "open" and row.get("returned_at"):
            continue
        if filters["away_status"] == "returned" and (not row.get("returned_at") or models.is_away_transferred(row)):
            continue
        if filters["away_status"] == "transferred" and not models.is_away_transferred(row):
            continue
        row["transferred"] = models.is_away_transferred(row)
        row.update(_split_diagnosis(row))
        search = " ".join(str(row.get(k) or "") for k in
                          ("patient_name", "hospital", "memo", "primary_diagnosis", "secondary_diagnosis", "diseases"))
        if filters["away_q"].casefold() not in search.casefold():
            continue
        # 상담에 입원일이 없으면 원무 명부 회차의 입·퇴원일로 채운다 — 명부로만
        # 적재된 환자는 상담 쪽 날짜가 비어 '재원일수 미확인'으로 나왔다.
        row["admitted_on"] = (row.get("actual_admission_date") or row.get("roster_admitted_at")
                              or row.get("admission_date") or None)
        row["discharge_date"] = row.get("discharge_date") or row.get("roster_discharged_at") or None
        row["discharge_watch"] = _discharge_watch(row)
        row["stay_days_inclusive"] = inclusive_days(
            row["admitted_on"], row.get("discharge_date") or date.today().isoformat())
        row["away_days_inclusive"] = inclusive_days(
            row.get("event_date"), row.get("returned_at") or date.today().isoformat())
        if filters["away_gender"] and (row.get("gender") or "U") != filters["away_gender"]:
            continue
        if any(filters[key].casefold() not in str(value or "").casefold() for key, value in (
            ("away_hospital", row.get("hospital")),
            ("away_dx", " ".join(row["dx_primary"] + row["dx_secondary"])))):
            continue
        age = row.get("patient_age")
        if numbers["away_age_min"] is not None and (age is None or age < numbers["away_age_min"]):
            continue
        if numbers["away_age_max"] is not None and (age is None or age > numbers["away_age_max"]):
            continue
        if numbers["away_number"] is not None and row.get("away_number") != numbers["away_number"]:
            continue
        if numbers["away_days_min"] is not None and (row["away_days_inclusive"] is None or row["away_days_inclusive"] < numbers["away_days_min"]):
            continue
        if numbers["away_dday_max"] is not None:
            watch = row["discharge_watch"]
            if row.get("discharge_date") or not watch or watch["days_left"] > numbers["away_dday_max"]:
                continue
        selected.append(row)
    for no, row in enumerate(selected, 1):
        row["no"] = no
    view = request.args.get("away_view") or "list"
    if view not in ("list", "feed"):
        view = "list"
    # 상세필터 값이 하나라도 있으면 화면에서 펼친 채로 연다.
    detail_active = any(filters[k] for k in ("away_gender", "away_age_min", "away_age_max", "away_hospital",
                                             "away_dx", "away_number", "away_days_min", "away_dday_max"))
    return dict(rows=selected, stats=stats, transfers=transfers, monthly=monthly, filters=filters,
                view=view, detail_active=detail_active, insights=_away_insights(rows))


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
    sort_dir = (request.args.get("dir") or "").lower()
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc" if sort in ("admission", "stay") else "asc"
    show_old = request.args.get("old") in ("1", "true", "yes")
    # KPI 카드별 세부 내역 필터 — 재원 목록을 해당 항목으로 좁혀 본다.
    filt = (request.args.get("filt") or "").strip() or None
    ward_f = (request.args.get("ward") or "").strip() or None      # 병동 빠른 조회
    tag_f = (request.args.get("tag") or "").strip() or None        # 관리 태그
    organism_f = (request.args.get("organism") or "").strip() or None  # 내성균 보유
    room_f = (request.args.get("room") or "").strip()
    gender_f = (request.args.get("gender") or "").strip()
    dx_f = (request.args.get("dx") or "").strip().lower()
    admission_from = (request.args.get("admission_from") or "").strip()
    admission_to = (request.args.get("admission_to") or "").strip()
    discharge_from = (request.args.get("discharge_from") or "").strip()
    discharge_to = (request.args.get("discharge_to") or "").strip()
    def _optional_int(name):
        try:
            raw = (request.args.get(name) or "").strip()
            return int(raw) if raw else None
        except ValueError:
            return None
    age_min, age_max = _optional_int("age_min"), _optional_int("age_max")
    stay_min, stay_max = _optional_int("stay_min"), _optional_int("stay_max")
    sido_f = (request.args.get("sido") or "").strip()
    stay_period = (request.args.get("stay_period") or "").strip()
    if stay_period not in _WARD_STAY_PERIODS:
        stay_period = ""
    ward_prefs=models.get_user_by_id(g.user['id']).get('preferences_data',{})
    subtab = (request.args.get("tab") or ward_prefs.get('ward_tab') or "status").strip()
    if subtab not in ("status", "away", "waiting", "trend", "blacklist", "quality"):
        subtab = "status"
    if subtab == "quality" and g.user.get("role") != "admin":
        abort(403)

    # 통합검색에서 파생 분류명도 바로 이해한다. DB에 그대로 저장되지 않는
    # D-30·연장·병동 분류는 기존 필터로 변환하고 검색어 표시는 유지한다.
    db_q = q
    q_compact = (q or "").replace(" ", "").lower()
    if q_compact:
        if "비회복기" in q_compact:
            filt, db_q = "nonrecovery", None
        elif "회복기종료" in q_compact or "회복기d-30" in q_compact:
            filt, db_q = "recdue", None
        elif "퇴원예정" in q_compact or "퇴원d-30" in q_compact:
            filt, db_q = "dis30", None
        elif q_compact in ("회복기", "s005"):
            filt, db_q = "recovery", None
        elif "연장1" in q_compact:
            filt, db_q = "ext1", None
        elif "연장2" in q_compact:
            filt, db_q = "ext2", None
        elif "균환자" in q_compact or q_compact in ("균", "내성균"):
            organism_f, db_q = "1", None
        else:
            for ward_name in WARDS:
                if ward_name.replace(" ", "").lower() == q_compact:
                    ward_f, db_q = ward_name, None
                    break

    # ── 재원 판정은 원무 명부(입원 회차)가 기준이다 ──
    # 상담의 입원완료·퇴원일로 세면 맞지 않는다. 퇴원일이 한 건도 채워지지
    # 않아 2024년 입원 환자가 아직 재원으로 잡히고, 올해 입원한 환자는 한 명도
    # 안 잡혔다. 회차 테이블은 원무 명부를 그대로 받은 것이라 사실과 같다.
    census = models.current_admission_census()
    rows = models.list_consultations(q=db_q, q_scope="ward", limit=10000)
    pending_pool = [c for c in rows
                    if c.get("admission_status") == "입원완료"
                    and not (c.get("discharge_date") or "").strip()
                    and not (c.get("actual_admission_date") or c.get("admission_date") or "").strip()
                    and c.get("patient_id") not in census["patients"]]
    if not census["has_roster"]:
        # 명부를 아직 안 올린 설치. 회차가 통째로 비어 있으면 재원 명단도 비므로
        # 옛 방식(상담의 입원완료·미퇴원)으로 돌아간다.
        rows = [c for c in rows
                if c.get("admission_status") == "입원완료"
                and not (c.get("discharge_date") or "").strip()]
        pending_pool = [c for c in rows
                        if not (c.get("actual_admission_date") or c.get("admission_date") or "").strip()]
        rows = [c for c in rows
                if (c.get("actual_admission_date") or c.get("admission_date") or "").strip()]
    else:
        rows = [c for c in rows if c["id"] in census["by_consultation"]]
    for c in rows:
        # 입원 사실은 명부 값으로 덮는다 — 상담에 적힌 입원일·병실은 상담 시점의
        # 예정값이라 실제와 어긋난다.
        ep = census["by_consultation"].get(c["id"])
        if not ep:
            continue
        c["episode_id"] = ep["id"]
        c["actual_admission_date"] = ep["admitted_at"]
        c["discharge_date"] = None
        if ep.get("room_number"):
            c["room_number"] = ep["room_number"]
        c["roster_ward"] = ep.get("ward")
        # 주치의는 상담 시점에 정해지지 않는 일이 많아 상담일지에는 대개 비어 있다.
        # 명부는 입원 건마다 실제 담당 의사를 들고 있으므로 그쪽을 쓴다.
        if (ep.get("attending_doctor") or "").strip():
            c["attending_doctor"] = ep["attending_doctor"].strip()
        c["roster_care_phase"] = _roster_care_phase(ep.get("care_type"))
        c["rehab_end_date"] = ep.get("rehab_end_date")
        c["rehab_end_imported"] = ep.get("rehab_end_imported")
    # 상담 없이 입원한 환자 — 명부에만 있다. 인원에서 빠지면 재원 수가 틀리므로
    # 회차가 들고 있는 값만으로 행을 만든다. 상담 id가 없어 화면에서 상담 상세와
    # 외진·퇴원 버튼은 뜨지 않는다(그 환자는 상담일지 자체가 없다).
    rows += [_ward_row_from_episode(ep) for ep in census["orphans"]
             if _orphan_matches(ep, db_q)]
    if doctor:
        rows = [c for c in rows if (c.get("attending_doctor") or "") == doctor]

    bed_waiting = models.list_consultations(admission_status="입원대기", limit=10000)
    for c in bed_waiting:
        c["wait_days"] = _days_since(c.get("wait_started_at") or c.get("consult_date"))
        c["contact_overdue"] = bool(c.get("wait_next_contact_date") and
                                    c["wait_next_contact_date"] < date.today().isoformat())
        c.update(_split_diagnosis(c))
    priority_order = {"긴급": 0, "우선": 1, "일반": 2}
    bed_waiting.sort(key=lambda c: (priority_order.get(c.get("wait_priority") or "일반", 2),
                                    -(c.get("wait_days") or 0), c.get("patient_name") or ""))

    away_records = models.away_now()
    away_by_pid = {a["pid"]: a for a in away_records}
    # 입원일 미확정(pending)은 상담 기준 그대로 둔다 — 데이터 점검 목록이다.
    admitted, pending = [], list(pending_pool)
    for c in rows:
        adm = (c.get("actual_admission_date") or c.get("admission_date") or "").strip()
        c["admitted_on"] = adm or None
        if not adm:
            continue
        c["away"] = _ward_current_away(c, away_by_pid)
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
    hist = models.away_history([c["id"] for c in admitted if c["id"]])
    for c in admitted:
        c["away_hist"] = hist.get(c["id"]) if c["id"] else None

    # 전원으로 재원 명부에서 빠져도 미복귀 외진은 별도로 계속 표시한다.
    away = _ward_away_panel(away_records, doctor=doctor)
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
        "stay": lambda c: (c.get("stay_days") or 0, c.get("patient_name") or ""),
        "name": lambda c: c.get("patient_name") or "",
        "admission": lambda c: (c.get("admitted_on") or "", c.get("patient_name") or ""),
        "discharge": lambda c: (c.get("discharge_due") or ("" if sort_dir == "desc" else "9999-12-31"),
                                 c.get("patient_name") or ""),
    }
    admitted.sort(key=sorters.get(sort, sorters["dday"]), reverse=(sort_dir == "desc"))

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
    bed_capacity = 355
    kpis = {
        "admitted": total_n,
        "recovery": recovery_n,
        "nonrecovery": nonrecovery_n,
        "recovery_ratio": recovery_ratio,
        "recovery_ratio_ok": recovery_ratio >= 40,
        "nonrecovery_ratio": round(nonrecovery_n / total_n * 100) if total_n else 0,
        "bed_capacity": bed_capacity,
        "bed_occupancy": round(total_n / bed_capacity * 100, 1),
        "away": len(away),
        "away_overdue": sum(1 for c in away if c["away"].get("overdue")),
        "recovery_due": sum(1 for c in admitted if c.get("recovery_due")),
        "recovery_due_unchecked": sum(1 for c in admitted
                                       if c.get("recovery_due") and not c.get("recovery_call_at")),
        "discharge_soon": sum(1 for c in admitted
                              if c.get("discharge_dday") is not None
                              and c["discharge_dday"] <= 7),
        "discharge_due_30": sum(1 for c in admitted
                                if c.get("discharge_dday") is not None
                                and c["discharge_dday"] <= 30),
        "discharge_due_unchecked": sum(1 for c in admitted
                                        if c.get("discharge_dday") is not None
                                        and c["discharge_dday"] <= 30
                                        and not c.get("discharge_sms_at")),
        "ext1": sum(1 for c in admitted if c.get("ext_tier") == 1),
        "ext2": sum(1 for c in admitted if c.get("ext_tier") == 2),
        "pending": len(pending),
        "pending_recent": len(pending_recent),
        "unassigned": len(unassigned),
        "bed_waiting": len(bed_waiting),
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
    # 여기도 기준은 원무 명부다 — 상담의 입·퇴원일로 복원하면 퇴원일이 비어 있어
    # census가 날이 갈수록 불어나기만 한다. 회차는 어느 시점을 끊어도 실제와 맞는다.
    trend_rows = {c["id"]: c for c in models.list_consultations(limit=10000)}
    spans = models.admission_spans()
    # 상담이 안 붙은 회차는 진단·발병일을 몰라 회복기 판정을 못 한다. 과거로
    # 갈수록 연결률이 떨어져(전체 73%) 이들을 '비회복기'로 세면 비율이 실제보다
    # 낮게 나온다 — 40% 기준선을 보는 지표라 그 왜곡이 위험하다. 그래서 비율은
    # 판정 가능한 인원만으로 내고, 인원(total)은 실제 재원 수 그대로 보여준다.
    #
    # 회차마다 회복기 구간의 끝을 한 번만 계산해 둔다(rec_end: None=회복기 아님,
    # date.max=끝없음). 날짜별 계산은 비교만 하므로 1년치 일별도 바로 나온다.
    trend_spans = [_trend_span(span, trend_rows.get(span["consultation_id"])) for span in spans]
    def _ratio_at(snapshot):
        snapshot_iso = snapshot.isoformat()
        total = known = recovery_count = 0
        for admitted_iso, discharged_iso, is_known, rec_end in trend_spans:
            if admitted_iso > snapshot_iso or (discharged_iso and discharged_iso <= snapshot_iso):
                continue
            total += 1
            if is_known:
                known += 1
                if rec_end is not None and snapshot <= rec_end:
                    recovery_count += 1
        return {"total": total, "known": known, "recovery": recovery_count,
                "ratio": round(recovery_count * 100 / known, 1) if known else 0}

    # 추이 기간 — 통계 페이지와 같은 preset/from/to 방식. 프리셋(30/90/180/365일)
    # 또는 custom(직접지정). 일별은 선택 기간 그대로, 월별은 기간을 덮는 달을 최소
    # 12개월까지 넓혀 보여준다. 종료일은 오늘을 넘지 못하고, 일별 계산 비용 때문에
    # 최대 2년으로 묶는다.
    today_d = date.today()
    trend_preset = (request.args.get("preset") or "").strip()
    trend_to = date.fromisoformat(_valid_date(request.args.get("to"), today_d.isoformat()))
    trend_to = min(trend_to, today_d)
    trend_from = _valid_date(request.args.get("from"))
    raw_to = _valid_date(request.args.get("to"))
    if trend_preset not in _WARD_TREND_RANGES:
        # custom이거나 preset이 빠졌어도 날짜가 왔으면 그 날짜를 쓴다. 날짜만 바꾸고
        # 라디오가 안 바뀐 채 조회해도 입력한 기간이 무시되지 않게.
        trend_preset = "custom" if (trend_from or raw_to) else "30"
    if trend_preset == "custom":
        trend_from = date.fromisoformat(trend_from) if trend_from else trend_to - timedelta(days=29)
    else:
        # 프리셋 '최근 N일'은 오늘까지 N일. 프리셋 칩은 날짜칸을 비우고 넘어오므로
        # 날짜가 함께 온 경우는 사용자가 날짜를 고친 것 — 그때만 직접지정으로 본다.
        trend_to = today_d
        preset_from = trend_to - timedelta(days=int(trend_preset) - 1)
        if raw_to and date.fromisoformat(raw_to) < today_d:
            trend_preset, trend_to = "custom", date.fromisoformat(raw_to)
            trend_from = date.fromisoformat(trend_from) if trend_from else trend_to - timedelta(days=29)
        elif trend_from and date.fromisoformat(trend_from) != preset_from:
            trend_preset, trend_from = "custom", date.fromisoformat(trend_from)
        else:
            trend_from = preset_from
    if trend_from > trend_to:
        trend_from, trend_to = trend_to, trend_from
    if (trend_to - trend_from).days > 730:
        trend_from = trend_to - timedelta(days=730)
    daily_ratio_trend, monthly_ratio_trend, trend_insight, trend_summary = [], [], None, None
    if subtab == "trend":
        snapshot = trend_from
        while snapshot <= trend_to:
            daily_ratio_trend.append({"label": snapshot.strftime("%m.%d"),
                                      "date": snapshot.isoformat(), **_ratio_at(snapshot)})
            snapshot += timedelta(days=1)
        end_index = trend_to.year * 12 + trend_to.month - 1
        start_index = trend_from.year * 12 + trend_from.month - 1
        month_count = max(12, end_index - start_index + 1)
        for offset in range(month_count - 1, -1, -1):
            year, month0 = divmod(end_index - offset, 12)
            month = month0 + 1
            snapshot = min(date(year, month, calendar.monthrange(year, month)[1]), trend_to)
            monthly_ratio_trend.append({"label": f"{str(year)[2:]}.{month:02d}",
                                        "date": snapshot.isoformat(), **_ratio_at(snapshot)})
        trend_insight = _ratio_insight(_ratio_at(today_d), admitted, _ratio_at, today_d)
        trend_summary = _trend_summary(daily_ratio_trend, monthly_ratio_trend)

    # 최근 퇴원도 명부 기준이다 — 상담의 '퇴원완료' 상태로는 한 건도 안 잡힌다.
    # 상담이 붙은 회차는 그 상담의 퇴원 사유·담당자를 함께 싣는다.
    discharged = models.recent_discharges(limit=100)
    con_by_episode = {sp["episode_id"]: trend_rows.get(sp["consultation_id"])
                      for sp in spans if sp["consultation_id"]}
    for d in discharged:
        con = con_by_episode.get(d["episode_id"]) or {}
        d["id"] = con.get("id")
        d["discharge_date"] = d["discharged_at"][:10]
        d["discharge_destination"] = con.get("discharge_destination")
        d["discharge_reason"] = con.get("discharge_reason")
        d["counselor"] = con.get("counselor")
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
    admitted_list = _apply_ward_column_filters(admitted_list)
    if filt == "recdue" and sort == "dday":
        admitted_list.sort(key=lambda c: (bool(c.get("recovery_call_at")), _dday(c)))
    elif filt == "dis30" and sort == "dday":
        admitted_list.sort(key=lambda c: (bool(c.get("discharge_sms_at")), _dday(c)))
    column_filter = bool(room_f or gender_f or dx_f or sido_f or stay_period or admission_from or admission_to
                         or discharge_from or discharge_to or age_min is not None
                         or age_max is not None or stay_min is not None or stay_max is not None)
    view = request.args.get("view") or "room"
    if filt in _WARD_FILTS or tag_f or organism_f or column_filter:
        view = "list"   # 세부 내역을 볼 땐 목록 뷰로
    any_filter = bool(filt in _WARD_FILTS or ward_f or tag_f or organism_f or column_filter)
    # 목록형만 페이지 단위로 표시한다. 병실 배치도는 전체 병상을 한 번에 보여준다.
    try:
        page_size = int(request.args.get("page_size") or 50)
    except ValueError:
        page_size = 50
    if page_size not in (30, 50, 100, 200):
        page_size = 50
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    total_filtered = len(admitted_list)
    total_pages = max(1, (total_filtered + page_size - 1) // page_size)
    page = min(page, total_pages)
    if view == "list":
        admitted_list = admitted_list[(page - 1) * page_size:page * page_size]
    quality_report = models.data_quality_report() if subtab == "quality" else None
    backup_status = backup.latest_status() if subtab == "quality" else None
    blacklisted = models.list_blacklisted_patients()
    away_report = _ward_away_report() if subtab == "away" else None
    return render_template(
        "ward.html", away=away, admitted=admitted_list,
        room_view=room_view, unassigned=unassigned,
        view=view,
        pending=pending_recent, pending_old=pending_old, show_old=show_old,
        kpis=kpis, q=q or "", doctor=doctor or "", sort=sort, sort_dir=sort_dir,
        filt=filt, filt_label=filt_label,
        ward_f=ward_f, tag_f=tag_f, organism_f=organism_f, any_filter=any_filter,
        organism_options=_ORGANISMS,
        WARDS=WARDS, MGMT_TAG_PRESETS=MGMT_TAG_PRESETS, tag_counts=tag_counts,
        doctor_options=doctor_options,
        recovery_due_list=recovery_due_list, discharge_due_list=discharge_due_list,
        daily_ratio_trend=daily_ratio_trend, monthly_ratio_trend=monthly_ratio_trend,
        trend_preset=trend_preset, trend_ranges=_WARD_TREND_RANGES,
        trend_from=trend_from.isoformat(), trend_to=trend_to.isoformat(),
        trend_month_count=len(monthly_ratio_trend), trend_insight=trend_insight,
        trend_summary=trend_summary,
        discharged=discharged,
        subtab=subtab, away_report=away_report, away_candidates=admitted,
        blacklisted=blacklisted,
        bed_waiting=bed_waiting,
        room_f=room_f, gender_f=gender_f, dx_f=dx_f,
        sido_f=sido_f, stay_period=stay_period, stay_periods=_WARD_STAY_PERIODS,
        ward_csv_url=url_for("ward_csv") + ("?" + urlencode(request.args.to_dict()) if request.args else ""),
        dx_options=list(_WARD_DIAGNOSES),
        admission_from=admission_from, admission_to=admission_to,
        discharge_from=discharge_from, discharge_to=discharge_to,
        age_min=age_min, age_max=age_max, stay_min=stay_min, stay_max=stay_max,
        column_filter=column_filter,
        roster_open=bool(q or doctor or any_filter or request.args.get("view")),
        page=page, page_size=page_size, total_pages=total_pages,
        total_filtered=total_filtered, quality_report=quality_report,
        backup_status=backup_status,
    )


@app.route("/api/consult/<int:cid>/waitlist", methods=["POST"])
@login_required
def api_consult_waitlist(cid):
    con = models.get_consultation(cid)
    if not con or con.get("admission_status") != "입원대기":
        return jsonify({"error": "입원 대기 환자가 아닙니다."}), 404
    payload = request.get_json(silent=True) or {}
    allowed = ("wait_priority", "wait_preferred_ward", "wait_bed_requirements",
               "wait_next_contact_date", "wait_cancel_reason")
    fields = {k: (payload.get(k) or "").strip() or None for k in allowed if k in payload}
    priority = fields.get("wait_priority")
    if priority and priority not in ("긴급", "우선", "일반"):
        return jsonify({"error": "허용되지 않은 우선순위입니다."}), 400
    if payload.get("contact_done"):
        fields["wait_last_contact_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fields["wait_contact_by"] = g.user.get("display_name") or g.user.get("username")
    if not fields:
        return jsonify({"error": "변경할 항목이 없습니다."}), 400
    models.update_consultation_meta(cid, **fields)
    models.log_audit(user_id=g.user["id"], username=g.user["username"],
                     action="update_waitlist", target_type="consultation", target_id=cid,
                     detail="입원 대기 우선순위·연락 관리", ip=request.remote_addr)
    return jsonify({"ok": True, **fields})


@app.route("/api/admin/backup/run", methods=["POST"])
@admin_required
def api_backup_run():
    path = backup.run_backup("manual")
    if not path:
        return jsonify({"error": "백업 생성 또는 무결성 검사에 실패했습니다."}), 500
    return jsonify({"ok": True, "status": backup.latest_status()})


@app.route("/api/admin/backup/verify", methods=["POST"])
@admin_required
def api_backup_verify():
    result = backup.verify_latest_restore()
    return jsonify(result), (200 if result.get("ok") else 500)


def _ward_current_away(record, by_patient):
    event = by_patient.get(record.get("patient_id"))
    if not event:
        return None
    admitted = record.get("admitted_on") or record.get("actual_admission_date") or record.get("admission_date")
    if admitted and event.get("event_date") and event["event_date"][:10] < admitted[:10]:
        return None
    return event


def _ward_away_panel(events, doctor=None):
    """미복귀 환자 전체. 재원 여부와 별개이며 같은 환자는 최근 외진으로 한 번만 센다."""
    latest = {}
    for event in sorted(events, key=lambda e: (e.get("event_date") or "", e["id"])):
        latest[event["pid"]] = event
    return [{"id": e["consultation_id"], "patient_id": e["pid"],
             "patient_name": e["pname"], "room_number": e.get("room_number"),
             "guardian_name": e.get("guardian_name"),
             "guardian_phone": e.get("guardian_phone"), "away": e}
            for e in latest.values()
            if not doctor or e.get("attending_doctor") == doctor]


def _ward_admitted_roster(q, doctor):
    """재원(입원완료·미퇴원·입원일 있음) 환자 목록 — 파생 필드 모두 부착. ward_view/CSV 공유."""
    census = models.current_admission_census()
    rows = models.list_consultations(q=q, q_scope="ward", limit=10000)
    if census["has_roster"]:
        rows = [c for c in rows if c["id"] in census["by_consultation"]]
        for c in rows:
            ep = census["by_consultation"][c["id"]]
            c.update(actual_admission_date=ep["admitted_at"], discharge_date=None,
                     admission_status="입원완료", episode_id=ep["id"],
                     roster_care_phase=_roster_care_phase(ep.get("care_type")),
                     rehab_end_date=ep.get("rehab_end_date"),
                     rehab_end_imported=ep.get("rehab_end_imported"))
            for field in ("room_number", "attending_doctor"):
                if ep.get(field):
                    c[field] = ep[field]
        rows += [_ward_row_from_episode(ep) for ep in census["orphans"]
                 if _orphan_matches(ep, q)]
    else:
        rows = [c for c in rows if c.get("admission_status") == "입원완료"
                and not (c.get("discharge_date") or "").strip()]
    if doctor:
        rows = [c for c in rows if (c.get("attending_doctor") or "") == doctor]
    away_records = models.away_now()
    away_by_pid = {a["pid"]: a for a in away_records}
    admitted = []
    for c in rows:
        adm = (c.get("actual_admission_date") or c.get("admission_date") or "").strip()
        if not adm:
            continue
        c["admitted_on"] = adm
        c["away"] = _ward_current_away(c, away_by_pid)
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


@app.route("/ward/away.xlsx")
@login_required
def ward_away_xlsx():
    """외진·전원 명부를 현재 필터 그대로 엑셀로 — 화면의 열 순서와 같다."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    report = _ward_away_report()
    wb = Workbook()
    ws = wb.active
    ws.title = "외진·전원 명부"
    headers = ["연번", "차수", "환자", "성별", "나이", "병실", "퇴원일(전원일)", "시각", "외진 일수",
               "구분", "전원기관", "주상병", "부상병", "사유", "본원 입원일", "재원일수",
               "퇴원 D-day", "퇴원 예정일", "복귀 상태", "복귀일", "복귀 병실", "복귀 기타", "전원 병원"]
    ws.append(headers)
    for c in report["rows"]:
        watch = c.get("discharge_watch") or {}
        days_left = watch.get("days_left")
        dday = ("퇴원 완료" if c.get("discharge_date") else
                "" if days_left is None else
                f"D-{days_left}" if days_left > 0 else "D-day" if days_left == 0 else f"D+{-days_left}")
        away_days = c.get("away_days_inclusive")
        ws.append([
            c.get("no"), c.get("away_number") or "", c.get("patient_name") or "",
            {"M": "남", "F": "여"}.get(c.get("gender"), ""),
            c.get("patient_age") if c.get("patient_age") is not None else "",
            c.get("room_number") or "",
            c.get("event_date") or "", (c.get("event_time") or "")[:5],
            "" if away_days is None else (away_days if c.get("returned_at") else f"{away_days}일째"),
            c.get("event_type") or "", c.get("hospital") or "",
            ", ".join(c.get("dx_primary") or []), ", ".join(c.get("dx_secondary") or []),
            c.get("memo") or "",
            c.get("admitted_on") or "",
            c.get("stay_days_inclusive") if c.get("stay_days_inclusive") is not None else "",
            dday, c.get("discharge_date") or watch.get("due_date") or "",
            "타 병원 전원" if c.get("transferred") else "복귀 완료" if c.get("returned_at") else "미복귀",
            (c.get("returned_at") or "")[:10], c.get("return_room") or "", c.get("return_note") or "",
            c.get("return_hospital") or "",
        ])
    head_fill = PatternFill("solid", fgColor="E4F2EB")
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
    widths = [5, 5, 10, 5, 5, 8, 12, 6, 9, 12, 16, 22, 18, 30, 12, 8, 9, 12, 11, 11, 10, 20, 16]
    for idx, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    f = report["filters"]
    meta = [f"조회 기간 {f['away_from'] or '전체'} ~ {f['away_to'] or '전체'}",
            f"유형 {f['away_type'] or '전체'}", f"복귀 {f['away_status'] or '전체'}",
            f"내보낸 시각 {datetime.now().strftime('%Y-%m-%d %H:%M')}"]
    ws.cell(row=ws.max_row + 2, column=1, value=" · ".join(meta))
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    models.log_audit(user_id=g.user["id"], username=g.user["username"],
                     action="export_xlsx", target_type="ward_away",
                     detail=f"외진 명부 {len(report['rows'])}건", ip=request.remote_addr)
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True,
                     download_name=f"away_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx")


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
    admitted = _apply_ward_column_filters(admitted)
    admitted.sort(key=lambda c: (_room_sort_key(c.get("room_number")), c.get("patient_name") or ""))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["병동", "호실", "환자", "나이", "성별", "주치의", "주진단", "부진단",
                "내성균", "입원일", "재원일수", "수가구간", "D-day", "회복기종료/만료일",
                "퇴원예정", "연장", "관리태그", "지역(시도)", "재원기간 기준", "기준 도달일", "재원기간 D-day"])
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
            c.get("residence_sido") or "",
            _WARD_STAY_PERIODS.get(request.args.get("stay_period"), ""),
            c.get("stay_milestone_date") or "", c.get("stay_milestone_dday") or "",
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


_ORGANISMS = ("CRE", "VRE", "CPE", "MRSA", "MRAB", "MRPA")


def _roster_care_phase(value):
    """명부의 수가 구분 값 → 화면의 수가 구간.

    CRM은 발병일+진단군으로 회복기를 추정할 수밖에 없는데, 발병일이 비어 있는
    재원 환자가 35명이라 추정이 실제와 어긋난다(우리 82명 vs 원무 115명).
    명부에 구분이 실려 오면 추정하지 않고 그 값을 쓴다.

    원무 쪽 표기가 '회복기재활', '회복기(S005)'처럼 길 수 있어 접두로 본다.
    """
    v = (value or "").strip()
    if not v:
        return None
    if v.startswith("비회복"):
        return "비회복기"
    if v.startswith("회복"):
        return "회복기"
    if v.startswith("일반재활") or v.startswith("요양"):
        return "미판정"
    return None



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


def _orphan_matches(ep, q):
    """상담 없는 명부 환자에게도 재원 검색어를 적용한다.

    상담 검색(list_consultations q_scope='ward')은 상담 테이블만 보므로 명부에만
    있는 환자는 걸러지지 않은 채 늘 붙어 나왔다 — 환자명을 검색해도 '상담기록
    없음' 환자 전부가 따라왔다. 명부가 들고 있는 값(이름·병실·병동·주치의·진단·
    차트번호) 안에서 부분 일치로 거른다. 검색어가 없으면 전부 통과.
    """
    q = (q or "").strip()
    if not q:
        return True
    needle = q.replace(" ", "").casefold()
    hay = "".join(str(ep.get(k) or "") for k in
                  ("patient_name", "room_number", "ward", "attending_doctor",
                   "diagnosis_name", "diagnosis_code", "chart_no", "care_type")).replace(" ", "").casefold()
    return needle in hay


def _ward_row_from_episode(ep):
    """상담 없이 입원한 환자의 재원 행 — 원무 명부 값만으로 만든다.

    상담일지가 없는 환자라 id가 None이다. 화면은 이 값으로 상담 상세 링크와
    외진·퇴원 버튼을 감춘다. 인원에서 빼면 재원 수가 실제와 달라지므로
    명단에는 반드시 올린다.
    """
    return {
        "id": None, "patient_id": ep["patient_id"], "patient_name": ep.get("patient_name"),
        "gender": ep.get("gender"), "consult_date": None, "counselor": None,
        "admission_status": "입원완료",
        "admission_date": ep.get("admitted_at"),
        "actual_admission_date": ep.get("admitted_at"),
        "discharge_date": None, "discharge_due_date": None,
        "room_number": ep.get("room_number"), "roster_ward": ep.get("ward"),
        "attending_doctor": ep.get("attending_doctor"),
        "insurance_type": ep.get("insurance_type"),
        "primary_diagnosis": ep.get("diagnosis_name"),
        "diagnosis_code": ep.get("diagnosis_code"),
        "disease_detail": None, "diseases": [], "secondary_diagnosis": None,
        "patient_age": None, "episode_id": ep["id"],
        "roster_care_phase": _roster_care_phase(ep.get("care_type")),
        "rehab_end_date": ep.get("rehab_end_date"),
        "rehab_end_imported": ep.get("rehab_end_imported"),
        "roster_only": True,
    }


def _split_diagnosis(c):
    """diseases를 주 진단(회복기재활 입원 질환)·부 진단(기저·기타)으로 분리하고,
    내성균 목록을 뽑는다.
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


# 재원관리에서 선택하는 핵심 질환과 기존 입력 표현의 대응.
_WARD_DIAGNOSES = {
    "뇌경색": ("뇌경색",),
    "뇌출혈": ("뇌출혈",),
    "척수손상": ("척수손상",),
    "대퇴부골절": ("대퇴부골절", "대퇴골골절"),
    "고관절 골절": ("고관절골절",),
    "골반골절": ("골반골절",),
    "하지절단": ("하지절단", "하지부위절단"),
    "양측무릎슬관절": ("양측무릎슬관절", "양측슬관절치환술", "양측무릎관절치환술"),
    "비사용증후군": ("비사용증후군",),
    "파킨슨": ("파킨슨",),
    "폐렴": ("폐렴",),
    "신생물": ("신생물",),
    "심장질환": ("심장질환",),
}


def _ward_matches_diagnosis(c, diagnosis):
    if not diagnosis:
        return True
    aliases = _WARD_DIAGNOSES.get(diagnosis)
    if not aliases:
        return False
    values = (c.get("dx_primary") or []) + (c.get("dx_secondary") or [])
    # 호흡질환의 상세에 입력된 폐렴도 검색한다.
    values = values + [c.get("lung_detail") or ""]
    return any(alias in "".join(str(value).split())
               for value in values for alias in aliases)


def _trend_span(span, con):
    """추이 계산용 회차 요약 → (입원일, 퇴원일, 판정 가능 여부, 회복기 종료일).

    _effective_roster_care_phase / _recovery_status 와 같은 규칙이되, 날짜마다
    다시 판정하지 않도록 '언제까지 회복기인가'만 뽑아 둔다.
      rec_end None     = 어느 날짜에도 회복기가 아님
      rec_end date.max = 재원 내내 회복기 (종료일을 계산할 수 없는 경우)
    """
    admitted_iso, discharged_iso = span["admitted_at"], span.get("discharged_at")
    roster_phase = _roster_care_phase(span.get("care_type"))
    if roster_phase:
        if span.get("rehab_end_imported"):
            end = span.get("rehab_end_date")
            return admitted_iso, discharged_iso, True, (date.fromisoformat(end) if end else None)
        if roster_phase != "회복기":
            return admitted_iso, discharged_iso, True, None
        period = compute_admission_period((con or {}).get("diseases"), "회복기")
        if not period:
            return admitted_iso, discharged_iso, True, date.max
        try:
            end = _day_of(date.fromisoformat(str(admitted_iso)[:10]),
                          period.get("billing") or period.get("total"))
        except (TypeError, ValueError):
            return admitted_iso, discharged_iso, True, date.max
        return admitted_iso, discharged_iso, True, end
    if con is None:
        return admitted_iso, discharged_iso, False, None
    if _recovery_status(con).get("label") != "회복기":
        return admitted_iso, discharged_iso, True, None
    period = compute_admission_period(con.get("diseases"), "회복기")
    try:
        admitted_on = date.fromisoformat(admitted_iso)
    except (TypeError, ValueError):
        return admitted_iso, discharged_iso, True, None
    if period and period.get("billing"):
        return admitted_iso, discharged_iso, True, _day_of(admitted_on, period["billing"])
    return admitted_iso, discharged_iso, True, date.max


def _trend_summary(daily, monthly, threshold=40):
    """선택 기간의 요약 — 상단 카드용.

    기간 평균은 '회복기 연인원 ÷ 판정 가능 연인원'(재원일수 가중)으로 낸다. 일별
    비율의 단순 평균은 인원이 적은 날이 과대 반영되는데, 지정 기준 평가도 연인원
    기준이라 이쪽이 실제와 맞는다. 단순 평균은 참고로 함께 둔다.
    """
    days = [d for d in daily if d["known"]]
    if not days:
        return None
    rec_sum = sum(d["recovery"] for d in days)
    known_sum = sum(d["known"] for d in days)
    below = [d for d in days if d["ratio"] < threshold]
    low = min(days, key=lambda d: (d["ratio"], d["date"]))
    high = max(days, key=lambda d: (d["ratio"], d["date"]))
    # 40% 미만이 이어진 구간 — 연속된 날짜를 하나로 묶고 구간의 최저치를 함께 둔다.
    runs, run = [], None
    for d in days:
        if d["ratio"] < threshold:
            if run and (date.fromisoformat(d["date"]) - date.fromisoformat(run["end"]["date"])).days == 1:
                run["end"] = d
                run["days"] += 1
                if d["ratio"] < run["low"]["ratio"]:
                    run["low"] = d
            else:
                run = {"start": d, "end": d, "days": 1, "low": d}
                runs.append(run)
        else:
            run = None
    months = [m for m in monthly if m["known"]]
    month_avg = (round(sum(m["recovery"] for m in months) * 100 / sum(m["known"] for m in months), 1)
                 if months else None)
    return {
        "avg": round(rec_sum * 100 / known_sum, 1),
        "avg_simple": round(sum(d["ratio"] for d in days) / len(days), 1),
        "days": len(days), "below_days": len(below),
        "below_first": below[0] if below else None,
        "below_runs": runs,
        "low": low, "high": high,
        "first": days[0], "last": days[-1],
        "month_avg": month_avg, "month_count": len(months),
        "ok": rec_sum * 100 >= threshold * known_sum,
    }


def _ratio_insight(now, admitted, ratio_at, today, threshold=40, horizon=60):
    """회복기 비율 40% 기준선에 대한 여유·필요 인원과 향후 예상 추이.

    비율은 추이 그래프와 같은 분모(회복기 판정이 가능한 재원 인원 known)로 낸다.
    정수 산식(비율 = R/K, 기준 t = threshold/100):
      · 회복기 k명 퇴원해도 유지 → (R-k) ≥ t(K-k)  → k ≤ (R - tK)/(1-t)
      · 비회복기 m명 입원해도 유지 → R ≥ t(K+m)      → m ≤ R/t - K
      · 회복기 n명 입원하면 도달 → (R+n) ≥ t(K+n)   → n ≥ (tK - R)/(1-t)
      · 비회복기 m명 퇴원하면 도달 → R ≥ t(K-m)     → m ≥ K - R/t
    예상 추이는 '지금 재원이 그대로 있다'고 가정하고 회복기 종료일이 지나는 환자만
    비회복기로 바꿔 계산한다 — ratio_at이 미래 날짜도 같은 규칙으로 판정하므로 그대로 쓴다.
    """
    R, K = now["recovery"], now["known"]
    if not K:
        return None
    t_num, t_den = threshold, 100          # t = t_num/t_den
    ok = R * t_den >= t_num * K
    # 회복기/비회복기 각각 x명 늘거나 줄 때의 비율
    def ratio_after(d_rec=0, d_non=0):
        r, k = R + d_rec, K + d_rec + d_non
        return round(r * 100 / k, 1) if k > 0 else 0
    insight = {"ratio": now["ratio"], "recovery": R, "known": K, "total": now["total"],
               "ok": ok, "threshold": threshold}
    if ok:
        # 회복기 퇴원 여유: k ≤ (100R - tK) / (100 - t)
        rec_out = max(0, (t_den * R - t_num * K) // (t_den - t_num))
        # 비회복기 입원 여유: m ≤ (100R - tK) / t
        non_in = max(0, (t_den * R - t_num * K) // t_num)
        insight.update(rec_out=rec_out, rec_out_ratio=ratio_after(d_rec=-(rec_out + 1)),
                       non_in=non_in, non_in_ratio=ratio_after(d_non=non_in + 1))
    else:
        need = t_num * K - t_den * R
        rec_in = -(-need // (t_den - t_num))     # ceil
        non_out = -(-need // t_num)
        insight.update(rec_in=rec_in, rec_in_ratio=ratio_after(d_rec=rec_in),
                       non_out=non_out, non_out_ratio=ratio_after(d_non=-non_out))
    # 향후 예상 — 회복기 종료로만 비율이 내려간다. 40% 아래로 처음 내려가는 날을 찾는다.
    forecast, cross = [], None
    for offset in range(0, horizon + 1):
        snapshot = today + timedelta(days=offset)
        point = {"label": snapshot.strftime("%m.%d"), "date": snapshot.isoformat(),
                 **ratio_at(snapshot)}
        forecast.append(point)
        if cross is None and ok and point["known"] and point["recovery"] * t_den < t_num * point["known"]:
            cross = point
    ending = sorted(
        [c for c in admitted if c.get("care_phase") == "회복기"
         and c.get("phase_dday") is not None and 0 <= c["phase_dday"] <= horizon],
        key=lambda c: (c["phase_dday"], c.get("patient_name") or ""))
    insight.update(forecast=forecast, cross=cross, horizon=horizon,
                   ending=ending, ending_30=sum(1 for c in ending if c["phase_dday"] <= 30),
                   end_ratio=forecast[-1]["ratio"])
    return insight


_WARD_TREND_RANGES = {"30": "최근 30일", "90": "최근 90일", "180": "최근 6개월", "365": "최근 1년"}
_WARD_STAY_PERIODS = {"6": "6개월", "12": "1년", "18": "1년 6개월", "24": "2년"}


def _ward_stay_milestone(admitted_on, months, today=None):
    """달력상 입원 기념일 기준: 이전 D-, 당일 D-Day, 이후 D+."""
    try:
        admitted_date = date.fromisoformat(admitted_on or "")
        today = today or date.today()
        if admitted_date > today:
            return {}
        due = _add_months(admitted_date, int(months))
    except (ValueError, TypeError, OverflowError):
        return {}
    delta = (today - due).days
    return {"stay_milestone_date": due.isoformat(), "stay_milestone_delta": delta,
            "stay_milestone_dday": "D-Day" if delta == 0 else f"D{delta:+d}"}


def _apply_ward_column_filters(admitted):
    """화면과 CSV에 동일한 상세조건을 적용한다."""
    room_f = (request.args.get("room") or "").strip()
    gender_f = (request.args.get("gender") or "").strip()
    dx_f = (request.args.get("dx") or "").strip().lower()
    admission_from = (request.args.get("admission_from") or "").strip()
    admission_to = (request.args.get("admission_to") or "").strip()
    discharge_from = (request.args.get("discharge_from") or "").strip()
    discharge_to = (request.args.get("discharge_to") or "").strip()
    def _optional_int(name):
        try:
            raw = (request.args.get(name) or "").strip()
            return int(raw) if raw else None
        except ValueError:
            return None
    age_min, age_max = _optional_int("age_min"), _optional_int("age_max")
    stay_min, stay_max = _optional_int("stay_min"), _optional_int("stay_max")
    sido_f = (request.args.get("sido") or "").strip()
    stay_period = (request.args.get("stay_period") or "").strip()
    if stay_period not in _WARD_STAY_PERIODS:
        stay_period = ""
    admitted = [c for c in admitted
                     if (not room_f or room_f.lower() in (c.get("room_number") or "").lower())
                     and (not gender_f or c.get("gender") == gender_f)
                     and (age_min is None or (c.get("patient_age") is not None and c["patient_age"] >= age_min))
                     and (age_max is None or (c.get("patient_age") is not None and c["patient_age"] <= age_max))
                     and _ward_matches_diagnosis(c, dx_f)
                     and (not admission_from or (c.get("admitted_on") or "") >= admission_from)
                     and (not admission_to or (c.get("admitted_on") or "") <= admission_to)
                     and (stay_min is None or (c.get("stay_days") or 0) >= stay_min)
                     and (stay_max is None or (c.get("stay_days") or 0) <= stay_max)
                     and (not discharge_from or (c.get("discharge_due") or "") >= discharge_from)
                     and (not discharge_to or (c.get("discharge_due") or "") <= discharge_to)]
    if sido_f:
        admitted = [c for c in admitted
                    if _sido_short(c.get("residence_sido")) == _sido_short(sido_f)]
    if stay_period:
        selected = []
        for c in admitted:
            milestone = _ward_stay_milestone(c.get("admitted_on"), stay_period)
            if milestone and abs(milestone["stay_milestone_delta"]) <= 30:
                selected.append(dict(c, **milestone))
        admitted = selected
    return admitted


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
    if organism_f == "1":
        out = [c for c in out if c.get("organisms")]
    elif organism_f:
        out = [c for c in out if organism_f in (c.get("organisms") or [])]
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
    fields = {"actual_admission_date": adate, "admission_date": adate,
              "admission_status": "입원완료"}
    room = (payload.get("room_number") or "").strip()
    if room:
        fields["room_number"] = room
    models.update_consultation(cid, **fields)
    _sync_lifecycle_stage(con["patient_id"], "입원완료")
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

@app.route('/inbox',methods=['GET','POST'])
@login_required
def unified_inbox():
    if not INBOX_ENABLED: abort(404)
    if menu_level(current_user(),'dashboard')<PERM_VIEW: abort(403)
    token=session.setdefault('inbox_csrf',secrets.token_hex(32))
    if request.method=='POST':
        if not secrets.compare_digest(token,request.form.get('csrf','')): abort(400)
        if menu_level(current_user(),'consult')<PERM_EDIT: abort(403)
        action=(request.form.get('action') or '').strip()
        if action=='create':
            channel=(request.form.get('channel') or '').strip()
            summary=(request.form.get('summary') or '').strip()
            if channel not in COMM_CHANNELS or not summary:
                flash('채널과 문의 요약을 입력해주세요.','error')
            else:
                cid=models.create_communication(channel=channel,direction='in',contact=(request.form.get('contact') or '').strip() or None,
                    summary=summary[:200],body=(request.form.get('body') or '').strip()[:3000] or None,
                    follow_up_at=(request.form.get('follow_up_at') or '').strip() or None,
                    occurred_at=(request.form.get('occurred_at') or '').strip() or None,created_by=g.user['display_name'])
                models.update_communication(cid,assigned_user_id=g.user['id'],priority=(request.form.get('priority') if request.form.get('priority') in ('normal','high','urgent') else 'normal'))
                flash('새 문의를 통합 인박스에 등록했습니다.','success')
        elif action=='update':
            try: cid=int(request.form.get('comm_id') or 0)
            except ValueError: cid=0
            comm=models.get_communication(cid)
            if not comm: abort(404)
            status=request.form.get('status') if request.form.get('status') in ('open','in_progress','waiting','done') else 'open'
            assignee=(request.form.get('assigned_user_id') or '').strip()
            priority=request.form.get('priority') if request.form.get('priority') in ('normal','high','urgent') else 'normal'
            models.update_communication(cid,status=status,assigned_user_id=(int(assignee) if assignee.isdigit() else None),priority=priority,
                follow_up_at=(request.form.get('follow_up_at') or '').strip() or None)
            models.log_audit(user_id=g.user['id'],username=g.user['username'],action='update_inbox',target_type='communication',target_id=cid,detail=f'{status}/{priority}',ip=request.remote_addr)
            flash('문의 처리 상태를 저장했습니다.','success')
        return redirect(request.form.get('return_to') if _is_safe_next_url(request.form.get('return_to')) else url_for('unified_inbox'))
    filters={k:(request.args.get(k) or '').strip() for k in ('status','channel','assignee','q')}
    if not filters['status']: filters['status']='active'
    users=[u for u in models.list_users() if u.get('active') and u.get('role') in ('admin','staff')]
    return render_template('inbox.html',rows=models.inbox_communications(**filters),summary=models.inbox_summary(),
        callbacks=models.inbox_callbacks(),users=users,filters=filters,csrf=token)


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
    if event_type in models.AWAY_EVENT_TYPES:
        if models.open_away_event(cid):
            return jsonify({"error": "미복귀 기록이 있습니다. 먼저 복귀 처리하세요."}), 400
        if event_date and event_date > date.today().isoformat():
            return jsonify({"error": "전원·외진일은 미래일 수 없습니다."}), 400
        admitted_on = (con.get("actual_admission_date") or con.get("admission_date") or "")[:10]
        if event_date and admitted_on and event_date < admitted_on:
            return jsonify({"error": "전원·외진일이 입원일보다 빠릅니다."}), 400
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
    if ev.get("event_type") not in models.AWAY_EVENT_TYPES:
        return jsonify({"error": "외진·전원 기록만 복귀 처리할 수 있습니다."}), 400
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
    # 병실·기타 사항은 선택 입력이다 — 재원 현황 카드의 [↩ 복귀] 1클릭은
    # 날짜만 보낸다. 병실을 안 주면 현재 병실을 그대로 둔다.
    return_room = (payload.get("return_room") or "").strip()
    return_note = (payload.get("return_note") or "").strip()
    if len(return_room) > 50:
        return jsonify({"error": "복귀 병실은 50자 이내로 입력하세요."}), 400
    if len(return_note) > 3000:
        return jsonify({"error": "기타 사항은 3000자 이내로 입력하세요."}), 400
    # 외진이 복귀가 아니라 타 병원 전원으로 끝나는 경우 — 종결일(returned_at)은
    # 같은 칸에 찍되 outcome='전원'으로 구분한다. 병실은 옮기지 않고 단계는 퇴원.
    outcome = (payload.get("return_outcome") or "복귀").strip()
    if outcome not in ("복귀", "전원"):
        return jsonify({"error": "처리 결과는 복귀 또는 전원입니다."}), 400
    return_hospital = (payload.get("return_hospital") or "").strip()
    if outcome == "전원" and not return_hospital:
        return jsonify({"error": "전원 간 병원을 입력하세요."}), 400
    if len(return_hospital) > 200:
        return jsonify({"error": "전원 병원은 200자 이내로 입력하세요."}), 400
    models.mark_admission_event_returned(
        event_id, return_date=return_date,
        returned_by=g.user.get("display_name"),
        return_room=return_room or None, return_note=return_note or None,
        outcome=outcome, return_hospital=return_hospital or None,
    )
    con = models.get_consultation(ev["consultation_id"])
    back_to = "퇴원" if outcome == "전원" else (ev.get("stage_before") or "입원").strip()
    if con:
        _set_lifecycle_stage_clinical(con["patient_id"], back_to)
    if outcome == "전원":
        # 타 병원으로 갔으면 재원이 아니다 — 명부 회차도 닫는다(퇴원 처리와 같은 이유).
        ep = models.current_admission_census()["by_consultation"].get(ev["consultation_id"])
        if ep:
            models.close_roster_episode(ep["id"], discharged_at=return_date or date.today().isoformat(),
                                        destination=return_hospital, reason="타 병원 전원")
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="return_admission_event", target_type="consultation",
        target_id=ev["consultation_id"],
        detail=(f"{ev.get('event_type')} → 타 병원 전원({return_hospital})" if outcome == "전원"
                else f"{ev.get('event_type')} 복귀 → {back_to}"), ip=request.remote_addr,
    )
    return jsonify({"ok": True, "stage": back_to,
                    "return_date": return_date or date.today().isoformat()})


@app.route("/api/admission-event/<int:event_id>/details", methods=["POST"])
@login_required
def api_admission_event_details(event_id):
    """외진 명부에서 생년월일·재입원 여부를 고친다.

    두 항목은 상담일지에 없던 값이라 외진 서류를 준비하며 그 자리에서
    확인되는 일이 많다. 상담 상세까지 들어갔다 나오지 않게 명부에서 바로 받는다.
    """
    ev = models.get_admission_event(event_id)
    if not ev or ev.get("event_type") not in models.AWAY_EVENT_TYPES:
        return jsonify({"error": "외진·전원 기록이 아닙니다."}), 404
    payload = request.get_json(silent=True) or {}
    birth_date = (payload.get("birth_date") or "").strip()
    readmission = (payload.get("readmission") or "").strip()
    if readmission not in ("", "예", "아니오"):
        return jsonify({"error": "재입원 여부는 예·아니오 중에서 고르세요."}), 400
    if birth_date:
        try:
            if datetime.strptime(birth_date, "%Y-%m-%d").date() > date.today():
                raise ValueError
        except ValueError:
            return jsonify({"error": "생년월일을 확인하세요 (YYYY-MM-DD, 미래 불가)."}), 400
    try:
        cid = models.set_away_record_details(
            event_id, birth_date=birth_date or None, readmission=readmission or None)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="update_admission_event", target_type="consultation", target_id=cid,
        detail="외진 명부 생년월일·재입원 여부 수정", ip=request.remote_addr,
    )
    return jsonify({"ok": True})


@app.route("/api/admission-event/<int:event_id>", methods=["DELETE"])
@login_required
def api_admission_event_delete(event_id):
    if not models.get_admission_event(event_id):
        return jsonify({"error": "not found"}), 404
    models.delete_admission_event(event_id)
    return jsonify({"ok": True})


@app.route("/api/inbound/alerts")
@login_required
def api_inbound_alerts():
    """미처리 인바운드 알림 피드 — 상담사 브라우저가 주기적으로 폴링.
    홈페이지·카카오 등 채널 문의가 새로 들어오면 화면 알림으로 띄운다.
    프론트가 localStorage로 '이미 본 id'를 관리하므로 서버는 현재 대기목록만 반환."""
    items = []
    for m in models.inbox_open_communications():
        items.append({
            "id": m.get("id"),
            "channel": m.get("channel") or "기타",
            "bucket": _dashboard_inbound_bucket(m),
            "summary": (m.get("summary") or m.get("body") or "새 문의")[:80],
            "contact": m.get("contact") or "",
            "patient_name": m.get("patient_name") or "",
            "blacklist": bool(m.get("blacklist")),
            "created_at": m.get("occurred_at") or m.get("created_at") or "",
        })
    return jsonify({"count": len(items), "items": items})


# ───────────────────── 인바운드 webhook (옴니채널 직수신) ─────────────────────
# 카카오 비즈채널·홈페이지 문의폼이 사내망 CRM으로 보내는 유일한 외부 노출 경로.
# 보안: 역프록시에서 /api/webhook/* 만 외부로 열고 나머지는 사내망 유지.
#  ① 채널별 토큰(hmac.compare_digest, 상수시간 비교) ② 선택적 IP 화이트리스트
#  ③ 요청 크기 제한 ④ IP당 rate limit ⑤ 감사로그(식별정보 평문 미기록).

_WEBHOOK_MAX_BYTES = 16 * 1024          # 인바운드 문의 1건 — 16KB면 충분
_WEBHOOK_RATE_MAX = 60                   # IP당
_WEBHOOK_RATE_WINDOW = 60                # 초
_WEBHOOK_HITS: dict[str, list[float]] = {}


def _webhook_rate_limited(ip: str) -> bool:
    """IP당 슬라이딩 윈도우 rate limit — 외부 노출 경로 남용 완화 (메모리 기반)."""
    now = time.time()
    hits = [t for t in _WEBHOOK_HITS.get(ip, []) if now - t < _WEBHOOK_RATE_WINDOW]
    hits.append(now)
    _WEBHOOK_HITS[ip] = hits
    if len(_WEBHOOK_HITS) > 2048:        # 메모리 누수 방지 — 오래된 IP 정리
        for k in [k for k, v in _WEBHOOK_HITS.items()
                  if v and now - v[-1] > _WEBHOOK_RATE_WINDOW]:
            _WEBHOOK_HITS.pop(k, None)
    return len(hits) > _WEBHOOK_RATE_MAX


def _webhook_guard(token_env: str):
    """공통 웹훅 검문. 통과하면 (payload, None), 막히면 (None, (json, status)).
    외부에는 내부 사정을 노출하지 않도록 오류 메시지를 최소화한다.
    """
    ip = (request.remote_addr or "").strip()
    # 선택적 IP 화이트리스트 — 설정 시 카카오/홈페이지 서버 IP만 허용
    allow = [x.strip() for x in os.getenv("WEBHOOK_ALLOW_IPS", "").split(",") if x.strip()]
    if allow and ip not in allow:
        return None, (jsonify({"error": "forbidden"}), 403)
    if _webhook_rate_limited(ip):
        return None, (jsonify({"error": "rate limited"}), 429)
    expected = os.getenv(token_env, "").strip()
    if not expected:                     # 토큰 미설정 = 채널 비활성 (기본 안전)
        return None, (jsonify({"error": "webhook disabled"}), 503)
    token = (request.headers.get("X-Webhook-Token") or request.args.get("token") or "")
    if not hmac.compare_digest(token, expected):
        return None, (jsonify({"error": "unauthorized"}), 401)
    raw = request.get_data(cache=True) or b""
    if len(raw) > _WEBHOOK_MAX_BYTES:
        return None, (jsonify({"error": "payload too large"}), 413)
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return None, (jsonify({"error": "bad payload"}), 400)
    return payload, None


def _log_webhook(channel: str, pid, comm_id):
    """웹훅 인바운드 감사 로그 — 식별정보(전화/이름/본문)는 남기지 않는다."""
    try:
        models.log_audit(
            user_id=None, username="webhook", action="inbound_webhook",
            target_type="communication", target_id=comm_id,
            detail=f"{channel}/in", ip=request.remote_addr,
        )
    except Exception:                    # 감사 실패가 문의 수신을 막지 않도록
        pass


@app.route("/api/webhook/kakao", methods=["POST"])
def api_webhook_kakao():
    """카카오 비즈채널 인바운드 — .env KAKAO_WEBHOOK_TOKEN으로 검증.
    보호자 번호로 환자 자동 매칭해 communications(인바운드)로 기록 → 인박스 노출.
    실제 카카오 페이로드 형식은 채널 연동 시 확정 (현재는 범용 형태 수신)."""
    payload, err = _webhook_guard("KAKAO_WEBHOOK_TOKEN")
    if err:
        return err
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
        body=message[:4000], status="open", created_by="카카오봇",
    )
    _log_webhook("카카오", pid, comm_id)
    return jsonify({"ok": True, "id": comm_id, "matched_patient": pid})


# 카카오 i 오픈빌더 상담신청 폼 → 필드 별칭 매핑 (성함/연락처/… 파라미터 흡수)
_KAKAO_FIELDS = [
    ("name",      ("성함", "이름", "name")),
    ("phone",     ("연락처", "전화", "휴대", "phone", "tel")),
    ("call_time", ("연락가능", "가능시간", "통화가능", "연락시간")),
    ("residence", ("거주지", "거주", "주소", "지역")),
    ("age",       ("환자나이", "나이", "연세", "age")),
    ("message",   ("상담내용", "문의내용", "상담", "내용", "message")),
]
_KAKAO_LABELS = {"call_time": "연락가능시간", "residence": "거주지",
                 "age": "환자나이", "message": "상담내용"}


def _norm_phone(raw: str) -> str:
    """숫자만 남겨 010-XXXX-XXXX 형태로. 매칭률을 높이되 실패해도 원본 반환."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("01"):
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    if len(digits) == 10 and digits.startswith("01"):
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return (raw or "").strip()


def _kakao_skill_extract(params: dict):
    """오픈빌더 action.params(구조화 값)에서 상담 필드를 뽑고,
    누락 없이 body에 모든 값을 라벨과 함께 보존한다."""
    picked, used = {}, set()
    for canon, aliases in _KAKAO_FIELDS:
        for k, v in params.items():
            if k in used or not v:
                continue
            if any(a in k for a in aliases) or k == canon:
                picked[canon] = str(v).strip()
                used.add(k)
                break
    # 매핑 안 된 나머지 파라미터도 버리지 않고 보존
    leftovers = {k: v for k, v in params.items()
                 if k not in used and v and not k.startswith("sys_")}
    return picked, leftovers


@app.route("/api/webhook/kakao/skill", methods=["POST"])
def api_webhook_kakao_skill():
    """카카오 i 오픈빌더 '스킬' 콜백 — 챗봇 상담신청 폼 제출을 수신.
    오픈빌더가 action.params(성함·연락처·연락가능시간·거주지·환자나이·상담내용)를 보내면
    communications(카카오/인바운드)로 등록하고, 사용자에게는 접수 확인 말풍선을 응답한다.
    토큰은 스킬 URL의 ?token= 또는 커스텀 헤더 X-Webhook-Token 으로 검증(KAKAO_WEBHOOK_TOKEN).
    """
    payload, err = _webhook_guard("KAKAO_WEBHOOK_TOKEN")
    if err:
        return err

    def skill_say(text):
        return jsonify({"version": "2.0",
                        "template": {"outputs": [{"simpleText": {"text": text}}]}})

    action = payload.get("action") or {}
    params = action.get("params") or {}
    # detailParams가 있으면 정제된 value를 우선 사용
    detail = action.get("detailParams") or {}
    for k, dv in detail.items():
        if isinstance(dv, dict) and dv.get("value"):
            params.setdefault(k, dv["value"])
    utterance = ((payload.get("userRequest") or {}).get("utterance") or "").strip()

    picked, leftovers = _kakao_skill_extract(params)
    name = picked.get("name", "")
    phone = _norm_phone(picked.get("phone", ""))
    msg = picked.get("message", "") or utterance

    # body: 라벨 붙은 구조화 텍스트로 모든 값 보존
    lines = []
    for canon in ("message", "call_time", "residence", "age"):
        if picked.get(canon):
            lines.append(f"{_KAKAO_LABELS.get(canon, canon)}: {picked[canon]}")
    for k, v in leftovers.items():
        lines.append(f"{k}: {v}")
    body = "\n".join(lines).strip() or utterance or "상담 신청"

    pid = models.match_patient_by_phone(phone) if phone else None
    summary = "카카오 상담신청" + (f" · {name}" if name else "")
    comm_id = models.create_communication(
        patient_id=pid, channel="카카오", direction="in",
        contact=phone or name or None,
        summary=summary[:200], body=body[:4000],
        status="open", created_by="카카오챗봇",
    )
    _log_webhook("카카오", pid, comm_id)
    return skill_say(
        f"상담 신청이 접수되었습니다{(' · ' + name) if name else ''}.\n"
        "평일 09:00~17:30, 토 09:00~12:30 중 순차적으로 전화드리겠습니다. 감사합니다.")


@app.route("/api/webhook/homepage", methods=["POST"])
def api_webhook_homepage():
    """홈페이지 문의폼 인바운드 — .env HOMEPAGE_WEBHOOK_TOKEN으로 검증.
    홈페이지 서버가 문의 1건을 서버-투-서버로 POST한다(토큰은 브라우저에 노출 금지).
    body(JSON): { name, phone, email?, subject?, message } — message 필수.
    전화번호로 환자 자동 매칭, communications(채널=웹문의, 인바운드)로 기록."""
    payload, err = _webhook_guard("HOMEPAGE_WEBHOOK_TOKEN")
    if err:
        return err
    name = (payload.get("name") or "").strip()
    phone = (payload.get("phone") or "").strip()
    email = (payload.get("email") or "").strip()
    subject = (payload.get("subject") or "").strip()
    message = (payload.get("message") or payload.get("content") or "").strip()
    if not message:
        return jsonify({"error": "message 필수"}), 400
    pid = models.match_patient_by_phone(phone)
    head = subject or "홈페이지 문의"
    summary = head + (f" · {name}" if name else "")
    body = message[:4000]
    if email:
        body = f"{body}\n\n[이메일] {email}"
    comm_id = models.create_communication(
        patient_id=pid, channel="웹문의", direction="in",
        contact=phone or email or name or None,
        summary=summary, body=body, status="open", created_by="홈페이지",
    )
    _log_webhook("웹문의", pid, comm_id)
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
    date_from = _valid_date(request.args.get("from"))
    date_to = _valid_date(request.args.get("to"))
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
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
        preselect=preselect, log=models.list_sms_log(200, date_from=date_from, date_to=date_to),
        date_from=date_from, date_to=date_to,
        placeholders=SMS_PLACEHOLDERS,
        gateway=sms_gateway.gateway_info(),
        gateway_ready=sms_gateway.gateway_configured(),
        sms_max_bytes=sms_gateway.SMS_MAX_BYTES, lms_max_bytes=sms_gateway.LMS_MAX_BYTES,
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

    msg_type, nbytes = sms_gateway.message_type(body)
    if msg_type == "TOO_LONG":
        return jsonify({"error": f"본문이 {nbytes}바이트 — 장문(LMS) 한도 "
                                 f"{sms_gateway.LMS_MAX_BYTES}바이트를 넘습니다."}), 400

    status, error, provider, provider_msg_id, sent_to = "manual", None, None, None, None
    if sms_gateway.gateway_configured():
        result = sms_gateway.send_sms(to_phone, body)
        status, error = result["status"], result.get("error")
        msg_type = result.get("msg_type") or msg_type
        provider, provider_msg_id = sms_gateway.provider_name(), result.get("provider_msg_id")
        sent_to = result.get("sent_to")

    sid = models.log_sms(
        consultation_id=payload.get("consultation_id"),
        patient_id=payload.get("patient_id"),
        template_id=payload.get("template_id"),
        to_name=(payload.get("to_name") or "").strip() or None,
        to_phone=to_phone, body=body, status=status,
        sent_by=g.user.get("display_name"),
        msg_type=msg_type, provider=provider, provider_msg_id=provider_msg_id,
        sent_to=sent_to, error=error,
    )
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="send_sms", target_type="consultation",
        target_id=payload.get("consultation_id"),
        detail=f"{to_phone} [{status}/{msg_type}]", ip=request.remote_addr,
    )
    return jsonify({"ok": True, "id": sid, "status": status, "error": error,
                    "msg_type": msg_type, "bytes": nbytes, "sent_to": sent_to})


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
