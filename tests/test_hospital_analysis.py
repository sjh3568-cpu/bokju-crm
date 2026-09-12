"""모병원 분석 보강 — 전기 대비·질환군·협력기관 연결·필터. 임시 DB."""
import os
import tempfile
import unittest
from unittest.mock import patch

import app as main
import models
import partnerships as coop
import hospital_analysis as ha


class HospitalAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 't.db')); self.db_patch.start()
        models.init_db(); coop.init_schema()
        models._kind_index_cache['stamp'] = None
        db = models.get_db()
        coop.import_facility_directory([
            {'official_code': 'A', 'name': '의료법인안동병원', 'kind': '종합병원', 'region': '경북', 'address': 'x'},
            {'official_code': 'B', 'name': '강릉아산병원', 'kind': '상급종합', 'region': '강원', 'address': 'y'},
        ], 'test')
        def consult(hosp, day, status, diseases):
            pid = db.execute("INSERT INTO patients(name) VALUES (?)", (f'{hosp}{day}',)).lastrowid
            db.execute("INSERT INTO consultations(patient_id,consult_date,admission_status,source_hospital,diseases) VALUES (?,?,?,?,?)",
                       (pid, day, status, hosp, diseases))
        # 이번 기간(6월): 안동병원 3건(뇌출혈·뇌경색·고관절), 강릉아산 2건 / 전기(5월): 안동병원 1건, 강릉아산 3건
        for day, st, dz in (('2026-06-02', '입원완료', '["뇌출혈"]'), ('2026-06-10', '입원완료', '["뇌경색"]'), ('2026-06-20', '상담중', '["고관절 골절"]')):
            consult('안동병원', day, st, dz)
        for day in ('2026-06-05', '2026-06-06'):
            consult('강릉아산병원', day, '상담중', '["뇌경색"]')
        consult('안동병원', '2026-05-15', '상담중', '["뇌출혈"]')
        for day in ('2026-05-03', '2026-05-09', '2026-05-21'):
            consult('강릉아산병원', day, '입원완료', '["뇌경색"]')
        consult('새병원', '2026-06-11', '상담중', '["뇌경색"]'); consult('새병원', '2026-06-12', '상담중', '["뇌경색"]')
        # 협력기관: 강릉아산병원 등록 (명부 B 연결)
        db.execute("INSERT OR IGNORE INTO source_hospitals(name,active) VALUES ('강릉아산병원',1)")
        h = db.execute("SELECT id FROM source_hospitals WHERE name='강릉아산병원'").fetchone()[0]
        d = db.execute("SELECT id FROM cooperation_facility_directory WHERE official_code='B'").fetchone()[0]
        db.execute("INSERT INTO cooperation_partners(hospital_id,directory_id,official_name) VALUES (?,?,'강릉아산병원')", (h, d))
        db.commit(); db.close()

    def tearDown(self):
        self.db_patch.stop(); self.tmp.cleanup()

    def test_previous_period_has_same_length(self):
        self.assertEqual(ha.previous_period('2026-06-01', '2026-06-30'), ('2026-05-02', '2026-05-31'))
        self.assertEqual(ha.previous_period('2026-03-01', '2026-08-31'), ('2025-08-29', '2026-02-28'))

    def test_enrich_adds_delta_diseases_partner_region(self):
        d = ha.enrich('2026-06-01', '2026-06-30')
        by = {h['name']: h for h in d['hospitals']}
        a, g, n = by['안동병원'], by['강릉아산병원'], by['새병원']
        self.assertEqual((a['referrals'], a['prev_referrals'], a['delta_referrals']), (3, 1, 2))
        self.assertEqual((a['admissions'], a['prev_admissions'], a['delta_admissions']), (2, 0, 2))
        self.assertEqual((g['delta_referrals'], g['delta_admissions']), (-1, -3))
        self.assertEqual([x['short'] for x in a['diseases']], ['중추신경', '근골격'])
        self.assertEqual(a['diseases'][0]['pct'], 67)                    # 3건 중 2건이 중추신경
        self.assertIsNotNone(g['partner_id']); self.assertIsNone(a['partner_id'])
        self.assertEqual((a['region'], g['region']), ('경북', '강원'))
        self.assertTrue(n['is_new']); self.assertFalse(a['is_new'])
        self.assertEqual((d['delta_referrals'], d['delta_admissions']), (7 - 4, 2 - 3))
        self.assertEqual(d['partner_count'], 1)

    def test_filters_and_sort(self):
        d = ha.enrich('2026-06-01', '2026-06-30')
        self.assertEqual([h['name'] for h in ha.apply_filters(d['hospitals'], kind='상급종합')], ['강릉아산병원'])
        self.assertEqual([h['name'] for h in ha.apply_filters(d['hospitals'], region='경북')], ['안동병원'])
        self.assertEqual([h['name'] for h in ha.apply_filters(d['hospitals'], partner='yes')], ['강릉아산병원'])
        self.assertEqual(sorted(h['name'] for h in ha.apply_filters(d['hospitals'], partner='no')), ['새병원', '안동병원'])
        up = sorted(d['hospitals'], key=ha.SORT_KEYS['up']); down = sorted(d['hospitals'], key=ha.SORT_KEYS['down'])
        self.assertEqual(up[0]['name'], '안동병원'); self.assertEqual(down[0]['name'], '강릉아산병원')
        self.assertEqual(dict(ha.filter_options(d['hospitals'])['kinds'])['종합병원'], 1)

    def test_page_renders_new_columns_and_register_button(self):
        models.ensure_admin_user('t', 'x', display_name='t'); u = models.get_user('t')
        with patch.object(main, '_db_initialized', True), patch.object(models, 'first_unread_required_announcement', return_value=None):
            main.app.config.update(TESTING=True); c = main.app.test_client()
            with c.session_transaction() as s:
                s.update(user_id=u['id'], username='t', role='admin', perms={k: 3 for k in main.MENU_KEYS})
            page = c.get('/stats/hospitals?preset=custom&from=2026-06-01&to=2026-06-30').get_data(as_text=True)
            filtered = c.get('/stats/hospitals?preset=custom&from=2026-06-01&to=2026-06-30&kind=상급종합').get_data(as_text=True)
        self.assertIn('전기 대비', page); self.assertIn('주요 질환', page)
        self.assertIn('★ 관리 중', page)                                  # 강릉아산 = 협력기관
        self.assertIn('action="/partners/add-candidate"', page)           # 안동병원 = 등록 버튼
        self.assertIn('중추신경 <b>67%</b>', page)
        self.assertIn('NEW', page)
        self.assertIn('강릉아산병원', filtered); self.assertNotIn('<b>안동병원</b>', filtered)


if __name__ == '__main__':
    unittest.main()
