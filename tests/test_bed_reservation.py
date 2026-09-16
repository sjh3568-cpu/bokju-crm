"""병상 예약(사용 예정자) — 빈 방 표시·예약 생성/해제·가용 계산·재원 미포함·입원 완료 자동 해제 (2026-09-17)."""
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import app as main
import dashboard_metrics
import models
import partnerships
import support_requests


class BedReservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 'res.db'))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user('res-test', 'test-password', display_name='테스트')
        self.uid = models.get_user('res-test')['id']
        boot = patch.object(main, '_db_initialized', True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username='res-test', display_name='테스트',
                           role='admin', cooperation_permissions_v2=True,
                           perms={k: 2 for k in main.MENU_KEYS})
        self.today = date.today().isoformat()
        with models.get_db() as conn:
            conn.execute("INSERT INTO patients (id,name,gender) VALUES (1,'재원자','F')")
            conn.execute("INSERT INTO patients (id,name,gender) VALUES (2,'예정자','M')")
            # 12병동 1206호에 여자 재원 1명(명부 회차)
            conn.execute("""INSERT INTO consultations (id,patient_id,consult_date,admission_status,actual_admission_date,room_number)
                            VALUES (1,1,'2026-09-01','입원완료','2026-09-01','1206호')""")
            conn.execute("""INSERT INTO admission_episodes (patient_id,episode_no,status,admitted_at,room_number,ward,roster_key,consultation_id)
                            VALUES (1,1,'admitted','2026-09-01','1206호','12병동','k1',1)""")
            # 입원예정 상담(남) — 예약 폼에서 이어 둘 후보
            conn.execute("""INSERT INTO consultations (id,patient_id,consult_date,admission_status,planned_admission_date)
                            VALUES (2,2,'2026-09-01','입원예정',?)""", ((date.today() + timedelta(days=3)).isoformat(),))

    def _ward12(self):
        return next(w for w in dashboard_metrics.ward_occupancy() if w['ward'] == '12병동')

    def test_room_view_draws_empty_rooms_from_layout(self):
        html = self.client.get('/ward?view=room').get_data(as_text=True)
        self.assertIn('rm-no">1207<', html)          # 빈 방도 배치표대로
        self.assertIn('rm-no">1206<', html)
        self.assertIn('rm-reserve-btn', html)          # 빈 침상에 예약 버튼
        # 검색·주치의로 좁히면 빈 방은 그리지 않는다(그 조건의 환자가 없는 방이 빈 방처럼 보이므로)
        narrowed = self.client.get('/ward?view=room&doctor=아무개').get_data(as_text=True)
        self.assertNotIn('rm-no">1207<', narrowed)

    def test_reservation_excluded_from_free_beds_but_not_counted_as_admitted(self):
        before = self._ward12()
        r = self.client.post('/api/bed-reservation', json={'room_number': '1207', 'name': '김예약', 'gender': 'M',
                                                           'expected_date': self.today, 'memo': '전화 확보'})
        self.assertEqual(r.status_code, 200, r.get_json())
        after = self._ward12()
        self.assertEqual(after['count'], before['count'])            # 재원 인원 그대로
        self.assertEqual(after['reserved'], 1)
        self.assertEqual(after['free'], before['free'] - 1)          # 가용에서만 1 빠짐
        self.assertEqual(after['free_m'], before['free_m'] + 2)      # 1207호(3인실)가 남자 방이 되어 남은 2병상은 남자 몫
        self.assertEqual(after['free_open'], before['free_open'] - 3)
        self.assertEqual(after['free_rooms'], before['free_rooms'] - 1)
        # 재원 화면: KPI 총 입원 환자는 1 그대로, 예약 카드·예약 배지 표시
        html = self.client.get('/ward?view=room').get_data(as_text=True)
        self.assertIn('rm-reserved', html)
        self.assertIn('김예약', html)
        self.assertIn('· 예약 1', html)
        self.assertIn('<span class="wd-k-n">1</span>', html)
        # 대시보드 병동 띠에도 예약 태그
        dash = self.client.get('/').get_data(as_text=True)
        self.assertIn('예약 1</i>', dash)
        # 상담일지 병실 충돌 확인에서도 예약이 planned로 보인다
        st = dashboard_metrics.room_status('1207호')
        self.assertEqual([p['status'] for p in st['planned']], ['병상 예약'])

    def test_capacity_and_gender_guards(self):
        self.assertEqual(self.client.post('/api/bed-reservation', json={'room_number': '502', 'name': 'a'}).status_code, 200)
        r = self.client.post('/api/bed-reservation', json={'room_number': '502호', 'name': 'b'})   # 1인실 — 정원 초과
        self.assertEqual(r.status_code, 400)
        self.assertIn('정원', r.get_json()['error'])
        r = self.client.post('/api/bed-reservation', json={'room_number': '1206', 'name': '남자', 'gender': 'M'})   # 여자 방
        self.assertEqual(r.status_code, 400)
        self.assertIn('여자 병실', r.get_json()['error'])
        r = self.client.post('/api/bed-reservation', json={'room_number': '1207', 'name': ''})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.post('/api/bed-reservation', json={'room_number': '', 'name': 'x'}).status_code, 400)

    def test_release_restores_free_beds(self):
        rid = self.client.post('/api/bed-reservation', json={'room_number': '1207', 'name': '김예약'}).get_json()['id']
        self.assertEqual(self._ward12()['reserved'], 1)
        r = self.client.post(f'/api/bed-reservation/{rid}/release', json={'reason': '취소'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._ward12()['reserved'], 0)
        self.assertEqual(models.list_bed_reservations(), [])
        self.assertEqual(models.get_bed_reservation(rid)['release_reason'], '취소')
        self.assertEqual(self.client.post('/api/bed-reservation/999/release', json={}).status_code, 404)

    def test_linked_consultation_fills_fields_and_auto_releases_on_admit(self):
        r = self.client.post('/api/bed-reservation', json={'room_number': '1207', 'consultation_id': 2})
        self.assertEqual(r.status_code, 200, r.get_json())
        res = models.list_bed_reservations()[0]
        self.assertEqual((res['name'], res['gender']), ('예정자', 'M'))      # 상담에서 이름·성별·예정일을 채운다
        self.assertEqual(res['expected_date'], (date.today() + timedelta(days=3)).isoformat())
        # 예약 폼 후보 목록에 입원예정 상담이 실린다
        html = self.client.get('/ward?view=room').get_data(as_text=True)
        self.assertIn('id="rm-reserve-candidates"', html)
        self.assertIn('"name": "\\uc608\\uc815\\uc790"', html)
        # 입원 완료 → 예약 자동 해제, 그 자리는 이제 재원
        r = self.client.post('/api/consult/2/admit', json={'admission_date': self.today, 'room_number': '1207'})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(models.list_bed_reservations(), [])
        self.assertEqual(models.get_bed_reservation(res['id'])['release_reason'], '입원 완료')

    def test_name_match_auto_release_without_link(self):
        self.client.post('/api/bed-reservation', json={'room_number': '1207', 'name': '예정자', 'gender': 'M'})
        self.client.post('/api/consult/2/admit', json={'admission_date': self.today, 'room_number': '1207호'})
        self.assertEqual(models.list_bed_reservations(), [])


if __name__ == '__main__':
    unittest.main()
