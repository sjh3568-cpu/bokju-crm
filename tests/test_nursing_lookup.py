"""상담일지 '환자상태 → 요양원'에서 마스터에 없는 요양원을 그 자리에서 등록 — 공단 조회·등록 API (2026-09-16)."""
import os
import tempfile
import unittest
from unittest.mock import patch

import app as main
import models
import ltci_sync


class NursingLookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 'test.db'))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        models.ensure_admin_user('nursingtest', 'test-only-password', display_name='테스트')
        user = models.get_user('nursingtest')
        boot = patch.object(main, '_db_initialized', True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=user['id'], username='nursingtest', role='admin',
                           perms={k: 3 for k in main.MENU_KEYS})

    def test_lookup_returns_ltci_candidates(self):
        rows = [{'official_code': 'K1', 'name': '안동행복요양원', 'kind': '노인요양시설', 'region': '경북', 'address': None, 'phone': None}]
        with patch.object(ltci_sync, 'service_key', return_value='dummy'), patch.object(ltci_sync, 'lookup', return_value=rows):
            r = self.client.get('/api/nursing/lookup?q=행복')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['items'][0]['official_code'], 'K1')
        with patch.object(ltci_sync, 'service_key', return_value=''):
            self.assertFalse(self.client.get('/api/nursing/lookup?q=행복').get_json()['configured'])
        with patch.object(ltci_sync, 'service_key', return_value='dummy'), patch.object(ltci_sync, 'lookup', side_effect=RuntimeError('API 오류')):
            self.assertEqual(self.client.get('/api/nursing/lookup?q=행복').status_code, 502)

    def test_register_from_ltci_then_autocomplete_matches(self):
        entry = {'official_code': 'K1', 'name': '안동행복요양원', 'kind': '노인요양시설', 'region': '경북'}
        r = self.client.post('/api/nursing/register', json=entry)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()['source'], 'ltci-lookup')
        items = models.autocomplete_nursing_homes('안동행복요양원')['items']
        self.assertTrue(items and items[0]['exact'] and items[0]['kind'] == '노인요양시설')
        db = models.get_db()
        self.assertEqual(db.execute("SELECT source FROM source_nursing_homes WHERE official_code='K1'").fetchone()[0], 'ltci-lookup')
        self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log WHERE action='nursing_register'").fetchone()[0], 1)
        db.close()

    def test_register_manual_name_and_validation(self):
        r = self.client.post('/api/nursing/register', json={'name': '새로생긴요양원'})
        self.assertEqual(r.get_json()['source'], 'manual')
        self.assertTrue(models.autocomplete_nursing_homes('새로생긴요양원')['items'][0]['exact'])
        self.assertEqual(self.client.post('/api/nursing/register', json={'name': '원'}).status_code, 400)
        with self.client.session_transaction() as session:
            session['perms'] = {k: 1 for k in main.MENU_KEYS}
        self.assertEqual(self.client.post('/api/nursing/register', json={'name': '권한없음요양원'}).status_code, 403)


if __name__ == '__main__':
    unittest.main()
