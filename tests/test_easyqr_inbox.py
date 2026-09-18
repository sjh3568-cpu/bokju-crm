"""EasyQR 전화상담 접수 폴링 → 인박스 (2026-09-17, 2026-09-18 API 전환).

EasyQR API 서버는 테스트 환경에 없으므로 _fetch()를 가짜(메모리 목록)로 갈아끼운다.
검증 대상은 '무엇을 가져오는가'가 아니라 '무엇을 인박스에 넣는가'다.
_fetch 자체(헤더·파라미터·success 판정)는 requests.get을 흉내내 따로 본다.
"""
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import easyqr_inbox
import models

_ENV = {
    "EASYQR_API_URL": "http://172.16.1.250/Developer/EasyQR/api/consult_export.php",
    "EASYQR_API_KEY": "test-key",
}


def _row(rid, name="홍길동", phone="01012345678", content="재활 입원 문의드립니다", **kw):
    row = {
        "id": rid, "name": name, "phone": phone, "content": content,
        "available_time": "오후(13~17시)", "address": "안동시 풍산읍",
        "patient_age": 78, "status": "pending", "memo": None,
        "created_at": "2026-09-17 10:30:00",           # API는 문자열로 준다
        "updated_at": "2026-09-17 10:30:00",
    }
    row.update(kw)
    return row


def _fake_fetch(rows):
    """API의 after_id/limit 의미를 그대로 흉내낸다 — id > after_id, 오름차순, limit개."""
    def fetch(after_id, limit=easyqr_inbox.BATCH_LIMIT):
        return [r for r in sorted(rows, key=lambda r: r["id"])
                if r["id"] > int(after_id)][:int(limit)]
    return fetch


class EasyQRInboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "test.db"))
        self.db_patch.start()
        models.init_db()
        self.status_path = os.path.join(self.tmp.name, "easyqr_sync_status.json")
        self.sp = patch.object(easyqr_inbox, "STATUS_PATH", __import__("pathlib").Path(self.status_path))
        self.sp.start()
        self.env = patch.dict(os.environ, _ENV); self.env.start()

    def tearDown(self):
        self.env.stop(); self.sp.stop(); self.db_patch.stop(); self.tmp.cleanup()

    def _poll(self, rows):
        with patch.object(easyqr_inbox, "_fetch", side_effect=_fake_fetch(rows)):
            return easyqr_inbox.poll_once()

    def _comms(self):
        conn = models.get_db()
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM communications ORDER BY id").fetchall()]
        conn.close()
        return rows

    def _status(self) -> dict:
        with open(self.status_path, encoding="utf-8") as fh:
            return json.load(fh)

    def _seed(self, rows, from_id=0):
        """첫 기동 기준점을 from_id로 고정한 뒤 폴링."""
        with patch.dict(os.environ, {"EASYQR_BACKFILL_FROM": str(from_id)}):
            return self._poll(rows)

    # ── 적재 내용 ──────────────────────────────────────────────

    def test_new_row_becomes_inbox_card(self):
        self.assertEqual(self._seed([_row(19)]), 1)
        comm = self._comms()[0]
        self.assertEqual(comm["channel"], "카카오")          # 카카오 채널 버튼이 여는 페이지 → 카카오톡으로 분류
        self.assertEqual(comm["direction"], "in")
        self.assertEqual(comm["status"], "open")
        self.assertEqual(comm["created_by"], "EasyQR")
        self.assertEqual(comm["summary"], "전화상담 신청 #19 · 홍길동")
        self.assertEqual(comm["contact"], "010-1234-5678")        # 하이픈 정규화
        self.assertEqual(comm["occurred_at"], "2026-09-17 10:30:00")   # 접수 시각(문자열 그대로), 수집 시각 아님
        for line in ("재활 입원 문의드립니다", "[연락가능시간] 오후(13~17시)",
                     "[거주지] 안동시 풍산읍", "[환자나이] 78"):
            self.assertIn(line, comm["body"])

    def test_missing_age_is_not_shown_as_zero(self):
        self._seed([_row(1, patient_age=0)])
        self.assertNotIn("[환자나이]", self._comms()[0]["body"])

    def test_phone_matches_patient(self):
        pid = models.find_or_create_patient(name="김복주", guardian_phone="010-1234-5678")
        self._seed([_row(1)])
        self.assertEqual(self._comms()[0]["patient_id"], pid)

    def test_unmatched_phone_still_registers(self):
        self._seed([_row(1, phone="0559991234")])
        comm = self._comms()[0]
        self.assertIsNone(comm["patient_id"])
        self.assertEqual(comm["contact"], "0559991234")

    # ── 중복 방지 ──────────────────────────────────────────────

    def test_second_poll_registers_nothing_new(self):
        rows = [_row(1), _row(2, name="이순신")]
        self.assertEqual(self._seed(rows), 2)
        self.assertEqual(self._poll(rows), 0)              # 워터마크로 차단
        self.assertEqual(len(self._comms()), 2)

    def test_only_rows_after_watermark_are_fetched(self):
        rows = [_row(1), _row(2)]
        self._seed(rows)
        rows.append(_row(3, name="유관순"))
        self.assertEqual(self._poll(rows), 1)
        self.assertEqual(self._comms()[-1]["summary"], "전화상담 신청 #3 · 유관순")

    def test_lost_status_file_does_not_duplicate(self):
        """상태 파일이 사라져도 접수번호 검사가 2차 방어선이 된다."""
        rows = [_row(1), _row(2)]
        self._seed(rows)
        os.remove(self.status_path)
        self.assertEqual(self._seed(rows), 0)
        self.assertEqual(len(self._comms()), 2)

    def test_rows_saved_as_web_before_channel_switch_are_not_duplicated(self):
        """9/18 웹문의→카카오 전환 전에 들어온 행도 접수번호 대조에 걸려야 한다 + init_db가 카카오로 이관."""
        self._seed([_row(5, name="신사임당")])
        conn = models.get_db()
        conn.execute("UPDATE communications SET channel = '웹문의' WHERE created_by = 'EasyQR'")
        conn.commit(); conn.close()
        os.remove(self.status_path)
        self.assertEqual(self._seed([_row(5, name="신사임당")]), 0)
        models.init_db()                                     # 재기동 = 1회성 이관
        self.assertEqual([c["channel"] for c in self._comms()], ["카카오"])

    def test_receipt_number_prefix_is_not_confused(self):
        """#1 이 등록돼 있어도 #12 는 새 건으로 들어가야 한다."""
        self._seed([_row(1)])
        os.remove(self.status_path)
        self.assertEqual(self._seed([_row(1), _row(12, name="장영실")]), 1)
        self.assertEqual(self._comms()[-1]["summary"], "전화상담 신청 #12 · 장영실")

    # ── 첫 기동 기준점 ────────────────────────────────────────

    def test_first_run_skips_existing_backlog(self):
        """백필 지정이 없으면 기존 접수는 가져오지 않는다(인박스 폭탄 방지)."""
        self.assertEqual(self._poll([_row(1), _row(2), _row(3)]), 0)
        self.assertEqual(self._comms(), [])
        self.assertEqual(self._status()["last_id"], 3)

    def test_first_run_pages_past_one_full_batch(self):
        """API엔 MAX(id)가 없어 페이지를 넘겨 끝을 찾는다 — 한 페이지(500)보다 많아도."""
        rows = [_row(i) for i in range(1, easyqr_inbox.BOOTSTRAP_LIMIT + 3)]
        self.assertEqual(self._poll(rows), 0)
        self.assertEqual(self._status()["last_id"], easyqr_inbox.BOOTSTRAP_LIMIT + 2)

    def test_backfill_from_zero_takes_everything(self):
        self.assertEqual(self._seed([_row(1), _row(2)], from_id=0), 2)

    def test_backfill_from_id_takes_only_later_rows(self):
        self.assertEqual(self._seed([_row(1), _row(2), _row(3)], from_id=1), 2)

    # ── 장애 내성 ──────────────────────────────────────────────

    def test_disabled_without_api_key(self):
        with patch.dict(os.environ, {"EASYQR_API_KEY": ""}):
            self.assertEqual(easyqr_inbox.poll_once(), 0)

    def test_query_failure_keeps_watermark(self):
        self._seed([_row(1)])
        before = self._status()["last_id"]
        with patch.object(easyqr_inbox, "_fetch", side_effect=OSError("down")):
            self.assertEqual(easyqr_inbox.poll_once(), 0)
        st = self._status()
        self.assertEqual(before, st["last_id"])
        self.assertFalse(st["ok"])
        self.assertIn("down", st["error"])

    def test_failing_row_is_retried_then_skipped(self):
        """한 건이 계속 실패해도 뒤에 쌓인 접수까지 막히면 안 된다."""
        rows = [_row(1), _row(2, name="이순신")]
        real = easyqr_inbox._register

        def boom(row):
            if int(row["id"]) == 1:
                raise RuntimeError("등록 실패")
            return real(row)

        with patch.dict(os.environ, {"EASYQR_BACKFILL_FROM": "0"}):
            with patch.object(easyqr_inbox, "_register", side_effect=boom):
                for _ in range(easyqr_inbox.MAX_STRIKES - 1):
                    self.assertEqual(self._poll(rows), 0)   # #1에 막혀 뒤로 못 간다
                self.assertEqual(self._comms(), [])
                self._poll(rows)                            # MAX_STRIKES번째 — 건너뛴다
        self.assertEqual([c["summary"] for c in self._comms()],
                         ["전화상담 신청 #2 · 이순신"])       # #1은 버려지고 #2만 들어온다


