"""원무 명부 → CRM 환자 매칭 규칙 테스트.

여기서 지키는 것은 '어느 단서를 먼저 믿는가'다. 상담 나이는 만나이·세는나이가
섞이고 오기까지 있어 역산 생년이 흔들리는 반면, 상담에 적힌 입원일·상담일은
원무 명부와 같은 사실을 가리킨다. 실제 명부 1,600행을 돌려 보니 생년을 먼저
믿는 순서가 몇 년 전 상담에 입원을 갖다 붙이고 있었다.
"""
import sqlite3
import unittest
from datetime import date

from tools.import_admission_roster import Matcher


def rec(chart, name, *, admitted, birth_year=None, gender=None):
    return {"chart_no": chart, "name": name, "admitted_at": admitted,
            "birth_year": birth_year, "gender": gender}


class MatcherTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE patients (id INTEGER PRIMARY KEY, name TEXT, gender TEXT, chart_no TEXT);
            CREATE TABLE consultations (
                patient_id INTEGER, consult_date TEXT, patient_age INTEGER,
                admission_date TEXT, actual_admission_date TEXT);
        """)

    def patient(self, pid, name, gender="M", chart=None, consults=()):
        self.conn.execute("INSERT INTO patients (id,name,gender,chart_no) VALUES (?,?,?,?)",
                          (pid, name, gender, chart))
        for consult_date, age, admitted in consults:
            self.conn.execute(
                "INSERT INTO consultations (patient_id,consult_date,patient_age,"
                "actual_admission_date) VALUES (?,?,?,?)", (pid, consult_date, age, admitted))

    def match(self, r):
        return Matcher(self.conn).match(r)

    def test_chart_number_wins_over_everything(self):
        self.patient(1, "김철수", chart="0001", consults=[("2024-01-01", 60, None)])
        self.patient(2, "김철수", consults=[("2024-03-01", 70, "2024-03-10")])
        pid, why = self.match(rec("0001", "김철수", admitted=date(2024, 3, 10),
                                  birth_year=1954, gender="M"))
        self.assertEqual((pid, why), (1, "차트번호"))

    def test_unique_name_and_gender_narrowing(self):
        self.patient(1, "박영희", gender="F", consults=[("2024-01-01", 70, None)])
        self.assertEqual(self.match(rec("9", "박영희", admitted=date(2024, 2, 1)))[1], "이름 유일")
        self.patient(2, "박영희", gender="M", consults=[("2024-01-01", 70, None)])
        pid, why = self.match(rec("9", "박영희", admitted=date(2024, 2, 1),
                                  birth_year=1954, gender="M"))
        self.assertEqual((pid, why), (2, "이름+성별"))

    def test_admission_date_beats_birth_year(self):
        """박원호 패턴 — 생년은 다른 쪽이 ±1로 맞지만, 명부 입원일이 적힌 상담이 정답."""
        self.patient(1, "박원호", consults=[("2023-11-24", 64, None)])          # 역산 1959
        self.patient(2, "박원호", consults=[("2023-12-29", 65, "2024-01-22")])  # 역산 1958
        pid, why = self.match(rec("9", "박원호", admitted=date(2024, 1, 22),
                                  birth_year=1960, gender="M"))
        self.assertEqual((pid, why), (2, "이름+입원일 일치"))

    def test_grossly_different_birth_year_excluded_despite_same_admission_date(self):
        """이영애 패턴 — 입원일이 겹쳐도 생년이 9년 차이면 남남이다."""
        self.patient(1, "이영애", gender="F", consults=[("2024-04-22", 86, "2024-04-15")])  # 1938
        self.patient(2, "이영애", gender="F", consults=[("2024-04-08", 78, None)])          # 1946
        pid, why = self.match(rec("9", "이영애", admitted=date(2024, 4, 15),
                                  birth_year=1947, gender="F"))
        self.assertEqual(pid, 2)
        self.assertIn("근접", why)

    def test_recent_consult_beats_stale_birth_year_match(self):
        """권분남 패턴 — 생년 ±1로는 옛 상담이 걸리지만 입원 직전 상담이 우선."""
        self.patient(1, "권분남", gender="F", consults=[("2025-10-08", 83, None)])  # 역산 1942
        self.patient(2, "권분남", gender="F", consults=[("2026-03-07", 83, None)])  # 역산 1943
        pid, why = self.match(rec("9", "권분남", admitted=date(2025, 10, 16),
                                  birth_year=1944, gender="F"))
        self.assertEqual((pid, why), (1, "이름+상담일 근접(90일)"))

    def test_absurd_age_typo_does_not_poison_birth_year(self):
        """김선애 패턴 — 나이가 678로 적혀 역산 생년이 1347이 되던 건."""
        self.patient(1, "김선애", gender="F", consults=[("2025-05-16", 678, None)])
        self.patient(2, "김선애", gender="F", consults=[("2020-01-01", 62, None)])
        matcher = Matcher(self.conn)
        self.assertEqual(matcher.birth_years[1], set())      # 1347년은 버려진다
        pid, why = matcher.match(rec("9", "김선애", admitted=date(2025, 6, 2),
                                     birth_year=1958, gender="F"))
        self.assertEqual((pid, why), (1, "이름+상담일 근접(90일)"))

    def test_namesakes_only_reported_as_absent_not_ambiguous(self):
        """김동욱 패턴 — 생년도 안 맞고 입원 전 상담도 없으면 CRM에 없는 사람이다.

        '확정 불가'로 묶으면 이 환자의 입원 이력이 통째로 누락된다.
        """
        self.patient(1, "김동욱", consults=[("2025-08-13", 38, None)])   # 1987
        self.patient(2, "김동욱", consults=[("2025-09-05", 57, None)])   # 1968
        pid, why = self.match(rec("9", "김동욱", admitted=date(2024, 3, 29),
                                  birth_year=1960, gender="M"))
        self.assertIsNone(pid)
        self.assertEqual(why, "동명이인뿐 — CRM에 기록 없음")

    def test_genuinely_ambiguous_stays_held(self):
        """생년이 둘 다 맞고 둘 다 입원 직전에 상담했으면 도구가 고르면 안 된다."""
        self.patient(1, "손상원", consults=[("2023-11-29", 81, None)])
        self.patient(2, "손상원", consults=[("2023-11-28", 81, None)])
        pid, why = self.match(rec("9", "손상원", admitted=date(2023, 12, 12),
                                  birth_year=1942, gender="M"))
        self.assertIsNone(pid)
        self.assertEqual(why, "동명이인 2명 중 확정 불가")

    def test_date_evidence_ranks_chart_collisions(self):
        """차트번호 둘이 한 환자를 가리킬 때 어느 쪽이 진짜인지 가르는 점수.

        김선애 패턴 — CRM에 그 이름이 하나뿐이라 두 차트가 모두 붙는다.
        입원 직전에 상담한 쪽이 이 환자이고, 다른 쪽은 CRM에 없는 동명이인이다.
        """
        self.patient(1, "김선애", gender="F", consults=[("2025-05-16", 67, None)])
        matcher = Matcher(self.conn)
        mine = rec("A", "김선애", admitted=date(2025, 6, 2), birth_year=1958, gender="F")
        other = rec("B", "김선애", admitted=date(2025, 12, 12), birth_year=1943, gender="F")
        self.assertEqual(matcher.date_evidence(mine, 1), 1)
        self.assertEqual(matcher.date_evidence(other, 1), 0)

    def test_admission_date_outranks_mere_proximity(self):
        self.patient(1, "정수현", consults=[("2024-01-02", 70, "2024-01-20")])
        matcher = Matcher(self.conn)
        exact = rec("A", "정수현", admitted=date(2024, 1, 20), birth_year=1954, gender="M")
        near = rec("B", "정수현", admitted=date(2024, 2, 1), birth_year=1954, gender="M")
        self.assertEqual(matcher.date_evidence(exact, 1), 2)
        self.assertEqual(matcher.date_evidence(near, 1), 1)


if __name__ == "__main__":
    unittest.main()
