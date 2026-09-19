"""관리 → 엑셀 적재 화면의 원무 입퇴원 명부 모드 (2026-09-19 요청).

상담내역 시트만 받던 화면이 명부 파일에 '스키마 감지 실패'를 냈다. 명부는 시트 1장·차트번호 기준이라
적재 경로가 아예 다르다 → 헤더로 알아보고 회차 적재 + 보험·입원일·발병일 백필 + 퇴원완료 전환을 돌린다.
현재 재원 환자는 어떤 경우에도 바뀌지 않는다.
"""
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import openpyxl

import app as main
import models
import partnerships
import support_requests


def d(n):
    return (date.today() + timedelta(days=n)).isoformat()


HEADERS = ["차트번호", "수진자명", "주민번호", "성별/나이", "입원일", "퇴원일", "총일수", "환자유형",
           "진료의사", "병동", "병실", "주소", "발병일", "주상병", "주상병명칭", "의사성명",
           "재활시작일자", "재활대상구분"]


class AdminRosterImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "roster.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("roster-test", "test-password", display_name="점검")
        self.uid = models.get_user("roster-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="roster-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        # CRM 쪽: 퇴원한 환자 1명 + 지금 재원 1명 (둘 다 상담 입원완료)
        with models.get_db() as conn:
            for pid, name, adm in ((1, "퇴원갑", d(-300)), (2, "재원을", d(-40))):
                conn.execute("INSERT INTO patients (id,name,gender) VALUES (?,?,'F')", (pid, name))
                conn.execute("""INSERT INTO consultations (id,patient_id,consult_date,admission_status,
                                actual_admission_date) VALUES (?,?,?,'입원완료',?)""", (pid, pid, adm, adm))
        self.xlsx = os.path.join(self.tmp.name, "입퇴재원환자현황(테스트).xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active; ws.title = "입퇴재원환자현황(테스트)"
        ws.append(HEADERS)
        ws.append(["0000000001", "퇴원갑", "500101-2000000", "여/76세", d(-300), d(-250), "51", "건강보험",
                   "재활의학과1", "3병동", "301호", "경북 안동시", d(-330), "I639^00", "뇌경색증", "이성범",
                   d(-300), "1.뇌"])
        ws.append(["0000000002", "재원을", "550101-2000000", "여/71세", d(-40), "", "41", "의료급여",
                   "재활의학과2", "5병동", "510호", "경북 예천군", d(-70), "I610^00", "뇌내출혈", "허남연",
                   d(-40), "1.뇌"])
        wb.save(self.xlsx); wb.close()
        os.replace(self.xlsx, os.path.join(self.tmp.name, "입퇴재원환자현황(테스트).xlsx"))
        self.fname = "입퇴재원환자현황(테스트).xlsx"

    def _post(self, action, **extra):
        return self.client.post("/admin/import", data={"file": self.fname, "action": action, **extra},
                                follow_redirects=True)

    def test_screen_detects_roster_and_offers_the_roster_form(self):
        html = self.client.get(f"/admin/import?file={self.fname}").get_data(as_text=True)
        self.assertIn("원무 입퇴원 명부", html)
        self.assertIn('value="roster-dryrun"', html)
        self.assertIn("퇴원완료 전환", html)
        self.assertNotIn('value="dryrun"', html)      # 상담내역 시트 폼은 안 뜬다

    def test_dryrun_reports_without_touching_db(self):
        html = self._post("roster-dryrun", close_discharged="1").get_data(as_text=True)
        self.assertIn("명부 행: 2", html)
        self.assertIn("dry-run", html)
        self.assertEqual(models.get_consultation(1)["admission_status"], "입원완료")
        with models.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM admission_episodes").fetchone()[0], 0)

    def test_apply_loads_episodes_backfills_and_closes_discharged(self):
        html = self._post("roster-apply", close_discharged="1").get_data(as_text=True)
        self.assertIn("명부 적재 완료", html)
        with models.get_db() as conn:
            eps = {r["roster_key"]: dict(r) for r in conn.execute(
                "SELECT roster_key, admitted_at, discharged_at, onset_date, insurance_type FROM admission_episodes")}
        self.assertEqual(sorted(eps), [f"0000000001|{d(-300)}", f"0000000002|{d(-40)}"])
        self.assertEqual(eps[f"0000000001|{d(-300)}"]["discharged_at"], d(-250))
        self.assertEqual(eps[f"0000000001|{d(-300)}"]["onset_date"], d(-330))    # 발병일까지
        # 보험유형은 환자 칸으로
        self.assertEqual(models.get_patient(1)["insurance_type"], "건강보험")
        self.assertEqual(models.get_patient(2)["insurance_type"], "의료급여")
        # 퇴원한 사람만 퇴원완료, 재원은 그대로
        self.assertEqual((models.get_consultation(1)["admission_status"],
                          models.get_consultation(1)["discharge_date"]), ("퇴원완료", d(-250)))
        self.assertEqual(models.get_consultation(2)["admission_status"], "입원완료")
        self.assertIsNone(models.get_consultation(2)["discharge_date"])
        self.assertEqual(models.current_admission_census()["patients"], {2})

    def test_apply_without_closing_keeps_status(self):
        self._post("roster-apply")           # 체크 해제 = close_discharged 없음
        self.assertEqual(models.get_consultation(1)["admission_status"], "입원완료")
        with models.get_db() as conn:        # 회차·발병일은 그대로 들어간다
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM admission_episodes").fetchone()[0], 2)

    def test_rerun_is_idempotent(self):
        self._post("roster-apply", close_discharged="1")
        self._post("roster-apply", close_discharged="1")
        with models.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM admission_episodes").fetchone()[0], 2)
        self.assertEqual(models.current_admission_census()["patients"], {2})


if __name__ == "__main__":
    unittest.main()
