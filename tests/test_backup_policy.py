"""백업 보관 규칙 — 압축 저장, daily 30일, startup 5개, 옛 manual_배포전은 전부 삭제, 1회성 pre_*는 보존."""
import gzip
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import backup
import models


class BackupPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for target, value in (("DB_PATH", str(root / "bokju.db")),):
            p = patch.object(models, target, value); p.start(); self.addCleanup(p.stop)
        self.bdir = root / "backups"
        p = patch.object(backup, "BACKUP_DIR", self.bdir); p.start(); self.addCleanup(p.stop)
        models.init_db()

    def _touch(self, name, days_ago=0):
        self.bdir.mkdir(exist_ok=True)
        f = self.bdir / name
        f.write_bytes(b"x")
        ts = time.time() - days_ago * 86400
        os.utime(f, (ts, ts))
        return f

    def test_backup_is_gzipped_and_restorable(self):
        out = backup.run_backup("startup")
        self.assertIsNotNone(out)
        self.assertTrue(out.name.startswith("bokju_startup_") and out.name.endswith(".db.gz"))
        self.assertEqual(sorted(p.name for p in self.bdir.iterdir()), [out.name])  # 원본 .db는 지움
        with gzip.open(out, "rb") as fh:
            self.assertTrue(fh.read(16).startswith(b"SQLite format 3"))
        status = backup.latest_status()
        self.assertEqual((status["file_count"], status["latest_file"]), (1, out.name))
        self.assertTrue(backup.verify_latest_restore()["ok"])

    def test_prune_keeps_recent_startup_manual_and_daily_window(self):
        for i in range(8):
            self._touch(f"bokju_startup_2026091{i}_000000.db.gz", days_ago=8 - i)
        for i in range(5):
            self._touch(f"manual_배포전_2026091{i}_000000.db", days_ago=5 - i)
        self._touch("bokju_daily_20260801_030000.db.gz", days_ago=45)   # 30일 지남
        self._touch("bokju_daily_20260901_030000.db", days_ago=15)      # 옛 무압축도 같은 규칙
        self._touch("bokju_daily_20260915_030000.db.gz", days_ago=1)
        self._touch("pre_excel_import_20260701.db", days_ago=77)        # 1회성 — 손대지 않음
        backup._prune()
        left = sorted(p.name for p in self.bdir.iterdir())
        self.assertEqual(backup.KEEP_STARTUP, 5)
        self.assertEqual([n for n in left if n.startswith("bokju_startup_")],
                         [f"bokju_startup_2026091{i}_000000.db.gz" for i in (3, 4, 5, 6, 7)])  # 최근 5개
        self.assertEqual(backup.KEEP_MANUAL, 0)   # deploy.sh가 더는 만들지 않으므로 남은 것도 정리
        self.assertEqual([n for n in left if n.startswith("manual_배포전_")], [])
        self.assertNotIn("bokju_daily_20260801_030000.db.gz", left)
        self.assertIn("bokju_daily_20260901_030000.db", left)
        self.assertIn("pre_excel_import_20260701.db", left)

    def test_prune_never_deletes_last_daily(self):
        self._touch("bokju_daily_20260101_030000.db.gz", days_ago=200)
        backup._prune()
        self.assertTrue((self.bdir / "bokju_daily_20260101_030000.db.gz").exists())


if __name__ == "__main__":
    unittest.main()
