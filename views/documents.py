"""팩스·문서 자료함 (2026-09-17) — NAS 폴더로 들어온 모병원 팩스를 화면에서 보고, AI 요약을 확인하고,
환자·상담에 연결한다. 감시·판독·파일명 정리는 fax_inbox.py, 판독 프롬프트는 llm.analyze_document.

권한: 상담(consult) 메뉴 — 조회는 열람, 수정 이상이면 판독 다시·연결·완료·삭제·업로드.
"""
import json
import logging
import mimetypes
from pathlib import Path

from flask import Blueprint, abort, g, jsonify, redirect, render_template, request, send_file

import fax_inbox
import models
from auth import current_user, login_required, menu_level
from config import PERM_EDIT

logger = logging.getLogger(__name__)
bp = Blueprint("documents", __name__)

STATUS_LABELS = {"pending": "판독 대기", "analyzed": "확인 필요", "done": "처리완료"}


def _can_edit():
    return menu_level(current_user(), "consult") >= PERM_EDIT


def _need_edit():
    if not _can_edit():
        abort(403)


def _doc_or_404(doc_id: int) -> dict:
    doc = models.get_document(doc_id)
    if not doc:
        abort(404)
    doc["ai"] = _parse_ai(doc)
    doc["status_label"] = STATUS_LABELS.get(doc.get("status") or "", doc.get("status") or "")
    return doc


def _parse_ai(doc: dict) -> dict:
    try:
        ai = json.loads(doc.get("ai_json") or "{}")
        return ai if isinstance(ai, dict) else {}
    except ValueError:
        return {}


def _audit(action: str, doc_id: int, detail: str = ""):
    try:
        models.log_audit(user_id=g.user["id"], username=g.user["username"], action=action,
                         target_type="document", target_id=doc_id, detail=detail[:200] or None,
                         ip=request.remote_addr)
    except Exception:
        logger.exception("문서 감사 로그 실패")


# ───────────────────── 화면 ─────────────────────

@bp.route("/documents")
@login_required
def documents_list():
    comm = request.args.get("comm")
    if comm:                                     # 인박스 카드 '원본·요약' 링크
        try:
            doc = models.document_by_comm(int(comm))
        except (TypeError, ValueError):
            doc = None
        if doc:
            return redirect(f"/documents/{doc['id']}")
    status = (request.args.get("status") or "").strip()
    q = (request.args.get("q") or "").strip()
    rows = models.list_documents(300, status=status or None, q=q or None)
    for r in rows:
        r["ai"] = _parse_ai(r)
        r["status_label"] = STATUS_LABELS.get(r.get("status") or "", r.get("status") or "")
    counts = models.document_counts()
    return render_template("documents.html", rows=rows, counts=counts, status=status, q=q,
                           fax=fax_inbox.status(), can_edit=_can_edit(), STATUS_LABELS=STATUS_LABELS)


@bp.route("/documents/<int:doc_id>")
@login_required
def document_detail(doc_id):
    doc = _doc_or_404(doc_id)
    comm = models.get_communication(doc["comm_id"]) if doc.get("comm_id") else None
    consult = models.get_consultation(doc["consultation_id"]) if doc.get("consultation_id") else None
    candidates = models.patients_by_name(doc.get("patient_name_ai") or "") if doc.get("patient_name_ai") else []
    _audit("view_document", doc_id)
    return render_template("document_detail.html", doc=doc, comm=comm, consult=consult,
                           candidates=candidates, can_edit=_can_edit(), fax=fax_inbox.status(),
                           file_exists=(not doc.get("file_deleted_at")) and Path(doc.get("stored_path") or "").is_file())


@bp.route("/documents/<int:doc_id>/file")
@login_required
def document_file(doc_id):
    doc = _doc_or_404(doc_id)
    if doc.get("file_deleted_at"):
        abort(410)                                   # 보관기간 경과로 원본 삭제됨 — 요약만 남음
    path = Path(doc.get("stored_path") or "")
    if not path.is_file():
        abort(404)
    mime = doc.get("mime") or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    as_attachment = request.args.get("download") == "1"
    resp = send_file(str(path), mimetype=mime, as_attachment=as_attachment,
                     download_name=doc.get("filename") or path.name, conditional=True)
    resp.headers["Cache-Control"] = "no-store, private"
    return resp


# ───────────────────── API ─────────────────────

@bp.route("/api/documents/scan", methods=["POST"])
@login_required
def api_documents_scan():
    _need_edit()
    if not fax_inbox.enabled():
        return jsonify({"error": "팩스 수신 폴더(FAX_INBOX_DIR)가 설정되지 않았습니다."}), 400
    try:
        n = fax_inbox.scan_once()
    except Exception as e:
        logger.exception("팩스 수동 스캔 실패")
        return jsonify({"error": f"스캔 실패: {e}"}), 500
    return jsonify({"ok": True, "registered": n})


@bp.route("/api/documents/upload", methods=["POST"])
@login_required
def api_documents_upload():
    _need_edit()
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "파일을 선택하세요."}), 400
    data = f.read()
    if len(data) > 40 * 1024 * 1024:
        return jsonify({"error": "40MB 이하 파일만 올릴 수 있습니다."}), 400
    try:
        doc_id = fax_inbox.save_upload(f.filename, data, created_by=g.user["username"])
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception("문서 업로드 실패")
        return jsonify({"error": f"저장 실패: {e}"}), 500
    if doc_id is None:
        return jsonify({"error": "이미 등록된 파일입니다(내용 동일)."}), 409
    _audit("upload_document", doc_id, f.filename)
    return jsonify({"ok": True, "id": doc_id})


