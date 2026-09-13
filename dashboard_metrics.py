"""대시보드 KPI 보조 지표 — 비교값·스파크라인·병동별 재원·입퇴원 추이.

대시보드 본체(app.dashboard)가 쓰는 재원·회복기 판정은 그대로 두고, 그 위에 얹는
'변화'와 '분포'만 여기서 만든다. 근거는 두 곳뿐이다.

- 상담 건수: consultations.consult_date
- 입원·퇴원·재원: admission_episodes 중 roster_key가 있는 회차(원무 명부). 앱이 만든
  회차는 퇴원일이 안 채워져 흐름·재원을 부풀리므로 census와 같은 기준으로 뺀다.

모든 함수는 날짜 문자열(ISO)만 다루고 화면 라벨은 만들지 않는다.
"""
from datetime import date, timedelta

from config import ROOM_BED_CAPACITIES, WARD_BED_CAPACITIES
from models import AWAY_EVENT_TYPES, get_db

ROSTER = "roster_key IS NOT NULL"


def _days(n, end=None):
    end = end or date.today()
    return [(end - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


def _ward_sort_key(ward):
    digits = "".join(ch for ch in (ward or "") if ch.isdigit())
    return (0, int(digits)) if digits else (1, ward or "")


# ── 상담 ──────────────────────────────────────────────────────────────

def consult_counts_by_date(dates):
    """dates(ISO 목록)별 상담 건수. 없는 날은 0."""
    if not dates:
        return {}
    conn = get_db()
    try:
        ph = ",".join("?" * len(dates))
        rows = conn.execute(
            f"SELECT consult_date AS d, COUNT(*) AS n FROM consultations "
            f"WHERE consult_date IN ({ph}) GROUP BY consult_date", dates).fetchall()
    finally:
        conn.close()
    found = {r["d"]: r["n"] for r in rows}
    return {d: found.get(d, 0) for d in dates}


def consult_admissions_by_date(dates):
    """dates별 상담 기준 입원(예정+완료) 건수 — 대시보드 '오늘 입원' KPI와 같은 정의.

    날짜는 입원완료면 실제 입원일, 아니면 입원예정일. 취소·퇴원완료는 뺀다.
    """
    if not dates:
        return {}
    conn = get_db()
    try:
        ph = ",".join("?" * len(dates))
        rows = conn.execute(
            f"""SELECT date(COALESCE(
                       CASE WHEN admission_status = '입원완료' THEN NULLIF(actual_admission_date, '') END,
                       CASE WHEN admission_status = '입원완료' THEN NULLIF(admission_date, '') END,
                       NULLIF(planned_admission_date, ''))) AS d, COUNT(*) AS n
                FROM consultations
                WHERE COALESCE(admission_status, '') NOT IN ('입원취소', '퇴원완료')
                GROUP BY d HAVING d IN ({ph})""", dates).fetchall()
    finally:
        conn.close()
    found = {r["d"]: r["n"] for r in rows}
    return {d: found.get(d, 0) for d in dates}


def consult_weekday_matrix(weeks=8, today=None):
    """최근 n주 요일×주차 상담 건수 — '어느 요일에 몰리나' 히트맵용.

    행은 이번 주(월~일)부터 거슬러 n주, 열은 월~일. 상담 시각(consult_time)은
    8천여 건 중 채워진 게 없어 시간대별은 만들 수 없다.
    """
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    first = monday - timedelta(weeks=weeks - 1)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT consult_date AS d, COUNT(*) AS n FROM consultations "
            "WHERE consult_date BETWEEN ? AND ? GROUP BY consult_date",
            (first.isoformat(), (monday + timedelta(days=6)).isoformat())).fetchall()
    finally:
        conn.close()
    by_date = {r["d"]: r["n"] for r in rows}
    out_weeks = []
    totals = [0] * 7
    peak = 0
    for w in range(weeks):
        start = first + timedelta(weeks=w)
        counts = []
        for i in range(7):
            d = start + timedelta(days=i)
            n = by_date.get(d.isoformat(), 0) if d <= today else None
            counts.append(n)
            if n:
                totals[i] += n
                peak = max(peak, n)
        out_weeks.append({
            "label": start.strftime("%m.%d"),
            "start": start.isoformat(),
            "current": start == monday,
            "counts": counts,
        })
    return {"weeks": out_weeks, "totals": totals, "peak": peak,
            "from": first.isoformat(), "to": today.isoformat()}


def consult_weekday_hour_matrix(date_from=None, date_to=None):
    """기간 내 상담을 요일 × 시간대로 센다 — 통계 화면 히트맵.

    시간대는 consult_time(HH:MM)의 시(時). 시각이 비어 있는 상담은 '시각 미입력' 행에
    모아 요일 합계에는 넣는다 — 지금까지 입력된 시각이 없어 당분간 이 행이 대부분이다.
    """
    date_from = date_from or "2000-01-01"
    date_to = date_to or date.today().isoformat()
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT consult_date AS d, COALESCE(NULLIF(TRIM(consult_time), ''), '') AS t, COUNT(*) AS n
               FROM consultations WHERE consult_date BETWEEN ? AND ?
               GROUP BY consult_date, t""", (date_from, date_to)).fetchall()
    finally:
        conn.close()
    hours = list(range(8, 19))            # 08시 ~ 18시
    grid = {h: [0] * 7 for h in hours}
    unknown = [0] * 7
    totals = [0] * 7
    timed = 0
    for r in rows:
        try:
            wd = date.fromisoformat(r["d"][:10]).weekday()
        except (TypeError, ValueError):
            continue
        totals[wd] += r["n"]
        hh = None
        t = r["t"]
        if t and t[:2].isdigit():
            hh = int(t[:2])
        if hh is None:
            unknown[wd] += r["n"]
            continue
        timed += r["n"]
        hh = min(max(hh, hours[0]), hours[-1])
        grid[hh][wd] += r["n"]
    peak = max((v for h in hours for v in grid[h]), default=0)
    return {"hours": [{"label": f"{h:02d}시", "counts": grid[h]} for h in hours],
            "unknown": unknown, "totals": totals, "peak": peak, "timed": timed,
            "total": sum(totals), "from": date_from, "to": date_to}


# ── 입원·퇴원·재원 (원무 명부 회차) ───────────────────────────────────

def admission_flow_by_date(dates):
    """dates별 명부 입원·퇴원 건수."""
    if not dates:
        return {}
    conn = get_db()
    try:
        lo, hi = min(dates), max(dates)
        ins = conn.execute(
            f"SELECT date(admitted_at) AS d, COUNT(*) AS n FROM admission_episodes "
            f"WHERE {ROSTER} AND admitted_at IS NOT NULL AND admitted_at != '' "
            f"AND date(admitted_at) BETWEEN ? AND ? GROUP BY date(admitted_at)", (lo, hi)).fetchall()
        outs = conn.execute(
            f"SELECT date(discharged_at) AS d, COUNT(*) AS n FROM admission_episodes "
            f"WHERE {ROSTER} AND discharged_at IS NOT NULL AND discharged_at != '' "
            f"AND date(discharged_at) BETWEEN ? AND ? GROUP BY date(discharged_at)", (lo, hi)).fetchall()
    finally:
        conn.close()
    i_map = {r["d"]: r["n"] for r in ins}
    o_map = {r["d"]: r["n"] for r in outs}
    return {d: {"in": i_map.get(d, 0), "out": o_map.get(d, 0)} for d in dates}


def census_by_date(dates):
    """dates별 그날 자정 기준 재원 인원(명부 회차: 입원일 ≤ d < 퇴원일)."""
    if not dates:
        return {}
    conn = get_db()
    try:
        out = {}
        for d in dates:
            out[d] = conn.execute(
                f"SELECT COUNT(*) FROM admission_episodes "
                f"WHERE {ROSTER} AND admitted_at IS NOT NULL AND admitted_at != '' "
                f"AND date(admitted_at) <= ? "
                f"AND (discharged_at IS NULL OR discharged_at = '' OR date(discharged_at) > ?)",
                (d, d)).fetchone()[0]
    finally:
        conn.close()
    return out


def away_by_date(dates):
    """dates별 외진 중 인원(나간 날 ≤ d, 복귀일 없거나 > d)."""
    if not dates:
        return {}
    conn = get_db()
    try:
        ph = ",".join("?" * len(AWAY_EVENT_TYPES))
        out = {}
        for d in dates:
            out[d] = conn.execute(
                f"SELECT COUNT(*) FROM admission_events "
                f"WHERE event_type IN ({ph}) AND event_date IS NOT NULL AND event_date != '' "
                f"AND event_date <= ? "
                f"AND (returned_at IS NULL OR returned_at = '' OR date(returned_at) > ?)",
                (*AWAY_EVENT_TYPES, d, d)).fetchone()[0]
    finally:
        conn.close()
    return out


def discharges_on(d):
    """d에 퇴원한 명부 회차 목록 — 오늘 입·퇴원 일정의 '퇴원' 행."""
    conn = get_db()
    try:
        rows = conn.execute(
            f"""SELECT e.id, e.patient_id, e.consultation_id, e.room_number, e.ward,
                       e.discharged_at, e.discharge_destination, e.attending_doctor,
                       e.diagnosis_name, p.name AS patient_name
                FROM admission_episodes e JOIN patients p ON p.id = e.patient_id
                WHERE {ROSTER} AND date(e.discharged_at) = ?
                ORDER BY e.ward, e.room_number""", (d,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _norm_room(room):
    """'316호 ★'·'316' → '316호' — 명부 표기 흔들림을 config 키와 맞춘다."""
    digits = "".join(ch for ch in (room or "") if ch.isdigit())
    return f"{digits}호" if digits else (room or "").strip()


def _ward_of_room(room):
    """'1203호'→'12병동', '305호'→'3병동' — app._dashboard_ward_label과 같은 규칙."""
    digits = "".join(ch for ch in (room or "") if ch.isdigit())
    if len(digits) >= 4:
        return f"{int(digits[:2])}병동"
    if len(digits) == 3:
        return f"{int(digits[0])}병동"
    return None


def ward_occupancy():
    """병동별 재원·남/여·빈 병상 → 가동률·압박 단계.

    허가 병상(config.WARD_BED_CAPACITIES)이 있는 병동은 재원 0명이어도 줄을 만든다.
    남/여 빈 병상은 병실별 병상 수(config.ROOM_BED_CAPACITIES)가 있어야 센다 —
    남자만 있는 방의 빈자리는 남자 병상, 여자만 있는 방은 여자 병상, 빈 방은 성별 무관.
    병실 병상 수가 없는 병동은 rooms_known=False로 두고 화면이 남/여 재원만 보여준다.
    단계: ~90% easy · 90~95% mid · 95%~ full · 병상 수 없음 none.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            f"""SELECT COALESCE(NULLIF(e.ward, ''), '병동 미지정') AS ward,
                       COALESCE(NULLIF(e.room_number, ''), '') AS room,
                       CASE WHEN p.gender IN ('M', 'F') THEN p.gender ELSE 'U' END AS gender,
                       COUNT(*) AS n
                FROM admission_episodes e JOIN patients p ON p.id = e.patient_id
                WHERE e.{ROSTER} AND e.discharged_at IS NULL
                  AND e.admitted_at IS NOT NULL AND e.admitted_at != ''
                GROUP BY 1, 2, 3""").fetchall()
    finally:
        conn.close()
    wards = {}
    rooms = {}   # (ward, room) -> {"M":n,"F":n,"U":n}
    for r in rows:
        w = wards.setdefault(r["ward"], {"count": 0, "male": 0, "female": 0})
        w["count"] += r["n"]
        if r["gender"] == "M":
            w["male"] += r["n"]
        elif r["gender"] == "F":
            w["female"] += r["n"]
        if r["room"]:
            rooms.setdefault((r["ward"], _norm_room(r["room"])), {"M": 0, "F": 0, "U": 0})[r["gender"]] += r["n"]
    for w in WARD_BED_CAPACITIES:
        wards.setdefault(w, {"count": 0, "male": 0, "female": 0})
    # 병실 병상 수를 병동별로 모은다 — 지금 비어 있는 방도 병동을 방 번호로 알아낸다.
    room_caps_by_ward = {}
    for room, cap in ROOM_BED_CAPACITIES.items():
        room = _norm_room(room)
        w = next((k[0] for k in rooms if k[1] == room), None) or _ward_of_room(room)
        if w:
            room_caps_by_ward.setdefault(w, {})[room] = cap

    items = []
    peak = max((v["count"] for v in wards.values()), default=0) or 1
    for ward, v in wards.items():
        cap = WARD_BED_CAPACITIES.get(ward)
        n = v["count"]
        pct = round(n / cap * 100, 1) if cap else None
        if pct is None:
            level, bar = "none", round(n / peak * 100)
        else:
            level = "full" if pct >= 95 else "mid" if pct >= 90 else "easy"
            bar = min(100, pct)
        item = {"ward": ward, "count": n, "capacity": cap, "pct": pct, "level": level, "bar": bar,
                "free": (cap - n) if cap else None,
                "male": v["male"], "female": v["female"],
                "rooms_known": False, "free_m": None, "free_f": None, "free_open": None, "free_unknown": None, "no_room": 0}
        rcaps = room_caps_by_ward.get(ward)
        if cap and rcaps:
            free_m = free_f = free_open = 0
            for room, rcap in rcaps.items():
                occ = rooms.get((ward, room), {"M": 0, "F": 0, "U": 0})
                used = occ["M"] + occ["F"] + occ["U"]
                left = max(0, rcap - used)
                if used == 0:
                    free_open += left
                elif occ["M"] and not occ["F"]:
                    free_m += left
                elif occ["F"] and not occ["M"]:
                    free_f += left
                # 남녀가 섞인 방(입력 오류)이나 성별 미상만 있는 방은 어느 쪽에도 넣지 않는다.
            known_total = sum(rcaps.values())
            # 병실이 안 적힌 재원(명부 누락)은 어느 방인지 몰라 위 셈에 안 들어간다 — 따로 보여준다.
            in_rooms = sum(sum(v.values()) for k, v in rooms.items() if k[0] == ward)
            item.update(rooms_known=True, free_m=free_m, free_f=free_f, free_open=free_open,
                        free_unknown=max(0, cap - known_total), no_room=max(0, n - in_rooms))
        items.append(item)
    items.sort(key=lambda x: _ward_sort_key(x["ward"]))
    return items


