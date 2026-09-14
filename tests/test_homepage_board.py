"""홈페이지 상담게시판 연동 (homepage_board.py, 2026-09-15).
실제 사이트에 접속하지 않는다 — 화면 구조를 본뜬 합성 HTML과 가짜 관리자 세션으로 검증."""
import os
import tempfile
import unittest
from unittest.mock import patch

import app as main
import homepage_board as hb
import models


def _public_list(posts):
    """공개 목록 HTML — <ul class="contentWrap"><a href=…idx=N><li class=…>"""
    rows = "".join(
        f'''<ul class="contentWrap">
            <a href="/sub/07_community/guide_01_view?idx={p['idx']}" data-idx="{p['idx']}" class='checkPassword'>
              <li class="no">{p['no']}</li>
              <li class="title">{p['title']} <i class='fas fa-lock'></i></li>
              <li class="status"><span>{p['status']}</span></li>
              <li class="name">{p['name']}</li>
              <li class="date">{p['date']}</li>
              <li class="count">1</li>
            </a></ul>''' for p in posts)
    return f'<div id="counselWrap"><ul class="titleWrap"><li class="no">NO</li></ul>{rows}</div>'


def _admin_view(name, phone, title, content, answer=None):
    ans = ""
    if answer:
        ans = f'''<h3>고객의소리 답변</h3><table>
            <tr><th>제목</th><td>{answer['title']}</td></tr>
            <tr><th>등록날짜</th><td>{answer['date']}</td></tr>
            <tr><th>내용</th><td>{answer['content']}</td></tr></table>'''
    return f'''<html><body><div class="titWrap">고객의 소리</div><table>
        <tr><th>이름</th><td>{name}</td><th>비밀번호</th><td>0000</td></tr>
        <tr><th>이메일</th><td></td><th>연락처</th><td>{phone}</td></tr>
        <tr><th>제목</th><td>{title}</td></tr>
        <tr><th>내용</th><td>{content}</td></tr></table>{ans}</body></html>'''


SAMPLE_POSTS = [
    {"idx": 300, "no": 12, "title": "입원 문의드립니다.", "status": "접수", "name": "홍*동", "date": "2026-09-15"},
    {"idx": 298, "no": 11, "title": "입원가능여부", "status": "답변완료", "name": "김*안", "date": "2026-09-10"},
]


class ParserTests(unittest.TestCase):
    def test_public_list(self):
        posts = hb.parse_public_list(_public_list(SAMPLE_POSTS))
        self.assertEqual([p["idx"] for p in posts], [300, 298])
        self.assertEqual(posts[0]["board_no"], 12)
        self.assertEqual(posts[0]["title"], "입원 문의드립니다.")   # 아이콘 태그는 제거
        self.assertEqual(posts[0]["status"], "접수")
        self.assertEqual(posts[1]["status"], "답변완료")
        self.assertEqual(posts[0]["reg_date"], "2026-09-15")

    def test_admin_view(self):
        page = _admin_view("홍길동", "01012345678", "입원 문의", "첫 줄<br /><br />둘째 줄",
                           answer={"title": "감사합니다", "date": "2026-09-15", "content": "<p>안녕하세요</p>"})
        d = hb.parse_admin_view(page)
        self.assertEqual(d["name"], "홍길동")
        self.assertEqual(d["phone"], "01012345678")
        self.assertEqual(d["title"], "입원 문의")
        self.assertEqual(d["content"], "첫 줄\n\n둘째 줄")
        self.assertEqual(d["answer_title"], "감사합니다")
        self.assertEqual(d["answer_content"], "안녕하세요")

    def test_admin_view_without_answer(self):
        d = hb.parse_admin_view(_admin_view("홍길동", "010-1234-5678", "제목", "내용"))
        self.assertEqual(d["answer_content"], "")

    def test_update_form_hidden_fields(self):
        html = '<form id="submitFrm"><input type="hidden" name="group_idx" value="300">' \
               '<input type="hidden" name="answer_idx" value=""></form>'
        self.assertEqual(hb.parse_admin_update_form(html), {"group_idx": "300", "answer_idx": ""})

    def test_text_to_html_escapes_and_paragraphs(self):
        self.assertEqual(hb.text_to_html("안녕하세요.\n둘째 줄\n\n<b>새 단락</b>"),
                         "<p>안녕하세요.<br>둘째 줄</p><p>&lt;b&gt;새 단락&lt;/b&gt;</p>")


