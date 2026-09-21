"""주간 상담 현황 — 주를 골라 볼 수 있고 기본은 지난주(월~일) (2026-09-21 요청).
집계는 dashboard_summary 에서 떼어낸 models.weekly_report() 하나만 쓴다."""
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import app as main
import models


class WeeklyReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 'test.db'))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        models.ensure_admin_user('wk', 'test-only-password', display_name='테스트')
        user = models.get_user('wk')
        boot = patch.object(main, '_db_initialized', True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as s:
            s.update(user_id=user['id'], username='wk', role='admin', perms={k: 3 for k in main.MENU_KEYS})
        self.today = date.today()
        self.this_mon = self.today - timedelta(days=self.today.weekday())
        self.last_mon = self.this_mon - timedelta(days=7)
        # 지난주 화요일 1건(입원완료), 이번 주 월요일 1건(상담완료)
        for day, status in ((self.last_mon + timedelta(days=1), '입원완료'), (self.this_mon, '상담완료')):
            pid = models.find_or_create_patient(name=f"주간{day}", guardian_phone=None)
            models.create_consultation(patient_id=pid, consult_date=day.isoformat(), counselor='테스트',
                                       consult_channel='전화상담', admission_status=status)

    def test_default_is_last_week_monday_to_sunday(self):
        r = models.weekly_report()
        self.assertEqual(r['week_start'], self.last_mon.isoformat())
        self.assertEqual(r['week_end'], (self.last_mon + timedelta(days=6)).isoformat())
        self.assertEqual(date.fromisoformat(r['week_start']).weekday(), 0)          # 월요일
        self.assertEqual(r['current']['totals']['total'], 1)                           # 지난주 것만
        self.assertEqual(r['current']['totals']['admitted'], 1)
        self.assertEqual(len(r['current']['days']), 7)

    def test_any_weekday_snaps_to_that_weeks_monday(self):
        thursday = self.last_mon + timedelta(days=3)
        r = models.weekly_report(thursday)
        self.assertEqual(r['week_start'], self.last_mon.isoformat())

    def test_page_picker_and_this_week_marked_in_progress(self):
        html = self.client.get('/report/weekly').get_data(as_text=True)
        self.assertIn('class="week-picker', html)
        self.assertIn(f'value="{self.last_mon.isoformat()}"', html)                   # 기본 선택 = 지난주
        self.assertIn(f'{self.last_mon.isoformat()} ~ {(self.last_mon + timedelta(days=6)).isoformat()}', html)
        cur = self.client.get(f'/report/weekly?week={self.this_mon.isoformat()}').get_data(as_text=True)
        self.assertIn('<b>진행 중</b>', cur)                                            # 이번 주는 진행 중 표시
        bad = self.client.get('/report/weekly?week=abc')
        self.assertEqual(bad.status_code, 200)                                          # 잘못된 값은 기본으로
        self.assertIn(f'value="{self.last_mon.isoformat()}"', bad.get_data(as_text=True))

    def test_dashboard_no_longer_computes_weekly_report(self):
        """대시보드가 열릴 때마다 1년치 상담을 읽던 낭비를 없앴다 — 키 자체가 없어야 한다."""
        self.assertNotIn('weekly_report', models.dashboard_summary())


if __name__ == '__main__':
    unittest.main()
