"""대시보드 입·퇴원 현황의 퇴원예정 행에서 바로 퇴원 확정 (2026-09-21 요청).
전에는 '예정' 배지만 있어 재원 관리로 가야 했다. 드롭다운 → /api/consult/<id>/discharge complete →
상담 퇴원완료 + 명부 회차 닫힘 → 재원 census·재원 관리 명단에서 빠지고, 대시보드 행은 퇴원완료로 바뀐다."""
import os
import re
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import app as main
import models
import partnerships
import support_requests
import transport


class DashboardDischargeActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 'test.db'))
        db.start(); self.addCleanup(db.stop)
        models.init_db(); partnerships.init_schema(); support_requests.init_schema(); transport.init_schema()
        models.ensure_admin_user('dc', 'test-only-password', display_name='테스트')
        user = models.get_user('dc')
        boot = patch.object(main, '_db_initialized', True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as s:
            s.update(user_id=user['id'], username='dc', role='admin', perms={k: 3 for k in main.MENU_KEYS})
        self.today = date.today()
        adm = (self.today - timedelta(days=18)).isoformat()
        self.pid = models.find_or_create_patient(name="퇴원예정검증", guardian_phone="010-0000-0100")
        self.cid = models.create_consultation(
            patient_id=self.pid, consult_date=(self.today - timedelta(days=20)).isoformat(), counselor='테스트',
            admission_status='입원완료', actual_admission_date=adm, room_number='301호',
            discharge_due_date=self.today.isoformat())
        # 운영처럼 원무 명부 회차로 — 재원 census는 명부 회차를 기준으로 센다
        conn = models.get_db()
        with conn:
            conn.execute("UPDATE admission_episodes SET roster_key=?, ward='3병동' WHERE patient_id=?",
                         (f'c{self.pid}|x', self.pid))

    def _dashboard(self):
        return self.client.get('/').get_data(as_text=True)

    def test_planned_discharge_row_has_action_dropdown(self):
        html = self._dashboard()
        m = re.search(r'<select class="adm-action-sel" data-kind="discharge" data-cid="%d"[^>]*data-due="([^"]+)"' % self.cid, html)
        self.assertIsNotNone(m, "퇴원예정 행에 처리 드롭다운이 없다")
        self.assertEqual(m.group(1), self.today.isoformat())              # 예정일을 데이터로 들고 있어 '예정 변경' 기본값이 된다
        self.assertIn('<option value="퇴원완료">퇴원</option>', html)
        self.assertIn('<option value="예정변경">예정 변경</option>', html)
        self.assertNotIn('재원 관리에서 [퇴원]으로 확정합니다', html)      # 옛 안내 배지는 사라졌다

    def test_complete_from_dashboard_removes_from_census_and_roster(self):
        self.assertIn(self.cid, models.current_admission_census()["by_consultation"])
        r = self.client.post(f'/api/consult/{self.cid}/discharge',
                             json={'action': 'complete', 'discharge_date': self.today.isoformat()})
        self.assertEqual(r.status_code, 200, r.get_json())
        c = models.get_consultation(self.cid)
        self.assertEqual((c['admission_status'], c['discharge_date']), ('퇴원완료', self.today.isoformat()))
        ep = models.get_db().execute("SELECT status, discharged_at FROM admission_episodes WHERE consultation_id=?",
                                     (self.cid,)).fetchone()
        self.assertEqual((ep['status'], ep['discharged_at']), ('discharged', self.today.isoformat()))   # 명부 회차도 닫힘
        self.assertNotIn(self.cid, models.current_admission_census()["by_consultation"])             # 재원에서 빠짐
        roster = self.client.get('/ward?partial=roster&view=list').get_data(as_text=True)
        self.assertNotIn('퇴원예정검증', roster)                                                       # 재원 관리 명단에서도
        html = self._dashboard()
        self.assertIsNone(re.search(r'data-kind="discharge" data-cid="%d"' % self.cid, html))       # 드롭다운은 사라지고
        self.assertRegex(html, r'title="퇴원완료 [^"]*"')                                             # 퇴원완료 행으로

    def test_extend_keeps_patient_in_census(self):
        new_due = (self.today + timedelta(days=3)).isoformat()
        r = self.client.post(f'/api/consult/{self.cid}/discharge', json={'action': 'extend', 'discharge_due_date': new_due})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(models.get_consultation(self.cid)['discharge_due_date'], new_due)
        self.assertIn(self.cid, models.current_admission_census()["by_consultation"])                # 예정 변경은 재원 유지


if __name__ == '__main__':
    unittest.main()
