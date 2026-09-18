"""퇴원 예정일 초과 — '오늘 처리 필요'엔 1주일만, 그 뒤는 연장 1회/2회로 본다 (2026-09-18 사용자 정의).

퇴원예정일 = 입원 1년 만료일이라, 지나고도 재원이면 연장해서 머무는 것이다. 매일 '퇴원 예정일이 지남'으로
큐에 남으면 오늘 처리할 일이 아닌 것이 쌓인다. 앞으로 올 D-30은 오른쪽 '기한 임박' 카드가 맡는다.
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


class DischargeOverdueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "dis.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("dis-test", "test-password", display_name="점검")
        self.uid = models.get_user("dis-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="dis-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        # (환자, 퇴원예정일, 입원일) — 연장 단계는 입원 경과일 기준(1년 초과 1회 / 1년 6개월 초과 2회)
        rows = [(1, "곧퇴원", d(5), d(-370)), (2, "사흘지남", d(-3), d(-370)),
                (3, "스무날지남", d(-20), d(-385)), (4, "연장둘", d(-200), d(-600))]
        with models.get_db() as conn:
            for pid, name, due, adm in rows:
                conn.execute("INSERT INTO patients (id,name,gender) VALUES (?,?,'F')", (pid, name))
                conn.execute("""INSERT INTO consultations (id, patient_id, consult_date, admission_status,
                                actual_admission_date, discharge_due_date, attending_doctor, room_number, patient_age)
                                VALUES (?,?,?,'입원완료',?,?,'RM1 이성범 부장','301호',70)""",
                             (pid, pid, d(-700), adm, due))
                conn.execute("""INSERT INTO admission_episodes (patient_id, consultation_id, episode_no, status,
                                admitted_at, room_number, ward, roster_key)
                                VALUES (?,?,1,'admitted',?,'301호','3병동',?)""", (pid, pid, adm, f"c{pid}|x"))

    def _watch(self, cid):
        return main._discharge_watch(models.get_consultation(cid))

    def test_watch_state_by_days_left(self):
        self.assertEqual(self._watch(1)["state"], "퇴원예정")     # 아직 안 지남
        self.assertEqual(self._watch(2)["state"], "퇴원지연")     # 3일 지남 — 1주일 안
        self.assertEqual(self._watch(3)["state"], "연장1회")      # 20일 지남 — 1년 초과
        self.assertEqual(self._watch(3)["ext_tier"], 1)
        self.assertEqual(self._watch(4)["state"], "연장2회")      # 1년 6개월도 지남
        self.assertEqual(self._watch(4)["ext_tier"], 2)

    def test_queue_holds_only_the_first_week(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("퇴원지연", html)
        self.assertIn("퇴원 예정일 3일 지남", html)
        self.assertIn("사흘지남", html)
        for name in ("스무날지남", "연장둘"):
            self.assertNotIn(name, html, f"{name}은 연장 상태 — 오늘 처리 필요에 남으면 안 된다")
        self.assertNotIn("퇴원 예정일이 지남", html)   # 옛 문구

    def test_due_card_still_takes_upcoming_only(self):
        data = self.client.get("/").get_data(as_text=True)
        self.assertIn("곧퇴원", data)                  # 기한 임박(D-30) 쪽
        # 큐 항목의 묶음 이름도 '퇴원지연'
        self.assertEqual(main._dashboard_action_group("퇴원지연"), "퇴원지연")
        self.assertIn("퇴원지연", main._ACTION_GROUP_ORDER)


if __name__ == "__main__":
    unittest.main()
