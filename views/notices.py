"""공지사항 — 목록·필독 확인·작성·활성/비활성.

app.py에서 분리(2026-09-15). 라우트 경로·동작은 그대로, 엔드포인트 이름만 "notices.<함수>"가 됐다.
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
    _is_safe_next_url,
    _valid_date,
    app,
)

logger = logging.getLogger(__name__)
bp = Blueprint("notices", __name__)

# ───────────────────── 공지사항 ─────────────────────

@bp.route("/notices")
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

@bp.route("/notices/required")
@login_required
def notice_required():
    user = current_user()
    notice = models.first_unread_required_announcement(
        user["id"], user.get("role", "staff"))
    next_url = request.args.get("next") or url_for("main.dashboard")
    if not _is_safe_next_url(next_url):
        next_url = url_for("main.dashboard")
    if not notice:
        return redirect(next_url)
    return render_template("notice_required.html", notice=notice, next_url=next_url)

@bp.route("/notices/<int:notice_id>/ack", methods=["POST"])
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
    next_url = request.form.get("next") or url_for("main.dashboard")
    if not _is_safe_next_url(next_url):
        next_url = url_for("main.dashboard")
    if models.first_unread_required_announcement(user["id"], user.get("role", "staff")):
        return redirect(url_for("notices.notice_required", next=next_url))
    flash("공지사항을 확인했습니다.", "success")
    return redirect(next_url)

@bp.route("/notices/create", methods=["POST"])
@login_required
def notice_create():
    user = current_user()
    if user.get("role") != "admin":
        abort(403)
    title = (request.form.get("title") or "").strip()
    body = (request.form.get("body") or "").strip()
    if not title or not body:
        flash("공지 제목과 내용을 모두 입력해주세요.", "error")
        return redirect(url_for("notices.notices_view"))
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
    return redirect(url_for("notices.notices_view"))

@bp.route("/notices/<int:notice_id>/active", methods=["POST"])
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
    return redirect(url_for("notices.notices_view"))
