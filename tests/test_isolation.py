"""격리 상태 — 입원 시 보균(상담일지)과 현재 격리(해제·재격리 이벤트)를 분리 (2026-09-17, 오경자 님 VRE 해제)."""
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


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "iso.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("iso-test", "test-password", display_name="점검")
        self.uid = models.get_user("iso-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="iso-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        with models.get_db() as conn:
            conn.execute("INSERT INTO patients (id,name,gender) VALUES (1,'오경자','F')")
            # 입원 시 VRE 보균 — 특수관리 항목에 기록. 오늘 입원완료(대시보드 행·재원 명단 모두에 나온다)
            conn.execute("""INSERT INTO consultations (id, patient_id, consult_date, admission_status, actual_admission_date,
                            attending_doctor, room_number, patient_age, special_care, primary_diagnosis)
                            VALUES (1, 1, ?, '입원완료', ?, 'RM1 이성범 부장', '510호', 74, '["VRE"]', '뇌출혈')""", (d(-30), d(0)))
            conn.execute("""INSERT INTO admission_episodes (patient_id, consultation_id, episode_no, status, admitted_at, room_number, ward, roster_key)
                            VALUES (1, 1, 1, 'admitted', ?, '510호', '5병동', 'c1|x')""", (d(0),))

    def _dash_row(self):
        return [r for r in models.dashboard_summary(d(-1), d(1))["admission_schedule"] if r["id"] == 1][0]

    def _ward_html(self, **q):
        qs = "&".join(f"{k}={v}" for k, v in q.items())
        return self.client.get("/ward?partial=roster&view=list" + ("&" + qs if qs else "")).get_data(as_text=True)

    def test_detected_from_consultation_and_cleared_by_event(self):
        self.assertEqual(models.detected_organisms(models.get_consultation(1)), ["VRE"])
        self.assertEqual(self._dash_row()["admission_organisms"], ["VRE"])
        html = self._ward_html()
        self.assertIn('class="wd-org"', html); self.assertIn('wd-iso-clear', html)
        # 해제 — 감염병동 공유 근거를 메모로
        r = self.client.post("/api/consult/1/isolation", json={
            "organism": "vre", "status": "해제", "event_date": d(0), "note": "감염병동 카톡·아마란스 9/16 공유"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["state"]["VRE"]["status"], "해제")
        row = self._dash_row()
        self.assertEqual(row["admission_organisms"], [])                       # 빨간 배지 없음
        self.assertEqual([x["organism"] for x in row["admission_organisms_cleared"]], ["VRE"])
        html = self._ward_html()
        self.assertIn("VRE 해제", html); self.assertIn("wd-iso-redetect", html)
        self.assertNotIn('class="wd-org"', html.replace('class="wd-org off"', ''))   # 빨간 배지 없음
        # '균 보유' 필터에서도 빠진다
        self.assertNotIn("오경자", self._ward_html(organism="1"))
        # 상담일지의 입원 시 보균 기록은 그대로
        self.assertEqual(models.get_consultation(1)["special_care"], ["VRE"])
        # 재검출 → 다시 격리
        r = self.client.post("/api/consult/1/isolation", json={"organism": "VRE", "status": "검출", "event_date": d(0)})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self._dash_row()["admission_organisms"], ["VRE"])
        self.assertIn("오경자", self._ward_html(organism="1"))

    def test_detail_page_shows_state_and_history(self):
        self.client.post("/api/consult/1/isolation", json={"organism": "VRE", "status": "해제", "event_date": d(-1), "note": "아마란스"})
        html = self.client.get("/consult/1").get_data(as_text=True)
        self.assertIn("격리 상태", html)
        self.assertIn(f"VRE 해제 {d(-1)}", html)
        self.assertIn('data-status="검출"', html)     # 재격리 버튼
        self.assertIn("아마란스", html)

    def test_validation(self):
        bad = [({"organism": "ABC", "status": "해제", "event_date": d(0)}, "균 종류"),
               ({"organism": "VRE", "status": "완치", "event_date": d(0)}, "상태"),
               ({"organism": "VRE", "status": "해제", "event_date": d(1)}, "미래"),
               ({"organism": "VRE", "status": "해제", "event_date": "2026-13-01"}, "형식")]
        for body, msg in bad:
            r = self.client.post("/api/consult/1/isolation", json=body)
            self.assertEqual(r.status_code, 400, body); self.assertIn(msg, r.get_json()["error"])
        self.assertEqual(self.client.post("/api/consult/999/isolation", json={"organism": "VRE", "status": "해제", "event_date": d(0)}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
