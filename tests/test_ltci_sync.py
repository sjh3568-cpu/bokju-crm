"""공단 장기요양기관(요양원) 명부 자동 갱신 — API는 가짜 응답으로 대체하고 파싱·적재·이름 충돌 처리를 검증한다."""
import os
import tempfile
import unittest
from unittest.mock import patch

import models
import ltci_sync


def _fake_pages(rows_by_sido, per_page):
    """_fetch_page 대역. (siDoCd, adminPttnCd) 조합별 행을 page 단위로 잘라 준다."""
    def fetch(key, sido, code, page):
        rows = [r for r in rows_by_sido.get(sido, []) if r['adminPttnCd'] == code]
        start = (page - 1) * per_page
        return rows[start:start + per_page], len(rows)
    return fetch


class LtciParseTests(unittest.TestCase):
    def test_parses_xml_items_and_total(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <response><header><resultCode>00</resultCode><resultMsg>NORMAL SERVICE.</resultMsg></header>
        <body><items><item><longTermAdminSym>34711000123</longTermAdminSym><adminPttnCd>A03</adminPttnCd>
        <adminNm> 안동 행복요양원 </adminNm><siDoCd>47</siDoCd><siGunGuCd>170</siGunGuCd></item></items>
        <numOfRows>1000</numOfRows><pageNo>1</pageNo><totalCount>1</totalCount></body></response>""".encode("utf-8")
        items, total = ltci_sync._parse_response(xml)
        self.assertEqual(total, 1)
        self.assertEqual(items[0]['adminNm'], '안동 행복요양원')
        entry = ltci_sync._entry(items[0], '경북')
        self.assertEqual(entry['name'], '안동 행복요양원')
        self.assertEqual((entry['official_code'], entry['kind'], entry['region']), ('34711000123', '노인요양시설', '경북'))

    def test_parses_json_when_offered(self):
        raw = '{"response":{"header":{"resultCode":"00"},"body":{"items":{"item":{"longTermAdminSym":"1","adminPttnCd":"A04","adminNm":"작은집"}},"totalCount":1}}}'.encode('utf-8')
        items, total = ltci_sync._parse_response(raw)
        self.assertEqual((total, items[0]['adminNm']), (1, '작은집'))
        self.assertEqual(ltci_sync._entry(items[0], '서울')['kind'], '노인요양공동생활가정')

    def test_gateway_error_raises(self):
        xml = b"""<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg>
        <returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg><returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>"""
        with self.assertRaises(RuntimeError) as cm:
            ltci_sync._parse_response(xml)
        self.assertIn('NOT_REGISTERED', str(cm.exception))


class LtciSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 't.db')); self.db_patch.start()
        self.status_patch = patch.object(ltci_sync, 'STATUS_PATH', ltci_sync.Path(self.tmp.name) / 'status.json'); self.status_patch.start()
        models.init_db()

    def tearDown(self):
        self.status_patch.stop(); self.db_patch.stop(); self.tmp.cleanup()

    def test_run_loads_residential_only_and_disambiguates_duplicate_names(self):
        rows = {
            '47': [  # 경북
                {'longTermAdminSym': 'K1', 'adminPttnCd': 'A03', 'adminNm': '행복요양원'},
                {'longTermAdminSym': 'K2', 'adminPttnCd': 'A04', 'adminNm': '행복요양원'},   # 같은 시도 안 동명
                {'longTermAdminSym': 'K3', 'adminPttnCd': 'A03', 'adminNm': '복주요양원'},
                {'longTermAdminSym': 'K9', 'adminPttnCd': 'C01', 'adminNm': '행복재가센터'},  # 재가 — 요청 안 함
            ],
            '11': [  # 서울
                {'longTermAdminSym': 'S1', 'adminPttnCd': 'A03', 'adminNm': '행복요양원'},
            ],
        }
        with patch.object(ltci_sync, '_fetch_page', _fake_pages(rows, per_page=1)), \
             patch.object(ltci_sync, 'service_key', return_value='dummy'), \
             patch.object(ltci_sync.time, 'sleep'):
            out = ltci_sync.run('test')
        self.assertTrue(out['ok'], out)
        self.assertEqual(out['fetched'], 4)
        db = models.get_db()
        names = {r[0]: (r[1], r[2], r[3]) for r in db.execute(
            "SELECT name, region, kind, official_code FROM source_nursing_homes WHERE source='ltci-api'")}
        db.close()
        self.assertEqual(set(names), {'행복요양원 (경북 1)', '행복요양원 (경북 2)', '행복요양원 (서울)', '복주요양원'})
        self.assertEqual(names['행복요양원 (경북 1)'], ('경북', '노인요양시설', 'K1'))
        self.assertEqual(names['행복요양원 (경북 2)'], ('경북', '노인요양공동생활가정', 'K2'))
        self.assertEqual(names['복주요양원'][1], '노인요양시설')
        self.assertTrue(ltci_sync.master_synced())
        self.assertFalse(ltci_sync.needs_bootstrap())
        self.assertEqual(ltci_sync.status()['master_total'], 4)
        # 자동완성이 공단 명부에서 찾는다
        items = models.autocomplete_nursing_homes('행복요양원')['items']
        self.assertEqual(len(items), 3)

        # 다음 갱신: 서울에 '복주요양원'이 생겨 경북 것이 '복주요양원 (경북)'으로 바뀌고, 행복요양원 (서울)은 폐업
        rows['11'] = [{'longTermAdminSym': 'S2', 'adminPttnCd': 'A03', 'adminNm': '복주요양원'}]
        with patch.object(ltci_sync, '_fetch_page', _fake_pages(rows, per_page=1)), \
             patch.object(ltci_sync, 'service_key', return_value='dummy'), \
             patch.object(ltci_sync.time, 'sleep'):
            out = ltci_sync.run('test2')
        self.assertTrue(out['ok'], out)
        self.assertEqual(out['deactivated'], 2)   # '복주요양원'(옛 이름) + '행복요양원 (서울)'
        db = models.get_db()
        active = {r[0] for r in db.execute("SELECT name FROM source_nursing_homes WHERE source='ltci-api' AND active=1")}
        inactive = {r[0] for r in db.execute("SELECT name FROM source_nursing_homes WHERE active=0")}
        db.close()
        self.assertEqual(active, {'행복요양원 (경북 1)', '행복요양원 (경북 2)', '복주요양원 (경북)', '복주요양원 (서울)'})
        self.assertEqual(inactive, {'복주요양원', '행복요양원 (서울)'})
        self.assertEqual(ltci_sync.status()['master_total'], 4)

    def test_run_without_key_or_with_api_failure_records_error(self):
        with patch.object(ltci_sync, 'service_key', return_value=''):
            out = ltci_sync.run('test')
        self.assertFalse(out['ok']); self.assertIn('LTCI_SERVICE_KEY', out['error'])
        self.assertTrue(ltci_sync.needs_bootstrap())

        def boom(key, sido, code, page):
            raise RuntimeError('API 오류 30: SERVICE_KEY_IS_NOT_REGISTERED_ERROR')
        with patch.object(ltci_sync, '_fetch_page', boom), patch.object(ltci_sync, 'service_key', return_value='dummy'):
            out = ltci_sync.run('test')
        self.assertFalse(out['ok']); self.assertIn('활용신청', out['error'])
        self.assertLessEqual(ltci_sync._next_wait(out), ltci_sync.RETRY_AFTER_FAILURE)

    def test_lookup_queries_every_sido_and_keeps_residential_only(self):
        calls = []

        def fake_call(key, params, timeout):
            calls.append(params['siDoCd'])
            if params['siDoCd'] == '47':
                return [
                    {'longTermAdminSym': 'K1', 'adminPttnCd': 'A03', 'adminNm': '안동행복요양원'},
                    {'longTermAdminSym': 'K9', 'adminPttnCd': 'C01', 'adminNm': '행복방문요양센터'},
                ], 2
            return [], 0
        with patch.object(ltci_sync, '_call', fake_call):
            items = ltci_sync.lookup('dummy', '행복')
        self.assertEqual(sorted(calls), sorted(c for c, _ in ltci_sync.SIDO_CODES))
        self.assertEqual([(i['name'], i['region'], i['kind']) for i in items], [('안동행복요양원', '경북', '노인요양시설')])
        self.assertEqual(ltci_sync.lookup('dummy', ''), [])


if __name__ == '__main__':
    unittest.main()
