"""심평원 상세 XLSX에서 기관협력용 진료과목·병상·간호간병 정보를 갱신한다."""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
import sys

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import partnerships  # noqa: E402

BED_COLUMNS = (
    "일반입원실상급병상수", "일반입원실일반병상수", "성인중환자병상수",
    "소아중환자병상수", "신생아중환자병상수", "분만실병상수",
    "정신과폐쇄상급병상수", "정신과폐쇄일반병상수",
    "정신과개방상급병상수", "정신과개방일반병상수",
    "격리병실병상수", "무균치료실병상수",
)


def rows(path: Path):
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.active
    iterator = sheet.iter_rows(values_only=True)
    headers = [str(value or "").strip() for value in next(iterator)]
    try:
        for values in iterator:
            yield {headers[index]: values[index] for index in range(min(len(headers), len(values)))}
    finally:
        workbook.close()


def load_details(base: Path):
    facility = next(base.glob("3.*시설정보*.xlsx"))
    department = next(base.glob("5.*진료과목정보*.xlsx"))
    special = next(base.glob("10.*특수진료정보*.xlsx"))
    beds = {}
    for row in rows(facility):
        code = str(row.get("암호화요양기호") or "").strip()
        if code:
            beds[code] = sum(int(row.get(column) or 0) for column in BED_COLUMNS)
    departments = defaultdict(set)
    for row in rows(department):
        code = str(row.get("암호화요양기호") or "").strip()
        name = str(row.get("진료과목코드명") or "").strip()
        if code and name:
            departments[code].add(name)
    integrated = {
        str(row.get("암호화요양기호") or "").strip()
        for row in rows(special)
        if str(row.get("검색코드") or "").strip() == "KH"
    }
    return departments, beds, integrated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path, help="전국 병의원 현황 XLSX 폴더")
    parser.add_argument("--updated-at", default="2026-03", help="화면에 표시할 자료 기준일")
    args = parser.parse_args()
    departments, beds, integrated = load_details(args.directory)
    count = partnerships.import_facility_details(
        departments=departments, beds=beds, integrated_codes=integrated,
        updated_at=args.updated_at,
    )
    print(f"기관 상세정보 {count:,}건 갱신")


if __name__ == "__main__":
    main()
