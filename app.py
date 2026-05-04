"""Flask 앱 — 복주회복병원 상담실 CRM (bokju-crm).

진입점. 인증·상담 등록/목록/상세·자동완성·통계 API를 한 파일에 모음.
규모가 커지면 Blueprint로 쪼갤 것 (현재는 cafe-helper 스타일 단일 파일).
"""
import csv
import io
import logging
import os
import secrets
from datetime import datetime, timedelta

from dotenv import load_dotenv
from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)

import models
from auth import (
    admin_required, authenticate, current_user, is_locked_out,
    login_required, login_user, logout_user,
)
from config import (
    ACTIVITY_ACTIVE_OPTIONS, ACTIVITY_DIAPER_OPTIONS, ACTIVITY_OTHERS_OPTIONS,
    ACTIVITY_WHEELCHAIR_OPTIONS, ADMISSION_DOCS, ADMISSION_STATUSES,
    BED_OPTIONS, CAREGIVER_OPTIONS,
    CONSCIOUSNESS_MAIN_OPTIONS, CONSULT_CHANNELS, CONVERSATION_LEVEL_OPTIONS,
    REJECTION_REASONS,
    COST_GUIDANCE_OPTIONS, CURRENT_LOCATION_TYPES, DIET_TYPES,
    DISEASES_CHECKLIST, DISEASES_GROUPS, GUARDIAN_RELATION_SUGGESTIONS,
    HEARING_OPTIONS, INFO_PROVIDED_OPTIONS,
    COUNSELORS, DISEASES_LAYOUT, OTHERS_LAYOUT,
    INSURANCE_TYPES, OTHERS_CHECKLIST, REFERRAL_SOURCE_GROUPS, REFERRAL_TYPES,
    SIDO_LIST, SIGUNGU_INDEX, SIGUNGU_LIST,
    SPECIAL_CARE_OPTIONS,
    THERAPY_OPTIONS, TRANSPORT_OPTIONS, WOUND_CARE_OPTIONS,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(hours=int(os.getenv("SESSION_HOURS", "4")))

_db_initialized = False


@app.before_request
def _bootstrap():
    """첫 요청 시 DB 초기화 + admin 계정 셋업.
    .env의 APP_PASSWORD를 admin 계정 비밀번호로 자동 동기화 (단일 비밀번호 MVP).
    """
    global _db_initialized
    if not _db_initialized:
        models.init_db()
        admin_pw = os.getenv("APP_PASSWORD", "").strip()
        if admin_pw:
            models.ensure_admin_user("admin", admin_pw, display_name="관리자")
        _db_initialized = True


@app.after_request
def _no_store(resp):
    """환자 정보 페이지가 브라우저 캐시에 남지 않도록.
    로그아웃 후 뒤로가기로 노출되는 것 방지.
    """
    resp.headers["Cache-Control"] = "no-store, private, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.context_processor
def _inject_globals():
    return {
        "current_user": current_user(),
        "INSURANCE_TYPES": INSURANCE_TYPES,
        "CONSULT_CHANNELS": CONSULT_CHANNELS,
        "ADMISSION_STATUSES": ADMISSION_STATUSES,
        "REJECTION_REASONS": REJECTION_REASONS,
        "GUARDIAN_RELATION_SUGGESTIONS": GUARDIAN_RELATION_SUGGESTIONS,
        "COUNSELORS": COUNSELORS,
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
        "WOUND_CARE_OPTIONS": WOUND_CARE_OPTIONS,
        "SPECIAL_CARE_OPTIONS": SPECIAL_CARE_OPTIONS,
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
    }


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


@app.template_filter("agefrom")
def _agefrom(birth_year):
    if not birth_year:
        return ""
    return datetime.now().year - int(birth_year)


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
        next_url = request.args.get("next") or request.form.get("next") or url_for("dashboard")
        if not next_url.startswith("/"):
            next_url = url_for("dashboard")
        return redirect(next_url)
    if current_user():
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout", methods=["POST", "GET"])
def logout_view():
    logout_user()
    flash("로그아웃되었습니다.", "info")
    return redirect(url_for("login_view"))


# ───────────────────── 메인 ─────────────────────

@app.route("/")
@login_required
def dashboard():
    data = models.dashboard_summary()
    return render_template("dashboard.html", **data)


@app.route("/healthz")
def healthz():
    return {"ok": True}


# ───────────────────── 통계 (Phase 3) ─────────────────────

@app.route("/stats")
@login_required
def stats_view():
    preset, date_from, date_to = _stats_period_from_request()
    return render_template(
        "stats.html",
        preset=preset, date_from=date_from, date_to=date_to,
        insight_enabled=bool(os.getenv("ANTHROPIC_API_KEY")),
    )


@app.route("/api/stats.json")
@login_required
def api_stats():
    _, date_from, date_to = _stats_period_from_request()
    data = models.aggregate_stats(date_from, date_to)
    return jsonify(data)


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
        return jsonify({"insight": "이번 달 상담 기록이 없어 인사이트를 생성할 수 없습니다."})
    try:
        from llm import summarize_monthly
        text = summarize_monthly(data)
    except Exception as e:
        logger.warning(f"월간 인사이트 실패: {e}")
        return jsonify({"error": f"인사이트 생성 실패: {e}"}), 502
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="report_insight", target_type="monthly_report",
        detail=f"{year}-{month:02d}", ip=request.remote_addr,
    )
    return jsonify({"insight": text})


