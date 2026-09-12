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

    def test_details_only_for_recent_hospitals_and_partners(self):
        """상세는 최근 1년 상담에 나온 모병원 + 협력기관만 받고, 명부에 진료과목·병상·간호간병을 쓴다."""
        rows = [
            {'ykiho': 'K1', 'yadmNm': '의료법인안동병원', 'clCdNm': '종합병원', 'sidoCdNm': '경북', 'addr': 'a', 'telno': '1'},
            {'ykiho': 'K2', 'yadmNm': '먼곳병원', 'clCdNm': '병원', 'sidoCdNm': '서울', 'addr': 'b', 'telno': '2'},
            {'ykiho': 'K3', 'yadmNm': '협력기관병원', 'clCdNm': '병원', 'sidoCdNm': '경북', 'addr': 'c', 'telno': '3'},
        ]
        with patch.object(hira_sync, '_fetch_page', _fake_pages(rows, 10)), \
             patch.object(hira_sync, 'service_key', return_value='dummy'), patch.object(hira_sync.time, 'sleep'), \
             patch.object(hira_sync, 'sync_details', return_value={'targets': 0, 'updated': 0, 'failed': [], 'failed_count': 0}):
            hira_sync.run('base')
        db = models.get_db()
        pid = db.execute("INSERT INTO patients(name) VALUES ('상세환자')").lastrowid
        recent = (hira_sync.datetime.now() - hira_sync.timedelta(days=30)).date().isoformat()
        db.execute("INSERT INTO consultations(patient_id,consult_date,admission_status,source_hospital) VALUES (?,?,'상담중','안동병원')", (pid, recent))
        # 협력기관: 명부 K3에 연결
        d_id = db.execute("SELECT id FROM cooperation_facility_directory WHERE official_code='K3'").fetchone()[0]
        h_id = db.execute("SELECT id FROM source_hospitals WHERE name='협력기관병원'").fetchone()[0]
        db.execute("INSERT INTO cooperation_partners(hospital_id,directory_id,official_name) VALUES (?,?,'협력기관병원')", (h_id, d_id))
        db.commit(); db.close()
        models._kind_index_cache['stamp'] = None

        targets = hira_sync.detail_targets()
        self.assertEqual(set(targets.values()), {'안동병원', '협력기관병원'})   # 먼곳병원은 대상 아님

        def fake_detail(key, ykiho):
            return {'K1': {'departments': {'내과', '재활의학과'}, 'integrated': True, 'beds': 1049},
                    'K3': {'departments': {'정형외과'}, 'integrated': False, 'beds': 120}}[ykiho]
        with patch.object(hira_sync, 'fetch_detail', side_effect=fake_detail), patch.object(hira_sync.time, 'sleep'):
            out = hira_sync.sync_details('dummy')
        self.assertEqual((out['targets'], out['updated'], out['failed_count']), (2, 2, 0))
        db = models.get_db()
        r = db.execute("SELECT departments, bed_count, integrated_nursing, detail_updated_at FROM cooperation_facility_directory WHERE official_code='K1'").fetchone()
        untouched = db.execute("SELECT bed_count FROM cooperation_facility_directory WHERE official_code='K2'").fetchone()[0]
        db.close()
        self.assertEqual((r[0], r[1], r[2]), ('내과, 재활의학과', 1049, 1))
        self.assertRegex(r[3], r'^\d{4}-\d{2}-\d{2}$')
        self.assertIsNone(untouched)

    def test_detail_failure_of_one_hospital_does_not_stop_the_rest(self):
        def flaky(key, ykiho):
            if ykiho == 'BAD':
                raise AttributeError("'int' object has no attribute 'strip'")   # 실제로 났던 오류 — 어떤 예외든 삼켜야 한다
            return {'departments': {'내과'}, 'integrated': False, 'beds': 10}
        coop.import_facility_directory([{'official_code': 'OK', 'name': '정상병원', 'kind': '병원'},
                                        {'official_code': 'BAD', 'name': '불통병원', 'kind': '병원'}], 'test')
        with patch.object(hira_sync, 'fetch_detail', side_effect=flaky), patch.object(hira_sync.time, 'sleep'):
            out = hira_sync.sync_details('dummy', targets={'BAD': '불통병원', 'OK': '정상병원'})
        self.assertEqual((out['updated'], out['failed']), (1, ['불통병원']))

    def test_next_run_lands_on_configured_weekday_hour(self):
        with patch.object(hira_sync, 'WEEKDAY', 0), patch.object(hira_sync, 'HOUR', 6):
            secs = hira_sync._seconds_until_next_run()
        nxt = datetime.now().timestamp() + secs
        nxt = datetime.fromtimestamp(nxt)
        self.assertEqual((nxt.weekday(), nxt.hour, nxt.minute), (0, 6, 0))
        self.assertGreater(secs, 0)


if __name__ == '__main__':
    unittest.main()
