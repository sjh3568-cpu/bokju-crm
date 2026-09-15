"""옴니채널 인바운드 — 커뮤니케이션(인박스), 홈페이지 상담게시판 답변, 외진 이벤트 API, 카카오/홈페이지 webhook.

app.py에서 분리(2026-09-15). 라우트 경로·동작은 그대로, 엔드포인트 이름만 "inbound.<함수>"가 됐다.
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
    _dashboard_inbound_bucket,
    _is_safe_next_url,
    _set_lifecycle_stage_clinical,
    _valid_time,
    app,
)

logger = logging.getLogger(__name__)
bp = Blueprint("inbound", __name__)

# ───────────────────── 옴니채널—커뮤니케이션 ─────────────────────

@bp.route('/inbox',methods=['GET','POST'])
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
        return redirect(request.form.get('return_to') if _is_safe_next_url(request.form.get('return_to')) else url_for('inbound.unified_inbox'))
    filters={k:(request.args.get(k) or '').strip() for k in ('status','channel','assignee','q')}
    if not filters['status']: filters['status']='active'
    users=[u for u in models.list_users() if u.get('active') and u.get('role') in ('admin','staff')]
    return render_template('inbox.html',rows=models.inbox_communications(**filters),summary=models.inbox_summary(),
        callbacks=models.inbox_callbacks(),users=users,filters=filters,csrf=token)

@bp.route("/api/communication", methods=["POST"])
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

@bp.route("/api/communication/<int:comm_id>/done", methods=["POST"])
@login_required
def api_communication_done(comm_id):
    if not models.get_communication(comm_id):
        return jsonify({"error": "not found"}), 404
    models.update_communication(comm_id, status="done")
    return jsonify({"ok": True})

@bp.route("/api/communication/<int:comm_id>/missed", methods=["POST"])
@login_required
def api_communication_missed(comm_id):
    """부재중 → 재연락 예약. 인바운드 문의에 전화했는데 안 받았을 때
    status='waiting' + follow_up_at으로 돌리고, 시각이 되면 다시 알림·배지에 올라온다."""
    comm = models.get_communication(comm_id)
    if not comm:
        return jsonify({"error": "not found"}), 404
    if (comm.get("status") or "") == "done":
        return jsonify({"error": "이미 완료된 문의입니다."}), 400
    payload = request.get_json(silent=True) or {}
    raw = (payload.get("follow_up_at") or "").strip().replace("T", " ")
    try:
        when = datetime.strptime(raw[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return jsonify({"error": "재연락 시각 형식 오류 (YYYY-MM-DD HH:MM)"}), 400
    if when < datetime.now() - timedelta(minutes=5):
        return jsonify({"error": "재연락 시각은 지금 이후여야 합니다."}), 400
    follow_up_at = when.strftime("%Y-%m-%d %H:%M")
    n = models.mark_communication_missed(comm_id, follow_up_at, by=g.user.get("display_name") or "")
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="missed_communication", target_type="communication", target_id=comm_id,
        detail=f"부재 {n}회 → 재연락 {follow_up_at}", ip=request.remote_addr,
    )
    return jsonify({"ok": True, "missed_count": n, "follow_up_at": follow_up_at})

@bp.route("/api/homepage-board/<int:comm_id>")
@login_required
def api_homepage_board_get(comm_id):
    """인박스 '답변' 대화상자용 — 원문 + 기본 답변 문안 + 홈페이지 관리자 링크."""
    comm = models.get_communication(comm_id)
    post = models.homepage_post_by_comm(comm_id)
    if not comm or not post:
        return jsonify({"error": "홈페이지 게시판 글과 연결되지 않은 문의입니다."}), 404
    return jsonify({
        "comm_id": comm_id, "idx": post["idx"], "board_no": post.get("board_no"),
        "title": post.get("title") or "", "site_status": post.get("site_status") or "",
        "summary": comm.get("summary") or "", "body": comm.get("body") or "",
        "contact": comm.get("contact") or "", "occurred_at": comm.get("occurred_at") or comm.get("created_at") or "",
        "admin_url": homepage_board.admin_view_url(post["idx"]),
        "admin_ready": homepage_board.admin_configured(),
        "reply_title": homepage_board.REPLY_DEFAULT_TITLE,
        "reply_body": homepage_board.REPLY_DEFAULT_BODY,
    })

@bp.route("/api/homepage-board/<int:comm_id>/reply", methods=["POST"])
@login_required
def api_homepage_board_reply(comm_id):
    """CRM 인박스에서 쓴 답변을 홈페이지 상담게시판에 등록하고 인박스를 완료 처리."""
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()[:100]
    body = (data.get("body") or "").strip()[:4000]
    try:
        after = homepage_board.reply_to_post(comm_id, title, body, g.user["username"])
    except homepage_board.BoardError as e:
        return jsonify({"error": str(e)}), 502
    except Exception:
        logger.exception("홈페이지 답변 등록 중 예외 (comm=%s)", comm_id)
        return jsonify({"error": "홈페이지 답변 등록 중 오류가 났습니다. 홈페이지 관리자에서 직접 올려주세요."}), 500
    return jsonify({"ok": True, "answer_date": after.get("answer_date") or ""})

@bp.route("/api/communication/<int:comm_id>", methods=["DELETE"])
@login_required
def api_communication_delete(comm_id):
    if not models.get_communication(comm_id):
        return jsonify({"error": "not found"}), 404
    models.delete_communication(comm_id)
    return jsonify({"ok": True})

def _valid_expected_return(value, event_date=None):
    """외진 복귀 예정일 검증 → (날짜|None, 오류메시지|None). 빈 값은 '미정'."""
    value = (value or "").strip()
    if not value:
        return None, None
    try:
        rd = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None, "복귀 예정일 형식 오류 (YYYY-MM-DD)"
    out = (event_date or "").strip()[:10]
    if out:
        try:
            if rd < datetime.strptime(out, "%Y-%m-%d").date():
                return None, f"복귀 예정일이 외진 나간 날({out})보다 빠릅니다."
        except ValueError:
            pass
    return rd.isoformat(), None

@bp.route("/api/admission-event/<int:event_id>/expected-return", methods=["POST"])
@login_required
def api_admission_event_expected_return(event_id):
    """외진 복귀 예정 — 재원 관리·외진 명부의 [복귀]. 대시보드 '오늘 입원' 카드에 '입원 예정'으로
    잡히고, 실제로 돌아오면 대시보드 입원 처리 [완료](= /return)가 '입원 완료'로 넘긴다.
    복귀 병실·기타 사항은 여기서 미리 받아 두고 [완료] 때 반영한다.
    """
    ev = models.get_admission_event(event_id)
    if not ev or ev.get("event_type") not in models.AWAY_EVENT_TYPES:
        return jsonify({"error": "외진·전원 기록이 아닙니다."}), 404
    payload = request.get_json(silent=True) or {}
    expected, err = _valid_expected_return(payload.get("expected_return_date"), ev.get("event_date"))
    if err:
        return jsonify({"error": err}), 400
    return_room = payload.get("return_room")
    return_note = payload.get("return_note")
    if return_room is not None and len(return_room) > 50:
        return jsonify({"error": "복귀 병실은 50자 이내로 입력하세요."}), 400
    if return_note is not None and len(return_note) > 3000:
        return jsonify({"error": "기타 사항은 3000자 이내로 입력하세요."}), 400
    try:
        cid = models.set_away_expected_return(event_id, expected,
                                              return_room=return_room, return_note=return_note)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="update_admission_event", target_type="consultation", target_id=cid,
        detail=f"외진 복귀 예정일 {expected or '미정'}", ip=request.remote_addr,
    )
    return jsonify({"ok": True, "expected_return_date": expected})

@bp.route("/api/consult/<int:cid>/admission-event", methods=["POST"])
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
    expected_return = None
    if event_type in models.AWAY_EVENT_TYPES:
        if models.open_away_event(cid):
            return jsonify({"error": "미복귀 기록이 있습니다. 먼저 복귀 처리하세요."}), 400
        if event_date and event_date > date.today().isoformat():
            return jsonify({"error": "전원·외진일은 미래일 수 없습니다."}), 400
        admitted_on = (con.get("actual_admission_date") or con.get("admission_date") or "")[:10]
        if event_date and admitted_on and event_date < admitted_on:
            return jsonify({"error": "전원·외진일이 입원일보다 빠릅니다."}), 400
        expected_return, err = _valid_expected_return(payload.get("expected_return_date"), event_date)
        if err:
            return jsonify({"error": err}), 400
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
        expected_return_date=expected_return,
    )
    # 외진은 단계를 바꾸지 않는다 — 병상을 유지한 일시 이탈이므로 '입원'에 그대로
    # 머물고, 미복귀 플래그(returned_at IS NULL)와 카드 배지로만 표시한다.
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="add_admission_event", target_type="consultation", target_id=cid,
        detail=event_type, ip=request.remote_addr,
    )
    return jsonify({"ok": True, "id": eid})

@bp.route("/api/admission-event/<int:event_id>/return", methods=["POST"])
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
    # 병실·기타 사항은 선택 입력이다 — 대시보드 입원 처리 [완료]·재원 현황의 1클릭은
    # 날짜만 보내므로, 복귀 예정 때 미리 적어 둔 값이 있으면 그것을 쓴다. 둘 다 없으면
    # 현재 병실을 그대로 둔다.
    return_room = (payload.get("return_room") or ev.get("return_room") or "").strip()
    return_note = (payload.get("return_note") or ev.get("return_note") or "").strip()
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

@bp.route("/api/admission-event/<int:event_id>/return/undo", methods=["POST"])
@login_required
def api_admission_event_return_undo(event_id):
    """복귀 처리 되돌리기 — 다시 '외진 중'이 되고, 복귀 예정일을 주면 대시보드 입원 예정에 잡힌다."""
    ev = models.get_admission_event(event_id)
    if not ev or ev.get("event_type") not in models.AWAY_EVENT_TYPES:
        return jsonify({"error": "외진·전원 기록이 아닙니다."}), 404
    payload = request.get_json(silent=True) or {}
    expected, err = _valid_expected_return(payload.get("expected_return_date"), ev.get("event_date"))
    if err:
        return jsonify({"error": err}), 400
    try:
        cid = models.undo_admission_event_return(event_id, expected)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="update_admission_event", target_type="consultation", target_id=cid,
        detail=f"{ev.get('event_type')} 복귀 취소 → 외진 중 (복귀 예정 {expected or '미정'})",
        ip=request.remote_addr,
    )
    return jsonify({"ok": True, "expected_return_date": expected})

@bp.route("/api/admission-event/<int:event_id>/details", methods=["POST"])
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

@bp.route("/api/admission-event/<int:event_id>/update", methods=["POST"])
@login_required
def api_admission_event_update(event_id):
    """입원 중 이벤트 내용 수정 — 상담 상세 표의 [수정]. 발생일·시각·의료기관·메모만 받는다.
    유형은 못 바꾼다(외진 판정·복귀 흐름이 유형에 걸려 있다 — 틀렸으면 삭제 후 다시 등록)."""
    ev = models.get_admission_event(event_id)
    if not ev:
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    event_date = (payload.get("event_date") or "").strip() or None
    if event_date:
        try:
            ed = datetime.strptime(event_date, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "발생일 형식 오류 (YYYY-MM-DD)"}), 400
        if ev.get("event_type") in models.AWAY_EVENT_TYPES:
            if ed > date.today():
                return jsonify({"error": "전원·외진일은 미래일 수 없습니다."}), 400
            back = (ev.get("returned_at") or "")[:10]
            if back and back < event_date:
                return jsonify({"error": f"복귀일({back})보다 늦은 날짜로는 바꿀 수 없습니다."}), 400
            exp = (ev.get("expected_return_date") or "")[:10]
            if exp and exp < event_date:
                return jsonify({"error": f"복귀 예정일({exp})보다 늦은 날짜로는 바꿀 수 없습니다."}), 400
    event_time = _valid_time(payload.get("event_time"))
    if payload.get("event_time") and not event_time:
        return jsonify({"error": "이송 시각 형식 오류"}), 400
    hospital = (payload.get("hospital") or "").strip()
    memo = (payload.get("memo") or "").strip()
    if len(hospital) > 200:
        return jsonify({"error": "의료기관은 200자 이내로 입력하세요."}), 400
    if len(memo) > 3000:
        return jsonify({"error": "메모는 3000자 이내로 입력하세요."}), 400
    try:
        cid = models.update_admission_event(event_id, event_date=event_date, event_time=event_time,
                                            hospital=hospital or None, memo=memo or None)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    models.log_audit(
        user_id=g.user["id"], username=g.user["username"],
        action="update_admission_event", target_type="consultation", target_id=cid,
        detail=f"{ev.get('event_type') or '이벤트'} 내용 수정 ({event_date or '일자 없음'})", ip=request.remote_addr,
    )
    return jsonify({"ok": True})

@bp.route("/api/admission-event/<int:event_id>", methods=["DELETE"])
@login_required
def api_admission_event_delete(event_id):
    if not models.get_admission_event(event_id):
        return jsonify({"error": "not found"}), 404
    models.delete_admission_event(event_id)
    return jsonify({"ok": True})

@bp.route("/api/inbound/alerts")
@login_required
def api_inbound_alerts():
    """미처리 인바운드 알림 피드 — 상담사 브라우저가 주기적으로 폴링.
    홈페이지·카카오 등 채널 문의가 새로 들어오면 화면 알림으로 띄운다.
    프론트가 localStorage로 '이미 본 id'를 관리하므로 서버는 현재 대기목록만 반환."""
    items = []
    for m in models.inbox_open_communications():
        if m.get("status") == "waiting":
            # 부재중 → 재연락 예약: 시각이 되기 전엔 조용히, 되면 새 알림으로(id를 시각과 묶어 재통지)
            if not m.get("callback_due"):
                continue
            items.append({
                "id": f"cb{m.get('id')}@{m.get('follow_up_at')}",
                "channel": m.get("channel") or "기타",
                "bucket": "재연락",
                "title": f"🔁 재연락 시간 — {_dashboard_inbound_bucket(m)} 문의 (부재 {m.get('missed_count') or 1}회)",
                "summary": (m.get("summary") or m.get("body") or "재연락")[:80],
                "contact": m.get("contact") or "",
                "patient_name": m.get("patient_name") or "",
                "blacklist": bool(m.get("blacklist")),
                "created_at": m.get("follow_up_at") or "",
            })
            continue
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
    count = len(items)                      # 배지는 미처리 문의 수만(신규 + 재연락 시각 도래)
    try:
        items += transport.assignment_alerts()   # 🚐 운행팀 배정 완료 — 토스트로만 알림
    except Exception:
        logger.exception("운행 배정 알림 조회 실패")
    return jsonify({"count": count, "items": items})


# ───────────────────── 인바운드webhook(옴니채널직수신) ─────────────────────

_WEBHOOK_MAX_BYTES = 16 * 1024          # 인바운드 문의 1건 — 16KB면 충분

_WEBHOOK_RATE_MAX = 60                   # IP당

_WEBHOOK_RATE_WINDOW = 60                # 초
_WEBHOOK_HITS: dict[str, list[float]] = {}   # IP별 최근 호출 시각(메모리) — 분리 시 app.py에서 옮김

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

@bp.route("/api/webhook/kakao", methods=["POST"])
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

@bp.route("/api/webhook/kakao/skill", methods=["POST"])
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

_HOMEPAGE_EXTRA_FIELDS = [
    ("available_time", "연락가능시간"),
    ("address", "거주지"),
    ("patient_age", "환자나이"),
]

@bp.route("/api/webhook/homepage", methods=["POST"])
def api_webhook_homepage():
    """홈페이지 문의폼 인바운드 — .env HOMEPAGE_WEBHOOK_TOKEN으로 검증.
    홈페이지 서버가 문의 1건을 서버-투-서버로 POST한다(토큰은 브라우저에 노출 금지).
    body(JSON): { name, phone, email?, subject?, message|content, receipt_no|id?,
                  available_time?, address?, patient_age? } — message 필수.
    전화번호로 환자 자동 매칭, communications(채널=웹문의, 인바운드)로 기록."""
    payload, err = _webhook_guard("HOMEPAGE_WEBHOOK_TOKEN")
    if err:
        return err
    name = (payload.get("name") or "").strip()
    phone = _norm_phone((payload.get("phone") or "").strip())
    email = (payload.get("email") or "").strip()
    subject = (payload.get("subject") or "").strip()
    message = (payload.get("message") or payload.get("content") or "").strip()
    if not message:
        return jsonify({"error": "message 필수"}), 400
    pid = models.match_patient_by_phone(phone)
    # EasyQR '빠른 전화상담 신청'(walk.induk.ai.kr consult.php)처럼 부가 항목을 함께 보내는
    # 폼은 라벨을 붙여 본문에 보존한다. receipt_no(접수번호)는 제목에 붙여 홈페이지 쪽과 대조.
    receipt = str(payload.get("receipt_no") or payload.get("id") or "").strip()
    head = subject or "홈페이지 문의"
    if receipt:
        head = f"{head} #{receipt}"
    summary = head + (f" · {name}" if name else "")
    body = message[:4000]
    extras = [(label, str(payload.get(key) or "").strip()) for key, label in _HOMEPAGE_EXTRA_FIELDS]
    extras = [(label, v) for label, v in extras if v]
    if extras:
        body = body + "\n\n" + "\n".join(f"[{label}] {v}" for label, v in extras)
    if email:
        body = f"{body}\n\n[이메일] {email}"
    comm_id = models.create_communication(
        patient_id=pid, channel="웹문의", direction="in",
        contact=phone or email or name or None,
        summary=summary, body=body, status="open", created_by="홈페이지",
    )
    _log_webhook("웹문의", pid, comm_id)
    return jsonify({"ok": True, "id": comm_id, "matched_patient": pid})
