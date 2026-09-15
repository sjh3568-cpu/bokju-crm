"""메인 — 대시보드(/), 통합 달력, 주간 현황, healthz, 도움말.

app.py에서 분리(2026-09-15). 라우트 경로·동작은 그대로, 엔드포인트 이름만 "main.<함수>"가 됐다.
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
    DASHBOARD_DUE_WINDOW_DAYS,
    WARD_BED_CAPACITY,
    _admission_expiry,
    _care_phase,
    _dashboard_action_queue,
    _dashboard_disease_labels,
    _dashboard_elapsed_label,
    _dashboard_groups,
    _dashboard_inbound_bucket,
    _dashboard_parse_datetime,
    _dashboard_ward_label,
    _discharge_watch,
    _kr_holidays,
    _lunar_label,
    _valid_date,
    app,
)
from views.ward import _recovery_ratio_spark, _roster_care_phase, _ward_row_from_episode

logger = logging.getLogger(__name__)
bp = Blueprint("main", __name__)

# ───────────────────── 메인 ─────────────────────

def _dashboard_calendar_context(uid, year, month, counselor=None):
    """상담·입퇴원 일정과 개인/공유 ToDo를 합친 월간 달력.
    counselor 지정 시 상담·입퇴원 일정은 그 상담사 담당 건만 (내 담당만 보기)."""
    first = date(year, month, 1)
    start = first - timedelta(days=(first.weekday() + 1) % 7)
    days = [start + timedelta(days=i) for i in range(42)]
    last = days[-1]
    buckets = {d.isoformat(): [] for d in days}

    def add(day, kind, title, meta="", href="#", time="", done=False, sub=""):
        key = (day or "")[:10]
        if key not in buckets:
            return
        buckets[key].append({"kind": kind, "title": title, "meta": meta,
                             "href": href, "time": (time or "")[:5], "done": done, "sub": sub})

    def patient_sub(row):
        """이름 옆 부가정보 — 성별/나이 · 주상병 (달력 칸엔 툴팁, 날짜 패널엔 작은 글씨)."""
        g = {"M": "남", "F": "여"}.get(row.get("gender") or "", "")
        age = row.get("patient_age")
        who = "/".join(v for v in (g, f"{age}세" if age else "") if v)
        rec = dict(row)
        if isinstance(rec.get("diseases"), str):
            try:
                rec["diseases"] = json.loads(rec["diseases"] or "[]")
            except ValueError:
                rec["diseases"] = []
        labels = _dashboard_disease_labels(rec)
        dx = "" if labels == ["병명 미지정"] else labels[0]
        return " · ".join(v for v in (who, dx[:18]) if v)

    for row in models.dashboard_calendar_rows(start.isoformat(), last.isoformat(), counselor):
        name = row.get("patient_name") or "환자 미지정"
        href = f"/consult/{row['id']}"
        sub = patient_sub(row)
        add(row.get("consult_date"), "consult", name,
            "상담" + (f" · {row['counselor']}" if row.get("counselor") else ""),
            href, row.get("consult_time"), sub=sub)
        actual = row.get("actual_admission_date") or row.get("admission_date")
        planned = row.get("planned_admission_date")
        if actual:
            add(actual, "admitted", name, "입원", href, row.get("planned_admission_time"), sub=sub)
        if planned and (not actual or planned != actual):
            add(planned, "admission", name, "입원예정", href, row.get("planned_admission_time"), sub=sub)
        discharged = row.get("discharge_date")
        discharge_due = row.get("discharge_due_date")
        if discharged:
            add(discharged, "discharged", name, "퇴원", href, sub=sub)
        elif discharge_due:
            add(discharge_due, "discharge", name, "퇴원예정", href, sub=sub)

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

    # 회복기 전환일 — 재원 중 환자의 회복기 수가 만료일(30일 전 보호자 안내의 기준일). 달력에서 켜고 끌 수 있다.
    try:
        _census, residents = _dashboard_residents()
        for con in residents:
            if counselor and (con.get("counselor") or "").strip() != counselor:
                continue
            ax = _admission_expiry(con)
            bd = ax.get("billing_date") if ax else None
            if bd:
                add(bd, "recovery", con.get("patient_name") or "환자 미지정", "회복기 전환", f"/consult/{con['id']}",
                    sub=patient_sub(con))
    except Exception:
        logger.exception("달력 회복기 전환일 계산 실패")

    order = {"admission": 0, "admitted": 0, "discharge": 1, "discharged": 1, "recovery": 1,
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

def _ward_status_strip(census=None):
    """대시보드 최상단 '현재 상태' 스트립 지표.

    재원·회복기 비율은 /ward와 같은 근거(원무 명부 census + _care_phase 판정)로
    내 두 화면의 숫자가 어긋나지 않게 한다. /ward 라우트 로직은 건드리지 않고
    검증된 헬퍼만 재사용한다. 명부가 없으면(has_roster=False) 명부 의존 지표는
    None으로 두고 화면이 '명부 필요'로 대신 표시한다.
    """
    today = date.today()
    week_from = (today - timedelta(days=today.weekday())).isoformat()   # 월요일
    week_to = (today - timedelta(days=today.weekday()) + timedelta(days=6)).isoformat()
    month_from = today.replace(day=1).isoformat()
    month_to = today.isoformat()   # 이번 달은 오늘까지(미래 예정 입원은 제외)
    flow = models.admission_flow_counts(week_from, week_to, month_from, month_to)
    if census is None:
        census = models.current_admission_census()
    strip = {
        "has_roster": bool(census.get("has_roster")),
        "away": len(models.away_now()),        # 외진 중 — 명부와 무관
        "bed_capacity": WARD_BED_CAPACITY,
        "week_in": flow["week_in"], "week_out": flow["week_out"],
        "month_in": flow["month_in"], "month_out": flow["month_out"],
        "admitted": None, "bed_occupancy": None,
        "recovery_ratio": None, "recovery_ratio_ok": None,
        "recovery": None, "recovery_judged": None,
    }
    if not census.get("has_roster"):
        return strip
    # 재원자 각각을 /ward와 같은 방식으로 회복기 판정한다. 상담이 붙은 회차는 그
    # 상담에 명부 값(수가구분·재활종료일)을 얹어서, 상담 없이 입원한 회차(orphans)는
    # 명부 값만으로 행을 만들어서 — 둘 다 _care_phase를 거친다.
    trend_rows = {c["id"]: c for c in models.list_consultations(ids=list(census["by_consultation"]), limit=10000)}
    recovery_n = total_n = 0
    for cid, ep in census["by_consultation"].items():
        base = trend_rows.get(cid)
        if not base:
            continue
        c = dict(base)
        c["roster_care_phase"] = _roster_care_phase(ep.get("care_type"))
        c["rehab_end_date"] = ep.get("rehab_end_date")
        c["rehab_end_imported"] = ep.get("rehab_end_imported")
        total_n += 1
        if _care_phase(c).get("care_phase") == "회복기":
            recovery_n += 1
    for ep in census["orphans"]:
        total_n += 1
        if _care_phase(_ward_row_from_episode(ep)).get("care_phase") == "회복기":
            recovery_n += 1
    # /ward KPI 카드와 같은 정의 — 전체 재원 대비 회복기 인원.
    ratio = round(recovery_n / total_n * 100, 2) if total_n else 0
    strip.update(
        admitted=total_n,
        bed_occupancy=round(total_n / WARD_BED_CAPACITY * 100, 1) if WARD_BED_CAPACITY else None,
        recovery=recovery_n,
        recovery_judged=total_n,
        recovery_ratio=ratio,
        recovery_ratio_ok=(ratio >= 40),
    )
    return strip

@bp.route("/calendar")
@login_required
def calendar_page():
    """통합 달력 — 대시보드에서 분리한 자기 페이지. 월 이동·내 담당/전체는 쿼리로."""
    try:
        cal_year = int(request.args.get("cal_year") or date.today().year)
        cal_month = int(request.args.get("cal_month") or date.today().month)
        if not 2000 <= cal_year <= 2100:
            raise ValueError
        date(cal_year, cal_month, 1)
    except (TypeError, ValueError):
        cal_year, cal_month = date.today().year, date.today().month
    prefs = models.get_user_by_id(g.user["id"]).get("preferences_data", {})
    cal_default = "1" if prefs.get("calendar_mine", True) else "0"
    cal_mine = request.args.get("cal_mine", cal_default) != "0"
    cal_counselor = g.user.get("display_name") if cal_mine else None
    return render_template("calendar.html",
                           **_dashboard_calendar_context(g.user["id"], cal_year, cal_month, cal_counselor))

@bp.route("/report/weekly")
@login_required
def report_weekly():
    """주간 상담 현황 — 대시보드에서 분리. 집계는 dashboard_summary의 weekly_report 그대로."""
    data = models.dashboard_summary()
    return render_template("report_weekly.html", weekly_report=data["weekly_report"])

def _recovery_projection(strip, recovery_due, discharge_due, horizon=7):
    """회복기 비율 7일 전망 — 만료 예정·퇴원 예정만 반영한 보수적 추정.

    입원 예정 환자의 회복기 여부는 입원 전엔 확정이 아니라 넣지 않는다. 그래서 실제는
    이 값보다 좋아지면 좋아졌지 나빠지진 않는다. margin은 지금 비율이 40% 아래로
    떨어지기까지 회복기 환자가 몇 명 빠져도 되는지(전환·퇴원 합계).
    """
    if not strip.get("has_roster") or not strip.get("admitted"):
        return None
    rec, tot = strip["recovery"] or 0, strip["admitted"]
    expiring = [x for x in recovery_due if 0 <= (x["watch"].get("billing_left") or 0) <= horizon]
    leaving = [x for x in discharge_due if 0 <= (x["watch"].get("days_left") or 0) <= horizon]
    leaving_rec = sum(1 for x in leaving if _care_phase(x["con"]).get("care_phase") == "회복기")
    rec2 = max(0, rec - len(expiring) - leaving_rec)
    tot2 = max(1, tot - len(leaving))
    ratio = round(rec2 / tot2 * 100, 2)
    margin = rec - (-(-40 * tot // 100))   # ceil(0.4 * tot)
    return {"ratio": ratio, "ok": ratio >= 40, "horizon": horizon,
            "expiring": len(expiring), "leaving": len(leaving), "margin": margin}

def _dashboard_residents():
    """지금 재원 중인 상담(입원완료) 목록 — 원무 명부 census 기준. (census, residents)를 돌려준다.
    대시보드 기한 임박·오늘 처리 필요와 통합 달력(회복기 전환일)이 같은 목록을 쓴다."""
    census = models.current_admission_census()
    admitted = models.list_consultations(
        admission_status="입원완료", limit=10000,
        ids=list(census["by_consultation"]) if census["has_roster"] else None)   # 재원만 읽는다(전체 X)
    if census["has_roster"]:
        # census 재원만 남기고, 회복기·만료 계산이 /ward와 같아지도록 명부값
        # (실제 입원일·수가구분·재활종료일·발병일)을 상담에 얹는다.
        residents = []
        for c in admitted:
            ep = census["by_consultation"].get(c["id"])
            if not ep:
                continue
            c = dict(c)
            c["actual_admission_date"] = ep["admitted_at"]
            c["discharge_date"] = None
            c["roster_care_phase"] = _roster_care_phase(ep.get("care_type"))
            c["rehab_end_date"] = ep.get("rehab_end_date")
            c["rehab_end_imported"] = ep.get("rehab_end_imported")
            c["onset_date"] = ep.get("onset_date")
            c["roster_diagnosis"] = ep.get("diagnosis_name")
            residents.append(c)
        admitted = residents
    return census, admitted

@bp.route("/")
@login_required
def dashboard():
    today_d = date.today()
    legacy_admission_date = request.args.get("admission_date")
    # 기본 조회 기간은 '이번 주(월~일)' — 빠른 조회 버튼 대신(2026-09-14 요청). 기간 입력으로 바꿀 수 있다.
    week_mon = today_d - timedelta(days=today_d.weekday())
    week_sun = week_mon + timedelta(days=6)
    admission_from = _valid_date(
        request.args.get("admission_from") or legacy_admission_date, week_mon.isoformat())
    admission_to = _valid_date(
        request.args.get("admission_to") or legacy_admission_date,
        admission_from if (request.args.get("admission_from") or legacy_admission_date) else week_sun.isoformat())
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
        "admission_lookup_label": (
            f"이번 주 {admission_date_label(admission_from)} ~ {admission_date_label(admission_to)}"
            if (admission_from, admission_to) == (week_mon.isoformat(), week_sun.isoformat()) else
            admission_date_label(admission_from) if admission_from == admission_to else
            f"{admission_date_label(admission_from)} ~ {admission_date_label(admission_to)}"),
    })
    open_comms = models.inbox_open_communications()
    # 홈페이지 상담게시판에서 온 문의는 인박스에서 바로 '답변'할 수 있게 게시글 idx를 붙인다.
    hp_posts = models.homepage_post_map([m.get("id") for m in open_comms])
    for m in open_comms:
        hp = hp_posts.get(m.get("id"))
        m["homepage_idx"] = hp["idx"] if hp else None
    callbacks = models.inbox_callbacks()

    # 입원예정 상태인데 planned_admission_date가 비어 있는 상담 — 액션큐에 표시.
    planned_consults = models.list_consultations(admission_status="입원예정", limit=10000)
    planned_missing_date = [
        c for c in planned_consults
        if not (c.get("planned_admission_date") or "").strip()
    ]

    # 입원완료 환자 중 회복기→비회복기 전환 D-30, 퇴원예정 D-30.
    # 상담의 '입원완료'는 퇴원해도 안 바뀐다(discharge_date가 한 건도 없다). 그대로
    # 돌리면 2023년에 퇴원한 환자까지 '퇴원 예정'으로 잡혀 재원 264명에 큐가 652건이
    # 됐다. 현황 스트립·/ward와 같은 원무 명부 census로 지금 재원인 상담만 남기고,
    # 만료일이 창(±DASHBOARD_DUE_WINDOW_DAYS) 안인 것만 담는다. 명부가 없는 환경은
    # 상담 상태로 대체한다.
    census, admitted = _dashboard_residents()
    window = DASHBOARD_DUE_WINDOW_DAYS
    recovery_transition_due = []
    discharge_due = []
    for con in admitted:
        disease_labels = _dashboard_disease_labels(con)
        con["disease_summary"] = "" if disease_labels == ["병명 미지정"] else ", ".join(disease_labels[:3])
        con["ward"] = _dashboard_ward_label(con.get("room_number"))
        ax = _admission_expiry(con)
        # 회복기→비회복기 전환 30일 전 안내 대상 — 이미 전환된(음수) 환자는 안내 시점이 지났으므로 뺀다.
        if ax and ax.get("billing_left") is not None and 0 <= ax["billing_left"] <= window:
            recovery_transition_due.append({"con": con, "watch": ax})
        dw = _discharge_watch(con)
        # 기한 임박(앞으로 30일)과 초과분(오늘 처리 필요)을 한 목록에 담고, 화면에서 나눈다.
        if dw and dw.get("days_left") is not None and -window <= dw["days_left"] <= window:
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
    # 퇴원 예정: 예정일이 지난 재원(초과)은 '오늘 처리 필요' 큐로, 앞으로 30일은 '기한 임박' 카드로.
    discharge_all = discharge_due
    discharge_due = [x for x in discharge_due if (x["watch"].get("days_left") or 0) >= 0]
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
        data, open_comms, callbacks, recovery_transition_due, discharge_all,
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
    data["ward_strip"] = _ward_status_strip(census)
    # KPI 8장 — 지난주 같은 요일 비교·7일 스파크라인·병동별 재원·30일 입퇴원·요일 히트맵
    data["metrics"] = dashboard_metrics.kpi_metrics(today_d)
    data["metrics"]["ratio"] = {"spark": _recovery_ratio_spark(data["metrics"]["spark_dates"])}
    data["month_kpi"] = dashboard_metrics.month_performance(today_d)
    data["ward_occupancy"] = dashboard_metrics.ward_occupancy()
    data["unassigned_planned"] = dashboard_metrics.unassigned_planned(2, today_d)
    data["consult_heat"] = dashboard_metrics.consult_weekday_matrix(8, today_d)
    data["discharges_today"] = dashboard_metrics.discharges_on(today_d.isoformat())
    data["recovery_projection"] = _recovery_projection(
        data["ward_strip"], recovery_transition_due, discharge_due)
    # 인바운드 경과 — 1시간 넘긴 문의는 화면에서 따로 강조한다.
    now_dt = datetime.now()
    for m in open_comms:
        occurred = _dashboard_parse_datetime(m.get("occurred_at") or m.get("created_at"))
        mins = int((now_dt - occurred).total_seconds() // 60) if occurred else None
        m["elapsed_min"] = mins
        m["elapsed_label"] = _dashboard_elapsed_label(occurred) if occurred else ""
        m["over_1h"] = bool(mins is not None and mins >= 60)
    data["inbound_over_1h"] = sum(1 for m in open_comms if m.get("over_1h"))
    data["today_weekday"] = "월화수목금토일"[today_d.weekday()]
    return render_template("dashboard.html", **data)

@bp.route("/healthz")
def healthz():
    return {"ok": True}

@bp.route("/help")
@login_required
def help_manual():
    """상담 표준 지침 + 현재 CRM 메뉴별 작성·등록·관리 매뉴얼."""
    return render_template("help.html")
