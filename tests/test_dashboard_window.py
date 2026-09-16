"""대시보드 입원 환자 현황 — 기본 기간 어제~내일, 복귀·상담 중복 제거, 구분 배지 (2026-09-16 요청)."""
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


class DashboardWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "win.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("win-test", "test-password", display_name="점검")
        self.uid = models.get_user("win-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="win-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        with models.get_db() as conn:
            for pid, name in ((1, "어제예정미처리"), (2, "오늘완료"), (3, "내일예정"), (4, "장광진복귀"), (5, "다음주예정")):
                conn.execute("INSERT INTO patients (id,name,gender) VALUES (?,?,'M')", (pid, name))
            rows = [  # id, pid, status, planned, actual
                (1, 1, "입원예정", d(-1), None),
                (2, 2, "입원완료", d(0), d(0)),
                (3, 3, "입원예정", d(1), None),
                (4, 4, "입원완료", d(0), d(0)),     # 외진 복귀한 날 상담도 입원완료로 처리된 케이스
                (5, 5, "입원예정", d(7), None),
            ]
            for cid, pid, st, planned, actual in rows:
                conn.execute("""INSERT INTO consultations (id, patient_id, consult_date, admission_status,
                                planned_admission_date, actual_admission_date, attending_doctor, room_number, patient_age)
                                VALUES (?,?,?,?,?,?,'RM1 이성범 부장','301호',70)""", (cid, pid, d(-20), st, planned, actual))
            # 4번: 20일 전 응급전원 → 오늘 복귀
            conn.execute("""INSERT INTO admission_events (consultation_id, event_type, event_date, hospital, returned_at, return_outcome, expected_return_date)
                            VALUES (4, '응급전원', ?, '안동병원', ?, '복귀', ?)""", (d(-20), d(0), d(0)))
        # 화면에서 저장했을 때처럼 회차(admission_episodes)도 만들어 둔다 — 재원·이번주 입원 집계가 회차를 본다
        for cid in (1, 2, 3, 4, 5):
            models.sync_admission_episode(cid)

    def test_default_window_is_yesterday_to_tomorrow_and_shows_all_three_days(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("어제~내일", html)
        for name in ("어제예정미처리", "오늘완료", "내일예정", "장광진복귀"):
            self.assertIn(name, html, name)
        self.assertNotIn("다음주예정", html)
        # 접기 없이 — 선택일 전체 표에 data-limit이 없다
        self.assertNotRegex(html, r'dash-adm-tbl"[^>]*data-limit=')

    def test_return_row_replaces_same_day_consultation_row(self):
        rows = [r for r in models.dashboard_summary(d(-1), d(1))["admission_schedule"] if r["patient_id"] == 4]
        self.assertEqual(len(rows), 1, "복귀 행 하나만 남아야 한다")
        self.assertEqual(rows[0]["admission_kind"], "return")

    def test_kind_badges(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('adm-kind-planned', html)   # 예정
        self.assertIn('adm-kind-done', html)      # 완료
        self.assertIn('adm-kind-return', html)    # 복귀
        self.assertIn('>복귀</span>', html)
        self.assertIn('>예정</span>', html)
        self.assertIn('>완료</span>', html)

    def test_week_in_counts_return_and_consultation_once(self):
        with main.app.test_request_context():
            strip = main._ward_status_strip()
        # 오늘완료(2) + 장광진복귀(4) = 2. 복귀와 같은 날 CRM 입원완료를 겹쳐 세지 않는다
        self.assertEqual(strip["week_in"], 2)


if __name__ == "__main__":
    unittest.main()
