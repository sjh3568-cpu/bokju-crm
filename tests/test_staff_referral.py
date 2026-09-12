"""직원소개 분석 — 기관·부서 구조화, 소개 환자 상세, 재단 내부 필터.

소개자를 (기관·부서·이름)으로 나눠 입력하면 기관/부서 단위 성과와, 소개자별로
어떤 환자를 소개했는지(주상병·소개일·입원일·소요일)까지 낸다. 재단 내부만 필터는
소개자 기관이 재단 3개 기관인 건만 센다(외부 지인·타병원 제외).
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import models


class StaffReferralTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "staff.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        with models.get_db() as conn:
            for pid, name in ((1, "환자가"), (2, "환자나"), (3, "환자다")):
                conn.execute("INSERT INTO patients (id,name,gender) VALUES (?,?,'F')", (pid, name))

            def ins(cid, pid, person, org, dept, status, adm, diseases):
                conn.execute(
                    """INSERT INTO consultations
                       (id, patient_id, consult_date, admission_status, actual_admission_date,
                        referral_source_detail, referrer_person, referrer_org, referrer_dept, diseases)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (cid, pid, "2026-08-01", status, adm,
                     json.dumps(["직원소개"], ensure_ascii=False),
                     person, org, dept, json.dumps(diseases, ensure_ascii=False)))
            # 박수정(복주회복병원·간호부) 소개 2건 중 1건 입원
            ins(1, 1, "박수정", "복주회복병원", "간호부", "입원완료", "2026-08-10", ["뇌경색"])
            ins(2, 2, "박수정", "복주회복병원", "간호부", "입원예정", None, ["고관절 골절"])
            # 외부 지인(기관 없음) 1건 — 재단 내부 필터에서 빠져야 한다
            ins(3, 3, "주변지인", "", "", "입원완료", "2026-08-15", ["비사용증후군"])

    def test_org_dept_and_patient_rows(self):
        d = models.staff_referral_overview()
        park = next(r for r in d["referrers"] if r["name"] == "박수정")
        self.assertEqual(park["org"], "복주회복병원")
        self.assertEqual(park["dept"], "간호부")
        self.assertEqual(park["referrals"], 2)
        self.assertEqual(park["admissions"], 1)
        # 소개 환자 상세 — 주상병(diseases)·소개일·입원일·소요일
        rows = {row["patient_name"]: row for row in park["rows"]}
        self.assertEqual(set(rows), {"환자가", "환자나"})
        self.assertIn("뇌경색", rows["환자가"]["diagnosis"])
        self.assertTrue(rows["환자가"]["admitted"])
        self.assertEqual(rows["환자가"]["lead_days"], 9)   # 08-01 → 08-10
        self.assertFalse(rows["환자나"]["admitted"])
        self.assertIsNone(rows["환자나"]["admitted_at"])

    def test_org_and_dept_rollup(self):
        d = models.staff_referral_overview()
        org = next(o for o in d["orgs"] if o["org"] == "복주회복병원")
        self.assertEqual(org["referrals"], 2)
        self.assertEqual(org["admissions"], 1)
        dept = next(x for x in d["depts"] if x["dept"] == "간호부")
        self.assertEqual(dept["org"], "복주회복병원")
        self.assertEqual(dept["referrals"], 2)

    def test_internal_only_excludes_orgless(self):
        alld = models.staff_referral_overview()
        internal = models.staff_referral_overview(internal_only=True)
        self.assertEqual(alld["referrals"], 3)      # 박수정 2 + 주변지인 1
        self.assertEqual(internal["referrals"], 2)  # 기관 없는 주변지인 제외

    def test_monthly_trend(self):
        d = models.staff_referral_overview()
        aug = next(m for m in d["monthly"] if m["month"] == "2026-08")
        self.assertEqual(aug["referrals"], 3)
        self.assertEqual(aug["admissions"], 2)


if __name__ == "__main__":
    unittest.main()
