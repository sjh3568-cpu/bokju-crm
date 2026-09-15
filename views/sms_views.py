"""문자 발송 — 발송 화면·템플릿·발송 API.

app.py에서 분리(2026-09-15). 라우트 경로·동작은 그대로, 엔드포인트 이름만 "sms.<함수>"가 됐다.
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
    _valid_date,
    app,
)

logger = logging.getLogger(__name__)
bp = Blueprint("sms", __name__)

# ───────────────────── 문자발송(5번요청) ─────────────────────

@bp.route("/sms")
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

@bp.route("/sms/templates")
@login_required
def sms_templates_view():
    return render_template(
        "sms_templates.html",
        templates=models.list_sms_templates(active_only=False),
    )

@bp.route("/api/sms/template", methods=["POST"])
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

@bp.route("/api/sms/template/<int:tid>", methods=["POST"])
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

@bp.route("/api/sms/template/<int:tid>", methods=["DELETE"])
@login_required
def api_sms_template_delete(tid):
    if not models.get_sms_template(tid):
        return jsonify({"error": "not found"}), 404
    models.delete_sms_template(tid)
    return jsonify({"ok": True})

@bp.route("/api/sms/send", methods=["POST"])
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
