"""입원예정 필수값(예정일·주치의·병실) — 세 경로 모두 서버가 막는다 + 재입원 표시.

2026-09-16 사용자 규칙: 상담·재원 데이터는 제일 정확해야 한다. 입원예정 환자는
예정일·주치의·병실을 무조건 입력해야 하고, 재입원 환자는 기존 입원일과 신규
입원일을 다 파악할 수 있어야 한다.
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


class PlannedAdmissionRequiredTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "planned.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("planned-test", "test-password", display_name="점검")
        self.uid = models.get_user("planned-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="planned-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        self.today = date.today().isoformat()
        self.tomorrow = (date.today() + timedelta(days=1)).isoformat()

    # ── 경로 1: 상담일지 등록(/api/consult) ──
    def _create(self, **consult):
        return self.client.post("/api/consult", json={
            "patient": {"name": "예정환자", "gender": "F"},
            "consultation": {"consult_date": self.today, **consult}})

    def test_create_planned_without_room_is_rejected(self):
        r = self._create(admission_status="입원예정", planned_admission_date=self.tomorrow,
                         attending_doctor="RM1 이성범 부장")
        self.assertEqual(r.status_code, 400)
        self.assertIn("병실", r.get_json()["error"])
        self.assertNotIn("주치의", r.get_json()["error"].split("누락:")[1])

    def test_create_planned_with_all_three_is_accepted(self):
        r = self._create(admission_status="입원예정", planned_admission_date=self.tomorrow,
                         attending_doctor="RM1 이성범 부장", room_number="305호")
        self.assertEqual(r.status_code, 200, r.get_json())
        con = models.get_consultation(r.get_json()["id"])
        self.assertEqual((con["admission_status"], con["room_number"]), ("입원예정", "305호"))

    def test_create_without_planned_status_needs_nothing(self):
        r = self._create(admission_status="")
        self.assertEqual(r.status_code, 200, r.get_json())

    # ── 경로 2: 상담일지 수정(/api/consult/<id>) ──
    def test_update_to_planned_merges_with_existing_values(self):
        cid = self._create(attending_doctor="RM1 이성범 부장", room_number="305호").get_json()["id"]
        # 기존 상담에 주치의·병실이 있으면 예정일만 보내도 통과
        r = self.client.post(f"/api/consult/{cid}", json={"consultation": {
            "admission_status": "입원예정", "planned_admission_date": self.tomorrow}})
        self.assertEqual(r.status_code, 200, r.get_json())
        # 입원예정인 상담의 병실을 비우는 저장은 거부
        r = self.client.post(f"/api/consult/{cid}", json={"consultation": {"room_number": ""}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("병실", r.get_json()["error"])
        # 입원예정과 무관한 칸만 고치는 저장은 그대로 된다
        r = self.client.post(f"/api/consult/{cid}", json={"consultation": {"note": "메모"}})
        self.assertEqual(r.status_code, 200, r.get_json())

    # ── 경로 3: 상태 API(/api/consult/<id>/status — 대시보드·상담목록·상담상세) ──
    def test_status_api_requires_all_three(self):
        cid = self._create().get_json()["id"]
        r = self.client.post(f"/api/consult/{cid}/status", json={
            "admission_status": "입원예정", "planned_admission_date": self.tomorrow})
        self.assertEqual(r.status_code, 400)
        err = r.get_json()["error"]
        self.assertIn("주치의", err); self.assertIn("병실", err)
        r = self.client.post(f"/api/consult/{cid}/status", json={
            "admission_status": "입원예정", "planned_admission_date": self.tomorrow,
            "attending_doctor": "RM1 이성범 부장", "room_number": "305호"})
        self.assertEqual(r.status_code, 200, r.get_json())
        con = models.get_consultation(cid)
        self.assertEqual(con["attending_doctor"], "RM1 이성범 부장")

    def test_status_api_readmission_clears_stale_first_admission_date(self):
        """김한진 님 사례 — 8/25 입원·9/9 퇴원(명부)한 상담에 다시 입원예정을 잡으면 옛 실제입원일을 비운다."""
        first = (date.today() - timedelta(days=21)).isoformat()
        left = (date.today() - timedelta(days=7)).isoformat()
        cid = self._create(admission_status="입원완료", actual_admission_date=first,
                           attending_doctor="RM1 이성범 부장", room_number="204호").get_json()["id"]
        pid = models.get_consultation(cid)["patient_id"]
        with models.get_db() as conn:
            conn.execute("""INSERT INTO admission_episodes
                (patient_id, episode_no, status, admitted_at, discharged_at, room_number, ward, roster_key)
                VALUES (?, 9, 'discharged', ?, ?, '204호', '2병동', ?)""", (pid, first, left, f"c{pid}|{first}"))
        r = self.client.post(f"/api/consult/{cid}/status", json={
            "admission_status": "입원예정", "planned_admission_date": self.today,
            "planned_admission_time": "15:00", "attending_doctor": "IM2 신현범 과장", "room_number": "204호"})
        self.assertEqual(r.status_code, 200, r.get_json())
        con = models.get_consultation(cid)
        self.assertIsNone(con["actual_admission_date"])
        self.assertIsNone(con["admission_date"])
        # 대시보드 입원 환자 현황: 오늘 예정으로 잡히고 '재입원' 표시에 이전 입원·퇴원일이 붙는다
        rows = [x for x in models.dashboard_summary()["admission_schedule"] if x["id"] == cid]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["admission_bucket"], "planned")
        self.assertTrue(rows[0]["readmission"])
        self.assertEqual([(s["admitted_at"], s["discharged_at"], s["source"]) for s in rows[0]["prior_stays"]],
                         [(first, left, "명부")])
        # 완료 처리 → 오늘 입원완료로, 이전 입원 이력은 회차에 그대로 남는다
        r = self.client.post(f"/api/consult/{cid}/status", json={
            "admission_status": "입원완료", "admission_date": self.today})
        self.assertEqual(r.status_code, 200, r.get_json())
        eps = models.list_admission_episodes(pid)
        self.assertEqual(sorted((e["admitted_at"], e["discharged_at"]) for e in eps),
                         sorted([(first, left), (self.today, None)]))

    def test_dashboard_queue_lists_every_missing_field(self):
        # 서버 검증 이전에 들어온 옛 데이터(주치의·병실 없음)는 처리 큐에 누락 항목이 나열된다
        cid = self._create().get_json()["id"]
        with models.get_db() as conn:
            conn.execute("UPDATE consultations SET admission_status='입원예정', planned_admission_date=? WHERE id=?",
                         (self.tomorrow, cid))
        self.assertEqual(models.planned_admission_missing(models.get_consultation(cid)), ["주치의", "병실"])
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("주치의·병실 미지정", html)


if __name__ == "__main__":
    unittest.main()
