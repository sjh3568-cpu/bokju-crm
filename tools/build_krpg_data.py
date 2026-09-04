"""KRPG 2.2 엑셀의 사업대상 시트를 웹 검색용 JSON으로 변환한다.

사용법:
  python tools/build_krpg_data.py 원본.xlsx [출력.json]
"""
import json
import sys
from pathlib import Path

from openpyxl import load_workbook


SHEET_NAME = "사업대상(1,477개)"


def clean(value):
    return str(value).strip() if value is not None else ""


def main():
    if len(sys.argv) < 2:
        raise SystemExit("원본 xlsx 경로를 입력하세요.")
    source = Path(sys.argv[1])
    output = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("data/krpg_v22.json")
    wb = load_workbook(source, read_only=True, data_only=True)
    if SHEET_NAME not in wb.sheetnames:
        raise SystemExit(f"시트를 찾을 수 없습니다: {SHEET_NAME}")
    ws = wb[SHEET_NAME]
    rows = []
    for values in ws.iter_rows(min_row=4, values_only=True):
        seq, kric, kcd, name_ko, name_en, note = values[:6]
        if not clean(kcd):
            continue
        rows.append({
            "seq": int(seq) if str(seq).isdigit() else clean(seq),
            "kric": clean(kric).zfill(2),
            "kcd": clean(kcd).upper(),
            "name_ko": clean(name_ko),
            "name_en": clean(name_en),
            "note": clean(note),
        })
    if len(rows) != 1477:
        raise SystemExit(f"사업대상 행 수가 1,477개가 아닙니다: {len(rows):,}개")
    payload = {
        "title": "한국형 재활환자분류체계(KRPG) 버전 2.2 KCD 진단 코드 목록",
        "version": "2.2",
        "sheet": SHEET_NAME,
        "count": len(rows),
        "source_file": source.name,
        "items": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{output}: {len(rows):,}개 저장")


if __name__ == "__main__":
    main()
