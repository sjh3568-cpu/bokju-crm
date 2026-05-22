"""admission_purpose(입원목적) 데이터 정정 — bokju-crm.

레거시 엑셀 마이그레이션 시 admission_purpose 칸에 잘못 들어간 값을 정리한다.

- '다제내성균' 177건: 입원목적이 아니라 임상 상태(CRE/VRE 감염)를 오기재한 것.
  발병일이 없어 회복기/비회복기 구분이 불가능 → 빈값(미판정)으로 정정.
  (다제내성균 환자 대부분은 diseases 에 비사용증후군 등 재활 질환이 이미 기록돼 있음)
- 혼입값 7종: 보험유형('자보')·부가서비스('간호통합')가 섞이거나 오타.
  표준값으로 정규화하되, 회복기/일반 구분이 불가능한 값은 추측하지 않고 빈값 처리.

표준 admission_purpose 값: 회복기재활 / 비회복기재활 / 일반재활 / 요양

사용법:
    python tools/fix_admission_purpose.py          → dry-run (DB 변경 없음)
    python tools/fix_admission_purpose.py --apply  → 실제 적용 + 직전 자동 백업

멱등(idempotent) — 이미 정리됐으면 다시 실행해도 변경 없음.
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

# 정정 매핑 — 값이 None 이면 빈값(NULL, 미판정) 처리.
# 회복기/비회복기/일반 구분이 불가능한 값은 추측하지 않고 NULL 로 둔다.
FIXES: dict[str, str | None] = {
    "다제내성균":             None,         # 임상상태 오기재, 발병일無 → 미판정
    "회복기재활.대재내성균":   "회복기재활",   # '대재내성균'=다제내성균 오타
    "회복기재활, 간호통합":    "회복기재활",   # '간호통합'=부가서비스 메모
    "일반재활(자보)":         "일반재활",     # '(자보)'=보험유형 혼입
    "비회복기(자보)":         "비회복기재활",  # '(자보)' 제거 + 표준명
    "자보":                  None,         # 자동차보험=보험유형, 입원목적 아님
    "재활":                  None,         # 회복기/일반 구분 불가
    "재활, 간호간병통합서비스": None,         # '재활' 구분 불가
}


def backup_db() -> None:
    src = ROOT / "bokju.db"
    dst = ROOT / "backups" / f"pre_purpose_cleanup_{datetime.now():%Y%m%d_%H%M%S}.db"
    dst.parent.mkdir(exist_ok=True)
    shutil.copy2(src, dst)
    print(f"[backup] {dst}")


def main() -> None:
    ap = argparse.ArgumentParser(description="admission_purpose 데이터 정정")
    ap.add_argument("--apply", action="store_true",
                    help="실제 DB 적용 (기본: dry-run)")
    args = ap.parse_args()

    conn = models.get_db()
    print("=== 정정 대상 현황 ===")
    plan: list[tuple[str, str | None, int]] = []
    for old, new in FIXES.items():
        n = conn.execute(
            "SELECT COUNT(*) FROM consultations WHERE TRIM(admission_purpose) = ?",
            (old,),
        ).fetchone()[0]
        if n:
            plan.append((old, new, n))
            print(f"  '{old}' ({n}건) -> {new if new is not None else '(빈값/미판정)'}")
    total = sum(n for _, _, n in plan)
    print(f"  총 {total}건 정정 예정")
    conn.close()

    if not args.apply:
        print("\n[dry-run] --apply 를 붙이면 실제 적용됩니다. DB 변경 없음.")
        return
    if total == 0:
        print("\n정정할 데이터가 없습니다. (이미 정리됨)")
        return

    backup_db()
    conn = models.get_db()
    for old, new, _ in plan:
        conn.execute(
            "UPDATE consultations SET admission_purpose = ? "
            "WHERE TRIM(admission_purpose) = ?",
            (new, old),
        )
    conn.commit()
    remain = conn.execute(
        "SELECT COUNT(*) FROM consultations WHERE TRIM(admission_purpose) IN ({})"
        .format(",".join("?" * len(FIXES))),
        tuple(FIXES.keys()),
    ).fetchone()[0]
    conn.close()
    print(f"\n[apply] {total}건 정정 완료. 잔여 정정대상: {remain}건 (0이어야 정상)")


if __name__ == "__main__":
    main()
