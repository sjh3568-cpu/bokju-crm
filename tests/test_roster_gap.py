"""원무 명부에 없는 재원 — 재원 수가 원무와 다를 때 그 차이가 누구인지 화면에서 바로 찾게 (2026-09-28 요청).

CRM 재원 = 명부 열린 회차 + 명부 기준일 이후 CRM 입원완료. 두 숫자가 어긋나면 늘 뒤쪽이 원인인데,
그걸 보여 주는 화면이 없어 백업 파일을 뒤져야 했다("자체적으로 알 수 없나").
"""
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import app as main
import models
import partnerships
import support_requests


def d(n):
    return (date.today() + timedelta(days=n)).isoformat()


class RosterGapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "gap.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("gap-test", "test-password", display_name="점검")
        self.uid = models.get_user("gap-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="gap-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        with models.get_db() as conn:
            # 1) 명부에 있는 재원 — 정상
            conn.execute("INSERT INTO patients (id,name,gender,chart_no) VALUES (1,'명부환자','M','0000000001')")
            conn.execute("""INSERT INTO consultations (id,patient_id,consult_date,admission_status,
                            actual_admission_date,room_number) VALUES (1,1,?,'입원완료',?,'301호')""", (d(-40), d(-30)))
            conn.execute("""INSERT INTO admission_episodes (patient_id,consultation_id,episode_no,status,
                            admitted_at,room_number,ward,roster_key)
                            VALUES (1,1,1,'admitted',?, '301호','3병동','c1|x')""", (d(-30),))
            # 2) 명부 기준일 이후 CRM 입원완료 — 이게 차이의 정체. 차트번호도 없다(백승환 님 사례)
            conn.execute("INSERT INTO patients (id,name,gender) VALUES (2,'명부밖환자','M')")
            conn.execute("""INSERT INTO consultations (id,patient_id,consult_date,admission_status,
                            actual_admission_date,room_number) VALUES (2,2,?,'입원완료',?,'1003호')""", (d(-20), d(-1)))
            # 입원완료를 누르면 CRM 회차(roster_key 없음)가 함께 생긴다 — 재원 집계가 이 행을 본다
            conn.execute("""INSERT INTO admission_episodes (patient_id,consultation_id,episode_no,status,
                            admitted_at,room_number) VALUES (2,2,1,'admitted',?, '1003호')""", (d(-1),))

    def test_census_counts_both_but_marks_the_source(self):
        c = models.current_admission_census()
        self.assertEqual(len(c["patients"]), 2)
        self.assertEqual((c["roster_count"], c["crm_count"]), (1, 1))

    def test_quality_report_names_the_person(self):
        rep = models.data_quality_report()
        check = next(c for c in rep["checks"] if c["title"] == "원무 명부에 없는 재원")
        self.assertEqual(check["count"], 1)
        row = check["rows"][0]
        self.assertEqual(row["patient_name"], "명부밖환자")
        self.assertEqual(row["actual_admission_date"], d(-1))
        self.assertIn("적재된 명부는", check["description"])      # 명부가 어디까지인지 함께 알려 준다

    def test_quality_check_is_empty_when_roster_covers_everyone(self):
        with models.get_db() as conn:
            # 명부 회차는 상담과 따로 들어온다(한 상담에 회차는 하나뿐이라 consultation_id는 비워 둔다)
            conn.execute("""INSERT INTO admission_episodes (patient_id,episode_no,status,
                            admitted_at,room_number,ward,roster_key)
                            VALUES (2,2,'admitted',?, '1003호','10병동','c2|y')""", (d(-1),))
        check = next(c for c in models.data_quality_report()["checks"] if c["title"] == "원무 명부에 없는 재원")
        self.assertEqual(check["count"], 0)

    def test_ward_header_links_to_the_list(self):
        html = self.client.get("/ward").get_data(as_text=True)
        self.assertIn('class="wd-roster-gap"', html)
        self.assertIn("명부 밖 1명", html)
        self.assertIn('href="/ward?tab=quality"', html)

    def test_ward_header_says_nothing_when_there_is_no_gap(self):
        with models.get_db() as conn:
            # 명부 회차는 상담과 따로 들어온다(한 상담에 회차는 하나뿐이라 consultation_id는 비워 둔다)
            conn.execute("""INSERT INTO admission_episodes (patient_id,episode_no,status,
                            admitted_at,room_number,ward,roster_key)
                            VALUES (2,2,'admitted',?, '1003호','10병동','c2|y')""", (d(-1),))
        html = self.client.get("/ward").get_data(as_text=True)
        # 'wd-roster-gap'은 스타일 규칙 이름으로도 늘 들어 있다 — 배지 문구로 확인한다
        self.assertNotIn("명부 밖", html)


if __name__ == "__main__":
    unittest.main()
