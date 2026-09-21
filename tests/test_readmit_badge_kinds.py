"""재입원 배지를 둘로 — '외진 복귀 · N일'과 '재입원 · 퇴원 후 N일' (2026-09-21 요청).

원무 명부는 외진 나간 날 퇴원, 복귀한 날 새 회차로 적어서 외진 복귀도 회차만 보면 재입원이다.
배지에 이유를 적어 마우스를 올리지 않아도 갈리게 한다. 재원 명단(목록)과 침상 카드 둘 다.
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


def d(n):
    return (date.today() + timedelta(days=n)).isoformat()


class ReadmitBadgeKindTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "rb.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("rb-test", "test-password", display_name="점검")
        self.uid = models.get_user("rb-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="rb-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        with models.get_db() as conn:
            for pid, name in ((1, "외진복귀자"), (2, "진짜재입원")):
                conn.execute("INSERT INTO patients (id,name,gender) VALUES (?,?,'M')", (pid, name))
                conn.execute("""INSERT INTO consultations (id,patient_id,consult_date,admission_status,
                                actual_admission_date,room_number,attending_doctor)
                                VALUES (?,?,?,'입원완료',?,'301호','RM1 이성범 부장')""", (pid, pid, d(-100), d(-6)))
                # 이번 회차(열림) + 지난 회차(닫힘) — 명부가 둘로 나눠 적은 모양
                conn.execute("""INSERT INTO admission_episodes (patient_id,episode_no,status,admitted_at,room_number,ward,roster_key)
                                VALUES (?,2,'admitted',?,'301호','3병동',?)""", (pid, d(-6), f"c{pid}|{d(-6)}"))
            # 1) 외진 복귀 — 9일 전 나가서 6일 전 복귀, 명부는 그 사이를 퇴원으로 적었다
            conn.execute("""INSERT INTO admission_episodes (patient_id,episode_no,status,admitted_at,discharged_at,room_number,ward,roster_key)
                            VALUES (1,1,'discharged',?,?,'301호','3병동',?)""", (d(-40), d(-9), f"c1|{d(-40)}"))
            conn.execute("""INSERT INTO admission_events (consultation_id,event_type,event_date,hospital,returned_at)
                            VALUES (1,'응급전원',?,'안동병원',?)""", (d(-9), d(-6)))
            # 2) 진짜 재입원 — 20일 전 퇴원, 외진 기록 없음
            conn.execute("""INSERT INTO admission_episodes (patient_id,episode_no,status,admitted_at,discharged_at,room_number,ward,roster_key)
                            VALUES (2,1,'discharged',?,?,'204호','2병동',?)""", (d(-60), d(-20), f"c2|{d(-60)}"))

    def test_helpers_split_the_two_cases(self):
        returns = models.away_return_for_readmission([1, 2])
        self.assertEqual(sorted(returns), [1])
        hit = models.match_away_return(returns[1], d(-6))
        self.assertEqual((hit["hospital"], hit["days"]), ("안동병원", 3))
        self.assertIsNone(models.match_away_return(returns[1], d(-30)))    # 복귀일과 먼 입원엔 안 붙는다

    def test_list_and_bed_card_show_the_reason_in_the_badge(self):
        for view in ("list", "room"):
            html = self.client.get(f"/ward?partial=roster&view={view}").get_data(as_text=True)
            self.assertIn('wd-readmit wd-readmit-away', html, view)
            self.assertIn('>외진 복귀 · 3일</span>', html, view)
            self.assertIn('>재입원 · 퇴원 후 14일</span>', html, view)
            self.assertNotIn('>재입원</span>', html, view)          # 이유 없는 옛 배지는 더 없다
            self.assertIn("안동병원", html, view)                   # 외진 복귀 툴팁에 병원


if __name__ == "__main__":
    unittest.main()
