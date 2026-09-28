"""외진일 앞당기기 — 2026-09-28 사용자: 외진을 늦게 알고 등록하면 외진일이 등록한 날로 남아,
그보다 앞선 실제 복귀일을 달력에서 고를 수 없었다. 확인(move_away_date)을 받으면 외진일도 옮긴다."""
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


class AwayMoveDateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "move.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("move-test", "test-password", display_name="점검")
        self.uid = models.get_user("move-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="move-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        with models.get_db() as conn:
            conn.execute("INSERT INTO patients (id,name,gender) VALUES (1,'테스트','F')")
            conn.execute("""INSERT INTO consultations (id, patient_id, consult_date, admission_status, actual_admission_date,
                            attending_doctor, room_number, patient_age)
                            VALUES (1, 1, ?, '입원완료', ?, 'RM1 이성범 부장', '510호', 74)""", (d(-100), d(-30)))
            # 실제로는 열흘 전에 나가 닷새 전에 돌아왔는데, 이틀 전에야 알고 등록해 외진일이 그날로 남은 상태
            conn.execute("""INSERT INTO admission_events (id, consultation_id, event_type, event_date, hospital)
                            VALUES (1, 1, '응급전원', ?, '안동병원')""", (d(-2),))

    def _post(self, path, body):
        return self.client.post(f"/api/admission-event/1/{path}", json=body)

    def test_return_before_away_date_needs_confirmation(self):
        r = self._post("return", {"return_date": d(-5)})
        self.assertEqual(r.status_code, 400)
        self.assertIn("빠릅니다", r.get_json()["error"])
        self.assertIsNone(models.get_admission_event(1)["returned_at"])

    def test_return_moves_away_date_back_when_confirmed(self):
        r = self._post("return", {"return_date": d(-5), "move_away_date": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        ev = models.get_admission_event(1)
        self.assertEqual((ev["event_date"][:10], ev["returned_at"][:10], ev["hospital"]), (d(-5), d(-5), "안동병원"))

    def test_expected_return_moves_away_date_back_when_confirmed(self):
        r = self._post("expected-return", {"expected_return_date": d(-6), "move_away_date": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        ev = models.get_admission_event(1)
        self.assertEqual((ev["event_date"][:10], ev["expected_return_date"][:10]), (d(-6), d(-6)))

    def test_never_before_admission(self):
        r = self._post("return", {"return_date": d(-40), "move_away_date": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("입원일", r.get_json()["error"])
        self.assertEqual(models.get_admission_event(1)["event_date"][:10], d(-2))

    def test_fix_completed_return_date_moves_away_date(self):
        self.assertEqual(self._post("return", {}).status_code, 200)       # 오늘 [완료]
        r = self._post("return-date", {"return_date": d(-7), "move_away_date": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        ev = models.get_admission_event(1)
        self.assertEqual((ev["event_date"][:10], ev["returned_at"][:10]), (d(-7), d(-7)))

    def test_ward_inputs_have_no_event_date_min(self):
        html = self.client.get("/ward?tab=away").get_data(as_text=True)
        self.assertIn(f'data-event-date="{d(-2)}"', html)
        self.assertNotIn(f'min="{d(-2)}"', html)

    def test_away_date_inputs_default_today(self):
        # 외진일 기본값은 오늘 — v1.9.34에서 비웠다가 같은 날 사용자 결정으로 되돌림(2026-09-28).
        # 미래 날짜는 여전히 막는다(max=오늘).
        html = self.client.get("/ward?tab=away").get_data(as_text=True)
        self.assertIn(f'<input type="date" name="event_date" value="{d(0)}" max="{d(0)}" required', html)


if __name__ == "__main__":
    unittest.main()
