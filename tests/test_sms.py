import os
import tempfile
import unittest
from unittest.mock import patch

import app as main
import models
import sms


class MessageTypeTests(unittest.TestCase):
    def test_korean_counts_two_bytes(self):
        self.assertEqual(sms.body_bytes("가나다"), 6)
        self.assertEqual(sms.body_bytes("abc 12"), 6)

    def test_boundaries(self):
        self.assertEqual(sms.message_type("가" * 45), ("SMS", 90))
        self.assertEqual(sms.message_type("가" * 46), ("LMS", 92))
        self.assertEqual(sms.message_type("가" * 1000), ("LMS", 2000))
        self.assertEqual(sms.message_type("가" * 1001)[0], "TOO_LONG")

    def test_emoji_counts_two(self):
        self.assertEqual(sms.body_bytes("😀"), 2)

    def test_normalize_phone(self):
        self.assertEqual(sms.normalize_phone("010-1234-5678"), "01012345678")


class SendSmsTests(unittest.TestCase):
    ENV = {"SMS_PROVIDER": "aligo", "SMS_API_KEY": "k", "SMS_API_USER": "u",
           "SMS_SENDER": "054-550-1700"}

    def _post(self, result_code="1", error_cnt=0, msg_id="77"):
        class R:
            def raise_for_status(self): pass
            def json(self):
                return {"result_code": result_code, "message": "ok",
                        "msg_id": msg_id, "success_cnt": 1, "error_cnt": error_cnt}
        return R()

    def test_not_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(sms.gateway_configured())
            self.assertEqual(sms.send_sms("01011112222", "안녕")["status"], "not_configured")

    def test_sent_sms(self):
        with patch.dict(os.environ, self.ENV, clear=True), \
             patch.object(sms.requests, "post", return_value=self._post()) as post:
            r = sms.send_sms("010-1111-2222", "안녕하세요")
        self.assertEqual(r["status"], "sent")
        self.assertEqual(r["msg_type"], "SMS")
        self.assertEqual(r["provider_msg_id"], "77")
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["receiver"], "01011112222")
        self.assertEqual(data["sender"], "0545501700")
        self.assertEqual(data["msg_type"], "SMS")
        self.assertNotIn("title", data)

    def test_lms_gets_title(self):
        with patch.dict(os.environ, self.ENV, clear=True), \
             patch.object(sms.requests, "post", return_value=self._post()) as post:
            r = sms.send_sms("01011112222", "가" * 60)
        self.assertEqual(r["msg_type"], "LMS")
        self.assertIn("title", post.call_args.kwargs["data"])

    def test_test_redirect(self):
        env = {**self.ENV, "SMS_TEST_TO": "010-9999-0000"}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(sms.requests, "post", return_value=self._post()) as post:
            r = sms.send_sms("01011112222", "안녕")
        self.assertEqual(r["status"], "test")
        self.assertEqual(r["sent_to"], "01099990000")
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["receiver"], "01099990000")
        self.assertTrue(data["msg"].startswith("[테스트 → 01011112222]"))

    def test_provider_error(self):
        with patch.dict(os.environ, self.ENV, clear=True), \
             patch.object(sms.requests, "post", return_value=self._post(result_code="-101")):
            r = sms.send_sms("01011112222", "안녕")
        self.assertEqual(r["status"], "failed")
        self.assertIn("-101", r["error"])

    def test_bad_phone(self):
        with patch.dict(os.environ, self.ENV, clear=True), \
             patch.object(sms.requests, "post") as post:
            r = sms.send_sms("054-123-4567", "안녕")
        self.assertEqual(r["status"], "failed")
        post.assert_not_called()

    def test_connection_error(self):
        with patch.dict(os.environ, self.ENV, clear=True), \
             patch.object(sms.requests, "post", side_effect=sms.requests.ConnectionError("down")):
            r = sms.send_sms("01011112222", "안녕")
        self.assertEqual(r["status"], "failed")
        self.assertIn("연결 실패", r["error"])


