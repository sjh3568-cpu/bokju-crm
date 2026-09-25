"""명부 적재 — 하루치 완전 스냅샷이면 "없는 사람은 퇴원자" (2026-09-26 사용자 결정).

원무 명부는 '그날 재원자' 스냅샷으로 올라온다(파일명 `입퇴재원환자현황(20260919-20260919).xlsx`).
그 파일에 없는 열린 명부 회차는 그날 이전에 퇴원한 것이다. 예전 적재기는 "행이 없다고 퇴원으로 칠 수
없다(부분 파일이면 나머지 전원이 퇴원 처리된다)"며 손대지 않았고, 그래서 9/17~18 퇴원자 6명이 재원에 남았다.

여기서 고정하는 것:
① 파일명이 하루치(시작=끝)일 때만 스냅샷이다. 기간 파일·날짜 없는 파일은 아니다.
② 부분 파일 가드 — 행 수가 열린 명부 회차의 SNAPSHOT_MIN_RATIO 미만이면 건너뛰고 이유를 적는다.
③ 없는 사람의 퇴원일: CRM이 아는 날(상담 퇴원완료일·미복귀 외진 출발일)이 있으면 그 날, 없으면 스냅샷
   날짜에 '추정' 표시. 스냅샷 이후에 입원한 회차는 명부가 아직 모를 뿐이라 건드리지 않는다.
④ 미리보기(dry-run)는 닫힐 명단만 보여주고 DB를 바꾸지 않는다. 체크박스를 끄면 아무것도 안 한다.
⑤ 이어지는 백필도 같은 스냅샷 날짜로 돌아 상담까지 퇴원완료가 된다.
"""
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import openpyxl

import models
import tools.import_admission_roster as roster_tool
from tools.import_admission_roster import run as roster_run, snapshot_date_from_name

HEADERS = ["차트번호", "수진자명", "주민번호", "성별/나이", "입원일", "퇴원일", "총일수", "환자유형",
           "진료의사", "병동", "병실", "주소", "발병일", "주상병", "주상병명칭", "의사성명",
           "재활시작일자", "재활대상구분"]


def d(n):
    return (date.today() + timedelta(days=n)).isoformat()


def ymd(iso):
    return iso.replace("-", "")


class SnapshotNameTests(unittest.TestCase):
    def test_single_day_range_is_a_snapshot(self):
        self.assertEqual(snapshot_date_from_name("x/입퇴재원환자현황(20260919-20260919).xlsx"), date(2026, 9, 19))

    def test_period_or_no_date_is_not(self):
        self.assertIsNone(snapshot_date_from_name("입퇴재원환자현황(20230701-20260916) (1).xlsx"))
        self.assertIsNone(snapshot_date_from_name("입퇴재원환자현황(테스트).xlsx"))


