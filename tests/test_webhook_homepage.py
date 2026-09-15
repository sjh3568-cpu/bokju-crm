"""홈페이지 문의 웹훅 — EasyQR '빠른 전화상담 신청'(consult.php) 페이로드 수용 (2026-09-15)."""
import os
import tempfile
import unittest
from unittest.mock import patch

import app as main
import models


class HomepageWebhookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "test.db"))
        self.db_patch.start()
        models.init_db()
        self.boot = patch.object(main, "_db_initialized", True); self.boot.start()
        self.env = patch.dict(os.environ, {"HOMEPAGE_WEBHOOK_TOKEN": "t0ken", "WEBHOOK_ALLOW_IPS": ""}); self.env.start()
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()

    def tearDown(self):
        self.env.stop(); self.boot.stop(); self.db_patch.stop(); self.tmp.cleanup()

    def _post(self, payload, token="t0ken"):
        return self.client.post("/api/webhook/homepage", json=payload, headers={"X-Webhook-Token": token})

    def test_easyqr_payload_kept_with_labels(self):
        r = self._post({"name": "홍길동", "phone": "01012345678", "available_time": "오후(13~17시)",
                        "address": "안동시 풍산읍", "patient_age": "78",
                        "content": "뇌졸중 재활 입원 문의드립니다", "receipt_no": 19,
                        "subject": "빠른 전화상담 신청"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        comm = models.get_communication(r.get_json()["id"])
        self.assertEqual(comm["channel"], "웹문의")
        self.assertEqual(comm["status"], "open")
        self.assertEqual(comm["contact"], "010-1234-5678")            # 정규화
        self.assertEqual(comm["summary"], "빠른 전화상담 신청 #19 · 홍길동")
        for line in ("뇌졸중 재활 입원 문의드립니다", "[연락가능시간] 오후(13~17시)", "[거주지] 안동시 풍산읍", "[환자나이] 78"):
            self.assertIn(line, comm["body"])
        self.assertEqual(models.open_inbound_count(), 1)

    def test_legacy_payload_and_auth(self):
        r = self._post({"name": "김문의", "phone": "010-9999-8888", "message": "입원 가능한가요"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(models.get_communication(r.get_json()["id"])["summary"], "홈페이지 문의 · 김문의")
        self.assertEqual(self._post({"message": "x"}, token="wrong").status_code, 401)
        self.assertEqual(self._post({"name": "x", "phone": "010-1-1"}).status_code, 400)   # message 없음


if __name__ == "__main__":
    unittest.main()
