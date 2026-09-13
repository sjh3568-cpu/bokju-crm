"""차량 운행(픽업) 요청 — 입원 예정 환자의 모병원 픽업을 CRM에서 기록하고
운행팀 구글 시트('운행 공유')에 자동으로 한 줄 넣는다.

흐름
  상담사: 상담 상세 → 🚐 운행 요청 칸에 필요 여부·이동수단·장소·도착시간·연락처 저장
  CRM   : 저장 즉시 시트의 해당 날짜 탭에 '진료협력' 행 추가 (탭이 없으면 전송 대기)
  스케줄: 1시간마다 전송 대기분 재시도 + 운행팀이 채운 배정자·차량을 읽어와 표시
  대시보드: 내일 입원인데 운행 여부 미정 / 전송 안 됨 / 배정 대기 경고

시트 쪽은 Apps Script 웹앱(docs/apps-script/transport_sheet.gs)이 받는다. 탭은 절대
만들지 않는다 — 운행팀이 주관해서 추가하는 것이라 없으면 기다린다.

.env
  TRANSPORT_SHEET_URL    Apps Script 웹앱 URL (없으면 CRM 안에만 기록)
  TRANSPORT_SHEET_TOKEN  스크립트와 맞춘 비밀 토큰
  TRANSPORT_MOBILITY_OPTIONS  이동수단 선택지 (기본 "W/C,walk,Rec") — 시트 드롭다운 값과 같게
  TRANSPORT_SYNC_MINUTES 재시도·배정 읽기 주기 (기본 60)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import date, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Blueprint, g, jsonify, request, session

import models
from auth import login_required

logger = logging.getLogger("bokju.transport")
bp = Blueprint("transport", __name__)

DEPARTMENT = "진료협력"
DEFAULT_REASON = "픽업(입원)"
REASON_OPTIONS = ["픽업(입원)", "픽업", "외진", "퇴원"]
STATUS_LABELS = {
    "draft": "CRM에만 기록", "pending": "전송 대기 (시트에 날짜 탭 없음)", "sent": "시트 전송 완료",
    "error": "전송 실패", "skip": "운행 불필요",
}


def sheet_url() -> str:
    return (os.getenv("TRANSPORT_SHEET_URL") or "").strip()


def sheet_token() -> str:
    return (os.getenv("TRANSPORT_SHEET_TOKEN") or "").strip()


def mobility_options() -> list[str]:
    raw = os.getenv("TRANSPORT_MOBILITY_OPTIONS") or "W/C,walk,Rec"
    return [x.strip() for x in raw.split(",") if x.strip()]


def init_schema(conn=None):
    own = conn is None
    conn = conn or models.get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transport_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            consultation_id INTEGER NOT NULL UNIQUE REFERENCES consultations(id) ON DELETE CASCADE,
            patient_id INTEGER NOT NULL,
            needed TEXT,                    -- 'yes' | 'no' | NULL(미정)
            pickup_date DATE,               -- 시트 탭 날짜 (= 입원예정일)
            mobility TEXT,                  -- W/C · walk · Rec
            reason TEXT,                    -- 픽업(입원) 등
            place TEXT,                     -- 요청 장소 (안동병원 814호)
            arrive_time TEXT,               -- '14:00'
            contact TEXT,                   -- 형님 010-… (메모)
            requested_by TEXT,              -- 요청자(상담사) 표시명
            sheet_status TEXT DEFAULT 'draft',
            sheet_tab TEXT,
            sheet_row INTEGER,
            sent_at DATETIME,
            last_error TEXT,
            driver TEXT,                    -- 시트에서 읽어온 배정자
            vehicle TEXT,                   -- 시트에서 읽어온 배정 차량
            assigned_checked_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_transport_date ON transport_requests(pickup_date, sheet_status)")
    if own:
        conn.commit(); conn.close()


# ── 조회 ─────────────────────────────────────────────────────────────
def get_request(cid: int):
    conn = models.get_db()
    try:
        row = conn.execute("SELECT * FROM transport_requests WHERE consultation_id=?", (cid,)).fetchone()
    except sqlite3.OperationalError:
        # 앱 기동 전(테스트·마이그레이션)에 화면이 먼저 열린 경우 — 표를 만들고 다시 읽는다
        init_schema(conn); conn.commit()
        row = conn.execute("SELECT * FROM transport_requests WHERE consultation_id=?", (cid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def info_for_template(cid: int) -> dict:
    """상담 상세 카드용 — 요청 + 선택지 + 상태 문구. 템플릿 전역으로 노출."""
    cache = getattr(g, "_transport_cache", None)
    if cache is None:
        cache = g._transport_cache = {}
    if cid not in cache:
        r = get_request(cid)
        cache[cid] = {
            "req": r,
            "status_label": STATUS_LABELS.get((r or {}).get("sheet_status") or "draft", ""),
            "mobility_options": mobility_options(),
            "reason_options": REASON_OPTIONS,
            "sheet_configured": bool(sheet_url()),
        }
    return cache[cid]


def _display_name() -> str:
    u = getattr(g, "user", None) or {}
    return (u.get("display_name") or u.get("username") or "").strip()


# ── 시트 통신 ─────────────────────────────────────────────────────────
def _call_sheet(action: str, **payload) -> dict:
    """Apps Script 웹앱 호출. 302 리다이렉트를 따라가야 본문이 온다."""
    url = sheet_url()
    if not url:
        return {"ok": False, "error": "NOT_CONFIGURED"}
    body = json.dumps({"token": sheet_token(), "action": action, **payload}, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8", "replace")
    except HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}"}
    except (URLError, TimeoutError, OSError) as e:
        return {"ok": False, "error": f"연결 실패: {e}"}
    try:
        return json.loads(text)
    except ValueError:
        # 로그인 페이지 HTML이 오면 배포 설정(액세스: 모든 사용자)이 틀린 것
        return {"ok": False, "error": "응답이 JSON이 아님 — 웹앱 배포 '액세스: 모든 사용자' 확인"}


def _row_values(r: dict) -> list:
    """시트 B~I 열 순서: 요청부서·요청자·이름·이동수단·요청이유·요청 장소·도착시간·연락처."""
    arrive = (r.get("arrive_time") or "").strip()
    if arrive and "도착" not in arrive and "출발" not in arrive:
        arrive += " 도착"
    return [DEPARTMENT, r.get("requested_by") or "", r.get("patient_name") or "", r.get("mobility") or "",
            r.get("reason") or DEFAULT_REASON, r.get("place") or "", arrive, r.get("contact") or ""]


def push(cid: int) -> dict:
    """요청 한 건을 시트로 보낸다(이미 보낸 행이면 갱신). 결과를 DB 상태에 남긴다."""
    conn = models.get_db()
    r = conn.execute("""SELECT t.*, p.name AS patient_name FROM transport_requests t
                        JOIN patients p ON p.id=t.patient_id WHERE t.consultation_id=?""", (cid,)).fetchone()
    if not r:
        conn.close(); return {"ok": False, "error": "요청 없음"}
    r = dict(r)
    if r["needed"] != "yes":
        conn.close(); return {"ok": True, "status": r["sheet_status"]}
    if not r.get("pickup_date"):
        conn.execute("UPDATE transport_requests SET sheet_status='draft', last_error='입원예정일 없음', updated_at=CURRENT_TIMESTAMP WHERE id=?", (r["id"],))
        conn.commit(); conn.close(); return {"ok": False, "error": "입원예정일이 없어 시트 탭을 정할 수 없습니다"}
    if not sheet_url():
        conn.execute("UPDATE transport_requests SET sheet_status='draft', last_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (r["id"],))
        conn.commit(); conn.close(); return {"ok": True, "status": "draft"}

    res = _call_sheet("upsert", date=r["pickup_date"], row=_row_values(r),
                      match_row=r.get("sheet_row") if r.get("sheet_tab") else None, match_name=r["patient_name"])
    if res.get("ok"):
        conn.execute("""UPDATE transport_requests SET sheet_status='sent', sheet_tab=?, sheet_row=?, sent_at=CURRENT_TIMESTAMP,
                        last_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?""", (res.get("tab"), res.get("row"), r["id"]))
        status = "sent"
    elif res.get("error") == "NO_TAB":
        conn.execute("UPDATE transport_requests SET sheet_status='pending', last_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (r["id"],))
        status = "pending"
    else:
        err = str(res.get("error"))
        if err == "WRITE_NOT_PERSISTED":
            err = "시트에 값이 저장되지 않음 — 스크립트 실행 계정의 편집 권한 또는 시트 보호 확인"
        elif err == "BAD_TOKEN":
            err = "토큰 불일치 — .env TRANSPORT_SHEET_TOKEN 과 스크립트 TOKEN 을 맞추세요"
        conn.execute("UPDATE transport_requests SET sheet_status='error', last_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (err[:300], r["id"]))
        status = "error"
    conn.commit(); conn.close()
    return {"ok": status in ("sent", "pending"), "status": status, "error": res.get("error"), "tab": res.get("tab"), "row": res.get("row")}


def refresh_assignments(days_back: int = 1, days_ahead: int = 14) -> dict:
    """시트에서 진료협력 행의 배정자·차량을 읽어와 sent 상태 요청에 붙인다."""
    if not sheet_url():
        return {"ok": False, "error": "NOT_CONFIGURED"}
    conn = models.get_db()
    lo = (date.today() - timedelta(days=days_back)).isoformat()
    hi = (date.today() + timedelta(days=days_ahead)).isoformat()
    rows = [dict(x) for x in conn.execute("""SELECT t.*, p.name AS patient_name FROM transport_requests t JOIN patients p ON p.id=t.patient_id
                                             WHERE t.sheet_status='sent' AND t.pickup_date BETWEEN ? AND ?""", (lo, hi))]
    updated = 0
    for d in sorted({x["pickup_date"] for x in rows}):
        res = _call_sheet("read", date=d)
        if not res.get("ok"):
            continue
        by_row = {int(x["row"]): x for x in res.get("rows") or [] if x.get("row")}
        by_name = {(x.get("name") or "").strip(): x for x in res.get("rows") or []}
        for r in [x for x in rows if x["pickup_date"] == d]:
            hit = by_row.get(r.get("sheet_row") or -1) or by_name.get(r["patient_name"].strip())
            if not hit:
                continue
            conn.execute("""UPDATE transport_requests SET driver=?, vehicle=?, sheet_row=?, assigned_checked_at=CURRENT_TIMESTAMP
                            WHERE id=?""", ((hit.get("driver") or "").strip() or None, (hit.get("vehicle") or "").strip() or None,
                                            hit.get("row") or r.get("sheet_row"), r["id"]))
            updated += 1
    conn.commit(); conn.close()
    return {"ok": True, "updated": updated}


def retry_pending() -> dict:
    conn = models.get_db()
    ids = [x[0] for x in conn.execute("""SELECT consultation_id FROM transport_requests
                                          WHERE needed='yes' AND sheet_status IN ('pending','error') AND pickup_date >= ?""",
                                       ((date.today() - timedelta(days=1)).isoformat(),))]
    conn.close()
    out = {"tried": len(ids), "sent": 0}
    for cid in ids:
        if push(cid).get("status") == "sent":
            out["sent"] += 1
    return out


def sync_once(trigger: str = "manual") -> dict:
    if not sheet_url():
        return {"ok": False, "error": "NOT_CONFIGURED"}
    try:
        a = retry_pending(); b = refresh_assignments()
        logger.info("운행 시트 동기화(%s): 재시도 %s, 배정 갱신 %s", trigger, a, b)
        return {"ok": True, "retry": a, "assignments": b}
    except Exception as e:
        logger.exception("운행 시트 동기화 실패")
        return {"ok": False, "error": str(e)}


def _loop():
    minutes = max(5, int(os.getenv("TRANSPORT_SYNC_MINUTES") or 60))
    while True:
        threading.Event().wait(minutes * 60)
        sync_once("scheduler")


def start_scheduler():
    if not sheet_url():
        logger.info("운행 시트 연동 꺼짐 (TRANSPORT_SHEET_URL 없음)")
        return
    threading.Thread(target=_loop, name="transport-sheet-sync", daemon=True).start()
    logger.info("운행 시트 동기화 시작 — %s분마다", os.getenv("TRANSPORT_SYNC_MINUTES") or 60)


# ── 대시보드 경고 ────────────────────────────────────────────────────
def dashboard_alerts(today: date | None = None) -> list[dict]:
    """내일까지 입원 예정인데 운행 준비가 안 된 환자. (kind, tone, title, detail, meta, href, sort)"""
    today = today or date.today()
    hi = (today + timedelta(days=1)).isoformat()
    conn = models.get_db()
    rows = conn.execute("""
        SELECT c.id, c.planned_admission_date, p.name AS patient_name, t.needed, t.sheet_status, t.driver, t.last_error
        FROM consultations c JOIN patients p ON p.id=c.patient_id
        LEFT JOIN transport_requests t ON t.consultation_id=c.id
        WHERE c.admission_status IN ('입원예정','입원대기') AND c.planned_admission_date IS NOT NULL
          AND c.planned_admission_date BETWEEN ? AND ?
    """, (today.isoformat(), hi)).fetchall()
    conn.close()
    out = []
    for r in rows:
        when = "오늘 입원" if r["planned_admission_date"] == today.isoformat() else "내일 입원"
        href = f"/consult/{r['id']}#transport"
        if not r["needed"]:
            out.append(dict(kind="운행", tone="danger", title=r["patient_name"], detail="차량 운행 필요 여부 미정 — 상담 상세 🚐 칸에서 지정",
                            meta=when, href=href, sort=3))
        elif r["needed"] == "yes" and r["sheet_status"] == "pending":
            out.append(dict(kind="운행", tone="danger", title=r["patient_name"], detail=f"운행 시트에 {r['planned_admission_date'][5:].replace('-', '/')} 탭이 없어 전송 대기 — 운행팀에 탭 요청",
                            meta=when, href=href, sort=4))
        elif r["needed"] == "yes" and r["sheet_status"] == "error":
            out.append(dict(kind="운행", tone="danger", title=r["patient_name"], detail=f"운행 시트 전송 실패: {r['last_error'] or ''}"[:80],
                            meta=when, href=href, sort=4))
        elif r["needed"] == "yes" and r["sheet_status"] == "sent" and not r["driver"]:
            out.append(dict(kind="운행", tone="warn", title=r["patient_name"], detail="운행팀 배정자 미지정 (시트 확인)",
                            meta=when, href=href, sort=20))
    return out


# ── API ──────────────────────────────────────────────────────────────
@bp.route("/api/consult/<int:cid>/transport", methods=["GET"])
@login_required
def api_get(cid):
    return jsonify({"request": get_request(cid)})


@bp.route("/api/consult/<int:cid>/transport", methods=["POST"])
@login_required
def api_save(cid):
    c = models.get_consultation(cid)
    if not c:
        return jsonify({"error": "not found"}), 404
    p = request.get_json(silent=True) or {}
    needed = p.get("needed")
    if needed not in ("yes", "no"):
        return jsonify({"error": "운행 필요 여부(yes/no)를 지정하세요"}), 400
    f = {k: ((p.get(k) or "").strip()[:200] or None) for k in ("mobility", "reason", "place", "arrive_time", "contact")}
    if needed == "yes":
        missing = [lbl for k, lbl in (("mobility", "이동수단"), ("place", "요청 장소"), ("arrive_time", "도착시간")) if not f[k]]
        if missing:
            return jsonify({"error": "필수: " + ", ".join(missing)}), 400
    pickup_date = (p.get("pickup_date") or c.get("planned_admission_date") or "").strip() or None
    conn = models.get_db()
    existing = conn.execute("SELECT id, sheet_status FROM transport_requests WHERE consultation_id=?", (cid,)).fetchone()
    status = "skip" if needed == "no" else ("sent" if existing and existing["sheet_status"] == "sent" else "draft")
    if existing:
        conn.execute("""UPDATE transport_requests SET needed=?, pickup_date=?, mobility=?, reason=?, place=?, arrive_time=?, contact=?,
                        requested_by=COALESCE(NULLIF(?,''), requested_by), sheet_status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                     (needed, pickup_date, f["mobility"], f["reason"] or DEFAULT_REASON, f["place"], f["arrive_time"], f["contact"],
                      _display_name(), status, existing["id"]))
    else:
        conn.execute("""INSERT INTO transport_requests(consultation_id, patient_id, needed, pickup_date, mobility, reason, place, arrive_time,
                        contact, requested_by, sheet_status) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                     (cid, c["patient_id"], needed, pickup_date, f["mobility"], f["reason"] or DEFAULT_REASON, f["place"], f["arrive_time"],
                      f["contact"], _display_name(), status))
    conn.commit(); conn.close()
    result = push(cid) if needed == "yes" else {"ok": True, "status": "skip"}
    try:
        models.log_audit(user_id=g.user["id"], username=g.user["username"], action="transport_save",
                         target_type="consultation", target_id=cid, detail=f"{needed} {result.get('status')}", ip=request.remote_addr)
    except Exception:
        pass
    r = get_request(cid)
    return jsonify({"ok": True, "request": r, "push": result, "status_label": STATUS_LABELS.get(r["sheet_status"], "")})


@bp.route("/api/transport/sync", methods=["POST"])
@login_required
def api_sync():
    return jsonify(sync_once("manual"))


@bp.route("/api/transport/ping", methods=["POST"])
@login_required
def api_ping():
    """연동 점검 — 스크립트가 살아 있고 토큰이 맞는지, 오늘 탭이 있는지."""
    if not sheet_url():
        return jsonify({"ok": False, "error": "TRANSPORT_SHEET_URL 미설정"})
    return jsonify(_call_sheet("ping", date=date.today().isoformat()))


@bp.route("/transport/check")
@login_required
def check_page():
    """브라우저 주소창에 /transport/check 만 쳐도 연동 상태를 볼 수 있게 — 비개발자용 점검."""
    lines = []
    if not sheet_url():
        state, tone = "미설정", "bad"
        lines.append(".env 에 TRANSPORT_SHEET_URL / TRANSPORT_SHEET_TOKEN 이 없거나, 넣은 뒤 서버를 다시 시작하지 않았습니다.")
    else:
        res = _call_sheet("ping", date=date.today().isoformat())
        if res.get("ok"):
            state, tone = "정상 — 시트 연결됨", "ok"
            lines.append(f"시트 이름: {res.get('sheet')}")
            lines.append(f"탭 개수: {res.get('tabs')}")
            lines.append(f"오늘 탭: {res.get('tab_for_date') or '없음 (운행팀이 만들면 됨)'}")
            if res.get("running_as"):
                lines.append(f"스크립트 실행 계정: {res.get('running_as')}")
            if res.get("editors") is not None:
                lines.append(f"시트 편집 가능 계정: {res.get('editors')}")
        else:
            state, tone = "실패", "bad"
            hints = {"BAD_TOKEN": "토큰이 스크립트(TOKEN)와 .env(TRANSPORT_SHEET_TOKEN)에서 서로 다릅니다.",
                     "NOT_CONFIGURED": ".env 설정이 없습니다."}
            lines.append(hints.get(res.get("error"), str(res.get("error"))))
    body = "".join(f"<li>{x}</li>" for x in lines)
    color = {"ok": "#166534", "bad": "#b91c1c"}[tone]
    return (f'<!doctype html><meta charset="utf-8"><title>운행 시트 연동 점검</title>'
            f'<body style="font-family:sans-serif;padding:24px;max-width:720px">'
            f'<h2>🚐 운행 시트 연동 점검</h2>'
            f'<p style="font-size:1.3em;font-weight:800;color:{color}">상태: {state}</p><ul>{body}</ul>'
            f'<p style="color:#64748b;font-size:.9em">설정 방법: docs/TRANSPORT-SHEET.md · <a href="/">CRM으로</a></p></body>')


@bp.app_template_global("transport_info")
def _tg_transport_info(cid):
    return info_for_template(cid)
