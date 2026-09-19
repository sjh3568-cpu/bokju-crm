"""상담목록 '퇴원완료일' 열 (2026-09-19 요청).

퇴원일은 원무 명부 회차(admission_episodes.discharged_at)에만 있고 상담에는 거의 안 내려와 있었다.
그래서 이 열은 상담의 퇴원일을 먼저 쓰고, 없으면 명부 회차 값을 '명부' 꼬리표와 함께 보여준다.
열을 하나 더했으니 헤더·필터행·본문의 칸 수가 어긋나지 않는지도 함께 고정한다.
"""
import os
import re
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


class ConsultListDischargeColumnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, "DB_PATH", os.path.join(self.tmp.name, "list.db"))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        partnerships.init_schema()
        support_requests.init_schema()
        models.ensure_admin_user("list-test", "test-password", display_name="점검")
        self.uid = models.get_user("list-test")["id"]
        boot = patch.object(main, "_db_initialized", True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.uid, username="list-test", display_name="점검",
                           role="admin", cooperation_permissions_v2=True,
                           perms={k: 3 for k in main.MENU_KEYS})
        with models.get_db() as conn:
            # 1) 상담에 퇴원일이 적힌 건  2) 명부에만 있는 건  3) 아직 재원
            rows = [(1, "상담퇴원", "퇴원완료", d(-200), d(-150)), (2, "명부퇴원", "입원완료", d(-200), None),
                    (3, "재원중", "입원완료", d(-40), None)]
            for pid, name, st, adm, dis in rows:
                conn.execute("INSERT INTO patients (id,name,gender) VALUES (?,?,'F')", (pid, name))
                conn.execute("""INSERT INTO consultations (id,patient_id,consult_date,admission_status,
                                actual_admission_date,discharge_date) VALUES (?,?,?,?,?,?)""",
                             (pid, pid, d(-210), st, adm, dis))
            conn.execute("""INSERT INTO admission_episodes (patient_id,episode_no,status,admitted_at,discharged_at,roster_key)
                            VALUES (2,1,'discharged',?,?, 'c2|x')""", (d(-200), d(-120)))
            conn.execute("""INSERT INTO admission_episodes (patient_id,episode_no,status,admitted_at,roster_key)
                            VALUES (3,1,'admitted',?, 'c3|x')""", (d(-40),))

    def test_list_rows_carry_the_roster_discharge_date(self):
        rows = {r["patient_name"]: r for r in models.list_consultations(limit=50)}
        self.assertEqual(rows["상담퇴원"]["discharge_date"], d(-150))
        self.assertIsNone(rows["상담퇴원"].get("roster_discharged_at"))   # 상담 값이 있으면 명부는 안 얹는다
        self.assertEqual(rows["명부퇴원"]["roster_discharged_at"], d(-120))
        self.assertIsNone(rows["재원중"].get("roster_discharged_at"))     # 아직 퇴원 전

    def test_column_renders_with_source_tag(self):
        html = self.client.get("/consultations").get_data(as_text=True)
        self.assertIn("<th>퇴원완료일</th>", html)
        self.assertIn('class="dc-roster"', html)          # 명부에서 온 값에 꼬리표
        self.assertIn("원무 명부 기준", html)

    def test_header_filter_and_body_have_the_same_column_count(self):
        html = self.client.get("/consultations").get_data(as_text=True)
        table = html[html.index('class="tbl tbl-compact"'):]
        head = table[:table.index("</thead>")]
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", head, re.S)
        self.assertEqual(len(rows), 2, "헤더행 + 필터행")
        n_head = len(re.findall(r"<th", rows[0]))
        n_filter = len(re.findall(r"<th", rows[1]))
        body = table[table.index("<tbody>"):table.index("</tbody>")]
        first = re.search(r"<tr[^>]*data-cid=.*?</tr>", body, re.S).group(0)
        n_body = len(re.findall(r"<td", first))
        self.assertEqual((n_head, n_filter, n_body), (20, 20, 20))

    def test_column_toggle_covers_the_new_column(self):
        html = self.client.get("/consultations").get_data(as_text=True)
        self.assertIn("hide-c20", html)     # 상담자까지 숨길 수 있어야 한다


if __name__ == "__main__":
    unittest.main()
