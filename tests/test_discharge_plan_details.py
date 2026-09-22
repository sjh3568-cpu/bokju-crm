"""퇴원 예정 단계에서 행선지·사유도 받는다 (2026-09-22 요청).

전에는 예정 단계가 날짜만 물었다(prompt). 나갈 곳은 예정을 잡을 때 이미 정해져 있는 일이 많고,
대시보드 '퇴원예정' 행의 행선 칸은 이 값을 읽도록 이미 만들어져 있었는데 채우는 곳이 없었다.
최종 퇴원 때 같은 내용을 다시 적지 않도록 퇴원 폼도 이 값으로 채워 둔다.
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


class DischargePlanDetailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "plan.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("plan-test", "test-password", display_name="점검")
        self.uid = models.get_user("plan-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="plan-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        with models.get_db() as conn:
            conn.execute("INSERT INTO patients (id,name,gender) VALUES (1,'퇴원예정자','F')")
            conn.execute("""INSERT INTO consultations (id,patient_id,consult_date,admission_status,
                            actual_admission_date,room_number,attending_doctor)
                            VALUES (1,1,?,'입원완료',?,'301호','RM1 이성범 부장')""", (d(-100), d(-60)))
            conn.execute("""INSERT INTO admission_episodes (patient_id,consultation_id,episode_no,status,
                            admitted_at,room_number,ward,roster_key)
                            VALUES (1,1,1,'admitted',?,'301호','3병동','c1|x')""", (d(-60),))

    def _plan(self, **kw):
        body = {"action": "extend", "discharge_due_date": d(3)}
        body.update(kw)
        return self.client.post("/api/consult/1/discharge", json=body)

    def test_plan_saves_destination_and_reason_without_discharging(self):
        r = self._plan(discharge_destination="안동요양병원", discharge_reason="요양 전원")
        self.assertEqual(r.status_code, 200, r.get_json())
        con = models.get_consultation(1)
        self.assertEqual((con["discharge_due_date"], con["discharge_destination"], con["discharge_reason"]),
                         (d(3), "안동요양병원", "요양 전원"))
        self.assertIsNone(con["discharge_date"])
        self.assertEqual(con["admission_status"], "입원완료")
        self.assertIn(1, models.current_admission_census()["patients"], "예정 단계에선 재원에 남는다")

    def test_plan_without_details_keeps_what_was_there(self):
        """행선지를 적어 둔 뒤 날짜만 고칠 때 빈 칸으로 지워지면 안 된다."""
        self._plan(discharge_destination="자택", discharge_reason="치료 종료")
        r = self._plan(discharge_due_date=d(5), discharge_destination="", discharge_reason="")
        self.assertEqual(r.status_code, 200, r.get_json())
        con = models.get_consultation(1)
        self.assertEqual((con["discharge_due_date"], con["discharge_destination"]), (d(5), "자택"))
        self.assertEqual(con["discharge_reason"], "치료 종료")

    def test_roster_offers_the_plan_form_prefilled(self):
        """명단은 행 아래 폼에, 병실 카드는 data 속성에 기존 값을 실어 둔다(카드 폼은 ward.html 템플릿에서 복제)."""
        self._plan(discharge_destination="안동요양병원", discharge_reason="요양 전원")
        html = self.client.get("/ward?partial=roster&view=list").get_data(as_text=True)
        self.assertIn('class="wd-plan-destination"', html)
        self.assertIn('class="wd-plan-reason"', html)
        self.assertIn('value="안동요양병원"', html)
        self.assertIn('value="요양 전원"', html)
        card = self.client.get("/ward?partial=roster&view=room").get_data(as_text=True)
        self.assertIn('data-dest="안동요양병원"', card)
        self.assertIn('data-dreason="요양 전원"', card)
        page = self.client.get("/ward").get_data(as_text=True)
        self.assertIn("wd-plan-save", page)          # 카드가 복제해 쓰는 편집 폼 템플릿

    def test_dashboard_shows_the_destination_on_the_planned_row(self):
        """대시보드 '퇴원예정' 행의 행선 칸('모병원·행선')이 예정 단계에서 적은 값을 그대로 읽는다."""
        self._plan(discharge_destination="안동요양병원", discharge_reason="요양 전원")
        summary = models.dashboard_summary(d(0), d(7))
        rows = [e for e in summary["admission_selected"]
                if e.get("patient_name") == "퇴원예정자" and e.get("admission_bucket") == "discharge_planned"]
        self.assertTrue(rows, "퇴원예정 행이 나와야 한다")
        self.assertEqual(rows[0]["other_note"], "안동요양병원")
        self.assertIn("행선 안동요양병원", rows[0]["other_note_title"])


if __name__ == "__main__":
    unittest.main()
