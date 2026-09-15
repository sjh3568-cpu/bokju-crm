"""재원 관리·생애주기 — /ward(재원 현황·명단 partial), 외진·입원 확정·호실, 환자 단계·태그·블랙리스트.

app.py에서 분리(2026-09-15). 라우트 경로·동작은 그대로, 엔드포인트 이름만 "ward.<함수>"가 됐다.
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
    TOTAL_STAY_DAYS,
    _add_months,
    _admission_expiry,
    _care_phase,
    _dashboard_ward_label,
    _day_of,
    _discharge_watch,
    _recovery_status,
    _set_lifecycle_stage_clinical,
    _sido_short,
    _sync_lifecycle_stage,
    _valid_date,
    app,
    compute_admission_period,
)

logger = logging.getLogger(__name__)
bp = Blueprint("ward", __name__)

# ───────────────────── 생애주기(3번요청) ─────────────────────

@bp.route("/lifecycle")
@login_required
def lifecycle_board_legacy():
    """구 생애주기 보드 — 재원 관리(/ward)로 대체됐다. 북마크·외부 링크 보호용 리다이렉트."""
    return redirect(url_for("ward.ward_view", q=request.args.get("q") or None))

@bp.route("/lifecycle/board")
@login_required
def lifecycle_board():
    """환자 생애주기 관리 보드 — 단계별 컬럼에 환자 카드 배치.
    필터: q(검색) / period(기간) / stages[](단계) / dx(병명그룹) / doctor / archived(아카이브 포함)
    """
    # 구 주소로 접근해도 새 재원 관리 화면으로 일관되게 연결한다.
    return redirect(url_for("ward.ward_view", q=request.args.get("q") or None))

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

@bp.route("/ward")
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
    if subtab not in ("status", "away", "waiting", "trend", "blacklist", "quality", "moves"):
        subtab = "status"
    moves_report = ward_moves.report(request.args) if subtab == "moves" else None
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
    # 전체 상담 8천 건을 매번 읽지 않는다 — 입원 미확정 큐는 '입원완료' 상담만, 재원 명단은 census에 붙은 상담만.
    pool = models.list_consultations(admission_status="입원완료", q=db_q, q_scope="ward", limit=10000)
    pending_pool = [c for c in pool
                    if not (c.get("discharge_date") or "").strip()
                    and not (c.get("actual_admission_date") or c.get("admission_date") or "").strip()
                    and c.get("patient_id") not in census["patients"]]
    if not census["has_roster"]:
        # 명부를 아직 안 올린 설치. 회차가 통째로 비어 있으면 재원 명단도 비므로
        # 옛 방식(상담의 입원완료·미퇴원)으로 돌아간다.
        rows = [c for c in pool
                if not (c.get("discharge_date") or "").strip()]
        pending_pool = [c for c in rows
                        if not (c.get("actual_admission_date") or c.get("admission_date") or "").strip()]
        rows = [c for c in rows
                if (c.get("actual_admission_date") or c.get("admission_date") or "").strip()]
    else:
        rows = models.list_consultations(ids=list(census["by_consultation"]), q=db_q, q_scope="ward", limit=10000)
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
        c["onset_date"] = ep.get("onset_date")
        c["roster_diagnosis"] = ep.get("diagnosis_name")
        # 보험유형도 명부(원무 환자유형)가 실제값 — 상담 시점 값은 보조.
        if (ep.get("insurance_type") or "").strip():
            c["insurance_type"] = ep["insurance_type"].strip()
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
        # 회복기 종료 임박 — 대시보드 '회복기 만료 D-30'과 같은 정의(명부 재활종료일
        # 기준 ±DASHBOARD_DUE_WINDOW_DAYS). 두 화면이 같은 숫자를 내게 맞춘다.
        _ax = _admission_expiry(c)
        c["recovery_due"] = bool(
            _ax and _ax.get("billing_left") is not None
            and -DASHBOARD_DUE_WINDOW_DAYS <= _ax["billing_left"] <= DASHBOARD_DUE_WINDOW_DAYS)
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
    def _room_beds(r):
        # 병실 정원은 병실별 병상 표(5인실·2인실·1인실 반영), 표에 없는 방은 기본 4.
        key = r if r.endswith("호") else f"{r}호"
        return ROOM_BED_CAPACITIES.get(key, ROOM_CAPACITY)
    room_view = []
    for ward in sorted(rooms, key=lambda w: (_room_sort_key(w), w)):
        beds = [
            {"room": r, "patients": sorted(rooms[ward][r],
                                           key=lambda c: c.get("patient_name") or ""),
             "empty": max(0, max(_room_beds(r), len(rooms[ward][r])) - len(rooms[ward][r]))}
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
    recovery_ratio = round(recovery_n / total_n * 100, 2) if total_n else 0
    bed_capacity = 355
    kpis = {
        "admitted": total_n,
        "recovery": recovery_n,
        "nonrecovery": nonrecovery_n,
        "recovery_ratio": recovery_ratio,
        "recovery_ratio_ok": recovery_ratio >= 40,
        "nonrecovery_ratio": round(nonrecovery_n / total_n * 100, 2) if total_n else 0,
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
        # 퇴원 예정 D-30 — 대시보드 '퇴원 예정 D-30'과 같은 ±DASHBOARD_DUE_WINDOW_DAYS 창.
        "discharge_due_30": sum(1 for c in admitted
                                if c.get("discharge_dday") is not None
                                and -DASHBOARD_DUE_WINDOW_DAYS <= c["discharge_dday"] <= DASHBOARD_DUE_WINDOW_DAYS),
        "discharge_due_unchecked": sum(1 for c in admitted
                                        if c.get("discharge_dday") is not None
                                        and -DASHBOARD_DUE_WINDOW_DAYS <= c["discharge_dday"] <= DASHBOARD_DUE_WINDOW_DAYS
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
    spans = models.admission_spans()
    trend_rows = {c["id"]: c for c in models.list_consultations(
        ids=[sp["consultation_id"] for sp in spans if sp.get("consultation_id")], limit=100000)}
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
                "ratio": round(recovery_count * 100 / known, 2) if known else 0}

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
    daily_ratio_trend, monthly_ratio_trend, trend_insight, trend_summary, trend_flow = [], [], None, None, None
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
        trend_flow = _trend_flow(trend_insight, admitted, today_d)

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
    # partial=roster — 명단 본문만(지연 로딩). 접힌 첫 화면은 침상 카드 263장을 보내지 않는다.
    partial = request.args.get("partial") == "roster"
    return render_template(
        "_ward_roster.html" if partial else "ward.html", away=away, admitted=admitted_list,
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
        trend_flow=trend_flow,
        trend_summary=trend_summary,
        subtab=subtab, away_report=away_report, away_candidates=admitted, moves=moves_report,
        blacklisted=blacklisted,
        bed_waiting=bed_waiting,
        room_f=room_f, gender_f=gender_f, dx_f=dx_f,
        sido_f=sido_f, stay_period=stay_period, stay_periods=_WARD_STAY_PERIODS,
        ward_csv_url=url_for("ward.ward_csv") + ("?" + urlencode(request.args.to_dict()) if request.args else ""),
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

@bp.route("/api/consult/<int:cid>/waitlist", methods=["POST"])
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

@bp.route("/api/admin/backup/run", methods=["POST"])
@admin_required
def api_backup_run():
    path = backup.run_backup("manual")
    if not path:
        return jsonify({"error": "백업 생성 또는 무결성 검사에 실패했습니다."}), 500
    return jsonify({"ok": True, "status": backup.latest_status()})

@bp.route("/api/admin/backup/verify", methods=["POST"])
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
    if census["has_roster"]:
        rows = models.list_consultations(ids=list(census["by_consultation"]), q=q, q_scope="ward", limit=10000)
    else:
        rows = models.list_consultations(admission_status="입원완료", q=q, q_scope="ward", limit=10000)
    if census["has_roster"]:
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

@bp.route("/ward/away.xlsx")
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

@bp.route("/ward.csv")
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

@bp.route("/api/patient/<int:pid>/tags", methods=["POST"])
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
        "onset_date": ep.get("onset_date"),
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
    "ext1":        ("연장 1회 (입원 1년 초과)", lambda c: c.get("ext_tier") == 1),
    "ext2":        ("연장 2회 (1년 6개월 초과)", lambda c: c.get("ext_tier") == 2),
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

_RATIO_SPARK_CACHE = {"key": None, "value": None}

def _recovery_ratio_spark(dates):
    """대시보드 '회복기 비율' 카드 스파크라인 — 날짜별 회복기 비율(%). 재원 관리 '회복기 비율 추이'의
    일별 계산(_trend_span → 회차별 회복기 종료일)과 같은 규칙이라 두 화면 숫자가 맞는다.
    상담 1만 건·회차 전체를 읽는 계산이라 5분 캐시 — 대시보드는 30초마다 새로고침된다."""
    key = (tuple(dates), datetime.now().strftime("%Y%m%d%H%M")[:-1])   # 10분 단위 버킷 → 사실상 5~10분 캐시
    if _RATIO_SPARK_CACHE["key"] == key:
        return _RATIO_SPARK_CACHE["value"]
    try:
        raw_spans = models.admission_spans()
        trend_rows = {c["id"]: c for c in models.list_consultations(
            ids=[sp["consultation_id"] for sp in raw_spans if sp.get("consultation_id")], limit=100000)}
        spans = [_trend_span(sp, trend_rows.get(sp["consultation_id"])) for sp in raw_spans]
    except Exception:
        logger.exception("회복기 비율 스파크 계산 실패")
        return []
    out = []
    for iso in dates:
        d = date.fromisoformat(iso)
        known = rec = 0
        for admitted_iso, discharged_iso, is_known, rec_end in spans:
            if admitted_iso > iso or (discharged_iso and discharged_iso <= iso):
                continue
            if is_known:
                known += 1
                if rec_end is not None and d <= rec_end:
                    rec += 1
        out.append(round(rec * 100 / known, 2) if known else 0)
    _RATIO_SPARK_CACHE.update(key=key, value=out)
    return out

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
    month_avg = (round(sum(m["recovery"] for m in months) * 100 / sum(m["known"] for m in months), 2)
                 if months else None)
    return {
        "avg": round(rec_sum * 100 / known_sum, 2),
        "avg_simple": round(sum(d["ratio"] for d in days) / len(days), 2),
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
        return round(r * 100 / k, 2) if k > 0 else 0
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

def _trend_flow(insight, admitted, today, horizon=DASHBOARD_DUE_WINDOW_DAYS):
    """입·퇴원 D-30을 얹은 회복기 비율 추이 — 추이 탭의 '입·퇴원 반영' 칼럼.

    _ratio_insight의 예상은 회복기 종료(전환)만 본다. 여기엔 입원예정 상담
    (planned_admission_date)과 퇴원 예정(discharge_due, 재원 카드의 '퇴원 예정 D-30'과
    같은 값)을 날짜별로 더해 그날그날의 비율이 어디로 가는지 본다.
    - 입원 예정자의 회복기 여부는 상담 판정(_recovery_status). 판정 불가는 인원(total)에만
      들어가고 비율 분모(known)엔 안 들어간다 — 추이 그래프와 같은 규칙.
    - 회복기 환자가 종료일과 퇴원일을 둘 다 가지면 먼저 오는 쪽만 회복기로 센다:
      퇴원이 먼저면 회복기로 나가고(전환 없음), 종료가 먼저면 전환 뒤 비회복기로 나간다.
    가정 계산기의 기본값(rec_in/rec_out/non_in/non_out/rec_end)도 여기서 나온다.
    """
    if not insight:
        return None
    today_iso = today.isoformat()
    end_iso = (today + timedelta(days=horizon)).isoformat()
    days = {}
    for offset in range(horizon + 1):
        d = today + timedelta(days=offset)
        days[d.isoformat()] = {
            "date": d.isoformat(), "label": d.strftime("%m.%d"), "dday": offset,
            "in_rec": 0, "in_non": 0, "in_unknown": 0,
            "out_rec": 0, "out_non": 0, "out_unknown": 0, "rec_end": 0,
            "in_names": [], "out_names": [], "end_names": [],
        }
    def _who(c, phase):
        return {"id": c.get("id"), "name": c.get("patient_name") or "환자 미지정", "phase": phase}
    for c in models.list_consultations(admission_status="입원예정", limit=10000):
        planned = (c.get("planned_admission_date") or "").strip()[:10]
        if not today_iso <= planned <= end_iso:
            continue
        label = (_recovery_status(c) or {}).get("label")
        row = days[planned]
        row["in_rec" if label == "회복기" else "in_non" if label == "비회복기" else "in_unknown"] += 1
        row["in_names"].append(_who(c, label or "미판정"))
    for c in admitted:
        phase = c.get("care_phase")
        known = c.get("id") is not None
        out_dday = c.get("discharge_dday")
        leaving = out_dday is not None and 0 <= out_dday <= horizon and c.get("discharge_due") in days
        end_dday = c.get("phase_dday") if phase == "회복기" else None
        ending = end_dday is not None and 0 <= end_dday <= horizon
        if phase == "회복기" and ending and leaving and out_dday <= end_dday:
            ending = False          # 종료 전에 퇴원 — 회복기인 채로 나간다
        if ending:
            row = days[(today + timedelta(days=end_dday)).isoformat()]
            row["rec_end"] += 1
            row["end_names"].append(_who(c, "회복기"))
            if leaving:
                phase = "비회복기"   # 전환 뒤 퇴원
        if leaving:
            row = days[c["discharge_due"]]
            key = ("out_rec" if phase == "회복기" else "out_non" if known else "out_unknown")
            row[key] += 1
            row["out_names"].append(_who(c, phase if known else "미판정"))
    r, k, t = insight["recovery"], insight["known"], insight["total"]
    series, cross = [], None
    for row in sorted(days.values(), key=lambda x: x["dday"]):
        row["in_total"] = row["in_rec"] + row["in_non"] + row["in_unknown"]
        row["out_total"] = row["out_rec"] + row["out_non"] + row["out_unknown"]
        r += row["in_rec"] - row["out_rec"] - row["rec_end"]
        k += row["in_rec"] + row["in_non"] - row["out_rec"] - row["out_non"]
        t += row["in_total"] - row["out_total"]
        r, k, t = max(0, r), max(0, k), max(0, t)
        row.update(recovery=r, known=k, total=t,
                   ratio=round(r * 100 / k, 2) if k else 0)
        if cross is None and k and r * 100 < insight["threshold"] * k:
            cross = row
        series.append(row)
    total = {key: sum(d[key] for d in series)
             for key in ("in_rec", "in_non", "in_unknown", "in_total",
                         "out_rec", "out_non", "out_unknown", "out_total", "rec_end")}
    active = [d for d in series if d["in_total"] or d["out_total"] or d["rec_end"]]
    return {"horizon": horizon, "days": series, "active": active, "sum": total,
            "start_ratio": insight["ratio"], "end": series[-1], "cross": cross,
            "ok": series[-1]["known"] > 0 and series[-1]["recovery"] * 100 >= insight["threshold"] * series[-1]["known"]}

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
    """입원일로부터 실제 경과일로 연장 단계를 판정한다(2026-09-14 사용자 정의).
      기본  : 입원 1년(365일) 이내
      연장 1회: 1년 초과 ~ 1년 6개월(545일)   — 6개월 연장 1회분
      연장 2회: 1년 6개월 초과            — 최종 한도는 발병일 + 2년(730일)
    예전엔 수동으로 늘린 퇴원예정일(discharge_due_date)로 판정해, 예정일을 안 고친 환자는
    1년을 넘겨도 배지가 안 붙었다. 이제 예정일과 무관하게 경과일만 본다.
    Returns dict(ext_tier 0/1/2, ext_label, ext_extra_days, ext_end_date/left, ext_cap_date/left)."""
    out = {"ext_tier": 0, "ext_label": "기본 (1년)", "ext_extra_days": 0,
           "ext_end_date": None, "ext_end_left": None,
           "ext_cap_date": None, "ext_cap_left": None}
    today = date.today()
    onset = (c.get("disease_onset") or c.get("onset_date") or "").strip()
    cap = None
    if onset:
        try:
            cap = datetime.strptime(onset[:10], "%Y-%m-%d").date() + timedelta(days=730)
            out["ext_cap_date"] = cap.isoformat()
            out["ext_cap_left"] = (cap - today).days
        except (ValueError, TypeError):
            cap = None
    adm = c.get("admitted_on")
    if not adm:
        return out
    try:
        ad = datetime.strptime(adm[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return out
    days = (today - ad).days
    extra = days - TOTAL_STAY_DAYS
    out["ext_extra_days"] = max(0, extra)
    if extra <= 0:
        out["ext_end_date"] = (ad + timedelta(days=TOTAL_STAY_DAYS)).isoformat()
    elif days <= TOTAL_STAY_DAYS + EXTENSION_DAYS:
        out["ext_tier"] = 1
        out["ext_label"] = "연장 1회 (1년 6개월까지)"
        out["ext_end_date"] = (ad + timedelta(days=TOTAL_STAY_DAYS + EXTENSION_DAYS)).isoformat()
    else:
        out["ext_tier"] = 2
        out["ext_label"] = "연장 2회 (발병일 + 2년까지)"
        end = cap or (ad + timedelta(days=TOTAL_STAY_DAYS + 2 * EXTENSION_DAYS))
        out["ext_end_date"] = end.isoformat()
    if out["ext_end_date"]:
        out["ext_end_left"] = (date.fromisoformat(out["ext_end_date"]) - today).days
    return out

def _room_sort_key(room):
    """호실 정렬 — '502', '502-1', 'A동 3층' 등 섞여 있어 숫자 우선으로 정렬."""
    r = (room or "").strip()
    if not r:
        return (1, "")
    digits = "".join(ch for ch in r if ch.isdigit())
    return (0, int(digits)) if digits else (1, r)

@bp.route("/api/consult/<int:cid>/room", methods=["POST"])
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

@bp.route("/api/consult/<int:cid>/admit", methods=["POST"])
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

@bp.route("/api/patient/<int:pid>/stage", methods=["POST"])
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

@bp.route("/api/patient/<int:pid>/lifecycle/event", methods=["POST"])
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

@bp.route("/api/lifecycle/event/<int:eid>", methods=["DELETE"])
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

@bp.route("/api/patient/<int:pid>/blacklist", methods=["POST"])
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

@bp.route("/api/patient/blacklist-check")
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
