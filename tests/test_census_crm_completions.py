"""현재 재원 = 원무 명부 열린 회차 + 명부 이후 CRM에서 입원완료한 환자 (2026-09-16 규칙).

"입원완료는 어디에서 하든 현재 재원에 반영돼야 한다." 단 명부상 이미 퇴원한 옛
CRM 회차(746건)는 섞이면 안 되므로 ① 명부 마지막 입원일 이후 ② 지금도 입원완료
③ 명부가 그 입원을 모르는 것만 더한다. 회차 이력은 시간순으로 번호를 매긴다.
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


class CensusCrmCompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "census.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("census-test", "test-password", display_name="점검")
        self.uid = models.get_user("census-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="census-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        with models.get_db() as conn:
            for pid, name in ((1, "명부재원A"), (2, "명부재원B"), (3, "명부퇴원C"),
                              (4, "CRM신규D"), (5, "CRM옛날E"), (6, "CRM중복F")):
                conn.execute("INSERT INTO patients (id,name,gender) VALUES (?,?,'F')", (pid, name))
            # 명부: A·B 재원(D-10 입원), C는 D-30 입원 → D-20 퇴원. 명부 마지막 입원일 = D-10
            for pid, adm, dis in ((1, d(-10), None), (2, d(-10), None), (3, d(-30), d(-20))):
                conn.execute("""INSERT INTO admission_episodes
                    (patient_id, episode_no, status, admitted_at, discharged_at, room_number, ward, care_type, roster_key)
                    VALUES (?, 1, ?, ?, ?, '301호', '3병동', '회복기재활', ?)""",
                             (pid, "discharged" if dis else "admitted", adm, dis, f"c{pid}|{adm}"))
            # 상담: 각 환자 1건 (D-40 상담)
            for pid in (1, 2, 3, 4, 5, 6):
                conn.execute("INSERT INTO consultations (id, patient_id, consult_date, patient_age) VALUES (?,?,?,70)",
                             (pid, pid, d(-40)))

    def _complete(self, cid, on):
        r = self.client.post(f"/api/consult/{cid}/status", json={"admission_status": "입원완료", "admission_date": on})
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_census_adds_only_post_roster_crm_completions(self):
        self._complete(4, d(0))      # 명부 이후 CRM 입원완료 → 더한다
        self._complete(5, d(-40))    # 명부 이전인데 명부에 없음 → 옛 데이터, 안 더한다
        self._complete(6, d(-15))    # 명부 이후지만…
        with models.get_db() as conn:  # …명부가 D-12에 이 환자의 입원을 이미 안다 → 명부 회차가 대신
            conn.execute("""INSERT INTO admission_episodes
                (patient_id, episode_no, status, admitted_at, discharged_at, room_number, roster_key)
                VALUES (6, 2, 'discharged', ?, ?, '302호', 'c6|x')""", (d(-12), d(-11)))
        census = models.current_admission_census()
        self.assertEqual(census["patients"], {1, 2, 4})
        self.assertEqual((census["roster_count"], census["crm_count"]), (2, 1))
        self.assertEqual(census["by_consultation"][4]["source"], "crm")
        self.assertIsNone(census["by_consultation"][4]["ward"])   # 상담에 병실이 없으면 병동도 비움(명부 적재 때 채워진다)
        # 대시보드 스트립·이번주 입원
        with main.app.test_request_context():
            strip = main._ward_status_strip()
        self.assertEqual((strip["admitted"], strip["roster_n"], strip["crm_n"]), (3, 2, 1))
        self.assertEqual(strip["week_in"], 1)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("명부 2 + CRM 1", html)
        # 재원관리 명단·KPI에도 같은 숫자
        html = self.client.get("/ward").get_data(as_text=True)
        self.assertIn("명부 2 + CRM 1", html)

    def test_crm_discharge_removes_patient_from_census(self):
        self._complete(4, d(0))
        self.assertIn(4, models.current_admission_census()["patients"])
        r = self.client.post("/api/consult/4/discharge", json={"action": "complete", "discharge_date": d(0)})
        self.assertEqual(r.status_code, 200, r.get_json())
        census = models.current_admission_census()
        self.assertNotIn(4, census["patients"])
        self.assertEqual(census["crm_count"], 0)

    def test_history_numbers_stays_chronologically(self):
        """김한진 님: CRM 회차(상담이 먼저 만듦, 재입원 오늘)가 1회, 명부 회차(첫 입원)가 2회로 보였다."""
        self._complete(4, d(0))          # CRM 회차 = episode_no 1, 오늘 입원
        with models.get_db() as conn:    # 명부: 첫 입원 D-30 → D-10 퇴원 = episode_no 2
            conn.execute("""INSERT INTO admission_episodes
                (patient_id, episode_no, status, admitted_at, discharged_at, room_number, ward, roster_key)
                VALUES (4, 2, 'discharged', ?, ?, '204호', '2병동', 'c4|first')""", (d(-30), d(-10)))
        hist = models.patient_admission_history(4)
        self.assertEqual([(h["seq"], h["admitted_at"], h["source"], h["readmission"]) for h in hist],
                         [(1, d(-30), "명부", False), (2, d(0), "CRM", True)])
        html = self.client.get("/consult/4").get_data(as_text=True)
        self.assertIn("1회", html); self.assertIn("재입원", html)

    def test_history_merges_roster_and_crm_rows_for_same_admission(self):
        self._complete(4, d(0))
        with models.get_db() as conn:    # 다음 명부 적재로 같은 입원이 들어오면 한 줄로
            conn.execute("""INSERT INTO admission_episodes
                (patient_id, episode_no, status, admitted_at, room_number, ward, roster_key)
                VALUES (4, 2, 'admitted', ?, '204호', '2병동', 'c4|today')""", (d(0),))
        hist = models.patient_admission_history(4)
        self.assertEqual(len(hist), 1)
        self.assertEqual((hist[0]["source"], hist[0]["room_number"], hist[0]["consultation_id"]), ("명부+CRM", "204호", 4))
        # census도 두 번 세지 않는다 — 명부 회차가 대신한다
        census = models.current_admission_census()
        self.assertEqual((census["roster_count"], census["crm_count"]), (3, 0))


if __name__ == "__main__":
    unittest.main()
