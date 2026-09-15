"""통계·보고서 — /stats, 모병원·직원소개 분석, 월간보고서.

app.py에서 분리(2026-09-15). 라우트 경로·동작은 그대로, 엔드포인트 이름만 "stats.<함수>"가 됐다.
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
    _stats_period_from_request,
    app,
)

logger = logging.getLogger(__name__)
bp = Blueprint("stats", __name__)

# ───────────────────── 통계(Phase3) ─────────────────────

@bp.route("/stats")
@login_required
def stats_view():
    preset, date_from, date_to = _stats_period_from_request()
    consult_heat = dashboard_metrics.consult_weekday_hour_matrix(date_from, date_to)
    return render_template(
        "stats.html",
        preset=preset, date_from=date_from, date_to=date_to,
        consult_heat=consult_heat)

@bp.route("/api/stats.json")
@login_required
def api_stats():
    _, date_from, date_to = _stats_period_from_request()
    data = models.aggregate_stats(date_from, date_to)
    # 채널 문의(홈페이지·카카오톡) → 상담 → 입원 깔때기 — 유입경로 섹션 옆에 표시
    data["inbound_funnel"] = models.inbound_funnel(date_from, date_to)
    return jsonify(data)

@bp.route("/stats/hospitals")
@login_required
def hospital_stats_view():
    """모병원 전체의 상담의뢰·입원완료 성과 + 전기 대비·질환군·협력기관 여부·종별/지역 필터."""
    import hospital_analysis as ha
    import partnerships
    preset, date_from, date_to = _stats_period_from_request()
    q = (request.args.get("q") or "").strip()
    sort = request.args.get("sort") or "referrals"
    kind = (request.args.get("kind") or "").strip() or None
    region = (request.args.get("region") or "").strip() or None
    partner = (request.args.get("partner") or "").strip() or None
    data = ha.enrich(date_from, date_to, q=q or None)
    options = ha.filter_options(data["hospitals"])          # 필터 목록은 걸러내기 전 전체 기준
    data["hospitals"] = ha.apply_filters(data["hospitals"], kind=kind, region=region, partner=partner)
    data["hospital_count"] = len(data["hospitals"])
    key = ha.SORT_KEYS.get(sort, ha.SORT_KEYS["referrals"])
    data["hospitals"] = sorted(data["hospitals"], key=lambda h: (*key(h), h["name"]))
    return render_template(
        "stats_hospitals.html", preset=preset, date_from=date_from, date_to=date_to,
        q=q, sort=sort, kind=kind, region=region, partner=partner, options=options, data=data,
        csrf=partnerships._csrf(), can_edit_partners=menu_level(current_user(), "partners") >= PERM_EDIT,
    )

@bp.route("/stats/staff")
@login_required
def staff_referral_view():
    """직원소개를 소개자별로 집계. 전환율이 가장 높은 유입 경로라 따로 관리한다."""
    preset, date_from, date_to = _stats_period_from_request()
    q = (request.args.get("q") or "").strip()
    sort = request.args.get("sort") or "admissions"
    internal_only = request.args.get("internal") in ("1", "true", "yes")
    org = (request.args.get("org") or "").strip() or None
    if org and org not in STAFF_REFERRAL_ORGS:
        org = None
    data = models.staff_referral_overview(date_from, date_to, q=q or None,
                                          internal_only=internal_only, org=org)
    # 기간 빠른선택 — 직원소개는 누적 성과라 '이번 달' 기본으론 몇 건뿐이다.
    _t = date.today()
    quick_ranges = [
        {"label": "이번달", "from": _t.replace(day=1).isoformat(), "to": _t.isoformat()},
        {"label": "올해", "from": _t.replace(month=1, day=1).isoformat(), "to": _t.isoformat()},
        {"label": "최근 1년", "from": _t.replace(year=_t.year - 1).isoformat(), "to": _t.isoformat()},
        {"label": "전체", "from": "2023-01-01", "to": _t.isoformat()},
    ]
    keys = {"admissions": lambda r: (-r["admissions"], -r["referrals"]),
            "referrals": lambda r: (-r["referrals"], -r["admissions"]),
            "conversion": lambda r: (-r["conversion"], -r["admissions"])}
    data["referrers"] = sorted(data["referrers"],
                               key=lambda r: (*keys.get(sort, keys["admissions"])(r), r["name"]))
    return render_template(
        "stats_staff.html", preset=preset, date_from=date_from, date_to=date_to,
        q=q, sort=sort, data=data, internal_only=internal_only, org=org,
        quick_ranges=quick_ranges,
    )

@bp.route("/report/monthly")
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
    import hospital_analysis as ha
    return render_template(
        "report_monthly.html",
        data=data,
        hospital_section=ha.monthly_section(year, month),
        stay_section=models.stay_report(year, month),
        insight_enabled=bool(os.getenv("ANTHROPIC_API_KEY")),
    )

@bp.route("/api/report/monthly/insight", methods=["POST"])
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
            "pipeline_comment": "", "operation_comment": "", "hospital_comment": "", "alerts": [],
        }})
    try:
        from llm import summarize_monthly
        import hospital_analysis as ha
        data["hospital_section"] = ha.monthly_section(year, month)
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
