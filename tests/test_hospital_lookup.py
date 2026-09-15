"""상담일지에서 마스터에 없는 병원을 그 자리에서 등록 — 심평원 조회·등록 API (2026-09-15)."""
import os
import tempfile
import unittest
from unittest.mock import patch

import app as main
import models
import partnerships as coop
import hira_sync


class HospitalLookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 'test.db'))
        db.start(); self.addCleanup(db.stop)
        models.init_db(); coop.init_schema()
        models.ensure_admin_user('lookuptest', 'test-only-password', display_name='테스트')
        user = models.get_user('lookuptest')
        boot = patch.object(main, '_db_initialized', True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=user['id'], username='lookuptest', role='admin',
                           perms={k: 3 for k in main.MENU_KEYS})

    def test_lookup_returns_hira_candidates(self):
        rows = [{'official_code': 'A1', 'name': '재단법인아산사회복지재단 서울아산병원', 'kind': '상급종합', 'region': '서울', 'address': '송파구', 'phone': '02'}]
        with patch.object(hira_sync, 'service_key', return_value='dummy'), patch.object(hira_sync, 'lookup', return_value=rows):
            r = self.client.get('/api/hospital/lookup?q=서울아산')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['items'][0]['official_code'], 'A1')
        with patch.object(hira_sync, 'service_key', return_value=''):
            self.assertFalse(self.client.get('/api/hospital/lookup?q=서울아산').get_json()['configured'])
        with patch.object(hira_sync, 'service_key', return_value='dummy'), patch.object(hira_sync, 'lookup', side_effect=RuntimeError('API 오류')):
            self.assertEqual(self.client.get('/api/hospital/lookup?q=서울아산').status_code, 502)

    def test_register_from_hira_then_autocomplete_matches(self):
        entry = {'official_code': 'A1', 'name': '재단법인아산사회복지재단 서울아산병원', 'kind': '상급종합', 'region': '서울', 'address': '송파구'}
        r = self.client.post('/api/hospital/register', json=entry)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()['source'], 'hira-lookup')
        items = models.autocomplete_hospitals('서울아산병원')['items']
        self.assertTrue(items and items[0]['exact'] and items[0]['kind'] == '상급종합')
        db = models.get_db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM cooperation_facility_directory WHERE official_code='A1'").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log WHERE action='hospital_register'").fetchone()[0], 1)
        db.close()

    def test_register_manual_name_and_validation(self):
        r = self.client.post('/api/hospital/register', json={'name': '새로생긴병원'})
        self.assertEqual(r.get_json()['source'], 'manual')
        self.assertTrue(models.autocomplete_hospitals('새로생긴병원')['items'][0]['exact'])
        self.assertEqual(self.client.post('/api/hospital/register', json={'name': '병'}).status_code, 400)
        with self.client.session_transaction() as session:
            session['perms'] = {k: 1 for k in main.MENU_KEYS}
        self.assertEqual(self.client.post('/api/hospital/register', json={'name': '권한없음병원'}).status_code, 403)
