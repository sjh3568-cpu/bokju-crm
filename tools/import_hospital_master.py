"""전국 병원 공식명 마스터 import.

사용 예:
  python tools/import_hospital_master.py --xlsx "전국 병의원 및 약국 현황 2026.3/1.병원정보서비스(2026.3.).xlsx"
  python tools/import_hospital_master.py --csv hospitals.csv
  python tools/import_hospital_master.py --api --service-key YOUR_DATA_GO_KR_KEY

XLSX는 심평원 opendata.hira.or.kr '전국 병의원 및 약국 현황' 패키지의 1.병원정보서비스 파일을 권장한다.
CSV는 심평원/공공데이터 계열의 흔한 헤더명을 자동 인식한다.
API는 건강보험심사평가원 병원정보서비스(getHospBasisList) JSON 응답을 기준으로 한다.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import models  # noqa: E402


DEFAULT_HOSPITAL_KINDS = {
    "상급종합",
    "종합병원",
    "병원",
    "요양병원",
    "정신병원",
    "치과병원",
    "한방병원",
}

API_ENDPOINT = "https://apis.data.go.kr/B551182/hospInfoServicev2/getHospBasisList"


def _pick(row: dict, *keys: str) -> str:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return str(row[key]).strip()
    lower = {str(k).strip().lower(): v for k, v in row.items()}
    for key in keys:
        val = lower.get(key.lower())
        if val not in (None, ""):
            return str(val).strip()
    return ""


def _entry_from_row(row: dict) -> dict:
    addr = _pick(row, "주소", "소재지", "addr", "address")
    region = _pick(row, "시도코드명", "시도명", "시도", "sidoCdNm", "sido")
    if not region and addr:
        region = addr.split()[0]
    return {
        "name": _pick(row, "요양기관명", "병원명", "기관명", "yadmNm", "name"),
        "region": region,
        "kind": _pick(row, "종별코드명", "종별", "clCdNm", "kind"),
        "address": addr,
        "phone": _pick(row, "전화번호", "대표전화", "telno", "phone"),
        "official_code": _pick(row, "암호화요양기호", "요양기관기호", "ykiho", "code"),
    }


def _wanted(entry: dict, include_clinics: bool) -> bool:
    if not entry.get("name"):
        return False
    if include_clinics:
        return True
    kind = entry.get("kind") or ""
    return kind in DEFAULT_HOSPITAL_KINDS


def read_xlsx(path: Path, *, include_clinics: bool) -> list[dict]:
    """심평원 '전국 병의원 및 약국 현황' xlsx (1.병원정보서비스 등) → entries."""
    import openpyxl  # 지연 import — xlsx 모드에서만 필요

    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb.active
    iterator = ws.iter_rows(values_only=True)
    try:
        headers = [str(c).strip() if c is not None else "" for c in next(iterator)]
    except StopIteration:
        return []
    entries: list[dict] = []
    for row in iterator:
        if not row or not any(row):
            continue
        rec = {headers[i]: row[i] for i in range(min(len(headers), len(row)))}
        entry = _entry_from_row(rec)
        if _wanted(entry, include_clinics):
            entries.append(entry)
    return entries


def read_csv(path: Path, *, encoding: str, include_clinics: bool) -> list[dict]:
    with path.open("r", encoding=encoding, newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t|")
        rows = csv.DictReader(f, dialect=dialect)
        return [
            entry for row in rows
            if _wanted(entry := _entry_from_row(row), include_clinics)
        ]


def _items_from_api_payload(payload: dict) -> tuple[list[dict], int]:
    body = (((payload or {}).get("response") or {}).get("body") or {})
    total = int(body.get("totalCount") or 0)
    items = (body.get("items") or {}).get("item") or []
    if isinstance(items, dict):
        items = [items]
    return items, total


def read_api(service_key: str, *, include_clinics: bool, rows_per_page: int) -> list[dict]:
    page = 1
    total = None
    entries = []
    while total is None or len(entries) < total:
        params = {
            "serviceKey": service_key,
            "pageNo": page,
            "numOfRows": rows_per_page,
            "_type": "json",
        }
        url = f"{API_ENDPOINT}?{urlencode(params)}"
        with urlopen(url, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        items, total_count = _items_from_api_payload(payload)
        if total is None:
            total = total_count
        if not items:
            break
        for item in items:
            entry = _entry_from_row(item)
            if _wanted(entry, include_clinics):
                entries.append(entry)
        page += 1
        time.sleep(0.08)
    return entries


def main() -> int:
    p = argparse.ArgumentParser(description="전국 병원 공식명 마스터 import")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--xlsx", type=Path, help="심평원 '전국 병의원 및 약국 현황' 1.병원정보서비스 xlsx 경로")
    src.add_argument("--csv", type=Path, help="공식 병원 목록 CSV/TSV 파일 경로")
    src.add_argument("--api", action="store_true", help="공공데이터 병원정보서비스 API에서 직접 가져오기")
    p.add_argument("--service-key", default=os.getenv("DATA_GO_KR_SERVICE_KEY") or os.getenv("HIRA_SERVICE_KEY"))
    p.add_argument("--encoding", default="utf-8-sig")
    p.add_argument("--include-clinics", action="store_true", help="의원/약국 등도 포함")
    p.add_argument("--rows-per-page", type=int, default=1000)
    args = p.parse_args()

    models.init_db()
    if args.xlsx:
        entries = read_xlsx(args.xlsx, include_clinics=args.include_clinics)
        source = "hira_xlsx"
    elif args.csv:
        entries = read_csv(args.csv, encoding=args.encoding, include_clinics=args.include_clinics)
        source = "hira_csv"
    else:
        if not args.service_key:
            p.error("--api 사용 시 --service-key 또는 DATA_GO_KR_SERVICE_KEY/HIRA_SERVICE_KEY가 필요합니다.")
        entries = read_api(args.service_key, include_clinics=args.include_clinics, rows_per_page=args.rows_per_page)
        source = "hira_api"

    imported = models.upsert_source_hospitals(entries, source=source)
    print(f"imported={imported} source={source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
