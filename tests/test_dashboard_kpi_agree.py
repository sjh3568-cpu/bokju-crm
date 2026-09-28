"""대시보드 KPI(현재 상태 스트립)의 회복기 수가 재원관리 카드와 같은가 (2026-09-28).

대시보드 _ward_status_strip은 상담 행에 명부 수가구분·재활종료일만 얹고 명부 입원일 등은 덮어쓰지
않았다. 상담에 적힌 입원일(예정값)과 명부 입원일이 다르면 _care_phase의 수가 기간 계산이 갈려,
같은 환자를 재원관리는 회복기·대시보드는 비회복기로 셌다(한영도 님, 114 vs 113).
이제 두 화면이 views.ward.apply_episode_to_row 하나로 행을 만든다.
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


class DashboardKpiAgreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "kpi.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("kpi-test", "test-password", display_name="점검")
        self.uid = models.get_user("kpi-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="kpi-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        with models.get_db() as conn:
            # 1) 한영도 님 유형 — 상담에는 옛 입원일(1년 전 예정값)이 남아 있고, 명부 입원일은 20일 전.
            #    발병 30일 전 뇌출혈이라 명부 입원일 기준이면 회복기, 상담 입원일 기준이면 기간 만료로 비회복기.
            conn.execute("INSERT INTO patients (id,name,gender,chart_no) VALUES (1,'입원일불일치','M','0000000001')")
            conn.execute("""INSERT INTO consultations (id,patient_id,consult_date,admission_status,actual_admission_date,
                            room_number,diseases,disease_onset,admission_purpose)
                            VALUES (1,1,?,'입원완료',?,'301호','["뇌출혈"]',?,'회복기재활')""", (d(-380), d(-370), d(-30)))
            conn.execute("""INSERT INTO admission_episodes (patient_id,episode_no,status,admitted_at,room_number,ward,roster_key)
                            VALUES (1,1,'admitted',?,'301호','3병동','c1|x')""", (d(-20),))
            # 2) 보통 환자 — 두 화면이 원래도 같게 세던 경우
            conn.execute("INSERT INTO patients (id,name,gender,chart_no) VALUES (2,'보통환자','F','0000000002')")
            conn.execute("""INSERT INTO consultations (id,patient_id,consult_date,admission_status,actual_admission_date,
                            room_number,diseases,disease_onset,admission_purpose)
                            VALUES (2,2,?,'입원완료',?,'302호','["뇌경색"]',?,'회복기재활')""", (d(-40), d(-20), d(-30)))
            conn.execute("""INSERT INTO admission_episodes (patient_id,episode_no,status,admitted_at,room_number,ward,roster_key)
                            VALUES (2,1,'admitted',?,'302호','3병동','c2|x')""", (d(-20),))
            # 3) 비회복기 — 분모만 늘린다
            conn.execute("INSERT INTO patients (id,name,gender,chart_no) VALUES (3,'비회복기','M','0000000003')")
            conn.execute("""INSERT INTO consultations (id,patient_id,consult_date,admission_status,actual_admission_date,
                            room_number,diseases,disease_onset,admission_purpose)
                            VALUES (3,3,?,'입원완료',?,'303호','["뇌경색"]',?,'회복기재활')""", (d(-400), d(-390), d(-400)))
            conn.execute("""INSERT INTO admission_episodes (patient_id,episode_no,status,admitted_at,room_number,ward,roster_key)
                            VALUES (3,1,'admitted',?,'303호','3병동','c3|x')""", (d(-390),))

    def test_dashboard_strip_matches_ward_card(self):
        from views.main import _ward_status_strip
        from views.ward import _ward_admitted_roster
        admitted = _ward_admitted_roster("", "")
        card_rec = sum(1 for c in admitted if c.get("care_phase") == "회복기")
        self.assertEqual((len(admitted), card_rec), (3, 2), "명부 입원일 기준이면 1·2번이 회복기")
        with main.app.test_request_context():
            strip = _ward_status_strip()
        self.assertEqual((strip["recovery_judged"], strip["recovery"]), (3, 2))
        self.assertEqual(strip["recovery_ratio"], 66.67)

    def test_ward_row_carries_roster_facts(self):
        """공용 행 빌더가 명부 입원일·병실·수가구분·발병일을 상담 행에 덮어쓴다."""
        from views.ward import apply_episode_to_row
        c = {"id": 1, "actual_admission_date": d(-370), "room_number": "예정호", "discharge_date": d(-1)}
        ep = {"id": 9, "admitted_at": d(-20), "room_number": "301호", "ward": "3병동", "care_type": "회복기재활",
              "rehab_end_date": None, "rehab_end_imported": 0, "onset_date": d(-30), "diagnosis_name": "뇌내출혈",
              "attending_doctor": " 김우근 "}
        apply_episode_to_row(c, ep)
        self.assertEqual(c["actual_admission_date"], d(-20))
        self.assertIsNone(c["discharge_date"])
        self.assertEqual((c["room_number"], c["roster_ward"], c["attending_doctor"]), ("301호", "3병동", "김우근"))
        self.assertEqual((c["roster_care_phase"], c["onset_date"], c["roster_diagnosis"]), ("회복기", d(-30), "뇌내출혈"))


if __name__ == "__main__":
    unittest.main()
