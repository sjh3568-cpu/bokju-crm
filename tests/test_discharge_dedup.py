"""퇴원 중복·재원일수·데이터 점검 (2026-09-19 권현수 님 사례).

원무 명부는 9/15 퇴원, 외진 기록은 9/16 응급전원 — 하루가 어긋나 대시보드에 퇴원이 두 줄로 잡혔다.
같은 사건이므로 한 줄로 합치되, 사유·행선지까지 있는 외진 기록을 남긴다(사용자: 실제 퇴원은 9/16).
어긋난 값 자체는 데이터 점검에서 보이게 해 사람이 고치게 한다.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

import models

ADM, ROSTER_OUT, AWAY_OUT = "2026-07-21", "2026-09-15", "2026-09-16"
LO, HI = "2026-09-14", "2026-09-18"


class DischargeDedupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "dedup.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        with models.get_db() as conn:
            conn.execute("INSERT INTO patients (id,name,gender) VALUES (1,'권현수','F')")
            conn.execute("""INSERT INTO consultations (id,patient_id,consult_date,admission_status,
                            actual_admission_date,discharge_date,room_number)
                            VALUES (1,1,'2026-07-10','퇴원완료',?,?,'309호')""", (ADM, ROSTER_OUT))
            conn.execute("""INSERT INTO admission_episodes (patient_id,episode_no,status,admitted_at,
                            discharged_at,room_number,ward,roster_key)
                            VALUES (1,2,'discharged',?,?,'309호','3병동','0000000134|2026-07-21')""", (ADM, ROSTER_OUT))
            conn.execute("""INSERT INTO admission_events (consultation_id,event_type,event_date,hospital)
                            VALUES (1,'응급전원',?,'안동병원')""", (AWAY_OUT,))

    def _out_events(self):
        return [e for e in models.admission_flow_events(LO, HI) if e["kind"] == models.ADMISSION_EVENT_OUT]

    def test_one_discharge_row_dated_by_the_away_record(self):
        out = self._out_events()
        self.assertEqual(len(out), 1, "명부 퇴원과 외진 나감은 한 사건 — 두 줄이면 안 된다")
        self.assertEqual((out[0]["date"], out[0]["sources"]), (AWAY_OUT, ["외진"]))
        self.assertEqual(out[0]["discharge_destination"], "안동병원")

    def test_dashboard_shows_it_once(self):
        rows = [r for r in models.dashboard_summary(LO, HI)["admission_selected"]
                if r.get("patient_name") == "권현수"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["admission_display_date"], AWAY_OUT)

    def test_week_out_counts_it_once(self):
        flow = models.admission_flow_counts(LO, "2026-09-20", "2026-09-01", "2026-09-30")
        self.assertEqual(flow["week_out"], 1)

    def test_stay_days_match_the_roster_total(self):
        """원무 명부 '총일수'는 입원일·퇴원일을 모두 센다 — 7/21~9/15는 57일."""
        rows = [r for r in models.dashboard_summary(LO, HI)["admission_selected"]
                if r.get("patient_name") == "권현수"]
        self.assertEqual(rows[0]["discharge_stay_days"], 58)      # 7/21~9/16
        with models.get_db() as conn:
            conn.execute("UPDATE admission_events SET event_date = ? WHERE consultation_id = 1", (ROSTER_OUT,))
        rows = [r for r in models.dashboard_summary(LO, HI)["admission_selected"]
                if r.get("patient_name") == "권현수"]
        self.assertEqual(rows[0]["discharge_stay_days"], 57)      # 명부와 같은 날이면 명부 총일수와 일치

    def test_same_day_admit_and_discharge_is_one_day_not_zero(self):
        with models.get_db() as conn:
            conn.execute("INSERT INTO patients (id,name,gender) VALUES (2,'당일퇴원','M')")
            conn.execute("""INSERT INTO consultations (id,patient_id,consult_date,admission_status,
                            actual_admission_date,discharge_date) VALUES (2,2,'2026-09-16','퇴원완료',?,?)""",
                         (AWAY_OUT, AWAY_OUT))
            conn.execute("""INSERT INTO admission_episodes (patient_id,episode_no,status,admitted_at,
                            discharged_at,roster_key) VALUES (2,1,'discharged',?,?, 'c2|x')""", (AWAY_OUT, AWAY_OUT))
        row = [r for r in models.dashboard_summary(LO, HI)["admission_selected"]
               if r.get("patient_name") == "당일퇴원" and r.get("admission_kind") == "discharge"][0]
        self.assertEqual(row["discharge_stay_days"], 1)           # 전에는 0일로 보였다

    def test_quality_report_flags_the_mismatch(self):
        checks = {c["title"]: c for c in models.data_quality_report()["checks"]}
        self.assertIn("퇴원일 불일치", checks)
        self.assertEqual(checks["퇴원일 불일치"]["count"], 1)
        row = checks["퇴원일 불일치"]["rows"][0]
        self.assertEqual((row["patient_name"], row["roster_discharged_at"], row["away_date"]),
                         ("권현수", ROSTER_OUT, AWAY_OUT))
        self.assertIn("장기 미복귀 외진", checks)


if __name__ == "__main__":
    unittest.main()
