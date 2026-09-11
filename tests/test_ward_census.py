"""재원 판정이 원무 명부(입원 회차)를 따르는지.

상담의 `admission_status='입원완료' AND discharge_date 비어있음`으로 세던 시절에는
수치가 실제와 달랐다. 퇴원일이 8천여 건 중 한 건도 채워지지 않아 2024년에 입원한
환자가 계속 재원으로 잡혔고, 반대로 올해 입원한 환자는 한 명도 안 잡혔다.
회차 테이블(admission_episodes)은 원무 명부를 그대로 받은 것이라 여기가 기준이다.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

import app as main
import models


class WardCensusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "census.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        models.ensure_admin_user("census-test", "test-password", display_name="점검")
        self.uid = models.get_user("census-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="census-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 2 for k in main.MENU_KEYS})
        with models.get_db() as conn:
            for pid, name in ((1, "재원환자"), (2, "이미퇴원한사람"), (3, "명부환자"), (4, "미확정환자")):
                conn.execute("INSERT INTO patients (id,name,gender) VALUES (?,?,'F')", (pid, name))
            # 상담은 넷 다 '입원완료 · 퇴원일 없음'이다 — 옛 기준이면 넷 다 재원이었다.
            for cid, pid, adm in ((1, 1, "2024-01-01"), (2, 2, "2024-02-01"),
                                  (4, 4, None)):
                conn.execute(
                    """INSERT INTO consultations
                       (id, patient_id, consult_date, admission_status, actual_admission_date,
                        patient_age, primary_diagnosis)
                       VALUES (?, ?, '2023-12-01', '입원완료', ?, 70, '테스트병명')""",
                    (cid, pid, adm))
            conn.execute("UPDATE consultations SET consult_date = DATE('now','-10 day') WHERE id = 4")
            # 회차 — 원무 명부가 말하는 사실
            for pid, admitted, discharged, room in (
                    (1, "2026-08-01", None, "301호"),      # 아직 재원
                    (2, "2024-02-01", "2024-05-01", "202호"),  # 이미 퇴원
                    (3, "2026-07-15", None, "405호")):     # 상담 없이 입원
                conn.execute(
                    """INSERT INTO admission_episodes
                       (patient_id, episode_no, status, admitted_at, discharged_at,
                        room_number, ward, diagnosis_name, roster_key)
                       VALUES (?, 1, ?, ?, ?, ?, '3병동', '상세불명의 뇌경색증', ?)""",
                    (pid, "discharged" if discharged else "admitted",
                     admitted, discharged, room, "chart%d|%s" % (pid, admitted)))

    def test_census_counts_episodes_not_consultations(self):
        census = models.current_admission_census()
        self.assertEqual(len(census["patients"]), 2)          # 퇴원환자는 빠진다
        self.assertEqual(list(census["by_consultation"]), [1])
        self.assertEqual([e["patient_id"] for e in census["orphans"]], [3])

    def test_one_consultation_is_not_reused_by_two_episodes(self):
        """같은 환자가 재입원하면 회차가 둘이다. 한 상담이 둘의 근거가 될 수는 없다."""
        with models.get_db() as conn:
            conn.execute(
                """INSERT INTO admission_episodes
                   (patient_id, episode_no, status, admitted_at, room_number, roster_key)
                   VALUES (1, 2, 'admitted', '2026-08-20', '302호', 'chart1|2026-08-20')""")
        census = models.current_admission_census()
        # 같은 사람이 동시에 두 번 재원일 수는 없다 — 늦게 들어간 회차가 현재 입원
        self.assertEqual(len(census["patients"]), 2)
        self.assertEqual(len(census["by_consultation"]) + len(census["orphans"]), 2)

    def test_ward_screen_counts_and_labels(self):
        response = self.client.get("/ward?view=list")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('<span class="wd-k-n">2</span>', html)  # 총 입원 환자 = 2
        # 재원 목록은 '최근 퇴원환자 관리' 앞까지다 — 퇴원자는 그 아래에만 나와야 한다
        roster = html.split('id="sec-discharged"')[0]
        self.assertIn("재원환자", roster)
        self.assertIn("명부환자", roster)
        self.assertNotIn("이미퇴원한사람", roster)   # 명부가 퇴원이라 재원에서 빠진다
        # 상담일지가 없는 환자는 상담 상세·외진·퇴원 버튼 대신 안내가 붙는다
        self.assertIn("상담기록 없음", html)
        self.assertIn("/consult/new?patient_id=3", html)

    def test_admission_date_comes_from_roster(self):
        """상담에 적힌 입원일은 상담 시점 예정값이라 실제와 어긋난다."""
        html = self.client.get("/ward?view=list").get_data(as_text=True)
        self.assertIn("2026-08-01", html)
        self.assertNotIn("2024-01-01", html)

    def test_pending_keeps_consultation_basis(self):
        """'입원일 미확정'은 데이터 점검 목록이라 상담 기준 그대로 둔다."""
        html = self.client.get("/ward?view=list").get_data(as_text=True)
        self.assertIn("미확정환자", html)
        self.assertIn('⚠ 입원일 미확정 <span class="wd-n">1</span>', html)

    def test_admitted_patient_drops_out_of_pending(self):
        """명부가 재원이라 말하면 상담에 입원일이 없어도 '미확정'이 아니다."""
        with models.get_db() as conn:
            conn.execute(
                """INSERT INTO admission_episodes
                   (patient_id, episode_no, status, admitted_at, room_number, roster_key)
                   VALUES (4, 1, 'admitted', '2026-09-01', '501호', 'chart4|2026-09-01')""")
        html = self.client.get("/ward?view=list").get_data(as_text=True)
        self.assertIn('⚠ 입원일 미확정 <span class="wd-n">0</span>', html)
        self.assertIn('<span class="wd-k-n">3</span>', html)  # 재원 3명으로 늘어난다

    def test_recent_discharges_come_from_roster(self):
        """상담의 '퇴원완료' 상태로는 한 건도 안 잡힌다 — 회차의 퇴원일이 기준이다."""
        rows = models.recent_discharges()
        self.assertEqual([(r["patient_name"], r["discharged_at"]) for r in rows],
                         [("이미퇴원한사람", "2024-05-01")])
        html = self.client.get("/ward?view=list").get_data(as_text=True)
        self.assertIn("최근 퇴원환자 관리 <span class=\"wd-n\">1</span>", html)

    def test_ratio_trend_denominator_is_the_roster_census(self):
        """추이도 회차로 복원한다. 상담 기준이면 퇴원이 안 빠져 인원이 불어나기만 했다."""
        response = self.client.get("/ward?tab=trend")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        import json, re
        series = json.loads(re.search(r"data-series='(\[.*?\])'", html).group(1))
        today = series[-1]
        self.assertEqual(today["total"], 2)      # 지금 재원 2명
        # 상담이 안 붙은 회차는 비율 분모에서 빠진다 — 회복기 판정을 못 하기 때문
        self.assertEqual(today["known"], 1)

    def test_falls_back_to_consultations_when_roster_is_empty(self):
        """명부를 아직 안 올린 설치에서는 옛 방식으로 돌아간다.

        회차가 통째로 비어 있는데 그대로 세면 재원 명단이 빈 화면이 된다.
        '적재 전'과 '지금 재원 0명'은 다르다.
        """
        with models.get_db() as conn:
            conn.execute("DELETE FROM admission_episodes")
        census = models.current_admission_census()
        self.assertFalse(census["has_roster"])
        html = self.client.get("/ward?view=list").get_data(as_text=True)
        # 상담 기준 = 입원일이 있는 입원완료 2건(재원환자·이미퇴원한사람)
        self.assertIn('<span class="wd-k-n">2</span>', html)
        self.assertIn("재원환자", html)
        self.assertIn("이미퇴원한사람", html)

    def test_app_created_episodes_are_not_counted(self):
        """앱이 외진·입원확정 때 만든 회차(roster_key 없음)는 재원에 세지 않는다.

        그 회차는 상담을 근거로 하는데 상담에 퇴원일이 없어 퇴원한 환자도 영원히
        열린 채로 남는다. 섞어 세면 재원이 두 배가 된다(260명이 558명으로 나왔다).
        """
        with models.get_db() as conn:
            conn.execute(
                """INSERT INTO admission_episodes
                   (patient_id, episode_no, status, admitted_at, consultation_id)
                   VALUES (2, 9, 'admitted', '2024-02-01', 2)""")   # 이미 퇴원한 사람
        census = models.current_admission_census()
        self.assertEqual(len(census["patients"]), 2)
        self.assertNotIn(2, census["patients"])
        html = self.client.get("/ward?view=list").get_data(as_text=True)
        self.assertIn('<span class="wd-k-n">2</span>', html)

    def test_roster_only_flag_ignores_app_created_episodes(self):
        """앱이 만든 회차만 있으면 명부는 아직 없는 것이다 — 옛 방식으로 돌아간다."""
        with models.get_db() as conn:
            conn.execute("UPDATE admission_episodes SET roster_key = NULL")
        self.assertFalse(models.current_admission_census()["has_roster"])


if __name__ == "__main__":
    unittest.main()
