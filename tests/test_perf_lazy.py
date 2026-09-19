"""속도·크기 개선 (2026-09-15) — 상담 ids 조회, 재원 명단 지연 로딩, 침상 편집 폼 템플릿, 부분 갱신, gzip."""
import gzip
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

import app as main
import models


class PerfLazyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 'test.db'))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        import partnerships, support_requests, transport
        partnerships.init_schema(); support_requests.init_schema(); transport.init_schema()
        models.ensure_admin_user('perftest', 'test-only-password', display_name='테스트')
        user = models.get_user('perftest')
        boot = patch.object(main, '_db_initialized', True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=user['id'], username='perftest', role='admin',
                           perms={k: 3 for k in main.MENU_KEYS})
        today = date.today().isoformat()
        self.cids = []
        for i in range(5):
            pid = models.find_or_create_patient(name=f"환자{i}", guardian_phone=f"010-0000-000{i}")
            self.cids.append(models.create_consultation(patient_id=pid, consult_date=today, counselor="박세연",
                                                        admission_status="입원완료" if i < 3 else "상담완료",
                                                        actual_admission_date=today, room_number=f"20{i}호"))

    def test_list_consultations_ids_filter(self):
        got = {c["id"] for c in models.list_consultations(ids=self.cids[:2], limit=100)}
        self.assertEqual(got, set(self.cids[:2]))
        self.assertEqual(models.list_consultations(ids=[], limit=100), [])               # 빈 목록 = 없음
        both = {c["id"] for c in models.list_consultations(ids=self.cids, admission_status="입원완료", limit=100)}
        self.assertEqual(both, set(self.cids[:3]))                                        # 다른 필터와 AND
        # 900개 초과는 나눠 읽어도 결과가 같다
        many = list(range(1, 2000)) + self.cids
        self.assertEqual({c["id"] for c in models.list_consultations(ids=many, limit=100000)}, set(self.cids))

    def test_ward_roster_is_lazy_by_default_and_served_as_partial(self):
        html = self.client.get('/ward').get_data(as_text=True)
        self.assertIn('id="wd-roster-body" hidden data-lazy="1"', html)
        self.assertNotIn('class="rm-bed', html)                      # 접힌 명단은 보내지 않는다
        self.assertIn('id="rm-editor-tpl"', html)                      # 편집 폼 템플릿은 1벌만
        part = self.client.get('/ward?partial=roster&view=room').get_data(as_text=True)
        self.assertNotIn('<html', part)                                 # 조각만
        self.assertIn('class="rm-bed', part)
        self.assertNotIn('rm-inline-away', part)                        # 카드 안에 편집 폼 없음(data 속성만)
        self.assertIn('data-room="200호"', part)
        # 필터·view가 있으면 예전처럼 바로 렌더 (검색 결과 화면)
        full = self.client.get('/ward?view=room').get_data(as_text=True)
        self.assertIn('id="wd-roster-body" >', full)
        self.assertIn('class="rm-bed', full)

    def test_gzip_for_large_html_only(self):
        r = self.client.get('/ward?view=room', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(r.headers.get('Content-Encoding'), 'gzip')
        self.assertIn('Accept-Encoding', r.headers.get('Vary', ''))
        self.assertIn('재원 관리', gzip.decompress(r.data).decode('utf-8'))
        small = self.client.get('/healthz', headers={'Accept-Encoding': 'gzip'})
        self.assertIsNone(small.headers.get('Content-Encoding'))       # 4KB 미만은 그대로
        plain = self.client.get('/ward?view=room')
        self.assertIsNone(plain.headers.get('Content-Encoding'))       # 클라이언트가 안 받으면 그대로

    def test_partial_refresh_script_present_on_autorefresh_pages(self):
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('data-autorefresh="30"', html)
        self.assertIn('async function partialRefresh()', html)
        self.assertIn("main.replaceWith(next)", html)

    def test_static_is_revalidated_not_refetched(self):
        """정적 자산은 no-cache(재검증) — 화면마다 통째로 다시 받지 않는다.
        환자 정보가 없는 css·js에까지 no-store를 걸어 대시보드 1회에 420KB가 새로 나가던 것을 고쳤다.
        max-age를 주지 않는 이유는 배포로 파일이 바뀌면 ETag가 달라져 그 자리에서 새로 받게 하기 위함."""
        first = self.client.get('/static/css/style.css')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers.get('Cache-Control'), 'no-cache')
        self.assertIsNone(first.headers.get('Pragma'))                  # no-store 시절 잔재가 남으면 안 됨
        etag = first.headers.get('ETag')
        self.assertTrue(etag)                                           # 재검증의 근거
        again = self.client.get('/static/css/style.css', headers={'If-None-Match': etag})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(len(again.data), 0)                            # 본문이 다시 나가지 않는다

    def test_pages_still_no_store(self):
        """환자 정보 화면의 no-store는 그대로 — 로그아웃 후 뒤로가기 노출 방지가 이 훅의 원래 목적이다."""
        for path in ('/', '/consultations', '/ward'):
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertEqual(r.headers.get('Cache-Control'), 'no-store, private, must-revalidate')
                self.assertEqual(r.headers.get('Pragma'), 'no-cache')

if __name__ == "__main__":
    unittest.main()
