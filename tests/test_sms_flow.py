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



class GuardTests(Base):
    def test_unresolved_tokens(self):
        self.assertEqual(sms.unresolved_tokens("{환자명}님 {주치의} 안내"), ["{환자명}", "{주치의}"])
        self.assertEqual(sms.unresolved_tokens("홍길동님 안내"), [])

    def test_send_rejects_unresolved_token(self):
        with patch.object(sms, "send_sms") as send:
            r = self.client.post("/api/sms/send", json={"to_phone": "01011112222", "body": "{환자명}님 안내"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("{환자명}", r.get_json()["error"])
        send.assert_not_called()
        self.assertEqual(models.list_sms_log(5), [])

    def test_send_rejects_bad_phone(self):
        r = self.client.post("/api/sms/send", json={"to_phone": "054-550-1700", "body": "안내"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(models.list_sms_log(5), [])

    def test_page_has_confirm_dialog(self):
        html = self.client.get("/sms").get_data(as_text=True)
        self.assertIn('<dialog id="sms-confirm"', html)
        self.assertIn(">확인하고 보내기<", html)



class TemplateAxisTests(Base):
    def test_api_saves_timing_and_rejects_unknown(self):
        r = self.client.post("/api/sms/template", json={"name": "퇴원 안내", "body": "x",
                                                       "template_group": "공통", "timing": "퇴원 당일"})
        tid = r.get_json()["id"]
        self.assertEqual(models.get_sms_template(tid)["timing"], "퇴원 당일")
        self.client.post(f"/api/sms/template/{tid}", json={"timing": "없는시점"})
        self.assertEqual(models.get_sms_template(tid)["timing"], "수시")

    def test_compose_page_has_timing_tabs(self):
        html = self.client.get("/sms").get_data(as_text=True)
        for t in config.SMS_TIMINGS:
            self.assertIn(f'data-timing="{t}"', html)
        self.assertIn('id="sms-tpl-all"', html)

    def test_manage_page_has_both_selects(self):
        html = self.client.get("/sms/templates").get_data(as_text=True)
        self.assertIn('class="t-timing"', html)
        self.assertIn("대상 질환", html)



def flow(kind, day, pid, sources=("명부",), is_return=False, cid=None):
    return {"kind": kind, "date": day, "patient_id": pid, "consultation_id": cid, "patient_name": "홍길동",
            "sources": list(sources), "is_return": is_return}


def fake_flows(rows):
    return patch.object(models, "admission_flow_events",
                        side_effect=lambda a, b: [r for r in rows if a <= r["date"] <= b])


class ReminderTests(Base):
    def turn_on(self, *timings):
        # 시점 알림은 그 시점에 사용 중 템플릿이 있어야 켜진다(시드에는 상담 직후·입원 전날만 있다)
        for t in timings:
            models.create_sms_template(name=f"{t} 안내", body="x", timing=t)

    def keys(self, today=TODAY):
        return {r["key"] for r in models.sms_reminders(today)}

    def test_consult_followup_until_sent(self):
        pid = self.patient(); cid = self.consult(pid, consult_date=d(-2))
        key = f"상담 직후:c{cid}"
        self.assertIn(key, self.keys())
        models.log_sms(patient_id=pid, consultation_id=cid, to_phone="01011112222", body="x",
                       status="phone", reminder_key=key)
        self.assertNotIn(key, self.keys())

    def test_failed_or_unconfirmed_send_keeps_reminder(self):
        pid = self.patient(); cid = self.consult(pid)
        key = f"상담 직후:c{cid}"
        for st in ("failed", "manual"):
            models.log_sms(patient_id=pid, to_phone="01011112222", body="x", status=st, reminder_key=key)
        self.assertIn(key, self.keys())

    def test_consult_followup_window_and_cancel(self):
        pid = self.patient()
        old = self.consult(pid, consult_date=d(-config.SMS_CONSULT_FOLLOWUP_DAYS))
        edge = self.consult(pid, consult_date=d(-(config.SMS_CONSULT_FOLLOWUP_DAYS - 1)))
        cancel = self.consult(pid, consult_date=TODAY, consult_result="상담취소", consult_result_reason="타병원")
        keys = self.keys()
        self.assertNotIn(f"상담 직후:c{old}", keys)
        self.assertIn(f"상담 직후:c{edge}", keys)
        self.assertNotIn(f"상담 직후:c{cancel}", keys)

    def test_close_removes_and_requires_reason(self):
        pid = self.patient(); cid = self.consult(pid)
        key = f"상담 직후:c{cid}"
        self.assertEqual(self.client.post("/api/sms/reminder/close", json={"key": key, "reason": " "}).status_code, 400)
        r = self.client.post("/api/sms/reminder/close", json={"key": key, "reason": "전화로 안내함", "consultation_id": cid})
        self.assertTrue(r.get_json()["ok"])
        self.assertNotIn(key, self.keys())
        again = self.client.post("/api/sms/reminder/close", json={"key": key, "reason": "또"}).get_json()
        self.assertTrue(again["already"])

    def test_admission_tomorrow(self):
        pid = self.patient(); self.consult(pid, consult_date=d(-20), planned_admission_date=d(1))
        done = self.patient(name="이미입원"); self.consult(done, consult_date=d(-20), planned_admission_date=d(1),
                                                           admission_status="입원완료")
        keys = self.keys()
        self.assertIn(f"입원 전날:p{pid}:{d(1)}", keys)
        self.assertNotIn(f"입원 전날:p{done}:{d(1)}", keys)

    def test_flow_based_timings(self):
        pid = self.patient()
        n = config.SMS_AFTER_DISCHARGE_DAYS
        self.turn_on("입원 당일", "퇴원 당일", "퇴원 후")
        with fake_flows([flow("in", TODAY, pid), flow("out", TODAY, pid), flow("out", d(-n), pid, ("상담",))]):
            keys = self.keys()
        self.assertTrue({f"입원 당일:p{pid}:{TODAY}", f"퇴원 당일:p{pid}:{TODAY}", f"퇴원 후:p{pid}:{d(-n)}"} <= keys)

    def test_away_events_excluded(self):
        pid = self.patient()
        self.turn_on("입원 당일", "퇴원 당일")
        with fake_flows([flow("in", TODAY, pid, ("외진",), is_return=True), flow("out", TODAY, pid, ("외진",))]):
            keys = self.keys()
        self.assertFalse(any(k.startswith(("입원 당일", "퇴원 당일")) for k in keys))

    def test_same_patient_same_day_one_reminder(self):
        pid = self.patient()
        self.turn_on("입원 당일")
        with fake_flows([flow("in", TODAY, pid, ("명부", "상담"), cid=1), flow("in", TODAY, pid, ("CRM",))]):
            n = sum(1 for r in models.sms_reminders(TODAY) if r["timing"] == "입원 당일")
        self.assertEqual(n, 1)

    def test_timing_without_template_is_off(self):
        pid = self.patient(); cid = self.consult(pid)
        for t in models.list_sms_templates(timing="상담 직후"):
            models.update_sms_template(t["id"], active=0)
        self.assertNotIn(f"상담 직후:c{cid}", self.keys())
        with fake_flows([flow("in", TODAY, pid)]):   # 입원 당일 템플릿은 시드에 없다
            self.assertFalse(any(k.startswith("입원 당일") for k in self.keys()))

    def test_reminder_without_phone_flagged(self):
        pid = self.patient(phone=""); cid = self.consult(pid)
        r = [x for x in models.sms_reminders(TODAY) if x["key"] == f"상담 직후:c{cid}"][0]
        self.assertFalse(r["has_phone"])

    def test_compose_opens_with_reminder(self):
        import json, re
        pid = self.patient(); cid = self.consult(pid)
        html = self.client.get(f"/sms?reminder=상담 직후:c{cid}&cid={cid}").get_data(as_text=True)
        rem = json.loads(re.search(r"window\.SMS_REMINDER = (.*?);\n", html).group(1))
        self.assertEqual(rem, {"key": f"상담 직후:c{cid}", "timing": "상담 직후"})

    def test_send_records_reminder_key(self):
        pid = self.patient(); cid = self.consult(pid)
        key = f"상담 직후:c{cid}"
        with patch.dict(os.environ, {}, clear=True):
            self.client.post("/api/sms/send", json={"to_phone": "01011112222", "body": "안내", "reminder_key": key})
        self.assertEqual(models.list_sms_log(1)[0]["reminder_key"], key)



class DashboardReminderTests(Base):
    def setUp(self):
        super().setUp()
        import partnerships, support_requests, transport
        partnerships.init_schema(); support_requests.init_schema(); transport.init_schema()

    def test_queue_has_reminder_row(self):
        pid = self.patient(name="알림환자"); cid = self.consult(pid)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn(">안내 문자<", html)
        self.assertIn(f'href="/sms?reminder=상담 직후:c{cid}&amp;cid={cid}"', html)
        self.assertIn('sms-rm-close" data-key="상담 직후:c', html)

    def test_no_phone_row_links_patient(self):
        pid = self.patient(name="번호없음", phone=""); self.consult(pid)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn(f'href="/patients/{pid}" title="보호자 번호를 먼저 입력하세요">번호 없음<', html)

    def test_queue_builder_counts_reminders(self):
        items = [{"key": "상담 직후:c1", "timing": "상담 직후", "patient_id": 1, "consultation_id": 1,
                  "patient_name": "갑", "guardian_phone": "01011112222", "anchor_date": TODAY, "has_phone": True}]
        q = main._dashboard_action_queue({}, [], [], [], [], sms_reminders=items)
        self.assertEqual([x["kind"] for x in q["items_all"] if x["kind"] == "안내 문자"], ["안내 문자"])
        self.assertIn("안내 문자", [g["label"] for g in q["groups"]])



class ConsultDetailSmsTests(Base):
    def test_pending_then_sent(self):
        pid = self.patient(); cid = self.consult(pid)
        html = self.client.get(f"/consult/{cid}").get_data(as_text=True)
        self.assertIn("보호자 안내 문자", html)
        self.assertIn(f'href="/sms?reminder=상담 직후:c{cid}&amp;cid={cid}"', html)
        models.log_sms(patient_id=pid, consultation_id=cid, to_phone="01011112222", body="x",
                       status="phone", reminder_key=f"상담 직후:c{cid}")
        html = self.client.get(f"/consult/{cid}").get_data(as_text=True)
        self.assertIn("보낸 문자 1건", html)
        self.assertNotIn(f'href="/sms?reminder=상담 직후:c{cid}', html)

    def test_closed_reason_shown(self):
        pid = self.patient(); cid = self.consult(pid)
        models.close_sms_reminder(key=f"상담 직후:c{cid}", reason="전화로 안내함", closed_by="점검", consultation_id=cid)
        html = self.client.get(f"/consult/{cid}").get_data(as_text=True)
        self.assertIn("안 보냄 — 전화로 안내함", html)



class HandoffTests(Base):
    def test_qr_svg(self):
        r = self.client.post("/api/sms/qr", json={"to_phone": "010-1111-2222", "body": "안녕하세요"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/svg+xml")
        self.assertIn(b"<svg", r.data)
        self.assertIn("no-store", r.headers.get("Cache-Control", ""))

    def test_qr_encodes_smsto(self):
        import segno
        with patch.object(segno, "make", wraps=segno.make) as make:
            self.client.post("/api/sms/qr", json={"to_phone": "010-1111-2222", "body": "첫줄\n둘째줄"})
        self.assertEqual(make.call_args.args[0], "SMSTO:01011112222:첫줄\n둘째줄")

    def test_qr_refuses_unresolved_and_bad_phone(self):
        self.assertEqual(self.client.post("/api/sms/qr", json={"to_phone": "01011112222", "body": "{환자명}"}).status_code, 400)
        self.assertEqual(self.client.post("/api/sms/qr", json={"to_phone": "0545501700", "body": "안내"}).status_code, 400)

    def test_phone_confirm_logged_as_phone(self):
        with patch.dict(os.environ, {}, clear=True):
            r = self.client.post("/api/sms/send", json={"to_phone": "01011112222", "body": "안내", "mode": "phone"})
        self.assertEqual(r.get_json()["status"], "phone")
        self.assertEqual(models.list_sms_log(1)[0]["status"], "phone")

    def test_phone_mode_ignored_when_gateway_ready(self):
        env = {"SMS_PROVIDER": "aligo", "SMS_API_KEY": "k", "SMS_SENDER": "0545501700"}
        fake = {"ok": True, "status": "sent", "msg_type": "SMS", "error": None, "provider_msg_id": "1", "sent_to": "01011112222"}
        with patch.dict(os.environ, env, clear=True), patch.object(sms, "send_sms", return_value=fake) as send:
            r = self.client.post("/api/sms/send", json={"to_phone": "01011112222", "body": "안내", "mode": "phone"})
        send.assert_called_once()
        self.assertEqual(r.get_json()["status"], "sent")

    def test_history_labels(self):
        models.log_sms(to_phone="01011112222", body="a", status="phone")
        models.log_sms(to_phone="01011112222", body="b", status="manual")
        html = self.client.get("/sms").get_data(as_text=True)
        self.assertIn(">휴대폰<", html)
        self.assertIn(">수동(확인 안 됨)<", html)



class FinalReviewFixTests(Base):
    """최종 검토에서 나온 문제의 재현 테스트 (2026-09-29)."""

    def roster_episode(self, pid, admitted, discharged=None, key=None):
        with models.get_db() as conn:
            no = conn.execute("SELECT COALESCE(MAX(episode_no), 0) + 1 FROM admission_episodes WHERE patient_id=?",
                              (pid,)).fetchone()[0]
            conn.execute("""INSERT INTO admission_episodes (patient_id, episode_no, status, admitted_at, discharged_at, roster_key)
                            VALUES (?, ?, ?, ?, ?, ?)""",
                         (pid, no, "discharged" if discharged else "admitted", admitted, discharged, key or f"k{pid}|{admitted}"))

    def away(self, cid, out_day, returned=None):
        with models.get_db() as conn:
            conn.execute("""INSERT INTO admission_events (consultation_id, event_type, event_date, hospital, returned_at)
                            VALUES (?, '응급전원', ?, '안동병원', ?)""", (cid, out_day, returned))

    def test_i1_departure_yesterday_roster_discharge_today_is_not_discharge_reminder(self):
        # 앱은 어제 응급전원 외진, 명부는 오늘 퇴원으로 적음 → '퇴원 당일' 안내 대상이 아니다
        models.create_sms_template(name="퇴원", body="x", timing="퇴원 당일")
        pid = self.patient()
        cid = self.consult(pid, consult_date=d(-60), admission_status="입원완료", actual_admission_date=d(-50))
        self.roster_episode(pid, d(-50), TODAY)
        self.away(cid, d(-1))
        self.assertFalse([r for r in models.sms_reminders(TODAY) if r["timing"] == "퇴원 당일"])

    def test_i1_return_yesterday_roster_opens_today_is_not_admission_reminder(self):
        # 어제 외진 복귀, 명부는 오늘 새 회차 → '입원 당일'(입원 안내) 대상이 아니다
        models.create_sms_template(name="입원", body="x", timing="입원 당일")
        pid = self.patient()
        cid = self.consult(pid, consult_date=d(-60), admission_status="입원완료", actual_admission_date=d(-50))
        self.roster_episode(pid, d(-50), d(-5), key="a")
        self.away(cid, d(-5), returned=d(-1))
        self.roster_episode(pid, TODAY, key="b")
        self.assertFalse([r for r in models.sms_reminders(TODAY) if r["timing"] == "입원 당일"])

    def test_i1_plain_roster_discharge_still_reminds(self):
        models.create_sms_template(name="퇴원", body="x", timing="퇴원 당일")
        pid = self.patient()
        self.consult(pid, consult_date=d(-60), admission_status="입원완료", actual_admission_date=d(-50))
        self.roster_episode(pid, d(-50), TODAY)
        self.assertEqual([r["timing"] for r in models.sms_reminders(TODAY) if r["patient_id"] == pid], ["퇴원 당일"])

    def test_m1_hold_or_cancelled_not_admission_tomorrow(self):
        hold = self.patient(name="보류"); self.consult(hold, consult_date=d(-9), planned_admission_date=d(1),
                                                      admission_status="입원보류", admission_status_reason="병상")
        canc = self.patient(name="취소"); self.consult(canc, consult_date=d(-9), planned_admission_date=d(1),
                                                      consult_result="상담취소", consult_result_reason="타병원")
        keys = {r["key"] for r in models.sms_reminders(TODAY)}
        self.assertNotIn(f"입원 전날:p{hold}:{d(1)}", keys)
        self.assertNotIn(f"입원 전날:p{canc}:{d(1)}", keys)

    def test_m7_admission_tomorrow_links_the_planned_consult(self):
        pid = self.patient()
        planned = self.consult(pid, consult_date=d(-9), planned_admission_date=d(1))
        self.consult(pid, consult_date=d(-8))   # 더 최근 상담 — 입원예정일 없음
        item = [r for r in models.sms_reminders(TODAY) if r["timing"] == "입원 전날"][0]
        q = main._dashboard_action_queue({}, [], [], [], [], sms_reminders=[item])
        href = [x["href"] for x in q["items_all"] if x["kind"] == "안내 문자"][0]
        self.assertIn(f"cid={planned}", href)

    def test_m5_bad_anchor_date_does_not_break_queue(self):
        item = {"key": "상담 직후:c1", "timing": "상담 직후", "patient_id": 1, "consultation_id": 1,
                "patient_name": "갑", "guardian_phone": "01011112222", "anchor_date": "2026-9-3 오전", "has_phone": True}
        q = main._dashboard_action_queue({}, [], [], [], [], sms_reminders=[item])
        self.assertEqual(len([x for x in q["items_all"] + q["stale_items"] if x["kind"] == "안내 문자"]), 1)

    def test_i2_no_reminders_without_sms_create_permission(self):
        import partnerships, support_requests, transport, auth
        partnerships.init_schema(); support_requests.init_schema(); transport.init_schema()
        pid = self.patient(); self.consult(pid)
        real = auth.menu_level
        with patch.object(auth, "menu_level", lambda user, key: 1 if key == "sms" else real(user, key)):
            html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn('sms-rm-close" data-key', html)

    def test_i2_close_button_hidden_without_sms_permission(self):
        css = open(os.path.join(os.path.dirname(__file__), "..", "static", "css", "style.css"), encoding="utf-8").read()
        self.assertIn("body:not(.cc-sms) .sms-rm-close", css)


if __name__ == "__main__":
    unittest.main()
