"""재원관리 → 입원·퇴원 이력 탭 — 명부 회차 기준 기간 조회·필터·요약·엑셀."""
import os
import tempfile
import unittest
from unittest.mock import patch

import app as main
import models
import ward_moves


class WardMovesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 't.db')); self.db_patch.start()
        models.init_db()
        db = models.get_db()
        def patient(name, gender='M', birth=1950):
            return db.execute("INSERT INTO patients(name,gender,birth_year) VALUES (?,?,?)", (name, gender, birth)).lastrowid
        def episode(pid, adm, dis, ward, room, doctor, care, key):
            db.execute("""INSERT INTO admission_episodes(patient_id,episode_no,status,admitted_at,discharged_at,ward,room_number,attending_doctor,care_type,roster_key)
                          VALUES (?,1,'admitted',?,?,?,?,?,?,?)""", (pid, adm, dis, ward, room, doctor, care, key))
        a = patient('입원자'); b = patient('퇴원자', 'F', 1940); c = patient('오래전')
        episode(a, '2026-06-10', None, '3병동', '301', '이성범', '회복기재활', 'k1')
        episode(b, '2026-03-01', '2026-06-20', '5병동', '502', '정기천', '비회복기', 'k2')
        episode(c, '2025-01-01', '2025-02-01', '3병동', '305', '이성범', '회복기', 'k3')   # 기간 밖
        cid = db.execute("""INSERT INTO consultations(patient_id,consult_date,admission_status,counselor,discharge_destination,discharge_reason)
                            VALUES (?,?,'입원완료','박세연','자택 귀가','치료 종료')""", (b, '2026-02-20')).lastrowid
        db.commit(); db.close()
        models.ensure_admin_user('t', 'x', display_name='t'); u = models.get_user('t')
        self.ctx = [patch.object(main, '_db_initialized', True), patch.object(models, 'first_unread_required_announcement', return_value=None)]
        for x in self.ctx: x.start()
        main.app.config.update(TESTING=True); self.c = main.app.test_client()
        with self.c.session_transaction() as s:
            s.update(user_id=u['id'], username='t', role='admin', perms={k: 3 for k in main.MENU_KEYS})

    def tearDown(self):
        for x in self.ctx: x.stop()
        self.db_patch.stop(); self.tmp.cleanup()

    def test_report_rows_summary_and_filters(self):
        rep = ward_moves.report({'date_from': '2026-06-01', 'date_to': '2026-06-30'})
        self.assertEqual([(r['kind'], r['patient_name']) for r in rep['rows']], [('out', '퇴원자'), ('in', '입원자')])
        out = rep['rows'][0]
        self.assertEqual((out['stay_days'], out['destination'], out['reason'], out['counselor'], out['care']), (112, '자택 귀가', '치료 종료', '박세연', '비회복기'))
        self.assertEqual((rep['summary']['admissions'], rep['summary']['discharges'], rep['summary']['net'], rep['summary']['avg_stay']), (1, 1, 0, 112.0))
        self.assertEqual(rep['options']['doctors'], ['이성범', '정기천'])
        self.assertEqual([(c['label'], c['n']) for c in rep['summary']['care_cells']], [('회복기', 1), ('비회복기', 0), ('구분 없음', 0)])
        self.assertEqual([r['patient_name'] for r in ward_moves.report({'date_from': '2026-06-01', 'date_to': '2026-06-30', 'kind': 'in'})['rows']], ['입원자'])
        self.assertEqual([r['patient_name'] for r in ward_moves.report({'date_from': '2026-06-01', 'date_to': '2026-06-30', 'doctor': '정기천'})['rows']], ['퇴원자'])
        self.assertEqual([r['patient_name'] for r in ward_moves.report({'date_from': '2026-06-01', 'date_to': '2026-06-30', 'destination': '귀가'})['rows']], ['퇴원자'])
        self.assertEqual(ward_moves.report({'date_from': '2026-06-01', 'date_to': '2026-06-30', 'q': '없는이름'})['rows'], [])

    def test_default_period_is_last_30_days(self):
        f = ward_moves.report({})['filters']
        from datetime import date, timedelta
        self.assertEqual(f['date_to'], date.today().isoformat())
        self.assertEqual(f['date_from'], (date.today() - timedelta(days=29)).isoformat())

    def test_tab_renders_and_xlsx_downloads(self):
        page = self.c.get('/ward?tab=moves&date_from=2026-06-01&date_to=2026-06-30').get_data(as_text=True)
        self.assertIn('입원·퇴원 이력', page); self.assertIn('퇴원자', page); self.assertIn('자택 귀가', page); self.assertIn('순증감', page)
        self.assertNotIn('오래전', page)
        # 표: 연번·연도 붙은 날짜·환자 (성별/나이)·호실만·퇴원일 열
        self.assertIn('<th>연번</th><th>날짜</th>', page); self.assertIn('<th>퇴원일</th>', page)
        self.assertIn('<td class="mv-no">1</td>', page); self.assertIn('2026-06-20(토)', page)
        self.assertIn('(여/86)', page); self.assertIn('>502</td>', page); self.assertNotIn('5병동 · 502', page)
        self.assertIn('재원 중', page)   # 입원 행의 퇴원일
        self.assertIn('<option value="">병동 전체</option>', page)
        self.assertEqual([r['patient_name'] for r in ward_moves.report({'date_from': '2026-06-01', 'date_to': '2026-06-30', 'ward': '5병동'})['rows']], ['퇴원자'])
        status = self.c.get('/ward').get_data(as_text=True)
        self.assertIn('href="/ward?tab=moves"', status)
        self.assertNotIn('<th>보호자</th>', status.split('id="sec-away"')[1].split('</table>')[0])   # 외진 중 표에서 보호자 열 제거
        x = self.c.get('/ward/moves.xlsx?date_from=2026-06-01&date_to=2026-06-30')
        self.assertEqual(x.status_code, 200); self.assertIn('spreadsheetml', x.mimetype)


if __name__ == '__main__':
    unittest.main()
