"""admin 계정의 비밀번호가 재기동마다 .env 초기값으로 되돌아가지 않는지.

2026-09-14 운영에서 관리자가 사용자 관리로 비밀번호를 바꿔도 배포(컨테이너 재기동)마다
초기 비밀번호로 원복됐다. ensure_admin_user가 매 부팅 ON CONFLICT DO UPDATE로 해시·표시명·
권한을 덮어썼기 때문이다. 이제는 없을 때만 만들고, force(APP_PASSWORD_RESET=1)일 때만 되돌린다.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.security import check_password_hash

import models


class AdminPasswordSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "sync.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()

    def _admin(self):
        return models.get_user("admin")

    def test_first_boot_creates_admin_with_env_password(self):
        models.ensure_admin_user("admin", "initial-pw", display_name="admin(비상)")
        u = self._admin()
        self.assertTrue(check_password_hash(u["password_hash"], "initial-pw"))
        self.assertEqual((u["display_name"], u["role"], u["active"]), ("admin(비상)", "admin", 1))

    def test_reboot_keeps_changed_password_display_name_and_permissions(self):
        """사용자 관리에서 바꾼 값은 다음 기동(ensure_admin_user 재호출)에도 그대로다."""
        models.ensure_admin_user("admin", "initial-pw", display_name="admin(비상)")
        uid = self._admin()["id"]
        models.set_user_password(uid, "changed-by-admin")
        models.update_user(uid, "관리자", "admin", permissions={"sms": 1})
        # 다음 배포 — .env 값은 여전히 초기 비밀번호
        models.ensure_admin_user("admin", "initial-pw", display_name="admin(비상)")
        u = self._admin()
        self.assertTrue(check_password_hash(u["password_hash"], "changed-by-admin"))
        self.assertFalse(check_password_hash(u["password_hash"], "initial-pw"))
        self.assertEqual(u["display_name"], "관리자")
        self.assertEqual(u["perms"]["sms"], 1)

    def test_force_reset_restores_env_password_and_reactivates(self):
        """APP_PASSWORD_RESET=1 — 분실 복구 때만 비밀번호·표시명·권한·활성 상태를 되돌린다."""
        models.ensure_admin_user("admin", "initial-pw", display_name="admin(비상)")
        uid = self._admin()["id"]
        models.set_user_password(uid, "forgotten")
        models.update_user(uid, "관리자", "admin", permissions={"sms": 1})
        models.set_user_active(uid, False)
        models.ensure_admin_user("admin", "initial-pw", display_name="admin(비상)", force=True)
        u = self._admin()
        self.assertTrue(check_password_hash(u["password_hash"], "initial-pw"))
        self.assertEqual((u["display_name"], u["active"], u["perms"]["sms"]), ("admin(비상)", 1, 3))


if __name__ == "__main__":
    unittest.main()
