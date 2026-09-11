import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

import models
from tools import backfill_from_roster as bf


class PickAdmissionTests(unittest.TestCase):
    def test_prefers_nearest_after_consult(self):
        c = date(2026, 3, 1)
        self.assertEqual(bf.pick_admission(c, [date(2026, 1, 5), date(2026, 3, 9), date(2026, 5, 1)]),
                         date(2026, 3, 9))

    def test_falls_back_to_recent_before(self):
        c = date(2026, 3, 1)
        self.assertEqual(bf.pick_admission(c, [date(2026, 2, 25)]), date(2026, 2, 25))
        self.assertIsNone(bf.pick_admission(c, [date(2026, 1, 25)]))       # 14일 넘게 전

    def test_ignores_far_future(self):
        self.assertIsNone(bf.pick_admission(date(2026, 3, 1), [date(2026, 12, 1)]))


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 'test.db'))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        self.conn = models.get_db()
        self.addCleanup(self.conn.close)
        c = self.conn
        c.execute("INSERT INTO patients (id, name, gender) VALUES (1, '가', 'F')")
        c.execute("INSERT INTO patients (id, name, gender, insurance_type) VALUES (2, '나', 'M', '자보')")
        c.execute("INSERT INTO patients (id, name, gender) VALUES (3, '다', 'M')")
        # 환자1: 두 회차, 최신은 의료급여. 상담은 두 번째 입원 직전.
        c.execute("INSERT INTO admission_episodes (patient_id, episode_no, status, admitted_at, insurance_type, roster_key) "
                  "VALUES (1, 1, 'discharged', '2025-01-10', '건강보험', 'A|2025-01-10')")
        c.execute("INSERT INTO admission_episodes (patient_id, episode_no, status, admitted_at, insurance_type, roster_key) "
                  "VALUES (1, 2, 'admitted', '2026-03-09', '의료급여', 'A|2026-03-09')")
        c.execute("INSERT INTO consultations (id, patient_id, consult_date, admission_status) "
                  "VALUES (10, 1, '2026-03-01', '입원완료')")
        c.execute("INSERT INTO admission_episodes (patient_id, consultation_id, episode_no, status) "
                  "VALUES (1, 10, 3, 'planned')")
        # 환자2: 보험이 손으로 입력돼 있다 → 보존. 상담 날짜는 이미 있음 → 손대지 않음.
        c.execute("INSERT INTO admission_episodes (patient_id, episode_no, status, admitted_at, insurance_type, roster_key) "
                  "VALUES (2, 1, 'admitted', '2026-02-01', '건강보험', 'B|2026-02-01')")
        c.execute("INSERT INTO consultations (id, patient_id, consult_date, admission_status, actual_admission_date) "
                  "VALUES (20, 2, '2026-01-20', '입원완료', '2026-02-01')")
        # 환자3: 명부 없음 → 못 채움
        c.execute("INSERT INTO consultations (id, patient_id, consult_date, admission_status) "
                  "VALUES (30, 3, '2026-01-20', '입원완료')")
        # 환자4: 상태 미정인데 명부엔 상담 5일 뒤 입원 → 승격 대상. 환자5: 60일 뒤 → 보류.
        c.execute("INSERT INTO patients (id, name, gender) VALUES (4, '라', 'F')")
        c.execute("INSERT INTO patients (id, name, gender) VALUES (5, '마', 'F')")
        c.execute("INSERT INTO admission_episodes (patient_id, episode_no, status, admitted_at, insurance_type, roster_key) "
                  "VALUES (4, 1, 'admitted', '2026-04-05', '건강보험', 'D|2026-04-05')")
        c.execute("INSERT INTO consultations (id, patient_id, consult_date) VALUES (40, 4, '2026-04-01')")
        c.execute("INSERT INTO admission_episodes (patient_id, episode_no, status, admitted_at, insurance_type, roster_key) "
                  "VALUES (5, 1, 'admitted', '2026-06-01', '건강보험', 'E|2026-06-01')")
        c.execute("INSERT INTO consultations (id, patient_id, consult_date) VALUES (50, 5, '2026-04-01')")
        c.commit()

    def test_dry_run_changes_nothing(self):
        bf.run(self.conn, apply=False, quiet=True)
        self.assertIsNone(self.conn.execute("SELECT insurance_type FROM patients WHERE id=1").fetchone()[0])
        self.assertIsNone(self.conn.execute("SELECT actual_admission_date FROM consultations WHERE id=10").fetchone()[0])

    def test_apply_fills_and_is_idempotent(self):
        ins, adm, pro = bf.run(self.conn, apply=True, quiet=True)
        self.assertIsNone(pro)
        self.assertEqual(ins["stats"]["채움"], 3)
        self.assertEqual(ins["stats"]["값 있어 보존"], 1)
        self.assertEqual(adm["stats"]["채움"], 1)
        self.assertEqual(adm["stats"]["명부 회차 없음"], 1)
        q = lambda s: self.conn.execute(s).fetchone()
        self.assertEqual(q("SELECT insurance_type FROM patients WHERE id=1")[0], "의료급여")   # 최신 회차
        self.assertEqual(q("SELECT insurance_type FROM patients WHERE id=2")[0], "자보")
        self.assertEqual(q("SELECT actual_admission_date FROM consultations WHERE id=10")[0], "2026-03-09")
        self.assertEqual(tuple(q("SELECT admitted_at, status FROM admission_episodes WHERE consultation_id=10")),
                         ("2026-03-09", "admitted"))
        self.assertIsNone(q("SELECT actual_admission_date FROM consultations WHERE id=30")[0])
        ins2, adm2, _ = bf.run(self.conn, apply=True, quiet=True)
        self.assertEqual(ins2["stats"]["이미 같음"], 3)
        self.assertEqual(adm2["targets"], 1)   # 환자3만 남는다
        # 승격 없이 돌렸으니 미정 상담은 그대로
        self.assertIsNone(q("SELECT admission_status FROM consultations WHERE id=40")[0])

    def test_promote_undecided(self):
        _, _, pro = bf.run(self.conn, apply=True, promote=True, quiet=True)
        self.assertEqual(pro["stats"]["입원완료로 승격"], 1)
        self.assertEqual(pro["stats"]["30일 넘어 입원 — 보류"], 1)
        q = lambda s: self.conn.execute(s).fetchone()
        self.assertEqual(tuple(q("SELECT admission_status, actual_admission_date FROM consultations WHERE id=40")),
                         ("입원완료", "2026-04-05"))
        self.assertIsNone(q("SELECT admission_status FROM consultations WHERE id=50")[0])
        _, _, pro2 = bf.run(self.conn, apply=True, promote=True, quiet=True)
        self.assertEqual(pro2["stats"]["입원완료로 승격"], 0)   # 멱등

    def test_overwrite_insurance(self):
        bf.run(self.conn, apply=True, overwrite_insurance=True, quiet=True)
        self.assertEqual(self.conn.execute("SELECT insurance_type FROM patients WHERE id=2").fetchone()[0], "건강보험")


if __name__ == '__main__':
    unittest.main()
