"""회복기 판정 규칙 통일 (원장 확인 2026-09-28).

"중추신경계만 회복기/비회복기로 나뉜다. 그 밖의 회복기 재활 대상 — 근골격계(대퇴골절·고관절 골절·
골반 골절), 비사용증후군, 양측 슬관절치환술, 하지 부위 절단 — 은 입원해 있으면 무조건 회복기."
전에는 이들을 '단일구간'으로 빼서 회복기 비율 분자에서 통째로 빠졌다(재원 271명 중 9명).
KPI 카드·스파크라인·회복기 비율 추이가 같은 규칙·같은 분모(그날 재원 전체)를 쓴다.
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
from app import _care_phase, phase_diseases


def d(n):
    return (date.today() + timedelta(days=n)).isoformat()


class CarePhaseRuleTests(unittest.TestCase):
    def _c(self, **kw):
        base = {"admission_purpose": "회복기재활", "actual_admission_date": d(-10),
                "consult_date": d(-20), "admission_status": "입원완료"}
        base.update(kw)
        return base

    def test_noncns_recovery_targets_are_recovery_while_admitted(self):
        for dz in (["근골격계"], ["고관절 골절"], ["대퇴부 골절"], ["비사용증후군"], ["슬관절 치환술"], ["하지 부위 절단"]):
            self.assertEqual(_care_phase(self._c(diseases=dz))["care_phase"], "회복기", dz)

    def test_noncns_recovery_target_does_not_expire(self):
        """근골격계 30일 기간이 지나도 입원 중이면 회복기 — 전환 개념이 없다."""
        c = self._c(diseases=["근골격계"], actual_admission_date=d(-200), disease_onset=d(-230))
        self.assertEqual(_care_phase(c)["care_phase"], "회복기")

    def test_other_noncns_stays_single_lane(self):
        self.assertEqual(_care_phase(self._c(diseases=["암"]))["care_phase"], "단일구간")
        self.assertEqual(_care_phase(self._c(diseases=["폐질환"]))["care_phase"], "단일구간")

    def test_cns_keeps_recovery_vs_nonrecovery_split(self):
        self.assertEqual(_care_phase(self._c(diseases=["뇌출혈"], disease_onset=d(-30)))["care_phase"], "회복기")
        self.assertEqual(_care_phase(self._c(diseases=["뇌출혈"], disease_onset=d(-400),
                                             actual_admission_date=d(-390)))["care_phase"], "비회복기")

    def test_mixed_cns_and_disuse_follows_cns_rule(self):
        """'뇌출혈+비사용증후군'은 중추 규칙 — 입원목적이 일반재활이면 미판정, 회복기로 부풀리지 않는다."""
        c = self._c(diseases=["뇌출혈", "비사용증후군"], admission_purpose="일반재활")
        self.assertEqual(_care_phase(c)["care_phase"], "미판정")

    def test_empty_group_falls_back_to_detail_text(self):
        """진단군 칸이 비어도 병명 상세로 판정 — 김한진 님 '비사용 증후군 / 림프종'(띄어쓰기), 차진섭 님 '뇌손상 / 수두증'."""
        self.assertEqual(phase_diseases({"diseases": [], "disease_detail": "비사용 증후군 / 림프종"}), ["비사용증후군"])
        self.assertEqual(_care_phase(self._c(diseases=[], disease_detail="비사용 증후군 / 림프종"))["care_phase"], "회복기")
        self.assertEqual(_care_phase(self._c(diseases=[], disease_detail="뇌손상 / 수두증, 폐렴",
                                             disease_onset=d(-40)))["care_phase"], "회복기")
        # 저장된 진단군 값은 건드리지 않는다 — 판정에만 쓴다
        c = self._c(diseases=[], disease_detail="뇌손상")
        _care_phase(c)
        self.assertEqual(c["diseases"], [])

    def test_roster_rehab_end_still_wins(self):
        """명부 재활종료일(Q열)이 적재됐으면 그 날짜가 사실 — 지났으면 근골격계라도 비회복기."""
        c = self._c(diseases=["근골격계"], rehab_end_imported=True, rehab_end_date=d(-1))
        self.assertEqual(_care_phase(c)["care_phase"], "비회복기")


class CardAndTrendAgreeTests(unittest.TestCase):
    """같은 날 KPI 카드와 추이·스파크라인이 같은 분자·분모를 낸다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "agree.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("agree-test", "test-password", display_name="점검")
        self.uid = models.get_user("agree-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="agree-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        with models.get_db() as conn:
            people = [  # (id, 이름, 진단군, 상세, 발병일, 입원목적)
                (1, "뇌출혈회복", '["뇌출혈"]', "", d(-30), "회복기재활"),
                (2, "뇌경색만료", '["뇌경색"]', "", d(-400), "회복기재활"),
                (3, "대퇴골절", '["근골격계"]', "근골격계 단일부위 / 대퇴골절", None, "회복기재활"),
                (4, "비사용빈칸", '[]', "비사용 증후군 / 림프종", None, "회복기재활"),
                (5, "암환자", '["암"]', "", None, "요양"),
            ]
            for pid, name, dz, detail, onset, purpose in people:
                adm = d(-20) if pid != 2 else d(-390)
                conn.execute("INSERT INTO patients (id,name,gender,chart_no) VALUES (?,?,'M',?)", (pid, name, f"{pid:010d}"))
                conn.execute("""INSERT INTO consultations (id,patient_id,consult_date,admission_status,actual_admission_date,
                                room_number,diseases,disease_detail,disease_onset,admission_purpose)
                                VALUES (?,?,?,'입원완료',?,'301호',?,?,?,?)""",
                             (pid, pid, d(-60), adm, dz, detail, onset, purpose))
                conn.execute("""INSERT INTO admission_episodes (patient_id,episode_no,status,admitted_at,room_number,ward,roster_key)
                                VALUES (?,1,'admitted',?,'301호','3병동',?)""", (pid, adm, f"c{pid}|x"))
            # 6) 명부 이후 CRM 입원완료(명부 밖) — 카드도 추이도 재원으로 세야 한다
            conn.execute("INSERT INTO patients (id,name,gender) VALUES (6,'명부밖골절','M')")
            conn.execute("""INSERT INTO consultations (id,patient_id,consult_date,admission_status,actual_admission_date,
                            room_number,diseases,admission_purpose)
                            VALUES (6,6,?,'입원완료',?,'1003호','["고관절 골절"]','회복기재활')""", (d(-30), d(-1)))
            conn.execute("""INSERT INTO admission_episodes (patient_id,consultation_id,episode_no,status,admitted_at,room_number)
                            VALUES (6,6,1,'admitted',?,'1003호')""", (d(-1),))

    def _card_recovery(self, cen):
        rows = models.list_consultations(ids=list(cen["by_consultation"]), limit=100)
        n = 0
        for c in rows:
            ep = cen["by_consultation"][c["id"]]
            c["actual_admission_date"] = ep["admitted_at"]
            if _care_phase(c)["care_phase"] == "회복기":
                n += 1
        return n

    def test_today_numbers_match(self):
        from views.ward import _trend_span
        cen = models.current_admission_census()
        self.assertEqual(len(cen["patients"]), 6)
        card_rec = self._card_recovery(cen)
        self.assertEqual(card_rec, 4, "뇌출혈회복·대퇴골절·비사용빈칸·명부밖골절")

        spans = models.admission_spans()
        self.assertEqual(len(spans), 6, "명부 밖 CRM 회차도 추이 분모에 든다")
        tr = {c["id"]: c for c in models.list_consultations(
            ids=[sp["consultation_id"] for sp in spans if sp.get("consultation_id")], limit=100)}
        open_con = {ep["id"]: cid for cid, ep in cen["by_consultation"].items()}
        extra = [cid for cid in open_con.values() if cid not in tr]
        if extra:
            tr.update({c["id"]: c for c in models.list_consultations(ids=extra, limit=100)})
        today = date.today(); iso = today.isoformat(); trend_rec = trend_tot = 0
        for sp in spans:
            cid = open_con.get(sp["episode_id"]) or sp.get("consultation_id")
            a, dis, known, rend = _trend_span(sp, tr.get(cid))
            if a > iso or (dis and dis <= iso):
                continue
            trend_tot += 1
            if known and rend is not None and today <= rend:
                trend_rec += 1
        self.assertEqual((trend_rec, trend_tot), (card_rec, 6))

    def test_ward_page_and_trend_tab_show_the_same_ratio(self):
        ward = self.client.get("/ward").get_data(as_text=True)
        trend = self.client.get("/ward?tab=trend&preset=30").get_data(as_text=True)
        self.assertIn("66.7", ward)     # 4/6
        self.assertIn("66.7", trend)


if __name__ == "__main__":
    unittest.main()
