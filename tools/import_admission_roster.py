"""원무 입퇴원 명부(입퇴재원환자현황.xlsx) → admission_episodes 적재 — bokju-crm.

상담내역 스프레드시트는 입원 사실이 부실하다. 입원완료로 표시됐는데 실제
입원일이 없는 상담이 563건, 퇴원일은 전 건이 비어 있다. 원무 시스템에서 뽑은
입퇴원 명부에는 그 둘이 정확히 들어 있어, 재원 명단과 퇴원 관리를 실제와
맞추려면 이 명부가 기준이 되어야 한다.

명부 1행 = 입원 1회차다. 같은 환자가 여러 번 입원하면 행이 나뉜다(2회 183명,
3회 49명, 4회 이상 15명). 그래서 consultations가 아니라 admission_episodes에
넣는다 — 회차 테이블이 1환자 N회 입퇴원을 담도록 이미 설계돼 있다.

## 환자 매칭

명부는 차트번호로 환자를 식별하지만 CRM은 그 번호를 모른다. 게다가 CRM의
birth_year는 전 건 비어 있어(상담 임포터가 나이만 넣었다) 주민번호로 바로
대조할 수 없다. 그래서 아래 순서로 좁힌다 — 위에서 확정되면 아래는 보지 않는다:

  1. chart_no  — 이전 실행에서 확정해 저장해 둔 번호. 2회차부터는 여기서 끝난다.
  2. 이름이 DB에 유일
  3. 이름 + 성별(주민번호 뒷자리 첫 숫자)
  4. 이름 + 상담에 적힌 입원일이 명부 입원일과 일치 — 나이 오기에 면역이라
     생년 역산보다 먼저 본다(상담 나이가 678로 적혀 생년이 1347년으로
     역산된 환자가 실제로 있었다).
  5. 이름 + 성별 + 나이 — 상담일과 patient_age로 생년을 역산해 ±1년 비교
  6. 이름 + 성별 + 상담일 근접성 — 입원 직전 상담이 그 입원의 상담일 가능성이
     높다. 입원일 이전 PROXIMITY_DAYS 이내에 상담한 후보가 하나뿐이면 확정.
     둘 이상 남으면 PROXIMITY_TIGHT 이내로 한 번 더 좁힌다.

어느 단계에서도 하나로 좁혀지지 않으면 건드리지 않고 리포트에만 남긴다.
확정된 건은 chart_no·birth_year·gender를 환자 레코드에 채워 다음 실행을 돕는다.

단, **생년이 어느 후보와도 안 맞고 입원 전 상담도 없으면** 이름만 같은 남남이다.
이때는 '확정 불가'가 아니라 '동명이인뿐 — CRM에 기록 없음'으로 판정해
--create-missing 대상으로 넘긴다. 예전에는 이런 건이 보류로 묶여 그 환자의
입원 이력이 통째로 누락됐다.

## 명부에만 있는 환자

상담 없이 입원한 환자가 있다(1,264명 중 119명). --create-missing 을 주면
환자를 새로 만들고 입원 회차를 남긴다. 기본값은 만들지 않고 목록만 보고한다.

사용법:
    python tools/import_admission_roster.py "<명부>.xlsx"
        → dry-run. 무엇이 붙고 무엇이 애매한지만 출력. DB는 건드리지 않는다.
    python tools/import_admission_roster.py "<명부>.xlsx" --apply --create-missing
        → 실제 적재. 직전 자동 백업.
    --report <경로.csv> 로 행별 판정 결과를 CSV로 받는다.

재실행해도 안전하다. roster_key(차트번호+입원일)에 UNIQUE가 걸려 있어 같은
회차는 덮어쓴다 — excel_import 와 달리 멱등이다.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import openpyxl  # noqa: E402

import models  # noqa: E402
from tools.excel_import import backup_db  # noqa: E402

# 명부 헤더 → 내부 이름. 컬럼 순서가 바뀌어도 이름으로 찾는다.
COLUMNS = {
    "차트번호": "chart_no",
    "수진자명": "name",
    "주민번호": "rrn",
    "성별/나이": "sex_age",
    "입원일": "admitted_at",
    "퇴원일": "discharged_at",
    "총일수": "total_days",
    "환자유형": "insurance_type",
    "진료의사": "department",
    "병동": "ward",
    "병실": "room_number",
    "주상병": "diagnosis_code",
    "주상병명칭": "diagnosis_name",
    "의사성명": "attending_doctor",
}
REQUIRED = ("chart_no", "name", "admitted_at")

# 입원 전 며칠까지의 상담을 '그 입원의 상담'으로 볼지. 상담 후 대기 기간이
# 길어야 몇 달이라 180일로 둔다. 넓히면 오매칭이 늘어난다.
PROXIMITY_DAYS = 180
# 180일 안에 후보가 둘 이상 남을 때만 쓰는 좁은 창. 더 가까운 상담이 그
# 입원의 상담일 가능성이 높다는 것 외에 새로 가정하는 것이 없다.
PROXIMITY_TIGHT = 90
# 날짜 증거(입원일 일치·상담일 근접)로 확정할 때, 생년이 이만큼 넘게 어긋나는
# 후보는 동명이인으로 보고 뺀다. ±1로 좁히지 않는 이유는 상담의 나이가 만나이와
# 세는나이를 오가서 1~2년은 흔히 어긋나기 때문 — 여기서 거르려는 건 '명백한
# 남남'(실제로 9년 차이 나는 동명이인이 입원일까지 겹친 사례가 있었다)뿐이다.
BIRTH_CONTRADICTION = 3
# 상담 나이 오기로 나온 말도 안 되는 역산 생년은 버린다. 나이가 678로 적힌
# 환자가 있어 1347년이 나왔고, 그 값이 매칭을 통째로 어긋나게 했다.
BIRTH_YEAR_RANGE = (1900, date.today().year)

# match()가 '이 사람은 CRM에 없다'로 판정하는 사유들. 호출부가 이 사유를 보고
# --create-missing 대상으로 넘긴다.
ABSENT_REASONS = ("DB에 없음", "동명이인뿐 — CRM에 기록 없음")


def parse_rrn(rrn):
    """주민번호 앞자리 → (생년 4자리, 성별). 뒷자리는 마스킹돼 있어도 된다."""
    m = re.match(r"(\d{2})(\d{2})(\d{2})-([1-8])", str(rrn or "").strip())
    if not m:
        return None, None
    yy, _mm, _dd, s = m.groups()
    century = 1900 if s in "1256" else 2000
    return century + int(yy), ("M" if s in "1357" else "F")


def to_date(v):
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()[:10].replace("/", "-").replace(".", "-")
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def read_roster(path):
    """명부 xlsx → 행 dict 목록. 헤더 이름으로 컬럼을 찾는다."""
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
    rows = ws.iter_rows(values_only=True)
    header = next(rows)
    idx = {}
    for i, v in enumerate(header):
        key = COLUMNS.get(str(v).strip() if v is not None else "")
        if key and key not in idx:
            idx[key] = i
    missing = [k for k in REQUIRED if k not in idx]
    if missing:
        raise SystemExit("명부에 필수 컬럼이 없다: %s" % ", ".join(missing))

    out = []
    for n, row in enumerate(rows, start=2):
        rec = {k: (row[i] if i < len(row) else None) for k, i in idx.items()}
        if not rec.get("name") or not rec.get("chart_no"):
            continue
        rec["row_no"] = n
        rec["name"] = str(rec["name"]).strip()
        rec["chart_no"] = str(rec["chart_no"]).strip()
        rec["admitted_at"] = to_date(rec.get("admitted_at"))
        rec["discharged_at"] = to_date(rec.get("discharged_at"))
        if rec["admitted_at"] is None:
            continue
        rec["birth_year"], rec["gender"] = parse_rrn(rec.get("rrn"))
        out.append(rec)
    return out


class Matcher:
    """CRM 환자 색인. 이름·성별·추정 생년·상담일을 들고 후보를 좁힌다."""

    def __init__(self, conn):
        self.by_chart = {}
        self.by_name = defaultdict(list)
        self.gender = {}
        self.birth_years = defaultdict(set)
        self.consult_dates = defaultdict(list)
        self.admission_dates = defaultdict(set)

        for pid, name, gender, chart in conn.execute(
                "SELECT id, name, gender, chart_no FROM patients"):
            name = (name or "").strip()
            self.by_name[name].append(pid)
            self.gender[pid] = gender
            if chart:
                self.by_chart[str(chart).strip()] = pid

        # 상담 기록에서 생년을 역산한다 — patients.birth_year 가 비어 있어서다.
        # 상담에 적힌 입원일도 같이 걷는다. 나이 오기에 흔들리는 생년 역산과
        # 달리 날짜는 원무 명부와 같은 사실을 가리키므로 더 단단한 단서다.
        for pid, cd, age, adm, actual in conn.execute(
                "SELECT patient_id, consult_date, patient_age, admission_date, "
                "       actual_admission_date FROM consultations "
                "WHERE consult_date IS NOT NULL"):
            for value in (actual, adm):
                a = to_date(value)
                if a:
                    self.admission_dates[pid].add(a)
            d = to_date(cd)
            if d is None:
                continue
            self.consult_dates[pid].append(d)
            if age not in (None, ""):
                try:
                    year = d.year - int(age)
                except (TypeError, ValueError):
                    continue
                if BIRTH_YEAR_RANGE[0] <= year <= BIRTH_YEAR_RANGE[1]:
                    self.birth_years[pid].add(year)

    def date_evidence(self, rec, pid):
        """이 입원이 그 환자 것이라는 날짜 증거의 세기. 0이면 근거 없음.

        차트번호 충돌(두 차트가 한 환자를 가리킴)을 가를 때 쓴다.
        """
        admitted = rec["admitted_at"]
        if admitted in self.admission_dates.get(pid, ()):
            return 2
        window = admitted - timedelta(days=PROXIMITY_TIGHT)
        if any(window <= d <= admitted for d in self.consult_dates.get(pid, ())):
            return 1
        return 0

    def match(self, rec):
        """(환자id 또는 None, 판정 사유). 하나로 안 좁혀지면 (None, 사유)."""
        pid = self.by_chart.get(rec["chart_no"])
        if pid:
            return pid, "차트번호"

        cands = list(self.by_name.get(rec["name"], ()))
        if not cands:
            return None, "DB에 없음"
        if len(cands) == 1:
            return cands[0], "이름 유일"

        if rec["gender"]:
            narrowed = [p for p in cands if self.gender.get(p) == rec["gender"]]
            if len(narrowed) == 1:
                return narrowed[0], "이름+성별"
            if narrowed:
                cands = narrowed

        # ── 날짜 증거를 생년 역산보다 먼저 본다 ──
        # 상담 나이는 만나이·세는나이가 섞이고 오기도 있어 역산 생년이 1~2년씩
        # 흔들린다. 반면 상담에 적힌 입원일과 상담일은 원무 명부와 같은 사실을
        # 가리킨다. 다만 날짜만 믿으면 생년이 9년 차이 나는 동명이인을 입원일이
        # 겹쳤다는 이유로 채택하는 사고가 나므로, 명백히 어긋나는 후보는 뺀다.
        admitted = rec["admitted_at"]

        def compatible(p):
            known = self.birth_years.get(p, ())
            if not rec["birth_year"] or not known:
                return True
            return any(abs(y - rec["birth_year"]) <= BIRTH_CONTRADICTION for y in known)

        dated = [p for p in cands if compatible(p)] or cands

        narrowed = [p for p in dated if admitted in self.admission_dates.get(p, ())]
        if len(narrowed) == 1:
            return narrowed[0], "이름+입원일 일치"

        window = admitted - timedelta(days=PROXIMITY_TIGHT)
        narrowed = [p for p in dated
                    if any(window <= d <= admitted for d in self.consult_dates.get(p, ()))]
        if len(narrowed) == 1:
            return narrowed[0], "이름+상담일 근접(%d일)" % PROXIMITY_TIGHT

        birth_fit = []
        if rec["birth_year"]:
            birth_fit = [p for p in cands
                         if any(abs(y - rec["birth_year"]) <= 1
                                for y in self.birth_years.get(p, ()))]
            if len(birth_fit) == 1:
                return birth_fit[0], "이름+성별+나이"
            if birth_fit:
                cands = birth_fit

        window = admitted - timedelta(days=PROXIMITY_DAYS)
        near = [p for p in cands
                if any(window <= d <= admitted for d in self.consult_dates.get(p, ()))]
        if len(near) == 1:
            return near[0], "이름+상담일 근접(%d일)" % PROXIMITY_DAYS

        # 생년이 어느 후보와도 안 맞고 입원 전 상담도 없다 = 이름만 같은
        # 남남이다. '확정 불가'로 묶어두면 이 사람의 입원 이력이 통째로
        # 누락된다 — 신규 생성 대상으로 넘긴다.
        if rec["birth_year"] and not birth_fit and not near:
            return None, "동명이인뿐 — CRM에 기록 없음"

        return None, "동명이인 %d명 중 확정 불가" % len(cands)


def upsert_patient(conn, rec, pid):
    """확정된 환자에 차트번호·생년·성별을 채운다. 이미 있는 값은 덮지 않는다."""
    conn.execute(
        "UPDATE patients SET chart_no = COALESCE(NULLIF(chart_no,''), ?), "
        "  birth_year = COALESCE(birth_year, ?), "
        "  gender = CASE WHEN gender IN ('M','F') THEN gender ELSE COALESCE(?, gender) END, "
        "  updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ?",
        (rec["chart_no"], rec["birth_year"], rec["gender"], pid),
    )


def create_patient(conn, rec):
    cur = conn.execute(
        "INSERT INTO patients (name, birth_year, gender, chart_no, note) "
        "VALUES (?, ?, ?, ?, ?)",
        (rec["name"], rec["birth_year"], rec["gender"] or "U", rec["chart_no"],
         "원무 입퇴원 명부에서 생성 (상담 기록 없음)"),
    )
    return cur.lastrowid


def upsert_episode(conn, pid, rec):
    """회차를 넣거나 덮어쓴다. roster_key UNIQUE 덕분에 재실행해도 안전하다."""
    key = "%s|%s" % (rec["chart_no"], rec["admitted_at"].isoformat())
    existing = conn.execute(
        "SELECT id FROM admission_episodes WHERE roster_key = ?", (key,)
    ).fetchone()
    status = "discharged" if rec["discharged_at"] else "admitted"
    values = (
        status,
        rec["admitted_at"].isoformat(),
        rec["discharged_at"].isoformat() if rec["discharged_at"] else None,
        rec.get("room_number"), rec.get("ward"), rec.get("attending_doctor"),
        rec.get("insurance_type"), rec.get("diagnosis_code"), rec.get("diagnosis_name"),
    )
    if existing:
        conn.execute(
            "UPDATE admission_episodes SET status=?, admitted_at=?, discharged_at=?, "
            "  room_number=?, ward=?, attending_doctor=?, insurance_type=?, "
            "  diagnosis_code=?, diagnosis_name=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=?", values + (existing[0],))
        return "갱신"

    episode_no = conn.execute(
        "SELECT COALESCE(MAX(episode_no),0)+1 FROM admission_episodes WHERE patient_id=?",
        (pid,)).fetchone()[0]
    conn.execute(
        "INSERT INTO admission_episodes "
        "  (patient_id, episode_no, status, admitted_at, discharged_at, room_number, "
        "   ward, attending_doctor, insurance_type, diagnosis_code, diagnosis_name, roster_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (pid, episode_no) + values + (key,))
    return "신규"


def main():
    ap = argparse.ArgumentParser(description="원무 입퇴원 명부를 입원 회차로 적재")
    ap.add_argument("path", help="입퇴재원환자현황 xlsx 경로")
    ap.add_argument("--apply", action="store_true", help="실제로 DB에 쓴다 (없으면 dry-run)")
    ap.add_argument("--create-missing", action="store_true",
                    help="상담 기록이 없는 환자를 새로 만든다")
    ap.add_argument("--report", help="행별 판정 결과를 CSV로 저장할 경로")
    args = ap.parse_args()

    rows = read_roster(args.path)
    print("명부 행: %d" % len(rows))

    if args.apply:
        backup_db("admission_roster")

    conn = models.get_db()
    matcher = Matcher(conn)
    rows.sort(key=lambda r: (r["chart_no"], r["admitted_at"]))

    # ── 1차: 차트번호마다 환자를 정한다. 아직 DB에는 쓰지 않는다.
    # 같은 차트번호는 한 사람이므로 첫 행의 판정을 나머지 행도 따른다.
    resolved, reasons, first_rec = {}, {}, {}
    for rec in rows:
        chart = rec["chart_no"]
        if chart in resolved:
            continue
        resolved[chart], reasons[chart] = matcher.match(rec)
        first_rec[chart] = rec

    # ── 충돌 검사: 차트번호 둘이 같은 CRM 환자를 가리키면 둘 중 하나는 틀렸다.
    # 실제로 동명이인이었다 — 김희수(1961년생/1983년생)처럼 이름이 같고 나이만
    # 다른 사람들이다. 생년 비교를 ±1년으로 두다 보니 46년생과 47년생도 같은
    # 후보로 남는다. 어느 쪽이 맞는지 도구가 알 수 없으니 양쪽 다 보류한다.
    claimed = defaultdict(list)
    for chart, pid in resolved.items():
        if pid is not None:
            claimed[pid].append(chart)
    for pid, charts in claimed.items():
        if len(charts) <= 1:
            continue
        # 한쪽만 날짜 증거(그 입원일이 상담에 적혀 있거나 직전에 상담했다)를
        # 가지면 그 차트가 이 환자다. 나머지는 동명이인이지만 어느 환자인지
        # 도구가 알 수 없으니 보류로 남긴다 — 함부로 새 환자를 만들면 중복이
        # 된다(성별이 잘못 적혀 후보에서 빠진 실제 짝이 있었다).
        scored = {c: matcher.date_evidence(first_rec[c], pid) for c in charts}
        best = max(scored.values())
        winners = [c for c in charts if scored[c] == best]
        keep = winners[0] if best > 0 and len(winners) == 1 else None
        for chart in charts:
            if chart == keep:
                continue
            resolved[chart] = None
            reasons[chart] = "차트번호 %d개가 같은 환자를 가리킴 — 확정 불가" % len(charts)

    # ── 2차: 확정되지 않은 신규 환자를 만들고, 회차를 적재한다.
    for chart, pid in resolved.items():
        if pid is None and reasons[chart] in ABSENT_REASONS and args.create_missing:
            resolved[chart] = create_patient(conn, first_rec[chart]) if args.apply else -1
            reasons[chart] = "신규 생성"
        elif pid is not None and args.apply:
            upsert_patient(conn, first_rec[chart], pid)

    results = []
    stats = defaultdict(int)
    for rec in rows:
        pid, why = resolved[rec["chart_no"]], reasons[rec["chart_no"]]
        stats[why] += 1
        action = "건너뜀"
        if pid is not None and pid > 0 and args.apply:
            action = upsert_episode(conn, pid, rec)
        elif pid is not None:
            action = "적재 예정"
        results.append((rec, pid, why, action))

    if args.apply:
        conn.commit()

    patients_seen = len(resolved)
    matched = sum(1 for p in resolved.values() if p is not None)
    print()
    print("환자 %d명 (차트번호 기준) — 판정 사유별 행 수" % patients_seen)
    for why, n in sorted(stats.items(), key=lambda kv: -kv[1]):
        print("  %-24s %5d행" % (why, n))
    print()
    print("  확정된 환자      : %d / %d" % (matched, patients_seen))
    print("  적재 대상 회차   : %d행" % sum(1 for r in results if r[1] is not None))
    print("  보류(확정 불가)  : %d행" % sum(1 for r in results if r[1] is None))
    if not args.apply:
        print()
        print("  ** dry-run이라 DB는 건드리지 않았다. 반영하려면 --apply **")

    if args.report:
        with open(args.report, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["행", "차트번호", "이름", "생년", "성별", "입원일", "퇴원일",
                        "병동", "병실", "환자id", "판정", "처리"])
            for rec, pid, why, action in results:
                w.writerow([rec["row_no"], rec["chart_no"], rec["name"],
                            rec["birth_year"], rec["gender"],
                            rec["admitted_at"], rec["discharged_at"] or "",
                            rec.get("ward") or "", rec.get("room_number") or "",
                            pid if pid and pid > 0 else "", why, action])
        print("  리포트: %s" % args.report)


if __name__ == "__main__":
    main()
