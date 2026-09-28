# -*- coding: utf-8 -*-
"""상담내역 구글 시트 → CRM 자동 반영(consult_sheet_sync) — 시트를 받지 않고 SheetBook을 직접 넣어 검사한다.

규칙(2026-09-28 사용자 결정): 새 행은 추가, 고친 행은 바뀐 칸만 반영, 시트에서 지운 행은 CRM에 남긴다.
CRM에서도 고친 칸은 CRM 값을 지키고 충돌로 보고한다. 예전 엑셀 적재분은 처음 만나면 연결만 한다(기준선)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as main
import models
import consult_sheet_sync as sync

# 스키마 C(2025.12~) 머리글 — 1행은 묶음 라벨, 2행이 머리글, 3행부터 자료(parse_sheet가 2·3행에서 머리글을 찾는다)
HEADERS = ["상담일자", "요일", "환자이름", "성별", "나이", "이름", "관계", "연락처", "연고지1(도)", "연고지2(지역)",
           "상담자", "상담방법", "현재 거처", "병원이름", "병원정보확인", "세부 경로", "입원 목적", "대상환자군",
           "대상질환", "병명", "입원여부", "입원일 / 비고", "재 접촉 관리 현황"]


def row(name, date, *, age=70, phone="010-1111-2222", status="", hospital="안동병원", memo="", disease="뇌경색",
        counselor="", purpose="회복기재활"):
    return [f"{date} 00:00:00", "월", name, "여", age, "보호자", "자녀", phone, "경북", "안동시", counselor, "전화",
            "입원중", hospital, "소개", "", purpose, "중추신경계", "", disease, status, "", memo]


def book(rows, sheet="26.9월"):
    return sync.SheetBook([{"name": sheet, "rows": [[""] * len(HEADERS), HEADERS] + rows}])


class ConsultSheetSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "sheet.db"))
        db.start(); self.addCleanup(db.stop)
        st = patch.object(sync, "_status_path", lambda: Path(self.tmp.name) / "status.json")
        st.start(); self.addCleanup(st.stop)
        bk = patch.object(sync, "_needs_backup", lambda: False)      # 임시 DB 백업은 건너뛴다
        bk.start(); self.addCleanup(bk.stop)
        models.init_db()
        sync.init_schema()

    def _consults(self):
        with models.get_db() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT c.id, p.name, c.consult_date, c.admission_status, c.source_hospital, c.recontact_memo, "
                "c.import_source, c.patient_id FROM consultations c JOIN patients p ON p.id=c.patient_id ORDER BY c.id")]

    def _links(self):
        with models.get_db() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM consult_sheet_links ORDER BY id")]

    def test_preview_changes_nothing_and_apply_inserts_new_rows(self):
        b = book([row("김새롬", "2026-09-10", status="입원예정"), row("이둘째", "2026-09-11")])
        rep = sync.run(apply=False, book=b)
        self.assertTrue(rep["ok"]); self.assertEqual(rep["totals"]["new"], 2)
        self.assertEqual(self._consults(), [])                       # 미리보기는 DB를 안 바꾼다
        rep = sync.run(apply=True, book=b)
        cons = self._consults()
        self.assertEqual([(c["name"], c["consult_date"], c["admission_status"], c["import_source"]) for c in cons],
                         [("김새롬", "2026-09-10", "입원예정", "sheet"), ("이둘째", "2026-09-11", None, "sheet")])
        links = self._links()
        self.assertEqual([(l["sheet_name"], l["row_no"], l["row_key"]) for l in links],
                         [("26.9월", 3, "김새롬|2026-09-10"), ("26.9월", 4, "이둘째|2026-09-11")])
        # 다시 돌리면 아무것도 안 바뀐다(멱등)
        rep = sync.run(apply=True, book=b)
        self.assertEqual((rep["totals"]["new"], rep["totals"]["updated"], rep["totals"]["unchanged"]), (0, 0, 2))
        self.assertEqual(len(self._consults()), 2)

    def test_edited_row_updates_only_changed_fields(self):
        sync.run(apply=True, book=book([row("김새롬", "2026-09-10", hospital="안동병원", memo="")]))
        cid = self._consults()[0]["id"]
        # CRM에서 메모를 따로 적어 둔다
        with models.get_db() as conn:
            conn.execute("UPDATE consultations SET recontact_memo='CRM에서 적음' WHERE id=?", (cid,))
        # 시트에서 병원이름만 고침 → 병원이름은 반영, CRM 메모는 그대로
        rep = sync.run(apply=True, book=book([row("김새롬", "2026-09-10", hospital="서울아산병원", memo="")]))
        self.assertEqual(rep["totals"]["updated"], 1)
        c = self._consults()[0]
        self.assertEqual((c["source_hospital"], c["recontact_memo"]), ("서울아산병원", "CRM에서 적음"))
        # 병원이름 칸 하나가 상담의 병원이름·거처 이름 두 컬럼으로 풀리므로 둘 다 반영된다
        self.assertEqual(rep["updates"][0]["fields"], ["거처 이름", "병원이름"])

    def test_conflict_keeps_crm_value_and_reports_once(self):
        sync.run(apply=True, book=book([row("김새롬", "2026-09-10", hospital="안동병원")]))
        cid = self._consults()[0]["id"]
        with models.get_db() as conn:
            conn.execute("UPDATE consultations SET source_hospital='CRM병원' WHERE id=?", (cid,))
        b = book([row("김새롬", "2026-09-10", hospital="시트병원")])
        rep = sync.run(apply=True, book=b)
        # 병원이름은 충돌(CRM 유지), 같은 칸에서 풀린 거처 이름은 CRM에서 안 고쳤으니 반영 → 수정 1
        self.assertEqual((rep["totals"]["conflicts"], rep["totals"]["updated"]), (1, 1))
        self.assertEqual(self._consults()[0]["source_hospital"], "CRM병원")
        self.assertEqual((rep["conflicts"][0]["field"], rep["conflicts"][0]["sheet_value"], rep["conflicts"][0]["crm_value"]),
                         ("병원이름", "시트병원", "CRM병원"))
        # 기준선이 시트 값으로 옮겨져 같은 충돌을 다시 보고하지 않는다
        rep = sync.run(apply=True, book=b)
        self.assertEqual((rep["totals"]["conflicts"], rep["totals"]["unchanged"]), (0, 1))

    def test_inserted_row_shifts_positions_but_keys_still_match(self):
        sync.run(apply=True, book=book([row("김새롬", "2026-09-10"), row("이둘째", "2026-09-11")]))
        # 맨 위에 행이 끼어들어 기존 행 번호가 밀린다 — 키(이름|날짜)로 찾아 새 행만 추가한다
        rep = sync.run(apply=True, book=book([row("박신규", "2026-09-09"), row("김새롬", "2026-09-10"),
                                              row("이둘째", "2026-09-11")]))
        self.assertEqual((rep["totals"]["new"], rep["totals"]["unchanged"]), (1, 2))
        self.assertEqual([(l["row_key"], l["row_no"]) for l in self._links()],
                         [("김새롬|2026-09-10", 4), ("이둘째|2026-09-11", 5), ("박신규|2026-09-09", 3)])

    def test_name_typo_fix_matches_by_position(self):
        sync.run(apply=True, book=book([row("김새름", "2026-09-10")]))
        rep = sync.run(apply=True, book=book([row("김새롬", "2026-09-10")]))
        self.assertEqual((rep["totals"]["new"], rep["totals"]["updated"]), (0, 1))
        cons = self._consults()
        self.assertEqual(len(cons), 1)
        self.assertEqual(cons[0]["name"], "김새롬")          # 환자 이름도 고쳐진다

    def test_deleted_row_is_reported_not_deleted(self):
        sync.run(apply=True, book=book([row("김새롬", "2026-09-10"), row("이둘째", "2026-09-11")]))
        rep = sync.run(apply=True, book=book([row("김새롬", "2026-09-10")]))
        self.assertEqual(rep["totals"]["missing"], 1)
        self.assertEqual(len(self._consults()), 2)
        self.assertTrue(self._links()[1]["missing_since"])
        # 다시 나타나면 표시가 풀린다
        sync.run(apply=True, book=book([row("김새롬", "2026-09-10"), row("이둘째", "2026-09-11")]))
        self.assertIsNone(self._links()[1]["missing_since"])

    def test_existing_consultation_is_adopted_without_changes(self):
        """예전 엑셀 적재분 — 같은 환자·날짜가 이미 있으면 연결만 하고 값은 안 바꾼다(기준선)."""
        with models.get_db() as conn:
            conn.execute("INSERT INTO patients (id, name, gender) VALUES (1, '김새롬', 'F')")
            conn.execute("INSERT INTO consultations (id, patient_id, consult_date, source_hospital, import_source) "
                         "VALUES (1, 1, '2026-09-10', '기존병원', 'excel')")
        rep = sync.run(apply=True, book=book([row("김새롬", "2026-09-10", hospital="시트병원")]))
        self.assertEqual((rep["totals"]["adopted"], rep["totals"]["new"]), (1, 0))
        self.assertEqual(self._consults()[0]["source_hospital"], "기존병원")
        self.assertEqual(self._links()[0]["consultation_id"], 1)
        # 그 뒤 시트에서 고치면 반영된다(기준선=시트 값이었으므로 CRM 값과 달라도 충돌은 아니다 — 처음 한 번은 CRM 값을 지킨다)
        rep = sync.run(apply=True, book=book([row("김새롬", "2026-09-10", hospital="새병원")]))
        self.assertEqual(rep["totals"]["conflicts"], 2)          # 병원이름·거처 이름 모두 CRM ≠ 기준선 '시트병원' → CRM 유지
        self.assertEqual(self._consults()[0]["source_hospital"], "기존병원")

    def test_status_change_syncs_episode_and_stage(self):
        sync.run(apply=True, book=book([row("김새롬", "2026-09-10", status="입원예정")]))
        with patch.object(models, "sync_admission_episode") as ep:
            rep = sync.run(apply=True, book=book([row("김새롬", "2026-09-10", status="입원완료")]))
        self.assertEqual(rep["totals"]["updated"], 1)
        self.assertEqual(self._consults()[0]["admission_status"], "입원완료")
        ep.assert_called_once()
        self.assertEqual(models.get_patient(self._consults()[0]["patient_id"]).get("lifecycle_stage"), "입원")

    def test_waiting_sheet_and_unknown_tabs_are_skipped(self):
        b = sync.SheetBook([
            {"name": "입원환자 대기 명단", "rows": [[""] * len(HEADERS), HEADERS, row("대기자", "2026-08-01")]},
            {"name": "메모", "rows": [["아무거나"], ["x", "y"]]},
            {"name": "26.9월", "rows": [[""] * len(HEADERS), HEADERS, row("김새롬", "2026-09-10")]},
        ])
        rep = sync.run(apply=True, book=b)
        self.assertEqual(rep["totals"]["new"], 1)
        self.assertEqual([s["sheet"] for s in rep["sheets"] if s.get("skipped")], ["입원환자 대기 명단", "메모"])
        self.assertIn("대기 명단", sync.render_report(rep))

    def test_status_file_and_auto_toggle(self):
        self.assertFalse(sync.auto_enabled())
        sync.set_auto(True)
        self.assertTrue(sync.auto_enabled())
        sync.run(apply=False, book=book([row("김새롬", "2026-09-10")]))
        st = sync.status()
        self.assertTrue(st["auto"])                                   # 실행이 auto 값을 지우지 않는다
        self.assertEqual(st["last"]["totals"]["new"], 1)
        self.assertIn("미리보기", st["last_report_text"])

    def test_admin_page_shows_card_and_actions(self):
        models.ensure_admin_user("sheet-admin", "pw", display_name="관리")
        uid = models.get_user("sheet-admin")["id"]
        boot = patch.object(main, "_db_initialized", True); boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        client = main.app.test_client()
        with client.session_transaction() as s:
            s.update(user_id=uid, username="sheet-admin", display_name="관리", role="admin",
                     cooperation_permissions_v2=True, perms={k: 3 for k in main.MENU_KEYS})
        html = client.get("/admin/import").get_data(as_text=True)
        self.assertIn("구글 시트 자동 반영", html)
        self.assertIn("아직 연결되지 않았습니다", html)               # .env 없음
        with patch.dict(os.environ, {"CONSULT_SHEET_URL": "https://x/exec", "CONSULT_SHEET_TOKEN": "t"}), \
             patch.object(sync, "fetch_sheets", return_value=[{"name": "26.9월", "rows": [[""] * len(HEADERS), HEADERS,
                                                                                         row("김새롬", "2026-09-10")]}]):
            html = client.get("/admin/import").get_data(as_text=True)
            self.assertIn("미리보기 (DB 변경 없음)", html)
            r = client.post("/admin/consult-sheet", data={"action": "preview"}, follow_redirects=True)
            self.assertIn("미리보기 완료", r.get_data(as_text=True))
            self.assertEqual(self._consults(), [])
            r = client.post("/admin/consult-sheet", data={"action": "apply"}, follow_redirects=True)
            self.assertIn("반영 완료", r.get_data(as_text=True))
            self.assertEqual(len(self._consults()), 1)
            client.post("/admin/consult-sheet", data={"action": "auto_on"}, follow_redirects=True)
            self.assertTrue(sync.auto_enabled())


if __name__ == "__main__":
    unittest.main()
