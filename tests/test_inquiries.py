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
        # 입원 — 연결 상담이 입원예정이면 아직 0, 입원완료로 바뀌면 1 (문의 대비 33%, 상담 대비 100%)
        self.assertEqual((s["admitted"], s["admission_pending"]), (0, 1))
        conn = models.get_db(); conn.execute("UPDATE consultations SET admission_status='입원완료'"); conn.commit(); conn.close()
        s = models.inquiry_summary(models.inquiry_rows())
        self.assertEqual((s["admitted"], s["admit_rate"], s["admit_rate_consult"]), (1, 33, 100))
        self.assertEqual(s["by_channel"]["카카오"]["admitted"], 1)
        self.assertEqual(models.inquiry_monthly(12)[-1]["admitted"], 1)
        # 퇴원해도 입원완료로 센다
        conn = models.get_db(); conn.execute("UPDATE consultations SET admission_status='퇴원완료'"); conn.commit(); conn.close()
        self.assertEqual(models.inquiry_summary(models.inquiry_rows())["admitted"], 1)

    def test_stats_json_includes_inbound_funnel(self):
        conn = models.get_db(); conn.execute("UPDATE consultations SET admission_status='입원완료'"); conn.commit(); conn.close()
        r = self.client.get('/api/stats.json?preset=this_month')
        self.assertEqual(r.status_code, 200)
        f = r.get_json()["inbound_funnel"]
        self.assertEqual(f["total"]["total"], 3)
        self.assertEqual(f["total"]["admitted"], 1)
        by = {c["label"]: c for c in f["channels"]}
        self.assertEqual(by["홈페이지"]["total"], 2)
        self.assertEqual((by["카카오톡"]["converted"], by["카카오톡"]["admitted"], by["카카오톡"]["admit_rate_consult"]), (1, 1, 100))
        html = self.client.get('/stats').get_data(as_text=True)
        self.assertIn('id="inbound-funnel"', html)

    def test_monthly_trend_has_12_months_and_counts_this_month(self):
        m = models.inquiry_monthly(12)
        self.assertEqual(len(m), 12)
        self.assertEqual(m[-1]["ym"], date.today().strftime("%Y-%m"))
        self.assertEqual(m[-1]["total"], 3)
        self.assertEqual(m[-1]["converted"], 1)
        self.assertEqual(m[-1]["channels"], {"웹문의": 2, "카카오": 1})
        # 끝 달을 과거로 주면 그 달까지만 — 이번 달 문의는 빠진다
        past = date(2026, 3, 31)
        m = models.inquiry_monthly(6, end=past)
        self.assertEqual([x["ym"] for x in m], ["2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03"])
        self.assertEqual(sum(x["total"] for x in m), 0)

    def test_monthly_span_follows_filter_with_minimum_six(self):
        html = self.client.get('/consultations/inquiries').get_data(as_text=True)       # 이번 달 → 6개월
        self.assertIn('6개월 (선택 기간 끝 달 기준', html)
        html = self.client.get('/consultations/inquiries?from=2025-01-01&to=2025-12-31').get_data(as_text=True)
        self.assertIn('2025-01 ~ 2025-12 · 12개월', html)
        html = self.client.get('/consultations/inquiries?from=2020-01-01&to=2026-09-30').get_data(as_text=True)
        self.assertIn('24개월', html)                                                     # 상한

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
        self.assertIn('상담 전환율 33%', html)
        self.assertIn('입원 완료', html)
        # 단계 필터
        html2 = self.client.get('/consultations/inquiries?stage=상담등록').get_data(as_text=True)
        self.assertIn('상담 #', html2)
        self.assertNotIn('data-comm="%d"' % self.c_open, html2)

    def test_default_period_widens_to_oldest_open_inquiry(self):
        """이번 달보다 오래된 미처리 문의(EasyQR 백필 등)는 기본 화면에서 빠지면 안 된다 (2026-09-18)."""
        old = models.create_communication(channel="웹문의", direction="in", contact="010-7777-8888",
                                          summary="전화상담 신청 #12 · 정진수", body="교통사고 재활 문의",
                                          occurred_at="2026-06-19 10:12:33", created_by="EasyQR")
        r = self.client.get("/consultations/inquiries")
        html = r.get_data(as_text=True)
        self.assertIn("전화상담 신청 #12", html)
        self.assertIn("2026-06-19 ~", html)                 # 기본 시작일이 그 접수일까지 넓혀짐
        self.assertIn("미처리·최근 완료 포함 확장", html)
        # 기간을 직접 주면 그대로 존중
        r = self.client.get("/consultations/inquiries?from=2026-09-01")
        self.assertNotIn("전화상담 신청 #12", r.get_data(as_text=True))
        # 완료해도 일주일은 남는다 — "완료 눌렀더니 사라졌다"가 되지 않게. 처리완료 카드에 세어진다.
        models.update_communication(old, status="done")
        html = self.client.get("/consultations/inquiries").get_data(as_text=True)
        self.assertIn("최근 완료 포함 확장", html)
        self.assertIn("전화상담 신청 #12", html)
        self.assertIn("stage=처리완료", html)                # 카드가 단계 필터 링크
        # 일주일 넘게 지난 완료 건은 기본 기간(이번 달)으로 돌아간다
        conn = models.get_db()
        conn.execute("UPDATE communications SET resolved_at = datetime('now', '-10 days') WHERE id = ?", (old,))
        conn.commit(); conn.close()
        html = self.client.get("/consultations/inquiries").get_data(as_text=True)
        self.assertNotIn("포함 확장", html)
        self.assertNotIn("전화상담 신청 #12", html)

    def test_consult_new_from_easyqr_inquiry_prefills_name_age_region_and_ai_memo(self):
        """EasyQR 문의 → 상담 등록: 이름·나이·거주지·연락처가 칸에 들어가고 문의 내용은 AI 채우기 입력에 (2026-09-18)."""
        cid = models.create_communication(
            channel="카카오", direction="in", contact="010-7118-4526",
            summary="전화상담 신청 #12 · 정진수",
            body="교통사고 환자도 재활로 입원가능한지 궁금합니다\n\n[연락가능시간] 언제든 가능\n[거주지] 포항시\n[환자나이] 78",
            occurred_at="2026-06-19 10:12:33", created_by="EasyQR")
        html = self.client.get(f"/consult/new?comm_id={cid}").get_data(as_text=True)
        self.assertRegex(html, r'name="patient.name"\s+value="정진수"')
        self.assertRegex(html, r'id="patient-age"[^>]*value="78"')
        self.assertRegex(html, r'name="patient.residence_sigungu"\s+value="포항시"')
        self.assertRegex(html, r'value="경상북도"\s+selected')                 # 시/군/구 → 시/도 자동
        self.assertRegex(html, r'name="patient.guardian_phone"[^>]*value="010-7118-4526"')
        self.assertRegex(html, r'value="카카오톡 채널"\s+checked')
        self.assertIn('data-autofill="1"', html)
        self.assertIn("교통사고 환자도 재활로 입원가능한지 궁금합니다", html)   # AI 메모 입력
        self.assertIn("환자 나이 78세", html)
        self.assertNotIn("[환자나이]", html.split('id="ai-memo-text"')[1].split("</textarea>")[0])  # 라벨 줄은 뺀다
        self.assertNotIn("재상담 등록", html)                                   # 재상담 배너는 안 뜬다

    def test_inquiry_prefill_parser(self):
        from views.inbound import inquiry_prefill
        p = inquiry_prefill({"summary": "[상담게시판 #12] 입원 문의 · 홍길동", "body": "본문만", "contact": "010-1-2"})
        self.assertEqual((p["name"], p["patient_age"], p["residence_sido"], p["residence_sigungu"]), ("홍길동", None, "", ""))
        self.assertIn("본문만", p["memo"]); self.assertIn("연락처 010-1-2", p["memo"])
        p = inquiry_prefill({"summary": "전화상담 신청 #16", "body": "x\n[거주지] 경상북도 의성읍\n[환자나이] 0"})
        self.assertEqual((p["name"], p["patient_age"], p["residence_sido"], p["residence_sigungu"]), ("", None, "경상북도", ""))
        p = inquiry_prefill({"summary": "전화상담 신청 #17 · 김은경", "body": "[거주지] 고성군"})   # 두 시/도에 있는 이름 → 시/도 비움
        self.assertEqual((p["residence_sigungu"], p["residence_sido"], p["memo"]), ("고성군", "", ""))

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
