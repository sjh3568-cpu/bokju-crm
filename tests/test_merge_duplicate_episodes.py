"""같은 입원이 두 줄로 남은 것을 명부 회차 하나로 합친다 (2026-09-26 사용자 결정).

한 번의 입원이 ① 원무 명부 적재로 만든 회차(roster_key)와 ② 상담사가 [입원완료]를 눌러 만든
회차(consultation_id) 두 줄로 남아 있었다 — 재원 환자 190명, 194쌍. 194쌍 중 188쌍이 입원일까지
같다. 두 줄이면 "CRM 회차는 퇴원일이 안 채워진다"는 문제가 계속 생기고, 그걸 피하려고 조건
①③ 같은 우회 장치를 계속 붙여야 한다. 합치면 그 문제 자체가 사라진다.

방향: **명부 회차를 남기고 CRM 회차를 흡수한다.**
  · admission_episodes.consultation_id 는 UNIQUE — 상담 연결을 명부 회차로 옮긴 뒤 CRM 회차를 지운다.
    그러면 다음부터 sync_admission_episode 의 ON CONFLICT(consultation_id) 가 명부 회차를 UPDATE 하고,
    roster_key 가드가 입·퇴원일을 지켜 준다(새 중복이 안 생긴다).
  · 외진 기록(admission_events.episode_id)은 명부 회차로 다시 붙인다 — 안 그러면 삭제로 끊긴다.
  · 명부에 없는 CRM 전용 값(퇴원예정일 등)은 명부 회차로 옮긴다.
  · 날짜 창: 명부가 CRM보다 늦은 건 30일까지 같은 입원으로 본다(상담에 적힌 날은 예정일인 경우).
    반대로 CRM이 더 늦으면 명부가 아직 모르는 **새 입원**일 수 있어 3일까지만.
"""
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import models
from tools.backfill_from_roster import merge_duplicate_episodes


def d(n):
    return (date.today() + timedelta(days=n)).isoformat()


class MergeDuplicateEpisodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "merge.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        with models.get_db() as conn:
            for pid, name in ((1, "같은날"), (2, "명부가늦음"), (3, "CRM이늦음"), (4, "명부만"),
                              (5, "이미퇴원한명부"), (6, "CRM둘")):
                conn.execute("INSERT INTO patients (id,name,gender) VALUES (?,?,'F')", (pid, name))
                conn.execute("""INSERT INTO consultations (id,patient_id,consult_date,admission_status,
                                actual_admission_date) VALUES (?,?,?,'입원완료',?)""", (pid, pid, d(-60), d(-30)))
            # 1: 입원일 같음 — 합침. CRM 회차가 퇴원예정일과 외진 기록을 들고 있다.
            self._roster(conn, 1, d(-30), room="301호")
            self._crm(conn, 1, d(-30), cid=1, due=d(+5))
            conn.execute("""INSERT INTO admission_events (consultation_id, event_type, event_date, episode_id)
                            VALUES (1, '응급전원', ?, (SELECT id FROM admission_episodes WHERE patient_id=1 AND roster_key IS NULL))""", (d(-2),))
            # 2: 명부가 9일 늦음(상담에 적힌 건 예정일) — 합침
            self._roster(conn, 2, d(-21)); self._crm(conn, 2, d(-30), cid=2)
            # 3: CRM이 10일 늦음 — 명부가 모르는 새 입원일 수 있다 → 합치지 않는다
            self._roster(conn, 3, d(-40)); self._crm(conn, 3, d(-30), cid=3)
            # 4: 명부 회차만 — 대상 아님
            self._roster(conn, 4, d(-30))
            # 5: 명부 회차가 이미 닫힘 → 열린 CRM 회차가 있어도 합치지 않는다(다른 입원)
            self._roster(conn, 5, d(-90), dis=d(-70)); self._crm(conn, 5, d(-30), cid=5)
            # 6: 한 명부 회차에 CRM 회차 둘 — 가까운 쪽만 합치고 나머지는 남긴다
            self._roster(conn, 6, d(-30))
            self._crm(conn, 6, d(-30), cid=6)
            self._crm(conn, 6, d(-25), cid=None, no=9)
            # 7: 한 명부 회차에 CRM 회차 둘인데 **각각 다른 상담**을 들고 있다. 두 번째까지 흡수하면
            #    그 상담의 회차 연결이 끊긴다 — 가까운 하나만 흡수하고 두 번째는 영원히 남긴다.
            conn.execute("INSERT INTO patients (id,name,gender) VALUES (7,'상담둘','F')")
            for cid in (71, 72):
                conn.execute("""INSERT INTO consultations (id,patient_id,consult_date,admission_status,
                                actual_admission_date) VALUES (?,7,?,'입원완료',?)""", (cid, d(-60), d(-30)))
            self._roster(conn, 7, d(-30))
            self._crm(conn, 7, d(-30), cid=71, no=5)
            self._crm(conn, 7, d(-28), cid=72, no=9)

    @staticmethod
    def _roster(conn, pid, adm, dis=None, room="301호", no=1):
        conn.execute("""INSERT INTO admission_episodes (patient_id, episode_no, status, admitted_at,
                        discharged_at, room_number, ward, roster_key)
                        VALUES (?,?,?,?,?,?, '3병동', ?)""",
                     (pid, no, "discharged" if dis else "admitted", adm, dis, room, f"c{pid}|{adm}"))

    @staticmethod
    def _crm(conn, pid, adm, cid=None, due=None, no=5):
        conn.execute("""INSERT INTO admission_episodes (patient_id, episode_no, status, admitted_at,
                        room_number, consultation_id, discharge_due_date)
                        VALUES (?,?, 'admitted', ?, '999호', ?, ?)""", (pid, no, adm, cid, due))

    def _eps(self, pid):
        with models.get_db() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, roster_key IS NOT NULL AS roster, date(admitted_at) adm, consultation_id, "
                "discharge_due_date, room_number FROM admission_episodes WHERE patient_id=? ORDER BY id", (pid,))]

    def test_dry_run_changes_nothing(self):
        with models.get_db() as conn:
            rep = merge_duplicate_episodes(conn, apply=False)
        self.assertEqual(rep["merged"], 4)          # 1·2·6·7
        self.assertEqual(len(self._eps(1)), 2, "미리보기는 지우지 않는다")

    def test_merges_into_the_roster_episode(self):
        with models.get_db() as conn:
            rep = merge_duplicate_episodes(conn, apply=True); conn.commit()
        self.assertEqual(rep["merged"], 4)
        eps = self._eps(1)
        self.assertEqual(len(eps), 1, "CRM 회차가 흡수돼 한 줄만 남는다")
        r = eps[0]
        self.assertTrue(r["roster"])
        self.assertEqual(r["consultation_id"], 1, "상담 연결이 명부 회차로 옮겨졌다")
        self.assertEqual(r["discharge_due_date"], d(+5), "명부에 없는 CRM 값은 보존")
        self.assertEqual(r["room_number"], "301호", "병실은 명부가 사실")
        # 외진 기록이 명부 회차를 가리킨다
        with models.get_db() as conn:
            self.assertEqual(conn.execute("SELECT episode_id FROM admission_events WHERE consultation_id=1").fetchone()[0], r["id"])

    def test_roster_later_than_crm_merges_but_crm_later_does_not(self):
        with models.get_db() as conn:
            merge_duplicate_episodes(conn, apply=True); conn.commit()
        self.assertEqual(len(self._eps(2)), 1, "명부가 9일 늦은 건 같은 입원")
        self.assertEqual(len(self._eps(3)), 2, "CRM이 10일 늦으면 새 입원일 수 있다 — 안 합침")

    def test_closed_roster_episode_is_not_a_partner(self):
        with models.get_db() as conn:
            merge_duplicate_episodes(conn, apply=True); conn.commit()
        self.assertEqual(len(self._eps(5)), 2)

    def test_only_the_closest_crm_episode_is_absorbed(self):
        with models.get_db() as conn:
            merge_duplicate_episodes(conn, apply=True); conn.commit()
        eps = self._eps(6)
        self.assertEqual(len(eps), 2, "가까운 하나만 흡수하고 나머지는 남긴다")
        self.assertEqual([e["adm"] for e in eps if not e["roster"]], [d(-25)])
        self.assertEqual([e["consultation_id"] for e in eps if e["roster"]], [6])

    def test_second_consultation_keeps_its_episode(self):
        """명부 회차가 이미 다른 상담과 연결돼 있으면 두 번째 CRM 회차는 건드리지 않는다.

        건드리면 그 상담의 회차 연결이 소리 없이 끊긴다 — 실제로 4건 끊어 본 뒤에 넣은 가드다.
        """
        with models.get_db() as conn:
            merge_duplicate_episodes(conn, apply=True); conn.commit()
        eps = self._eps(7)
        self.assertEqual(len(eps), 2)
        self.assertEqual([e["consultation_id"] for e in eps if e["roster"]], [71])
        self.assertEqual([e["consultation_id"] for e in eps if not e["roster"]], [72],
                         "두 번째 상담은 자기 회차를 그대로 갖고 있어야 한다")
        with models.get_db() as conn:
            linked = {r[0] for r in conn.execute(
                "SELECT consultation_id FROM admission_episodes WHERE consultation_id IN (71, 72)")}
        self.assertEqual(linked, {71, 72}, "두 상담 모두 회차 연결이 남아 있어야 한다")

    def test_idempotent(self):
        with models.get_db() as conn:
            merge_duplicate_episodes(conn, apply=True); conn.commit()
        with models.get_db() as conn:
            again = merge_duplicate_episodes(conn, apply=True); conn.commit()
        self.assertEqual(again["merged"], 0)
        self.assertEqual(len(self._eps(7)), 2, "재실행해도 두 번째 CRM 회차는 그대로")

    def test_census_is_unchanged(self):
        """합치기는 재원 인원을 바꾸지 않는다 — 환자 단위로 세므로 두 줄이든 한 줄이든 같다."""
        before = models.current_admission_census()["patients"]
        with models.get_db() as conn:
            merge_duplicate_episodes(conn, apply=True); conn.commit()
        self.assertEqual(models.current_admission_census()["patients"], before)

    def test_future_sync_updates_the_roster_episode_instead_of_duplicating(self):
        """합친 뒤 상담을 저장해도 새 회차가 생기지 않고, 명부 입원일이 지켜진다."""
        with models.get_db() as conn:
            merge_duplicate_episodes(conn, apply=True); conn.commit()
        models.sync_admission_episode(2)
        eps = self._eps(2)
        self.assertEqual(len(eps), 1, "중복이 다시 생기면 안 된다")
        self.assertEqual(eps[0]["adm"], d(-21), "명부 입원일이 상담값으로 덮이지 않는다")


if __name__ == "__main__":
    unittest.main()
