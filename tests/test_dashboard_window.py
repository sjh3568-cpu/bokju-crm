"""대시보드 입원 환자 현황 — 기본 기간 어제~내일, 복귀·상담 중복 제거, 구분 배지 (2026-09-16 요청)."""
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


class DashboardWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "win.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("win-test", "test-password", display_name="점검")
        self.uid = models.get_user("win-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="win-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        with models.get_db() as conn:
            for pid, name in ((1, "어제예정미처리"), (2, "오늘완료"), (3, "내일예정"), (4, "장광진복귀"), (5, "다음주예정")):
                conn.execute("INSERT INTO patients (id,name,gender) VALUES (?,?,'M')", (pid, name))
            rows = [  # id, pid, status, planned, actual
                (1, 1, "입원예정", d(-1), None),
                (2, 2, "입원완료", d(0), d(0)),
                (3, 3, "입원예정", d(1), None),
                (4, 4, "입원완료", d(0), d(0)),     # 외진 복귀한 날 상담도 입원완료로 처리된 케이스
                (5, 5, "입원예정", d(7), None),
            ]
            for cid, pid, st, planned, actual in rows:
                conn.execute("""INSERT INTO consultations (id, patient_id, consult_date, admission_status,
                                planned_admission_date, actual_admission_date, attending_doctor, room_number, patient_age,
                                source_hospital, current_location_type, admission_purpose, disease_detail,
                                referral_source_type, referral_source_detail, referrer_person, referrer_institution)
                                VALUES (?,?,?,?,?,?,'RM1 이성범 부장','301호',70,?,?,'회복기재활','뇌손상 / 뇌경색',
                                        '["소개"]','["지인추천"]',?,?)""",
                             (cid, pid, d(-20), st, planned, actual,
                              None if pid == 3 else "제천서울병원", "집" if pid == 3 else "입원중",
                              "홍길동" if pid == 2 else None, "안동병원 사회사업실" if pid == 2 else None))
            # 4번: 20일 전 응급전원 → 오늘 복귀
            conn.execute("""INSERT INTO admission_events (consultation_id, event_type, event_date, hospital, returned_at, return_outcome, expected_return_date)
                            VALUES (4, '응급전원', ?, '안동병원', ?, '복귀', ?)""", (d(-20), d(0), d(0)))
        # 화면에서 저장했을 때처럼 회차(admission_episodes)도 만들어 둔다 — 재원·이번주 입원 집계가 회차를 본다
        for cid in (1, 2, 3, 4, 5):
            models.sync_admission_episode(cid)

    def test_default_window_is_yesterday_to_tomorrow_and_shows_all_three_days(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("어제~내일", html)
        for name in ("어제예정미처리", "오늘완료", "내일예정", "장광진복귀"):
            self.assertIn(name, html, name)
        self.assertNotIn("다음주예정", html)
        # 접기 없이 — 선택일 전체 표에 data-limit이 없다
        self.assertNotRegex(html, r'dash-adm-tbl"[^>]*data-limit=')

    def test_return_row_replaces_same_day_consultation_row(self):
        rows = [r for r in models.dashboard_summary(d(-1), d(1))["admission_schedule"] if r["patient_id"] == 4]
        self.assertEqual(len(rows), 1, "복귀 행 하나만 남아야 한다")
        self.assertEqual(rows[0]["admission_kind"], "return")

    def test_kind_badges(self):
        """구분은 상담실이 쓰는 말 그대로 — 입원예정·입원완료·외진복귀 (2026-09-19 요청)."""
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('adm-kind-planned', html)   # 입원예정
        self.assertIn('adm-kind-done', html)      # 입원완료
        self.assertIn('adm-kind-return', html)    # 외진복귀
        self.assertIn('>외진복귀</span>', html)
        self.assertIn('>입원예정</span>', html)
        self.assertIn('>입원완료</span>', html)

    def test_origin_column_shows_source_hospital_or_home_and_away_type(self):
        """'모병원·외진' 칸 — 일반 입원은 모병원(집이면 자택), 복귀는 외진 종류·병원. 병명·입원목적은 안 겹친다."""
        rows = {r["patient_id"]: r for r in models.dashboard_summary(d(-1), d(1))["admission_schedule"]}
        self.assertEqual(rows[2]["other_note"], "제천서울병원")
        self.assertEqual(rows[3]["other_note"], "자택")
        self.assertEqual(rows[4]["other_note"], "응급전원 · 안동병원")
        self.assertIn("입원목적 회복기재활", rows[2]["other_note_title"])
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("<th>모병원·행선</th>", html)
        self.assertNotIn("뇌손상 / 뇌경색", html)    # 병명과 중복되던 옛 메모는 더 이상 표에 없다

    def test_care_column_shows_items_written_in_the_consultation_as_is(self):
        """상태·처치 칸 — 상담일지에 적힌 항목만 적힌 표기 그대로. 앱이 중증도를 평가하지 않는다(2026-09-18 요청).

        근거가 두 곳이다. ① 체크박스(의식·활동·식사·상처소독·특수처치)
        ② 병명 상세 자유 기재 — 운영 자료는 거의 전부 여기에 있다(섬망·홈벤트·목관이 글로 적혀 있다).
        """
        with models.get_db() as conn:
            conn.execute("""UPDATE consultations SET special_care='["인공호흡기","산소요법","CRE"]', wound_care='["욕창","기관절개"]',
                            diet_types='["비강영양(L-tube)"]', activity_others='["와상"]', consciousness_main='반혼수', oxygen_lpm='3L'
                            WHERE id=1""")
            # 체크박스는 안 쓰고 자유 기재에만 적은 상담 — 운영에서 가장 흔한 형태
            conn.execute("""UPDATE consultations SET disease_detail='만성호흡부전.홈벤트 섬망 목관 CRE' WHERE id=3""")
        rows = {r["patient_id"]: r for r in models.dashboard_summary(d(-1), d(1))["admission_selected"]}
        care = rows[1]["admission_care"]
        # 등급·점수는 더 이상 없다
        self.assertNotIn("level", care)
        self.assertNotIn("score", care)
        # 체크한 항목이 폼 문구 그대로, 정해진 순서로
        self.assertEqual([t["label"] for t in care["tags"]],
                         ["반혼수", "와상", "비강영양(L-tube)", "기관절개", "인공호흡기", "산소요법", "욕창"])
        self.assertEqual(rows[1]["admission_organisms"], ["CRE"])   # 내성균은 빨간 배지로 따로
        self.assertIn("산소요법 — 3L", care["title"])                # 인라인 수기는 툴팁에
        # 자유 기재에 적은 것도 적힌 표기 그대로 — '홈벤트'로 적었으면 홈벤트
        self.assertEqual([t["label"] for t in rows[3]["admission_care"]["tags"]],
                         ["섬망", "목관", "홈벤트"])
        self.assertEqual(rows[3]["admission_organisms"], ["CRE"])
        self.assertEqual(rows[2]["admission_care"]["tags"], [])      # 적힌 항목이 없으면 빈 칸
        self.assertEqual(rows[4]["admission_care"]["tags"], [])      # 복귀 행도 같은 구조
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("<th>상태·처치</th>", html)
        self.assertNotIn('class="sev sev-high"', html)               # 높음/중간 알약은 걷어냈다
        self.assertIn('>비강영양(L-tube)</i>', html)
        self.assertIn('>홈벤트</i>', html)
        self.assertLess(html.index("<th>주병명</th>"), html.index("<th>상태·처치</th>"))
        self.assertLess(html.index("<th>상태·처치</th>"), html.index("<th>발병일</th>"))

    def test_care_items_skip_words_that_only_look_like_care_items(self):
        """같은 글자가 진단명으로 쓰인 것은 항목이 아니다 — 실제 자료에서 확인한 것만 막는다.

        개발 DB의 '산소' 37건 중 대부분이 진단명 '저산소성 뇌손상'·'무산소증'이고, '흡인' 8건은
        전부 '흡인성폐렴'이었다. 이것들을 산소요법·석션으로 잡으면 병동이 헛준비를 한다.
        """
        def labels(detail):
            return [t["label"] for t in models.care_items({"disease_detail": detail})]
        self.assertEqual(labels("저산소성 뇌손상, 홈벤트"), ["홈벤트"])
        self.assertEqual(labels("뇌손상 / 무산소증"), [])
        self.assertEqual(labels("흡인성폐렴-VRE"), [])
        self.assertEqual(labels("발목관절 골절"), [])
        # 진짜 산소 사용·기관절개는 그대로 잡는다
        self.assertEqual(labels("COPD-가정산소사용자"), ["산소"])
        self.assertEqual(labels("뇌출혈, 반무의식,목관"), ["반무의식", "목관"])

    def test_care_items_ignore_values_that_need_no_preparation(self):
        """'정상' 의식·'스스로' 활동처럼 병동이 챙길 것 없는 값은 칩에 올리지 않는다."""
        self.assertEqual(models.care_items({"consciousness_main": "정상",
                                            "activity_others": ["에어매트리스 안내"],
                                            "diet_types": ["밥"]}),
                         [{"key": "airmat", "label": "에어매트리스 안내", "title": "에어매트리스 안내"}])

    def test_layout_a_ward_strip_and_referral_column(self):
        """A안: 병동별 재원은 KPI 아래 띠 + 전체 펼치기, 입원 환자 현황은 전체 폭에 유입경로·소개자 열."""
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="ward-strip"', html)
        self.assertNotIn('ward-detail-toggle', html)      # 펼치기/접기는 뺐다 — 칩에 다 들어 있다
        self.assertLess(html.index('id="ward-strip"'), html.index('id="admission-schedule"'))   # 띠가 표 위에
        self.assertIn("<th>유입경로·소개자</th>", html)
        self.assertIn("소개 (지인추천)", html)
        self.assertIn("홍길동 · 안동병원 사회사업실", html)

    def test_editing_admission_date_after_away_closes_the_away_event(self):
        """오경자 님(2026-09-17): 8/29 응급전원 중 → 9/16 재입원을 상담 실제입원일 정정으로만 기록.
        외진 기록이 열린 채 남아 '복귀예정 9/17'이 따로 보였다 → 입원일이 외진 이후면 그날 복귀로 자동 닫는다."""
        with models.get_db() as conn:
            conn.execute("INSERT INTO patients (id,name,gender) VALUES (9,'오경자','F')")
            conn.execute("""INSERT INTO consultations (id, patient_id, consult_date, admission_status, actual_admission_date,
                            attending_doctor, room_number, patient_age)
                            VALUES (9, 9, ?, '입원완료', ?, 'RM1 이성범 부장', '510호', 74)""", (d(-100), d(-60)))
            conn.execute("""INSERT INTO admission_events (consultation_id, event_type, event_date, hospital, expected_return_date)
                            VALUES (9, '응급전원', ?, '안동병원', ?)""", (d(-20), d(1)))
        models.sync_admission_episode(9)
        before = [r for r in models.dashboard_summary(d(-1), d(1))["admission_schedule"] if r["patient_id"] == 9]
        self.assertEqual([(r["admission_kind"], r["admission_bucket"], r["admission_display_date"]) for r in before],
                         [("return", "planned", d(1))])
        # 상담 상세에서 실제입원일만 오늘로 정정
        r = self.client.post("/api/consult/9", json={"consultation": {"actual_admission_date": d(0), "admission_date": d(0)}})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIsNone(models.open_away_event(9), "외진 기록이 그날 복귀로 자동으로 닫혀야 한다")
        rows = [r for r in models.dashboard_summary(d(-1), d(1))["admission_schedule"] if r["patient_id"] == 9]
        self.assertEqual([(r["admission_kind"], r["admission_bucket"], r["admission_display_date"]) for r in rows],
                         [("return", "completed", d(0))])   # 복귀 9/16 한 줄, 9/17 예정 행 없음

    def test_stale_planned_return_row_is_hidden_when_readmitted(self):
        """저장 훅 이전에 들어온 데이터 — 외진 기록은 열려 있어도 외진 이후 입원완료 행이 있으면 복귀예정 행은 숨긴다."""
        with models.get_db() as conn:
            conn.execute("INSERT INTO patients (id,name,gender) VALUES (10,'옛데이터','F')")
            conn.execute("""INSERT INTO consultations (id, patient_id, consult_date, admission_status, actual_admission_date,
                            attending_doctor, room_number, patient_age)
                            VALUES (10, 10, ?, '입원완료', ?, 'RM1 이성범 부장', '511호', 70)""", (d(-100), d(0)))
            conn.execute("""INSERT INTO admission_events (consultation_id, event_type, event_date, hospital, expected_return_date)
                            VALUES (10, '응급전원', ?, '안동병원', ?)""", (d(-20), d(1)))
        rows = [r for r in models.dashboard_summary(d(-1), d(1))["admission_schedule"] if r["patient_id"] == 10]
        self.assertEqual([(r.get("admission_kind"), r["admission_bucket"]) for r in rows], [(None, "completed")])

    def test_week_in_counts_return_and_consultation_once(self):
        with main.app.test_request_context():
            strip = main._ward_status_strip()
        # 오늘완료(2) + 장광진복귀(4) = 2. 복귀와 같은 날 CRM 입원완료를 겹쳐 세지 않는다
        self.assertEqual(strip["week_in"], 2)


if __name__ == "__main__":
    unittest.main()
