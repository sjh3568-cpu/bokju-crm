# -*- coding: utf-8 -*-
"""퇴원 취소 — 잘못 누른 퇴원을 되돌려 다시 재원으로 (2026-09-19 사용자 요청).

유순여 님은 9월 21일 퇴원 예정인데 9월 19일 퇴원으로 처리됐다. 상태를 '입원완료'로
되돌리는 것만으로는 재원 명단에 안 돌아온다 — 상담의 퇴원일과 닫힌 입원 회차가 남아
재원 판정이 그 둘을 다 보기 때문이다. 그래서 셋을 함께 되돌린다.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

import app as main
import models
import partnerships
import support_requests

CHART = "0000002421"
OLD_ADM, OLD_OUT = "2025-12-31", "2026-02-27"     # 지난 입원 — 건드리면 안 된다
ADM, WRONG_OUT, DUE = "2026-07-24", "2026-09-19", "2026-09-21"


class CancelDischargeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "cancel.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("cancel-test", "test-password", display_name="점검")
        self.uid = models.get_user("cancel-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="cancel-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        with models.get_db() as conn:
            conn.execute("INSERT INTO patients (id,name,gender,chart_no) VALUES (1,'유순여','F',?)", (CHART,))
            conn.execute("""INSERT INTO consultations (id,patient_id,consult_date,admission_status,
                            actual_admission_date,discharge_date,discharge_destination,discharge_reason,room_number)
                            VALUES (1,1,'2025-12-20','퇴원완료',?,?,'자택','치료 종료','305호')""", (ADM, WRONG_OUT))
            # 지난 입원 — 명부가 제대로 닫은 회차
            conn.execute("""INSERT INTO admission_episodes (patient_id,episode_no,status,admitted_at,
                            discharged_at,roster_key) VALUES (1,1,'discharged',?,?,?)""",
                         (OLD_ADM, OLD_OUT, f"{CHART}|{OLD_ADM}"))
            # 이번 입원 — 잘못 누른 퇴원으로 명부 회차와 CRM 회차가 함께 닫혔다
            conn.execute("""INSERT INTO admission_episodes (patient_id,episode_no,status,admitted_at,
                            discharged_at,room_number,ward,roster_key,discharge_edited)
                            VALUES (1,3,'discharged',?,?,'305호','3병동',?,1)""",
                         (ADM, WRONG_OUT, f"{CHART}|{ADM}"))
            conn.execute("""INSERT INTO admission_episodes (patient_id,consultation_id,episode_no,status,
                            admitted_at,discharged_at) VALUES (1,1,2,'discharged',?,?)""", (ADM, WRONG_OUT))

    def _cancel(self, **extra):
        return self.client.post("/api/consult/1/discharge", json={"action": "cancel", **extra})

    def _episodes(self):
        with models.get_db() as conn:
            return {r["id"]: dict(r) for r in conn.execute(
                "SELECT id, roster_key, admitted_at, discharged_at, status, discharge_edited "
                "FROM admission_episodes ORDER BY id")}

    def test_cancel_brings_the_patient_back_to_the_census(self):
        self.assertNotIn(1, models.current_admission_census()["patients"])   # 잘못 잡힌 상태
        r = self._cancel()
        self.assertEqual(r.status_code, 200, r.get_json())
        con = models.get_consultation(1)
        self.assertEqual(con["admission_status"], "입원완료")
        self.assertFalse(con["discharge_date"])
        self.assertFalse(con["discharge_destination"])
        self.assertIn(1, models.current_admission_census()["patients"])

    def test_only_the_wrong_discharge_is_reopened(self):
        self._cancel()
        eps = list(self._episodes().values())
        old = [e for e in eps if str(e["admitted_at"])[:10] == OLD_ADM][0]
        now = [e for e in eps if str(e["admitted_at"])[:10] == ADM]
        self.assertEqual(old["discharged_at"], OLD_OUT, "지난 입원은 그대로 닫혀 있어야 한다")
        self.assertEqual(len(now), 2)
        for e in now:
            self.assertIsNone(e["discharged_at"])
            self.assertEqual(e["status"], "admitted")
            # 취소는 'CRM이 틀렸다'는 뜻 — 다음 명부 적재가 실제 퇴원일을 가져올 수 있어야 한다
            self.assertEqual(e["discharge_edited"], 0)

    def test_cancel_can_record_the_planned_discharge_date(self):
        r = self._cancel(discharge_due_date=DUE)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(models.get_consultation(1)["discharge_due_date"], DUE)
        self.assertEqual(r.get_json()["cancelled_date"], WRONG_OUT)

    def test_rejects_non_discharged_and_bad_due_date(self):
        self.assertEqual(self._cancel(discharge_due_date="2026-13-40").status_code, 400)
        self._cancel()
        again = self._cancel()
        self.assertEqual(again.status_code, 400)
        self.assertIn("퇴원완료", again.get_json()["error"])

    def test_detail_offers_the_cancel_button(self):
        """되돌리는 자리는 환자를 여는 상담 상세다 — 퇴원한 환자는 재원 명단에 없다(2026-09-19 요청)."""
        html = self.client.get("/consult/1").get_data(as_text=True)
        self.assertIn('id="dc-undo"', html)
        self.assertIn("퇴원 취소", html)
        self.assertIn("/api/consult/${cid}/discharge", html)
        # 상담목록에서는 뺐다 — 목록의 [날짜 수정]은 그대로
        lst = self.client.get("/consultations").get_data(as_text=True)
        self.assertNotIn('data-act="cancel"', lst)
        self.assertIn('data-act="fix-date"', lst)

    def test_list_script_keeps_multiline_messages_escaped(self):
        """안내 문구의 줄바꿈은 \\n이어야 한다 — 생 줄바꿈이 들어가 스크립트가 통째로 죽었었다.

        '날짜 수정' 안내문에 줄바꿈이 그대로 들어가 SyntaxError가 나면서 상담목록 인라인
        스크립트의 모든 기능(상태 변경·퇴원 버튼·행 선택)이 멈췄다. 같은 실수를 막는다.
        """
        html = self.client.get("/consultations").get_data(as_text=True)
        self.assertIn("(YYYY-MM-DD)\\n여기 값이", html)


if __name__ == "__main__":
    unittest.main()
