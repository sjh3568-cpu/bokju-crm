# -*- coding: utf-8 -*-
"""대시보드 '이번달 입원 · 전환율' — 입원은 입원일 기준, 전환율은 상담일 기준 (2026-10-02 사용자 결정).

지난달에 상담하고 이번달에 입원한 환자는
  · 이번달 '입원'(admitted)에는 들어가고
  · 이번달 '전환율'(rate)의 분모·분자에는 들어가지 않으며 (지난달 상담 코호트의 성사로 잡힌다)
  · 두 기준의 차이를 설명하는 '지난달 상담→이번달 입원'(carry)에 센다.
"""
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

import dashboard_metrics
import models

TODAY = date(2026, 10, 15)          # 이번달 10/1~10/15, 지난달 같은 기간 9/1~9/15, 지난달 코호트 9/1~9/30


class MonthPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "month.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        with models.get_db() as conn:
            for pid in range(1, 8):
                conn.execute("INSERT INTO patients (id,name,gender) VALUES (?,?,'F')", (pid, f"환자{pid}"))
            rows = [
                # id, 상담일,        상태,      실제 입원일
                (1, "2026-09-20", "입원완료", "2026-10-03"),   # 지난달 상담 → 이번달 입원 (carry)
                (2, "2026-10-02", "입원완료", "2026-10-05"),   # 이번달 상담 → 이번달 입원
                (3, "2026-10-04", "입원예정", None),           # 이번달 상담, 미확정
                (4, "2026-10-06", "입원취소", None),           # 이번달 상담, 취소 (미확정 아님)
                (5, "2026-09-05", "입원완료", "2026-09-10"),   # 지난달 같은 기간 상담·입원
                (6, "2026-09-25", "입원취소", None),           # 지난달 코호트(같은 기간 밖) 취소
            ]
            for cid, cd, status, adm in rows:
                conn.execute("INSERT INTO consultations (id, patient_id, consult_date, patient_age, "
                             "admission_status, actual_admission_date) VALUES (?,?,?,70,?,?)",
                             (cid, cid, cd, status, adm))
            # 7번: 상담 기록 없이 원무 명부에만 있는 이번달 입원 — 입원일 기준에는 센다
            conn.execute("""INSERT INTO admission_episodes
                (patient_id, episode_no, status, admitted_at, discharged_at, room_number, ward, roster_key)
                VALUES (7, 1, 'admitted', '2026-10-08', NULL, '301호', '3병동', 'c7|2026-10-08')""")

    def test_admitted_is_by_admission_date(self):
        mk = dashboard_metrics.month_performance(TODAY)
        # 1(지난달 상담), 2(이번달 상담), 7(명부만) — 모두 이번달 입원
        self.assertEqual(mk["admitted"], 3)
        self.assertEqual(mk["prev_admitted"], 1)          # 5번: 9/10 입원
        self.assertEqual(mk["delta_admitted"], 2)
        self.assertEqual(sum(mk["spark_admitted"]), 3)
        self.assertEqual(len(mk["spark_admitted"]), 15)
        self.assertEqual(mk["admitted_return"], 0)

    def test_rate_is_consult_cohort(self):
        mk = dashboard_metrics.month_performance(TODAY)
        # 이번달 상담 = 2, 3, 4 → 성사 1건(2번) → 33.33%. 1번은 지난달 상담이라 분자에도 분모에도 없다.
        self.assertEqual(mk["total"], 3)
        self.assertEqual(mk["done"], 1)
        self.assertEqual(mk["rate"], 33.33)
        self.assertEqual(mk["open"], 1)                   # 3번(입원예정)만 미확정, 취소는 확정
        # 상담일 평균 = 상담 ÷ 실제 상담이 있던 날 (10/2, 10/4, 10/6 = 3일) — /stats·월간 보고서와 같은 정의
        self.assertEqual(mk["active_days"], 3)
        self.assertEqual(mk["active_day_avg"], 1.0)
        self.assertEqual(mk["prev"]["active_days"], 1)
        # 지난달 같은 기간(9/1~9/15) = 5번 1건 성사 → 100%
        self.assertEqual(mk["prev"]["total"], 1)
        self.assertEqual(mk["prev"]["rate"], 100.0)

    def test_prev_cohort_and_carry(self):
        mk = dashboard_metrics.month_performance(TODAY)
        # 지난달 전체 상담 코호트 = 1, 5, 6 → 성사 2건 → 66.67%, 미확정 0
        self.assertEqual(mk["prev_cohort"]["total"], 3)
        self.assertEqual(mk["prev_cohort"]["done"], 2)
        self.assertEqual(mk["prev_cohort"]["rate"], 66.67)
        self.assertEqual(mk["prev_cohort"]["open"], 0)
        self.assertEqual(mk["prev_cohort"]["label"], "9월")
        # 지난달 상담 → 이번달 입원 = 1번만
        self.assertEqual(mk["carry"], 1)

    def test_new_admissions_excludes_returns(self):
        """외진 복귀(is_return)는 새 입원이 아니다 — '현재 재원' 카드의 이번달 입과는 복귀 수만큼 다르다."""
        with patch.object(models, "admission_flow_events", return_value=[
            {"kind": models.ADMISSION_EVENT_IN, "date": "2026-10-02", "patient_id": 1, "is_return": False},
            {"kind": models.ADMISSION_EVENT_IN, "date": "2026-10-02", "patient_id": 2, "is_return": True},
            {"kind": models.ADMISSION_EVENT_OUT, "date": "2026-10-03", "patient_id": 3, "is_return": False},
        ]):
            r = models.new_admissions("2026-10-01", "2026-10-15")
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["returns"], 1)
        self.assertEqual(r["by_date"], {"2026-10-02": 1})


if __name__ == "__main__":
    unittest.main()
