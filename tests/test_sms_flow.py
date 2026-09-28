"""문자 안전 발송·시점별 템플릿·깜빡 방지 알림 (2026-09-28 사용자 요청).
설계: docs/work/2026-09-28-문자-안전발송-알림.md"""
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import app as main
import config
import models
import sms

TODAY = date.today().isoformat()
def d(n): return (date.today() + timedelta(days=n)).isoformat()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "sms.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        models.ensure_admin_user("sms-flow", "test-password", display_name="점검")
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        self.client.post("/login", data={"username": "sms-flow", "password": "test-password"})

    def patient(self, name="홍길동", phone="010-1111-2222"):
        conn = models.get_db()
        pid = conn.execute("INSERT INTO patients (name, guardian_name, guardian_phone) VALUES (?,?,?)",
                           (name, "보호자", phone)).lastrowid
        conn.commit(); conn.close()
        return pid

    def preselect(self, url):
        import json, re
        html = self.client.get(url).get_data(as_text=True)
        return json.loads(re.search(r"window\.SMS_PRESELECT = (.*?);\n", html).group(1))

    def consult(self, pid, **fields):
        fields.setdefault("consult_date", TODAY)
        return models.create_consultation(patient_id=pid, **fields)


class DataTests(Base):
    def test_timing_column_and_seed_mapping(self):
        rows = {t["name"]: t["timing"] for t in models.list_sms_templates(active_only=False)}
        self.assertEqual(rows["입원 예정 안내"], "입원 전날")
        self.assertEqual(rows["상담 감사 안내"], "상담 직후")
        tid = models.create_sms_template(name="자유", body="본문")
        self.assertEqual(models.get_sms_template(tid)["timing"], "수시")
        models.update_sms_template(tid, timing="퇴원 후")
        self.assertEqual([t["id"] for t in models.list_sms_templates(timing="퇴원 후")], [tid])

    def test_user_templates_default_to_anytime(self):
        # 운영에서 직접 만든 템플릿은 추측하지 않고 '수시'
        conn = models.get_db()
        conn.execute("INSERT INTO sms_templates (name, template_group, body) VALUES ('직접','공통','x')")
        conn.execute("DELETE FROM app_meta WHERE key='sms_template_timing_v1'")
        conn.commit(); conn.close()
        models.init_db()
        self.assertEqual(models.list_sms_templates(timing="수시")[0]["name"], "직접")

    def test_log_sms_keeps_reminder_key(self):
        sid = models.log_sms(to_phone="01011112222", body="x", status="phone", reminder_key="상담 직후:c1")
        self.assertEqual(models.list_sms_log(1)[0]["reminder_key"], "상담 직후:c1")
        self.assertTrue(sid)

    def test_consult_disease_groups(self):
        self.assertEqual(models.consult_disease_groups({"diseases": ["뇌경색", "당뇨"]}), ["기저질환", "중추신경계"])
        self.assertEqual(models.consult_disease_groups({"diseases": '["단일부위"]'}), ["근골격계"])
        self.assertEqual(models.consult_disease_groups({"diseases": None}), [])



class RecipientTests(Base):
    def test_recipient_from_latest_consult(self):
        pid = self.patient()
        self.consult(pid, consult_date=d(-3), diseases=["당뇨"])
        cid = self.consult(pid, consult_date=d(-1), diseases=["뇌경색"], planned_admission_date=d(2),
                           attending_doctor="김의사")
        r = self.client.get(f"/api/sms/recipient?pid={pid}").get_json()
        self.assertEqual((r["consultation_id"], r["guardian_phone"], r["planned"], r["doctor"]),
                         (cid, "010-1111-2222", d(2), "김의사"))
        self.assertEqual(r["disease_groups"], ["중추신경계"])

    def test_recipient_by_consult_id_and_404(self):
        pid = self.patient(); cid = self.consult(pid)
        self.assertEqual(self.client.get(f"/api/sms/recipient?cid={cid}").get_json()["patient_id"], pid)
        self.assertEqual(self.client.get("/api/sms/recipient?pid=999999").status_code, 404)

    def test_page_has_search_not_200_dropdown(self):
        html = self.client.get("/sms").get_data(as_text=True)
        self.assertIn('id="sms-search"', html)
        self.assertNotIn('id="sms-consult"', html)

    def test_page_preselects_old_consult(self):
        # 최근 200건 밖이어도 cid로 들어오면 채워진다
        pid = self.patient(name="오래된환자"); cid = self.consult(pid, consult_date="2020-01-01")
        self.assertEqual(self.preselect(f"/sms?cid={cid}")["patient_name"], "오래된환자")


if __name__ == "__main__":
    unittest.main()