def unassigned_planned(days=2, today=None):
    """오늘~n일 내 입원예정인데 병실이 비어 있는 상담 — 원무와 병실을 정할 대상."""
    today = today or date.today()
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT c.id, c.planned_admission_date, c.planned_admission_time,
                      c.attending_doctor, c.counselor, p.name AS patient_name
               FROM consultations c JOIN patients p ON p.id = c.patient_id
               WHERE c.admission_status = '입원예정'
                 AND c.planned_admission_date BETWEEN ? AND ?
                 AND (c.room_number IS NULL OR TRIM(c.room_number) = '')
               ORDER BY c.planned_admission_date, COALESCE(c.planned_admission_time, ''), c.id""",
            (today.isoformat(), (today + timedelta(days=days)).isoformat())).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ── 이번달 실적 (상담 → 입원 전환) ───────────────────────────────────

def _month_span(first, until):
    return first.isoformat(), until.isoformat()


def month_performance(today=None):
    """이번달 1일~오늘 상담 유입·입원 성사·전환율과, 지난달 같은 기간(1일~같은 날) 비교.

    정의는 /stats KPI와 같다 — 성사 = 입원완료 + 퇴원완료, 전환율 = 성사 / 상담.
    한 달을 통째로 비교하면 월초엔 늘 지난달보다 적게 보이므로 같은 일수로 자른다.
    """
    today = today or date.today()
    first = today.replace(day=1)
    prev_last = first - timedelta(days=1)
    prev_first = prev_last.replace(day=1)
    prev_until = min(prev_last, prev_first + timedelta(days=today.day - 1))
    conn = get_db()
    try:
        def agg(lo, hi):
            r = conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN admission_status IN ('입원완료', '퇴원완료') THEN 1 ELSE 0 END) AS done,
                          SUM(CASE WHEN admission_status = '입원예정' THEN 1 ELSE 0 END) AS planned,
                          SUM(CASE WHEN admission_status = '입원보류' OR consult_result = '상담보류' THEN 1 ELSE 0 END) AS hold
                   FROM consultations WHERE consult_date BETWEEN ? AND ?""", (lo, hi)).fetchone()
            total, done = r["total"] or 0, r["done"] or 0
            return {"total": total, "done": done, "planned": r["planned"] or 0, "hold": r["hold"] or 0,
                    "rate": round(done / total * 100, 1) if total else 0.0}
        cur = agg(*_month_span(first, today))
        prev = agg(*_month_span(prev_first, prev_until))
        daily = conn.execute(
            """SELECT consult_date AS d, COUNT(*) AS n,
                      SUM(CASE WHEN admission_status IN ('입원완료', '퇴원완료') THEN 1 ELSE 0 END) AS done
               FROM consultations WHERE consult_date BETWEEN ? AND ? GROUP BY consult_date""",
            _month_span(first, today)).fetchall()
    finally:
        conn.close()
    by = {r["d"]: r for r in daily}
    days = [(first + timedelta(days=i)).isoformat() for i in range((today - first).days + 1)]
    return {
        "from": first.isoformat(), "to": today.isoformat(),
        "prev_from": prev_first.isoformat(), "prev_to": prev_until.isoformat(),
        "prev_label": f"{prev_first.month}월 1~{prev_until.day}일",
        **cur,
        "prev": prev,
        "delta_total": cur["total"] - prev["total"],
        "delta_done": cur["done"] - prev["done"],
        "delta_rate": round(cur["rate"] - prev["rate"], 1),
        "spark_total": [by[d]["n"] if d in by else 0 for d in days],
        "spark_done": [by[d]["done"] if d in by else 0 for d in days],
    }


