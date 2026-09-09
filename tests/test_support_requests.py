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
