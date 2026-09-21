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
        self.assertIn('(여/86)', page); self.assertIn('>502호</td>', page); self.assertNotIn('5병동 · 502', page)
        self.assertIn('재원 중', page)   # 입원 행의 퇴원일
        self.assertIn('<option value="">병동 전체</option>', page)
        self.assertEqual([r['patient_name'] for r in ward_moves.report({'date_from': '2026-06-01', 'date_to': '2026-06-30', 'ward': '5병동'})['rows']], ['퇴원자'])
        status = self.c.get('/ward').get_data(as_text=True)
        self.assertIn('href="/ward?tab=moves"', status)
        away = self.c.get('/ward?tab=away').get_data(as_text=True)      # 외진은 외진 환자 탭에서(2026-09-19)
        self.assertNotIn('<th>보호자</th>', away)                            # 외진 표에서 보호자 열 제거
        x = self.c.get('/ward/moves.xlsx?date_from=2026-06-01&date_to=2026-06-30')
        self.assertEqual(x.status_code, 200); self.assertIn('spreadsheetml', x.mimetype)


if __name__ == '__main__':
    unittest.main()


class AwayReturnAsAdmissionTests(unittest.TestCase):
    """외진 복귀 = 그날의 입원 (2026-09-15). 명부 회차가 없으면 복귀 기록으로, 있으면 회차로 한 번만."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 't.db')); self.db_patch.start()
        models.init_db()
        db = models.get_db()
        self.pid = db.execute("INSERT INTO patients(name,gender,birth_year) VALUES ('박성락','M',1950)").lastrowid
        self.cid = db.execute("""INSERT INTO consultations(patient_id,consult_date,admission_status,actual_admission_date,counselor,attending_doctor)
                                 VALUES (?,?,'입원완료','2026-08-10','박세연','정기천')""", (self.pid, '2026-07-29')).lastrowid
        # 명부: 8/10 입원 → 9/2 응급전원으로 퇴원 회차
        db.execute("""INSERT INTO admission_episodes(patient_id,episode_no,status,admitted_at,discharged_at,ward,room_number,care_type,roster_key)
                      VALUES (?,1,'discharged','2026-08-10','2026-09-02','3병동','310호','회복기','c1|2026-08-10')""", (self.pid,))
        db.execute("""INSERT INTO admission_events(consultation_id,event_type,event_date,hospital,returned_at,return_room)
                      VALUES (?,'응급전원','2026-09-02','안동병원','2026-09-11','602호')""", (self.cid,))
        db.commit(); db.close()

    def tearDown(self):
        self.db_patch.stop(); self.tmp.cleanup()

    def _add_roster_readmission(self):
        db = models.get_db()
        db.execute("""INSERT INTO admission_episodes(patient_id,episode_no,status,admitted_at,ward,room_number,care_type,roster_key)
                      VALUES (?,2,'admitted','2026-09-11','6병동','602호','회복기','c1|2026-09-11')""", (self.pid,))
        db.commit(); db.close()

    def test_return_without_roster_episode_is_an_admission_row(self):
        rep = ward_moves.report({'date_from': '2026-09-08', 'date_to': '2026-09-14'})
        self.assertEqual([(r['kind'], r['date'], r.get('is_return', False)) for r in rep['rows']], [('in', '2026-09-11', True)])
        row = rep['rows'][0]
        self.assertEqual((row['patient_name'], row['room'], row['doctor'], row['counselor'], row['away_type']),
                         ('박성락', '602호', '정기천', '박세연', '응급전원'))
        self.assertEqual(rep['summary']['admissions'], 1)
        # 나간 날은 명부 퇴원 회차가 이미 들고 있다 — 복귀 행이 퇴원을 만들지 않는다
        wide = ward_moves.report({'date_from': '2026-09-01', 'date_to': '2026-09-14'})
        self.assertEqual([(r['kind'], r['date']) for r in wide['rows']], [('in', '2026-09-11'), ('out', '2026-09-02')])

    def test_return_already_in_roster_is_counted_once(self):
        self._add_roster_readmission()
        rep = ward_moves.report({'date_from': '2026-09-08', 'date_to': '2026-09-14'})
        self.assertEqual([(r['kind'], r['date'], r.get('is_return', False)) for r in rep['rows']], [('in', '2026-09-11', False)])
        self.assertEqual(rep['summary']['admissions'], 1)

    def test_transfer_outcome_is_not_an_admission(self):
        db = models.get_db()
        db.execute("UPDATE admission_events SET return_outcome='전원', return_hospital='타병원'"); db.commit(); db.close()
        self.assertEqual(ward_moves.report({'date_from': '2026-09-08', 'date_to': '2026-09-14'})['rows'], [])

    def test_flow_counts_and_daily_metrics_include_returns(self):
        import dashboard_metrics
        flow = models.admission_flow_counts('2026-09-07', '2026-09-13', '2026-09-01', '2026-09-15')
        self.assertEqual((flow['week_in'], flow['week_out'], flow['month_in'], flow['month_out']), (1, 0, 1, 1))
        by_date = dashboard_metrics.admission_flow_by_date(['2026-09-10', '2026-09-11'])
        self.assertEqual(by_date['2026-09-11'], {'in': 1, 'out': 0})
        self._add_roster_readmission()   # 명부가 갱신돼도 두 번 세지 않는다
        flow = models.admission_flow_counts('2026-09-07', '2026-09-13', '2026-09-01', '2026-09-15')
        self.assertEqual(flow['week_in'], 1)
        self.assertEqual(dashboard_metrics.admission_flow_by_date(['2026-09-11'])['2026-09-11']['in'], 1)

    def test_flow_counts_match_event_basis(self):
        week_flow = models.admission_flow_counts('2026-09-07', '2026-09-13', '2026-09-01', '2026-09-15')
        expected = {
            'week_in': sum(1 for e in models.admission_flow_events('2026-09-07', '2026-09-13') if e['kind'] == models.ADMISSION_EVENT_IN),
            'week_out': sum(1 for e in models.admission_flow_events('2026-09-07', '2026-09-13') if e['kind'] == models.ADMISSION_EVENT_OUT),
            'month_in': sum(1 for e in models.admission_flow_events('2026-09-01', '2026-09-15') if e['kind'] == models.ADMISSION_EVENT_IN),
            'month_out': sum(1 for e in models.admission_flow_events('2026-09-01', '2026-09-15') if e['kind'] == models.ADMISSION_EVENT_OUT),
        }
        self.assertEqual(week_flow, expected)

    def test_sync_does_not_overwrite_roster_episode_dates(self):
        """상담을 다시 저장해도 명부 회차의 입원일(9/11)이 상담 입원일(8/10)로 되돌아가지 않는다."""
        db = models.get_db()
        eid = db.execute("""INSERT INTO admission_episodes(patient_id,consultation_id,episode_no,status,admitted_at,ward,room_number,roster_key)
                            VALUES (?,?,2,'admitted','2026-09-11','6병동','602호','c1|2026-09-11')""", (self.pid, self.cid)).lastrowid
        db.commit(); db.close()
        self.assertEqual(models.sync_admission_episode(self.cid), eid)
        db = models.get_db()
        ep = dict(db.execute("SELECT admitted_at, discharged_at, room_number, status FROM admission_episodes WHERE id=?", (eid,)).fetchone())
        db.close()
        self.assertEqual(ep, {'admitted_at': '2026-09-11', 'discharged_at': None, 'room_number': '602호', 'status': 'admitted'})

    def test_init_db_repairs_roster_admitted_at_from_key(self):
        db = models.get_db()
        eid = db.execute("""INSERT INTO admission_episodes(patient_id,consultation_id,episode_no,status,admitted_at,roster_key)
                            VALUES (?,?,2,'admitted','2026-08-10','c1|2026-09-11')""", (self.pid, self.cid)).lastrowid
        db.commit(); db.close()
        models.init_db()
        db = models.get_db()
        self.assertEqual(db.execute("SELECT admitted_at FROM admission_episodes WHERE id=?", (eid,)).fetchone()[0], '2026-09-11')
        db.close()

    def test_data_quality_report_flags_roster_mismatch_and_returns_without_roster(self):
        db = models.get_db()
        db.execute("""INSERT INTO admission_episodes(patient_id,consultation_id,episode_no,status,admitted_at,roster_key)
                      VALUES (?,?,2,'admitted','2026-08-10','c1|2026-09-11')""", (self.pid, self.cid))
        db.commit(); db.close()
        checks = {c['title']: c for c in models.data_quality_report()['checks']}
        self.assertEqual(checks['명부 회차 입원일 불일치']['count'], 1)
        self.assertEqual(checks['명부 회차 입원일 불일치']['rows'][0]['patient_name'], '박성락')
        # 8/10 회차는 9/11에 시작하지 않으므로 복귀는 여전히 '명부 회차 없음'
        self.assertEqual(checks['외진 복귀 — 명부 회차 없음']['count'], 1)
        models.init_db()   # 복구 후 불일치 0, 회차가 9/11로 맞춰져 복귀 검사도 0
        checks = {c['title']: c for c in models.data_quality_report()['checks']}
        self.assertEqual((checks['명부 회차 입원일 불일치']['count'], checks['외진 복귀 — 명부 회차 없음']['count']), (0, 0))
