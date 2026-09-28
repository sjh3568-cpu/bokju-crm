"""상담내역 구글 시트("상담내역 종합") → CRM 자동 반영 — 과도기용(2026-09-28).

상담사가 아직 구글 시트에 상담을 적는 동안, 시트에 쓴 것이 CRM에도 들어오게 한다.
  · 새 행       → 환자·상담을 새로 만든다(엑셀 적재와 같은 규칙: 같은 환자·같은 날짜는 중복으로 본다)
  · 고친 행     → 그 행에 연결된 상담의 **바뀐 칸만** 고친다. CRM에서 따로 고친 칸은 건드리지 않는다(충돌로 보고)
  · 지운 행     → CRM에서 지우지 않는다. '시트에서 사라짐'으로만 보고한다
  · 이미 CRM에 있던 상담(예전 엑셀 적재분)은 처음 만나는 순간 그 행과 연결만 하고 값은 안 바꾼다(기준선)

시트 쪽은 Apps Script 웹앱(docs/apps-script/consult_sheet.gs)이 시트 전체를 JSON으로 돌려준다 —
운행 시트 연동(transport.py)과 같은 방식이라 구글 클라우드 계정·키 파일이 필요 없다.
행 해석은 tools/excel_import.parse_sheet — 엑셀 적재와 한 코드라 두 경로의 결과가 같다.

행과 상담을 잇는 표 consult_sheet_links: (시트, 행번호, 키=이름|상담일) ↔ consultation_id + 마지막 동기화 때
시트 값(values_json). 행이 끼워 넣어져 번호가 밀리면 키로, 이름·날짜를 고쳐 키가 바뀌면 번호로 다시 찾는다.
'입원환자 대기 명단' 시트는 상담 내역이 아니라 건너뛴다(엑셀 적재 --all과 같다).

.env
  CONSULT_SHEET_URL      Apps Script 웹앱 URL (없으면 연동 꺼짐)
  CONSULT_SHEET_TOKEN    스크립트와 맞춘 비밀 토큰
  CONSULT_SHEET_MINUTES  자동 동기화 주기(분, 기본 10, 최소 5)
자동 반영 켜기/끄기는 관리 → 엑셀 적재 화면의 버튼(상태 파일 auto)으로 — 재시작 없이 바뀐다.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import models

logger = logging.getLogger("bokju.consult_sheet")

# 시트 행 → DB 컬럼. 상담 칸(consultations)과 환자 칸(patients)으로 나뉜다. 엑셀 적재의 _insert_consultation과 같은 목록.
CONSULT_FIELDS = ("consult_date", "counselor", "consult_channel", "patient_age", "current_location_type",
                  "current_location_name", "source_hospital", "admission_purpose", "disease_detail",
                  "admission_status", "actual_admission_date", "planned_admission_date", "wait_started_at",
                  "recontact_memo", "referrer_person")
JSON_FIELDS = ("referral_source_type", "referral_source_detail", "diseases")     # DB에는 JSON 문자열
PATIENT_FIELDS = {"patient_name": "name", "gender": "gender", "residence_sido": "residence_sido",
                  "residence_sigungu": "residence_sigungu", "guardian_name": "guardian_name",
                  "guardian_relation": "guardian_relation", "guardian_phone": "guardian_phone"}
ALL_FIELDS = CONSULT_FIELDS + JSON_FIELDS + tuple(PATIENT_FIELDS)
FIELD_LABELS = {"consult_date": "상담일자", "counselor": "상담자", "consult_channel": "상담방법", "patient_age": "나이",
                "current_location_type": "현재 거처", "current_location_name": "거처 이름", "source_hospital": "병원이름",
                "admission_purpose": "입원 목적", "disease_detail": "병명", "admission_status": "입원여부",
                "actual_admission_date": "입원일", "planned_admission_date": "입원예정일", "wait_started_at": "대기시작일",
                "recontact_memo": "재접촉 메모", "referrer_person": "추천인", "referral_source_type": "유입경로",
                "referral_source_detail": "세부 경로", "diseases": "진단군", "patient_name": "환자이름", "gender": "성별",
                "residence_sido": "거주지(도)", "residence_sigungu": "거주지(시군구)", "guardian_name": "보호자",
                "guardian_relation": "관계", "guardian_phone": "연락처"}
MAX_LIST = 200          # 보고서에 담는 충돌·사라진 행 등 목록 상한
BACKUP_EVERY_HOURS = 12  # 반영 전 DB 백업 — 10분마다 36MB를 복사하지 않게 하루 두 번까지만


# ── 설정·상태 ──────────────────────────────────────────────────────────

def sheet_url() -> str:
    return (os.getenv("CONSULT_SHEET_URL") or "").strip()


def sheet_token() -> str:
    return (os.getenv("CONSULT_SHEET_TOKEN") or "").strip()


def sync_minutes() -> int:
    try:
        return max(5, int(os.getenv("CONSULT_SHEET_MINUTES") or 10))
    except ValueError:
        return 10


def configured() -> bool:
    return bool(sheet_url() and sheet_token())


def _status_path() -> Path:
    db_path = os.getenv("BOKJU_DB_PATH")
    base = Path(db_path).parent if db_path else Path("./data")
    return base / "consult_sheet_sync.json"


def status() -> dict:
    try:
        return json.loads(_status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_status(**fields) -> dict:
    data = {**status(), **fields, "at": datetime.now().isoformat(timespec="seconds")}
    p = _status_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def auto_enabled() -> bool:
    return status().get("auto") is True


def set_auto(on: bool) -> None:
    _write_status(auto=bool(on))


def panel_info() -> dict:
    """관리 → 엑셀 적재 화면의 '구글 시트 자동 반영' 카드용."""
    st = status()
    return {"configured": configured(), "auto": st.get("auto") is True, "minutes": sync_minutes(),
            "last": st.get("last"), "last_report_text": st.get("last_report_text"),
            "link": (os.getenv("CONSULT_SHEET_LINK") or "").strip() or st.get("sheet_link") or ""}


# ── 엑셀 적재 모듈(tools/excel_import.py) — tools/에 __init__.py가 없어 경로로 읽는다 ──

_EI = {"mod": None}


def _ei():
    if _EI["mod"] is None:
        path = Path(__file__).resolve().parent / "tools" / "excel_import.py"
        spec = importlib.util.spec_from_file_location("bokju_excel_import_for_sheet", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _EI["mod"] = mod
    return _EI["mod"]


# ── 스키마 ──────────────────────────────────────────────────────────────

def init_schema(conn=None):
    own = conn is None
    if own:
        conn = models.get_db()
    try:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS consult_sheet_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sheet_name TEXT NOT NULL,
            row_no INTEGER NOT NULL,
            row_key TEXT NOT NULL,
            consultation_id INTEGER NOT NULL UNIQUE REFERENCES consultations(id) ON DELETE CASCADE,
            patient_id INTEGER,
            values_json TEXT NOT NULL,
            row_hash TEXT NOT NULL,
            synced_at TEXT NOT NULL,
            missing_since TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_consult_sheet_links_sheet ON consult_sheet_links(sheet_name, row_no)")
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


# ── 시트 읽기 ───────────────────────────────────────────────────────────

class SyncError(Exception):
    pass


class _Sheet:
    """openpyxl Worksheet 흉내 — parse_sheet가 쓰는 iter_rows(min_row, max_row, values_only)만 있다."""

    def __init__(self, rows):
        self._rows = [list(r) for r in rows]
        self._width = max((len(r) for r in self._rows), default=0)

    def iter_rows(self, min_row=1, max_row=None, values_only=True, max_col=None):
        end = len(self._rows) if max_row is None else min(max_row, len(self._rows))
        for i in range(max(0, min_row - 1), end):
            r = self._rows[i] + [None] * (self._width - len(self._rows[i]))
            if max_col:
                r = r[:max_col]
            yield tuple(None if v == "" else v for v in r)


class SheetBook:
    """Apps Script dump → parse_sheet에 넘길 워크북 모양."""

    def __init__(self, sheets):
        self._sheets = {s["name"]: _Sheet(s.get("rows") or []) for s in sheets}
        self.sheetnames = [s["name"] for s in sheets]

    def __getitem__(self, name):
        return self._sheets[name]


def fetch_sheets() -> list[dict]:
    """Apps Script 웹앱에서 시트 전체를 받는다 → [{name, rows:[[셀,...],...]}, ...]. 302를 따라가야 본문이 온다."""
    body = json.dumps({"token": sheet_token(), "action": "dump"}).encode("utf-8")
    req = Request(sheet_url(), data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=120) as resp:
            text = resp.read().decode("utf-8", "replace")
    except HTTPError as e:
        raise SyncError(f"HTTP {e.code}") from e
    except (URLError, TimeoutError, OSError) as e:
        raise SyncError(f"연결 실패: {e}") from e
    try:
        res = json.loads(text)
    except ValueError:
        raise SyncError("응답이 JSON이 아님 — 웹앱 배포 '액세스: 모든 사용자' 확인") from None
    if not isinstance(res, dict) or not res.get("ok"):
        raise SyncError(str((res or {}).get("error") if isinstance(res, dict) else res) or "알 수 없는 오류")
    if res.get("url"):
        _write_status(sheet_link=res["url"])
    return res.get("sheets") or []


# ── 값 비교 ─────────────────────────────────────────────────────────────

def _norm(v):
    """비교용 정규화 — 빈 값은 None, JSON 문자열은 풀어서, 숫자는 int로. DB 값과 시트 값을 같은 모양으로 놓는다."""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        if s[:1] in "[{":
            try:
                return _norm(json.loads(s))
            except ValueError:
                pass
        return s
    if isinstance(v, bool):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    return v


def _snapshot(parsed: dict) -> dict:
    return {f: parsed.get(f) for f in ALL_FIELDS}


def _row_hash(snap: dict) -> str:
    return hashlib.sha1(json.dumps(snap, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _db_value(field, parsed_value):
    if field in JSON_FIELDS:
        return json.dumps(parsed_value, ensure_ascii=False) if parsed_value else None
    return parsed_value if parsed_value not in ("",) else None


def _row_key(parsed: dict) -> str:
    return f"{parsed.get('patient_name')}|{parsed.get('consult_date')}"


# ── 동기화 본체 ─────────────────────────────────────────────────────────

_run_lock = threading.Lock()


def run(apply: bool = False, trigger: str = "manual", book=None) -> dict:
    """시트 전체를 읽어 CRM과 맞춘다. apply=False면 무엇을 할지만 세고 DB는 그대로(트랜잭션 롤백).

    book을 주면(테스트) 시트를 받지 않고 그것을 쓴다. 보고서(dict)를 돌려주고 상태 파일에도 남긴다.
    """
    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "error": "이미 동기화가 진행 중입니다"}
    try:
        started = datetime.now()
        report = {"ok": True, "apply": apply, "trigger": trigger, "started": started.isoformat(timespec="seconds"),
                  "sheets": [], "totals": {"rows": 0, "new": 0, "updated": 0, "adopted": 0, "unchanged": 0,
                                           "conflicts": 0, "missing": 0, "duplicates": 0, "skipped": 0},
                  "new_rows": [], "updates": [], "conflicts": [], "missing": [], "errors": []}
        try:
            if book is None:
                book = SheetBook(fetch_sheets())
            _sync_book(book, apply, report)
        except SyncError as e:
            report.update(ok=False, error=str(e))
        except Exception as e:                       # 예상 못 한 오류도 화면에서 보이게
            logger.exception("상담 시트 동기화 실패")
            report.update(ok=False, error=f"{type(e).__name__}: {e}")
        report["finished"] = datetime.now().isoformat(timespec="seconds")
        report["seconds"] = round((datetime.now() - started).total_seconds(), 1)
        text = render_report(report)
        _write_status(last={k: report[k] for k in ("ok", "apply", "trigger", "started", "finished", "seconds", "totals")
                            } | ({"error": report["error"]} if not report["ok"] else {}),
                      last_report_text=text[:30000])
        if apply and report["ok"] and (report["totals"]["new"] or report["totals"]["updated"]):
            try:
                models.log_audit(user_id=None, username=f"sheet-sync:{trigger}", action="sheet_sync",
                                 target_type="sheet", target_id=None,
                                 detail=f"추가 {report['totals']['new']} · 수정 {report['totals']['updated']} · "
                                        f"충돌 {report['totals']['conflicts']} · 시트에서 사라짐 {report['totals']['missing']}")
            except Exception:
                logger.exception("동기화 감사 기록 실패")
        return report
    finally:
        _run_lock.release()


def _sync_book(book, apply, report):
    ei = _ei()
    parsed_sheets = []
    for name in book.sheetnames:
        if ei.is_waiting_sheet(name):
            report["sheets"].append({"sheet": name, "skipped": "대기 명단 — 상담 내역이 아니라 건너뜀"})
            continue
        try:
            rep, rows = ei.parse_sheet(book, name)
        except SystemExit as e:                     # 스키마 감지 실패 = 상담 시트가 아닌 탭(메모·통계 등)
            report["sheets"].append({"sheet": name, "skipped": str(e)})
            continue
        except Exception as e:
            report["errors"].append(f"{name}: {type(e).__name__}: {e}")
            continue
        parsed_sheets.append((name, rep, rows))

    if apply and _needs_backup():
        try:
            ei.backup_db(label="sheet_sync")
            _write_status(backup_at=datetime.now().isoformat(timespec="seconds"))
        except Exception:
            logger.exception("동기화 전 백업 실패 — 반영은 계속한다")

    conn = models.get_db()
    try:
        init_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        touched = []          # (cid, pid, status) — 커밋 뒤 회차·생애주기 동기화
        for name, rep, rows in parsed_sheets:
            sheet_rep = _sync_sheet(conn, ei, name, rep, rows, apply, report, touched)
            report["sheets"].append(sheet_rep)
        if apply:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if apply:
        for cid, pid, st in touched:
            try:
                models.sync_admission_episode(cid)
                ei._advance_stage(pid, st)
            except Exception:
                logger.exception("회차·생애주기 동기화 실패 cid=%s", cid)


def _needs_backup() -> bool:
    last = status().get("backup_at")
    if not last:
        return True
    try:
        return (datetime.now() - datetime.fromisoformat(last)).total_seconds() > BACKUP_EVERY_HOURS * 3600
    except ValueError:
        return True


def _sync_sheet(conn, ei, name, rep, rows, apply, report, touched):
    """시트 하나: 행 ↔ 연결 맞추기 → 새 행 추가 / 고친 행 반영 / 사라진 행 표시."""
    sr = {"sheet": name, "schema": rep.get("schema"), "rows": len(rows), "new": 0, "updated": 0, "adopted": 0,
          "unchanged": 0, "conflicts": 0, "missing": 0, "duplicates": 0, "skipped": rep.get("rows_skipped", 0)}
    t = report["totals"]
    t["rows"] += len(rows)
    t["skipped"] += sr["skipped"]
    now = datetime.now().isoformat(timespec="seconds")

    # 이름 칸이 '0'처럼 숫자뿐인 행은 사람이 아니라 빈 행(수식·서식이 0으로 보이는 칸)이다 — 첫 미리보기에서
    # 50행이 '새 상담'으로 잡혔다(2026-09-28). 상담으로 만들지 않고 '읽지 못함'에 센다.
    junk = [(row_no, parsed) for row_no, parsed in rows if _junk_name(parsed.get("patient_name"))]
    if junk:
        rows = [(row_no, parsed) for row_no, parsed in rows if not _junk_name(parsed.get("patient_name"))]
        sr["rows"] = len(rows); sr["skipped"] += len(junk); t["rows"] -= len(junk); t["skipped"] += len(junk)
        sr["junk_names"] = len(junk)

    # 같은 이름·날짜가 한 시트에 두 번이면 키에 #2, #3을 붙여 구분한다(순서 기준).
    seen = {}
    current = []                                  # (row_no, key, parsed)
    for row_no, parsed in rows:
        base = _row_key(parsed)
        seen[base] = seen.get(base, 0) + 1
        key = base if seen[base] == 1 else f"{base}#{seen[base]}"
        current.append((row_no, key, parsed))

    links = [dict(r) for r in conn.execute("SELECT * FROM consult_sheet_links WHERE sheet_name=?", (name,))]
    by_key = {l["row_key"]: l for l in links}
    used, matched, unmatched = set(), [], []
    for row_no, key, parsed in current:
        link = by_key.get(key)
        if link and link["id"] not in used:
            used.add(link["id"]); matched.append((row_no, key, parsed, link))
        else:
            unmatched.append((row_no, key, parsed))
    # 키로 못 찾은 행 — 같은 자리(행번호)에 있던 연결이 남았으면 이름·날짜를 고친 행이다
    leftover_by_pos = {l["row_no"]: l for l in links if l["id"] not in used}
    still = []
    for row_no, key, parsed in unmatched:
        link = leftover_by_pos.pop(row_no, None)
        if link:
            used.add(link["id"]); matched.append((row_no, key, parsed, link))
        else:
            still.append((row_no, key, parsed))

    for row_no, key, parsed, link in matched:
        _apply_row(conn, ei, name, row_no, key, parsed, link, apply, report, sr, touched, now)

    for row_no, key, parsed in still:
        _new_or_adopt(conn, ei, name, row_no, key, parsed, apply, report, sr, touched, now)

    # 시트에서 사라진 행 — CRM은 지우지 않고 표시만
    for link in links:
        if link["id"] in used:
            continue
        sr["missing"] += 1; t["missing"] += 1
        if len(report["missing"]) < MAX_LIST:
            report["missing"].append({"sheet": name, "row": link["row_no"], "key": link["row_key"],
                                      "consultation_id": link["consultation_id"]})
        if apply and not link.get("missing_since"):
            conn.execute("UPDATE consult_sheet_links SET missing_since=? WHERE id=?", (now, link["id"]))
    return sr


def _apply_row(conn, ei, name, row_no, key, parsed, link, apply, report, sr, touched, now):
    t = report["totals"]
    snap = _snapshot(parsed)
    h = _row_hash(snap)
    baseline = json.loads(link["values_json"])
    if h == link["row_hash"]:
        sr["unchanged"] += 1; t["unchanged"] += 1
        if apply and (link["row_no"] != row_no or link["row_key"] != key or link.get("missing_since")):
            conn.execute("UPDATE consult_sheet_links SET row_no=?, row_key=?, missing_since=NULL WHERE id=?",
                         (row_no, key, link["id"]))
        return
    cid, pid = link["consultation_id"], link["patient_id"]
    con = conn.execute("SELECT * FROM consultations WHERE id=?", (cid,)).fetchone()
    if not con:                                   # CRM에서 상담을 지웠다 — 연결도 끊고 새 행 취급은 하지 않는다
        sr["missing"] += 1; t["missing"] += 1
        if apply:
            conn.execute("DELETE FROM consult_sheet_links WHERE id=?", (link["id"],))
        return
    pat = conn.execute("SELECT * FROM patients WHERE id=?", (con["patient_id"],)).fetchone()
    con, pat = dict(con), dict(pat) if pat else {}
    changed = [f for f in ALL_FIELDS if _norm(snap.get(f)) != _norm(baseline.get(f))]
    c_sets, c_vals, p_sets, p_vals, applied = [], [], [], [], []
    for f in changed:
        col = PATIENT_FIELDS.get(f, f)
        crm_val = pat.get(col) if f in PATIENT_FIELDS else con.get(f)
        if _norm(crm_val) != _norm(baseline.get(f)):
            # CRM에서도 그 사이 고쳤다 — CRM 값을 지키고 충돌로 보고한다
            sr["conflicts"] += 1; t["conflicts"] += 1
            if len(report["conflicts"]) < MAX_LIST:
                report["conflicts"].append({"sheet": name, "row": row_no, "patient": parsed.get("patient_name"),
                                            "consultation_id": cid, "field": FIELD_LABELS.get(f, f),
                                            "sheet_value": _show(snap.get(f)), "crm_value": _show(crm_val)})
            continue
        if f in PATIENT_FIELDS:
            p_sets.append(f"{col}=?"); p_vals.append(_db_value(f, snap.get(f)))
        else:
            c_sets.append(f"{f}=?"); c_vals.append(_db_value(f, snap.get(f)))
        applied.append(f)
    if applied:
        sr["updated"] += 1; t["updated"] += 1
        if len(report["updates"]) < MAX_LIST:
            report["updates"].append({"sheet": name, "row": row_no, "patient": parsed.get("patient_name"),
                                      "consultation_id": cid, "fields": [FIELD_LABELS.get(f, f) for f in applied]})
        if apply:
            if c_sets:
                conn.execute(f"UPDATE consultations SET {', '.join(c_sets)}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                             c_vals + [cid])
            if p_sets and pat:
                conn.execute(f"UPDATE patients SET {', '.join(p_sets)}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                             p_vals + [pat["id"]])
            if "admission_status" in applied and snap.get("admission_status") in ("입원대기", "입원예정", "입원완료"):
                touched.append((cid, con["patient_id"], snap["admission_status"]))
    elif not changed:
        sr["unchanged"] += 1; t["unchanged"] += 1
    if apply:
        # 충돌한 칸도 기준선은 새 시트 값으로 — 같은 충돌을 10분마다 다시 보고하지 않게(한 번은 위 목록·감사로 남는다)
        conn.execute("""UPDATE consult_sheet_links SET row_no=?, row_key=?, values_json=?, row_hash=?, synced_at=?,
                        missing_since=NULL WHERE id=?""",
                     (row_no, key, json.dumps(snap, ensure_ascii=False, default=str), h, now, link["id"]))


def _new_or_adopt(conn, ei, name, row_no, key, parsed, apply, report, sr, touched, now):
    """연결 없는 행 — CRM에 이미 같은 환자·같은 날짜 상담이 있으면 연결만(기준선), 없으면 새로 만든다."""
    t = report["totals"]
    snap = _snapshot(parsed)
    h = _row_hash(snap)
    pid = _find_patient(conn, parsed)
    cid = ei._consultation_exists(conn, pid, parsed) if pid else None
    if cid:
        if conn.execute("SELECT 1 FROM consult_sheet_links WHERE consultation_id=?", (cid,)).fetchone():
            # 다른 행(또는 다른 시트)이 이미 이 상담을 잡고 있다 — 시트 안 중복 행
            sr["duplicates"] += 1; t["duplicates"] += 1
            return
        sr["adopted"] += 1; t["adopted"] += 1
        if apply:
            _insert_link(conn, name, row_no, key, cid, pid, snap, h, now)
        return
    sr["new"] += 1; t["new"] += 1
    if len(report["new_rows"]) < MAX_LIST:
        report["new_rows"].append({"sheet": name, "row": row_no, "patient": parsed.get("patient_name"),
                                   "date": parsed.get("consult_date"), "status": parsed.get("admission_status")})
    if not apply:
        return
    dummy = {"patients_new": 0, "patients_matched": 0}
    pid = ei._upsert_patient(conn, parsed, dummy)
    cid = ei._insert_consultation(conn, pid, parsed)
    conn.execute("UPDATE consultations SET import_source='sheet' WHERE id=?", (cid,))
    _insert_link(conn, name, row_no, key, cid, pid, snap, h, now)
    if parsed.get("admission_status") in ("입원대기", "입원예정", "입원완료"):
        touched.append((cid, pid, parsed["admission_status"]))


def _junk_name(name) -> bool:
    """환자이름이 이름일 수 없는 값('0', '-', 한 글자)이면 True."""
    s = (name or "").strip()
    return not s or s.isdigit() or len(s) < 2 or s in ("-", "–", "—", ".")


def _find_patient(conn, parsed):
    """엑셀 적재의 환자 찾기와 같은 규칙(이름+보호자 연락처 → 이름만) — 단, 만들지는 않는다."""
    name, phone = parsed.get("patient_name"), parsed.get("guardian_phone")
    row = None
    if phone:
        row = conn.execute("SELECT id FROM patients WHERE name=? AND guardian_phone=? LIMIT 1", (name, phone)).fetchone()
    if not row:
        row = conn.execute("SELECT id FROM patients WHERE name=? AND (guardian_phone IS NULL OR guardian_phone='') LIMIT 1",
                           (name,)).fetchone()
    return row["id"] if row else None


def _insert_link(conn, name, row_no, key, cid, pid, snap, h, now):
    conn.execute("""INSERT INTO consult_sheet_links (sheet_name, row_no, row_key, consultation_id, patient_id,
                    values_json, row_hash, synced_at) VALUES (?,?,?,?,?,?,?,?)""",
                 (name, row_no, key, cid, pid, json.dumps(snap, ensure_ascii=False, default=str), h, now))


def _show(v):
    if v is None or v == "":
        return "-"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v)


# ── 보고서 ─────────────────────────────────────────────────────────────

def render_report(report: dict) -> str:
    t = report.get("totals") or {}
    head = "반영" if report.get("apply") else "미리보기(DB 변경 없음)"
    lines = [f"[{head}] {report.get('started', '')} · {report.get('seconds', '?')}초 · 실행: {report.get('trigger', '')}"]
    if not report.get("ok"):
        lines.append(f"실패: {report.get('error')}")
    lines.append(f"행 {t.get('rows', 0)} — 새 상담 {t.get('new', 0)} · 고친 행 반영 {t.get('updated', 0)} · "
                 f"기존 상담과 연결(기준선) {t.get('adopted', 0)} · 변화 없음 {t.get('unchanged', 0)} · "
                 f"충돌(CRM 값 유지) {t.get('conflicts', 0)} · 시트에서 사라짐 {t.get('missing', 0)} · "
                 f"시트 안 중복 {t.get('duplicates', 0)} · 읽지 못함 {t.get('skipped', 0)}")
    for s in report.get("sheets") or []:
        if s.get("skipped") and "rows" not in s:
            lines.append(f"  · {s['sheet']}: 건너뜀 — {s['skipped']}")
        else:
            lines.append(f"  · {s['sheet']} ({s.get('schema')}): 행 {s['rows']} / 새 {s['new']} · 수정 {s['updated']} · "
                         f"연결 {s['adopted']} · 그대로 {s['unchanged']} · 충돌 {s['conflicts']} · 사라짐 {s['missing']}"
                         f"{' · 중복 ' + str(s['duplicates']) if s['duplicates'] else ''}"
                         f"{' · 읽지 못함 ' + str(s['skipped']) if s['skipped'] else ''}"
                         f"{' (이름이 숫자인 빈 행 ' + str(s['junk_names']) + ')' if s.get('junk_names') else ''}")
    if report.get("new_rows"):
        lines.append("새 상담:")
        lines += [f"  + {r['sheet']} {r['row']}행 {r['patient']} {r['date']} {r.get('status') or ''}" for r in report["new_rows"]]
    if report.get("updates"):
        lines.append("고친 행 반영:")
        lines += [f"  ~ {r['sheet']} {r['row']}행 {r['patient']} (#{r['consultation_id']}): {', '.join(r['fields'])}"
                  for r in report["updates"]]
    if report.get("conflicts"):
        lines.append("충돌 — CRM에서도 고친 칸이라 CRM 값을 지켰습니다(시트 값 → CRM 값):")
        lines += [f"  ! {r['sheet']} {r['row']}행 {r['patient']} (#{r['consultation_id']}) {r['field']}: "
                  f"{r['sheet_value']} → {r['crm_value']}" for r in report["conflicts"]]
    if report.get("missing"):
        lines.append("시트에서 사라진 행(CRM 상담은 그대로):")
        lines += [f"  - {r['sheet']} {r['row']}행 {r['key']} (#{r['consultation_id']})" for r in report["missing"]]
    if report.get("errors"):
        lines.append("오류:")
        lines += [f"  x {e}" for e in report["errors"]]
    return "\n".join(lines)


# ── 스케줄러 ───────────────────────────────────────────────────────────

def _loop():
    while True:
        threading.Event().wait(sync_minutes() * 60)
        if not configured() or not auto_enabled():
            continue
        try:
            rep = run(apply=True, trigger="scheduler")
            t = rep.get("totals") or {}
            logger.info("상담 시트 동기화: ok=%s 새 %s 수정 %s 충돌 %s", rep.get("ok"), t.get("new"), t.get("updated"),
                        t.get("conflicts"))
        except Exception:
            logger.exception("상담 시트 동기화 루프 오류")


def start_scheduler():
    if not configured():
        logger.info("상담 시트 연동 꺼짐 (CONSULT_SHEET_URL/TOKEN 없음)")
        return
    threading.Thread(target=_loop, name="consult-sheet-sync", daemon=True).start()
    logger.info("상담 시트 동기화 준비 — %s분마다(자동 반영은 관리 화면에서 켠다: %s)", sync_minutes(),
                "켜짐" if auto_enabled() else "꺼짐")
