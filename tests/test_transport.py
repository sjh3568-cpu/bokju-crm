"""차량 운행(픽업) 요청 — 시트 호출은 가짜로 바꾸고 저장·전송 상태·경고·화면을 검증한다."""
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import app as main
import models
import transport


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 't.db')); self.db_patch.start()
        models.init_db(); transport.init_schema()
        self.tomorrow = (date.today() + timedelta(days=1)).isoformat()
        db = models.get_db()
        pid = db.execute("INSERT INTO patients(name, guardian_relation, guardian_phone) VALUES ('한영도','형님','010-6378-2866')").lastrowid
        self.cid = db.execute("""INSERT INTO consultations(patient_id,consult_date,admission_status,source_hospital,planned_admission_date)
                                 VALUES (?,?,'입원예정','안동병원',?)""", (pid, date.today().isoformat(), self.tomorrow)).lastrowid
        db.commit(); db.close()
        models.ensure_admin_user('t', 'x', display_name='박세연'); u = models.get_user('t')
        self.ctx = [patch.object(main, '_db_initialized', True), patch.object(models, 'first_unread_required_announcement', return_value=None)]
        for c in self.ctx: c.start()
        main.app.config.update(TESTING=True); self.c = main.app.test_client()
        with self.c.session_transaction() as s:
            s.update(user_id=u['id'], username='t', display_name='박세연', role='admin', perms={k: 3 for k in main.MENU_KEYS})

    def tearDown(self):
        for c in self.ctx: c.stop()
        self.db_patch.stop(); self.tmp.cleanup()

    def _save(self, **kw):
        body = {'needed': 'yes', 'mobility': 'W/C', 'place': '안동병원 814호', 'arrive_time': '14:00', 'contact': '형님 010-6378-2866(회복)'}
        body.update(kw)
        return self.c.post(f'/api/consult/{self.cid}/transport', json=body)

    def test_undecided_then_skip_clears_alert(self):
        self.assertEqual([a['detail'][:14] for a in transport.dashboard_alerts()], ['차량 운행 필요 여부 미정'])
        r = self._save(needed='no')
        self.assertEqual(r.status_code, 200); self.assertEqual(r.get_json()['request']['sheet_status'], 'skip')
        self.assertEqual(transport.dashboard_alerts(), [])

    def test_required_fields(self):
        r = self._save(place='')
        self.assertEqual(r.status_code, 400); self.assertIn('요청 장소', r.get_json()['error'])

    def test_save_without_sheet_keeps_draft(self):
        with patch.dict(os.environ, {'TRANSPORT_SHEET_URL': ''}):
            r = self._save()
        j = r.get_json()
        self.assertEqual(j['request']['sheet_status'], 'draft')
        self.assertEqual(j['request']['requested_by'], '박세연')
        self.assertEqual(j['request']['pickup_date'], self.tomorrow)

    def test_push_sent_no_tab_and_assignment_readback(self):
        calls = []
        def fake(action, **p):
            calls.append((action, p))
            if action == 'upsert':
                return {'ok': False, 'error': 'NO_TAB'} if fake.no_tab else {'ok': True, 'tab': '2026.9.14', 'row': 14}
            if action == 'read':
                return {'ok': True, 'tab': '2026.9.14', 'rows': [{'row': 14, 'name': '한영도', 'driver': '권철호', 'vehicle': '6668'}]}
        fake.no_tab = True
        with patch.dict(os.environ, {'TRANSPORT_SHEET_URL': 'https://x/exec', 'TRANSPORT_SHEET_TOKEN': 't'}), \
             patch.object(transport, '_call_sheet', side_effect=fake):
            j = self._save().get_json()
            self.assertEqual(j['push']['status'], 'pending')
            self.assertIn('탭이 없어 전송 대기', transport.dashboard_alerts()[0]['detail'])
            # 보낸 행 값: B~I 순서, 도착시간에 '도착' 붙음
            self.assertEqual(calls[0][1]['row'], ['진료협력', '박세연', '한영도', 'W/C', '픽업(입원)', '안동병원 814호', '14:00 도착', '형님 010-6378-2866(회복)'])
            # 운행팀이 탭을 만든 뒤 스케줄 재시도 → 전송 → 배정 읽기
            fake.no_tab = False
            out = transport.sync_once('test')
            self.assertEqual((out['retry']['sent'], out['assignments']['updated']), (1, 1))
            r = transport.get_request(self.cid)
            self.assertEqual((r['sheet_status'], r['sheet_tab'], r['sheet_row'], r['driver'], r['vehicle']), ('sent', '2026.9.14', 14, '권철호', '6668'))
            self.assertEqual(transport.dashboard_alerts(), [])
            # 배정이 처음 확인된 순간 알림 피드에 오르고, 다시 동기화해도 같은 id(한 번만 토스트)
            alerts = transport.assignment_alerts()
            self.assertEqual(len(alerts), 1); self.assertEqual(alerts[0]['bucket'], '운행 배정')
            self.assertIn('권철호 / 6668', alerts[0]['summary']); self.assertEqual(alerts[0]['patient_name'], '한영도')
            first_id, first_at = alerts[0]['id'], transport.get_request(self.cid)['assigned_at']
            transport.sync_once('again')
            self.assertEqual(transport.get_request(self.cid)['assigned_at'], first_at)
            self.assertEqual(transport.assignment_alerts()[0]['id'], first_id)
            feed = self.c.get('/api/inbound/alerts').get_json()
            self.assertEqual(feed['count'], 0); self.assertEqual(feed['items'][0]['bucket'], '운행 배정')
            # 수정 저장 → 같은 행 갱신 요청(match_row)
            self._save(arrive_time='15:00')
            self.assertEqual(calls[-1][1]['match_row'], 14)

    def test_detail_and_waiting_pages_render(self):
        page = self.c.get(f'/consult/{self.cid}').get_data(as_text=True)
        self.assertIn('차량 운행(픽업) 요청', page)
        self.assertIn('value="안동병원 "', page)                 # 모병원 미리 채움
        self.assertIn('value="형님 010-6378-2866"', page)         # 보호자 연락처 미리 채움
        db = models.get_db(); db.execute("UPDATE consultations SET admission_status='입원대기'"); db.commit(); db.close()
        ward = self.c.get('/ward?tab=waiting').get_data(as_text=True)
        self.assertIn('wd-tp-none', ward)


if __name__ == '__main__':
    unittest.main()