class PpurioTests(unittest.TestCase):
    ENV = {"SMS_PROVIDER": "ppurio", "SMS_API_KEY": "secret", "SMS_API_USER": "bokju",
           "SMS_SENDER": "054-550-1700"}

    class _Resp:
        def __init__(self, payload, status=200, text=""):
            self._p, self.status_code, self.text = payload, status, text or str(payload)
        def raise_for_status(self): pass
        def json(self):
            if self._p is None: raise ValueError("no json")
            return self._p

    def test_token_then_message(self):
        seq = [self._Resp({"token": "tok", "type": "Bearer", "expired": "20260912000000"}),
               self._Resp({"code": 1000, "description": "성공", "messageKey": "K" * 33})]
        with patch.dict(os.environ, self.ENV, clear=True), \
             patch.object(sms.requests, "post", side_effect=seq) as post:
            r = sms.send_sms("010-1111-2222", "가" * 60)
        self.assertEqual(r["status"], "sent")
        self.assertEqual(r["provider_msg_id"], "K" * 33)
        tok_call, msg_call = post.call_args_list
        self.assertEqual(tok_call.args[0], "https://message.ppurio.com/v1/token")
        self.assertTrue(tok_call.kwargs["headers"]["Authorization"].startswith("Basic "))
        self.assertEqual(msg_call.args[0], "https://message.ppurio.com/v1/message")
        self.assertEqual(msg_call.kwargs["headers"]["Authorization"], "Bearer tok")
        body = msg_call.kwargs["json"]
        self.assertEqual(body["messageType"], "LMS")
        self.assertEqual(body["from"], "0545501700")
        self.assertEqual(body["targets"], [{"to": "01011112222"}])
        self.assertEqual(body["targetCount"], 1)
        self.assertIn("subject", body)

    def test_api_base_override(self):
        seq = [self._Resp({"token": "tok"}), self._Resp({"code": 1000, "messageKey": "k"})]
        with patch.dict(os.environ, {**self.ENV, "SMS_API_BASE": "https://example.test/"}, clear=True), \
             patch.object(sms.requests, "post", side_effect=seq) as post:
            sms.send_sms("01011112222", "안녕")
        self.assertEqual(post.call_args_list[0].args[0], "https://example.test/v1/token")

    def test_token_failure(self):
        with patch.dict(os.environ, self.ENV, clear=True), \
             patch.object(sms.requests, "post", return_value=self._Resp({"code": 4000}, text="denied")):
            r = sms.send_sms("01011112222", "안녕")
        self.assertEqual(r["status"], "failed")
        self.assertIn("토큰", r["error"])

    def test_message_error_code(self):
        seq = [self._Resp({"token": "tok"}),
               self._Resp({"code": 4001, "description": "발신번호 미등록"}, status=400)]
        with patch.dict(os.environ, self.ENV, clear=True), \
             patch.object(sms.requests, "post", side_effect=seq):
            r = sms.send_sms("01011112222", "안녕")
        self.assertEqual(r["status"], "failed")
        self.assertIn("발신번호 미등록", r["error"])


class SendApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 'test.db'))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        models.ensure_admin_user('sms-test', 'test-password', display_name='테스트')
        boot = patch.object(main, '_db_initialized', True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        self.client.post('/login', data={'username': 'sms-test', 'password': 'test-password'})

    def test_manual_mode_logs(self):
        with patch.dict(os.environ, {}, clear=True):
            res = self.client.post('/api/sms/send', json={"to_phone": "010-1111-2222", "body": "안녕하세요"})
        self.assertEqual(res.status_code, 200, res.data)
        j = res.get_json()
        self.assertEqual(j["status"], "manual")
        self.assertEqual(j["msg_type"], "SMS")
        log = models.list_sms_log(10)
        self.assertEqual(log[0]["status"], "manual")
        self.assertEqual(log[0]["msg_type"], "SMS")
        self.assertIsNone(log[0]["provider"])

    def test_too_long_rejected(self):
        res = self.client.post('/api/sms/send', json={"to_phone": "01011112222", "body": "가" * 1001})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(models.list_sms_log(10), [])

    def test_gateway_mode_logs_provider_fields(self):
        env = {"SMS_PROVIDER": "aligo", "SMS_API_KEY": "k", "SMS_SENDER": "0545501700",
               "SMS_TEST_TO": "01099990000"}
        fake = {"ok": True, "status": "test", "msg_type": "LMS", "error": None,
                "provider_msg_id": "abc", "sent_to": "01099990000"}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(sms, "send_sms", return_value=fake):
            res = self.client.post('/api/sms/send', json={"to_phone": "01011112222", "body": "가" * 60})
        j = res.get_json()
        self.assertEqual(j["status"], "test")
        row = models.list_sms_log(1)[0]
        self.assertEqual((row["provider"], row["provider_msg_id"], row["sent_to"], row["msg_type"]),
                         ("aligo", "abc", "01099990000", "LMS"))


if __name__ == '__main__':
    unittest.main()
