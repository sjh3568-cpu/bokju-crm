"""입원 만료(퇴원 예정)·회복기 종료 계산이 명부 실제값을 따르는지.

- 회복기(S005) 종료 = 명부 재활종료일(rehab_end). 입원일+180 추정이 아니다.
- 뇌졸중(중추) 퇴원 예정 = min(입원일+1년, 발병일+2년). 연장은 수동(discharge_due_date).
- 비중추 퇴원 예정 = 재활종료일(회복기 기간이 곧 입원 기간).
발병일은 원무 명부 L열 → admission_episodes.onset_date로 적재된다.
"""
import unittest
from datetime import date, timedelta

import app


def _expiry(consultation):
    with app.app.test_request_context():
        return app._admission_expiry(consultation)


def _iso(d):
    return d.isoformat()


class OnsetExpiryTests(unittest.TestCase):
    def _stroke(self, **kw):
        # 중추신경계는 명부 수가구분(회복기/비회복기)이 있어야 기간이 산정된다.
        base = {"diseases": ["뇌경색"], "admission_status": "입원완료",
                "actual_admission_date": None, "rehab_end_imported": 1,
                "roster_care_phase": "회복기"}
        base.update(kw)
        return base

    def test_billing_uses_roster_rehab_end(self):
        """회복기 종료 = 명부 재활종료일. 입원일+180 추정을 쓰지 않는다."""
        adm = date(2026, 1, 1)
        rehab = date(2026, 5, 20)   # 입원일+180(6/29)과 다른 실제값
        ax = _expiry(self._stroke(actual_admission_date=_iso(adm),
                                  rehab_end_date=_iso(rehab)))
        self.assertEqual(ax["billing_date"], _iso(rehab))

    def test_stroke_total_is_one_year_when_onset_far(self):
        """뇌졸중 — 발병일+2년이 멀면 입원일+1년이 퇴원 예정."""
        adm = date(2026, 1, 1)
        onset = date(2025, 12, 1)   # 발병+2년 = 2027-12-01 (입원+1년 2026-12-31보다 뒤)
        ax = _expiry(self._stroke(actual_admission_date=_iso(adm),
                                  onset_date=_iso(onset), rehab_end_date=_iso(date(2026, 6, 29))))
        self.assertEqual(ax["total_date"], _iso(app._day_of(adm, app.TOTAL_STAY_DAYS)))

    def test_stroke_total_capped_by_onset_plus_two_years(self):
        """발병 후 한참 지나 입원한 뇌졸중 — 발병일+2년이 입원일+1년보다 이르면 그게 상한."""
        onset = date(2024, 1, 1)
        adm = date(2025, 6, 1)      # 입원+1년=2026-06, 발병+2년=2025-12-31(더 이름)
        ax = _expiry(self._stroke(actual_admission_date=_iso(adm),
                                  onset_date=_iso(onset), rehab_end_date=_iso(date(2025, 11, 1))))
        self.assertEqual(ax["total_date"], _iso(app._day_of(onset, 730)))
        self.assertLess(ax["total_date"], _iso(app._day_of(adm, app.TOTAL_STAY_DAYS)))

    def test_stroke_without_onset_no_cap(self):
        """발병일이 없으면 상한 없이 입원일+1년."""
        adm = date(2026, 1, 1)
        ax = _expiry(self._stroke(actual_admission_date=_iso(adm),
                                  onset_date=None, rehab_end_date=_iso(date(2026, 6, 29))))
        self.assertEqual(ax["total_date"], _iso(app._day_of(adm, app.TOTAL_STAY_DAYS)))

    def test_stroke_from_roster_diagnosis_when_consult_misses_it(self):
        """상담 병명칸에 뇌졸중이 없어도 명부 주상병(뇌경색 등)이면 중추로 본다.
        → 퇴원 예정이 회복기 종료가 아니라 입원일+1년으로 잡혀야 한다."""
        adm = date(2026, 1, 1)
        ax = _expiry({"diseases": ["암"], "admission_status": "입원완료",
                      "actual_admission_date": _iso(adm), "rehab_end_imported": 1,
                      "roster_care_phase": "회복기", "rehab_end_date": _iso(date(2026, 6, 29)),
                      "roster_diagnosis": "기타 뇌경색증"})
        self.assertEqual(ax["total_date"], _iso(app._day_of(adm, app.TOTAL_STAY_DAYS)))

    def test_noncns_total_is_roster_rehab_end(self):
        """비중추 — 퇴원 예정 = 재활종료일(회복기 기간이 곧 입원 기간). 전환(billing) 없음."""
        adm = date(2026, 1, 1)
        rehab = date(2026, 3, 2)
        ax = _expiry({"diseases": ["고관절 골절"], "admission_status": "입원완료",
                      "actual_admission_date": _iso(adm), "rehab_end_imported": 1,
                      "rehab_end_date": _iso(rehab)})
        self.assertEqual(ax["total_date"], _iso(rehab))
        self.assertIsNone(ax["billing_left"])   # 비중추는 회복기→비회복기 전환 없음


if __name__ == "__main__":
    unittest.main()
