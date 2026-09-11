"""대시보드 최상단 '현재 상태' 스트립(_ward_status_strip).

재원·회복기 비율은 /ward와 같은 근거(원무 명부 census + _care_phase)로 내야
두 화면 숫자가 어긋나지 않는다. 여기서 그 일치와, 이번주·이번달 입퇴원 카운트,
명부가 없을 때의 우아한 대체(has_roster=False)를 고정한다.
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


class DashboardStripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "strip.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        # 대시보드는 기관협력·개선요청 컨텍스트 프로세서를 거치므로 그 스키마도 필요하다.
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("strip-test", "test-password", display_name="점검")
        self.uid = models.get_user("strip-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="strip-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 2 for k in main.MENU_KEYS})

        self.today = date.today()
        old = (self.today - timedelta(days=60)).isoformat()   # 이번주·이번달 밖
        self.today_iso = self.today.isoformat()
        with models.get_db() as conn:
            for pid, name in ((1, "회복기재원"), (2, "비회복기재원"), (3, "오늘퇴원")):
                conn.execute("INSERT INTO patients (id,name,gender) VALUES (?,?,'F')", (pid, name))
            # 환자1 — 상담이 붙은 재원(오늘 입원). 명부 수가구분이 회복기.
            conn.execute(
                """INSERT INTO consultations
                   (id, patient_id, consult_date, admission_status, actual_admission_date,
                    patient_age, primary_diagnosis)
                   VALUES (1, 1, ?, '입원완료', ?, 70, '상세불명의 뇌경색증')""",
                (old, self.today_iso))
            # 회차 — 원무 명부(roster_key)가 재원·입퇴원 흐름의 사실
            rows = [
                # (pid, admitted, discharged, care_type)
                (1, self.today_iso, None, "회복기재활"),        # 오늘 입원 · 회복기 · 재원
                (2, old, None, "비회복기"),                     # 예전 입원 · 비회복기 · 재원(상담 없음=orphan)
                (3, old, self.today_iso, "회복기재활"),          # 오늘 퇴원 · 재원 아님
            ]
            for pid, admitted, discharged, care in rows:
                conn.execute(
                    """INSERT INTO admission_episodes
                       (patient_id, episode_no, status, admitted_at, discharged_at,
                        room_number, ward, care_type, roster_key)
                       VALUES (?, 1, ?, ?, ?, '301호', '3병동', ?, ?)""",
                    (pid, "discharged" if discharged else "admitted",
                     admitted, discharged, care, "chart%d|%s" % (pid, admitted)))

    def test_strip_matches_roster_census(self):
        with main.app.test_request_context():
            strip = main._ward_status_strip()
        self.assertTrue(strip["has_roster"])
        # 재원 2명(회복기1 + 비회복기1), 퇴원한 환자3은 빠진다
        self.assertEqual(strip["admitted"], 2)
        self.assertEqual(strip["recovery"], 1)
        self.assertEqual(strip["recovery_ratio"], 50)   # 1/2
        self.assertTrue(strip["recovery_ratio_ok"])     # 40% 이상
        self.assertEqual(strip["bed_occupancy"],
                         round(2 / main.WARD_BED_CAPACITY * 100, 1))

    def test_strip_flow_counts_this_week_and_month(self):
        with main.app.test_request_context():
            strip = main._ward_status_strip()
        # 오늘 입원 1건(환자1), 오늘 퇴원 1건(환자3) — 둘 다 이번주·이번달 안
        self.assertEqual(strip["week_in"], 1)
        self.assertEqual(strip["week_out"], 1)
        self.assertEqual(strip["month_in"], 1)
        self.assertEqual(strip["month_out"], 1)
        self.assertEqual(strip["away"], 0)

    def test_strip_ratio_agrees_with_ward_kpi_card(self):
        """스트립의 회복기 비율은 /ward KPI 카드와 같은 값이어야 한다."""
        with main.app.test_request_context():
            ratio = main._ward_status_strip()["recovery_ratio"]
        html = self.client.get("/ward").get_data(as_text=True)
        self.assertIn('<span class="wd-k-n">%d<small>%%</small></span>' % ratio, html)

    def test_strip_renders_on_dashboard(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("dash-status-strip", html)
        self.assertIn("회복기", html)

    def test_no_roster_hides_census_metrics(self):
        with models.get_db() as conn:
            conn.execute("DELETE FROM admission_episodes")
        with main.app.test_request_context():
            strip = main._ward_status_strip()
        self.assertFalse(strip["has_roster"])
        self.assertIsNone(strip["admitted"])
        self.assertIsNone(strip["recovery_ratio"])
        # 명부가 없어도 흐름·외진 지표는 0으로 계산되고 화면은 '명부 필요'를 띄운다
        self.assertEqual(strip["week_in"], 0)
        self.assertEqual(strip["month_out"], 0)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("명부 필요", html)


if __name__ == "__main__":
    unittest.main()
