"""레거시 데이터 정정 — bokju-crm.

엑셀 마이그레이션 시 발생한 셀 오류 2종을 정정한다.

1) 상담일 연도 오타 3건 — '2006-MM-DD' → '2026-MM-DD'.
   주변 레코드(같은 import 배치)가 전부 2026년이고 엑셀 적재 범위가
   25.5~26.5 이므로 '2006'은 '2026'의 명백한 오타. id별 검증 완료.
2) 거처(현 위치) 칸에 병원명이 들어간 2건 — current_location_type 에
   '입원중/집/입소중/기타' 대신 병원명이 기재됨. 올바른 형태
   (type='입원중', name=병원명)로 교정. source_hospital 은 이미
   올바르므로 그대로 둔다.

사용법:
    python tools/fix_legacy_data.py          → dry-run (DB 변경 없음)
    python tools/fix_legacy_data.py --apply  → 실제 적용 + 직전 자동 백업

멱등 — 이미 정정됐으면 다시 실행해도 변경 없음.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import models  # noqa: E402

# (consultation id, 잘못된 상담일, 정정 상담일)
DATE_FIXES: list[tuple[int, str, str]] = [
    (1125, "2006-02-04", "2026-02-04"),  # 고광진 — 주변 id 1124·1126 모두 2026-02
    (1128, "2006-02-05", "2026-02-05"),  # 이필형 — 주변 id 1127·1129 모두 2026-02
    (1358, "2006-04-03", "2026-04-03"),  # 김을현 — 주변 id 1357·1359 모두 2026-04
]

# (consultation id, 병원명) — current_location_type 에 병원명이 들어간 케이스
LOCATION_FIXES: list[tuple[int, str]] = [
    (921, "포레메디한방병원"),  # 조영옥
    (874, "강릉아산병원"),      # 심순애
]


def backup_db() -> None:
    src = ROOT / "bokju.db"
    dst = ROOT / "backups" / f"pre_legacy_fix_{datetime.now():%Y%m%d_%H%M%S}.db"
    dst.parent.mkdir(exist_ok=True)
    shutil.copy2(src, dst)
    print(f"[backup] {dst}")


def main() -> None:
    ap = argparse.ArgumentParser(description="레거시 데이터 셀 오류 정정")
    ap.add_argument("--apply", action="store_true",
                    help="실제 DB 적용 (기본: dry-run)")
    args = ap.parse_args()

    conn = models.get_db()
    date_plan: list[tuple[int, str, str]] = []
    loc_plan: list[tuple[int, str, str]] = []

    print("=== 1) 상담일 연도 오타 ===")
    for cid, old, new in DATE_FIXES:
        row = conn.execute(
            "SELECT consult_date FROM consultations WHERE id = ?", (cid,)).fetchone()
        if not row:
            print(f"  id={cid} — 레코드 없음 (건너뜀)")
        elif row["consult_date"] == old:
            date_plan.append((cid, old, new))
            print(f"  id={cid} '{old}' -> '{new}'")
        elif row["consult_date"] == new:
            print(f"  id={cid} — 이미 정정됨 ('{new}')")
        else:
            print(f"  id={cid} — 예상과 다른 값 '{row['consult_date']}' (건너뜀)")

    print("=== 2) 거처 칸 병원명 ===")
    for cid, hosp in LOCATION_FIXES:
        row = conn.execute(
            "SELECT current_location_type, current_location_name "
            "FROM consultations WHERE id = ?", (cid,)).fetchone()
        if not row:
            print(f"  id={cid} — 레코드 없음 (건너뜀)")
        elif row["current_location_type"] == hosp:
            loc_plan.append((cid, hosp, row["current_location_name"]))
            print(f"  id={cid} type '{hosp}' -> '입원중', name -> '{hosp}'")
        elif row["current_location_type"] == "입원중":
            print(f"  id={cid} — 이미 정정됨 (type='입원중')")
        else:
            print(f"  id={cid} — 예상과 다른 값 '{row['current_location_type']}' (건너뜀)")

    total = len(date_plan) + len(loc_plan)
    print(f"\n총 {total}건 정정 예정 (날짜 {len(date_plan)} + 거처 {len(loc_plan)})")

    if not args.apply:
        print("\n[dry-run] --apply 를 붙이면 실제 적용됩니다. DB 변경 없음.")
        conn.close()
        return
    if total == 0:
        print("\n정정할 데이터가 없습니다. (이미 정리됨)")
        conn.close()
        return
    conn.close()

    backup_db()
    conn = models.get_db()
    for cid, old, new in date_plan:
        conn.execute("UPDATE consultations SET consult_date = ? WHERE id = ?",
                     (new, cid))
    for cid, hosp, _ in loc_plan:
        conn.execute(
            "UPDATE consultations SET current_location_type = '입원중', "
            "current_location_name = ? WHERE id = ?", (hosp, cid))
    conn.commit()
    conn.close()
    print(f"\n[apply] {total}건 정정 완료.")


if __name__ == "__main__":
    main()
