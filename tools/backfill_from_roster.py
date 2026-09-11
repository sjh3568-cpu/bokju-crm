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


def run(conn, *, apply=False, overwrite_insurance=False, quiet=False):
    ins = backfill_insurance(conn, overwrite=overwrite_insurance, apply=apply)
    adm = backfill_admission_dates(conn, apply=apply)
    if apply:
        conn.commit()
    if quiet:
        return ins, adm
    print("보험유형 — 명부에 보험이 있는 환자 %d명" % ins["patients"])
    for k, n in ins["stats"].most_common():
        print("  %-16s %5d명" % (k, n))
    if ins["values"]:
        print("  " + ", ".join("%s %d" % kv for kv in ins["values"].most_common()))
    print()
    print("입원완료일 — 입원완료인데 날짜 없는 상담 %d건" % adm["targets"])
    for k, n in adm["stats"].most_common():
        print("  %-28s %5d건" % (k, n))
    for cid, cd, dates in adm["samples"].get("기간 밖", []):
        print("    예) 상담 #%d %s — 명부 입원일 %s" % (cid, cd, ", ".join(d.isoformat() for d in dates)))
    if not apply:
        print()
        print("  ** dry-run이라 DB는 건드리지 않았다. 반영하려면 --apply **")
    return ins, adm


def main():
    ap = argparse.ArgumentParser(description="명부 회차 → 환자 보험유형·상담 입원완료일 백필")
    ap.add_argument("--apply", action="store_true", help="실제로 DB에 쓴다 (없으면 dry-run)")
    ap.add_argument("--overwrite-insurance", action="store_true",
                    help="이미 값이 있는 보험유형도 최신 명부 값으로 덮는다")
    args = ap.parse_args()
    if args.apply:
        backup_db("backfill_from_roster")
    conn = models.get_db()
    run(conn, apply=args.apply, overwrite_insurance=args.overwrite_insurance)


if __name__ == "__main__":
    main()