@app.route("/api/stats/insight", methods=["POST"])
@login_required
def api_stats_insight():
    if not os.getenv("ANTHROPIC_API_KEY"):
        return jsonify({"error": "Claude API 키가 설정되지 않았습니다."}), 503
    payload = request.get_json(silent=True) or {}
    date_from = payload.get("from") or None
    date_to = payload.get("to") or None
    data = models.aggregate_stats(date_from, date_to)
    if not data["summary"]["total"]:
        return jsonify({"insight": "분석할 상담 기록이 없습니다."})
    try:
        from llm import summarize_stats
        text = summarize_stats(data, date_from=date_from, date_to=date_to)
    except Exception as e:
        logger.warning(f"Claude 인사이트 실패: {e}")
        return jsonify({"error": f"인사이트 생성 실패: {e}"}), 502
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="stats_insight", target_type="stats",
        detail=f"{date_from}~{date_to}", ip=request.remote_addr,
    )
    return jsonify({"insight": text})


# ───────────────────── 상담 ─────────────────────

@app.route("/consult/new")
@login_required
def consult_new():
    return render_template("consult_form.html", consultation=None, patient=None)


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
    return render_template("consult_detail.html", c=c, history=history)


@app.route("/consult/<int:cid>/edit")
@login_required
def consult_edit(cid):
    c = models.get_consultation(cid)
    if not c:
        abort(404)
    patient = models.get_patient(c["patient_id"])
    return render_template("consult_form.html", consultation=c, patient=patient)


@app.route("/consultations")
@login_required
def consult_list():
    filters = _list_filters_from_request()
    rows = models.list_consultations(**filters, limit=200)
    return render_template("consult_list.html", rows=rows, filters=filters)


@app.route("/consultations.csv")
@admin_required
def consult_csv():
    filters = _list_filters_from_request()
    rows = models.list_consultations(**filters, limit=10000)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "상담일", "상담시각", "환자명", "성별", "나이", "주소",
        "보험유형", "보호자", "관계", "연락처",
        "상담방법", "유입경로(상위)", "세부 경로",
        "주치의", "호실", "입원예정일", "상담자",
    ])
    for r in rows:
        writer.writerow([
            r.get("consult_date") or "",
            r.get("consult_time") or "",
            r.get("patient_name") or "",
            r.get("gender") or "",
            r.get("patient_age") if r.get("patient_age") is not None else "",
            r.get("address_full") or "",
            r.get("insurance_type") or "",
            r.get("guardian_name") or "",
            r.get("guardian_relation") or "",
            r.get("guardian_phone") or "",
            r.get("consult_channel") or "",
            _csv_list(r.get("referral_source_type")),
            _csv_list(r.get("referral_source_detail")),
            r.get("attending_doctor") or "",
            r.get("room_number") or "",
            r.get("planned_admission_date") or "",
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
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="view_patient", target_type="patient", target_id=pid,
        ip=request.remote_addr,
    )
    return render_template("patient_detail.html", p=p, history=history)


# ───────────────────── API: 상담 CRUD ─────────────────────

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

    c = payload.get("consultation", {})
    cfields = _consult_fields_from_payload(c)
    cfields.setdefault("consult_date", datetime.now().strftime("%Y-%m-%d"))
    cfields.setdefault("counselor", g.user.get("display_name"))
    cid = models.create_consultation(patient_id=pid, **cfields)
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="create_consult", target_type="consultation", target_id=cid,
        ip=request.remote_addr,
    )
    return jsonify({"ok": True, "id": cid, "patient_id": pid})


