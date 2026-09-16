"""팩스·문서 자료함 (fax_inbox.py · views/documents.py, 2026-09-17).
실제 Claude API는 부르지 않는다 — analyze_file을 가짜 판독 결과로 바꿔 폴더 감시→파일명 정리→인박스→화면을 검증."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as main
import fax_inbox
import models
import partnerships
import support_requests

# 최소 PDF (1페이지, 내용 없음) — 판독은 가짜라 내용은 상관없다
MINI_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>endobj\n"
            b"trailer<</Root 1 0 R>>\n%%EOF\n")

FAKE_AI = {
    "document_type": "진료의뢰서", "patient_name": "홍길동", "birth_date": "1950-01-02", "age": "76", "sex": "남",
    "main_diagnosis": "뇌경색", "diagnoses": ["뇌경색", "고혈압"], "sender_hospital": "안동병원",
    "sender_department": "신경과", "sender_contact": "054-000-0000", "doc_date": "2026-09-16",
    "referral_reason": "급성기 치료 후 재활 목적 전원", "current_status": "우측 편마비, 의식 명료",
    "precautions": ["L-tube"], "medications": ["아스피린"],
    "summary": ["재활 목적 전원 의뢰", "우측 편마비", "비위관 유지 중"], "confidence": "high", "notes": "",
}


class FaxInboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self.tmp.name) / "수신"
        self.inbox.mkdir()
        self.db_patch = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "test.db")); self.db_patch.start()
        self.env = patch.dict(os.environ, {"FAX_INBOX_DIR": str(self.inbox), "FAX_ARCHIVE_DIR": "",
                                           "ANTHROPIC_API_KEY": "test-key", "FAX_AI_ENABLED": "1"}); self.env.start()
        self.settle = patch.object(fax_inbox, "SETTLE_SECONDS", 0); self.settle.start()
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("fax-test", "test-password", display_name="테스트")
        self.uid = models.get_user("fax-test")["id"]
        self.boot = patch.object(main, "_db_initialized", True); self.boot.start()
        self.notice = patch.object(models, "first_unread_required_announcement", return_value=None); self.notice.start()
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as s:
            s.clear()
            s.update(user_id=self.uid, username="fax-test", display_name="테스트", role="admin",
                     perms={k: 3 for k in main.MENU_KEYS}, cooperation_permissions_v2=True)

    def tearDown(self):
        self.notice.stop(); self.boot.stop(); self.settle.stop(); self.env.stop(); self.db_patch.stop(); self.tmp.cleanup()

    def _drop(self, name="FAX_20260916_1030.pdf", data=MINI_PDF):
        p = self.inbox / name
        p.write_bytes(data)
        return p

    # ── 폴더 감시 → 판독 → 파일명 정리 → 인박스 ──

    def test_scan_registers_renames_and_creates_inbox_card(self):
        self._drop()
        with patch.object(fax_inbox, "analyze_file", return_value=dict(FAKE_AI)):
            self.assertEqual(fax_inbox.scan_once(), 1)
        docs = models.list_documents()
        self.assertEqual(len(docs), 1)
        d = docs[0]
        self.assertEqual(d["status"], "analyzed")
        self.assertEqual(d["patient_name_ai"], "홍길동")
        self.assertEqual(d["diagnosis_ai"], "뇌경색")
        self.assertEqual(d["sender_ai"], "안동병원")
        self.assertEqual(d["doc_date"], "2026-09-16")
        self.assertEqual(d["filename"], "2026-09-16_홍길동_뇌경색.pdf")
        self.assertEqual(d["original_name"], "FAX_20260916_1030.pdf")
        archived = self.inbox / "정리" / "2026-09-16_홍길동_뇌경색.pdf"
        self.assertTrue(archived.is_file(), "정리 폴더로 옮겨져야 한다")
        self.assertFalse((self.inbox / "FAX_20260916_1030.pdf").exists(), "수신 폴더는 비어야 한다")
        self.assertEqual(d["stored_path"], str(archived))
        self.assertIn("비위관 유지 중", d["ai_summary"])
        # 인박스 카드
        comm = models.get_communication(d["comm_id"])
        self.assertEqual(comm["channel"], "팩스")
        self.assertEqual(comm["status"], "open")
        self.assertEqual(comm["summary"], "팩스 · 홍길동 뇌경색 (안동병원)")
        self.assertEqual(comm["contact"], "054-000-0000")
        self.assertEqual(models.open_inbound_count(), 1)

    def test_duplicate_file_is_skipped_and_archive_not_rescanned(self):
        self._drop("a.pdf")
        with patch.object(fax_inbox, "analyze_file", return_value=dict(FAKE_AI)):
            self.assertEqual(fax_inbox.scan_once(), 1)
            self._drop("b.pdf")                      # 같은 내용 다시 떨어짐
            self.assertEqual(fax_inbox.scan_once(), 0)
        self.assertEqual(len(models.list_documents()), 1)
        self.assertEqual(models.open_inbound_count(), 1)

    def test_ai_failure_keeps_pending_and_retries_then_archives_as_unknown(self):
        self._drop("x.pdf")
        with patch.object(fax_inbox, "analyze_file", side_effect=RuntimeError("timeout")):
            fax_inbox.scan_once()                    # 1회
            d = models.list_documents()[0]
            self.assertEqual(d["status"], "pending")
            self.assertEqual(d["ai_attempts"], 1)
            self.assertIn("timeout", d["ai_error"])
            self.assertTrue((self.inbox / "x.pdf").exists(), "실패 중엔 원래 자리")
            fax_inbox.scan_once(); fax_inbox.scan_once()   # 2·3회 → 포기 후 미확인으로 정리
        d = models.list_documents()[0]
        self.assertEqual(d["ai_attempts"], 3)
        self.assertEqual(d["filename"], f"{d['doc_date']}_미확인_x.pdf")
        self.assertTrue((self.inbox / "정리" / d["filename"]).is_file())
        # 한 번 더 돌아도 더 시도하지 않는다
        with patch.object(fax_inbox, "analyze_file", return_value=dict(FAKE_AI)) as m:
            fax_inbox.scan_once()
            m.assert_not_called()

    def test_ai_disabled_still_registers_with_unknown_name(self):
        with patch.dict(os.environ, {"FAX_AI_ENABLED": "0"}):
            self._drop("scan001.pdf")
            fax_inbox.scan_once()
        d = models.list_documents()[0]
        self.assertEqual(d["status"], "pending")
        self.assertTrue(d["filename"].endswith("_미확인_scan001.pdf"))
        self.assertIn("비활성", d["ai_error"])

    def test_tif_is_not_analyzed_but_registered(self):
        self._drop("fax.tif", b"II*\x00fake")
        with patch.object(fax_inbox, "analyze_file", return_value=dict(FAKE_AI)) as m:
            fax_inbox.scan_once()
            m.assert_not_called()
        d = models.list_documents()[0]
        self.assertIn("PDF", d["ai_error"])
        self.assertTrue(d["filename"].endswith("_미확인_fax.tif"))

    def test_filename_sanitized(self):
        self.assertEqual(fax_inbox.build_filename("2026-09-16", "홍/길:동", "뇌경색 (Lt. MCA)*", ".PDF"),
                         "2026-09-16_홍 길 동_뇌경색 (Lt. MCA).pdf")
        self.assertEqual(fax_inbox.build_filename("bad", "", "", ".pdf", fallback="orig.pdf")[10:],
                         "_미확인_orig.pdf")
        long_dx = "아주" * 40
        self.assertLessEqual(len(fax_inbox.build_filename("2026-01-01", "김", long_dx, ".pdf")), 11 + 1 + 1 + 30 + 4)

    def test_name_collision_gets_suffix(self):
        with patch.object(fax_inbox, "analyze_file", return_value=dict(FAKE_AI)):
            self._drop("one.pdf", MINI_PDF + b"1"); fax_inbox.scan_once()
            self._drop("two.pdf", MINI_PDF + b"2"); fax_inbox.scan_once()
        names = sorted(d["filename"] for d in models.list_documents())
        self.assertEqual(names, ["2026-09-16_홍길동_뇌경색 (2).pdf", "2026-09-16_홍길동_뇌경색.pdf"])

    # ── 화면·API ──

    def _registered(self):
        self._drop()
        with patch.object(fax_inbox, "analyze_file", return_value=dict(FAKE_AI)):
            fax_inbox.scan_once()
        return models.list_documents()[0]

    def test_pages_and_file(self):
        d = self._registered()
        r = self.client.get("/documents")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("홍길동", html); self.assertIn("뇌경색", html); self.assertIn("확인 필요", html)
        r = self.client.get(f"/documents/{d['id']}")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("비위관 유지 중", html); self.assertIn("L-tube", html); self.assertIn("2026-09-16_홍길동_뇌경색.pdf", html)
        r = self.client.get(f"/documents/{d['id']}/file")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/pdf")
        self.assertTrue(r.data.startswith(b"%PDF"))
        # 인박스 카드 → 자료함 링크
        r = self.client.get(f"/documents?comm={d['comm_id']}")
        self.assertEqual(r.status_code, 302); self.assertTrue(r.location.endswith(f"/documents/{d['id']}"))
        # 대시보드 인바운드 카드에 원본·요약 버튼
        r = self.client.get("/")
        self.assertIn(f"/documents?comm={d['comm_id']}", r.get_data(as_text=True))

    def test_update_and_rename(self):
        d = self._registered()
        r = self.client.post(f"/api/documents/{d['id']}", json={"patient_name": "홍길순", "diagnosis": "뇌출혈",
                                                                "doc_date": "2026-09-17", "rename": 1})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["filename"], "2026-09-17_홍길순_뇌출혈.pdf")
        self.assertTrue((self.inbox / "정리" / "2026-09-17_홍길순_뇌출혈.pdf").is_file())
        self.assertFalse((self.inbox / "정리" / "2026-09-16_홍길동_뇌경색.pdf").exists())
        self.assertEqual(models.get_communication(d["comm_id"])["summary"], "팩스 · 홍길순 뇌출혈 (안동병원)")
        self.assertEqual(self.client.post(f"/api/documents/{d['id']}", json={"doc_date": "17/09"}).status_code, 400)

    def test_link_to_patient_closes_card(self):
        d = self._registered()
        pid = models.find_or_create_patient(name="홍길동", guardian_phone="010-1111-2222")
        r = self.client.post(f"/api/documents/{d['id']}/link", json={"patient_id": pid})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d2 = models.get_document(d["id"])
        self.assertEqual(d2["status"], "done"); self.assertEqual(d2["patient_id"], pid)
        self.assertEqual(models.get_communication(d["comm_id"])["status"], "done")
        self.assertEqual(models.open_inbound_count(), 0)
        # 환자 타임라인에 문서가 보인다
        titles = [t["title"] for t in models.patient_timeline(pid)]
        self.assertTrue(any("2026-09-16_홍길동_뇌경색.pdf" in t for t in titles), titles)

    def test_consult_new_prefills_from_document_and_links_on_save(self):
        d = self._registered()
        r = self.client.get(f"/consult/new?doc_id={d['id']}")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("팩스 자료함에서 상담 등록 중", html)
        self.assertIn('value="홍길동"', html)
        self.assertIn('value="안동병원"', html)
        self.assertIn(f'data-doc-id="{d["id"]}"', html)
        # 저장 → 문서·카드가 상담에 연결되고 완료
        payload = {"patient": {"name": "홍길동", "guardian_phone": "010-3333-4444"},
                   "consultation": {"consult_date": "2026-09-17", "counselor": "테스트", "consult_channel": "전화상담"}}
        r = self.client.post(f"/api/consult?doc_id={d['id']}", json=payload)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        cid = r.get_json()["id"]
        d2 = models.get_document(d["id"])
        self.assertEqual(d2["status"], "done"); self.assertEqual(d2["consultation_id"], cid)
        self.assertEqual(models.get_communication(d["comm_id"])["status"], "done")

    def test_upload_and_delete_keeps_file(self):
        import io
        with patch.object(fax_inbox, "analyze_file", return_value=dict(FAKE_AI)):
            r = self.client.post("/api/documents/upload", data={"file": (io.BytesIO(MINI_PDF), "직접스캔.pdf")},
                                 content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        did = r.get_json()["id"]
        d = models.get_document(did)
        self.assertEqual(d["filename"], "2026-09-16_홍길동_뇌경색.pdf")
        path = Path(d["stored_path"])
        self.assertTrue(path.is_file())
        r = self.client.delete(f"/api/documents/{did}")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(models.get_document(did))
        self.assertTrue(path.is_file(), "NAS 파일은 남긴다")
        self.assertEqual(models.open_inbound_count(), 0)

    def test_view_only_user_cannot_edit(self):
        d = self._registered()
        with self.client.session_transaction() as s:
            s["perms"] = {k: 1 for k in main.MENU_KEYS}
        self.assertEqual(self.client.get(f"/documents/{d['id']}").status_code, 200)
        self.assertEqual(self.client.post(f"/api/documents/{d['id']}/done").status_code, 403)
        self.assertEqual(self.client.post("/api/documents/scan").status_code, 403)

    # ── 보관기간 · 장수 제한 ──

    def test_purge_deletes_only_expired_files_and_keeps_summary(self):
        from datetime import date, timedelta
        d = self._registered()                                    # doc_date 2026-09-16
        path = Path(d["stored_path"])
        with patch.dict(os.environ, {"FAX_KEEP_DAYS": "30"}):
            # 아직 30일 안 됨 → 그대로
            self.assertEqual(fax_inbox.purge_expired(date(2026, 10, 10)), 0)
            self.assertTrue(path.is_file())
            # 31일 지남 → 파일만 삭제, 행·요약·카드 유지
            self.assertEqual(fax_inbox.purge_expired(date(2026, 10, 17)), 1)
        self.assertFalse(path.exists())
        d2 = models.get_document(d["id"])
        self.assertIsNotNone(d2["file_deleted_at"])
        self.assertEqual(d2["patient_name_ai"], "홍길동")
        self.assertIn("비위관 유지 중", d2["ai_summary"])
        self.assertEqual(self.client.get(f"/documents/{d['id']}/file").status_code, 410)
        html = self.client.get(f"/documents/{d['id']}").get_data(as_text=True)
        self.assertIn("보관기간", html); self.assertIn("비위관 유지 중", html)
        self.assertIn("원본 삭제됨", self.client.get("/documents").get_data(as_text=True))
        # 이미 지운 건 다시 세지 않는다
        with patch.dict(os.environ, {"FAX_KEEP_DAYS": "30"}):
            self.assertEqual(fax_inbox.purge_expired(date(2026, 12, 1)), 0)

    def test_purge_disabled_and_outside_folder_untouched(self):
        from datetime import date
        d = self._registered()
        path = Path(d["stored_path"])
        with patch.dict(os.environ, {"FAX_KEEP_DAYS": "0"}):
            self.assertEqual(fax_inbox.purge_expired(date(2030, 1, 1)), 0)
        self.assertTrue(path.is_file())
        # 팩스 폴더 밖을 가리키는 경로는 지우지 않는다
        outside = Path(self.tmp.name) / "elsewhere.pdf"; outside.write_bytes(MINI_PDF)
        models.update_document(d["id"], stored_path=str(outside))
        with patch.dict(os.environ, {"FAX_KEEP_DAYS": "1"}):
            self.assertEqual(fax_inbox.purge_expired(date(2030, 1, 1)), 0)
        self.assertTrue(outside.is_file())
        self.assertIsNone(models.get_document(d["id"])["file_deleted_at"])

    def test_defaults_keep_10_days_and_20_pages(self):
        import llm
        with patch.dict(os.environ, {"FAX_KEEP_DAYS": "", "FAX_AI_MAX_PAGES": ""}):
            os.environ.pop("FAX_KEEP_DAYS"); os.environ.pop("FAX_AI_MAX_PAGES")
            self.assertEqual(fax_inbox.keep_days(), 10)
            self.assertEqual(llm.fax_max_pages(), 20)

    def test_size_limit_applies_after_page_cut(self):
        """책 두께 팩스 — 원본이 25MB를 넘어도 앞쪽만 잘라 보내면 판독된다."""
        import io, llm
        from pypdf import PdfWriter
        w = PdfWriter()
        for _ in range(3):
            w.add_blank_page(width=595, height=842)
        buf = io.BytesIO(); w.write(buf)
        big = self.inbox / "big.pdf"; big.write_bytes(buf.getvalue())
        captured = {}
        def fake_post(payload, api_key, **kw):
            captured["n"] = len(payload["messages"][0]["content"][0]["source"]["data"]); return dict(FAKE_AI)
        with patch.object(llm, "FAX_MAX_BYTES", 10), patch.object(llm, "_post_json", side_effect=fake_post):
            # 자른 뒤에도 10바이트를 넘으니 거부 — 메시지가 FAX_AI_MAX_PAGES를 가리킨다
            with self.assertRaises(RuntimeError) as cm:
                llm.analyze_document(str(big))
            self.assertIn("FAX_AI_MAX_PAGES", str(cm.exception))
        with patch.object(llm, "_post_json", side_effect=fake_post):
            r = llm.analyze_document(str(big))
        self.assertEqual(r["_pages_total"], 3); self.assertTrue(captured["n"] > 0)

    def test_ai_gets_only_first_pages_of_long_pdf(self):
        import io, base64
        import llm
        from pypdf import PdfReader, PdfWriter
        w = PdfWriter()
        for _ in range(20):
            w.add_blank_page(width=595, height=842)
        buf = io.BytesIO(); w.write(buf)
        long_pdf = self.inbox / "long.pdf"; long_pdf.write_bytes(buf.getvalue())
        captured = {}
        def fake_post(payload, api_key, **kw):
            captured["payload"] = payload
            return dict(FAKE_AI)
        with patch.dict(os.environ, {"FAX_AI_MAX_PAGES": "5"}), patch.object(llm, "_post_json", side_effect=fake_post):
            r = llm.analyze_document(str(long_pdf))
        self.assertEqual((r["_pages_total"], r["_pages_sent"]), (20, 5))
        content = captured["payload"]["messages"][0]["content"]
        sent = base64.b64decode(content[0]["source"]["data"])
        self.assertEqual(len(PdfReader(io.BytesIO(sent)).pages), 5)
        self.assertIn("앞 5쪽만", content[1]["text"])
        # 워커 흐름에서 쪽수가 저장되고 화면에 표시된다
        with patch.object(fax_inbox, "analyze_file", return_value=dict(FAKE_AI, _pages_total=20, _pages_sent=5)):
            fax_inbox.scan_once()
        d = models.list_documents()[0]
        self.assertEqual(d["pages"], 20)
        self.assertIn("앞 5쪽만 판독", self.client.get(f"/documents/{d['id']}").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
