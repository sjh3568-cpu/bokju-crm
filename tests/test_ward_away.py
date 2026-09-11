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

    def test_list_and_feed_views_number_rows(self):
        """목록형이 기본이고, 피드형은 날짜별로 묶는다. 연번은 두 보기에서 같다."""
        html = self.client.get('/ward?tab=away').get_data(as_text=True)
        self.assertIn('class="on">☰ 목록형', html)
        self.assertIn('<td class="away-col-no" data-label="#">1</td>', html)
        self.assertIn('일째', html)          # 미복귀 기록의 '며칠째' 배지
        self.assertIn('번째 외진', html)     # 차수 표시
        self.assertIn('data-detail-active="false"', html)
        feed = self.client.get('/ward?tab=away&away_view=feed').get_data(as_text=True)
        self.assertIn('class="on">▤ 피드형', feed)
        self.assertIn('away-feed-date', feed)
        self.assertIn('<div class="away-card-no">1</div>', feed)
        self.assertNotIn('away-table', feed.split('<style>')[0])
        # 상세필터에 값이 있으면 펼친 채로 연다
        html = self.client.get('/ward?tab=away&away_gender=F').get_data(as_text=True)
        self.assertIn('data-detail-active="true"', html)
        with main.app.test_request_context('/ward?tab=away&away_view=bogus'):
            self.assertEqual(main._ward_away_report()['view'], 'list')

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

    def test_return_saves_room_note_and_moves_current_room(self):
        models.sync_admission_episode(1)
        response = self.client.post('/api/admission-event/2/return', json={
            'return_date': '2026-03-05', 'return_room': '301호', 'return_note': '산소 유지 필요'})
        self.assertEqual(response.status_code, 200)
        event = models.get_admission_event(2)
        self.assertEqual((event['return_room'], event['return_note']), ('301호', '산소 유지 필요'))
        # 기록만이 아니라 현재 병실도 따라가야 병상 화면이 어긋나지 않는다
        self.assertEqual(models.get_consultation(1)['room_number'], '301호')
        with models.get_db() as conn:
            episode = conn.execute(
                'SELECT room_number FROM admission_episodes WHERE consultation_id = 1').fetchone()
        self.assertEqual(episode['room_number'], '301호')
        row = [r for r in models.list_away_records() if r['away_id'] == 2][0]
        self.assertEqual(row['return_room'], '301호')

    def test_return_without_room_keeps_current_room(self):
        with models.get_db() as conn:
            conn.execute("UPDATE consultations SET room_number='205호' WHERE id=1")
        self.assertEqual(self.client.post('/api/admission-event/2/return',
                                          json={'return_date': '2026-03-05'}).status_code, 200)
        self.assertEqual(models.get_consultation(1)['room_number'], '205호')

    def test_return_rejects_overlong_room_and_note(self):
        for payload in ({'return_room': '방' * 51}, {'return_note': '가' * 3001}):
            self.assertEqual(self.client.post('/api/admission-event/2/return',
                                              json=payload).status_code, 400)
        self.assertIsNone(models.get_admission_event(2)['returned_at'])

    def test_details_saves_birth_date_and_readmission(self):
        self.login(1)
        self.assertEqual(self.client.post('/api/admission-event/2/details',
                                          json={'readmission': '예'}).status_code, 403)
        self.login(2)
        response = self.client.post('/api/admission-event/2/details',
                                    json={'birth_date': '1955-04-02', 'readmission': '예'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(models.get_admission_event(2)['readmission'], '예')
        # 생년월일은 환자에 저장되므로 같은 환자의 다른 외진 기록에도 보인다
        rows = {r['away_id']: r for r in models.list_away_records()}
        self.assertEqual(rows[2]['birth_date'], '1955-04-02')
        self.assertEqual(rows[1]['birth_date'], '1955-04-02')
        self.assertIsNone(rows[1]['readmission'])

    def test_details_rejects_bad_values_and_non_away_event(self):
        future = (date.today() + timedelta(days=1)).isoformat()
        for payload in ({'birth_date': future}, {'birth_date': '55-4-2'}, {'readmission': '아마도'}):
            self.assertEqual(self.client.post('/api/admission-event/2/details',
                                              json=payload).status_code, 400)
        # 5번은 '기타' 이벤트 — 외진 명부 항목이 아니다
        self.assertEqual(self.client.post('/api/admission-event/5/details',
                                          json={'readmission': '예'}).status_code, 404)
        self.assertIsNone(models.get_patient(1)['birth_date'])

    def test_lifetime_numbering_survives_filters_and_readmission(self):
        with models.get_db() as conn:
            conn.execute("INSERT INTO consultations (id, patient_id, consult_date) VALUES (3, 1, '2026-04-01')")
            conn.execute("INSERT INTO admission_events (consultation_id,event_type,event_date) VALUES (3,'모병원 외래치료','2026-04-03')")
            conn.execute("INSERT INTO admission_events (consultation_id,event_type) VALUES (3,'응급전원')")
        rows = models.list_away_records(date_from='2026-03-01', event_type='응급전원')
        self.assertEqual([(r['away_id'], r['away_number']) for r in rows], [(2, 2)])
        rows = models.list_away_records(date_from='2026-04-01')
        self.assertEqual(rows[0]['away_number'], 3)
        missing = [r for r in models.list_away_records() if not r['event_date']]
        self.assertIsNone(missing[0]['away_number'])

    def test_inclusive_days_and_roster_filters(self):
        with main.app.test_request_context('/ward?tab=away&away_number=2&away_age_min=70&away_age_max=70&away_gender=F&away_hospital=기관&away_dx=병명&away_days_min=2'):
            report = main._ward_away_report()
        self.assertEqual({r['away_id'] for r in report['rows']}, {2, 3})
        self.assertEqual(report['stats']['events'], 4)
        row = next(r for r in report['rows'] if r['away_id'] == 2)
        self.assertEqual(row['stay_days_inclusive'], (date.today() - date(2026, 1, 1)).days + 1)
        self.assertEqual(row['away_days_inclusive'], (date.today() - date(2026, 3, 1)).days + 1)
        with models.get_db() as conn:
            conn.execute("UPDATE consultations SET discharge_date='2026-03-10' WHERE id=2")
        with main.app.test_request_context('/ward?tab=away'):
            rows = main._ward_away_report()['rows']
        returned = next(r for r in rows if r['away_id'] == 4)
        self.assertEqual(returned['away_days_inclusive'], 1)
        self.assertEqual(returned['stay_days_inclusive'], 69)
        for query in ('away_gender=M', 'away_age_min=71', 'away_age_max=69', 'away_hospital=없는기관',
                      'away_dx=없는병명', 'away_number=9', 'away_days_min=99999', 'away_dday_max=-99999'):
            with main.app.test_request_context('/ward?tab=away&' + query):
                self.assertEqual(main._ward_away_report()['rows'], [], query)
        for query in ('away_number=0', 'away_age_min=-1', 'away_age_min=80&away_age_max=70',
                      'away_days_min=oops', 'away_gender=bad', 'away_dday_max=1.5'):
            self.assertEqual(self.client.get('/ward?tab=away&' + query).status_code, 400, query)


if __name__ == '__main__':
    unittest.main()
