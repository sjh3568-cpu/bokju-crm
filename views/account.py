"""인증·계정 — 로그인/로그아웃, 시작 화면, 내 계정 설정, 비밀번호 재설정 요청.

app.py에서 분리(2026-09-15). 라우트 경로·동작은 그대로, 엔드포인트 이름만 "account.<함수>"가 됐다.
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
import partnerships
import support_requests
import transport
import homepage_board
import ward_moves
from flask import Blueprint
import logging
from app import (  # noqa: E402 — app.py 공용 헬퍼·상수 (app.py 맨 아래에서 이 모듈을 import하므로 순환 없음)
    INBOX_ENABLED,
    _MIN_PW_LEN,
    _REMEMBER_COOKIE,
    _REMEMBER_DAYS,
    _is_safe_next_url,
    _remember_fingerprint,
    _remember_serializer,
    app,
)

logger = logging.getLogger(__name__)
bp = Blueprint("account", __name__)

# ───────────────────── 인증 ─────────────────────

@bp.route("/login", methods=["GET", "POST"])
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
            next_url = url_for("main.dashboard")
        destination = (url_for("notices.notice_required", next=next_url)
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
        return redirect(url_for("main.dashboard"))
    return render_template("login.html")

@bp.route("/logout", methods=["POST", "GET"])
def logout_view():
    logout_user()
    flash("로그아웃되었습니다.", "info")
    response = redirect(url_for("account.login_view"))
    response.delete_cookie(_REMEMBER_COOKIE, path="/", samesite="Lax")
    return response

@bp.route('/settings/start-page',methods=['POST'])
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
    return redirect(request.referrer or url_for('main.dashboard'))

@bp.route('/account',methods=['GET','POST'])
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
            return redirect(url_for('account.account_settings'))
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
            return redirect(url_for('account.account_settings'))
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
                if ward_tab in ('status','away','waiting','trend','blacklist','quality','moves'): prefs['ward_tab']=ward_tab
                else: prefs.pop('ward_tab',None)
            models.set_user_preferences(g.user['id'],prefs);flash('개인 알림과 기본 보기를 저장했습니다.','success')
            return redirect(url_for('account.account_settings'))
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
            return redirect(url_for('account.account_settings'))
    user=models.get_user_by_id(g.user['id'])
    with closing(models.get_db()) as db:
        recent_logins=[dict(r) for r in db.execute("SELECT created_at,ip FROM audit_log WHERE user_id=? AND action='login' ORDER BY id DESC LIMIT 5",(g.user['id'],))]
    return render_template('account.html',csrf=session['account_csrf'],min_password_length=_MIN_PW_LEN,
                           account=user,recent_logins=recent_logins,menus=MENUS,
                           permission_requests=models.list_permission_requests(g.user['id']))

@bp.route("/password-reset/request", methods=["POST"])
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
    return redirect(url_for("account.login_view"))
