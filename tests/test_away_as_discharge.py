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
        with main.app.test_request_context():
            strip = main._ward_status_strip()
        self.assertEqual(strip["week_out"], 2)     # 권현수(외진 나감) + 명부퇴원
        self.assertEqual(strip["month_out"], 2)


if __name__ == "__main__":
    unittest.main()
