"""입원 기간 선택(근골격계 30·60일)과 비사용증후군 파킨슨의 회복기→비회복기 전환 (2026-09-29 사용자 정의).

- 근골격계는 30일 입원과 60일 입원이 있다. 상담일지 '입원 기간 선택'(stay_days)이 퇴원 예정일과
  '오늘 처리 필요 → 퇴원지연' 판정에 쓰인다. 비워 두면 병명 규칙(고관절·대퇴·골반 30, 내고정·치환술·다발 60).
- 비사용증후군으로 입원한 파킨슨은 입원 60일까지 회복기, 그 뒤 비회복기로 전환해 1년까지 재원한다.
  60일이 지났다고 '퇴원지연'에 오르면 안 된다. 근골격계 입원에 기저질환 파킨슨이 붙은 경우는 근골격계 규칙.
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
from app import (DISCHARGE_OVERDUE_GRACE_DAYS, _admission_expiry, _care_phase, _day_of,
                 _discharge_watch, compute_admission_period, is_parkinson_disuse)


def d(n):
    return (date.today() + timedelta(days=n)).isoformat()


def left(admitted_days_ago, stay):
    """입원 n일 전 + 재원 기간 → 오늘 기준 잔여일. 입원일을 1일째로 세는 _day_of 규칙 그대로."""
    return (_day_of(date.today() - timedelta(days=admitted_days_ago), stay) - date.today()).days


class StayPlanRuleTests(unittest.TestCase):
    def _c(self, **kw):
        base = {"admission_purpose": "회복기재활", "actual_admission_date": d(-10),
                "consult_date": d(-20), "admission_status": "입원완료"}
        base.update(kw)
        return base

    def test_msk_default_rule_and_selected_days(self):
        self.assertEqual(compute_admission_period(["고관절 골절"], None)["total"], 30)
        self.assertEqual(compute_admission_period(["고관절 골절"], None, 60)["total"], 60)
        self.assertEqual(compute_admission_period(["고관절 골절"], None, "60")["total"], 60)
        self.assertEqual(compute_admission_period(["다발부위"], None, 30)["total"], 30)
        for empty in ("", None, "abc", 0):
            self.assertEqual(compute_admission_period(["고관절 골절"], None, empty)["total"], 30, empty)
        self.assertTrue(compute_admission_period(["고관절 골절"], None, 60)["mandatory"])

    def test_selected_days_move_the_due_date_out_of_overdue(self):
        # 입원 40일째 고관절 골절 — 30일 규칙이면 10일 지남, 60일을 골랐으면 아직 D-20 '퇴원예정'
        c = self._c(diseases=["고관절 골절"], actual_admission_date=d(-40))
        self.assertEqual(_discharge_watch(c)["days_left"], left(40, 30))
        self.assertLess(left(40, 30), -DISCHARGE_OVERDUE_GRACE_DAYS)    # 30일 규칙이면 이미 '연장'
        c["stay_days"] = 60
        watch = _discharge_watch(c)
        self.assertEqual((watch["state"], watch["days_left"]), ("퇴원예정", left(40, 60)))

    def test_parkinson_disuse_detection(self):
        self.assertTrue(is_parkinson_disuse(["파킨슨(신규)"]))
        self.assertTrue(is_parkinson_disuse(["비사용증후군", "파킨슨"]))
        self.assertTrue(is_parkinson_disuse(["파킨슨-상세"]))
        self.assertFalse(is_parkinson_disuse(["고관절 골절", "파킨슨"]))   # 근골격계 입원 + 기저 파킨슨
        self.assertFalse(is_parkinson_disuse(["뇌경색", "파킨슨"]))        # 중추 규칙이 먼저
        self.assertFalse(is_parkinson_disuse(["비사용증후군"]))
        self.assertFalse(is_parkinson_disuse([]))

    def test_parkinson_period_is_one_year_with_60_day_recovery(self):
        p = compute_admission_period(["파킨슨(신규)"], None)
        self.assertEqual((p["total"], p["billing"], p["mandatory"], p.get("conversion")),
                         (365, 60, False, True))
        # '입원 기간 선택'은 근골격계용 — 파킨슨엔 영향 없음
        self.assertEqual(compute_admission_period(["파킨슨(신규)"], None, 30)["total"], 365)

    def test_parkinson_phase_converts_after_60_days(self):
        pk = lambda n, **kw: self._c(diseases=["파킨슨(신규)"], actual_admission_date=d(-n), **kw)
        self.assertEqual(_care_phase(pk(10))["care_phase"], "회복기")
        self.assertEqual(_care_phase(pk(59))["care_phase"], "회복기")
        self.assertEqual(_care_phase(pk(61))["care_phase"], "비회복기")
        # 명부의 회복기 표시가 있어도 60일이 지나면 비회복기
        c = self._c(diseases=["비사용증후군", "파킨슨"], actual_admission_date=d(-100), roster_care_phase="회복기")
        self.assertEqual(_care_phase(c)["care_phase"], "비회복기")
        # 근골격계 입원 + 기저 파킨슨은 그대로 회복기(전환 없음)
        c = self._c(diseases=["고관절 골절", "파킨슨"], actual_admission_date=d(-100))
        self.assertEqual(_care_phase(c)["care_phase"], "회복기")

    def test_parkinson_is_not_overdue_after_60_days(self):
        c = self._c(diseases=["파킨슨(신규)"], actual_admission_date=d(-70))
        watch = _discharge_watch(c)
        self.assertIsNone(watch["state"])              # 퇴원 예정일은 1년 뒤 — 큐에 안 오른다
        self.assertEqual(watch["days_left"], left(70, 365))
        ax = _admission_expiry(c)
        self.assertEqual(ax["billing_left"], left(70, 60))   # 전환 시점은 지났다
        self.assertLess(ax["billing_left"], 0)
        self.assertFalse(ax["mandatory"])

    def test_parkinson_from_primary_diagnosis_only(self):
        """진단군 칸이 비고 주상병에만 '파킨슨병'이 적힌 환자도 비사용증후군 파킨슨으로 본다."""
        c = self._c(diseases=[], primary_diagnosis="파킨슨병", actual_admission_date=d(-70))
        self.assertEqual(_admission_expiry(c)["total_days"], 365)
        self.assertIsNone(_discharge_watch(c)["state"])


class HeaderDischargeCountTests(unittest.TestCase):
    """상단 바 '퇴원 N' — 상담 퇴원일만 세면 명부 회차로만 닫힌 분·외진(타 병원 전원)이 빠진다(권해옥 님 9/29).
    대시보드 KPI와 같은 단일 근거(admission_flow_events)로 센다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "hd.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("hd-test", "test-password", display_name="점검")
        self.uid = models.get_user("hd-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="hd-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        today = date.today().isoformat()
        # (환자, 상담 상태, 상담 퇴원일, 회차 퇴원일)
        rows = [(1, "명부퇴원", "입원완료", None, today),      # 상담은 아직 입원완료 — 명부 회차만 오늘 닫힘
                (2, "상담퇴원", "퇴원완료", today, today),     # 둘 다 오늘 — 한 명으로
                (3, "재원중", "입원완료", None, None)]
        with models.get_db() as conn:
            for pid, name, status, ddate, ep_out in rows:
                conn.execute("INSERT INTO patients (id,name,gender) VALUES (?,?,'F')", (pid, name))
                conn.execute("""INSERT INTO consultations (id, patient_id, consult_date, admission_status,
                                actual_admission_date, discharge_date, attending_doctor, room_number, patient_age)
                                VALUES (?,?,?,?,?,?,'RM1 이성범 부장','301호',70)""",
                             (pid, pid, d(-40), status, d(-30), ddate))
                conn.execute("""INSERT INTO admission_episodes (patient_id, consultation_id, episode_no, status,
                                admitted_at, discharged_at, room_number, ward, roster_key)
                                VALUES (?,?,1,?,?,?,'301호','3병동',?)""",
                             (pid, pid, "discharged" if ep_out else "admitted", d(-30), ep_out, f"r{pid}|x"))

    def test_header_counts_roster_only_discharge(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("퇴원 <b>2</b>", html)


if __name__ == "__main__":
    unittest.main()
