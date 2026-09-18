# -*- coding: utf-8 -*-
"""대시보드 입·퇴원 현황의 퇴원 행 (2026-09-18 요청).

9/17에 퇴원 완료한 환자가 '입원 환자 현황'에 아예 안 나왔다. 그 표가 입원일만 보고,
퇴원은 '오늘 퇴원' 소제목이 오늘 날짜로만 뽑고 있어서다(9/18에 9/17 퇴원을 적으면 어디에도 없다).
→ 같은 표에 퇴원 행을 넣고 구분 탭으로 거른다.
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


class DashboardDischargeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "out.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("out-test", "test-password", display_name="점검")
        self.uid = models.get_user("out-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="out-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        with models.get_db() as conn:
            for pid, name in ((1, "어제퇴원"), (2, "명부만퇴원"), (3, "오늘입원")):
                conn.execute("INSERT INTO patients (id,name,gender,chart_no) VALUES (?,?,'M',?)",
                             (pid, name, "000%d" % pid))
            # 1번 — 앱에서 퇴원 처리한 환자. 상담·CRM 회차·명부 회차가 모두 닫힌다(한 줄로 묶여야 한다).
            conn.execute("""INSERT INTO consultations (id, patient_id, consult_date, admission_status,
                            actual_admission_date, discharge_date, discharge_destination, discharge_reason,
                            attending_doctor, room_number, patient_age, disease_detail, primary_diagnosis)
                            VALUES (1, 1, ?, '퇴원완료', ?, ?, '자택', '재활 종료',
                                    'RM1 이성범 부장', '305호', 71, '뇌경색 섬망', '뇌경색')""",
                         (d(-90), d(-60), d(-1)))
            conn.execute("""INSERT INTO admission_episodes
                            (patient_id, episode_no, status, consultation_id, admitted_at, discharged_at, room_number)
                            VALUES (1, 1, 'discharged', 1, ?, ?, '305호')""", (d(-60), d(-1)))
            conn.execute("""INSERT INTO admission_episodes
                            (patient_id, episode_no, status, admitted_at, discharged_at, room_number, ward,
                             attending_doctor, diagnosis_name, discharge_destination, roster_key)
                            VALUES (1, 2, 'discharged', ?, ?, '305호', '3병동', '변현숙', '뇌경색증', '자택', ?)""",
                         (d(-60), d(-1), "0001|%s" % d(-60)))
            # 2번 — 상담 없이 입원한 환자(원무 명부만). 이름 링크 없이 줄이 만들어져야 한다.
            conn.execute("""INSERT INTO admission_episodes
                            (patient_id, episode_no, status, admitted_at, discharged_at, room_number, ward,
                             attending_doctor, diagnosis_name, discharge_destination, roster_key)
                            VALUES (2, 1, 'discharged', ?, ?, '501호', '5병동', '변현숙', '고관절 골절', '요양병원', ?)""",
                         (d(-30), d(-1), "0002|%s" % d(-30)))
            # 3번 — 오늘 입원. 퇴원 행이 '오늘 입원' 집계를 부풀리지 않는지 보는 대조군.
            conn.execute("""INSERT INTO consultations (id, patient_id, consult_date, admission_status,
                            actual_admission_date, attending_doctor, room_number, patient_age)
                            VALUES (3, 3, ?, '입원완료', ?, 'RM1 이성범 부장', '306호', 68)""", (d(-10), d(0)))
        models.sync_admission_episode(3)

    def rows(self, scope="all", lo=None, hi=None):
        data = models.dashboard_summary(lo or d(-1), hi or d(1), scope)
        return data, {r["patient_id"]: r for r in data["admission_selected"]}

    def test_discharge_recorded_today_for_yesterday_shows_on_its_own_date(self):
        """어제 퇴원을 오늘 적어도 기본 창(어제~내일)의 어제 자리에 나온다 — 옛 화면에는 어디에도 없었다."""
        _, rows = self.rows()
        self.assertIn(1, rows, "어제 퇴원한 환자가 표에 있어야 한다")
        row = rows[1]
        self.assertEqual(row["admission_kind"], "discharge")
        self.assertEqual(row["admission_bucket"], "discharged")
        self.assertEqual(row["admission_display_date"], d(-1))

    def test_one_row_per_patient_even_when_consultation_and_both_episodes_closed(self):
        """앱에서 퇴원 처리하면 상담·CRM 회차·명부 회차가 다 닫힌다 — 그래도 한 줄이다."""
        data, _ = self.rows()
        mine = [r for r in data["admission_selected"] if r["patient_id"] == 1]
        self.assertEqual(len(mine), 1, [r["admission_kind"] for r in mine])
        self.assertEqual(mine[0]["discharge_source"], "CRM · 명부")   # 두 근거를 다 표시

    def test_discharge_row_carries_stay_days_destination_and_roster_fields(self):
        _, rows = self.rows()
        row = rows[1]
        self.assertEqual(row["discharge_admitted_at"], d(-60))
        self.assertEqual(row["discharge_stay_days"], 59)
        self.assertEqual(row["other_note"], "→ 자택")
        self.assertEqual(row["ward"], "3병동")                    # 명부 병동
        self.assertEqual(row["attending_doctor"], "변현숙")        # 명부 주치의가 상담 값을 덮는다
        self.assertIn("재원 59일", row["other_note_title"])
        self.assertEqual(row["id"], 1)                            # 상담 링크
        self.assertFalse(row["readmission"])                      # 자기 입원이 '이전 입원'으로 잡히지 않는다

    def test_roster_only_patient_gets_a_row_without_a_consultation_link(self):
        _, rows = self.rows()
        self.assertIn(2, rows)
        self.assertIsNone(rows[2].get("id"))
        self.assertEqual(rows[2]["patient_name"], "명부만퇴원")
        self.assertEqual(rows[2]["discharge_stay_days"], 29)
        self.assertEqual(rows[2]["admission_disease_summary"], "고관절 골절")   # 명부 진단명

    def test_scope_tabs_count_and_filter(self):
        data, _ = self.rows()
        counts = data["summary"]["admission_selected_counts"]
        self.assertEqual(counts["discharged"], 2)
        self.assertEqual(counts["completed"], 1)     # 오늘 입원 1건
        self.assertEqual(counts["all"], 3)
        _, rows = self.rows(scope="discharged")
        self.assertEqual(sorted(rows), [1, 2])
        _, rows = self.rows(scope="completed")
        self.assertEqual(sorted(rows), [3])

    def test_discharge_rows_do_not_inflate_admission_counts(self):
        """KPI·업무 큐가 쓰는 창에는 퇴원 행을 섞지 않는다 — '오늘 입원'이 부풀면 안 된다."""
        data, _ = self.rows()
        self.assertEqual(data["summary"]["admission_today"], 1)
        self.assertEqual(data["summary"]["admission_today_completed"], 1)
        self.assertTrue(all(r.get("admission_kind") != "discharge"
                            for r in data["admission_schedule"]))

    def test_care_items_come_from_the_consultation_on_discharge_rows_too(self):
        """퇴원 행도 상태·처치를 상담일지에서 그대로 가져온다(자유 기재의 '섬망')."""
        _, rows = self.rows()
        self.assertEqual([t["label"] for t in rows[1]["admission_care"]["tags"]], ["섬망"])

    def test_dashboard_page_renders_discharge_rows_and_scope_tabs(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("입·퇴원 현황", html)
        self.assertIn("adm-kind-out", html)          # 퇴원 구분 배지
        self.assertIn("어제퇴원", html)
        self.assertIn("명부만퇴원", html)
        self.assertIn("dash-scope-btn", html)        # 구분 탭
        self.assertIn("admission_scope=discharged", html)
        self.assertIn(">59일</small>", html)          # 시간 자리에 재원일수
        # 따로 있던 '오늘 퇴원' 소제목 표는 이 표에 합쳤다(KPI 카드의 오늘 입·퇴원 수는 그대로 둔다)
        self.assertNotIn("dash-discharge-today", html)

    def test_today_discharge_kpi_counts_crm_only_discharges(self):
        """KPI '오늘 입원·퇴원'의 퇴원 수 — 명부 회차가 없는 CRM 퇴원도 센다.

        전에는 명부 회차(roster_key)만 세서, 명부에 없는 환자를 CRM에서 퇴원 처리하면 0으로 남았다.
        """
        with models.get_db() as conn:
            conn.execute("INSERT INTO patients (id,name,gender) VALUES (4,'오늘퇴원','F')")
            conn.execute("""INSERT INTO consultations (id, patient_id, consult_date, admission_status,
                            actual_admission_date, discharge_date, discharge_destination,
                            attending_doctor, room_number, patient_age)
                            VALUES (4, 4, ?, '퇴원완료', ?, ?, '자택', 'RM1 이성범 부장', '307호', 80)""",
                         (d(-50), d(-20), d(0)))
        data, rows = self.rows()
        self.assertEqual(data["summary"]["discharge_today"], 1)
        self.assertEqual(rows[4]["discharge_stay_days"], 20)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("오늘퇴원", html)

    def test_lookup_range_in_the_past_shows_that_periods_discharges(self):
        """기간을 지난 날짜로 잡아도 그 기간의 퇴원이 나온다."""
        _, rows = self.rows(lo=d(-2), hi=d(-1))
        self.assertEqual(sorted(rows), [1, 2])


if __name__ == "__main__":
    unittest.main()
