"""명부 퇴원자 → 상담 '퇴원완료' 전환 (2026-09-19 요청).

사용자 규칙: 현재 재원환자 현황은 절대 변경하지 않는다. 그 외 입원 환자는 퇴원완료로 본다.
단 퇴원일을 아는 건(명부 회차에 퇴원일)만 바꾼다 — 모르는 건 그대로 둔다.
"""
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import models
from tools.backfill_from_roster import close_discharged


def d(n):
    return (date.today() + timedelta(days=n)).isoformat()


class CloseDischargedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "close.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        # (환자, 상담 입원일, 명부 회차들[(입원,퇴원)], 기대)
        self.people = [
            (1, "퇴원한사람", d(-300), [(d(-300), d(-250))]),                 # 전환 대상
            (2, "지금재원", d(-40), [(d(-400), d(-350)), (d(-40), None)]),     # 재원 — 옛 퇴원 있어도 제외
            (3, "명부에없음", d(-200), []),                                    # 퇴원일 모름 — 그대로
            (4, "재입원했다퇴원", d(-100), [(d(-300), d(-250)), (d(-100), d(-60))]),  # 입원일 맞는 회차로
        ]
        with models.get_db() as conn:
            for pid, name, adm, eps in self.people:
                conn.execute("INSERT INTO patients (id,name,gender) VALUES (?,?,'F')", (pid, name))
                conn.execute("""INSERT INTO consultations (id, patient_id, consult_date, admission_status,
                                actual_admission_date, room_number, patient_age)
                                VALUES (?,?,?,'입원완료',?, '301호', 70)""", (pid, pid, adm, adm))
                for i, (a, dis) in enumerate(eps, 1):
                    conn.execute("""INSERT INTO admission_episodes (patient_id, episode_no, status, admitted_at,
                                    discharged_at, room_number, roster_key)
                                    VALUES (?,?,?,?,?, '301호', ?)""",
                                 (pid, i, "discharged" if dis else "admitted", a, dis, f"c{pid}|{a}"))
            # 이미 퇴원일이 적힌 상담 — 손대지 않는다
            conn.execute("INSERT INTO patients (id,name,gender) VALUES (5,'이미퇴원','F')")
            conn.execute("""INSERT INTO consultations (id, patient_id, consult_date, admission_status,
                            actual_admission_date, discharge_date) VALUES (5,5,?, '입원완료', ?, ?)""",
                         (d(-300), d(-300), d(-280)))
            conn.execute("""INSERT INTO admission_episodes (patient_id, episode_no, status, admitted_at,
                            discharged_at, roster_key) VALUES (5,1,'discharged',?,?, 'c5|x')""", (d(-300), d(-250)))

    def _status(self, cid):
        c = models.get_consultation(cid)
        return c["admission_status"], c["discharge_date"]

    def test_dry_run_changes_nothing(self):
        with models.get_db() as conn:
            rep = close_discharged(conn, apply=False)
        self.assertEqual(rep["stats"]["퇴원완료로 전환"], 2)      # 1번·4번
        self.assertEqual(self._status(1), ("입원완료", None))

    def test_apply_closes_only_the_right_ones(self):
        with models.get_db() as conn:
            rep = close_discharged(conn, apply=True); conn.commit()
        self.assertEqual(self._status(1), ("퇴원완료", d(-250)))
        self.assertEqual(self._status(4), ("퇴원완료", d(-60)))   # 입원일이 맞는 회차의 퇴원일
        self.assertEqual(self._status(2), ("입원완료", None), "현재 재원은 절대 안 바뀐다")
        self.assertEqual(self._status(3), ("입원완료", None), "퇴원일을 모르면 그대로")
        self.assertEqual(self._status(5), ("입원완료", d(-280)), "이미 적힌 퇴원일은 보호")
        self.assertEqual(rep["stats"]["현재 재원 — 건드리지 않음"], 1)
        self.assertEqual(rep["stats"]["명부에 퇴원 기록 없음 — 그대로 둠"], 1)
        self.assertEqual(rep["residents"], 1)

    def test_idempotent(self):
        with models.get_db() as conn:
            close_discharged(conn, apply=True); conn.commit()
        with models.get_db() as conn:
            again = close_discharged(conn, apply=True); conn.commit()
        self.assertEqual(again["stats"].get("퇴원완료로 전환", 0), 0)
        self.assertEqual(self._status(1), ("퇴원완료", d(-250)))

    def test_current_census_is_unchanged(self):
        before = models.current_admission_census()["patients"]
        with models.get_db() as conn:
            close_discharged(conn, apply=True); conn.commit()
        self.assertEqual(models.current_admission_census()["patients"], before)
        self.assertEqual(before, {2})


if __name__ == "__main__":
    unittest.main()
