"""원무 명부 발병일이 표시와 회복기 판정 양쪽에 쓰인다 (2026-09-28 원장: "재원 환자 발병일 다 반영된 거야?").

재원 267명 중 207명은 발병일이 상담일지가 아니라 명부 회차(onset_date)에만 있었다. 전에는 그 값을
표시 전용(onset_display)으로만 넘겨서 ① 대시보드 입·퇴원 현황의 발병일 칸이 '-'였고 ② 회복기
자동판정(_recovery_status)이 발병일 없는 것으로 계산했다. 저장값(disease_onset)은 여전히 안 건드린다.
"""
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import app as main
import models
import partnerships
import support_requests
from app import _care_phase, _recovery_status


def d(n):
    return (date.today() + timedelta(days=n)).isoformat()


class RosterOnsetUsedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "onset.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("onset-test", "test-password", display_name="점검")
        self.uid = models.get_user("onset-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="onset-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        with models.get_db() as conn:
            # 뇌출혈 + 입원목적 '일반재활'(수동 라벨 없음) → 발병일이 있어야만 자동판정이 회복기가 된다.
            # 상담에는 발병일이 없고 명부 회차에만 있다(오일록 님 유형).
            conn.execute("INSERT INTO patients (id,name,gender,chart_no) VALUES (1,'명부발병','M','0000000001')")
            conn.execute("""INSERT INTO consultations (id,patient_id,consult_date,admission_status,actual_admission_date,
                            room_number,diseases,disease_onset,admission_purpose)
                            VALUES (1,1,?,'입원완료',?,'301호','["뇌출혈"]',NULL,'회복기재활')""", (d(-40), d(-20)))
            conn.execute("""INSERT INTO admission_episodes (patient_id,episode_no,status,admitted_at,room_number,ward,roster_key,onset_date)
                            VALUES (1,1,'admitted',?,'301호','3병동','c1|x',?)""", (d(-20), d(-45)))

    def test_ward_row_gets_roster_onset_and_judges_with_it(self):
        from views.ward import _ward_admitted_roster
        row = _ward_admitted_roster("", "")[0]
        self.assertEqual(row["disease_onset"], d(-45))
        self.assertTrue(row.get("onset_from_roster"))
        self.assertEqual(row["care_phase"], "회복기")
        # 저장값은 그대로 비어 있다
        self.assertIsNone(models.get_consultation(1)["disease_onset"])

    def test_recovery_status_accepts_display_onset(self):
        c = {"diseases": ["뇌출혈"], "actual_admission_date": d(-20), "admission_purpose": "회복기재활",
             "disease_onset": None, "onset_display": d(-45)}
        self.assertEqual(_recovery_status(c)["label"], "회복기")
        self.assertEqual(_care_phase(dict(c))["care_phase"], "회복기")

    def test_dashboard_rows_show_roster_onset(self):
        summary = models.dashboard_summary(d(-30), d(0))
        rows = [r for r in summary["admission_selected"] if r.get("patient_name") == "명부발병"]
        self.assertTrue(rows, "입원완료 행이 조회 기간에 있어야 한다")
        self.assertEqual(rows[0]["disease_onset"], d(-45))
        self.assertTrue(rows[0].get("onset_from_roster"))
        html = self.client.get(f"/?admission_scope=all&admission_from={d(-30)}&admission_to={d(0)}").get_data(as_text=True)
        self.assertIn(d(-45), html)


if __name__ == "__main__":
    unittest.main()
