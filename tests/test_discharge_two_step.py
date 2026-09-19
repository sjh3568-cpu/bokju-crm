"""재원 퇴원 2단계 — ① 퇴원 예정일 기입 ② 최종 퇴원 시 재원 제외 (2026-09-19 요청).

전에는 재원 명단에서 바로 최종 퇴원만 됐다. 예정일을 잡아 두는 단계가 없어, 나갈 날이 정해진
환자를 표시할 방법이 화면에 없었다. 예정 단계에서는 재원에 그대로 남고 '퇴원 예정' 칸에 D-day가 뜬다.
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


def d(n):
    return (date.today() + timedelta(days=n)).isoformat()


class DischargeTwoStepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "two.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("two-test", "test-password", display_name="점검")
        self.uid = models.get_user("two-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="two-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        with models.get_db() as conn:
            conn.execute("INSERT INTO patients (id,name,gender) VALUES (1,'퇴원예정환자','F')")
            conn.execute("""INSERT INTO consultations (id,patient_id,consult_date,admission_status,
                            actual_admission_date,room_number,attending_doctor)
                            VALUES (1,1,?, '입원완료', ?, '301호','RM1 이성범 부장')""", (d(-100), d(-90)))
            conn.execute("""INSERT INTO admission_episodes (patient_id,consultation_id,episode_no,status,
                            admitted_at,room_number,ward,roster_key)
                            VALUES (1,1,1,'admitted',?, '301호','3병동','c1|x')""", (d(-90),))

    def _roster_html(self):
        return self.client.get("/ward?partial=roster&view=list").get_data(as_text=True)

    def test_step1_sets_a_due_date_and_keeps_the_patient_in_census(self):
        r = self.client.post("/api/consult/1/discharge",
                             json={"action": "extend", "discharge_due_date": d(3)})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(models.get_consultation(1)["discharge_due_date"], d(3))
        self.assertIn(1, models.current_admission_census()["patients"], "예정 단계에선 재원에 남는다")
        self.assertIsNone(models.get_consultation(1)["discharge_date"])
        self.assertEqual(models.get_consultation(1)["admission_status"], "입원완료")

    def test_step2_final_discharge_removes_from_census(self):
        self.client.post("/api/consult/1/discharge", json={"action": "extend", "discharge_due_date": d(3)})
        r = self.client.post("/api/consult/1/discharge",
                             json={"action": "complete", "discharge_date": d(0),
                                   "discharge_destination": "자택", "discharge_reason": "치료 종료"})
        self.assertEqual(r.status_code, 200, r.get_json())
        con = models.get_consultation(1)
        self.assertEqual((con["admission_status"], con["discharge_date"]), ("퇴원완료", d(0)))
        self.assertNotIn(1, models.current_admission_census()["patients"], "최종 퇴원하면 재원에서 빠진다")

    def test_roster_offers_both_buttons_and_prefills_the_due_date(self):
        html = self._roster_html()
        self.assertIn('class="btn btn-secondary btn-xs wd-dplan"', html)
        self.assertIn("퇴원 예정", html)
        self.assertIn("재원에서 빠집니다", html)          # 최종 퇴원 폼의 안내
        self.client.post("/api/consult/1/discharge", json={"action": "extend", "discharge_due_date": d(3)})
        html = self._roster_html()
        self.assertIn(">퇴원예정</button>", html)          # 잡혀 있든 아니든 같은 문구
        self.assertIn(f'class="wd-dis-date" value="{d(3)}"', html)   # 퇴원 폼 기본값 = 예정일

    def test_due_date_shows_as_dday_in_the_roster(self):
        self.client.post("/api/consult/1/discharge", json={"action": "extend", "discharge_due_date": d(3)})
        html = self._roster_html()
        self.assertIn("D-3", html)


if __name__ == "__main__":
    unittest.main()
