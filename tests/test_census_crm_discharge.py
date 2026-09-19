# -*- coding: utf-8 -*-
"""퇴원은 어디에 적혀도 재원 수에서 빠진다 (2026-09-19 사용자 규칙).

원무 명부는 '그날 재원인 사람'만 담은 하루치 스냅샷으로도 올라온다. 그 파일에는 이미
퇴원한 사람의 행이 없어서 적재기가 회차를 닫을 근거가 없고, 행이 없다고 퇴원으로 칠
수도 없다(나머지 전원이 퇴원 처리된다). 그래서 2026-09-17~18에 퇴원한 6명이 명부
회차가 열린 채 재원에 남아 재원 수가 엑셀 273명 대비 279명으로 나왔다 — 상담실은
그때 이미 CRM에 퇴원완료를 입력한 뒤였다.

여기서 고정하는 것: 명부 회차가 열려 있어도 CRM이 이 입원의 퇴원을 알면 재원이 아니다.
그리고 그 판정을 재원 명단·병동 가동률·병실 만실·추이·퇴원 건수가 모두 같이 쓴다.
"""
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import dashboard_metrics
import models


def d(n):
    return (date.today() + timedelta(days=n)).isoformat()


class CensusCrmDischargeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "census.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        with models.get_db() as conn:
            for pid, name in ((1, "상담에만퇴원"), (2, "CRM회차퇴원"), (3, "재원그대로"),
                              (4, "재입원"), (5, "상담만퇴원완료")):
                conn.execute("INSERT INTO patients (id,name,gender) VALUES (?,?,'F')", (pid, name))
            # 명부: 다섯 명 모두 열린 회차 — 명부만 보면 5명 전원 재원이다
            for pid, adm in ((1, d(-10)), (2, d(-10)), (3, d(-10)), (4, d(-5)), (5, d(-10))):
                self._roster(conn, pid, adm)
            # 4번은 옛 입원이 따로 있다 — d(-30) 입원, d(-20) 퇴원(명부가 이미 닫음)
            self._roster(conn, 4, d(-30), dis=d(-20), no=2)
            for pid in (1, 2, 3, 4, 5):
                conn.execute("INSERT INTO consultations (id, patient_id, consult_date, patient_age, "
                             "admission_status, actual_admission_date) VALUES (?,?,?,70,'입원완료',?)",
                             (pid, pid, d(-40), d(-10) if pid != 4 else d(-5)))

    @staticmethod
    def _roster(conn, pid, adm, dis=None, no=1):
        conn.execute("""INSERT INTO admission_episodes
            (patient_id, episode_no, status, admitted_at, discharged_at, room_number, ward, roster_key)
            VALUES (?,?,?,?,?, '301호', '3병동', ?)""",
                     (pid, no, "discharged" if dis else "admitted", adm, dis, f"c{pid}|{adm}"))

    @staticmethod
    def _consult_discharged(cid, when):
        """상담 저장 폼으로 퇴원완료만 적은 상태 — 명부 회차는 열린 채 남는다."""
        with models.get_db() as conn:
            conn.execute("UPDATE consultations SET admission_status='퇴원완료', discharge_date=? WHERE id=?",
                         (when, cid))

    @staticmethod
    def _crm_episode_discharged(pid, adm, when):
        """앱이 만든 회차(roster_key 없음)에만 퇴원이 찍힌 상태."""
        with models.get_db() as conn:
            conn.execute("""INSERT INTO admission_episodes
                (patient_id, episode_no, status, admitted_at, discharged_at, consultation_id)
                VALUES (?, 9, 'discharged', ?, ?, ?)""", (pid, adm, when, pid))

    def test_roster_only_would_count_everyone(self):
        """전제 확인 — 명부 회차만 보면 5명이 재원이다(고쳐야 할 대상)."""
        with models.get_db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM admission_episodes "
                             "WHERE roster_key IS NOT NULL AND discharged_at IS NULL").fetchone()[0]
        self.assertEqual(n, 5)

    def test_consultation_discharge_removes_from_census(self):
        self._consult_discharged(1, d(-1))
        self.assertEqual(models.current_admission_census()["patients"], {2, 3, 4, 5})

    def test_crm_episode_discharge_removes_from_census(self):
        self._crm_episode_discharged(2, d(-10), d(-2))
        self.assertEqual(models.current_admission_census()["patients"], {1, 3, 4, 5})

    def test_old_discharge_does_not_close_the_readmission(self):
        """재입원 가드 — 옛 퇴원(d-20)이 지금 입원(d-5)을 닫으면 안 된다."""
        self._consult_discharged(4, d(-20))
        self.assertIn(4, models.current_admission_census()["patients"])

    def test_discharge_on_admission_day_counts(self):
        """입원 당일 퇴원도 퇴원이다."""
        self._consult_discharged(5, d(-10))
        self.assertNotIn(5, models.current_admission_census()["patients"])

    def test_every_screen_uses_the_same_rule(self):
        self._consult_discharged(1, d(-1))
        self._crm_episode_discharged(2, d(-10), d(-2))
        census = models.current_admission_census()
        self.assertEqual(census["patients"], {3, 4, 5})
        # 병동 가동률 — 퇴원자가 침대를 차지한 채 남으면 안 된다
        wards = {w["ward"]: w for w in dashboard_metrics.ward_occupancy()}
        self.assertEqual(wards["3병동"]["count"], 3)
        # 병실 만실 판정도 같은 인원
        self.assertEqual(dashboard_metrics.room_status("301호")["used"], 3)
        # 과거 재원 복원(추이) — 퇴원일이 CRM 값으로 실린다
        spans = {s["episode_id"]: s for s in models.admission_spans()}
        by_patient = {}
        with models.get_db() as conn:
            for r in conn.execute("SELECT id, patient_id FROM admission_episodes WHERE roster_key IS NOT NULL"):
                by_patient.setdefault(r["patient_id"], []).append(r["id"])
        self.assertEqual(spans[by_patient[1][0]]["discharged_at"], d(-1))
        self.assertEqual(spans[by_patient[2][0]]["discharged_at"], d(-2))
        self.assertIsNone(spans[by_patient[3][0]]["discharged_at"])
        # 이번주 퇴원 건수에도 잡힌다 — 명부가 아직 모르는 퇴원이라도 나간 건 나간 것이다
        flow = models.admission_flow_counts(d(-7), d(0), d(-30), d(0))
        self.assertEqual(flow["week_out"], 2)

    def test_unfilled_discharge_date_is_not_a_discharge(self):
        """퇴원완료인데 퇴원일이 비면(옛 데이터) 재원에서 함부로 빼지 않는다."""
        with models.get_db() as conn:
            conn.execute("UPDATE consultations SET admission_status='퇴원완료', discharge_date='' WHERE id=1")
        self.assertIn(1, models.current_admission_census()["patients"])


if __name__ == "__main__":
    unittest.main()
