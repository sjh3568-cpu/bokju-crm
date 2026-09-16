"""상담 — 상담일지 화면·목록·상세·CSV, 상담 CRUD API, 결과/퇴원 워크플로, 대시보드 follow-up 토글, 자동완성, 기간 계산기·KRPG.

app.py에서 분리(2026-09-15). 라우트 경로·동작은 그대로, 엔드포인트 이름만 "consult.<함수>"가 됐다.
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
    PERIOD_CALC_CAP_YEARS,
    RECOVERY_STAY_DAYS,
    TOTAL_STAY_DAYS,
    _add_months,
    _bump_master_use_counts,
    _consult_fields_from_payload,
    _csv_list,
    _day_of,
    _list_filters_from_request,
    _parse_date,
    _recovery_status,
    _sync_lifecycle_stage,
    _sync_lifecycle_stage_if_unset,
    _valid_date,
    _validate_consult_payload,
    app,
    is_cns_diseases,
    noncns_stay_days,
    recovery_window_days,
)
from views.inbound import _norm_phone
from views.todos import _annotate_todos

logger = logging.getLogger(__name__)
bp = Blueprint("consult", __name__)

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

_KRPG_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "krpg_v22.json"   # 프로젝트 루트의 data/ (views/로 옮기며 경로 보정)

@lru_cache(maxsize=1)
def _krpg_data():
    with _KRPG_DATA_PATH.open(encoding="utf-8") as fp:
        return json.load(fp)

def _normalize_kcd(value):
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())

@bp.route("/tools/krpg")
@login_required
def krpg_lookup():
    """KRPG 2.2 사업대상 1,477개 KCD 코드 즉시 조회."""
    template = ("krpg_lookup_embed.html" if request.args.get("embed") == "1"
                else "krpg_lookup.html")
    return render_template(template, krpg_meta=_krpg_data())

@bp.route("/api/krpg/search")
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

@bp.route("/tools/period-calc")
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

@bp.route("/consult/new")
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
            # 채널 문의 → 유입경로(홈페이지/카카오톡 채널) 자동 체크 + 상담방법은 전화상담.
            # 채널 문의가 상담·입원으로 얼마나 이어졌는지 상담 통계 유입경로에서 바로 비교하기 위해.
            ref = INBOUND_CHANNEL_REFERRAL.get((comm.get("channel") or "").strip())
            prefill_consult = {"referral_source_detail": [ref] if ref else [],
                               "consult_channel": "전화상담"}
            if comm.get("patient_id"):
                patient = models.get_patient(comm["patient_id"])
            else:
                # 환자 미연결 — contact(연락처)·body로 보호자 정보 추론하여 가상 patient
                contact = (comm.get("contact") or "").strip()
                patient = {
                    "id": None, "name": "",
                    "guardian_phone": contact if contact else "",
                }

    # 팩스 자료함에서 '상담 등록' — AI가 읽은 이름·주병명·모병원을 미리 채운다 (doc_id)
    fax_doc = None
    try:
        doc_id = int(request.args.get("doc_id") or 0)
    except (ValueError, TypeError):
        doc_id = 0
    if doc_id and not patient:
        fax_doc = models.get_document(doc_id)
        if fax_doc and (fax_doc.get("status") or "") != "done":
            import json as _json
            try:
                ai = _json.loads(fax_doc.get("ai_json") or "{}")
            except ValueError:
                ai = {}
            if fax_doc.get("comm_id") and not inbox_comm:
                inbox_comm = models.get_communication(fax_doc["comm_id"])
            prefill_consult = dict(prefill_consult or {})
            prefill_consult.setdefault("consult_channel", "전화상담")
            prefill_consult.setdefault("referral_source_detail", ["기관연계"])
            if fax_doc.get("sender_ai"):
                prefill_consult["current_location_type"] = "입원중"
                prefill_consult["current_location_name"] = fax_doc["sender_ai"]
                prefill_consult["referrer_institution"] = fax_doc["sender_ai"]
            if fax_doc.get("diagnosis_ai"):
                prefill_consult["diagnosis"] = fax_doc["diagnosis_ai"]
            if fax_doc.get("patient_id"):
                patient = models.get_patient(fax_doc["patient_id"])
            else:
                patient = {"id": None, "name": fax_doc.get("patient_name_ai") or "",
                           "gender": (ai.get("sex") or "") if ai.get("sex") in ("남", "여") else "",
                           "guardian_phone": ""}
        else:
            fax_doc = None

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
                        "referrer_org", "referrer_dept",
                        "attending_doctor",
                    )
                    prefill_consult = {k: last.get(k) for k in SAFE_PREFILL if last.get(k)}

    return render_template("consult_form.html", consultation=None, patient=patient,
                           inbox_comm=inbox_comm, prefill=prefill_consult, fax_doc=fax_doc,
                           top_hospitals=models.top_source_hospitals())

@bp.route("/consult/<int:cid>")
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
                           admission_episodes=models.patient_admission_history(c["patient_id"]),
                           patient_todos=patient_todos, today_str=date.today().isoformat(),
                           LIFECYCLE_EVENT_TYPES=LIFECYCLE_EVENT_TYPES)

@bp.route("/consult/<int:cid>/print")
@login_required
def consult_print(cid):
    """종이 상담일지(2025 수정본) 양식 그대로 A4 1장 인쇄용 화면(2026-09-15).
    상세 페이지의 [상담일지 인쇄]가 새 탭으로 연다. ?auto=1이면 열리자마자 인쇄 대화상자."""
    from config import (INSURANCE_TYPES, REFERRAL_SOURCE_GROUPS,
                        WOUND_CARE_NOTE_FIELDS, SPECIAL_CARE_NOTE_FIELDS)
    c = models.get_consultation(cid)
    if not c:
        abort(404)
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="view_consult", target_type="consultation", target_id=cid,
        detail="print", ip=request.remote_addr,
    )
    return render_template(
        "consult_print.html", c=c,
        printed_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        INSURANCE_TYPES=INSURANCE_TYPES, REFERRAL_SOURCE_GROUPS=REFERRAL_SOURCE_GROUPS,
        WOUND_CARE_NOTE_FIELDS=WOUND_CARE_NOTE_FIELDS, SPECIAL_CARE_NOTE_FIELDS=SPECIAL_CARE_NOTE_FIELDS,
        CONSULT_CHANNELS=CONSULT_CHANNELS, CONSCIOUSNESS_MAIN_OPTIONS=CONSCIOUSNESS_MAIN_OPTIONS,
        CONVERSATION_LEVEL_OPTIONS=CONVERSATION_LEVEL_OPTIONS, HEARING_OPTIONS=HEARING_OPTIONS,
        ACTIVITY_ACTIVE_OPTIONS=ACTIVITY_ACTIVE_OPTIONS, ACTIVITY_DIAPER_OPTIONS=ACTIVITY_DIAPER_OPTIONS,
        ACTIVITY_WHEELCHAIR_OPTIONS=ACTIVITY_WHEELCHAIR_OPTIONS, CAREGIVER_OPTIONS=CAREGIVER_OPTIONS,
        BED_OPTIONS=BED_OPTIONS, ADMISSION_DOCS=ADMISSION_DOCS,
        COST_GUIDANCE_OPTIONS=COST_GUIDANCE_OPTIONS, INFO_PROVIDED_OPTIONS=INFO_PROVIDED_OPTIONS,
    )

@bp.route("/consult/<int:cid>/edit")
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

@bp.route("/consultations")
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

@bp.route("/api/quick-filters", methods=["POST"])
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

def _inquiry_filters():
    """채널 문의 내역 필터 — 기간 기본값은 이번 달."""
    today = date.today()
    date_from = _valid_date(request.args.get("from")) or today.replace(day=1).isoformat()
    date_to = _valid_date(request.args.get("to")) or today.isoformat()
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    channel = (request.args.get("channel") or "").strip()
    if channel not in INBOUND_CHANNEL_LABELS:
        channel = ""
    stage = (request.args.get("stage") or "").strip()
    if stage not in ("미처리", "상담등록", "처리완료"):
        stage = ""
    return {"date_from": date_from, "date_to": date_to, "channel": channel,
            "stage": stage, "q": (request.args.get("q") or "").strip()[:100]}

@bp.route("/consultations/inquiries")
@login_required
def inquiry_list():
    """채널 문의 내역 — 홈페이지·카카오톡 등으로 들어온 문의 전체 기록과 '문의 → 상담' 전환 집계.
    미처리 큐(대시보드 오늘 처리 필요)와 달리 완료된 것도 남고 기간별로 본다.
    상담·입원 통계는 여기서 내지 않는다 — 상담일지 하나만 기준(유입경로 항목으로 비교)."""
    f = _inquiry_filters()
    rows = models.inquiry_rows(date_from=f["date_from"], date_to=f["date_to"],
                               channel=f["channel"], stage=f["stage"], q=f["q"])
    all_rows = rows if not f["stage"] else models.inquiry_rows(
        date_from=f["date_from"], date_to=f["date_to"], channel=f["channel"], q=f["q"])
    summary = models.inquiry_summary(all_rows)
    # 월별 추이 — 선택 기간의 끝 달까지, 기간을 덮되 최소 6개월(상한 24). 짧은 기간을 골라도 추이 맥락은 남긴다.
    d_from, d_to = date.fromisoformat(f["date_from"]), date.fromisoformat(f["date_to"])
    span = (d_to.year - d_from.year) * 12 + (d_to.month - d_from.month) + 1
    months = min(24, max(6, span))
    monthly = models.inquiry_monthly(months, end=d_to)
    return render_template(
        "inquiries.html", rows=rows, f=f, summary=summary, monthly=monthly, months=months,
        channel_labels=INBOUND_CHANNEL_LABELS,
        monthly_max=max([m["total"] for m in monthly] + [1]),
        admin_ready=homepage_board.admin_configured(),
    )

@bp.route("/consultations/inquiries.csv")
@login_required
def inquiry_csv():
    f = _inquiry_filters()
    rows = models.inquiry_rows(date_from=f["date_from"], date_to=f["date_to"],
                               channel=f["channel"], stage=f["stage"], q=f["q"], limit=100000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["일시", "채널", "환자", "연락처", "제목", "내용", "단계", "홈페이지 답변자", "답변 시각",
                "처리 시각", "상담일", "상담자", "상담 결과", "입원 진행", "상담 번호"])
    for r in rows:
        w.writerow([
            r.get("when") or "", INBOUND_CHANNEL_LABELS.get(r.get("channel"), r.get("channel") or ""),
            r.get("patient_name") or "", r.get("contact") or "", r.get("summary") or "",
            (r.get("body") or "").replace("\r", " ").replace("\n", " "), r.get("stage") or "",
            r.get("answered_by") or "", (r.get("answered_at") or "")[:16], (r.get("resolved_local") or "")[:16],
            r.get("consult_date") or "", r.get("counselor") or "", r.get("consult_result") or "",
            r.get("admission_status") or "", r.get("consultation_id") or "",
        ])
    models.log_audit(user_id=g.user["id"], username=g.user["username"], action="export_csv",
                     target_type="inquiries", detail=str(len(rows)), ip=request.remote_addr)
    data = buf.getvalue().encode("utf-8-sig")
    return send_file(io.BytesIO(data), mimetype="text/csv", as_attachment=True,
                     download_name=f"inquiries_{f['date_from']}_{f['date_to']}.csv")

@bp.route("/consultations.csv")
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

@bp.route("/patients/<int:pid>")
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


# ───────────────────── API:상담CRUD ─────────────────────

@bp.route("/api/consult-drafts", methods=["GET", "POST"])
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

@bp.route("/api/consult-drafts/<int:draft_id>", methods=["DELETE"])
@login_required
def api_consult_draft_delete(draft_id):
    if not models.delete_consultation_draft(g.user["id"], draft_id):
        return jsonify({"error": "임시저장본을 찾을 수 없습니다."}), 404
    return jsonify({"ok": True})

def _insurance_text(value):
    """보험유형 입력 정규화 — 폼은 복수 체크박스라 리스트로 오고, 명부 적재·LLM은 문자열로 온다.
    INSURANCE_TYPES 순서로 ', ' 이어 한 칸에 저장한다(표시·필터·통계가 모두 이 형태를 전제)."""
    if isinstance(value, (list, tuple)):
        picked = {str(v).strip() for v in value if str(v).strip()}
        ordered = [t for t in INSURANCE_TYPES if t in picked]
        ordered += sorted(v for v in picked if v not in INSURANCE_TYPES)   # 목록에 없는 옛 값도 버리지 않는다
        return ", ".join(ordered) or None
    if isinstance(value, str):
        return value.strip() or None
    return value

@bp.route("/api/consult", methods=["POST"])
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
        insurance_type=_insurance_text(p.get("insurance_type")),
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
    if cfields.get("admission_status") == "입원예정":
        missing = models.planned_admission_missing(cfields)
        if missing:
            return jsonify({"error": _planned_missing_msg(missing)}), 400
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
    # 팩스 자료함에서 등록 → 문서를 상담·환자에 연결하고 처리 완료
    try:
        doc_id = int(request.args.get("doc_id") or 0)
    except (ValueError, TypeError):
        doc_id = 0
    if doc_id and models.get_document(doc_id):
        models.update_document(doc_id, status="done", consultation_id=cid, patient_id=pid)
        fdoc = models.get_document(doc_id)
        if fdoc.get("comm_id"):
            models.update_communication(fdoc["comm_id"], status="done", consultation_id=cid, patient_id=pid)
        models.log_audit(
            user_id=g.user["id"], username=g.user["username"],
            action="link_document", target_type="document",
            target_id=doc_id, detail=f"→ consult #{cid}", ip=request.remote_addr,
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

@bp.route("/api/consult/ai-fill", methods=["POST"])
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

@bp.route("/api/consult/<int:cid>", methods=["POST"])
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
            if k == "insurance_type":
                valid[k] = _insurance_text(v)
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
    # 입원예정으로 바꾸거나, 입원예정인 상담의 예정일·주치의·병실을 건드리는 저장은 셋 다 채워져 있어야 한다.
    touched = {"admission_status", *(k for k, _ in models.PLANNED_ADMISSION_REQUIRED)} & set(update_fields)
    merged = {**existing, **update_fields}
    if touched and merged.get("admission_status") == "입원예정":
        missing = models.planned_admission_missing(merged)
        if missing:
            return jsonify({"error": _planned_missing_msg(missing)}), 400
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


# ───────────────────── API:결과(입원진행단계)변경 ─────────────────────

def _planned_missing_msg(missing):
    return "입원예정에는 입원예정일·주치의·병실이 모두 필요합니다. 누락: " + ", ".join(missing)


@bp.route("/api/consult/<int:cid>/status", methods=["POST"])
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
                except ValueError:
                    return jsonify({"error": "입원일자 형식 오류"}), 400
                # 화면·집계는 모두 COALESCE(actual_admission_date, admission_date)를 본다.
                # 재입원(8/25 입원 → 9/9 퇴원 → 오늘 재입원)이면 옛 실제입원일이 남아 있어
                # 새 입원일이 무시되고 입원 환자 현황·오늘 완료 집계에서 사라졌다(2026-09-15).
                # → 두 칸을 함께 맞춘다. 실제 입원일이 곧 이 처리의 사실이다.
                fields["admission_date"] = adate
                fields["actual_admission_date"] = adate
        elif status in ("입원예정", "입원대기"):
            # 입원예정을 고르는 그 자리에서 예정일·시간까지 한 번에 — 헤더 칸을 따로 고치러 갈 필요 없게(2026-09-14).
            pdate = (payload.get("planned_admission_date") or "").strip()
            ptime = (payload.get("planned_admission_time") or "").strip()
            if pdate:
                try:
                    datetime.strptime(pdate, "%Y-%m-%d")
                except ValueError:
                    return jsonify({"error": "입원예정일 형식 오류"}), 400
                fields["planned_admission_date"] = pdate
                audit.append(f"예정일:{pdate}")
            elif status == "입원예정" and "planned_admission_date" in payload:
                return jsonify({"error": "입원예정일을 입력하세요."}), 400
            if "planned_admission_time" in payload:
                if ptime and not re.fullmatch(r"\d{2}:\d{2}", ptime):
                    return jsonify({"error": "입원예정 시간 형식 오류(HH:MM)"}), 400
                fields["planned_admission_time"] = ptime or None
            # 주치의·병실도 같은 자리에서(2026-09-15). 키가 온 것만 반영, 빈값이면 비운다.
            for key, label in (("attending_doctor", "주치의"), ("room_number", "병실")):
                if key in payload:
                    val = (payload.get(key) or "").strip()
                    if len(val) > 60:
                        return jsonify({"error": f"{label} 값이 너무 깁니다."}), 400
                    fields[key] = val or None
                    if val:
                        audit.append(f"{label}:{val}")
            if status == "입원예정":
                merged = {**existing, **fields}
                missing = models.planned_admission_missing(merged)
                if missing:
                    return jsonify({"error": _planned_missing_msg(missing)}), 400
                # 재입원: 이전 입원(이미 퇴원한 회차)의 실제입원일이 상담에 남아 있으면 비운다.
                # 남겨 두면 회차 상태가 '재원'으로 보이고, 완료 처리 전까지 옛 날짜가 화면·집계에 섞인다.
                stale = (existing.get("actual_admission_date") or existing.get("admission_date") or "").strip()
                if stale and models.has_closed_episode_on(existing["patient_id"], stale):
                    fields["actual_admission_date"] = None
                    fields["admission_date"] = None
                    audit.append(f"이전 입원일 {stale} 초기화(재입원)")
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


# ───────────────────── API:퇴원워크플로(상담목록) ─────────────────────

@bp.route("/api/consult/<int:cid>/discharge", methods=["POST"])
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


# ───────────────────── 대시보드follow-up토글(회복기전환보호자연락/퇴원1차면담) ─────────────────────

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

@bp.route("/api/consult/<int:cid>/recovery-call", methods=["POST"])
@login_required
def api_consult_recovery_call(cid):
    """회복기→비회복기 전환 D-30 환자의 보호자 재상담 완료 마킹 토글."""
    return _toggle_follow_up(cid, "recovery_call_at", "recovery_call", "recovery_call_by")

@bp.route("/api/consult/<int:cid>/discharge-sms", methods=["POST"])
@login_required
def api_consult_discharge_sms(cid):
    """퇴원예정 D-30 환자의 보호자 퇴원 안내 문자 발송 확인 토글."""
    return _toggle_follow_up(cid, "discharge_sms_at", "discharge_sms", "discharge_sms_by")

@bp.route("/api/consult/<int:cid>/discharge-interview", methods=["POST"])
@login_required
def api_consult_discharge_interview(cid):
    """퇴원예정 D-30 환자의 1차 병동 면담 완료 마킹 토글."""
    return _toggle_follow_up(cid, "discharge_interview_at", "discharge_interview")

@bp.route("/api/consult/<int:cid>", methods=["DELETE"])
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


# ───────────────────── API:자동완성 ─────────────────────

@bp.route("/api/room-status")
@login_required
def api_room_status():
    """상담일지에서 병실을 적을 때 재원 현황과 맞춰 본다 — 만실·성별 불일치·다른 입원예정과 겹침(2026-09-14 요청).
    ?room=316호&gender=F&exclude=<상담id>"""
    room = (request.args.get("room") or "").strip()
    if not room:
        return jsonify({"ok": True, "known": False})
    info = dashboard_metrics.room_status(
        room, gender=(request.args.get("gender") or "").strip() or None,
        exclude_cid=request.args.get("exclude", type=int))
    return jsonify(info)

@bp.route("/api/autocomplete/hospital")
@login_required
def api_ac_hospital():
    q = (request.args.get("q") or "").strip()
    if len(q) < 1:
        return jsonify({"items": [], "master_size": 0})
    # autocomplete_hospitals는 {items, master_size} 반환
    return jsonify(models.autocomplete_hospitals(q, limit=50))

@bp.route("/api/hospital/lookup")
@login_required
def api_hospital_lookup():
    """상담일지에서 마스터에 없는 병원을 심평원에서 바로 찾기(2026-09-15).
    기관협력 화면까지 가지 않고 그 자리에서 후보를 골라 등록한다."""
    import hira_sync
    q = (request.args.get("q") or "").strip()
    key = hira_sync.service_key()
    if not key:
        return jsonify({"items": [], "configured": False})
    if len(q) < 2:
        return jsonify({"items": [], "configured": True})
    try:
        items = hira_sync.lookup(key, q)
    except Exception as e:  # noqa: BLE001 — 외부 API 장애는 화면에 사유만 보여주고 폼은 막지 않는다
        logger.warning("심평원 조회 실패 %r: %s", q, e)
        return jsonify({"items": [], "configured": True, "error": "심평원 조회에 실패했습니다. 잠시 뒤 다시 시도하세요."}), 502
    return jsonify({"items": items, "configured": True})

@bp.route("/api/hospital/register", methods=["POST"])
@login_required
def api_hospital_register():
    """병원 한 곳을 마스터에 등록 — 심평원 후보(official_code 있음) 또는 입력한 이름 그대로(manual).
    상담 작성 권한이 있으면 누구나. 감사 로그에 남긴다."""
    import hira_sync
    if menu_level(g.user, "consult") < PERM_EDIT:
        return jsonify({"error": "상담 작성 권한이 필요합니다."}), 403
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if len(name) < 2 or len(name) > 100:
        return jsonify({"error": "병원 이름을 2~100자로 입력하세요."}), 400
    entry = {
        "name": name,
        "kind": (data.get("kind") or "").strip() or None,
        "region": (data.get("region") or "").strip() or None,
        "address": (data.get("address") or "").strip() or None,
        "phone": (data.get("phone") or "").strip() or None,
        "official_code": (data.get("official_code") or "").strip() or None,
    }
    source = "hira-lookup" if entry["official_code"] else "manual"
    saved = hira_sync.register_one(entry, source=source)
    models.log_audit(user_id=g.user["id"], username=g.user.get("username"), action="hospital_register",
                     target_type="hospital", detail=f"{saved} ({source})", ip=request.remote_addr)
    return jsonify({"ok": True, "name": saved, "source": source})

@bp.route("/api/autocomplete/nursing")
@login_required
def api_ac_nursing():
    q = (request.args.get("q") or "").strip()
    if len(q) < 1:
        return jsonify({"items": [], "master_size": 0})
    return jsonify(models.autocomplete_nursing_homes(q, limit=50))

@bp.route("/api/nursing/lookup")
@login_required
def api_nursing_lookup():
    """상담일지 '환자상태 → 요양원'에서 마스터에 없는 요양원을 공단 장기요양기관 명부에서 바로 찾기(2026-09-16).
    병원의 /api/hospital/lookup과 같은 흐름 — 후보를 골라 그 자리에서 등록한다."""
    import ltci_sync
    q = (request.args.get("q") or "").strip()
    key = ltci_sync.service_key()
    if not key:
        return jsonify({"items": [], "configured": False})
    if len(q) < 2:
        return jsonify({"items": [], "configured": True})
    try:
        items = ltci_sync.lookup(key, q)
    except Exception as e:  # noqa: BLE001 — 외부 API 장애는 화면에 사유만 보여주고 폼은 막지 않는다
        logger.warning("공단 요양원 조회 실패 %r: %s", q, e)
        return jsonify({"items": [], "configured": True, "error": "공단 조회에 실패했습니다. 잠시 뒤 다시 시도하세요."}), 502
    return jsonify({"items": items, "configured": True})

@bp.route("/api/nursing/register", methods=["POST"])
@login_required
def api_nursing_register():
    """요양원 한 곳을 마스터에 등록 — 공단 후보(official_code 있음) 또는 입력한 이름 그대로(manual)."""
    import ltci_sync
    if menu_level(g.user, "consult") < PERM_EDIT:
        return jsonify({"error": "상담 작성 권한이 필요합니다."}), 403
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if len(name) < 2 or len(name) > 100:
        return jsonify({"error": "요양원 이름을 2~100자로 입력하세요."}), 400
    entry = {
        "name": name,
        "kind": (data.get("kind") or "").strip() or None,
        "region": (data.get("region") or "").strip() or None,
        "address": (data.get("address") or "").strip() or None,
        "phone": (data.get("phone") or "").strip() or None,
        "official_code": (data.get("official_code") or "").strip() or None,
    }
    source = "ltci-lookup" if entry["official_code"] else "manual"
    saved = ltci_sync.register_one(entry, source=source)
    models.log_audit(user_id=g.user["id"], username=g.user.get("username"), action="nursing_register",
                     target_type="nursing_home", detail=f"{saved} ({source})", ip=request.remote_addr)
    return jsonify({"ok": True, "name": saved, "source": source})

@bp.route("/api/autocomplete/diagnosis")
@login_required
def api_ac_diagnosis():
    q = (request.args.get("q") or "").strip()
    if len(q) < 1:
        return jsonify({"items": []})
    return jsonify({"items": models.autocomplete_diagnoses(q, limit=10)})

@bp.route("/api/autocomplete/patient")
@login_required
def api_ac_patient():
    q = (request.args.get("q") or "").strip()
    if len(q) < 1:
        return jsonify({"items": []})
    return jsonify({"items": models.autocomplete_patients(q, limit=10)})

@bp.route("/api/patient/<int:pid>/minicard")
@login_required
def api_patient_minicard(pid):
    info = models.patient_minicard(pid)
    if not info:
        return jsonify({"error": "not_found"}), 404
    return jsonify(info)

@bp.route("/api/patient/<int:pid>/blacklist-info")
@login_required
def api_patient_blacklist_info(pid):
    info = models.patient_blacklist_info(pid)
    if not info:
        return jsonify({"error": "not_found"}), 404
    return jsonify(info)

@bp.route("/api/patients/by-name")
@login_required
def api_patients_by_name():
    name = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"items": []})
    return jsonify({"items": models.patients_by_name(name)})

@bp.route("/api/patient/merge", methods=["POST"])
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
