import os, tempfile, unittest
from unittest.mock import patch
import models
import app as main

class ConsultationDisplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 'test.db'))
        self.db.start(); self.addCleanup(self.db.stop)
        models.init_db()
        models.ensure_admin_user('displaytest', 'test-only-password', display_name='테스트')
        user = models.get_user('displaytest')
        self.initialized = patch.object(main, '_db_initialized', True)
        self.initialized.start(); self.addCleanup(self.initialized.stop)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=user['id'], username='displaytest', role='admin', perms={k:3 for k in main.MENU_KEYS})
        pid = models.find_or_create_patient(name='표시검증환자', guardian_phone=None)
        self.cid = models.create_consultation(patient_id=pid, consult_date='2026-09-12', source_hospital='근로복지공단 동해병원', disease_onset='2026-01-02', actual_admission_date='2026-02-03', admission_status='입원완료')

    def test_hospital_alias_and_legacy_form(self):
        for query in ['동해병원', '근로복지공단동해병원', '근로복지공단 동해병원']:
            result = self.client.get('/api/autocomplete/hospital', query_string={'q':query})
            self.assertEqual(result.status_code, 200)
            self.assertIn('근로복지공단 동해병원', [r['name'] for r in result.json['items']])
        for path in [f'/consult/{self.cid}', f'/consult/{self.cid}/edit']:
            result = self.client.get(path)
            self.assertEqual(result.status_code, 200)
            self.assertIn('근로복지공단 동해병원', result.get_data(as_text=True))

    def test_list_dates_and_column_alignment(self):
        from html.parser import HTMLParser
        class TableParser(HTMLParser):
            def __init__(self): super().__init__(); self.rows=[]; self.in_row=False
            def handle_starttag(self,tag,attrs):
                if tag=='tr': self.rows.append(0); self.in_row=True
                if self.in_row and tag in ('th','td'): self.rows[-1]+=1
            def handle_endtag(self,tag):
                if tag=='tr': self.in_row=False
        result = self.client.get('/consultations')
        self.assertEqual(result.status_code,200)
        html=result.get_data(as_text=True)
        self.assertLess(html.index('<th>발병일</th>'),html.index('<th>입원완료일</th>'))
        self.assertIn('26.01.02',html)
        parser=TableParser();parser.feed(html)
        self.assertEqual(parser.rows[:3],[19,19,19])

if __name__=='__main__': unittest.main()
