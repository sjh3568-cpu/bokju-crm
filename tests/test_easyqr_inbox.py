"""EasyQR 전화상담 접수 폴링 → 인박스 (2026-09-17).

EasyQR MariaDB는 테스트 환경에 없으므로 _connect()를 가짜 커서로 갈아끼운다.
검증 대상은 '무엇을 가져오는가'가 아니라 '무엇을 인박스에 넣는가'다.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

import easyqr_inbox
import models

_ENV = {
    "EASYQR_DB_HOST": "127.0.0.1",
    "EASYQR_DB_USER": "bokjucrm_ro",
    "EASYQR_DB_PASS": "pw",
}


def _row(rid, name="홍길동", phone="01012345678", content="재활 입원 문의드립니다", **kw):
    row = {
        "id": rid, "name": name, "phone": phone, "content": content,
        "available_time": "오후(13~17시)", "address": "안동시 풍산읍",
        "patient_age": 78, "created_at": datetime(2026, 9, 17, 10, 30, 0),
    }
    row.update(kw)
    return row


class _FakeCursor:
    """easyqr_inbox가 실제로 쓰는 두 질의만 흉내낸다."""

    def __init__(self, rows):
        self._rows = rows
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        if "MAX(id)" in sql:
            self._result = [{"m": max([r["id"] for r in self._rows], default=0)}]
        else:
            last_id, limit = int(params[0]), int(params[1])
            self._result = [r for r in sorted(self._rows, key=lambda r: r["id"])
                            if r["id"] > last_id][:limit]

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)

    def close(self):
        pass


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
        with patch.object(easyqr_inbox, "_connect", return_value=_FakeConn(rows)):
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
        self.assertEqual(comm["channel"], "웹문의")
        self.assertEqual(comm["direction"], "in")
        self.assertEqual(comm["status"], "open")
        self.assertEqual(comm["created_by"], "EasyQR")
        self.assertEqual(comm["summary"], "전화상담 신청 #19 · 홍길동")
        self.assertEqual(comm["contact"], "010-1234-5678")        # 하이픈 정규화
        self.assertEqual(comm["occurred_at"], "2026-09-17 10:30:00")   # 수집 시각 아님
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

    def test_backfill_from_zero_takes_everything(self):
        self.assertEqual(self._seed([_row(1), _row(2)], from_id=0), 2)

    def test_backfill_from_id_takes_only_later_rows(self):
        self.assertEqual(self._seed([_row(1), _row(2), _row(3)], from_id=1), 2)

    # ── 장애 내성 ──────────────────────────────────────────────

    def test_disabled_without_credentials(self):
        with patch.dict(os.environ, {"EASYQR_DB_HOST": ""}):
            self.assertEqual(easyqr_inbox.poll_once(), 0)

    def test_query_failure_keeps_watermark(self):
        self._seed([_row(1)])
        before = self._status()["last_id"]
        with patch.object(easyqr_inbox, "_connect", side_effect=OSError("down")):
            self.assertEqual(easyqr_inbox.poll_once(), 0)
        self.assertEqual(before, self._status()["last_id"])

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


if __name__ == "__main__":
    unittest.main()
