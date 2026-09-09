import os
import tempfile
import unittest
from unittest.mock import patch
import app as main
import models
import support_requests as support


class SupportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db_patch=patch.object(models,'DB_PATH',os.path.join(self.tmp.name,'test.db'))
        self.db_patch.start()
        models.init_db(); support.init_schema()
        models.ensure_admin_user('support-test','test-password',display_name='테스트')
        self.uid=models.get_user('support-test')['id']
        self.boot=patch.object(main,'_db_initialized',True);self.boot.start()
        self.notice=patch.object(models,'first_unread_required_announcement',return_value=None);self.notice.start()
        main.app.config.update(TESTING=True)
        self.client=main.app.test_client()
        self.login()

    def tearDown(self):
        self.notice.stop();self.boot.stop();self.db_patch.stop();self.tmp.cleanup()

    def login(self,role='staff',uid=None):
        with self.client.session_transaction() as s:
            s.clear();s.update(user_id=uid or self.uid,username='support-test',display_name='테스트',role=role,
                perms={k:0 for k in main.MENU_KEYS},cooperation_permissions_v2=True,support_csrf='test-token')

    def create(self):
        return self.client.post('/support/',data={'csrf':'test-token','category':'기능 개선','title':'검색 개선','body':'<script>alert(1)</script>'})

    def test_registration_privacy_reply_and_filters(self):
        response=self.create();self.assertEqual(response.status_code,302)
        url=response.location
        page=self.client.get(url);self.assertEqual(page.status_code,200)
        self.assertIn('&lt;script&gt;',page.get_data(as_text=True))
        self.assertEqual(self.client.post(url,data={'csrf':'test-token','status':'완료','body':'처리'}).status_code,403)
        self.login(uid=self.uid+100)
        self.assertEqual(self.client.get(url).status_code,404)
        self.assertNotIn('검색 개선',self.client.get('/support/').get_data(as_text=True))
        self.login(role='admin')
        self.assertEqual(self.client.post(url,data={'csrf':'test-token','status':'완료','body':'개선했습니다.'}).status_code,302)
        self.login()
        self.assertIn('개선했습니다.',self.client.get(url).get_data(as_text=True))
        self.assertIn('검색 개선',self.client.get('/support/?status=완료').get_data(as_text=True))
        self.assertNotIn('검색 개선',self.client.get('/support/?status=접수').get_data(as_text=True))

    def test_validation_csrf_and_login(self):
        self.assertEqual(self.client.post('/support/',data={'title':'x'}).status_code,403)
        self.assertEqual(self.client.post('/support/',data={'csrf':'test-token','category':'잘못된 유형','title':'x','body':'y'}).status_code,400)
        self.assertEqual(self.client.get('/support/?status=invalid').status_code,400)
        self.assertEqual(self.create().status_code,302)
        self.login(role='admin')
        self.assertEqual(self.client.post('/support/1',data={'csrf':'test-token','status':'완료','body':' '}).status_code,400)
        with self.client.session_transaction() as s:s.clear()
        self.assertEqual(self.client.get('/support/').status_code,302)

    def test_combined_filters_pagination_and_counts(self):
        with models.get_db() as db:
            for i in range(23):
                db.execute('INSERT INTO support_requests (user_id,category,title,body,version,status) VALUES (?,?,?,?,?,?)',
                           (self.uid,'오류 신고',f'필터대상 {i}','검색본문','1.3.0','검토 중'))
            db.execute('INSERT INTO support_requests (user_id,category,title,body,version,status) VALUES (?,?,?,?,?,?)',
                       (self.uid,'기능 개선','제외대상','검색본문','1.3.0','완료'))
        params={'status':'검토 중','category':'오류 신고','q':'검색본문'}
        page=self.client.get('/support/',query_string=params)
        html=page.get_data(as_text=True)
        self.assertEqual(page.status_code,200)
        self.assertIn('검색 결과 23건',html)
        self.assertEqual(html.count('class="support-title"'),20)
        self.assertNotIn('제외대상',html)
        self.assertIn('category=',html);self.assertIn('q=',html)
        html=self.client.get('/support/',query_string={**params,'page':2}).get_data(as_text=True)
        self.assertEqual(html.count('class="support-title"'),3)
        self.login(uid=self.uid+100)
        html=self.client.get('/support/',query_string=params).get_data(as_text=True)
        self.assertIn('검색 결과 0건',html)
        self.assertNotIn('필터대상',html)
        self.assertEqual(self.client.get('/support/?category=invalid').status_code,400)