class FakeAdmin:
    """관리자 세션 대역 — 로그인·조회·답변을 메모리에서 흉내 낸다."""
    def __init__(self, posts):
        self.posts = posts          # idx → dict(name, phone, title, content, answer)
        self.replies = []

    def fetch_post(self, idx):
        p = self.posts[idx]
        return {"idx": idx, "name": p["name"], "phone": p["phone"], "email": "",
                "title": p["title"], "content": p["content"],
                "answer_title": "", "answer_date": "", "answer_content": p.get("answer", "")}

    def reply(self, idx, title, body_text):
        self.replies.append((idx, title, body_text))
        self.posts[idx]["answer"] = body_text
        return {"answer_date": "2026-09-15", "answer_content": body_text}


class PollTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 'test.db'))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        self.admin = FakeAdmin({
            300: {"name": "홍길동", "phone": "010-1234-5678", "title": "입원 문의드립니다.", "content": "아버지가 뇌경색으로…"},
            298: {"name": "김지안", "phone": "01099998888", "title": "입원가능여부", "content": "…", "answer": "답변"},
        })
        for name, target in (("_admin", lambda: self.admin), ("admin_configured", lambda: True)):
            p = patch.object(hb, name, target); p.start(); self.addCleanup(p.stop)
        self.listing = list(SAMPLE_POSTS)
        p = patch.object(hb, "fetch_public_list", lambda: hb.parse_public_list(_public_list(self.listing)))
        p.start(); self.addCleanup(p.stop)

    def test_first_run_registers_only_unanswered(self):
        self.assertEqual(hb.poll_once(), 1)
        known = models.homepage_post_known()
        self.assertEqual(set(known), {300, 298})
        self.assertIsNone(known[298]["comm_id"])              # 과거 답변완료 글은 매핑만
        comm = models.get_communication(known[300]["comm_id"])
        self.assertEqual(comm["channel"], "웹문의")
        self.assertEqual(comm["status"], "open")
        self.assertEqual(comm["contact"], "010-1234-5678")
        self.assertIn("[상담게시판 #12] 입원 문의드립니다. · 홍길동", comm["summary"])
        self.assertEqual(comm["body"], "아버지가 뇌경색으로…")
        self.assertEqual(hb.poll_once(), 0)                    # 같은 목록 다시 → 중복 등록 없음

    def test_new_post_matches_patient_by_guardian_phone(self):
        pid = models.find_or_create_patient(name="홍부친", guardian_phone="010-1234-5678")
        hb.poll_once()
        comm = models.get_communication(models.homepage_post_known()[300]["comm_id"])
        self.assertEqual(comm["patient_id"], pid)

    def test_answered_on_site_closes_inbox_item(self):
        hb.poll_once()
        comm_id = models.homepage_post_known()[300]["comm_id"]
        self.listing[0] = dict(self.listing[0], status="답변완료")   # 홈페이지 관리자에서 직접 답변
        hb.poll_once()
        self.assertEqual(models.get_communication(comm_id)["status"], "done")
        self.assertEqual(models.homepage_post_by_comm(comm_id)["answered_by"], "홈페이지 관리자(직접)")

    def test_admin_failure_registers_title_only_then_fills_later(self):
        def boom(idx):
            raise hb.BoardError("로그인 실패")
        with patch.object(self.admin, "fetch_post", boom):
            self.assertEqual(hb.poll_once(), 1)
        rec = models.homepage_post_known()[300]
        self.assertEqual(rec["detail_ok"], 0)
        comm = models.get_communication(rec["comm_id"])
        self.assertIn("홍*동", comm["summary"])                # 가린 이름으로라도 알림은 나간다
        hb.poll_once()                                          # 다음 주기 — 본문 보충
        rec = models.homepage_post_known()[300]
        self.assertEqual(rec["detail_ok"], 1)
        comm = models.get_communication(rec["comm_id"])
        self.assertEqual(comm["body"], "아버지가 뇌경색으로…")
        self.assertEqual(comm["contact"], "010-1234-5678")
        self.assertIn("· 홍길동", comm["summary"])

    def test_reply_posts_and_closes(self):
        hb.poll_once()
        comm_id = models.homepage_post_known()[300]["comm_id"]
        after = hb.reply_to_post(comm_id, "문의해 주셔서 감사드립니다", "안녕하세요.\n답변입니다.", "counselor1")
        self.assertEqual(self.admin.replies, [(300, "문의해 주셔서 감사드립니다", "안녕하세요.\n답변입니다.")])
        self.assertEqual(after["answer_date"], "2026-09-15")
        self.assertEqual(models.get_communication(comm_id)["status"], "done")
        rec = models.homepage_post_by_comm(comm_id)
        self.assertEqual(rec["site_status"], "답변완료")
        self.assertEqual(rec["answered_by"], "counselor1")

    def test_reply_rejects_unlinked_or_empty(self):
        cid = models.create_communication(channel="카카오", direction="in", summary="x", body="y")
        with self.assertRaises(hb.BoardError):
            hb.reply_to_post(cid, "t", "b", "u")
        hb.poll_once()
        comm_id = models.homepage_post_known()[300]["comm_id"]
        with self.assertRaises(hb.BoardError):
            hb.reply_to_post(comm_id, "", "b", "u")
        self.assertEqual(models.get_communication(comm_id)["status"], "open")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 'test.db'))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        import partnerships, support_requests, transport
        partnerships.init_schema(); support_requests.init_schema(); transport.init_schema()
        models.ensure_admin_user('hptest', 'test-only-password', display_name='테스트')
        user = models.get_user('hptest')
        boot = patch.object(main, '_db_initialized', True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=user['id'], username='hptest', role='admin',
                           perms={k: 3 for k in main.MENU_KEYS})
        self.comm_id = models.create_communication(channel="웹문의", direction="in", contact="010-1111-2222",
                                                   summary="[상담게시판 #12] 입원 문의 · 홍길동", body="본문")
        models.homepage_post_upsert(300, board_no=12, comm_id=self.comm_id, title="입원 문의",
                                    site_status="접수", detail_ok=1, reg_date="2026-09-15")

    def test_get_reply_context(self):
        r = self.client.get(f'/api/homepage-board/{self.comm_id}')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["idx"], 300)
        self.assertEqual(d["board_no"], 12)
        self.assertEqual(d["body"], "본문")
        self.assertEqual(d["reply_title"], hb.REPLY_DEFAULT_TITLE)
        self.assertIn("counselV.php?idx=300", d["admin_url"])

    def test_get_unlinked_404(self):
        cid = models.create_communication(channel="카카오", direction="in", summary="x", body="y")
        self.assertEqual(self.client.get(f'/api/homepage-board/{cid}').status_code, 404)

    def test_reply_success_and_board_error(self):
        with patch.object(hb, "reply_to_post", return_value={"answer_date": "2026-09-15"}) as m:
            r = self.client.post(f'/api/homepage-board/{self.comm_id}/reply', json={"title": "t", "body": "b"})
            self.assertEqual(r.status_code, 200, r.get_json())
            self.assertEqual(m.call_args.args, (self.comm_id, "t", "b", "hptest"))
        with patch.object(hb, "reply_to_post", side_effect=hb.BoardError("홈페이지 관리자 로그인 실패")):
            r = self.client.post(f'/api/homepage-board/{self.comm_id}/reply', json={"title": "t", "body": "b"})
            self.assertEqual(r.status_code, 502)
            self.assertIn("로그인 실패", r.get_json()["error"])

    def test_dashboard_shows_reply_button_for_board_posts(self):
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn(f'class="btn btn-primary btn-xs ib-hp-reply" data-comm="{self.comm_id}"', html)
        self.assertIn('id="hp-reply-dialog"', html)


if __name__ == "__main__":
    unittest.main()
