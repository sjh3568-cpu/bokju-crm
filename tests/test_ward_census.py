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
                        room_number, ward, diagnosis_name, attending_doctor, roster_key)
                       VALUES (?, 1, ?, ?, ?, ?, '3병동', '상세불명의 뇌경색증', '변현숙', ?)""",
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

    def test_ratio_trend_period_is_selectable(self):
        """통계 페이지와 같은 preset/from/to로 고른 기간을 일별 그래프가 그린다."""
        import json, re
        def series_of(url):
            html = self.client.get(url).get_data(as_text=True)
            return [json.loads(m) for m in re.findall(r"data-series='(\[.*?\])'", html)], html
        (daily, monthly, _f), html = series_of("/ward?tab=trend")
        self.assertEqual(len(daily), 30)          # 기본은 최근 30일
        self.assertEqual(len(monthly), 12)
        (daily, monthly, _f), html = series_of("/ward?tab=trend&preset=90")
        self.assertEqual(len(daily), 90)
        self.assertIn('<label class="preset-chip on"><input type="radio" name="preset" value="90" checked>', html)
        (daily, monthly, _f), _ = series_of("/ward?tab=trend&preset=custom&from=2025-01-05&to=2025-01-14")
        self.assertEqual([d["date"] for d in daily][::9], ["2025-01-05", "2025-01-14"])
        self.assertEqual(len(daily), 10)
        self.assertEqual(len(monthly), 12)        # 월별은 최소 12개월
        self.assertEqual(monthly[-1]["label"], "25.01")
        # 기간이 1년 넘게 걸치면 월별도 그만큼 늘어난다
        (daily, monthly, _f), _ = series_of("/ward?tab=trend&preset=custom&from=2024-01-01&to=2025-03-31")
        self.assertEqual(len(monthly), 15)
        # 종료일이 시작일보다 앞서면 서로 바꿔 쓴다
        (daily, _m, _f), _ = series_of("/ward?tab=trend&preset=custom&from=2025-01-14&to=2025-01-05")
        self.assertEqual(len(daily), 10)

    def test_ratio_trend_dates_apply_without_custom_radio(self):
        """날짜만 바꾸고 라디오가 프리셋에 남아 있어도 입력한 기간을 쓴다."""
        import json, re
        html = self.client.get("/ward?tab=trend&preset=30&from=2025-01-05&to=2025-01-14").get_data(as_text=True)
        daily = json.loads(re.findall(r"data-series='(\[.*?\])'", html)[0])
        self.assertEqual(len(daily), 10)
        self.assertIn('value="custom" checked', html)
        # 종료일만 과거로 바꿔도 직접지정
        html = self.client.get("/ward?tab=trend&preset=30&to=2025-01-14").get_data(as_text=True)
        self.assertIn("2024-12-16 ~ 2025-01-14", html)
        # 프리셋 칩은 날짜칸을 비우고 넘어온다 — 빈 날짜는 프리셋을 흔들지 않는다
        html = self.client.get("/ward?tab=trend&preset=90&from=&to=").get_data(as_text=True)
        self.assertEqual(len(json.loads(re.findall(r"data-series='(\[.*?\])'", html)[0])), 90)
        self.assertIn('value="90" checked', html)

    def test_ratio_insight_margins(self):
        """40% 기준선까지의 여유·필요 인원 산식."""
        from datetime import date
        ratio_at = lambda d: {"total": 0, "known": 0, "recovery": 0, "ratio": 0}
        # 회복기 5 / 판정 10 = 50% → 회복기 1명 나가면 4/9=44.4%, 2명이면 3/8=37.5%
        ins = main._ratio_insight({"total": 12, "known": 10, "recovery": 5, "ratio": 50.0}, [], ratio_at, date(2026, 9, 11))
        self.assertTrue(ins["ok"])
        self.assertEqual((ins["rec_out"], ins["rec_out_ratio"]), (1, 37.5))
        # 비회복기는 2명까지 (5/12=41.7%), 3명째 5/13=38.5%
        self.assertEqual((ins["non_in"], ins["non_in_ratio"]), (2, 38.5))
        # 회복기 3 / 판정 10 = 30% → 회복기 2명 들어오면 5/12=41.7%, 비회복기 3명 나가면 3/7=42.9%
        ins = main._ratio_insight({"total": 10, "known": 10, "recovery": 3, "ratio": 30.0}, [], ratio_at, date(2026, 9, 11))
        self.assertFalse(ins["ok"])
        self.assertEqual((ins["rec_in"], ins["rec_in_ratio"]), (2, 41.7))
        self.assertEqual((ins["non_out"], ins["non_out_ratio"]), (3, 42.9))
        self.assertEqual(len(ins["forecast"]), 61)
        self.assertIsNone(main._ratio_insight({"total": 0, "known": 0, "recovery": 0, "ratio": 0}, [], ratio_at, date(2026, 9, 11)))

    def test_trend_summary_is_person_day_weighted(self):
        """기간 평균은 연인원 가중이라 인원이 적은 날에 끌려가지 않는다."""
        daily = [
            {"date": "2026-09-01", "label": "09.01", "known": 100, "recovery": 45, "ratio": 45.0, "total": 100},
            {"date": "2026-09-02", "label": "09.02", "known": 10, "recovery": 1, "ratio": 10.0, "total": 10},
            {"date": "2026-09-03", "label": "09.03", "known": 0, "recovery": 0, "ratio": 0, "total": 0},
        ]
        monthly = [{"date": "2026-09-30", "label": "26.09", "known": 50, "recovery": 20, "ratio": 40.0, "total": 50}]
        ts = main._trend_summary(daily, monthly)
        self.assertEqual(ts["avg"], 41.8)            # 46/110
        self.assertEqual(ts["avg_simple"], 27.5)     # (45+10)/2 — known 0인 날은 제외
        self.assertEqual((ts["days"], ts["below_days"], ts["below_first"]["label"]), (2, 1, "09.02"))
        self.assertEqual((ts["low"]["label"], ts["high"]["label"]), ("09.02", "09.01"))
        self.assertEqual(ts["month_avg"], 40.0)
        self.assertEqual([(r["start"]["label"], r["end"]["label"], r["days"]) for r in ts["below_runs"]], [("09.02", "09.02", 1)])
        # 떨어진 날이 이어지면 한 구간, 사이에 40% 이상인 날이 끼면 두 구간
        run_days = [dict(daily[1], date="2026-09-%02d" % d, label="09.%02d" % d, ratio=r, recovery=r) for d, r in ((5, 39.0), (6, 35.0), (7, 41.0), (8, 38.0))]
        runs = main._trend_summary(run_days, [])["below_runs"]
        self.assertEqual([(r["start"]["label"], r["end"]["label"], r["days"], r["low"]["label"]) for r in runs],
                         [("09.05", "09.06", 2, "09.06"), ("09.08", "09.08", 1, "09.08")])
        self.assertTrue(ts["ok"])
        self.assertIsNone(main._trend_summary([daily[2]], []))

    def test_trend_page_renders_insight(self):
        html = self.client.get("/ward?tab=trend").get_data(as_text=True)
        self.assertIn("wd-insight", html)
        self.assertIn("가정 계산기", html)
        self.assertIn('data-forecast="1"', html)

    def test_search_filters_roster_only_patients_too(self):
        """환자명 검색에 상담 없는 명부 환자가 전부 딸려 나오지 않는다."""
        html = self.client.get("/ward?view=list&q=재원환자").get_data(as_text=True)
        self.assertIn("재원환자", html)
        self.assertNotIn("명부환자", html)
        html = self.client.get("/ward?view=list&q=명부환자").get_data(as_text=True)
        self.assertIn("명부환자", html)
        # 명부 값(병실·진단)으로도 찾힌다
        self.assertIn("명부환자", self.client.get("/ward?view=list&q=405").get_data(as_text=True))
        self.assertTrue(main._orphan_matches({"patient_name": "홍길동", "room_number": "301호"}, ""))
        self.assertFalse(main._orphan_matches({"patient_name": "홍길동"}, "김"))

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

    def test_attending_doctor_comes_from_roster(self):
        """주치의는 상담 시점에 안 정해져 상담일지에는 대개 비어 있다 — 명부 값을 쓴다."""
        html = self.client.get("/ward?view=list").get_data(as_text=True)
        roster = html.split('id="sec-discharged"')[0]
        # 상담일지에는 주치의가 없는데도 재원 두 명 모두에 명부 값이 붙는다
        self.assertGreaterEqual(roster.count("변현숙"), 2)
        with models.get_db() as conn:
            self.assertIsNone(conn.execute(
                "SELECT attending_doctor FROM consultations WHERE id=1").fetchone()[0])

    def test_expired_recovery_period_counts_as_nonrecovery(self):
        """S005 수가 기간이 끝난 환자는 회복기 비율에 세지 않는다.

        label은 '회복기로 입원했는가'(발병일 기준)를 말할 뿐이다. 입원일부터
        흘러간 수가 기간을 따로 보지 않으면 D+504인 환자까지 회복기로 세어
        비율이 부푼다. 월별 추이는 원래 만료를 반영하고 있어 KPI만 어긋났다.
        """
        long_ago = "2023-01-02"
        with models.get_db() as conn:
            conn.execute(
                """UPDATE consultations SET diseases = '["뇌출혈"]',
                       disease_onset = ?, admission_purpose = '회복기재활'
                   WHERE id = 1""", (long_ago,))
            conn.execute("UPDATE admission_episodes SET admitted_at = ? "
                         "WHERE patient_id = 1", (long_ago,))
        con = models.get_consultation(1)
        con["actual_admission_date"] = long_ago
        phase = main._care_phase(con)
        self.assertIsNotNone(phase["phase_dday"])
        self.assertLess(phase["phase_dday"], 0)        # 수가 기간이 지났다
        self.assertEqual(phase["care_phase"], "비회복기")

    def test_roster_recovery_does_not_override_expired_period(self):
        """명부가 회복기 대상이어도 만료된 환자는 목록·필터·추이에서 제외한다."""
        with models.get_db() as conn:
            # 수가 기간이 한참 지난 환자 — 추정이면 비회복기다
            conn.execute("""UPDATE consultations SET diseases = '["뇌출혈"]',
                                disease_onset = '2023-01-02', admission_purpose = '회복기재활'
                            WHERE id = 1""")
            conn.execute("UPDATE admission_episodes SET admitted_at = '2023-01-02' "
                         "WHERE patient_id = 1")
        con = models.get_consultation(1)
        con["actual_admission_date"] = "2023-01-02"
        self.assertEqual(main._care_phase(con)["care_phase"], "비회복기")

        with models.get_db() as conn:
            conn.execute("UPDATE admission_episodes SET care_type = '회복기재활' "
                         "WHERE patient_id = 1")
        with patch.object(main, "render_template", return_value="") as render:
            self.assertEqual(self.client.get("/ward?view=list&filt=recovery").status_code, 200)
            self.assertNotIn(1, [c["patient_id"] for c in render.call_args.kwargs["admitted"]])
        with patch.object(main, "render_template", return_value="") as render:
            self.client.get("/ward?view=list&filt=nonrecovery")
            patient = next(c for c in render.call_args.kwargs["admitted"] if c["patient_id"] == 1)
            self.assertEqual(patient["care_phase"], "비회복기")
            self.assertEqual(patient["phase_end_date"], "2024-01-01")
        import json, re
        html = self.client.get("/ward?tab=trend").get_data(as_text=True)
        series = json.loads(re.search(r"data-series='(\[.*?\])'", html).group(1))
        self.assertEqual(series[-1]["recovery"], 0)

    def test_roster_expiry_boundaries_and_unknown_period(self):
        from datetime import date, timedelta
        start = date(2026, 1, 1)
        end = start + timedelta(days=179)
        phase = main._effective_roster_care_phase
        self.assertEqual(phase("회복기", ["뇌출혈"], str(start), end), "회복기")
        self.assertEqual(phase("회복기", ["뇌출혈"], str(start), end + timedelta(days=1)), "비회복기")
        self.assertEqual(phase("비회복기", ["뇌출혈"], str(start), start), "비회복기")
        self.assertEqual(phase("회복기", [], str(start), end), "회복기")
        self.assertEqual(phase("회복기", ["뇌출혈"], None, end), "회복기")
        self.assertEqual(phase("회복기", ["비사용증후군"], str(start), start + timedelta(days=59)), "회복기")
        self.assertEqual(phase("회복기", ["비사용증후군"], str(start), start + timedelta(days=60)), "단일구간")

    def test_q_end_date_drives_screen_trend_and_csv(self):
        from datetime import date, timedelta
        future = (date.today() + timedelta(days=10)).isoformat()
        with models.get_db() as conn:
            conn.execute("UPDATE admission_episodes SET care_type='회복기(S005)', "
                         "rehab_end_imported=1, rehab_end_date=? WHERE patient_id=1", (future,))
            conn.execute("UPDATE admission_episodes SET care_type='비회복기(S006)', "
                         "rehab_end_imported=1, rehab_end_date=NULL WHERE patient_id=3")
            conn.execute("UPDATE consultations SET diseases='[\"비사용증후군\"]' WHERE id=1")
        with patch.object(main, "render_template", return_value="") as render:
            self.client.get("/ward?view=list&filt=recovery")
            rows = render.call_args.kwargs["admitted"]
            self.assertEqual([c["patient_id"] for c in rows], [1])
            self.assertEqual(rows[0]["phase_dday"], 10)
            self.assertEqual(rows[0]["phase_end_date"], future)
        with main.app.test_request_context("/ward.csv"):
            rows = main._ward_admitted_roster(None, None)
            self.assertEqual({r["patient_id"]: r["care_phase"] for r in rows},
                             {1: "회복기", 3: "비회복기"})
        import json, re
        html = self.client.get("/ward?tab=trend").get_data(as_text=True)
        series = json.loads(re.search(r"data-series='(\[.*?\])'", html).group(1))
        self.assertEqual(series[-1]["total"], 2)
        self.assertEqual(series[-1]["known"], 2)
        self.assertEqual(series[-1]["recovery"], 1)
        self.assertEqual(series[-1]["ratio"], 50.0)

    def test_q_date_boundary_blank_and_far_future(self):
        from datetime import date
        phase = main._effective_roster_care_phase
        for raw, snapshot, expected in (
                ("2026-09-11", date(2026, 9, 11), "회복기"),
                ("2026-09-11", date(2026, 9, 12), "비회복기"),
                (None, date(2026, 9, 11), "비회복기"),
                ("9999-12-31", date(2026, 9, 11), "회복기")):
            self.assertEqual(phase("회복기", ["비사용증후군"], "2023-01-01",
                                   snapshot, raw, True), expected)

    def test_away_panel_includes_transferred_patient_outside_census(self):
        with models.get_db() as conn:
            conn.execute("INSERT INTO admission_events(consultation_id,event_type,event_date,hospital,event_time) "
                         "VALUES(2,'응급전원','2024-05-01','확인병원','13:30')")
            conn.execute("INSERT INTO admission_events(consultation_id,event_type,event_date,returned_at) "
                         "VALUES(1,'응급전원','2026-08-03','2026-08-04')")
        with patch.object(main, "render_template", return_value="") as render:
            response = self.client.get("/ward?view=list")
            self.assertEqual(response.status_code, 200)
            ctx = render.call_args.kwargs
            self.assertEqual({r['patient_id'] for r in ctx['admitted']}, {1, 3})
            self.assertEqual([r['patient_id'] for r in ctx['away']], [2])
            self.assertEqual(ctx['kpis']['away'], 1)
            self.assertEqual(ctx['away'][0]['away']['event_time'], '13:30')
        html = self.client.get("/ward?view=list").get_data(as_text=True)
        panel = html.split('id="sec-away"')[1].split('id="sec-discharged"')[0]
        self.assertIn('이미퇴원한사람', panel)
        self.assertIn('확인병원', panel)
        self.assertIn('13:30', panel)

    def test_away_panel_deduplicates_patient_and_badge_ignores_previous_admission(self):
        events = [dict(id=1,pid=5,consultation_id=20,pname='검증',event_date='2026-08-01'),
                  dict(id=2,pid=5,consultation_id=21,pname='검증',event_date='2026-08-04')]
        panel = main._ward_away_panel(events)
        self.assertEqual(len(panel), 1)
        self.assertEqual(panel[0]['away']['id'], 2)
        self.assertEqual(main._ward_current_away(
            dict(id=99,patient_id=5,admitted_on='2026-08-02'), {5: events[1]})['id'], 2)
        self.assertIsNone(main._ward_current_away(
            dict(patient_id=5,admitted_on='2026-08-05'), {5: events[1]}))

    def test_roster_care_type_values_are_read_by_prefix(self):
        for raw, expected in (("회복기", "회복기"), ("회복기재활", "회복기"),
                              ("회복기(S005)", "회복기"), ("비회복기재활", "비회복기"),
                              ("일반재활", "미판정"), ("요양", "미판정"),
                              ("", None), ("다제내성균", None)):
            self.assertEqual(main._roster_care_phase(raw), expected, raw)


if __name__ == "__main__":
    unittest.main()