class EasyQRFetchTests(unittest.TestCase):
    """_fetch — API 명세대로 부르고, HTTP 코드가 아니라 success 필드로 판단하는지."""

    def setUp(self):
        self.env = patch.dict(os.environ, _ENV); self.env.start()

    def tearDown(self):
        self.env.stop()

    def _get(self, payload, status=200):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = payload
        resp.raise_for_status.side_effect = None if status < 400 else OSError(f"HTTP {status}")
        return patch("requests.get", return_value=resp)

    def test_sends_key_header_and_after_id(self):
        with self._get({"success": True, "count": 1, "max_id": 7, "items": [_row(7)]}) as get:
            rows = easyqr_inbox._fetch(6, 50)
        self.assertEqual([r["id"] for r in rows], [7])
        kwargs = get.call_args.kwargs
        self.assertEqual(get.call_args.args[0], _ENV["EASYQR_API_URL"])
        self.assertEqual(kwargs["headers"]["X-API-Key"], "test-key")
        self.assertIn("User-Agent", kwargs["headers"])                 # 외부 도메인(Cloudflare) 대비
        self.assertEqual(kwargs["params"], {"after_id": 6, "limit": 50, "client": "bokju-crm"})
        self.assertLessEqual(kwargs["timeout"], 15)

    def test_success_false_is_an_error_even_with_http_200(self):
        """서버 nginx가 4xx를 가로채므로 인증 실패도 200으로 온다."""
        with self._get({"success": False, "error": "인증 실패", "code": "unauthorized"}):
            with self.assertRaises(RuntimeError) as cm:
                easyqr_inbox._fetch(0)
        self.assertIn("unauthorized", str(cm.exception))

    def test_empty_items_is_not_an_error(self):
        with self._get({"success": True, "count": 0, "max_id": 0, "items": []}):
            self.assertEqual(easyqr_inbox._fetch(99), [])

    def test_http_error_raises(self):
        with self._get({}, status=502):
            with self.assertRaises(OSError):
                easyqr_inbox._fetch(0)


if __name__ == "__main__":
    unittest.main()