@app.route("/api/consult/<int:cid>", methods=["POST"])
@login_required
def api_consult_update(cid):
    existing = models.get_consultation(cid)
    if not existing:
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}

    p = payload.get("patient") or {}
    if p:
        patient_cols = (
            "gender", "address_full", "residence_sido", "residence_sigungu",
            "insurance_type", "guardian_name", "guardian_relation",
            "guardian_phone", "family_info",
        )
        valid = {k: v for k, v in p.items() if k in patient_cols and v not in (None, "")}
        if valid:
            models.update_patient(existing["patient_id"], **valid)

    c = payload.get("consultation") or {}
    update_fields = _consult_fields_from_payload(c)
    if update_fields:
        models.update_consultation(cid, **update_fields)

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
    existing = models.get_consultation(cid)
    if not existing:
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    status = (payload.get("admission_status") or "").strip()
    if status not in ADMISSION_STATUSES:
        return jsonify({"error": "허용되지 않은 상태값"}), 400

    fields = {"admission_status": status}
    # 입원완료 시 admission_date 함께 저장 (선택). 다른 상태는 admission_date 변경 안 함.
    adate = (payload.get("admission_date") or "").strip()
    if status == "입원완료" and adate:
        try:
            datetime.strptime(adate, "%Y-%m-%d")
            fields["admission_date"] = adate
        except ValueError:
            return jsonify({"error": "입원일자 형식 오류"}), 400

    # 입원취소 시 사유(라벨) + 자유메모 함께 저장. 라벨은 화이트리스트 검증.
    if status == "입원취소":
        reason = (payload.get("rejection_reason") or "").strip()
        reason_detail = (payload.get("rejection_reason_detail") or "").strip()
        if reason and reason not in REJECTION_REASONS:
            return jsonify({"error": "허용되지 않은 취소 사유"}), 400
        if reason:
            fields["rejection_reason"] = reason
        if reason_detail:
            fields["rejection_reason_detail"] = reason_detail

    models.update_consultation_meta(cid, **fields)
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="update_status", target_type="consultation", target_id=cid,
        detail=status, ip=request.remote_addr,
    )
    return jsonify({"ok": True, "admission_status": status,
                    "admission_date": fields.get("admission_date")})


# ───────────────────── API: 자동완성 ─────────────────────

@app.route("/api/autocomplete/hospital")
@login_required
def api_ac_hospital():
    q = (request.args.get("q") or "").strip()
    if len(q) < 1:
        return jsonify({"items": []})
    return jsonify({"items": models.autocomplete_hospitals(q, limit=10)})


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


# ───────────────────── helpers ─────────────────────

def _list_filters_from_request():
    return {
        "date_from": request.args.get("from") or None,
        "date_to": request.args.get("to") or None,
        "insurance": request.args.get("insurance") or None,
        "q": request.args.get("q") or None,
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
    # 모병원 자동 매핑: '현재' 라디오가 입원중/입소중인 경우에 한해
    # current_location_name을 source_hospital 컬럼에도 함께 기록.
    # 자택 거주는 모병원 없음(통계 분석에서 제외).
    loc_type = out.get("current_location_type")
    loc_name = out.get("current_location_name")
    if loc_type in ("입원중", "입소중") and loc_name:
        out["source_hospital"] = loc_name
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