# ── KPI 묶음 ──────────────────────────────────────────────────────────

def kpi_metrics(today=None, spark_days=7, trend_days=30):
    """KPI 카드 8장에 필요한 비교값·스파크라인과 30일 입·퇴원 추이를 한 번에.

    반환 dict 키:
      consult: {today, last_week, delta, spark}
      admission: {today, last_week, delta, spark}
      census: {now, last_week, delta, spark}
      away: {now, last_week, delta, spark}
      trend: [{d, in, out}] × trend_days, trend_sum: {in, out}
    '지난주'는 같은 요일(7일 전)이다 — 요일 편차가 커서 어제 대비는 뜻이 없다.
    """
    today = today or date.today()
    spark = _days(spark_days, today)
    last_week = (today - timedelta(days=7)).isoformat()
    need = sorted(set(spark + [last_week]))

    consults = consult_counts_by_date(need)
    admissions = consult_admissions_by_date(need)
    flow = admission_flow_by_date(_days(trend_days, today) + [last_week])
    census = census_by_date(need)
    away = away_by_date(need)
    t = today.isoformat()

    def block(series_map, key=None):
        get = (lambda d: series_map[d][key]) if key else (lambda d: series_map[d])
        now, prev = get(t), get(last_week)
        return {"today": now, "now": now, "last_week": prev, "delta": now - prev,
                "spark": [get(d) for d in spark]}

    trend_dates = _days(trend_days, today)
    trend = [{"d": d, "in": flow[d]["in"], "out": flow[d]["out"]} for d in trend_dates]
    return {
        "consult": block(consults),
        "admission": block(admissions),
        "roster_admission": block(flow, "in"),
        "discharge": block(flow, "out"),          # 명부 기준 퇴원 — 오늘·지난주 같은 요일·7일 스파크
        "census": block(census),
        "away": block(away),
        "trend": trend,
        "trend_sum": {"in": sum(x["in"] for x in trend), "out": sum(x["out"] for x in trend)},
        "spark_dates": spark,
        "last_week_date": last_week,
    }
