"""부재중 → 재연락 예약 (인바운드 문의, 2026-09-15).
전화했는데 안 받은 문의를 status='waiting' + follow_up_at으로 돌리고,
시각이 되기 전엔 배지·알림·액션큐에서 빠졌다가 시각이 되면 다시 올라오는지 확인."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import app as main
import models
import partnerships
import support_requests


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


class InboundMissedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "test.db"))
        self.db_patch.start()
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("missed-test", "test-password", display_name="테스트")
        self.uid = models.get_user("missed-test")["id"]
        self.boot = patch.object(main, "_db_initialized", True); self.boot.start()
        self.notice = patch.object(models, "first_unread_required_announcement", return_value=None); self.notice.start()
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as s:
            s.clear()
            s.update(user_id=self.uid, username="missed-test", display_name="테스트", role="admin",
                     perms={k: 1 for k in main.MENU_KEYS})
        self.cid = models.create_communication(
            channel="카카오채널", direction="in", contact="010-1234-5678",
            summary="카카오 상담신청 · 홍길동", body="상담내용: 입원 문의")

    def tearDown(self):
        self.notice.stop(); self.boot.stop(); self.db_patch.stop(); self.tmp.cleanup()

    def _missed(self, when):
        return self.client.post(f"/api/communication/{self.cid}/missed", json={"follow_up_at": when})

    def test_future_callback_hides_until_due(self):
        later = _fmt(datetime.now() + timedelta(hours=2))
        r = self._missed(later)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["missed_count"], 1)

        comm = models.get_communication(self.cid)
        self.assertEqual(comm["status"], "waiting")
        self.assertEqual(comm["follow_up_at"], later)
        self.assertIn("[부재중 1회", comm["body"])
        self.assertIn("테스트", comm["body"])          # 누가 걸었는지 남김

        # 시각 전: 배지 0, 알림 없음, 인바운드 카드엔 남아 있음(재연락 예약 표시)
        self.assertEqual(models.open_inbound_count(), 0)
        alerts = self.client.get("/api/inbound/alerts").get_json()
        self.assertEqual(alerts["count"], 0)
        rows = models.inbox_open_communications()
        self.assertEqual([m["id"] for m in rows], [self.cid])
        self.assertFalse(rows[0]["callback_due"])
        self.assertEqual(rows[0]["missed_count"], 1)

    def test_due_callback_resurfaces_and_counts(self):
        # 2번째 부재중까지 기록한 뒤, 시각이 지난 상태로 만든다
        self._missed(_fmt(datetime.now() + timedelta(hours=1)))
        r = self._missed(_fmt(datetime.now() + timedelta(hours=1)))
        self.assertEqual(r.get_json()["missed_count"], 2)
        past = _fmt(datetime.now() - timedelta(minutes=3))
        conn = models.get_db()
        conn.execute("UPDATE communications SET follow_up_at=? WHERE id=?", (past, self.cid))
        conn.commit(); conn.close()

        self.assertEqual(models.open_inbound_count(), 1)
        rows = models.inbox_open_communications()
        self.assertTrue(rows[0]["callback_due"])
        self.assertEqual(rows[0]["missed_count"], 2)

        alerts = self.client.get("/api/inbound/alerts").get_json()
        self.assertEqual(alerts["count"], 1)
        item = alerts["items"][0]
        self.assertEqual(item["bucket"], "재연락")
        self.assertEqual(item["id"], f"cb{self.cid}@{past}")   # 시각과 묶여 새 알림으로 뜸
        self.assertIn("부재 2회", item["title"])

        # 액션큐에도 '재연락' 종류로 올라온다
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("부재 2회", page)
        self.assertIn("ib-missed", page)

    def test_validation(self):
        self.assertEqual(self._missed("어제").status_code, 400)
        self.assertEqual(self._missed(_fmt(datetime.now() - timedelta(hours=1))).status_code, 400)
        # datetime-local 형식(T 구분자)도 받는다
        r = self._missed((datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"))
        self.assertEqual(r.status_code, 200)
        models.update_communication(self.cid, status="done")
        self.assertEqual(self._missed(_fmt(datetime.now() + timedelta(hours=1))).status_code, 400)

    def test_consult_from_waiting_closes_it(self):
        self._missed(_fmt(datetime.now() + timedelta(hours=1)))
        page = self.client.get(f"/consult/new?comm_id={self.cid}")
        self.assertEqual(page.status_code, 200)     # waiting 상태도 상담 등록 prefill 가능
        self.assertEqual(self.client.post(f"/api/communication/{self.cid}/done").status_code, 200)
        self.assertEqual(models.get_communication(self.cid)["status"], "done")
        self.assertEqual(models.inbox_open_communications(), [])


if __name__ == "__main__":
    unittest.main()
