"""외진 나감 = 그날의 퇴원 (2026-09-18 요청: 권현수 님 9/16 안동병원 응급전원).

복귀를 그날의 입원으로 세는 것과 대칭이다. 원무 명부도 외진 나간 날 퇴원, 복귀한 날 새 입원으로 적는다.
명부·CRM이 그날 퇴원으로 이미 적었으면 두 번 세지 않는다.
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
import ward_moves


def d(n):
    return (date.today() + timedelta(days=n)).isoformat()


class AwayAsDischargeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "away.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("away-test", "test-password", display_name="점검")
        self.uid = models.get_user("away-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="away-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        with models.get_db() as conn:
            for pid, name in ((1, "권현수"), (2, "명부퇴원"), (3, "전원환자")):
                conn.execute("INSERT INTO patients (id,name,gender) VALUES (?,?,'M')", (pid, name))
                conn.execute("""INSERT INTO consultations (id, patient_id, consult_date, admission_status,
                                actual_admission_date, attending_doctor, room_number, patient_age, primary_diagnosis)
                                VALUES (?,?,?,'입원완료',?,'RM1 이성범 부장','309호',70,'뇌경색증')""",
                             (pid, pid, d(-90), d(-60)))
            # 1) 권현수 — 이틀 전 응급전원, 아직 미복귀. 명부 회차는 열린 채(퇴원 기록 없음)
            conn.execute("""INSERT INTO admission_episodes (patient_id, episode_no, status, admitted_at, room_number, ward, roster_key)
                            VALUES (1, 1, 'admitted', ?, '309호', '3병동', 'c1|x')""", (d(-60),))
            conn.execute("""INSERT INTO admission_events (consultation_id, event_type, event_date, hospital)
                            VALUES (1, '응급전원', ?, '안동병원')""", (d(-2),))
            # 2) 명부가 같은 날 퇴원으로 이미 적은 건 — 두 번 세면 안 된다
            conn.execute("""INSERT INTO admission_episodes (patient_id, episode_no, status, admitted_at, discharged_at, room_number, ward, roster_key)
                            VALUES (2, 1, 'discharged', ?, ?, '310호', '3병동', 'c2|x')""", (d(-60), d(-2)))
            conn.execute("""INSERT INTO admission_events (consultation_id, event_type, event_date, hospital)
                            VALUES (2, '응급전원', ?, '안동병원')""", (d(-2),))
            # 3) 타 병원 전원으로 종결 — 종결 처리가 따로 퇴원을 만드므로 나감은 세지 않는다
            conn.execute("""INSERT INTO admission_episodes (patient_id, episode_no, status, admitted_at, room_number, ward, roster_key)
                            VALUES (3, 1, 'admitted', ?, '311호', '3병동', 'c3|x')""", (d(-60),))
            conn.execute("""INSERT INTO admission_events (consultation_id, event_type, event_date, hospital, returned_at, return_outcome, return_hospital)
                            VALUES (3, '응급전원', ?, '안동병원', ?, '전원', '서울병원')""", (d(-2), d(-1)))

    def test_query_lists_only_countable_departures(self):
        rows = {r["patient_name"]: r for r in models.away_departures_as_discharges(d(-7), d(0))}
        self.assertEqual(sorted(rows), ["권현수"])          # 명부가 그날 퇴원으로 적은 건·타 병원 전원은 빠진다
        r = rows["권현수"]
        self.assertEqual((str(r["event_date"])[:10], r["hospital"], r["event_type"]), (d(-2), "안동병원", "응급전원"))
        self.assertEqual(r["room_number" if "room_number" in r else "ep_room"], "309호")

    def test_week_out_counts_the_departure(self):
        """외진 나감이 주간·월간 퇴원 수에 들어간다.

        사건일이 '이틀 전'이라 월·화요일이나 매달 1·2일에는 이번 주/이번 달 창 밖으로 나간다 —
        그래서 집계는 사건일을 포함하는 창으로 직접 확인하고, 대시보드 스트립은 실제 창에
        맞춰 기대값을 계산한다(요일 따라 깨지던 테스트, 2026-09-21).
        """
        flow = models.admission_flow_counts(d(-7), d(0), d(-7), d(0))
        self.assertEqual((flow["week_out"], flow["month_out"]), (2, 2))   # 권현수(외진 나감) + 명부퇴원

        today = date.today()
        event = today - timedelta(days=2)
        in_week = event >= today - timedelta(days=today.weekday())        # 스트립의 창 = 이번 주 월요일부터
        in_month = event.month == today.month
        with main.app.test_request_context():
            strip = main._ward_status_strip()
        self.assertEqual(strip["week_out"], 2 if in_week else 0)
        self.assertEqual(strip["month_out"], 2 if in_month else 0)

    def test_flow_events_has_it_as_a_discharge_row(self):
        """입원·퇴원 이력·대시보드 입·퇴원 현황이 함께 쓰는 근거(admission_flow_events)에 '퇴원(외진)' 행이 선다."""
        out = {e["patient_name"]: e for e in models.admission_flow_events(d(-7), d(0))
               if e["kind"] == models.ADMISSION_EVENT_OUT}
        self.assertIn("권현수", out)
        row = out["권현수"]
        self.assertEqual((row["date"], row["sources"]), (d(-2), ["외진"]))
        self.assertEqual((row["discharge_destination"], row["discharge_reason"]), ("안동병원", "응급전원"))
        self.assertTrue(row.get("away_out"))
        self.assertEqual(row["room_number"], "309호")
        self.assertEqual(out["명부퇴원"]["sources"], ["명부"])      # 두 줄이 되면 안 된다
        self.assertNotIn("전원환자", out)

    def test_history_tab_lists_it_as_a_discharge(self):
        report = ward_moves.report({"from": d(-7), "to": d(0), "kind": "out"})
        row = next(r for r in report["rows"] if r["patient_name"] == "권현수")
        self.assertEqual((row["kind"], row["date"], row["destination"]), ("out", d(-2), "안동병원"))
        self.assertEqual(row["source"], "외진")


if __name__ == "__main__":
    unittest.main()
