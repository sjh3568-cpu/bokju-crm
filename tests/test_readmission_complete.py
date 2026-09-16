"""재입원 환자를 [처리]→완료로 눌렀을 때 오늘 입원으로 잡히는지.

김한진 님 사례(2026-09-15): 8/25 입원 → 9/9 퇴원 → 오늘 재입원. 상담의
actual_admission_date에 첫 입원일 8/25가 남아 있는데, 완료 처리가
admission_date만 채우는 바람에 화면·집계가 보는
COALESCE(actual_admission_date, admission_date)는 계속 8/25였다.
그래서 입원 환자 현황(이번 주)과 '오늘 완료'에서 사라졌다.
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


class ReadmissionCompleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "readm.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("readm-test", "test-password", display_name="점검")
        self.uid = models.get_user("readm-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="readm-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 2 for k in main.MENU_KEYS})

        self.today = date.today()
        self.today_iso = self.today.isoformat()
        # 첫 입원 3주 전, 퇴원 1주 전 → 오늘 재입원 예정
        self.first_admit = (self.today - timedelta(days=21)).isoformat()
        with models.get_db() as conn:
            conn.execute("INSERT INTO patients (id,name,gender) VALUES (1,'재입원환자','M')")
            conn.execute(
                """INSERT INTO consultations
                   (id, patient_id, consult_date, admission_status,
                    planned_admission_date, planned_admission_time,
                    actual_admission_date, room_number, patient_age, primary_diagnosis)
                   VALUES (1, 1, ?, '입원예정', ?, '15:00', ?, '204', 77, '뇌경색증')""",
                ((self.today - timedelta(days=30)).isoformat(), self.today_iso, self.first_admit))

    def _complete_today(self):
        return self.client.post("/api/consult/1/status", json={
            "admission_status": "입원완료", "admission_date": self.today_iso})

    def test_complete_overwrites_stale_actual_admission_date(self):
        res = self._complete_today()
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        row = models.get_consultation(1)
        self.assertEqual(row["admission_status"], "입원완료")
        # 두 칸 모두 오늘 — 옛 입원일(3주 전)이 남아 있으면 안 된다
        self.assertEqual(row["admission_date"], self.today_iso)
        self.assertEqual(row["actual_admission_date"], self.today_iso)

    def test_dashboard_counts_it_as_completed_today(self):
        self._complete_today()
        summary = models.dashboard_summary()
        rows = [r for r in summary["admission_schedule"] if r["id"] == 1]
        self.assertEqual(len(rows), 1, "입원 환자 현황에서 사라지면 안 된다")
        self.assertEqual(rows[0]["admission_display_date"], self.today_iso)
        self.assertEqual(rows[0]["admission_bucket"], "completed")
        self.assertEqual(summary["summary"]["admission_today_completed"], 1)

    def test_episode_admitted_at_follows_today(self):
        self._complete_today()
        with models.get_db() as conn:
            ep = conn.execute(
                "SELECT admitted_at FROM admission_episodes WHERE consultation_id=1").fetchone()
        self.assertIsNotNone(ep)
        self.assertEqual(ep["admitted_at"], self.today_iso)


if __name__ == "__main__":
    unittest.main()
