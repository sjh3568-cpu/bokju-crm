import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import app as main
import models


class AwayManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 'test.db'))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        models.ensure_admin_user('away-test', 'test-password', display_name='테스트')
        self.uid = models.get_user('away-test')['id']
        boot = patch.object(main, '_db_initialized', True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        self.login(2)
        with models.get_db() as conn:
            for pid in (1, 2):
                conn.execute('INSERT INTO patients (id, name, gender) VALUES (?, ?, ?)',
                             (pid, f'테스트환자{pid}', 'F'))
                conn.execute('''INSERT INTO consultations
                    (id, patient_id, consult_date, admission_status, actual_admission_date,
                     patient_age, primary_diagnosis, discharge_due_date)
                    VALUES (?, ?, '2026-01-01', '입원완료', '2026-01-01', 70, '테스트병명', '2026-12-31')''',
                             (pid, pid))
            for cid, kind, day, returned in [
                (1, '응급전원', '2026-02-01', '2026-02-05'),
                (1, '응급전원', '2026-03-01', None),
                (2, '응급전원', '2026-02-03', None),
                (2, '모병원 외래치료', '2026-01-10', '2026-01-10'),
                (2, '기타', '2026-01-11', None),
            ]:
                conn.execute('''INSERT INTO admission_events
                    (consultation_id, event_type, event_date, returned_at, hospital, memo)
                    VALUES (?, ?, ?, ?, '테스트기관', '<script>사유</script>')''',
                             (cid, kind, day, returned))

    def login(self, level):
        with self.client.session_transaction() as session:
            session.clear()
            session.update(user_id=self.uid, username='away-test', display_name='테스트',
                           role='staff', cooperation_permissions_v2=True,
                           perms={k: (level if k == 'ward' else 1) for k in main.MENU_KEYS})

    def test_unique_patients_repeat_transfers_and_discharged_history(self):
        with models.get_db() as conn:
            conn.execute("UPDATE consultations SET discharge_date='2026-03-10' WHERE id=2")
        rows = models.list_away_records(event_type='응급전원')
        stats = models.away_record_stats(rows)
        self.assertEqual((stats['patients'], stats['returned_patients'], stats['events']), (2, 1, 3))
        self.assertEqual(stats['open_patients'], 2)
        self.assertEqual(stats['patient_rate'], 50)
        self.assertEqual(stats['event_rate'], 33.3)
        self.assertEqual(len(models.list_away_records(date_from='2026-02-01', date_to='2026-02-28')), 2)
        self.assertEqual(models.away_record_stats([])['patient_rate'], 0)

    def test_tab_render_filters_and_monthly_cohort(self):
        page = self.client.get('/ward?tab=away')
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        for label in ('외진환자관리', '퇴원일(전원일)', '테스트병명', '복귀 저장', '70', '&lt;script&gt;'):
            self.assertIn(label, html)
        self.assertNotIn('<script>사유</script>', html)
        with main.app.test_request_context('/ward?tab=away&away_status=returned&away_q=환자1'):
            report = main._ward_away_report()
            self.assertEqual(len(report['rows']), 1)
            self.assertEqual(report['stats']['events'], 4)
            self.assertEqual(report['transfers']['returned_patients'], 1)
            self.assertEqual(dict(report['monthly'])['2026-02']['patients'], 2)
        for query in ('away_from=bad', 'away_from=2026-03-01&away_to=2026-02-01', 'away_type=invalid'):
            self.assertEqual(self.client.get('/ward?tab=away&' + query).status_code, 400)

    def test_return_persistence_validation_and_permissions(self):
        url = '/api/admission-event/2/return'
        self.login(1)
        html = self.client.get('/ward?tab=away').get_data(as_text=True)
        self.assertNotIn('id="away-register"', html)
        self.assertNotIn('복귀 저장', html)
        self.assertEqual(self.client.post(url, json={}).status_code, 403)
        self.login(0)
        self.assertEqual(self.client.get('/ward?tab=away').status_code, 403)
        self.login(2)
        for day in ('2026-02-01', (date.today() + timedelta(days=1)).isoformat(), 'bad'):
            self.assertEqual(self.client.post(url, json={'return_date': day}).status_code, 400)
        self.assertEqual(self.client.post('/api/admission-event/5/return', json={}).status_code, 400)
        self.assertEqual(self.client.post(url, json={'return_date': '2026-03-05'}).status_code, 200)
        self.assertEqual(models.get_admission_event(2)['returned_at'], '2026-03-05')
        stats = models.away_record_stats(models.list_away_records(event_type='응급전원'))
        self.assertEqual((stats['returned_patients'], stats['returned_events']), (1, 2))
        self.assertEqual(self.client.post(url, json={}).status_code, 400)
        result = self.client.post('/api/consult/1/admission-event', json={
            'event_type': '응급전원', 'event_date': '2026-03-10', 'hospital': '병원', 'memo': '검사'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.client.post('/api/consult/1/admission-event', json={
            'event_type': '응급전원', 'event_date': '2026-03-11'}).status_code, 400)


if __name__ == '__main__':
    unittest.main()
