"""사이드바 구역 제목 — 메뉴를 업무 단위로 묶어 보이게 (2026-09-25 사용자 요청).

최상위 11개가 한 줄로 늘어서 있어 무엇이 무엇인지 안 보인다는 요청. 클릭 수·메뉴 이름은
그대로 두고, 사이드바에 구역 제목(상담/환자·병상/분석/관리)만 얹는다.

여기서 지키는 것:
① 구역 제목 4개가 나온다.
② **링크가 하나도 사라지지 않는다** — 항목을 <div>로 다시 감싸다 빠뜨리는 게 이 작업의 유일한 위험.
③ 권한이 없으면 그 항목도, 항목이 다 빠진 구역의 제목도 함께 사라진다(빈 제목만 남지 않게).
"""
import os
import tempfile
import unittest
from unittest.mock import patch

import app as main
import models
import partnerships
import support_requests

SECTION_TITLES = ["상담", "환자·병상", "분석", "관리"]

# 최상위에 있어야 할 링크 — 구역으로 감싼 뒤에도 하나도 빠지면 안 된다.
TOP_LINKS = [
    'href="/"',                 # 대시보드 (구역 밖, 맨 위)
    'href="/consult/new"',      # 상담
    'href="/consultations"',
    'href="/documents"',
    'href="/sms"',
    'href="/ward"',             # 환자·병상
    'href="/partners"',
    'href="/stats"',            # 분석
    'href="/notices"',          # 관리
    'href="/support/"',
    'href="/admin/users"',
]


class NavSectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "nav.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("nav-test", "test-password", display_name="점검")
        self.uid = models.get_user("nav-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()

    def _login(self, perms=None):
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="nav-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms=perms if perms is not None else {k: 3 for k in main.MENU_KEYS})

    def _nav(self, perms=None):
        self._login(perms)
        html = self.client.get("/notices").get_data(as_text=True)
        start = html.index('<nav class="main-nav"')
        return html[start:html.index("</nav>", start)]

    def test_section_titles_are_rendered(self):
        nav = self._nav()
        for title in SECTION_TITLES:
            self.assertIn(f'<span>{title}</span>', nav, f"구역 제목 '{title}'이 없다")

    def test_no_link_is_lost(self):
        """구역으로 감싸면서 링크를 빠뜨리지 않았는지 — 이 작업의 유일한 실제 위험."""
        nav = self._nav()
        for link in TOP_LINKS:
            self.assertIn(link, nav, f"{link} 가 사이드바에서 사라졌다")

    def test_section_disappears_with_its_last_item(self):
        """권한이 없으면 항목도 구역 제목도 같이 사라진다 — 빈 제목만 남으면 안 된다.

        '분석' 구역에는 통계(stats)뿐이라, stats 권한을 0으로 두면 구역째 사라져야 한다.
        """
        perms = {k: 3 for k in main.MENU_KEYS}
        perms["stats"] = 0
        perms["report"] = 0
        nav = self._nav(perms)
        self.assertNotIn('href="/stats"', nav)
        self.assertIn('<span>상담</span>', nav)          # 다른 구역은 그대로
        self.assertNotIn('<span>분석</span>', nav)       # 내용이 없어진 구역은 제목도 없음

    def test_viewer_keeps_sections_that_still_have_items(self):
        """조회 전용 계정: '새 상담'은 빠져도 '상담' 구역은 상담목록 때문에 남는다."""
        perms = {k: 1 for k in main.MENU_KEYS}
        nav = self._nav(perms)
        self.assertNotIn('href="/consult/new"', nav)     # 등록 권한 없음
        self.assertIn('href="/consultations"', nav)
        self.assertIn('<span>상담</span>', nav)


if __name__ == "__main__":
    unittest.main()
