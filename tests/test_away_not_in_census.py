"""외진 중(미복귀)은 재원이 아니다 (2026-09-25 사용자 결정 — 황재명 님 9/21 응급전원).

9/18 규칙은 외진 나간 날을 '퇴원'으로 집계하게 했는데, 재원 명부는 그 환자를 계속 재원으로
들고 있었다. 같은 사람이 한 화면에선 퇴원 1건, 다른 화면에선 재원 1명 — 숫자가 안 맞았다.

여기서 고정하는 것:
① 미복귀 외진이 있는 환자는 재원이 아니다 — 판정은 crm_discharge_sql 한 곳(외진 출발일 = CRM이 아는 퇴원일).
② 그 판정을 재원 명단·병동 가동률·병실 만실·재원 추이·30일 census가 모두 같이 쓴다.
③ 복귀한 외진(returned_at 있음)은 재원을 건드리지 않는다. 옛 입원에 붙은 미복귀 외진도
   지금 입원(그 뒤에 새로 열린 회차)을 숨기지 않는다.
④ 재원에서 빠져도 상담에 입원일이 없다는 이유로 '입원일 미확정' 큐에 떨어지면 안 된다 —
   그 환자는 '외진 중'에만 있어야 한다.
⑤ 원무 명부는 그날의 참고 자료일 뿐이다 — 명부 회차가 열려 있어도 앱이 외진을 알면 재원이 아니다.
"""
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import app as main
import dashboard_metrics
import models
import partnerships
import support_requests


def d(n):
    return (date.today() + timedelta(days=n)).isoformat()


class AwayNotInCensusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "away.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("away-census", "test-password", display_name="점검")
        self.uid = models.get_user("away-census")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="away-census", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        with models.get_db() as conn:
            for pid, name in ((1, "외진중"), (2, "재원그대로"), (3, "복귀함"), (4, "옛외진새입원"),
                              (5, "CRM입원외진중"), (6, "CRM입원재원"), (7, "옛CRM회차퇴원안누름")):
                conn.execute("INSERT INTO patients (id,name,gender) VALUES (?,?,'M')", (pid, name))
            # 상담: 전부 입원완료. 1번은 황재명 님처럼 상담에 실제 입원일이 없다(명부가 준 날짜뿐).
            for pid, adm in ((1, None), (2, d(-10)), (3, d(-10)), (4, d(-5)), (5, d(-4)), (6, d(-4)), (7, d(-30))):
                conn.execute("INSERT INTO consultations (id, patient_id, consult_date, patient_age, "
                             "admission_status, actual_admission_date, room_number) "
                             "VALUES (?,?,?,70,'입원완료',?, '301호')", (pid, pid, d(-40), adm))
            # 명부 회차(roster_key) — 1·2·3은 d-10 입원, 열린 채. 4는 옛 회차(d-40~d-20, 명부가 닫음) + 새 회차 d-5.
            self._roster(conn, 1, d(-10), room="302호")
            self._roster(conn, 2, d(-10))
            self._roster(conn, 3, d(-10))
            self._roster(conn, 4, d(-40), dis=d(-20), no=1)
            self._roster(conn, 4, d(-5), no=2)
            # 5·6은 명부 이후 CRM에서 입원완료한 환자(roster_key 없는 회차) — 2026-09-16 규칙으로 재원에 더해진다.
            for pid in (5, 6):
                conn.execute("""INSERT INTO admission_episodes
                    (patient_id, episode_no, status, admitted_at, room_number, consultation_id)
                    VALUES (?, 1, 'admitted', ?, '303호', ?)""", (pid, d(-4), pid))
            # 7은 명부 마지막 입원일(d-5)보다 **앞선** CRM 회차 — 퇴원완료를 안 눌러 열린 채 남은 옛 기록.
            # 이런 게 746건 쌓여 있다. 명부 이전 구간은 명부가 사실이므로 어느 날짜에도 세면 안 된다.
            conn.execute("""INSERT INTO admission_episodes
                (patient_id, episode_no, status, admitted_at, room_number, consultation_id)
                VALUES (7, 1, 'admitted', ?, '303호', 7)""", (d(-30),))
            # 외진 기록
            self._away(conn, 1, d(-2))                       # 1: 이틀 전 응급전원, 미복귀 → 재원 아님
            self._away(conn, 3, d(-5), returned=d(-3))       # 3: 나갔다 돌아옴 → 재원
            self._away(conn, 4, d(-30))                      # 4: 옛 입원 때 나가서 기록이 안 닫힘 — 새 회차(d-5)는 재원
            self._away(conn, 5, d(-1))                       # 5: CRM 입원인데 외진 중 → 재원 아님

    @staticmethod
    def _roster(conn, pid, adm, dis=None, no=1, room="301호"):
        conn.execute("""INSERT INTO admission_episodes
            (patient_id, episode_no, status, admitted_at, discharged_at, room_number, ward, roster_key)
            VALUES (?,?,?,?,?,?, '3병동', ?)""",
                     (pid, no, "discharged" if dis else "admitted", adm, dis, room, f"c{pid}|{adm}"))

    @staticmethod
    def _away(conn, cid, out, returned=None):
        conn.execute("""INSERT INTO admission_events (consultation_id, event_type, event_date, hospital, returned_at)
                        VALUES (?, '응급전원', ?, '안동병원', ?)""", (cid, out, returned))

    # ── ①②⑤ 판정 하나를 모든 화면이 같이 쓴다 ──

    def test_away_patient_is_not_in_census(self):
        """명부 회차가 열려 있어도(⑤) 미복귀 외진이면 재원이 아니다. 복귀·옛 외진은 그대로 재원(③)."""
        self.assertEqual(models.current_admission_census()["patients"], {2, 3, 4, 6})

    def test_every_screen_uses_the_same_rule(self):
        # 병동 가동률 — 외진 나간 환자가 침대를 차지한 채 남으면 안 된다 (명부 회차 2·3·4 = 3명)
        wards = {w["ward"]: w for w in dashboard_metrics.ward_occupancy()}
        self.assertEqual(wards["3병동"]["count"], 3)
        # 병실 만실 — 1번 혼자 쓰던 302호는 비어 있어야 한다
        self.assertEqual(dashboard_metrics.room_status("302호")["used"], 0)
        self.assertEqual(dashboard_metrics.room_status("301호")["used"], 3)
        # 재원 추이 복원 — 1번 회차의 퇴원일은 외진 나간 날
        spans = {s["episode_id"]: s for s in models.admission_spans()}
        with models.get_db() as conn:
            ep1 = conn.execute("SELECT id FROM admission_episodes WHERE patient_id=1").fetchone()["id"]
        self.assertEqual(spans[ep1]["discharged_at"], d(-2))
        # 30일 census — 머릿수와 같은 식을 날짜별로 자른다(2026-09-25: CRM 입원완료 회차도 센다).
        #   오늘: 명부 2·3·4 + CRM 6 = 4 (1·5는 외진 중, 7은 명부 이전 옛 CRM 회차라 제외)
        #   d-3 : 명부 1·2·3·4 + CRM 5·6 = 6 (1은 d-2에 나갔고 5는 d-1에 나갔으니 그날은 둘 다 있었다)
        by_date = dashboard_metrics.census_by_date([d(0), d(-3), d(-29)])
        self.assertEqual((by_date[d(0)], by_date[d(-3)]), (4, 6))
        #   d-29: 명부 4의 옛 입원(d-40~d-20)은 그날 외진 중(d-30 출발·미복귀)이라 재원 아님 → 남는 후보는
        #   CRM 7(d-30 입원, 열린 채)뿐인데 명부 이전이라 세면 안 된다 → 0. (1이면 옛 CRM 회차가 새는 것)
        self.assertEqual(by_date[d(-29)], 0, "명부 이전의 열린 CRM 회차(7)가 과거 재원을 부풀리면 안 된다")
        # 오늘 스파크 값 = 재원 머릿수. 두 화면이 같은 숫자를 내야 한다.
        self.assertEqual(by_date[d(0)], len(models.current_admission_census()["patients"]))

    def test_returning_puts_the_patient_back(self):
        """복귀 처리하면 그 자리에서 다시 재원이다 — 명부 재적재를 기다리지 않는다."""
        with models.get_db() as conn:
            conn.execute("UPDATE admission_events SET returned_at=? WHERE consultation_id=1", (d(0),))
        self.assertIn(1, models.current_admission_census()["patients"])

    # ── ④ 재원 관리 화면 ──

    def test_ward_page_lists_away_patient_only_in_the_away_panel(self):
        """상담에 입원일이 없는 외진 환자가 재원에서 빠졌다고 '입원일 미확정' 큐에 떨어지면 안 된다."""
        html = self.client.get("/ward").get_data(as_text=True)
        pending = html[html.index('id="sec-pending"'):]
        self.assertNotIn("외진중", pending, "외진 중 환자가 '입원일 미확정' 큐에 떨어졌다")
        # 외진 중은 재원 현황이 아니라 외진 환자 탭에서 본다(2026-09-19) — 거기엔 있어야 한다
        self.assertIn("외진중", self.client.get("/ward?tab=away").get_data(as_text=True))

    # ── 전원 종결은 여전히 명부 회차를 닫는다 (census에서 빠진 뒤에도) ──

    def test_transfer_outcome_still_closes_the_roster_episode(self):
        with models.get_db() as conn:
            ev_id = conn.execute("SELECT id FROM admission_events WHERE consultation_id=1").fetchone()["id"]
        r = self.client.post(f"/api/admission-event/{ev_id}/return",
                             json={"return_date": d(0), "return_outcome": "전원", "return_hospital": "안동병원"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        with models.get_db() as conn:
            ep = conn.execute("SELECT discharged_at FROM admission_episodes WHERE patient_id=1").fetchone()
        self.assertEqual((ep["discharged_at"] or "")[:10], d(0))
        self.assertNotIn(1, models.current_admission_census()["patients"])


if __name__ == "__main__":
    unittest.main()
