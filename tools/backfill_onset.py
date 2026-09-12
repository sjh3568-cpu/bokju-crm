"""원무 명부(발병일 포함) xlsx의 L열 발병일을 입원 회차에 적재한다.

메인 명부 import(import_admission_roster.py)는 발병일 열을 안 읽는다. 이 스크립트는
같은 회차(roster_key = 차트번호|입원일)를 찾아 onset_date 컬럼만 채운다 — care_type·
재활종료일 등 다른 값은 건드리지 않는다(멱등, 재실행 안전).

  python tools/backfill_onset.py <xlsx>            dry-run (안 씀)
  python tools/backfill_onset.py <xlsx> --apply    실제 반영 (백업 후)
"""
import argparse
import os
import sys
from datetime import datetime

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import models  # noqa: E402
from tools.excel_import import backup_db  # noqa: E402

# 헤더 이름 → 내부 이름. 순서가 바뀌어도 이름으로 찾는다.
COLUMNS = {"차트번호": "chart_no", "입원일": "admitted_at", "발병일": "onset_date"}


def to_date(v):
    """'2026-05-25' / '2026/05/25' / datetime → 'YYYY-MM-DD' 또는 None."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    s = str(v).strip()[:10].replace("/", "-").replace(".", "-")
    try:
        return datetime.strptime(s, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def read_rows(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = ws.iter_rows(values_only=True)
    header = next(rows)
    idx = {}
    for i, v in enumerate(header):
        key = COLUMNS.get(str(v).strip() if v is not None else "")
        if key:
            idx[key] = i
    for need in ("chart_no", "admitted_at", "onset_date"):
        if need not in idx:
            sys.exit(f"헤더에서 '{need}' 열을 찾지 못했습니다. 발병일 포함 명부인지 확인하세요.")
    out = []
    for r in rows:
        chart = r[idx["chart_no"]]
        adm = to_date(r[idx["admitted_at"]])
        onset = to_date(r[idx["onset_date"]])
        if chart is None or not adm:
            continue
        out.append({"chart_no": str(chart).strip(),
                    "admitted_at": adm, "onset_date": onset})
    return out


def main():
    ap = argparse.ArgumentParser(description="명부 L열 발병일 → 입원 회차 onset_date 적재")
    ap.add_argument("path", help="발병일 포함 입퇴재원환자현황 xlsx 경로")
    ap.add_argument("--apply", action="store_true", help="실제로 DB에 쓴다 (없으면 dry-run)")
    args = ap.parse_args()

    rows = read_rows(args.path)
    print("명부 행: %d (발병일 있는 행: %d)"
          % (len(rows), sum(1 for r in rows if r["onset_date"])))

    if args.apply:
        backup_db("onset_backfill")

    conn = models.get_db()
    matched = missing = filled = 0
    try:
        for r in rows:
            key = "%s|%s" % (r["chart_no"], r["admitted_at"])
            ep = conn.execute(
                "SELECT id FROM admission_episodes WHERE roster_key = ?", (key,)).fetchone()
            if not ep:
                missing += 1
                continue
            matched += 1
            if r["onset_date"]:
                filled += 1
                if args.apply:
                    conn.execute(
                        "UPDATE admission_episodes SET onset_date = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (r["onset_date"], ep[0]))
        if args.apply:
            conn.commit()
    finally:
        conn.close()

    print("회차 매칭 %d · 발병일 채움 %d · 명부에 없는 회차 %d" % (matched, filled, missing))
    print("실제 반영 완료." if args.apply else "dry-run 이었습니다. --apply 로 실제 반영하세요.")


if __name__ == "__main__":
    main()
