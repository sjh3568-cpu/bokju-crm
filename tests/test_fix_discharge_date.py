"""퇴원일 수정 — CRM이 기준 (2026-09-19 사용자 결정).

"원무쪽 기록은 이제 생각하지 마라. 여기 자료를 바탕으로 봐야 한다 — 여기가 지속 업데이트되니까."
그래서 ① 이미 퇴원완료인 건도 날짜를 고칠 수 있고 ② 고치면 상담과 입원 회차가 함께 바뀌며
③ 명부를 다시 적재해도 그 퇴원일은 되돌아가지 않는다.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

import app as main
import models
import partnerships
import support_requests

ADM, OLD_OUT, NEW_OUT = "2026-07-21", "2026-09-15", "2026-09-16"


class FixDischargeDateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "fix.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("fix-test", "test-password", display_name="점검")
        self.uid = models.get_user("fix-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="fix-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        with models.get_db() as conn:
            conn.execute("INSERT INTO patients (id,name,gender,chart_no) VALUES (1,'권현수','F','0000000134')")
            conn.execute("""INSERT INTO consultations (id,patient_id,consult_date,admission_status,
                            actual_admission_date,discharge_date,room_number)
                            VALUES (1,1,'2026-07-10','퇴원완료',?,?,'309호')""", (ADM, OLD_OUT))
            conn.execute("""INSERT INTO admission_episodes (patient_id,episode_no,status,admitted_at,
                            discharged_at,room_number,ward,roster_key)
                            VALUES (1,2,'discharged',?,?,'309호','3병동',?)""",
                         (ADM, OLD_OUT, f"0000000134|{ADM}"))
            conn.execute("""INSERT INTO admission_episodes (patient_id,consultation_id,episode_no,status,
                            admitted_at,discharged_at) VALUES (1,1,1,'discharged',?,?)""", (ADM, OLD_OUT))

    def _fix(self, when):
        return self.client.post("/api/consult/1/discharge", json={"action": "fix-date", "discharge_date": when})

    def _episodes(self):
        with models.get_db() as conn:
            return {r["id"]: dict(r) for r in conn.execute(
                "SELECT id, roster_key, discharged_at, discharge_edited FROM admission_episodes")}

    def test_fix_updates_consultation_and_every_episode(self):
        r = self._fix(NEW_OUT)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(models.get_consultation(1)["discharge_date"], NEW_OUT)
        for ep in self._episodes().values():
            self.assertEqual(ep["discharged_at"], NEW_OUT)
            self.assertEqual(ep["discharge_edited"], 1)

    def test_roster_reimport_keeps_the_corrected_date(self):
        """명부를 다시 올려도 사람이 고친 퇴원일은 그대로 — 원무 값으로 되돌아가지 않는다."""
        self._fix(NEW_OUT)
        from datetime import date
        from tools.import_admission_roster import upsert_episode
        rec = {"chart_no": "0000000134", "admitted_at": date(2026, 7, 21),
               "discharged_at": date(2026, 9, 15), "room_number": "309호", "ward": "3병동",
               "attending_doctor": "김우근", "insurance_type": "건강보험",
               "diagnosis_code": "M6250^00", "diagnosis_name": "근육 소모", "care_type": "6.비사용증후군"}
        with models.get_db() as conn:
            action = upsert_episode(conn, 1, rec)
            conn.commit()
        self.assertEqual(action, "갱신(퇴원일 보존)")
        roster = [e for e in self._episodes().values() if e["roster_key"]][0]
        self.assertEqual(roster["discharged_at"], NEW_OUT)
        with models.get_db() as conn:   # 다른 값(주치의)은 명부가 갱신한다
            self.assertEqual(conn.execute(
                "SELECT attending_doctor FROM admission_episodes WHERE roster_key IS NOT NULL").fetchone()[0], "김우근")

    def test_rejects_bad_dates(self):
        from datetime import date, timedelta
        self.assertIn("미래", self._fix((date.today() + timedelta(days=1)).isoformat()).get_json()["error"])
        self.assertIn("입원일", self._fix("2026-07-01").get_json()["error"])
        self.assertEqual(self._fix("2026-13-40").status_code, 400)
        self.assertEqual(models.get_consultation(1)["discharge_date"], OLD_OUT)   # 그대로

    def test_list_shows_the_fix_button(self):
        html = self.client.get("/consultations").get_data(as_text=True)
        self.assertIn('data-act="fix-date"', html)
        self.assertIn("원무 명부를 다시 올려도", html)


if __name__ == "__main__":
    unittest.main()
