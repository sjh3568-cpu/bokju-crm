"""발병일 칸은 브라우저 기본 달력만 — 빠른 날짜 선택 창을 붙이지 않는다 (2026-09-28 사용자 요청).

발병일은 몇 달·몇 년 전 하루를 고르는 칸이라 '1주 전·1개월 후' 같은 빠른 버튼이 쓸모없고,
선택 창 안의 달력을 한 번 더 눌러야 했다. 기간(시작~끝) 칸의 빠른 조회는 그대로 둔다.
date-presets.js는 input의 data-date-presets="off"를 보고 그 칸을 건너뛴다.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as main
import models

ROOT = Path(__file__).resolve().parent.parent


class OnsetNativeCalendarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "onset.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        models.ensure_admin_user("onset-test", "test-password", display_name="점검")
        uid = models.get_user("onset-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=uid, username="onset-test", display_name="점검", role="admin",
                           perms={k: 3 for k in main.MENU_KEYS})

    def test_consult_form_onset_opts_out(self):
        html = self.client.get("/consult/new").get_data(as_text=True)
        self.assertIn('id="onset-date" data-date-presets="off"', html)
        # 같은 폼의 다른 날짜 칸(상담일)은 빠른 선택 창을 그대로 쓴다
        self.assertNotIn('name="consultation.consult_date" data-date-presets="off"', html)

    def test_period_calc_onset_opts_out(self):
        html = self.client.get("/tools/period-calc").get_data(as_text=True)
        self.assertIn('name="onset" value="" required data-date-presets="off"', html)

    def test_script_honors_input_level_opt_out(self):
        js = (ROOT / "static/js/date-presets.js").read_text(encoding="utf-8")
        self.assertIn("input.dataset.datePresets === 'off'", js)


if __name__ == "__main__":
    unittest.main()
