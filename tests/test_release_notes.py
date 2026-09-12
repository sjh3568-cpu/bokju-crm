"""실제 DB 없이 릴리스 공지의 중복 방지와 게시 정책을 검증한다."""
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch
import release_notes


class ReleaseNoticeTests(unittest.TestCase):
    def test_concurrent_startup_and_hidden_notice(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'test.db')
            def connect():
                return sqlite3.connect(path, timeout=10)
            with closing(connect()) as conn, conn:
                conn.execute('''CREATE TABLE announcements (
                    id INTEGER PRIMARY KEY, title TEXT, body TEXT,
                    target_role TEXT, requires_ack INTEGER,
                    created_by_name TEXT, active INTEGER DEFAULT 1)''')
            with patch.object(release_notes.models, 'get_db', side_effect=connect):
                with ThreadPoolExecutor(max_workers=3) as workers:
                    list(workers.map(lambda _: release_notes.publish_release_notes(), range(3)))
                with closing(connect()) as conn, conn:
                    rows = conn.execute('SELECT target_role, requires_ack FROM announcements').fetchall()
                    self.assertEqual(rows, [('all', 0)] * len(release_notes.RELEASE_NOTES))
                    conn.execute('UPDATE announcements SET active=0')
                release_notes.publish_release_notes()
                with closing(connect()) as conn, conn:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM announcements WHERE active=1').fetchone()[0], 0)
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM announcements').fetchone()[0], len(rows))

    def test_version_requires_matching_notice(self):
        with patch.object(release_notes, 'APP_VERSION', '99.0.0'):
            with self.assertRaises(ValueError):
                release_notes.publish_release_notes()
