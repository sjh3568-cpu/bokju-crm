"""채널 문의 내역 (/consultations/inquiries) + 문의 → 상담 등록 유입경로 자동 채움 (2026-09-15)."""
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

import app as main
import models


class InquiryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 'test.db'))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        import partnerships, support_requests, transport
        partnerships.init_schema(); support_requests.init_schema(); transport.init_schema()
        models.ensure_admin_user('iqtest', 'test-only-password', display_name='테스트')
        user = models.get_user('iqtest')
        boot = patch.object(main, '_db_initialized', True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=user['id'], username='iqtest', role='admin',
                           perms={k: 3 for k in main.MENU_KEYS})
        today = date.today().isoformat()
        # ① 홈페이지 문의 — 미처리
        self.c_open = models.create_communication(channel="웹문의", direction="in", contact="010-1111-2222",
                                                  summary="[상담게시판 #12] 입원 문의 · 홍길동", body="문의 본문",
                                                  occurred_at=f"{today} 09:00:00", created_by="홈페이지 게시판")
        models.homepage_post_upsert(300, board_no=12, comm_id=self.c_open, title="입원 문의", site_status="미답변",
                                    detail_ok=1, reg_date=today)
        # ② 카카오 문의 — 상담 등록으로 이어짐
        pid = models.find_or_create_patient(name="김환자", guardian_phone="010-3333-4444")
        cid = models.create_consultation(patient_id=pid, consult_date=today, counselor="박세연",
                                         consult_result="상담완료", admission_status="입원예정")
        self.c_conv = models.create_communication(channel="카카오", direction="in", contact="010-3333-4444",
                                                  summary="카카오 상담신청 · 김환자", body="입원 문의",
                                                  occurred_at=f"{today} 10:00:00", patient_id=pid)
        models.update_communication(self.c_conv, status="done", consultation_id=cid, patient_id=pid)
        # ③ 홈페이지 문의 — 답변만 하고 완료
        self.c_done = models.create_communication(channel="웹문의", direction="in", contact="010-5555-6666",
                                                  summary="[상담게시판 #11] 비용 문의 · 이보호", body="비용",
                                                  occurred_at=f"{today} 11:00:00")
        models.homepage_post_upsert(298, board_no=11, comm_id=self.c_done, site_status="답변완료", detail_ok=1,
                                    answered_by="iqtest", answered_at=f"{today} 11:30:00")
        models.update_communication(self.c_done, status="done")
        # ④ 아웃바운드(문자 발신 기록) — 문의 집계에서 제외돼야 함
        models.create_communication(channel="문자", direction="out", summary="발신", body="x",
                                    occurred_at=f"{today} 12:00:00")

    def test_rows_and_stages(self):
        rows = models.inquiry_rows()
        self.assertEqual([r["id"] for r in rows], [self.c_done, self.c_conv, self.c_open])   # 최신순, 아웃바운드 제외
        stages = {r["id"]: r["stage"] for r in rows}
        self.assertEqual(stages[self.c_open], "미처리")
        self.assertEqual(stages[self.c_conv], "상담등록")
        self.assertEqual(stages[self.c_done], "처리완료")
        conv = next(r for r in rows if r["id"] == self.c_conv)
        self.assertEqual(conv["patient_name"], "김환자")
        self.assertEqual(conv["counselor"], "박세연")
        self.assertEqual(conv["admission_status"], "입원예정")
        done = next(r for r in rows if r["id"] == self.c_done)
        self.assertTrue(done["answered"]); self.assertEqual(done["board_no"], 11)
        self.assertEqual([r["id"] for r in models.inquiry_rows(stage="미처리")], [self.c_open])
        self.assertEqual([r["id"] for r in models.inquiry_rows(channel="카카오")], [self.c_conv])
        self.assertEqual([r["id"] for r in models.inquiry_rows(q="비용")], [self.c_done])

    def test_summary_counts_funnel(self):
        s = models.inquiry_summary(models.inquiry_rows())
        self.assertEqual((s["total"], s["open"], s["converted"], s["done_only"], s["answered"]), (3, 1, 1, 1, 1))
        self.assertEqual(s["rate"], 33)
        self.assertEqual(s["by_channel"]["웹문의"]["total"], 2)
        self.assertEqual(s["by_channel"]["카카오"]["converted"], 1)

    def test_monthly_trend_has_12_months_and_counts_this_month(self):
        m = models.inquiry_monthly(12)
        self.assertEqual(len(m), 12)
        self.assertEqual(m[-1]["ym"], date.today().strftime("%Y-%m"))
        self.assertEqual(m[-1]["total"], 3)
        self.assertEqual(m[-1]["converted"], 1)
        self.assertEqual(m[-1]["channels"], {"웹문의": 2, "카카오": 1})

    def test_page_renders_with_actions_and_sidebar_entry(self):
        r = self.client.get('/consultations/inquiries')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('채널 문의 내역', html)
        self.assertIn('href="/consultations/inquiries"', html)                       # 사이드바 하위 메뉴
        self.assertIn(f'class="btn btn-primary btn-xs ib-hp-reply" data-comm="{self.c_open}"', html)
        self.assertIn(f'href="/consult/new?comm_id={self.c_open}"', html)
        self.assertIn('id="hp-reply-dialog"', html)
        self.assertIn('tel:010-1111-2222', html)
        self.assertIn('전환율 33%', html)
        # 단계 필터
        html2 = self.client.get('/consultations/inquiries?stage=상담등록').get_data(as_text=True)
        self.assertIn('상담 #', html2)
        self.assertNotIn('data-comm="%d"' % self.c_open, html2)

    def test_csv_export(self):
        r = self.client.get('/consultations/inquiries.csv')
        self.assertEqual(r.status_code, 200)
        text = r.get_data(as_text=True)
        self.assertIn('일시,채널,환자', text)
        self.assertIn('홈페이지', text)
        self.assertIn('상담등록', text)
        self.assertEqual(text.count('\n'), 4)   # 헤더 + 3건

    def test_consult_new_from_inquiry_prefills_referral_and_channel(self):
        html = self.client.get(f'/consult/new?comm_id={self.c_open}').get_data(as_text=True)
        self.assertRegex(html, r'value="홈페이지"\s+checked')
        self.assertRegex(html, r'value="전화상담"\s+checked')
        # 카카오 문의(미처리로 하나 더)
        ck = models.create_communication(channel="카카오", direction="in", contact="010-7777-8888", summary="k", body="k")
        html = self.client.get(f'/consult/new?comm_id={ck}').get_data(as_text=True)
        self.assertRegex(html, r'value="카카오톡 채널"\s+checked')
        # 문의 없이 열면 아무것도 체크되지 않는다
        html = self.client.get('/consult/new').get_data(as_text=True)
        self.assertNotRegex(html, r'value="홈페이지"\s+checked')
        self.assertNotRegex(html, r'value="전화상담"\s+checked')

    def test_saving_consult_from_inquiry_records_referral_type(self):
        payload = {
            'patient': {'name': '홍길동', 'gender': 'M', 'guardian_phone': '010-1111-2222'},
            'consultation': {'consult_date': date.today().isoformat(), 'consult_channel': '전화상담',
                             'referral_source_detail': ['홈페이지']},
        }
        r = self.client.post(f'/api/consult?comm_id={self.c_open}', json=payload)
        self.assertEqual(r.status_code, 200, r.get_json())
        cid = r.get_json()['id']
        con = models.get_consultation(cid)
        self.assertEqual(con['referral_source_detail'], ['홈페이지'])
        self.assertEqual(con['referral_source_type'], ['온라인'])
        comm = models.get_communication(self.c_open)
        self.assertEqual((comm['status'], comm['consultation_id']), ('done', cid))
        self.assertEqual(next(r for r in models.inquiry_rows() if r['id'] == self.c_open)['stage'], '상담등록')


if __name__ == "__main__":
    unittest.main()
