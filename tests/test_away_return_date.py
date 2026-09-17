"""완료된 외진 복귀의 날짜 수정 — 오경자 님(2026-09-17): 9/16에 돌아왔는데 [완료]가 오늘(9/17)로 찍혔다."""
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


class AwayReturnDateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "ret.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("ret-test", "test-password", display_name="점검")
        self.uid = models.get_user("ret-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="ret-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        with models.get_db() as conn:
            conn.execute("INSERT INTO patients (id,name,gender) VALUES (1,'오경자','F')")
            conn.execute("""INSERT INTO consultations (id, patient_id, consult_date, admission_status, actual_admission_date,
                            attending_doctor, room_number, patient_age)
                            VALUES (1, 1, ?, '입원완료', ?, 'RM1 이성범 부장', '510호', 74)""", (d(-100), d(-1)))
            # 8/29 응급전원 → [완료]를 오늘 눌러 오늘 복귀로 찍힌 상태
            conn.execute("""INSERT INTO admission_events (id, consultation_id, event_type, event_date, hospital, returned_at, return_outcome)
                            VALUES (1, 1, '응급전원', ?, '안동병원', ?, '복귀')""", (d(-20), d(0)))
            # 비교용: 타 병원 전원으로 종결된 기록
            conn.execute("""INSERT INTO admission_events (id, consultation_id, event_type, event_date, hospital, returned_at, return_outcome, return_hospital)
                            VALUES (2, 1, '응급전원', ?, '안동병원', ?, '전원', '서울병원')""", (d(-60), d(-50)))

    def _fix(self, eid, when):
        return self.client.post(f"/api/admission-event/{eid}/return-date", json={"return_date": when})

    def test_fix_return_date_to_yesterday(self):
        r = self._fix(1, d(-1))
        self.assertEqual(r.status_code, 200, r.get_json())
        ev = models.get_admission_event(1)
        self.assertEqual((ev["returned_at"][:10], ev["return_outcome"]), (d(-1), "복귀"))
        # 대시보드: 어제 복귀 한 줄(상담 완료 행은 같은 날이라 합쳐짐), 오늘 행 없음
        rows = [x for x in models.dashboard_summary(d(-1), d(1))["admission_selected"] if x["patient_id"] == 1]   # 조회 기간(어제 포함) 행
        self.assertEqual([(x.get("admission_kind"), x["admission_bucket"], x["admission_display_date"]) for x in rows],
                         [("return", "completed", d(-1))])

    def test_rejects_future_before_event_transfer_and_open(self):
        self.assertIn("미래", self._fix(1, d(1)).get_json()["error"])
        self.assertIn("빠릅니다", self._fix(1, d(-30)).get_json()["error"])
        self.assertIn("전원", self._fix(2, d(-49)).get_json()["error"])
        with models.get_db() as conn:
            conn.execute("UPDATE admission_events SET returned_at=NULL, return_outcome=NULL WHERE id=1")
        self.assertIn("아직 복귀 처리되지", self._fix(1, d(-1)).get_json()["error"])
        self.assertEqual(self._fix(1, "2026-13-40").status_code, 400)

    def test_away_list_shows_fix_button_for_completed_returns_only(self):
        html = self.client.get("/ward?tab=away").get_data(as_text=True)
        self.assertIn('class="btn btn-secondary btn-xs away-fix-return" data-aevent="1"', html)
        self.assertNotIn('away-fix-return" data-aevent="2"', html)    # 전원 기록엔 없다


if __name__ == "__main__":
    unittest.main()