class RosterSnapshotAbsentTests(unittest.TestCase):
    SNAP = d(-2)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "snap.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        # 환자 1: 파일에 있음(재원 그대로). 2: 파일에 없고 기록도 없음 → 스냅샷 날짜(추정).
        # 3: 파일에 없고 상담이 퇴원완료 d-4 → 그 날. 4: 파일에 없지만 스냅샷 뒤(d-1) 입원 → 그대로.
        # 5: 파일에 없고 미복귀 외진(d-3 출발) → 출발일.
        people = {1: ("파일에있음", d(-30)), 2: ("기록없음", d(-30)), 3: ("상담퇴원", d(-30)),
                  4: ("스냅샷뒤입원", d(-1)), 5: ("외진중", d(-30))}
        with models.get_db() as conn:
            for pid, (name, adm) in people.items():
                conn.execute("INSERT INTO patients (id,name,gender,chart_no) VALUES (?,?,'F',?)", (pid, name, f"{pid:010d}"))
                conn.execute("""INSERT INTO consultations (id,patient_id,consult_date,admission_status,actual_admission_date)
                                VALUES (?,?,?,'입원완료',?)""", (pid, pid, d(-40), adm))
                conn.execute("""INSERT INTO admission_episodes (patient_id, episode_no, status, admitted_at, room_number, ward, roster_key)
                                VALUES (?,1,'admitted',?, '301호','3병동', ?)""", (pid, adm, f"{pid:010d}|{adm}"))
            conn.execute("UPDATE consultations SET admission_status='퇴원완료', discharge_date=? WHERE id=3", (d(-4),))
            conn.execute("INSERT INTO admission_events (consultation_id, event_type, event_date, hospital) VALUES (5,'응급전원',?, '안동병원')", (d(-3),))
        self.fname = f"입퇴재원환자현황({ymd(self.SNAP)}-{ymd(self.SNAP)}).xlsx"
        self.path = os.path.join(self.tmp.name, self.fname)
        wb = openpyxl.Workbook(); ws = wb.active
        ws.append(HEADERS)
        ws.append(["0000000001", "파일에있음", "500101-2000000", "여/76세", d(-30), "", "30", "건강보험",
                   "재활의학과1", "3병동", "301호", "경북 안동시", d(-60), "I639^00", "뇌경색증", "이성범", d(-30), "1.뇌"])
        wb.save(self.path); wb.close()

    def _episode(self, pid):
        with models.get_db() as conn:
            r = conn.execute("SELECT discharged_at, discharge_reason FROM admission_episodes WHERE patient_id=?", (pid,)).fetchone()
        return (r["discharged_at"] or "")[:10] or None, r["discharge_reason"]

    def test_partial_file_guard_skips_absent_processing(self):
        """기본 가드(0.9): 행 1개 vs 열린 회차 4개(스냅샷 이전) → 부분 파일로 보고 건너뛴다.
        (close_discharged=False — '상담 퇴원완료 → 회차 닫기'라는 다른 규칙과 섞이지 않게)"""
        lines = []
        summary = roster_run(self.path, apply=True, out=lines.append, close_discharged=False)
        self.assertEqual(summary["absent"], 0)
        self.assertTrue(any("부분 파일" in ln for ln in lines), "\n".join(lines))
        for pid in (2, 3, 5):
            self.assertEqual(self._episode(pid)[0], None)

    def test_dryrun_lists_but_does_not_touch(self):
        with patch.object(roster_tool, "SNAPSHOT_MIN_RATIO", 0.1):
            lines = []
            summary = roster_run(self.path, apply=False, out=lines.append, close_discharged=True)
        self.assertEqual(summary["absent"], 3)                    # 2·3·5 (4는 스냅샷 뒤 입원)
        text = "\n".join(lines)
        self.assertIn("기록없음", text); self.assertIn("추정", text)
        self.assertIn("상담퇴원", text); self.assertIn(d(-4), text)
        self.assertIn("외진중", text); self.assertIn(d(-3), text)
        self.assertNotIn("스냅샷뒤입원", text)
        for pid in (2, 3, 4, 5):
            self.assertEqual(self._episode(pid)[0], None, "미리보기는 DB를 바꾸지 않는다")

    def test_apply_closes_absent_with_the_right_dates(self):
        with patch.object(roster_tool, "SNAPSHOT_MIN_RATIO", 0.1):
            summary = roster_run(self.path, apply=True, out=lambda s: None, close_discharged=True)
        self.assertEqual(summary["absent"], 3)
        self.assertEqual(self._episode(1)[0], None, "파일에 있는 사람은 그대로 재원")
        self.assertEqual(self._episode(4)[0], None, "스냅샷 뒤 입원은 명부가 아직 모를 뿐")
        dis2, why2 = self._episode(2)
        self.assertEqual(dis2, self.SNAP); self.assertIn("추정", why2 or "")
        self.assertEqual(self._episode(3)[0], d(-4), "상담 퇴원완료일이 있으면 그 날")
        self.assertEqual(self._episode(5)[0], d(-3), "미복귀 외진은 출발일")
        # 재원 판정도 같이 — 1·4만 남는다
        self.assertEqual(models.current_admission_census()["patients"], {1, 4})
        # ⑤ 상담까지 퇴원완료 — 2번은 회차와 같은 추정 날짜에 '추정' 사유가 따라온다, 3번은 이미 적힌 값 보존
        c2 = models.get_consultation(2)
        self.assertEqual((c2["admission_status"], c2["discharge_date"]), ("퇴원완료", self.SNAP))
        self.assertIn("추정", c2["discharge_reason"] or "")
        self.assertEqual(models.get_consultation(3)["discharge_date"], d(-4))
        self.assertEqual(models.get_consultation(4)["admission_status"], "입원완료")

    def test_checkbox_off_does_nothing(self):
        with patch.object(roster_tool, "SNAPSHOT_MIN_RATIO", 0.1):
            summary = roster_run(self.path, apply=True, out=lambda s: None, close_discharged=False,
                                 absent_discharge=False)
        self.assertEqual(summary["absent"], 0)
        for pid in (2, 3, 5):
            self.assertEqual(self._episode(pid)[0], None)

    def test_idempotent(self):
        with patch.object(roster_tool, "SNAPSHOT_MIN_RATIO", 0.1):
            roster_run(self.path, apply=True, out=lambda s: None, close_discharged=True)
            again = roster_run(self.path, apply=True, out=lambda s: None, close_discharged=True)
        self.assertEqual(again["absent"], 0)


if __name__ == "__main__":
    unittest.main()
