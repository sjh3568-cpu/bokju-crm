"""상담사 개인 할 일(To-Do).

app.py에서 분리(2026-09-15). 라우트 경로·동작은 그대로, 엔드포인트 이름만 "todos.<함수>"가 됐다.
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
    _add_months,
    _kr_holidays,
    _lunar_label,
    _valid_date,
    _valid_time,
    app,
)

logger = logging.getLogger(__name__)
bp = Blueprint("todos", __name__)

# ───────────────────── 상담사개인할일(To-Do) ─────────────────────

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

@bp.route("/todos")
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

@bp.route("/api/todos", methods=["POST"])
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

@bp.route("/api/todos/<int:tid>", methods=["POST"])
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

@bp.route("/api/todos/<int:tid>/toggle", methods=["POST"])
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

@bp.route("/api/todos/<int:tid>/carry", methods=["POST"])
@login_required
def api_todo_carry(tid):
    if not models.carry_todo_to(tid, g.user["id"], date.today().isoformat()):
        abort(404)
    return jsonify({"ok": True})

@bp.route("/api/todos/<int:tid>/delete", methods=["POST"])
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

@bp.route("/api/todos/<int:tid>/move", methods=["POST"])
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

@bp.route("/api/todos/reminders")
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
