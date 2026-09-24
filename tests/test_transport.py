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
        transport._GID_CACHE.clear(); transport._SHEET_LINK_CACHE['url'] = None   # 모듈 캐시가 테스트 사이에 새지 않게
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

    def test_reason_must_match_sheet_validation(self):
        # 시트 F열(요청이유)의 데이터 확인 규칙에 없는 값은 시트가 거부한다 — CRM 선택지와 검증을 그 규칙에 맞춘다
        self.assertEqual(transport.reason_options(), ['픽업(입원)', '외진', '단순운행', '혈액요청', '기관방문', '출장수행', '식당퇴근'])
        with patch.dict(os.environ, {'TRANSPORT_SHEET_URL': ''}):
            r = self._save(reason='픽업')
            self.assertEqual(r.status_code, 400); self.assertIn('요청이유', r.get_json()['error'])
            r = self._save(reason='')
            self.assertEqual(r.get_json()['request']['reason'], '픽업(입원)')
        with patch.dict(os.environ, {'TRANSPORT_REASON_OPTIONS': '픽업(입원), 외진'}):
            self.assertEqual(transport.reason_options(), ['픽업(입원)', '외진'])

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
                return {'ok': False, 'error': 'NO_TAB'} if fake.no_tab else {'ok': True, 'tab': '2026.9.14', 'row': 14, 'url': 'https://docs.google.com/spreadsheets/d/X/edit', 'gid': 777}
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
            self.assertEqual(r['sheet_gid'], '777')
            # 링크: .env 주소 우선, 전송된 건은 그 탭(#gid)으로. (가짜 응답이라 스크립트 URL 캐시는 비어 있음)
            with patch.dict(os.environ, {'TRANSPORT_SHEET_LINK': 'https://docs.google.com/spreadsheets/d/ENV/edit?gid=5#gid=5'}):
                self.assertEqual(transport.sheet_link('777'), 'https://docs.google.com/spreadsheets/d/ENV/edit#gid=777')
                self.assertEqual(transport.sheet_link(), 'https://docs.google.com/spreadsheets/d/ENV/edit')   # 복사 시 열려 있던 탭은 무시
                page = self.c.get(f'/consult/{self.cid}').get_data(as_text=True)
            self.assertIn('운행 시트 열기', page); self.assertIn('#gid=777', page)
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

    def test_private_ambulance_is_skip_with_reason(self):
        # '불필요(사설구급차)' — 관리과 차량은 안 나가므로 needed='no'와 같은 취급(시트 전송 없음·경고 없음), 사유만 따로 남긴다
        r = self._save(needed='private'); j = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual((j['request']['needed'], j['request']['skip_reason'], j['request']['sheet_status']), ('no', '사설구급차', 'skip'))
        self.assertEqual(transport.dashboard_alerts(), [])
        page = self.c.get(f'/consult/{self.cid}').get_data(as_text=True)
        self.assertIn('운행 불필요</b> (사설구급차)', page)
        self.assertIn('value="private" checked', page)
        self.assertEqual(self._save(needed='no').get_json()['request']['skip_reason'], '보호자 직접')
        db = models.get_db(); db.execute("UPDATE consultations SET admission_status='입원대기'"); db.commit(); db.close()
        self._save(needed='private')
        ward = self.c.get('/ward?tab=waiting').get_data(as_text=True)
        self.assertIn('wd-tp-skip', ward); self.assertIn('불필요(사설구급차)', ward)

    def test_open_tab_resolves_gid_by_date(self):
        # 클릭 시점에 날짜로 탭을 찾아 연다 — 전송 전이거나 서버 재시작 뒤라 gid를 몰라도 첫 탭(9.22)으로 떨어지지 않게
        calls = []
        def fake(action, **p):
            calls.append((action, p))
            if p.get('date') == '2026-09-28':
                return {'ok': True, 'tab_for_date': '2026.09.28', 'url': 'https://docs.google.com/spreadsheets/d/X/edit', 'gid': 77}
            return {'ok': True, 'tab_for_date': None, 'url': 'https://docs.google.com/spreadsheets/d/X/edit', 'gid': None}
        with patch.dict(os.environ, {'TRANSPORT_SHEET_URL': 'https://x/exec', 'TRANSPORT_SHEET_TOKEN': 't', 'TRANSPORT_SHEET_LINK': ''}), \
             patch.object(transport, '_call_sheet', side_effect=fake):
            r = self.c.get('/transport/open?date=2026-09-28')
            self.assertEqual(r.status_code, 302); self.assertEqual(r.headers['Location'], 'https://docs.google.com/spreadsheets/d/X/edit#gid=77')
            r = self.c.get('/transport/open?date=2026-09-28')      # 두 번째는 캐시 — 스크립트 왕복 없음
            self.assertEqual(r.status_code, 302); self.assertEqual(len(calls), 1)
            r = self.c.get('/transport/open?date=2026-10-05')
            self.assertEqual(r.status_code, 200); self.assertIn('탭이 아직 없습니다', r.get_data(as_text=True))
            self.assertEqual(self.c.get('/transport/open').status_code, 400)
            # 탭은 있는데 gid가 없음 = 구글 쪽 스크립트가 옛 버전 → '탭 없음'이 아니라 재배포 안내
            with patch.object(transport, '_call_sheet', return_value={'ok': True, 'tab_for_date': '2026.10.06', 'tab_last_row': 20}):
                r = self.c.get('/transport/open?date=2026-10-06')
                self.assertEqual(r.status_code, 200); self.assertIn('옛 버전', r.get_data(as_text=True))
            # 카드 상단 '운행 시트 열기'는 이 경로로 — 날짜(탭) 칸 값 기준
            page = self.c.get(f'/consult/{self.cid}').get_data(as_text=True)
            self.assertIn(f'/transport/open?date={self.tomorrow}', page)

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
