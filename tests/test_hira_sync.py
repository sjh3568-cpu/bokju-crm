"""심평원 API 자동 갱신 — API는 가짜 응답으로 대체하고 적재·상태·스케줄 계산을 검증한다."""
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

import models
import partnerships as coop
import hira_sync


def _fake_pages(rows, per_page):
    """_fetch_page 대역. page 번호에 맞춰 잘라 준다."""
    def fetch(key, page):
        start = (page - 1) * per_page
        return rows[start:start + per_page], len(rows)
    return fetch


class HiraSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 't.db')); self.db_patch.start()
        self.status_patch = patch.object(hira_sync, 'STATUS_PATH', hira_sync.Path(self.tmp.name) / 'status.json'); self.status_patch.start()
        models.init_db(); coop.init_schema()

    def tearDown(self):
        self.status_patch.stop(); self.db_patch.stop(); self.tmp.cleanup()

    def test_run_upserts_both_tables_and_records_status(self):
        rows = [
            {'ykiho': 'K1', 'yadmNm': '의료법인안동병원', 'clCdNm': '종합병원', 'sidoCdNm': '경북', 'addr': '안동시', 'telno': '054'},
            {'ykiho': 'K2', 'yadmNm': '안동성소병원', 'clCdNm': '종합병원', 'sidoCdNm': '경북', 'addr': '안동시', 'telno': '054'},
            {'ykiho': 'K3', 'yadmNm': '동네의원', 'clCdNm': '의원', 'sidoCdNm': '경북', 'addr': '안동시', 'telno': '054'},
            {'ykiho': 'K4', 'yadmNm': '길주요양병원', 'clCdNm': '요양병원', 'sidoCdNm': '경북', 'addr': '안동시', 'telno': '054'},
        ]
        with patch.object(hira_sync, '_fetch_page', _fake_pages(rows, per_page=3)), \
             patch.object(hira_sync, 'service_key', return_value='dummy'), \
             patch.object(hira_sync.time, 'sleep'):
            out = hira_sync.run('test')
        self.assertTrue(out['ok'])
        self.assertEqual(out['fetched'], 4)          # 두 페이지(3+1)를 다 돌았다
        db = models.get_db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM cooperation_facility_directory").fetchone()[0], 4)  # 명부는 의원 포함
        kinds = {r[0]: r[1] for r in db.execute("SELECT name, kind FROM source_hospitals")}
        db.close()
        self.assertEqual(kinds.get('길주요양병원'), '요양병원')
        self.assertEqual(kinds.get('동네의원'), '의원')          # 마스터도 의원까지 (기존 마스터와 같은 범위)
        self.assertEqual(hira_sync.status()['fetched'], 4)
        # 종별 배지가 API로 받은 명부에서 바로 잡힌다
        models._kind_index_cache['stamp'] = None
        self.assertEqual(models.hospital_kind('안동병원'), '종합병원')

    def test_rename_follows_official_code(self):
        first = [{'ykiho': 'K1', 'yadmNm': '옛이름병원', 'clCdNm': '병원', 'sidoCdNm': '경북', 'addr': 'a', 'telno': '1'}]
        renamed = [{'ykiho': 'K1', 'yadmNm': '새이름병원', 'clCdNm': '병원', 'sidoCdNm': '경북', 'addr': 'a', 'telno': '1'}]
        with patch.object(hira_sync, 'service_key', return_value='dummy'), patch.object(hira_sync.time, 'sleep'):
            with patch.object(hira_sync, '_fetch_page', _fake_pages(first, 10)):
                hira_sync.run('t1')
            with patch.object(hira_sync, '_fetch_page', _fake_pages(renamed, 10)):
                hira_sync.run('t2')
        db = models.get_db()
        names = [r[0] for r in db.execute("SELECT name FROM cooperation_facility_directory WHERE official_code='K1'")]
        db.close()
        self.assertEqual(names, ['새이름병원'])   # 같은 요양기호는 한 줄, 이름만 따라간다

    def test_missing_key_and_api_error_do_not_raise(self):
        with patch.object(hira_sync, 'service_key', return_value=''):
            out = hira_sync.run('nokey')
        self.assertFalse(out['ok']); self.assertIn('HIRA_SERVICE_KEY', out['error'])
        with patch.object(hira_sync, 'service_key', return_value='dummy'), \
             patch.object(hira_sync, '_fetch_page', side_effect=RuntimeError('API 오류 30: SERVICE_KEY_IS_NOT_REGISTERED_ERROR')):
            out = hira_sync.run('bad')
        self.assertFalse(out['ok']); self.assertIn('API 오류', out['error'])

    def test_next_run_lands_on_configured_weekday_hour(self):
        with patch.object(hira_sync, 'WEEKDAY', 0), patch.object(hira_sync, 'HOUR', 6):
            secs = hira_sync._seconds_until_next_run()
        nxt = datetime.now().timestamp() + secs
        nxt = datetime.fromtimestamp(nxt)
        self.assertEqual((nxt.weekday(), nxt.hour, nxt.minute), (0, 6, 0))
        self.assertGreater(secs, 0)


if __name__ == '__main__':
    unittest.main()
