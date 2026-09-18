"""대시보드 최상단 '현재 상태' 스트립(_ward_status_strip).

재원·회복기 비율은 /ward와 같은 근거(원무 명부 census + _care_phase)로 내야
두 화면 숫자가 어긋나지 않는다. 여기서 그 일치와, 이번주·이번달 입퇴원 카운트,
명부가 없을 때의 우아한 대체(has_roster=False)를 고정한다.
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


class DashboardStripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "strip.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        # 대시보드는 기관협력·개선요청 컨텍스트 프로세서를 거치므로 그 스키마도 필요하다.
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("strip-test", "test-password", display_name="점검")
        self.uid = models.get_user("strip-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="strip-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 2 for k in main.MENU_KEYS})

        self.today = date.today()
        old = (self.today - timedelta(days=60)).isoformat()   # 이번주·이번달 밖
        self.today_iso = self.today.isoformat()
        with models.get_db() as conn:
            for pid, name in ((1, "회복기재원"), (2, "비회복기재원"), (3, "오늘퇴원")):
                conn.execute("INSERT INTO patients (id,name,gender) VALUES (?,?,'F')", (pid, name))
            # 환자1 — 상담이 붙은 재원(오늘 입원). 명부 수가구분이 회복기.
            conn.execute(
                """INSERT INTO consultations
                   (id, patient_id, consult_date, admission_status, actual_admission_date,
                    patient_age, primary_diagnosis)
                   VALUES (1, 1, ?, '입원완료', ?, 70, '상세불명의 뇌경색증')""",
                (old, self.today_iso))
            # 회차 — 원무 명부(roster_key)가 재원·입퇴원 흐름의 사실
            rows = [
                # (pid, admitted, discharged, care_type)
                (1, self.today_iso, None, "회복기재활"),        # 오늘 입원 · 회복기 · 재원
                (2, old, None, "비회복기"),                     # 예전 입원 · 비회복기 · 재원(상담 없음=orphan)
                (3, old, self.today_iso, "회복기재활"),          # 오늘 퇴원 · 재원 아님
            ]
            for pid, admitted, discharged, care in rows:
                conn.execute(
                    """INSERT INTO admission_episodes
                       (patient_id, episode_no, status, admitted_at, discharged_at,
                        room_number, ward, care_type, roster_key)
                       VALUES (?, 1, ?, ?, ?, '301호', '3병동', ?, ?)""",
                    (pid, "discharged" if discharged else "admitted",
                     admitted, discharged, care, "chart%d|%s" % (pid, admitted)))

    def test_strip_matches_roster_census(self):
        with main.app.test_request_context():
            strip = main._ward_status_strip()
        self.assertTrue(strip["has_roster"])
        # 재원 2명(회복기1 + 비회복기1), 퇴원한 환자3은 빠진다
        self.assertEqual(strip["admitted"], 2)
        self.assertEqual(strip["recovery"], 1)
        self.assertEqual(strip["recovery_ratio"], 50)   # 1/2
        self.assertTrue(strip["recovery_ratio_ok"])     # 40% 이상
        self.assertEqual(strip["bed_occupancy"],
                         round(2 / main.WARD_BED_CAPACITY * 100, 1))

    def test_strip_flow_counts_this_week_and_month(self):
        with main.app.test_request_context():
            strip = main._ward_status_strip()
        # 오늘 입원 1건(환자1), 오늘 퇴원 1건(환자3) — 둘 다 이번주·이번달 안
        self.assertEqual(strip["week_in"], 1)
        self.assertEqual(strip["week_out"], 1)
        self.assertEqual(strip["month_in"], 1)
        self.assertEqual(strip["month_out"], 1)
        self.assertEqual(strip["away"], 0)

    def test_strip_ratio_agrees_with_ward_kpi_card(self):
        """스트립의 회복기 비율은 /ward KPI 카드와 같은 값이어야 한다."""
        with main.app.test_request_context():
            ratio = main._ward_status_strip()["recovery_ratio"]
        html = self.client.get("/ward").get_data(as_text=True)
        self.assertIn('<span class="wd-k-n">%.2f<small>%%</small></span>' % ratio, html)

    def test_strip_renders_on_dashboard(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("dash-status-strip", html)
        self.assertIn("회복기", html)

    def test_no_roster_hides_census_metrics(self):
        with models.get_db() as conn:
            conn.execute("DELETE FROM admission_episodes")
        with main.app.test_request_context():
            strip = main._ward_status_strip()
        self.assertFalse(strip["has_roster"])
        self.assertIsNone(strip["admitted"])
        self.assertIsNone(strip["recovery_ratio"])
        # 명부가 없어도 흐름·외진 지표는 0으로 계산되고 화면은 '명부 필요'를 띄운다
        self.assertEqual(strip["week_in"], 0)
        self.assertEqual(strip["month_out"], 0)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("명부 필요", html)


if __name__ == "__main__":
    unittest.main()


class DashboardDueQueueTests(DashboardStripTests):
    """회복기 만료·퇴원 예정 큐는 명부 재원만, 만료 ±30일 창만 담는다.

    상담의 '입원완료'는 퇴원해도 안 바뀌어서 그대로 돌리면 예전에 퇴원한 환자까지
    큐를 채운다(재원 264명에 퇴원 예정 652건). 명부 census로 재원만 남기고, 창을
    벗어난 옛 만료는 뺀다.
    """
    def _add_admitted_consult(self, cid, pid, admitted_iso, extension_iso=None):
        with models.get_db() as conn:
            conn.execute(
                """INSERT INTO consultations
                   (id, patient_id, consult_date, admission_status, actual_admission_date,
                    patient_age, primary_diagnosis, discharge_due_date)
                   VALUES (?, ?, ?, '입원완료', ?, 70, '상세불명의 뇌경색증', ?)""",
                (cid, pid, admitted_iso, admitted_iso, extension_iso))

    def _dashboard_counts(self):
        html = self.client.get("/").get_data(as_text=True)
        import re
        out = {}
        # 기한 임박 카드의 탭 버튼: '회복기 전환 D-30 <b class="num">22</b>' / '퇴원 예정 <b class="num">29</b>'
        for lab, short in (("회복기 만료 D-30", "회복기 전환 D-30"), ("퇴원 예정 D-30", "퇴원 예정")):
            m = re.search(re.escape(short) + r' <b class="num">(\d+)</b>', html)
            out[lab] = int(m.group(1))
        return out

    def test_discharged_patient_is_not_in_due_queue(self):
        # 환자4 — 2년 전 입원완료 상담. 명부에는 퇴원으로 남아 있다(재원 아님).
        two_years = (self.today - timedelta(days=730)).isoformat()
        with models.get_db() as conn:
            conn.execute("INSERT INTO patients (id,name,gender) VALUES (4,'옛퇴원','M')")
            conn.execute(
                """INSERT INTO admission_episodes
                   (patient_id, episode_no, status, admitted_at, discharged_at, roster_key)
                   VALUES (4, 1, 'discharged', ?, ?, 'chart4|old')""",
                (two_years, (self.today - timedelta(days=500)).isoformat()))
        self._add_admitted_consult(4, 4, two_years)
        # 환자5 — 재원 중이고 퇴원예정일이 10일 뒤 → 큐에 들어와야 한다.
        with models.get_db() as conn:
            conn.execute("INSERT INTO patients (id,name,gender) VALUES (5,'임박재원','M')")
            conn.execute(
                """INSERT INTO admission_episodes
                   (patient_id, episode_no, status, admitted_at, roster_key)
                   VALUES (5, 1, 'admitted', ?, 'chart5|now')""",
                ((self.today - timedelta(days=100)).isoformat(),))
        self._add_admitted_consult(5, 5, (self.today - timedelta(days=100)).isoformat(),
                                   (self.today + timedelta(days=10)).isoformat())
        counts = self._dashboard_counts()
        self.assertEqual(counts["퇴원 예정 D-30"], 1)

    def test_window_keeps_upcoming_and_moves_overrun_to_queue(self):
        base = (self.today - timedelta(days=100)).isoformat()
        with models.get_db() as conn:
            for pid, name in ((6, "최근초과"), (7, "곧만료")):
                conn.execute("INSERT INTO patients (id,name,gender) VALUES (?,?,'F')", (pid, name))
                conn.execute(
                    """INSERT INTO admission_episodes
                       (patient_id, episode_no, status, admitted_at, roster_key)
                       VALUES (?, 1, 'admitted', ?, ?)""", (pid, base, "chart%d|x" % pid))
        # 둘 다 재원. 예정일이 지난 환자는 '기한 임박'이 아니라 '오늘 처리 필요'(퇴원예정 탭)로 간다.
        self._add_admitted_consult(6, 6, base, (self.today - timedelta(days=5)).isoformat())
        self._add_admitted_consult(7, 7, base, (self.today + timedelta(days=30)).isoformat())
        counts = self._dashboard_counts()
        self.assertEqual(counts["퇴원 예정 D-30"], 1)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("5일 초과", html)
        self.assertIn("퇴원 예정일 5일 지남", html)   # 지난 지 1주일 안 — 큐에 남는다(2026-09-18)

    def test_queue_and_due_rows_show_who_and_primary_dx(self):
        """오늘 처리 필요·기한 임박의 환자 이름 옆에 성별/나이, 별도 열에 주상병이 붙는다."""
        base = (self.today - timedelta(days=100)).isoformat()
        with models.get_db() as conn:
            for pid, name, gender in ((6, "최근초과", "F"), (7, "곧만료", "M"), (8, "재연락대기", "M")):
                conn.execute("INSERT INTO patients (id,name,gender) VALUES (?,?,?)", (pid, name, gender))
            for pid in (6, 7):
                conn.execute(
                    """INSERT INTO admission_episodes
                       (patient_id, episode_no, status, admitted_at, roster_key)
                       VALUES (?, 1, 'admitted', ?, ?)""", (pid, base, "chart%d|x" % pid))
            # 재연락 대기(상담요청) — 주상병은 비우고 병명 목록만 있어 첫 병명이 주상병 열에 나온다.
            conn.execute(
                """INSERT INTO consultations
                   (id, patient_id, consult_date, consult_result, patient_age, diseases)
                   VALUES (8, 8, ?, '상담요청', 65, '["척수손상", "기저질환"]')""", (self.today_iso,))
            # 보류 행은 역직렬화를 거치지 않아 diseases가 JSON 문자열 그대로 — '["…"]'가 찍히면 안 된다.
            conn.execute("INSERT INTO patients (id,name,gender) VALUES (9,'보류환자','F')")
            conn.execute(
                """INSERT INTO consultations
                   (id, patient_id, consult_date, consult_result, patient_age, diseases, updated_at)
                   VALUES (9, 9, ?, '상담보류', 80, '["파킨슨병"]', ?)""",
                (self.today_iso, (self.today - timedelta(days=2)).isoformat() + " 09:00:00"))
        self._add_admitted_consult(6, 6, base, (self.today - timedelta(days=5)).isoformat())   # 오늘 처리 필요(초과)
        self._add_admitted_consult(7, 7, base, (self.today + timedelta(days=30)).isoformat())  # 기한 임박(D-30)
        html = self.client.get("/").get_data(as_text=True)
        self.assertGreaterEqual(html.count("<th>주상병</th>"), 3)     # 처리 큐 · 회복기 전환 · 퇴원 예정
        self.assertIn('최근초과</a> <span class="dash-who">여/70세</span>', html)
        self.assertIn('곧만료</a> <span class="dash-who">남/70세</span>', html)
        self.assertIn('재연락대기</a> <span class="dash-who">남/65세</span>', html)
        self.assertIn('<td class="dash-dx" title="상세불명의 뇌경색증">상세불명의 뇌경색증</td>', html)
        self.assertIn('<td class="dash-dx" title="척수손상">척수손상</td>', html)
        self.assertIn('<td class="dash-dx" title="파킨슨병">파킨슨병</td>', html)
        self.assertNotIn('title="[&#34;', html)
        # 경과·D-day 색상 단계: 오늘 상담요청=fresh, 5일 초과=old, D-30=far
        self.assertIn('aq-meta age-fresh', html)
        self.assertIn('aq-meta age-old', html)
        self.assertIn('<span class="dd dd-far" title="15일 이상 남음">D-30</span>', html)

    def test_no_roster_falls_back_to_consultation_status(self):
        with models.get_db() as conn:
            conn.execute("DELETE FROM admission_episodes")
        self._add_admitted_consult(8, 1, (self.today - timedelta(days=100)).isoformat(),
                                   (self.today + timedelta(days=5)).isoformat())
        counts = self._dashboard_counts()
        self.assertEqual(counts["퇴원 예정 D-30"], 1)


class BedCapacityConfigTests(unittest.TestCase):
    """config의 병실별 병상 표가 병동 허가 병상과 어긋나면 대시보드 '병동별 재원'에 '미확인'이 뜬다.

    2026-09-14 원무 확인: 5인실 205·206·207·303·304·305, 2인실 306·307·903·904·905,
    1인실 502, 3인실 1207·1307, 나머지 4인실, 총 355병상.
    """

    def test_room_beds_sum_to_ward_capacity(self):
        from config import ROOM_BED_CAPACITIES, WARD_BED_CAPACITIES
        by_ward = {}
        for room, cap in ROOM_BED_CAPACITIES.items():
            n = int(room.rstrip("호"))
            by_ward[f"{n // 100}병동"] = by_ward.get(f"{n // 100}병동", 0) + cap
        for ward, cap in WARD_BED_CAPACITIES.items():
            self.assertEqual(by_ward.get(ward), cap, f"{ward}: 병실 합 {by_ward.get(ward)} ≠ 허가 병상 {cap}")
        self.assertEqual(set(by_ward), set(WARD_BED_CAPACITIES), "병동 목록이 서로 다름")
        self.assertEqual(sum(WARD_BED_CAPACITIES.values()), 355)
        self.assertEqual(sum(WARD_BED_CAPACITIES.values()), main.WARD_BED_CAPACITY)

    def test_room_exceptions_match_confirmed_layout(self):
        from config import ROOM_BED_CAPACITIES
        expected = {"205호": 5, "206호": 5, "207호": 5, "303호": 5, "304호": 5, "305호": 5,
                    "306호": 2, "307호": 2, "903호": 2, "904호": 2, "905호": 2,
                    "502호": 1, "1207호": 3, "1307호": 3}
        for room, cap in expected.items():
            self.assertEqual(ROOM_BED_CAPACITIES.get(room), cap, room)
        others = {r: c for r, c in ROOM_BED_CAPACITIES.items() if r not in expected}
        self.assertTrue(all(c == 4 for c in others.values()),
                        f"4인실이 아닌 예외 외 방: {[r for r, c in others.items() if c != 4]}")
