"""직원소개 소개자를 기관·부서·이름으로 정리(backfill).

referrer_person이 자유텍스트라 '경도 권병일과장', '경도권병일과장', '권병일과장님
지인'처럼 한 사람이 여러 표기로 쪼개져 있었다. 아래 표대로 기관·부서를 채우고
이름을 정규화한다. 멱등(재실행 안전)하며, '경도연계' 등 표에 없는 값은 안 건드린다.

  python tools/backfill_referrer_staff.py           dry-run
  python tools/backfill_referrer_staff.py --apply   실제 반영(백업 후)

새 소개직원을 정리하려면 MAPPING에 (이름조각, 기관, 부서, 정규화이름)을 추가한다.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import models  # noqa: E402
from tools.excel_import import backup_db  # noqa: E402

# (referrer_person에 포함된 이름조각, 기관, 부서, 정규화할 이름)
MAPPING = [
    ("권병일", "경도요양병원", "원무과", "권병일"),
    ("박득수", "복주요양원", "행정부", "박득수"),
    ("박미라", "복주회복병원", "간호부", "박미라"),
    ("이라미", "복주회복병원", "간호부", "이라미"),
]


def main():
    ap = argparse.ArgumentParser(description="직원소개 소개자 기관·부서 정리")
    ap.add_argument("--apply", action="store_true", help="실제로 DB에 쓴다 (없으면 dry-run)")
    args = ap.parse_args()

    if args.apply:
        backup_db("referrer_staff_backfill")

    conn = models.get_db()
    total = 0
    try:
        for frag, org, dept, name in MAPPING:
            rows = conn.execute(
                "SELECT id, referrer_person FROM consultations "
                "WHERE referral_source_detail LIKE '%직원소개%' "
                "  AND referrer_person LIKE ?", ("%" + frag + "%",)).fetchall()
            if not rows:
                print(f"  [{name}] 매칭 없음")
                continue
            print(f"  [{name}] → {org}/{dept} : {len(rows)}건")
            for r in rows:
                print(f"      {r['referrer_person']!r} → 이름 '{name}'")
            total += len(rows)
            if args.apply:
                conn.execute(
                    "UPDATE consultations SET referrer_org=?, referrer_dept=?, "
                    "referrer_person=?, updated_at=CURRENT_TIMESTAMP "
                    "WHERE referral_source_detail LIKE '%직원소개%' AND referrer_person LIKE ?",
                    (org, dept, name, "%" + frag + "%"))
        if args.apply:
            conn.commit()
    finally:
        conn.close()

    print(f"대상 {total}건.", "반영 완료." if args.apply else "dry-run 이었습니다. --apply 로 실제 반영하세요.")


if __name__ == "__main__":
    main()
