"""모병원 분석 보강 — 전기 대비 증감·질환군·협력기관 연결·종별/지역 필터.

models.hospital_referral_overview()가 만든 표에 관리 판단에 필요한 맥락을 얹는다.
"어느 병원이 무슨 환자를 얼마나 보내는데, 최근 늘고 있나 줄고 있나, 관리 중인가"
가 한 줄에 읽히게 하는 것이 목적이다.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, timedelta

import models
from config import DISEASES_GROUPS

# 상담일지의 병명 → 질환군. 과거 적재분에는 그룹 이름('비사용증후군')이 값으로 직접 들어 있기도 하다.
_DISEASE_TO_GROUP = {name: group for group, names in DISEASES_GROUPS.items() for name in names}
_DISEASE_TO_GROUP.update({group: group for group in DISEASES_GROUPS})
# 자주 나오는 자유 입력 표기를 그룹으로 이어 준다.
_DISEASE_TO_GROUP.update({
    "고관절 골절": "근골격계", "대퇴부 골절": "근골격계", "골절": "근골격계", "골반 골절": "근골격계",
    "폐질환": "비사용증후군", "암": "비사용증후군", "신생물": "비사용증후군", "심장질환": "비사용증후군",
    "뇌졸중": "중추신경계", "편마비": "중추신경계", "사지마비": "중추신경계",
})
GROUP_SHORT = {"중추신경계": "중추신경", "근골격계": "근골격", "비사용증후군": "비사용", "기저질환": "기저"}


def previous_period(date_from: str, date_to: str) -> tuple[str, str]:
    """같은 길이의 직전 기간. 2026-03-01~08-31이면 2025-09-01~2026-02-28."""
    a, b = date.fromisoformat(date_from), date.fromisoformat(date_to)
    days = (b - a).days + 1
    return (a - timedelta(days=days)).isoformat(), (a - timedelta(days=1)).isoformat()


def disease_groups_by_hospital(date_from: str, date_to: str) -> dict[str, list[dict]]:
    """대표 표기별 상위 2개 질환군과 비율. {대표명: [{'group','short','pct'}, …]}."""
    display = models.hospital_display_map()
    conn = models.get_db()
    rows = conn.execute(
        """SELECT TRIM(source_hospital) name, diseases FROM consultations
           WHERE consult_date BETWEEN ? AND ? AND source_hospital IS NOT NULL AND TRIM(source_hospital)!=''
             AND TRIM(COALESCE(diseases,'')) NOT IN ('', '[]')""", (date_from, date_to)).fetchall()
    conn.close()
    counts: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if not models.is_institution_source(r["name"]):
            continue
        try:
            items = json.loads(r["diseases"])
        except (ValueError, TypeError):
            continue
        groups = {_DISEASE_TO_GROUP.get(str(x).strip()) for x in (items or [])} - {None}
        rep = display.get(r["name"], r["name"])
        for g in groups:
            counts[rep][g] += 1
    out = {}
    for rep, c in counts.items():
        total = sum(c.values())
        out[rep] = [{"group": g, "short": GROUP_SHORT.get(g, g), "pct": round(100 * n / total)}
                    for g, n in c.most_common(2)]
    return out


def partner_index() -> dict[str, int]:
    """협력기관으로 등록된 곳 — 대표 표기와 요양기호 양쪽으로 partner id를 찾을 수 있게."""
    display = models.hospital_display_map()
    conn = models.get_db()
    rows = conn.execute(
        """SELECT p.id, COALESCE(p.official_name, h.name) name, d.official_code
           FROM cooperation_partners p JOIN source_hospitals h ON h.id = p.hospital_id
           LEFT JOIN cooperation_facility_directory d ON d.id = p.directory_id""").fetchall()
    conn.close()
    idx = {}
    for r in rows:
        idx[models._hospital_substring_key(display.get(r["name"], r["name"]))] = r["id"]
        idx[models._hospital_substring_key(r["name"])] = r["id"]
        if r["official_code"]:
            idx["code:" + r["official_code"]] = r["id"]
    return idx


def enrich(date_from: str, date_to: str, q: str | None = None) -> dict:
    """overview에 전기 대비·질환군·협력기관 여부·지역을 붙인 결과."""
    cur = models.hospital_referral_overview(date_from, date_to, q=q or None)
    pf, pt = previous_period(date_from, date_to)
    prev = models.hospital_referral_overview(pf, pt)
    prev_by = {h["name"]: h for h in prev["hospitals"]}
    groups = disease_groups_by_hospital(date_from, date_to)
    partners = partner_index()
    kind_idx = models._hospital_kind_index()

    for h in cur["hospitals"]:
        p = prev_by.get(h["name"], {})
        h["prev_referrals"] = p.get("referrals", 0)
        h["prev_admissions"] = p.get("admissions", 0)
        h["delta_referrals"] = h["referrals"] - h["prev_referrals"]
        h["delta_admissions"] = h["admissions"] - h["prev_admissions"]
        h["is_new"] = h["prev_referrals"] == 0 and h["referrals"] >= 2   # 1건짜리 꼬리는 신규로 부르지 않는다
        h["diseases"] = groups.get(h["name"], [])
        # 협력기관 여부 — 대표 표기, 정식명, 요양기호 중 하나라도 맞으면
        code = models.hospital_official_code(h["name"], kind_idx)
        h["partner_id"] = (partners.get("code:" + code) if code else None) \
            or partners.get(models._hospital_substring_key(h["name"])) \
            or (partners.get(models._hospital_substring_key(h["official_name"])) if h.get("official_name") else None)
        # 지역 — 명부에서 특정된 경우만. 추정만 된 곳은 비워 둔다.
        hit = models._resolve_hospital(h["name"], kind_idx)
        if hit is None and h.get("kind_basis") == "유사":
            hit = models._fuzzy_hospital(h["name"], kind_idx)
        h["region"] = hit[2] if hit else None

    cur["prev_from"], cur["prev_to"] = pf, pt
    cur["prev_referrals"] = prev["referrals"]
    cur["prev_admissions"] = prev["admissions"]
    cur["delta_referrals"] = cur["referrals"] - prev["referrals"]
    cur["delta_admissions"] = cur["admissions"] - prev["admissions"]
    cur["prev_linked_referrals"] = prev["linked_referrals"]
    cur["prev_linked_admissions"] = prev["linked_admissions"]
    cur["delta_linked_referrals"] = cur["linked_referrals"] - prev["linked_referrals"]
    cur["delta_linked_admissions"] = cur["linked_admissions"] - prev["linked_admissions"]
    cur["delta_direct_referrals"] = cur["direct_referrals"] - prev["direct_referrals"]
    cur["delta_direct_admissions"] = cur["direct_admissions"] - prev["direct_admissions"]
    cur["prev_hospital_count"] = prev["total_count"]
    cur["new_hospitals"] = sum(1 for h in cur["hospitals"] if h["is_new"])
    cur["partner_count"] = sum(1 for h in cur["hospitals"] if h["partner_id"])
    return cur


def apply_filters(hospitals: list[dict], *, kind: str | None = None, region: str | None = None,
                  partner: str | None = None) -> list[dict]:
    out = hospitals
    if kind:
        out = [h for h in out if h.get("kind") == kind]
    if region:
        out = [h for h in out if h.get("region") == region]
    if partner == "yes":
        out = [h for h in out if h.get("partner_id")]
    elif partner == "no":
        out = [h for h in out if not h.get("partner_id")]
    return out


def filter_options(hospitals: list[dict]) -> dict:
    kinds = Counter(h["kind"] for h in hospitals if h.get("kind"))
    regions = Counter(h["region"] for h in hospitals if h.get("region"))
    order = ["상급종합", "종합병원", "병원", "요양병원", "한방병원", "정신병원", "의원", "요양원"]
    return {
        "kinds": [(k, kinds[k]) for k in order if kinds.get(k)] + [(k, n) for k, n in kinds.items() if k not in order],
        "regions": sorted(regions.items(), key=lambda kv: -kv[1]),
    }


SORT_KEYS = {
    "referrals": lambda h: (-h["referrals"], -h["admissions"]),
    "admissions": lambda h: (-h["admissions"], -h["referrals"]),
    "linked": lambda h: (-h["linked_admissions"], -h["linked_referrals"], -h["admissions"]),
    "up": lambda h: (-h["delta_referrals"], -h["referrals"]),
    "down": lambda h: (h["delta_referrals"], -h["prev_referrals"]),
}
