"""상담일지 입력 칸 — 기저질환 '기타' 자유 기재, 보험유형 복수 선택 (2026-09-14)."""
import os
import tempfile
import unittest
from unittest.mock import patch

import app as main
import models
from config import DISEASES_GROUPS


class ConsultFormFieldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 'test.db'))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        models.ensure_admin_user('fieldtest', 'test-only-password', display_name='테스트')
        user = models.get_user('fieldtest')
        boot = patch.object(main, '_db_initialized', True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=user['id'], username='fieldtest', role='admin',
                           perms={k: 3 for k in main.MENU_KEYS})

    def _create(self, name, insurance, **consult):
        payload = {
            'patient': {'name': name, 'gender': 'F', 'insurance_type': insurance},
            'consultation': {'consult_date': '2026-09-14', **consult},
        }
        r = self.client.post('/api/consult', json=payload)
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()['id']

    def test_chronic_other_is_a_text_field_not_a_disease(self):
        # 폼 렌더에 칸이 있고, 병명 그룹 목록에는 들어가지 않는다
        html = self.client.get('/consult/new').get_data(as_text=True)
        self.assertIn('name="consultation.chronic_other"', html)
        self.assertNotIn('기타', DISEASES_GROUPS['기저질환'])
        cid = self._create('기타환자', ['건강보험'], diseases=['고혈압'], chronic_other='갑상선기능저하증')
        con = models.get_consultation(cid)
        self.assertEqual(con['chronic_other'], '갑상선기능저하증')
        self.assertEqual(con['diseases'], ['고혈압'])
        detail = self.client.get(f'/consult/{cid}').get_data(as_text=True)
        self.assertIn('갑상선기능저하증', detail)
        edit = self.client.get(f'/consult/{cid}/edit').get_data(as_text=True)
        self.assertIn('value="갑상선기능저하증"', edit)
        # 수정으로 비우기
        r = self.client.post(f'/api/consult/{cid}', json={'consultation': {'chronic_other': ''}})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIsNone(models.get_consultation(cid)['chronic_other'])

    def test_multiple_insurance_types_saved_filtered_and_counted(self):
        cid = self._create('복수보험', ['장애', '건강보험'])       # 순서는 INSURANCE_TYPES 순으로 정리
        pid = models.get_consultation(cid)['patient_id']
        self.assertEqual(models.get_patient(pid)['insurance_type'], '건강보험, 장애')
        self._create('단일보험', ['자보'])
        self._create('보험없음', [])
        self.assertIsNone(models.get_patient(models.get_consultation(3)['patient_id'])['insurance_type'])
        # 목록 필터: 단일값으로 조합 안의 항목을 찾는다 (부분 문자열 오탐 없음: '장애' ≠ '장기요양')
        names = lambda ins: sorted(r['patient_name'] for r in models.list_consultations(insurance=ins, limit=100))
        self.assertEqual(names('건강보험'), ['복수보험'])
        self.assertEqual(names('장애'), ['복수보험'])
        self.assertEqual(names('자보'), ['단일보험'])
        self.assertEqual(names('장기요양'), [])
        # 수정 화면은 저장된 조합을 모두 체크한 채로 연다
        edit = self.client.get(f'/consult/{cid}/edit').get_data(as_text=True)
        for t, checked in (('건강보험', True), ('장애', True), ('자보', False)):
            row = next(l for l in edit.splitlines() if f'name="patient.insurance_type[]" value="{t}"' in l)
            self.assertEqual('checked' in row + edit.splitlines()[edit.splitlines().index(row) + 1], checked, t)
        # 통계: 조합은 유형별로 각각 1건
        stats = models.aggregate_stats('2026-09-01', '2026-09-30')
        by_ins = {x['label']: x['count'] for x in stats['by_insurance']}
        self.assertEqual(by_ins, {'건강보험': 1, '장애': 1, '자보': 1})
        # 수정 API로 바꾸기 — 리스트도 문자열도 받는다
        r = self.client.post(f'/api/consult/{cid}', json={'patient': {'insurance_type': ['산재']}})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(models.get_patient(pid)['insurance_type'], '산재')
        r = self.client.post(f'/api/consult/{cid}', json={'patient': {'insurance_type': '의료급여'}})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(models.get_patient(pid)['insurance_type'], '의료급여')


if __name__ == '__main__':
    unittest.main()