@bp.route("/api/documents/<int:doc_id>", methods=["POST"])
@login_required
def api_document_update(doc_id):
    """직원이 AI 판독값(이름·주병명·날짜·보낸 곳)을 고친다. rename=1이면 파일명도 다시 정리."""
    _need_edit()
    doc = _doc_or_404(doc_id)
    payload = request.get_json(silent=True) or {}
    fields = {}
    for key, col, limit in (("patient_name", "patient_name_ai", 40), ("diagnosis", "diagnosis_ai", 60),
                            ("sender", "sender_ai", 80), ("doc_date", "doc_date", 10)):
        if key in payload:
            fields[col] = (str(payload.get(key) or "").strip()[:limit]) or None
    if "doc_date" in fields and fields["doc_date"]:
        from datetime import datetime
        try:
            datetime.strptime(fields["doc_date"], "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "날짜는 YYYY-MM-DD 형식"}), 400
    if fields:
        models.update_document(doc_id, **fields)
    new_path = None
    if payload.get("rename"):
        try:
            new_path = fax_inbox.move_to_archive(doc_id)
        except Exception as e:
            logger.exception("파일명 정리 실패 (doc #%s)", doc_id)
            return jsonify({"error": f"파일명 변경 실패: {e}"}), 500
    doc = models.get_document(doc_id)
    if doc.get("comm_id"):
        models.update_communication(doc["comm_id"], summary=fax_inbox._comm_summary(None, doc))
    _audit("update_document", doc_id, ", ".join(fields.keys()))
    return jsonify({"ok": True, "filename": doc.get("filename"), "stored_path": new_path or doc.get("stored_path")})


@bp.route("/api/documents/<int:doc_id>/analyze", methods=["POST"])
@login_required
def api_document_analyze(doc_id):
    _need_edit()
    _doc_or_404(doc_id)
    import llm
    if not llm.fax_ai_enabled():
        return jsonify({"error": "AI 판독이 꺼져 있습니다(ANTHROPIC_API_KEY / FAX_AI_ENABLED)."}), 400
    ok = fax_inbox.analyze_document(doc_id, force=True)
    doc = models.get_document(doc_id)
    _audit("analyze_document", doc_id, "ok" if ok else (doc.get("ai_error") or "fail"))
    if not ok:
        return jsonify({"error": doc.get("ai_error") or "판독 실패"}), 502
    return jsonify({"ok": True, "filename": doc.get("filename"), "summary": doc.get("ai_summary")})


@bp.route("/api/documents/<int:doc_id>/link", methods=["POST"])
@login_required
def api_document_link(doc_id):
    """기존 환자(또는 상담)에 연결 → 처리 완료. 인박스 카드도 함께 완료."""
    _need_edit()
    doc = _doc_or_404(doc_id)
    payload = request.get_json(silent=True) or {}
    patient_id = payload.get("patient_id")
    consultation_id = payload.get("consultation_id")
    fields = {"status": "done"}
    if consultation_id:
        c = models.get_consultation(int(consultation_id))
        if not c:
            return jsonify({"error": "상담을 찾을 수 없습니다."}), 404
        fields["consultation_id"] = c["id"]
        patient_id = c.get("patient_id") or patient_id
    if patient_id:
        p = models.get_patient(int(patient_id))
        if not p:
            return jsonify({"error": "환자를 찾을 수 없습니다."}), 404
        fields["patient_id"] = p["id"]
    if not fields.get("patient_id") and not fields.get("consultation_id"):
        return jsonify({"error": "연결할 환자 또는 상담을 고르세요."}), 400
    models.update_document(doc_id, **fields)
    if doc.get("comm_id"):
        models.update_communication(doc["comm_id"], status="done",
                                    patient_id=fields.get("patient_id"),
                                    consultation_id=fields.get("consultation_id"))
    _audit("link_document", doc_id, f"patient={fields.get('patient_id')} consult={fields.get('consultation_id')}")
    return jsonify({"ok": True, "patient_id": fields.get("patient_id")})


@bp.route("/api/documents/<int:doc_id>/done", methods=["POST"])
@login_required
def api_document_done(doc_id):
    _need_edit()
    doc = _doc_or_404(doc_id)
    models.update_document(doc_id, status="done")
    if doc.get("comm_id"):
        models.update_communication(doc["comm_id"], status="done")
    _audit("close_document", doc_id)
    return jsonify({"ok": True})


@bp.route("/api/documents/<int:doc_id>/reopen", methods=["POST"])
@login_required
def api_document_reopen(doc_id):
    _need_edit()
    doc = _doc_or_404(doc_id)
    models.update_document(doc_id, status="analyzed" if doc.get("analyzed_at") else "pending")
    if doc.get("comm_id"):
        models.update_communication(doc["comm_id"], status="open")
    return jsonify({"ok": True})


@bp.route("/api/documents/<int:doc_id>", methods=["DELETE"])
@login_required
def api_document_delete(doc_id):
    """자료함에서만 지운다. NAS의 파일은 남긴다(원본 보존)."""
    _need_edit()
    doc = _doc_or_404(doc_id)
    models.delete_document(doc_id)
    if doc.get("comm_id"):
        try:
            models.delete_communication(doc["comm_id"])
        except Exception:
            models.update_communication(doc["comm_id"], status="done")
    _audit("delete_document", doc_id, doc.get("filename") or "")
    return jsonify({"ok": True})
