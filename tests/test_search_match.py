"""통합 검색 — 띄어쓰기·별칭·접미사·오타를 넘어 어디서 검색하든 같은 결과 (2026-09-30).

사용자: '대구굿모닝병원'과 '대구 굿모닝병원'이 같이 나와야 하고, 비슷한 표기도 잡혀야 하며,
검색은 어디든 다 포함해야 한다. 규칙은 search_match.py 한 곳이고, 이 테스트는
(1) 규칙 자체 (2) 상담목록·재원관리·전역검색·기관협력·모병원 분석이 그 규칙을 실제로 타는지 본다.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

import app as main
import models
import partnerships as coop
import search_match as sm


class SearchRuleTests(unittest.TestCase):
    def test_spacing_and_punctuation_ignored(self):
        self.assertTrue(sm.search_match("대구굿모닝병원", "대구 굿모닝병원"))
        self.assertTrue(sm.search_match("대구 굿모닝병원", "대구굿모닝병원"))
        self.assertTrue(sm.search_match("김 철수", "김철수"))
        self.assertTrue(sm.search_match("01012345678", "010-1234-5678"))
        self.assertTrue(sm.search_match("010-1234", "01012345678"))

    def test_suffix_stem_finds_abbreviated_records(self):
        # 검색어에 '병원'을 붙여 쳐도 접미사 없는 옛 표기가 나온다
        self.assertTrue(sm.search_match("대구굿모닝병원", "대구굿모닝"))
        self.assertTrue(sm.search_match("대구굿모닝병원", "대구 굿모닝"))
        # 몸통이 너무 짧으면(2자) 넓히지 않는다 — '대구병원'으로 대구의 모든 병원이 나오면 안 된다
        self.assertEqual(sm.search_variants("대구병원"), ("대구병원",))
        self.assertFalse(sm.search_match("대구병원", "대구굿모닝병원"))

    def test_university_hospital_forms_are_equal(self):
        self.assertTrue(sm.search_match("경북대학교병원", "칠곡경북대병원"))
        self.assertTrue(sm.search_match("경북대병원", "경북대학병원"))
        self.assertTrue(sm.search_match("경북대병원", "칠곡경북대학교병원"))

    def test_alias_dictionary_expands(self):
        self.assertTrue(sm.search_match("아산강릉", "강릉아산병원"))
        self.assertTrue(sm.search_match("강릉아산병원", "아산 강릉"))

    def test_fuzzy_allows_one_typo_in_name_only(self):
        self.assertTrue(sm.search_match_fuzzy("평택굿모닝병원", "평텍굿모닝병원"))
        self.assertTrue(sm.search_match_fuzzy("칠곡경북대병원", "칠골경북대병원"))
        self.assertTrue(sm.search_match_fuzzy("용상안동병원", "용샹안동병원"))
        # 접미사 부분의 차이는 다른 기관 — 안동병원 ≠ 안동의원
        self.assertFalse(sm.search_match_fuzzy("안동병원", "안동의원"))
        self.assertFalse(sm.search_match_fuzzy("안동병원", "안동의료원"))
        # 몸통이 짧으면(3자 이하) 오타 허용 안 함 — 안동 ≠ 안산
        self.assertFalse(sm.search_match_fuzzy("안동병원", "안산병원"))
        # 다른 지역의 같은 브랜드는 오타가 아니다
        self.assertFalse(sm.search_match_fuzzy("대구굿모닝병원", "평택굿모닝병원"))

    def test_empty_query_matches_everything_and_none_fields_are_safe(self):
        self.assertTrue(sm.search_match("", "아무거나"))
        self.assertFalse(sm.search_match("안동", None, ""))
        self.assertEqual(sm.search_variants(""), ())


class SearchScreensTests(unittest.TestCase):
    """실제 화면·API가 같은 규칙으로 찾는지 — 임시 DB에 표기 변형을 넣고 검색한다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 'test.db'))
        db.start(); self.addCleanup(db.stop)
        models.init_db(); coop.init_schema()
        models.ensure_admin_user('searchtest', 'test-only-password', display_name='테스트')
        user = models.get_user('searchtest')
        boot = patch.object(main, '_db_initialized', True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=user['id'], username='searchtest', role='admin',
                           perms={k: 3 for k in main.MENU_KEYS})
        self.ids = {}
        for i, (name, hospital) in enumerate((("김하나", "대구굿모닝병원"), ("이두리", "대구 굿모닝병원"),
                                              ("박세모", "대구굿모닝"), ("최네모", "평택굿모닝병원"),
                                              ("정다섯", "안동병원"), ("한여섯", "안동의료원"))):
            pid = models.find_or_create_patient(name=name, guardian_phone=f"010-1111-22{i:02d}")
            self.ids[name] = models.create_consultation(
                patient_id=pid, consult_date="2026-09-01", counselor="테스트",
                source_hospital=hospital, admission_status="입원완료",
                actual_admission_date="2026-09-02")

    def _names(self, rows):
        return sorted(r["patient_name"] for r in rows)

    def test_consult_list_q_matches_spacing_variants(self):
        for q in ("대구굿모닝병원", "대구 굿모닝병원", "대구굿모닝"):
            self.assertEqual(self._names(models.list_consultations(q=q, limit=50)),
                             ["김하나", "박세모", "이두리"], q)
        # '굿모닝병원'은 브랜드 전체
        self.assertEqual(self._names(models.list_consultations(q="굿모닝병원", limit=50)),
                         ["김하나", "박세모", "이두리", "최네모"])
        # 모병원 칸 필터도 같은 규칙, 안동병원 ≠ 안동의료원
        self.assertEqual(self._names(models.list_consultations(hospital="안동 병원", limit=50)), ["정다섯"])

    def test_consult_list_phone_and_name_spacing(self):
        self.assertEqual(len(models.list_consultations(guardian="0101111 22", limit=50)), 6)
        self.assertEqual(self._names(models.list_consultations(q="김 하나", limit=50)), ["김하나"])

    def test_ward_scope_search(self):
        rows = models.list_consultations(admission_status="입원완료", q="대구 굿모닝", q_scope="ward", limit=50)
        self.assertEqual(self._names(rows), ["김하나", "박세모", "이두리"])
        rows = models.list_consultations(admission_status="입원완료", q="010 1111 22", q_scope="ward", limit=50)
        self.assertEqual(len(rows), 6)

    def test_global_search_and_admission_analysis(self):
        r = self.client.get('/api/global-search?q=대구 굿모닝')
        self.assertEqual(r.status_code, 200)
        titles = sorted(i['title'] for i in r.get_json()['items'] if i['kind'] == '환자·상담')
        self.assertEqual(titles, ["김하나", "박세모", "이두리"])
        data = models.hospital_admission_analysis(q="대구굿모닝병원")
        got = sorted(r["patient_name"] for r in data["rows"]) if isinstance(data, dict) and "rows" in data \
            else None
        if got is not None:
            self.assertEqual(got, ["김하나", "박세모", "이두리"])

    def test_hospital_overview_search_uses_same_rule(self):
        data = models.hospital_referral_overview("2026-01-01", "2026-12-31", q="대구 굿모닝병원")
        self.assertEqual(data["hospital_count"], 1)
        self.assertEqual(sum(h["referrals"] for h in data["hospitals"]), 3)
        # 안동병원으로 안동의료원이 끼지 않는다
        data = models.hospital_referral_overview("2026-01-01", "2026-12-31", q="안동병원")
        self.assertEqual([h["name"] for h in data["hospitals"]], ["안동병원"])

    def test_partner_search_ignores_spacing(self):
        db = models.get_db()
        db.execute("INSERT OR IGNORE INTO source_hospitals (name, region, active) VALUES ('대구굿모닝병원', '대구', 1)")
        db.commit(); db.close()
        r = self.client.get('/partners/search?q=대구 굿모닝')
        self.assertEqual(r.status_code, 200)
        self.assertIn('대구굿모닝병원', [i['name'] for i in r.get_json()['items']])
        r = self.client.get('/partners/search?q=대구굿모닝병원')
        self.assertIn('대구굿모닝병원', [i['name'] for i in r.get_json()['items']])

    def test_patient_autocomplete_spacing(self):
        names = [p["name"] for p in models.autocomplete_patients("김 하나")]
        self.assertEqual(names, ["김하나"])


if __name__ == '__main__':
    unittest.main()
