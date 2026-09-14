"""재원관리 → 「입원·퇴원 이력」 탭 — 기간별 입원·퇴원 명부(원무 명부 회차 기준) + 필터 + 엑셀.

원무 명부 회차(admission_episodes.roster_key)가 실제 입·퇴원일을 들고 있으므로 상담 상태가
아니라 회차로 센다. 상담이 붙은 회차는 퇴원 장소·사유·담당 상담사·진단을 상담에서 가져온다.

  report(args)  → {filters, rows, summary, options}
  /ward/moves.xlsx  현재 필터 그대로 엑셀
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from io import BytesIO

from flask import Blueprint, request, send_file

import models
from auth import login_required

bp = Blueprint("ward_moves", __name__)

KINDS = {"": "전체", "in": "입원", "out": "퇴원"}
DEFAULT_DAYS = 30


def _care_label(value) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if v.startswith("비회복"):
        return "비회복기"
    if v.startswith("회복"):
        return "회복기"
    return v


def _parse_date(s: str):
    try:
        return date.fromisoformat((s or "").strip()[:10])
    except ValueError:
        return None


def _filters(args) -> dict:
    today = date.today()
    d_to = _parse_date(args.get("date_to")) or today
    d_from = _parse_date(args.get("date_from")) or (d_to - timedelta(days=DEFAULT_DAYS - 1))
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    kind = (args.get("kind") or "").strip()
    if kind not in KINDS:
        kind = ""
    return {
        "date_from": d_from.isoformat(), "date_to": d_to.isoformat(), "kind": kind,
        "doctor": (args.get("doctor") or "").strip(), "ward": (args.get("ward") or "").strip(),
        "care": (args.get("care") or "").strip(), "destination": (args.get("destination") or "").strip(),
        "reason": (args.get("reason") or "").strip(), "counselor": (args.get("counselor") or "").strip(),
        "q": (args.get("q") or "").strip(),
    }


def _load(d_from: str, d_to: str) -> list[dict]:
    """기간에 입원했거나 퇴원한 회차 — 한 회차가 둘 다면 두 줄(입원·퇴원)로 나온다."""
    conn = models.get_db()
    try:
        eps = [dict(r) for r in conn.execute(
            """SELECT e.id AS episode_id, e.patient_id, e.admitted_at, e.discharged_at, e.room_number, e.ward,
                      e.attending_doctor, e.diagnosis_name, e.care_type,
                      p.name AS patient_name, p.gender, p.birth_year
               FROM admission_episodes e JOIN patients p ON p.id = e.patient_id
               WHERE e.roster_key IS NOT NULL AND e.admitted_at IS NOT NULL AND e.admitted_at != ''
                 AND ((substr(e.admitted_at,1,10) BETWEEN ? AND ?)
                      OR (e.discharged_at IS NOT NULL AND e.discharged_at != '' AND substr(e.discharged_at,1,10) BETWEEN ? AND ?))
               ORDER BY e.admitted_at DESC""", (d_from, d_to, d_from, d_to))]
        if not eps:
            return []
        # 회차에 붙은 상담 — 퇴원 장소·사유·담당자·진단은 상담이 들고 있다
        owner = {sp["episode_id"]: sp["consultation_id"] for sp in models.admission_spans() if sp.get("consultation_id")}
        cids = sorted({owner[e["episode_id"]] for e in eps if e["episode_id"] in owner})
        cons = {}
        if cids:
            ph = ",".join("?" * len(cids))
            for r in conn.execute(
                    f"""SELECT id, counselor, discharge_destination, discharge_reason, attending_doctor, patient_age,
                               primary_diagnosis, diseases
                        FROM consultations WHERE id IN ({ph})""", cids):
                cons[r["id"]] = dict(r)
    finally:
        conn.close()

    today = date.today()
    out = []
    for e in eps:
        con = cons.get(owner.get(e["episode_id"])) or {}
        adm = _parse_date(e["admitted_at"]); dis = _parse_date(e.get("discharged_at") or "")
        age = con.get("patient_age")
        if age is None and e.get("birth_year"):
            age = today.year - int(e["birth_year"])
        dx = e.get("diagnosis_name") or con.get("primary_diagnosis") or ""
        if not dx and con.get("diseases"):
            try:
                import json
                dz = json.loads(con["diseases"]) if isinstance(con["diseases"], str) else con["diseases"]
                dx = ", ".join(dz[:2]) if isinstance(dz, list) else ""
            except Exception:
                dx = ""
        base = {
            "episode_id": e["episode_id"], "consultation_id": con.get("id"), "patient_id": e["patient_id"],
            "patient_name": e["patient_name"], "gender": e.get("gender"), "age": age,
            "ward": e.get("ward") or "", "room": e.get("room_number") or "",
            "doctor": (e.get("attending_doctor") or con.get("attending_doctor") or "").strip(),
            "care": _care_label(e.get("care_type")), "dx": dx,
            "admitted_at": adm.isoformat() if adm else "", "discharged_at": dis.isoformat() if dis else "",
            "counselor": (con.get("counselor") or "").strip(),
            "destination": (con.get("discharge_destination") or "").strip(),
            "reason": (con.get("discharge_reason") or "").strip(),
            "stay_days": ((dis or today) - adm).days + 1 if adm else None,
        }
        if adm and d_from <= adm.isoformat() <= d_to:
            out.append({**base, "kind": "in", "date": adm.isoformat()})
        if dis and d_from <= dis.isoformat() <= d_to:
            out.append({**base, "kind": "out", "date": dis.isoformat()})
    out.sort(key=lambda r: (r["date"], r["kind"] == "in", r["patient_name"]), reverse=True)
    return out


def _match(r: dict, f: dict) -> bool:
    if f["kind"] and r["kind"] != f["kind"]:
        return False
    if f["doctor"] and r["doctor"] != f["doctor"]:
        return False
    if f["ward"] and r["ward"] != f["ward"]:
        return False
    if f["care"] and r["care"] != f["care"]:
        return False
    if f["counselor"] and r["counselor"] != f["counselor"]:
        return False
    if f["destination"] and f["destination"].casefold() not in (r["destination"] or "").casefold():
        return False
    if f["reason"] and f["reason"].casefold() not in (r["reason"] or "").casefold():
        return False
    if f["q"]:
        hay = " ".join(str(r.get(k) or "") for k in ("patient_name", "dx", "ward", "room", "destination", "reason", "doctor")).casefold()
        if f["q"].casefold() not in hay:
            return False
    return True


def _care_cells(care_in: dict, total: int) -> list[dict]:
    """수가 구분 카드용 — 회복기·비회복기는 0이어도 항상 앞에, 그 외 값·구분 없음은 뒤에."""
    order = [("회복기", "rec"), ("비회복기", "non")]
    cells = [{"label": l, "n": care_in.get(l, 0), "cls": c} for l, c in order]
    for label in sorted(care_in):
        if label not in ("회복기", "비회복기", "구분 없음"):
            cells.append({"label": label, "n": care_in[label], "cls": "etc"})
    cells.append({"label": "구분 없음", "n": care_in.get("구분 없음", 0), "cls": "none"})
    for c in cells:
        c["pct"] = round(c["n"] * 100 / total) if total else 0
    return cells


def report(args) -> dict:
    f = _filters(args)
    all_rows = _load(f["date_from"], f["date_to"])
    rows = [r for r in all_rows if _match(r, f)]
    ins = [r for r in rows if r["kind"] == "in"]
    outs = [r for r in rows if r["kind"] == "out"]
    stays = [r["stay_days"] for r in outs if r["stay_days"]]
    care_in = {}
    for r in ins:
        care_in[r["care"] or "구분 없음"] = care_in.get(r["care"] or "구분 없음", 0) + 1
    summary = {
        "admissions": len(ins), "discharges": len(outs), "net": len(ins) - len(outs),
        "avg_stay": round(sum(stays) / len(stays), 1) if stays else None,
        "long_stay": sum(1 for s in stays if s >= 365),
        "care_in": sorted(care_in.items(), key=lambda kv: -kv[1]),
        "care_cells": _care_cells(care_in, len(ins)),
        "days": (date.fromisoformat(f["date_to"]) - date.fromisoformat(f["date_from"])).days + 1,
    }
    def opts(key):
        return sorted({r[key] for r in all_rows if r.get(key)})
    options = {"doctors": opts("doctor"), "wards": opts("ward"), "cares": opts("care"), "counselors": opts("counselor")}
    active = any(f[k] for k in ("kind", "doctor", "ward", "care", "destination", "reason", "counselor", "q"))
    return {"filters": f, "rows": rows, "summary": summary, "options": options, "kinds": KINDS, "filter_active": active}


@bp.route("/ward/moves.xlsx")
@login_required
def moves_xlsx():
    """입원·퇴원 이력을 현재 필터 그대로 엑셀로 — 화면 열 순서와 같다."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    rep = report(request.args)
    wb = Workbook(); ws = wb.active; ws.title = "입원·퇴원 이력"
    headers = ["연번", "날짜", "구분", "환자", "성별", "나이", "병동", "호실", "주치의", "수가", "진단", "입원일", "퇴원일", "재원일수", "퇴원 장소", "퇴원 사유", "담당 상담사"]
    ws.append(headers)
    for i, r in enumerate(rep["rows"], 1):
        ws.append([i, r["date"], KINDS[r["kind"]], r["patient_name"], {"M": "남", "F": "여"}.get(r["gender"], ""),
                   r["age"] if r["age"] is not None else "", r["ward"], r["room"], r["doctor"], r["care"], r["dx"],
                   r["admitted_at"], r["discharged_at"], r["stay_days"] or "", r["destination"], r["reason"], r["counselor"]])
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=i); c.font = Font(bold=True); c.fill = PatternFill("solid", fgColor="E2E8F0")
        ws.column_dimensions[get_column_letter(i)].width = max(8, min(28, len(h) * 2 + 4))
    ws.freeze_panes = "A2"
    buf = BytesIO(); wb.save(buf); buf.seek(0)
    f = rep["filters"]
    name = f"입원퇴원이력_{f['date_from']}_{f['date_to']}.xlsx"
    return send_file(buf, as_attachment=True, download_name=name,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
