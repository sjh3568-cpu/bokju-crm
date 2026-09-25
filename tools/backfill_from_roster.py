"""원무 명부 회차(admission_episodes) → 환자·상담 행 백필 — bokju-crm.

명부 적재(import_admission_roster.py)는 입원 사실을 `admission_episodes`에만
쓴다. 그런데 상담목록의 '보험' 칸은 `patients.insurance_type`, '입원완료일'은
`consultations.actual_admission_date`를 읽는다. 상담내역 스프레드시트에는 보험
칸이 아예 없고 입원일도 옛 시트엔 없어서, 명부를 다 넣고도 두 칸이 비어 보였다.
이 도구가 회차에서 두 칸으로 옮겨 적는다.

  1) 보험유형 — 환자별 **가장 최근 입원 회차**의 환자유형을 patients.insurance_type에.
     기본은 빈 칸만 채운다(화면에서 손으로 고친 값 보호). --overwrite-insurance 로 덮어쓰기.
  2) 입원완료일 — admission_status='입원완료'인데 날짜가 없는 상담에, 그 환자의
     명부 입원일 중 **상담일 이후 가장 가까운 것**(없으면 상담일 14일 이내 직전 것)을
     actual_admission_date로. 상담일로부터 180일 넘게 뒤의 입원은 딴 회차로 보고 안 붙인다.
     상담에 딸린 회차(consultation_id 있는 것)의 admitted_at도 같이 맞춘다.

  3) 미정 → 입원완료 승격 (--promote-undecided, 사용자 결정 2026-09-11) — 상담 상태가
     비어 있는데(미정) 원무 명부에 **상담 후 30일 이내 입원**이 있으면 원무 기록을
     믿고 admission_status='입원완료' + 입원일을 기록한다. 상담 시트에서 입원 여부를
     안 고친 341건이 이 경우였다. 30일 넘는 건 다른 목적의 재입원일 수 있어 안 건드린다.

UPDATE 전용 — 환자도 상담도 회차도 새로 만들지 않는다. 멱등이라 몇 번 돌려도 같다.
import_admission_roster.py --apply 끝에 자동으로 한 번 돈다. 단독 실행:

    python tools/backfill_from_roster.py            # dry-run
    python tools/backfill_from_roster.py --apply    # 반영 (직전 자동 백업)
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import models  # noqa: E402
from tools.excel_import import backup_db  # noqa: E402
from tools.import_admission_roster import to_date  # noqa: E402

BEFORE_DAYS = 14    # 상담일보다 이만큼 앞선 입원까지는 같은 회차로 본다(입원 중 재상담)
AFTER_DAYS = 180    # 상담일로부터 이보다 늦은 입원은 다른 회차
PROMOTE_DAYS = 30   # 미정 상담을 입원완료로 승격할 때는 이 안에 입원한 것만


def backfill_insurance(conn, *, overwrite=False, apply=False) -> dict:
    """환자별 최신 명부 회차의 보험유형 → patients.insurance_type."""
    rows = conn.execute("""
        SELECT e.patient_id, e.insurance_type, p.insurance_type AS current
          FROM admission_episodes e JOIN patients p ON p.id = e.patient_id
         WHERE e.roster_key IS NOT NULL AND e.insurance_type IS NOT NULL AND e.insurance_type <> ''
         ORDER BY e.patient_id, e.admitted_at DESC, e.id DESC
    """).fetchall()
    latest, current = {}, {}
    for pid, ins, cur in rows:
        latest.setdefault(pid, ins.strip())   # 첫 행 = 가장 최근 입원
        current[pid] = (cur or "").strip()
    stats = Counter()
    values = Counter()
    for pid, ins in latest.items():
        if current[pid] == ins:
            stats["이미 같음"] += 1
        elif current[pid] and not overwrite:
            stats["값 있어 보존"] += 1
        else:
            stats["덮어씀" if current[pid] else "채움"] += 1
            values[ins] += 1
            if apply:
                conn.execute("UPDATE patients SET insurance_type = ? WHERE id = ?", (ins, pid))
    return {"stats": stats, "values": values, "patients": len(latest)}


def pick_admission(consult_date: date, candidates: list[date]) -> date | None:
    """상담일 이후 가장 가까운 입원일, 없으면 14일 이내 직전 입원일."""
    after = sorted(d for d in candidates if consult_date <= d <= consult_date + timedelta(days=AFTER_DAYS))
    if after:
        return after[0]
    before = sorted(d for d in candidates if consult_date - timedelta(days=BEFORE_DAYS) <= d < consult_date)
    return before[-1] if before else None


def backfill_admission_dates(conn, *, apply=False) -> dict:
    """입원완료인데 입원일 없는 상담 → 명부 입원일."""
    roster = defaultdict(set)
    for pid, adm in conn.execute(
            "SELECT patient_id, admitted_at FROM admission_episodes "
            "WHERE roster_key IS NOT NULL AND admitted_at IS NOT NULL"):
        d = to_date(adm)
        if d:
            roster[pid].add(d)
    targets = conn.execute("""
        SELECT id, patient_id, consult_date FROM consultations
         WHERE admission_status = '입원완료'
           AND COALESCE(actual_admission_date, admission_date) IS NULL
         ORDER BY consult_date, id
    """).fetchall()
    stats = Counter()
    samples = defaultdict(list)
    for cid, pid, cd in targets:
        consult_date = to_date(cd)
        if consult_date is None:
            stats["상담일 없음"] += 1
            continue
        if pid not in roster:
            stats["명부 회차 없음"] += 1
            continue
        chosen = pick_admission(consult_date, roster[pid])
        if chosen is None:
            stats["기간 내 입원 없음(−%d~+%d일)" % (BEFORE_DAYS, AFTER_DAYS)] += 1
            if len(samples["기간 밖"]) < 5:
                samples["기간 밖"].append((cid, cd, sorted(roster[pid])[-3:]))
            continue
        kind = "채움" if chosen >= consult_date else "채움(입원 중 상담)"
        stats[kind] += 1
        if apply:
            iso = chosen.isoformat()
            conn.execute("UPDATE consultations SET actual_admission_date = ? WHERE id = ?", (iso, cid))
            # 상담에 딸린 회차도 같은 날짜로 — 앱 재기동 시 이관이 다시 맞추지만 지금 맞춘다.
            conn.execute("""UPDATE admission_episodes
                               SET admitted_at = ?, status = CASE WHEN discharged_at IS NULL THEN 'admitted' ELSE status END,
                                   updated_at = CURRENT_TIMESTAMP
                             WHERE consultation_id = ? AND admitted_at IS NULL""", (iso, cid))
    return {"stats": stats, "samples": samples, "targets": len(targets)}


def promote_undecided(conn, *, apply=False) -> dict:
    """상태 미정 상담 + 상담 후 30일 내 명부 입원 → 입원완료로 승격."""
    roster = defaultdict(set)
    for pid, adm in conn.execute(
            "SELECT patient_id, admitted_at FROM admission_episodes "
            "WHERE roster_key IS NOT NULL AND admitted_at IS NOT NULL"):
        d = to_date(adm)
        if d:
            roster[pid].add(d)
    targets = conn.execute("""
        SELECT id, patient_id, consult_date FROM consultations
         WHERE (admission_status IS NULL OR admission_status = '')
           AND consult_date IS NOT NULL
         ORDER BY consult_date, id
    """).fetchall()
    stats = Counter()
    for cid, pid, cd in targets:
        consult_date = to_date(cd)
        if consult_date is None or pid not in roster:
            continue
        after = sorted(d for d in roster[pid]
                       if consult_date <= d <= consult_date + timedelta(days=PROMOTE_DAYS))
        if not after:
            if any(d > consult_date for d in roster[pid]):
                stats["%d일 넘어 입원 — 보류" % PROMOTE_DAYS] += 1
            continue
        stats["입원완료로 승격"] += 1
        if apply:
            iso = after[0].isoformat()
            conn.execute("UPDATE consultations SET admission_status = '입원완료', "
                         "actual_admission_date = ? WHERE id = ?", (iso, cid))
            conn.execute("""UPDATE admission_episodes
                               SET admitted_at = ?, status = CASE WHEN discharged_at IS NULL THEN 'admitted' ELSE status END,
                                   updated_at = CURRENT_TIMESTAMP
                             WHERE consultation_id = ? AND admitted_at IS NULL""", (iso, cid))
    return {"stats": stats, "targets": len(targets)}


MATCH_AFTER_DAYS = 30   # 상담 입원일(대개 예정일)보다 며칠 뒤까지의 명부 입원을 '같은 입원'으로 볼지


def _pick_roster_discharge(cands, adm):
    """이 상담 입원의 명부 퇴원일을 고른다. cands=[(명부 입원일, 퇴원일)], adm=상담 입원일(없을 수 있음).

    같은 날 입원한 회차 → 없으면 입원일이 가장 가까운 회차(뒤쪽은 MATCH_AFTER_DAYS 이내만 —
    상담에 적힌 날은 예정일이라 실제 입원이 며칠 뒤인 게 흔하다, 2026-09-25에 12건).
    퇴원일이 상담 입원일보다 앞선 회차는 이 입원의 짝이 아니다(옛 입원의 퇴원을 갖다 붙이지 않게).
    """
    if not cands:
        return None
    if not adm:
        return max(cands, key=lambda t: t[0])[1]          # 입원일을 모르면 가장 최근 퇴원
    same = [d for a, d in cands if a == adm]
    if same:
        return same[0]
    near = [(abs((a - adm).days), a, d) for a, d in cands
            if d >= adm and (a <= adm or (a - adm).days <= MATCH_AFTER_DAYS)]
    return min(near)[2] if near else None


def close_discharged(conn, *, apply=False, snapshot=None) -> dict:
    """명부에 퇴원일이 있는 입원완료 상담을 '퇴원완료'로 바꾸고 퇴원일을 적는다.

    사용자 규칙(2026-09-19): "현재 재원환자 현황은 절대 변경하면 안 되고, 그 외 입원 환자는 다
    퇴원완료로 본다." 그래서
      ① 열린 명부 회차가 하나라도 있는 환자(= 지금 재원)는 통째로 건너뛴다. 옛 상담도 안 건드린다.
      ② 퇴원일을 아는 건만 바꾼다 — 명부에 없어 퇴원일을 모르는 상담은 그대로 둔다(빈 퇴원일 방지).
      ③ 상담에 이미 퇴원일이 적혀 있으면 손대지 않는다(화면에서 넣은 값 보호).
    상담과 회차는 입원일로 맞춘다(_pick_roster_discharge). UPDATE 전용·멱등. 회차의 consultation_id는 건드리지 않는다.

    snapshot(날짜, 2026-09-25 사용자 결정 "최근 명부에 없는 사람은 퇴원자"): 그 날짜의 명부는
    그날 재원 전원을 담은 완전 스냅샷이다. 그 날짜 **이전에 입원**했는데 지금 명부에 없는 상담은
    퇴원일을 몰라도 퇴원완료로 본다 — 퇴원일은 비워 두고 사유에 '명부 미등재'를 적는다(엉뚱한
    날짜를 넣으면 입·퇴원 이력에 가짜 퇴원이 생긴다). 스냅샷 **이후** 입원은 명부가 아직 모르는
    CRM 재원이므로 그대로 둔다. ①·③은 그대로다.
    """
    stats = Counter()
    resident = {r["patient_id"] for r in conn.execute(
        "SELECT DISTINCT patient_id FROM admission_episodes "
        "WHERE roster_key IS NOT NULL AND discharged_at IS NULL AND COALESCE(admitted_at,'') <> ''")}
    closed = defaultdict(list)      # patient_id -> [(admitted_at, discharged_at)]
    for r in conn.execute(
            "SELECT patient_id, admitted_at, discharged_at FROM admission_episodes "
            "WHERE roster_key IS NOT NULL AND COALESCE(discharged_at,'') <> '' "
            "AND COALESCE(admitted_at,'') <> ''"):
        a, d = to_date(r["admitted_at"]), to_date(r["discharged_at"])
        if a and d:
            closed[r["patient_id"]].append((a, d))
    rows = conn.execute(
        "SELECT id, patient_id, consult_date, actual_admission_date, admission_date, discharge_date "
        "FROM consultations WHERE admission_status = '입원완료'").fetchall()
    stats["대상 입원완료 상담"] = len(rows)
    snap = to_date(snapshot) if snapshot else None
    updates, absent = [], []
    for c in rows:
        if (c["discharge_date"] or "").strip():
            stats["이미 퇴원일 있음 — 건너뜀"] += 1
            continue
        if c["patient_id"] in resident:
            stats["현재 재원 — 건드리지 않음"] += 1
            continue
        adm = to_date(c["actual_admission_date"] or c["admission_date"] or "")
        pick = _pick_roster_discharge(closed.get(c["patient_id"]) or [], adm)
        if pick is not None:
            updates.append((c["id"], pick.isoformat()))
            stats["퇴원완료로 전환"] += 1
            continue
        had_roster = bool(closed.get(c["patient_id"]))
        if snap is None:
            stats["입원일이 명부 회차와 안 맞음 — 보류" if had_roster else "명부에 퇴원 기록 없음 — 그대로 둠"] += 1
            continue
        # 스냅샷 모드 — 입원일을 모르는 상담은 상담일로 판단한다(입원완료인데 날짜 없는 옛 상담).
        judged = adm or to_date(c["consult_date"] or "")
        if judged and judged > snap:
            stats["스냅샷 이후 입원 — 그대로 둠"] += 1
            continue
        absent.append(c["id"])
        stats["명부 미등재 — 퇴원완료(퇴원일 미상)로 전환"] += 1
    if apply and updates:
        conn.executemany(
            "UPDATE consultations SET admission_status='퇴원완료', discharge_date=?, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            [(d, cid) for cid, d in updates])
    if apply and absent:
        reason = f"명부 미등재 정리({snap.isoformat()} 스냅샷에 없음 — 퇴원일 미상)"
        conn.executemany(
            "UPDATE consultations SET admission_status='퇴원완료', "
            "discharge_reason=COALESCE(NULLIF(discharge_reason,''), ?), "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            [(reason, cid) for cid in absent])
    return {"stats": stats, "updates": updates, "absent": absent, "residents": len(resident)}


def close_roster_by_consultation(conn, *, apply=False) -> dict:
    """상담이 퇴원완료(퇴원일 있음)인데 열린 채 남은 명부 회차를 그 날짜로 닫는다(멱등).

    명부는 '그날 재원자' 스냅샷으로도 올라와 이미 퇴원한 사람의 행이 없고, 행이 없다고 적재기가
    회차를 닫지는 않는다. 그래서 9/17~18 퇴원자 6명의 명부 회차가 열린 채 남았다(2026-09-25).
    재원 판정(crm_discharge_sql)은 이미 상담 퇴원일을 보고 빼지만, 회차 자체도 사실과 맞춘다.
    상담 퇴원일이 회차 입원일보다 앞선 건(옛 입원의 퇴원)은 짝이 아니라 건드리지 않는다.
    미복귀 외진은 여기서 닫지 않는다 — 복귀하면 그 자리에서 다시 재원이어야 하기 때문.
    """
    rows = conn.execute(
        """SELECT e.id, e.patient_id, e.admitted_at,
                  (SELECT MIN(date(c.discharge_date)) FROM consultations c
                    WHERE c.patient_id = e.patient_id AND c.admission_status = '퇴원완료'
                      AND COALESCE(c.discharge_date,'') <> ''
                      AND date(c.discharge_date) >= date(e.admitted_at)) AS dis
             FROM admission_episodes e
            WHERE e.roster_key IS NOT NULL AND (e.discharged_at IS NULL OR e.discharged_at = '')
              AND COALESCE(e.admitted_at,'') <> ''""").fetchall()
    targets = [(r["dis"], r["id"]) for r in rows if r["dis"]]
    if apply and targets:
        conn.executemany(
            "UPDATE admission_episodes SET discharged_at=?, status='discharged', "
            "discharge_reason=COALESCE(NULLIF(discharge_reason,''), '상담 퇴원완료 반영'), "
            "updated_at=CURRENT_TIMESTAMP WHERE id=? AND roster_key IS NOT NULL "
            "AND (discharged_at IS NULL OR discharged_at = '')",
            targets)
    return {"closed": len(targets), "episodes": [eid for _, eid in targets]}


def run(conn, *, apply=False, overwrite_insurance=False, promote=False, quiet=False, close=False,
        out=None, snapshot=None):
    """회차에 들어간 값을 환자·상담 행으로 옮겨 적는다.

    out: 진행 문구를 받는 콜백(기본은 stdout). 관리 화면(/admin/import)은 리스트에 모아
    리포트로 보여주고, 명부 적재(import_admission_roster)는 자기 out으로 그대로 넘긴다.
    기본값을 그냥 print로 두지 않는 것은, Windows 콘솔(cp949)에서 '—' 같은 글자가
    UnicodeEncodeError로 터지기 때문이다 — 서버는 UTF-8이라 컨테이너 밖에서만 터진다.
    """
    def emit(line=""):
        if out is not None:
            out(line)
            return
        try:
            print(line)
        except UnicodeEncodeError:      # cp949 콘솔 — 못 쓰는 글자는 버리고 계속한다
            enc = sys.stdout.encoding or "utf-8"
            print(line.encode(enc, "replace").decode(enc, "replace"))

    ins = backfill_insurance(conn, overwrite=overwrite_insurance, apply=apply)
    adm = backfill_admission_dates(conn, apply=apply)
    pro = promote_undecided(conn, apply=apply) if promote else None
    rcl = close_roster_by_consultation(conn, apply=apply) if close else None
    clo = close_discharged(conn, apply=apply, snapshot=snapshot) if close else None
    if clo is not None:
        clo["roster_closed"] = rcl["closed"]
    if apply:
        conn.commit()
    if quiet:
        return ins, adm, pro, clo
    emit("보험유형 — 명부에 보험이 있는 환자 %d명" % ins["patients"])
    for k, n in ins["stats"].most_common():
        emit("  %-16s %5d명" % (k, n))
    if ins["values"]:
        emit("  " + ", ".join("%s %d" % kv for kv in ins["values"].most_common()))
    emit()
    emit("입원완료일 — 입원완료인데 날짜 없는 상담 %d건" % adm["targets"])
    for k, n in adm["stats"].most_common():
        emit("  %-28s %5d건" % (k, n))
    for cid, cd, dates in adm["samples"].get("기간 밖", []):
        emit("    예) 상담 #%d %s — 명부 입원일 %s" % (cid, cd, ", ".join(d.isoformat() for d in dates)))
    if pro is not None:
        emit()
        emit("미정 → 입원완료 승격 — 상태 없는 상담 %d건 중" % pro["targets"])
        for k, n in pro["stats"].most_common():
            emit("  %-28s %5d건" % (k, n))
    if clo is not None:
        emit()
        emit("상담 퇴원완료인데 열린 명부 회차 — %d건 닫음" % clo["roster_closed"])
        emit("퇴원완료 전환 — 현재 재원 %d명은 제외%s"
             % (clo["residents"], f" · 스냅샷 {snapshot} 이전 입원 중 명부 미등재는 퇴원일 미상으로" if snapshot else ""))
        for k, n in clo["stats"].most_common():
            emit("  %-28s %5d건" % (k, n))
    if not apply:
        emit()
        emit("  ** dry-run이라 DB는 건드리지 않았다. 반영하려면 --apply **")
    return ins, adm, pro, clo


def main():
    ap = argparse.ArgumentParser(description="명부 회차 → 환자 보험유형·상담 입원완료일 백필")
    ap.add_argument("--apply", action="store_true", help="실제로 DB에 쓴다 (없으면 dry-run)")
    ap.add_argument("--overwrite-insurance", action="store_true",
                    help="이미 값이 있는 보험유형도 최신 명부 값으로 덮는다")
    ap.add_argument("--close-discharged", action="store_true",
                    help="명부에 퇴원일이 있는 입원완료 상담을 퇴원완료로 (현재 재원 제외)")
    ap.add_argument("--promote-undecided", action="store_true",
                    help="상태 미정 상담에 30일 내 명부 입원이 있으면 입원완료로 바꾼다")
    ap.add_argument("--snapshot", metavar="YYYY-MM-DD",
                    help="--close-discharged와 함께: 이 날짜의 완전 명부에 없는 사람(그 이전 입원)은 "
                         "퇴원일 미상이어도 퇴원완료로 (2026-09-25 규칙)")
    args = ap.parse_args()
    if args.snapshot and not args.close_discharged:
        ap.error("--snapshot 은 --close-discharged 와 함께 써야 합니다")
    if args.apply:
        backup_db("backfill_from_roster")
    conn = models.get_db()
    run(conn, apply=args.apply, overwrite_insurance=args.overwrite_insurance,
        promote=args.promote_undecided, close=args.close_discharged, snapshot=args.snapshot)


if __name__ == "__main__":
    main()
