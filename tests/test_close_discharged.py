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


class SnapshotCloseTests(unittest.TestCase):
    """"최근 명부(하루치 완전 스냅샷)에 없는 사람은 퇴원자" (2026-09-25 사용자 결정).

    9/19 규칙은 퇴원일을 아는 건만 바꿨다. 이제 스냅샷 날짜를 주면 그 이전에 입원했는데
    명부에 없는 상담도 퇴원완료로 본다 — 단 날짜를 모르면 퇴원일은 비워 둔다(엉뚱한 날짜를
    넣으면 입·퇴원 이력에 가짜 퇴원이 생긴다). 스냅샷 이후 입원(명부가 아직 모르는 CRM 입원)은
    재원이므로 건드리지 않는다.
    """
    SNAP = d(-3)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "snap.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        # (환자, 상담 입원일, 명부 회차들[(입원,퇴원)])
        people = [
            (6, "예정일로적힘", d(-100), [(d(-95), d(-60))]),       # 명부 입원이 5일 뒤 — 그 회차 퇴원일로
            (7, "옛회차만멀리", d(-100), [(d(-400), d(-350))]),     # 명부 퇴원(d-350)이 이 입원(d-100)보다 앞 — 짝 아님 → 미상
            (3, "명부에없음", d(-200), []),                          # 스냅샷 이전 입원, 명부 없음 → 퇴원완료(미상)
            (8, "스냅샷이후CRM입원", d(-1), []),                     # 명부가 아직 모르는 재원 — 그대로
            (2, "지금재원", d(-40), [(d(-40), None)]),               # 열린 명부 회차 — 절대 안 건드림
        ]
        with models.get_db() as conn:
            for pid, name, adm, eps in people:
                conn.execute("INSERT INTO patients (id,name,gender) VALUES (?,?,'F')", (pid, name))
                conn.execute("""INSERT INTO consultations (id, patient_id, consult_date, admission_status,
                                actual_admission_date, room_number, patient_age)
                                VALUES (?,?,?,'입원완료',?, '301호', 70)""", (pid, pid, adm, adm))
                for i, (a, dis) in enumerate(eps, 1):
                    conn.execute("""INSERT INTO admission_episodes (patient_id, episode_no, status, admitted_at,
                                    discharged_at, room_number, roster_key)
                                    VALUES (?,?,?,?,?, '301호', ?)""",
                                 (pid, i, "discharged" if dis else "admitted", a, dis, f"c{pid}|{a}"))
            # 9: 상담은 이미 퇴원완료(d-2)인데 명부 회차가 열린 채 — 9/19 명부에 없던 6명이 이 모양
            conn.execute("INSERT INTO patients (id,name,gender) VALUES (9,'상담퇴원명부열림','F')")
            conn.execute("""INSERT INTO consultations (id, patient_id, consult_date, admission_status,
                            actual_admission_date, discharge_date) VALUES (9,9,?, '퇴원완료', ?, ?)""",
                         (d(-50), d(-50), d(-2)))
            conn.execute("""INSERT INTO admission_episodes (patient_id, episode_no, status, admitted_at,
                            room_number, roster_key) VALUES (9,1,'admitted',?, '305호', 'c9|x')""", (d(-50),))

    def _status(self, cid):
        c = models.get_consultation(cid)
        return c["admission_status"], c["discharge_date"]

    def test_without_snapshot_behaves_as_before(self):
        """스냅샷을 안 주면 9/19 동작 그대로 — 날짜 모르는 건 안 건드린다."""
        with models.get_db() as conn:
            rep = close_discharged(conn, apply=True); conn.commit()
        self.assertEqual(self._status(6), ("퇴원완료", d(-60)), "5일 뒤 명부 입원도 이 입원의 회차다")
        self.assertEqual(self._status(7), ("입원완료", None), "입원보다 앞선 퇴원일은 짝이 아니다")
        self.assertEqual(self._status(3), ("입원완료", None))
        self.assertEqual(rep["stats"].get("퇴원완료로 전환"), 1)

    def test_snapshot_closes_absent_patients_without_inventing_dates(self):
        with models.get_db() as conn:
            rep = close_discharged(conn, apply=True, snapshot=self.SNAP); conn.commit()
        self.assertEqual(self._status(6), ("퇴원완료", d(-60)))
        self.assertEqual(self._status(7), ("퇴원완료", None), "명부에 없으니 퇴원자, 날짜는 미상")
        self.assertEqual(self._status(3), ("퇴원완료", None))
        self.assertEqual(self._status(8), ("입원완료", None), "스냅샷 이후 입원은 명부가 아직 모를 뿐 재원")
        self.assertEqual(self._status(2), ("입원완료", None), "현재 재원은 절대 안 바뀐다")
        self.assertEqual(rep["stats"]["명부 미등재 — 퇴원완료(퇴원일 미상)로 전환"], 2)
        self.assertEqual(rep["stats"]["스냅샷 이후 입원 — 그대로 둠"], 1)
        reason = models.get_consultation(3)["discharge_reason"] or ""
        self.assertIn("명부 미등재", reason)
        self.assertIn(self.SNAP, reason)
        # 가짜 퇴원 이벤트가 생기지 않는다 — 날짜가 없으니 어느 기간에도 퇴원 행이 없다.
        # (7의 d-350 퇴원은 옛 명부 회차의 진짜 퇴원이라 그 한 줄만 있어야 한다)
        out = [(e["patient_id"], e["date"]) for e in models.admission_flow_events(d(-400), d(0)) if e["kind"] == "out"]
        self.assertEqual([o for o in out if o[0] == 3], [])
        self.assertEqual([o for o in out if o[0] == 7], [(7, d(-350))])

    def test_open_roster_episode_closes_from_discharged_consultation(self):
        """상담에 퇴원완료+퇴원일이 있는데 명부 회차가 열려 있으면 그 날짜로 닫는다(멱등)."""
        from tools.backfill_from_roster import close_roster_by_consultation
        with models.get_db() as conn:
            rep = close_roster_by_consultation(conn, apply=True); conn.commit()
            row = conn.execute("SELECT discharged_at, status FROM admission_episodes WHERE patient_id=9").fetchone()
        self.assertEqual((row["discharged_at"], row["status"]), (d(-2), "discharged"))
        self.assertEqual(rep["closed"], 1)
        with models.get_db() as conn:
            again = close_roster_by_consultation(conn, apply=True); conn.commit()
        self.assertEqual(again["closed"], 0)
        # 열린 회차 2번은 상담이 입원완료라 그대로
        with models.get_db() as conn:
            self.assertIsNone(conn.execute("SELECT discharged_at FROM admission_episodes WHERE patient_id=2").fetchone()[0])

    def test_snapshot_is_idempotent(self):
        with models.get_db() as conn:
            close_discharged(conn, apply=True, snapshot=self.SNAP); conn.commit()
        with models.get_db() as conn:
            again = close_discharged(conn, apply=True, snapshot=self.SNAP); conn.commit()
        self.assertEqual(again["stats"].get("명부 미등재 — 퇴원완료(퇴원일 미상)로 전환", 0), 0)
        self.assertEqual(again["stats"].get("퇴원완료로 전환", 0), 0)


if __name__ == "__main__":
    unittest.main()
