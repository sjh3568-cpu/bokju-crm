"""기관협력 회귀 검증. 실제 환자 DB 대신 임시 SQLite 사용."""
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import app as main
import models
import partnerships as coop


class CooperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 'test.db'))
        self.db_patch.start()
        models.init_db()
        db = models.get_db()
        for name in ('대구굿모닝병원', '에스포항병원'):
            db.execute('INSERT OR IGNORE INTO source_hospitals(name) VALUES (?)', (name,))
        db.commit(); db.close()
        coop.init_schema()
        models.ensure_admin_user('testadmin', 'test-only-password', display_name='테스트')
        self.user=models.get_user('testadmin')
        self.initialized=patch.object(main,'_db_initialized',True);self.initialized.start()
        self.notices=patch.object(models,'first_unread_required_announcement',return_value=None);self.notices.start()
        main.app.config.update(TESTING=True)
        self.client=main.app.test_client()
        with self.client.session_transaction() as s:
            s.update(user_id=self.user['id'],username='testadmin',role='admin',perms={k:3 for k in main.MENU_KEYS},cooperation_csrf='token')
        db=models.get_db()
        self.pid=db.execute('SELECT p.id FROM cooperation_partners p JOIN source_hospitals h ON h.id=p.hospital_id WHERE h.name=?',('대구굿모닝병원',)).fetchone()['id']
        self.other=db.execute('SELECT id FROM cooperation_partners WHERE id<>?',(self.pid,)).fetchone()['id']
        db.close()

    def tearDown(self):
        self.notices.stop();self.initialized.stop();self.db_patch.stop();self.tmp.cleanup()

    def post(self, **data):
        return self.client.post(f'/partners/{self.pid}/save',data=dict(csrf='token',**data))

    def scalar(self, sql):
        db=models.get_db()
        try:return db.execute(sql).fetchone()[0]
        finally:db.close()

    def test_profile_activity_reminder_and_contacts(self):
        self.assertEqual(self.client.get('/partners').status_code,200)
        self.assertEqual(self.post(action='profile',important='1',owner='담당',visit_cycle='30',contact_cycle='14',notes='메모').status_code,302)
        self.post(action='contact',name='협력담당',department='진료협력',position='팀장',phone='업무연락처')
        past=(date.today()-timedelta(days=30)).isoformat()
        self.post(action='activity',kind='방문',happened_on=past,met='협력담당',owner='담당',content='<script>검증</script>')
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM cooperation_tasks'),1)
        self.assertEqual(coop.reminders()[0]['dday'],'오늘')
        page=self.client.get(f'/partners/{self.pid}').get_data(as_text=True)
        self.assertIn('&lt;script&gt;',page)
        self.assertIn('협력담당',page)
        self.assertIn('방문 전 브리핑',page)
        self.assertIn('빠른 입력',page)
        self.assertIn('최근 방문 전 30일',page)
        self.assertIn('오늘의 기관협력 업무',self.client.get('/').get_data(as_text=True))
        # 완료·완료취소·기한변경
        tid=self.scalar('SELECT id FROM cooperation_tasks')
        self.post(action='complete',item_id=str(tid))
        self.assertEqual(coop.reminders(),[])
        self.post(action='reopen',item_id=str(tid))
        self.assertEqual(len(coop.reminders()),1)
        self.post(action='reschedule',item_id=str(tid),due_on=(date.today()+timedelta(days=8)).isoformat())
        self.assertEqual(coop.reminders(),[])
        # 다른 기관 항목을 변경할 수 없음
        response=self.client.post(f'/partners/{self.other}/save',data={'csrf':'token','action':'complete','item_id':str(tid)})
        self.assertEqual(response.status_code,404)

    def test_relationship_documents_and_visit_plan(self):
        self.post(action='profile',relationship_stage='협력 중',owner='담당',visit_preset='m1',
                  contact_preset='m1',remind_days='7',strengths='뇌졸중 연계')
        self.post(action='contact',name='대표자',department='협력실',status='재직',is_primary='1')
        self.post(action='document',category='공문',title='진료협력 안내',location='NAS/기관자료',expires_on='2027-01-01')
        self.post(action='visit_plan',visit_on='2026-10-01',sequence='2',purpose='정기 방문',owner='담당',status='예정')
        page=self.client.get(f'/partners/{self.pid}').get_data(as_text=True)
        self.assertIn('기관 자료대장',page)
        self.assertIn('진료협력 안내',page)
        self.assertIn('방문 계획표',page)
        self.assertIn('정기 방문',page)
        self.assertIn('협력 중',self.client.get('/partners?stage=협력+중').get_data(as_text=True))
        self.assertEqual(self.scalar('SELECT is_primary FROM cooperation_contacts WHERE name="대표자"'),1)

    def test_validation_and_permissions(self):
        self.post(action='profile',visit_cycle='0')
        self.assertIsNone(self.scalar('SELECT visit_cycle FROM cooperation_partners LIMIT 1'))
        self.post(action='activity',kind='방문',happened_on='2099-01-01',content='미래')
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM cooperation_activities'),0)
        self.assertEqual(self.client.post(f'/partners/{self.pid}/save',data={'action':'profile'}).status_code,400)
        with self.client.session_transaction() as s:
            s['perms']={k:(1 if k=='partners' else 0) for k in main.MENU_KEYS}
        self.assertEqual(self.client.get('/partners').status_code,200)
        self.post(action='task',kind='방문',due_on=date.today().isoformat(),title='권한없음')
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM cooperation_tasks'),0)
        with self.client.session_transaction() as s:
            s['perms']={k:0 for k in main.MENU_KEYS}
        self.assertEqual(self.client.get('/partners').status_code,403)
        self.assertEqual(self.client.get(f'/partners/{self.pid}/export').status_code,403)
        with self.client.session_transaction() as s:s.clear()
        self.assertEqual(self.client.get('/partners').status_code,302)

    def test_counts_use_separate_dates_and_explicit_attribution(self):
        db=models.get_db()
        patient=db.execute("INSERT INTO patients(name) VALUES ('=테스트')").lastrowid
        values=[
            ('2026-01-01','2026-02-01','입원완료','대구굿모닝병원','에스포항병원'),
            ('2026-02-10','2026-02-01','입원완료','대구굿모닝병원','에스포항병원'), # 같은 입원
            ('2026-02-28','2026-02-28','퇴원완료','대구굿모닝병원','에스포항병원'), # 재입원
            ('2026-02-12',None,'입원완료','대구굿모닝병원','에스포항병원'),
            ('2026-02-12','2026-02-15','입원취소','대구굿모닝병원','에스포항병원'),
            ('2026-03-01','2026-03-01','입원완료','대구굿모닝병원','에스포항병원'),
            ('2026-02-15','2026-02-16','입원완료','','대구굿모닝병원'),
        ]
        for con,adm,status,ref,source in values:
            db.execute('''INSERT INTO consultations(patient_id,consult_date,actual_admission_date,admission_status,referrer_institution,source_hospital,primary_diagnosis)
                VALUES (?,?,?,?,?,?,?)''',(patient,con,adm,status,ref,source,'뇌경색'))
        db.commit()
        consults,admissions=coop.patient_report(db,'대구굿모닝병원','referral','2026-02-01','2026-02-28')
        self.assertEqual(len(consults),4)
        self.assertEqual(len(admissions),2)
        self.assertEqual(len(coop.undated_admissions(db,'대구굿모닝병원','referral')),1)
        self.assertEqual(len(coop.patient_report(db,'대구굿모닝병원','source','2026-02-01','2026-02-28')[1]),1)
        db.close()
        query='period=custom&start=2026-02-01&end=2026-02-28&basis=referral'
        page=self.client.get(f'/partners/{self.pid}?{query}')
        self.assertEqual(page.status_code,200)
        self.assertIn('뇌경색',page.get_data(as_text=True))
        export=self.client.get(f'/partners/{self.pid}/export?{query}')
        self.assertEqual(export.status_code,200)
        self.assertEqual(len(export.get_data(as_text=True).splitlines()),3)
        self.assertIn("'=테스트",export.get_data(as_text=True))
        self.assertEqual(self.client.get(f'/partners/{self.pid}/export?period=custom&start=bad').status_code,400)

    def test_legacy_diagnosis_and_session_permission_refresh(self):
        db=models.get_db()
        patient=db.execute("INSERT INTO patients(name) VALUES ('테스트환자')").lastrowid
        db.execute("""INSERT INTO consultations(patient_id,consult_date,admission_date,admission_status,
                    source_hospital,diseases) VALUES (?, '2026-02-01','2026-02-02','입원완료',
                    '대구굿모닝병원','["뇌경색", "당뇨"]')""",(patient,))
        db.commit()
        _,admissions=coop.patient_report(db,'대구굿모닝병원','source','','')
        self.assertEqual(admissions[0]['diagnosis_label'],'질환: 뇌경색, 당뇨')
        self.assertEqual(admissions[0]['admitted_on'],'2026-02-02')
        db.close()
        with self.client.session_transaction() as s:
            s['perms']={k:3 for k in main.MENU_KEYS if k!='partners'}
        self.assertEqual(self.client.get('/partners').status_code,200)
        with self.client.session_transaction() as s:
            self.assertEqual(s['perms']['partners'],2)

    def test_personal_briefing_and_start_page_setting(self):
        page=self.client.get('/').get_data(as_text=True)
        self.assertIn('오늘의 개인 브리핑',page)
        self.assertIn('나의 입원 환자',page)
        self.assertIn('pb3-admission-split',page)
        self.assertIn('입원예정',page)
        self.assertIn('입원완료',page)
        self.assertRegex(page,r'\d{4}\.\d{2}\.\d{2} \([월화수목금토일]\)')
        self.assertIn('내 계정 설정',page)
        with self.client.session_transaction() as s:
            csrf=s['start_page_csrf']
        response=self.client.post('/settings/start-page',data={'csrf':csrf,'start_page':'partners'})
        self.assertEqual(response.status_code,302)
        self.assertEqual(models.get_user_by_id(self.user['id'])['start_page'],'partners')
        response=self.client.post('/settings/start-page',data={'csrf':csrf,'start_page':'default'})
        self.assertEqual(response.status_code,302)
        self.assertEqual(models.get_user_by_id(self.user['id'])['start_page'],'default')
        self.assertEqual(self.client.post('/settings/start-page',data={'csrf':'bad','start_page':'ward'}).status_code,400)

    def test_user_can_change_own_password(self):
        page=self.client.get('/account')
        self.assertEqual(page.status_code,200)
        self.assertIn('비밀번호 변경',page.get_data(as_text=True))
        with self.client.session_transaction() as s: csrf=s['account_csrf']
        self.client.post('/account',data={'csrf':csrf,'action':'profile','department':'상담실','position':'상담사','extension':'123','work_phone':'010-0000-0000'})
        self.client.post('/account',data={'csrf':csrf,'action':'preferences','mode':'views','calendar_mine':'0','partner_view':'feed','ward_tab':'waiting'})
        self.client.post('/account',data={'csrf':csrf,'action':'preferences','mode':'alerts','notify_todo':'1','notify_discharge':'1'})
        account=models.get_user_by_id(self.user['id'])
        self.assertEqual(account['department'],'상담실')
        self.assertEqual(account['preferences_data']['partner_view'],'feed')
        self.assertFalse(account['preferences_data']['notify_partner'])
        account_page=self.client.get('/account').get_data(as_text=True)
        self.assertIn('내 권한·요청',account_page)
        self.assertNotIn('상단 메뉴 표시',account_page)
        self.assertIn('data-account-display',account_page)
        self.assertIn('내 할 일',account_page)
        self.assertIn('미반영 (조직 기본값 적용)',account_page)
        self.assertIn('기본 · 대시보드',account_page)
        self.assertIn('기본 · 내 담당 일정',account_page)
        self.assertIn('상담 · 새 상담 등록',account_page)
        self.assertIn('재원 · 입원 대기',account_page)
        self.assertIn('기관협력 · 활동 피드',account_page)
        self.client.post('/account',data={'csrf':csrf,'action':'preferences','mode':'views',
            'calendar_mine':'','partner_view':'','ward_tab':''})
        reset_preferences=models.get_user_by_id(self.user['id'])['preferences_data']
        self.assertNotIn('calendar_mine',reset_preferences)
        self.assertNotIn('partner_view',reset_preferences)
        self.assertNotIn('ward_tab',reset_preferences)
        self.assertTrue(reset_preferences['notify_todo'])
        lowered={k:3 for k in main.MENU_KEYS};lowered['partners']=1;models.set_user_permissions(self.user['id'],lowered)
        with self.client.session_transaction() as s:s['perms']['partners']=1
        request_page=self.client.post('/account',data={'csrf':csrf,'action':'permission_request','menu_key':'partners','requested_level':'2','reason':'기관 수정 업무'})
        self.assertEqual(request_page.status_code,302)
        requests=models.list_permission_requests(self.user['id'],pending_only=True)
        self.assertEqual(len(requests),1)
        admin_page=self.client.get('/admin/users').get_data(as_text=True)
        self.assertIn('기관 수정 업무',admin_page)
        self.client.post(f"/admin/permission-requests/{requests[0]['id']}",data={'status':'반려'})
        self.assertEqual(models.list_permission_requests(self.user['id'])[0]['status'],'반려')
        self.client.post('/account',data={'csrf':csrf,'current_password':'wrong',
            'new_password':'new-password','confirm_password':'new-password'})
        self.assertIsNone(main.authenticate('testadmin','new-password'))
        response=self.client.post('/account',data={'csrf':csrf,'current_password':'test-only-password',
            'new_password':'new-password','confirm_password':'new-password'})
        self.assertEqual(response.status_code,302)
        self.assertIsNotNone(main.authenticate('testadmin','new-password'))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM audit_log WHERE action='change_own_password'"),1)

    # 통합 인박스는 2026-09-10부터 INBOX_ENABLED=0으로 숨김. 라우트 자체는 남아 있어
    # 되살릴 때를 대비해 플래그를 켠 상태로 회귀 검증을 유지한다.
    @patch.object(main,'INBOX_ENABLED',True)
    def test_unified_inbox_create_filter_assign_and_complete(self):
        page=self.client.get('/inbox')
        self.assertEqual(page.status_code,200)
        self.assertIn('통합 상담 인박스',page.get_data(as_text=True))
        with self.client.session_transaction() as s: csrf=s['inbox_csrf']
        response=self.client.post('/inbox',data={'csrf':csrf,'action':'create','channel':'전화',
            'priority':'urgent','contact':'010-1234-5678','summary':'입원 가능 여부 문의',
            'body':'보호자 전화 문의','follow_up_at':'2026-09-08'})
        self.assertEqual(response.status_code,302)
        db=models.get_db();row=dict(db.execute("SELECT * FROM communications WHERE summary='입원 가능 여부 문의'").fetchone());db.close()
        self.assertEqual(row['assigned_user_id'],self.user['id'])
        self.assertEqual(row['priority'],'urgent')
        filtered=self.client.get('/inbox?status=open&channel=전화&q=입원').get_data(as_text=True)
        self.assertIn('010-1234-5678',filtered)
        response=self.client.post('/inbox',data={'csrf':csrf,'action':'update','comm_id':row['id'],
            'status':'done','priority':'normal','assigned_user_id':str(self.user['id'])})
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.scalar(f"SELECT COUNT(*) FROM communications WHERE id={row['id']} AND status='done' AND resolved_at IS NOT NULL"),1)

    def test_schema_idempotent_and_master_only(self):
        coop.init_schema()
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM cooperation_partners'),2)
        self.client.post('/partners/add',data={'csrf':'token','hospital':'없는병원'})
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM cooperation_partners'),2)
        self.assertEqual(self.client.get('/partners/999999').status_code,404)

    def test_manual_public_institution_and_list_filters(self):
        response=self.client.post('/partners/add-manual',data={
            'csrf':'token','name':'대구 테스트 보건소','kind':'보건소',
            'region':'대구광역시','address':'대구광역시 테스트로 1','phone':'053-123-4567'})
        self.assertEqual(response.status_code,302)
        page=self.client.get('/partners?partner_kind=보건소&partner_region=대구광역시').get_data(as_text=True)
        self.assertIn('대구 테스트 보건소',page)
        self.assertNotIn('에스포항병원',page)
        # 같은 기관을 다시 입력해도 협력기관은 중복 생성하지 않는다.
        self.client.post('/partners/add-manual',data={
            'csrf':'token','name':'대구 테스트 보건소','kind':'공공기관','region':'대구광역시'})
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM cooperation_partners p JOIN source_hospitals h ON h.id=p.hospital_id WHERE h.name='대구 테스트 보건소'"),1)
        with self.client.session_transaction() as s:
            s['perms']={k:(1 if k=='partners' else 0) for k in main.MENU_KEYS}
        models.set_user_permissions(self.user['id'],{k:(1 if k=='partners' else 0) for k in main.MENU_KEYS})
        self.assertEqual(self.client.post('/partners/add-manual',data={
            'csrf':'token','name':'권한 없는 기관','kind':'기타기관'}).status_code,302)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM source_hospitals WHERE name='권한 없는 기관'"),0)

    def test_master_search_cycles_and_hospital_analysis_dates(self):
        db=models.get_db()
        db.execute("INSERT INTO source_hospitals(name,kind,region,address,active) VALUES ('테스트의원','의원','경상북도','경상북도 포항시',1)")
        patient=db.execute("INSERT INTO patients(name) VALUES ('날짜환자')").lastrowid
        db.execute("""INSERT INTO consultations(patient_id,consult_date,planned_admission_date,
                    admission_status,source_hospital,diseases)
                    VALUES (?, '2026-02-01','2026-02-10','입원완료','테스트의원','[\"뇌경색\"]')""",(patient,))
        db.commit(); db.close()
        result=self.client.get('/partners/search?q=테스트&kind=의원').get_json()
        self.assertEqual(result['items'][0]['name'],'테스트의원')
        self.assertEqual(result['items'][0]['kind'],'의원')
        analysis=models.hospital_admission_analysis('2026-02-01','2026-02-28',hospital='테스트의원')
        self.assertEqual(analysis['total'],0)
        self.assertEqual(len(analysis['undated']),1)
        self.post(action='profile',visit_preset='m1',contact_preset='m3',remind_days='14',
                  specialties='신경과',strengths='뇌졸중 진료')
        db=models.get_db(); partner=db.execute('SELECT * FROM cooperation_partners WHERE id=?',(self.pid,)).fetchone(); db.close()
        self.assertEqual(partner['visit_months'],1)
        self.assertEqual(partner['contact_months'],3)
        self.assertEqual(partner['remind_days'],14)
        page=self.client.get('/partners?view=list&cycle_type=visit&cycle=m1')
        self.assertEqual(page.status_code,200)
        self.assertIn('신경과',page.get_data(as_text=True))
        search=self.client.get('/api/global-search?q=대구굿모닝').get_json()
        self.assertTrue(any(item['kind']=='협력기관' for item in search['items']))
        menu_search=self.client.get('/api/global-search?q=모병원').get_json()
        self.assertTrue(any(item['kind']=='메뉴' and item['url']=='/stats/hospitals' for item in menu_search['items']))
        overview=models.hospital_referral_overview('2026-02-01','2026-02-28')
        target=next(h for h in overview['hospitals'] if h['name']=='테스트의원')
        self.assertEqual(target['referrals'],1)
        self.assertEqual(target['admissions'],1)
        page=self.client.get('/stats/hospitals?preset=custom&from=2026-02-01&to=2026-02-28').get_data(as_text=True)
        self.assertIn('모병원 전체 현황',page)
        self.assertIn('상담의뢰 내림차순',page)
        self.assertIn('병원명 검색',page)
        hit=models.hospital_referral_overview('2026-02-01','2026-02-28',q='테스트')
        self.assertEqual([h['name'] for h in hit['hospitals']],['테스트의원'])
        self.assertEqual(hit['hospital_count'],1)
        self.assertEqual(hit['total_count'],overview['hospital_count'])
        self.assertEqual(hit['max_referrals'],overview['max_referrals'])
        self.assertEqual(models.hospital_referral_overview('2026-02-01','2026-02-28',q='없는병원')['hospitals'],[])
        searched=self.client.get('/stats/hospitals?preset=custom&from=2026-02-01&to=2026-02-28&q=테스트').get_data(as_text=True)
        self.assertIn('테스트의원',searched)
        self.assertIn('전체 모병원 보기',searched)
        missed=self.client.get('/stats/hospitals?preset=custom&from=2026-02-01&to=2026-02-28&q=없는병원').get_data(as_text=True)
        self.assertNotIn('테스트의원',missed)
        report=self.client.get('/report/monthly?year=2026&month=2').get_data(as_text=True)
        self.assertIn('class="bd-subhead"',report)
        self.assertIn('<th colspan="2">전월</th>',report)
        self.assertIn('<th colspan="2">전년</th>',report)

    def test_hospital_overview_merges_spelling_variants(self):
        """띄어쓰기·약칭은 한 기관으로 합치되, 접미사가 다르면 따로 센다."""
        rows=[{'name':n,'patient_id':pid,'admission_status':st,'consult_date':d} for n,pid,st,d in [
            ('대구 굿모닝병원',1,'입원완료','2026-03-02'),
            ('대구굿모닝병원',1,'상담중','2026-03-05'),   # 같은 환자 다른 표기 → 환자는 1명
            ('대구굿모닝',2,'입원완료','2026-03-07'),     # 약칭 → 후보가 하나뿐이라 흡수
            ('안동병원',3,'입원완료','2026-03-03'),
            ('안동의료원',4,'상담중','2026-03-04'),       # 접미사가 달라 별개 기관
            ('굿모닝병원',5,'상담중','2026-03-06'),       # 대구·평택 어느 쪽인지 알 수 없어 별개
        ]]
        items=models._group_hospital_consultations(rows)
        by_name={i['name']:i for i in items}
        merged=by_name['대구 굿모닝병원']
        self.assertEqual(merged['referrals'],3)
        self.assertEqual(merged['admissions'],2)
        self.assertEqual(merged['patients'],2)
        self.assertEqual(merged['variant_count'],3)
        self.assertEqual(merged['latest_consult'],'2026-03-07')
        self.assertEqual(by_name['안동병원']['referrals'],1)
        self.assertEqual(by_name['안동의료원']['referrals'],1)
        self.assertEqual(by_name['굿모닝병원']['referrals'],1)
        self.assertEqual(len(items),4)
        self.assertEqual(models._fold_hospital_abbreviations({'안동','안동병원','안동의료원'}),{})

    def test_monthly_report_hospital_counts_share_the_same_grouping(self):
        """월간보고서·통계 대시보드의 모병원 집계도 표기 변형을 합친다."""
        db=models.get_db()
        for spelling,day in [('한마음 병원','2026-02-03'),('한마음병원','2026-02-04'),
                             ('한마음병원','2026-02-05'),('한마음병원','2026-01-06')]:
            patient=db.execute("INSERT INTO patients(name) VALUES (?)",(f'표기{day}',)).lastrowid
            db.execute("""INSERT INTO consultations(patient_id,consult_date,admission_status,source_hospital)
                        VALUES (?,?, '입원완료', ?)""",(patient,day,spelling))
        db.commit(); db.close()
        mapping=models.hospital_display_map()
        self.assertEqual(mapping['한마음 병원'],'한마음병원')  # 대표는 DB 전체에서 더 많이 쓰인 표기
        stats=models.aggregate_stats('2026-02-01','2026-02-28')
        labels=[h['label'] for h in stats['by_source_hospital']]
        self.assertIn('한마음병원',labels)
        self.assertNotIn('한마음 병원',labels)
        self.assertEqual(next(h['count'] for h in stats['by_source_hospital'] if h['label']=='한마음병원'),3)
        perf=next(h for h in stats['by_hospital_performance'] if h['label']=='한마음병원')
        self.assertEqual(perf['total'],3)
        self.assertEqual(perf['completed'],3)
        # 1월에 이미 온 병원이라 2월 신규 모병원으로 세면 안 된다 (표기만 달라진 경우 포함)
        self.assertEqual(models._new_hospitals_count(2026,2),0)

    def test_hospital_kind_matches_short_names_to_official_directory(self):
        """상담의 짧은 병원명을 명부의 정식 명칭과 맞춰 종별을 붙인다. 애매하면 비운다."""
        coop.import_facility_directory([
            {'official_code':'A1','name':'의료법인안동병원','kind':'종합병원','region':'경북','address':'안동'},
            {'official_code':'A2','name':'의료법인 안동의료재단 용상안동병원','kind':'정신병원','region':'경북','address':'안동'},
            {'official_code':'B1','name':'부산성소병원','kind':'병원','region':'부산','address':'부산'},
            {'official_code':'B2','name':'안동성소병원','kind':'종합병원','region':'경북','address':'안동'},
            {'official_code':'C1','name':'연세대학교의과대학 강남세브란스병원','kind':'상급종합','region':'서울','address':'서울'},
            {'official_code':'D1','name':'서울아산병원','kind':'상급종합','region':'서울','address':'서울'},
            {'official_code':'D2','name':'보령아산병원','kind':'종합병원','region':'충남','address':'보령'},
            {'official_code':'E1','name':'경북대학교병원','kind':'상급종합','region':'대구','address':'대구'},
            {'official_code':'F1','name':'계명대학교 동산병원','kind':'상급종합','region':'대구','address':'대구'},
        ],'test')
        db=models.get_db()
        db.execute("INSERT OR IGNORE INTO source_hospitals(name,kind,region,active) VALUES ('길주요양병원','요양병원','경북',1)")
        db.commit(); db.close()
        models._kind_index_cache['stamp']=None   # 색인 새로 만들게
        self.assertEqual(models.hospital_kind('안동병원'),'종합병원')        # 법인명 뗀 정식명 일치 — 용상안동병원에 안 밀린다
        self.assertEqual(models.hospital_kind('성소병원'),'종합병원')        # 부산·안동 갈리면 본원 인근(경북)
        self.assertEqual(models.hospital_kind('강남세브란스병원'),'상급종합')  # 정식명이 짧은 이름으로 끝남
        self.assertIsNone(models.hospital_kind('아산병원'))                 # 서울·보령 종별이 갈리고 인근도 아님 → 비움
        self.assertEqual(models.hospital_kind('경북대병원'),'상급종합')      # 대학병원 줄임말
        self.assertEqual(models.hospital_kind('계명대 동산병원'),'상급종합')
        self.assertEqual(models.hospital_kind('길주요양병원'),'요양병원')    # 명부에 없는 요양병원은 마스터에서
        self.assertIsNone(models.hospital_kind('없는병원'))
        # 지역 접두어를 떼고 찾는다 / 이름 끝이 종별을 말해주면 명부에 없어도 붙인다
        coop.import_facility_directory([{'official_code':'G1','name':'(의)수의료재단 로뎀요양병원','kind':'요양병원','region':'경북','address':'포항'}],'test')
        models._kind_index_cache['stamp']=None
        self.assertEqual(models.hospital_kind('포항 로뎀요양병원'),'요양병원')   # '포항 ' 떼면 (의)·재단 뗀 정식명과 일치
        self.assertEqual(models.hospital_kind('꽃가람요양원'),'요양원')          # 심평원 밖이지만 이름이 말해줌
        self.assertEqual(models.hospital_kind('처음보는요양병원'),'요양병원')
        self.assertIsNone(models.hospital_kind('처음보는병원'))               # '병원'만으로는 종별을 단정하지 않는다
        db=models.get_db()
        pid=db.execute("INSERT INTO patients(name) VALUES ('종별환자')").lastrowid
        db.execute("INSERT INTO consultations(patient_id,consult_date,admission_status,source_hospital) VALUES (?,'2026-06-01','입원완료','안동병원')",(pid,))
        db.commit(); db.close()
        page=self.client.get('/stats/hospitals?preset=custom&from=2026-06-01&to=2026-06-30').get_data(as_text=True)
        self.assertIn('class="kind-badge kind-종합병원"',page)
        self.assertIn('직접 문의',page)
        self.assertNotIn('직접 방문',page)

    def test_linked_referrals_split_and_non_institution_excluded(self):
        """기관연계와 직접 방문을 나눠 세고, '집' 같은 비기관 값은 집계에서 뺀다."""
        db=models.get_db()
        def consult(hospital,day,status,detail):
            pid=db.execute("INSERT INTO patients(name) VALUES (?)",(f'{hospital}{day}',)).lastrowid
            db.execute("""INSERT INTO consultations(patient_id,consult_date,admission_status,
                        source_hospital,referral_source_detail) VALUES (?,?,?,?,?)""",
                       (pid,day,status,hospital,detail))
        consult('연계병원','2026-04-01','입원완료','["기관연계"]')
        consult('연계병원','2026-04-02','상담중','["기관연계"]')
        consult('연계병원','2026-04-03','입원완료','["검색(블로그)"]')
        consult('집','2026-04-04','입원완료','["지인추천"]')
        db.commit(); db.close()
        data=models.hospital_referral_overview('2026-04-01','2026-04-30')
        names=[h['name'] for h in data['hospitals']]
        self.assertNotIn('집',names)          # 방문할 수 없는 값은 기관 집계에서 제외
        h=next(x for x in data['hospitals'] if x['name']=='연계병원')
        self.assertEqual((h['referrals'],h['admissions']),(3,2))
        self.assertEqual((h['linked_referrals'],h['linked_admissions']),(2,1))
        self.assertEqual((h['direct_referrals'],h['direct_admissions']),(1,1))
        self.assertEqual(h['linked_conversion'],50.0)
        self.assertEqual(data['linked_referrals'],2)
        self.assertEqual(data['linked_admissions'],1)
        # 통계 대시보드·월간보고서의 모병원 집계에서도 '집'이 빠진다
        stats=models.aggregate_stats('2026-04-01','2026-04-30')
        self.assertNotIn('집',[x['label'] for x in stats['by_source_hospital']])
        self.assertNotIn('집',[x['label'] for x in stats['by_hospital_performance']])
        page=self.client.get('/stats/hospitals?preset=custom&from=2026-04-01&to=2026-04-30&sort=linked').get_data(as_text=True)
        self.assertIn('기관연계',page)
        self.assertIn('직접 문의',page)
        self.assertNotIn('>집<',page)

    def test_staff_referral_merges_name_variants(self):
        """직원소개 소개자는 관계어·존칭 표기가 달라도 한 사람으로 합친다."""
        db=models.get_db()
        def consult(person,day,status,detail='["직원소개"]'):
            pid=db.execute("INSERT INTO patients(name) VALUES (?)",(f'{person}{day}',)).lastrowid
            db.execute("""INSERT INTO consultations(patient_id,consult_date,admission_status,
                        referrer_person,referral_source_detail) VALUES (?,?,?,?,?)""",
                       (pid,day,status,person,detail))
        for person,day,status in [
            ('김미화팀장 소개','2026-05-02','입원완료'),
            ('김미화팀장 지인','2026-05-03','입원완료'),
            ('김미화팀장님','2026-05-04','상담중'),
            ('이준화부장','2026-05-05','입원완료'),
            ('','2026-05-06','상담중'),                 # 소개자 미기재
        ]:
            consult(person,day,status)
        consult('김미화팀장','2026-05-07','입원완료','["지인추천"]')  # 직원소개가 아니면 제외
        db.commit(); db.close()

        data=models.staff_referral_overview('2026-05-01','2026-05-31')
        by={r['name']:r for r in data['referrers']}
        self.assertEqual(sorted(by),['김미화팀장','이준화부장'])
        merged=by['김미화팀장']
        self.assertEqual((merged['referrals'],merged['admissions']),(3,2))   # 지인추천 건은 빠진다
        self.assertEqual(merged['variant_count'],3)
        self.assertEqual(merged['conversion'],66.7)
        self.assertEqual(data['unnamed']['referrals'],1)                     # 이름 미기재도 총계에는 포함
        self.assertEqual(data['referrals'],5)
        self.assertEqual(models.staff_referrer_key('박세연 지인의 소개'),'박세연')
        self.assertEqual(models.staff_referrer_key('권춘열이사님지인'),'권춘열이사')
        # 검색은 원래 표기로도 찾힌다
        found=models.staff_referral_overview('2026-05-01','2026-05-31',q='김미화')
        self.assertEqual([r['name'] for r in found['referrers']],['김미화팀장'])
        page=self.client.get('/stats/staff?preset=custom&from=2026-05-01&to=2026-05-31').get_data(as_text=True)
        self.assertIn('직원소개 분석',page)
        self.assertIn('김미화팀장',page)
        self.assertIn('표기 3종 통합',page)
        menu=self.client.get('/api/global-search?q=직원소개').get_json()
        self.assertTrue(any(i['url']=='/stats/staff' for i in menu['items']))

    def test_partner_candidates_and_recent_performance(self):
        """실적 있는 미등록 기관을 후보로 제시하고, 등록/보류가 목록에 반영된다."""
        recent=date.today()-timedelta(days=20)
        db=models.get_db()
        def consult(hospital,when,status='입원완료'):
            pid=db.execute("INSERT INTO patients(name) VALUES (?)",(f'{hospital}{when}',)).lastrowid
            db.execute("""INSERT INTO consultations(patient_id,consult_date,admission_status,source_hospital)
                        VALUES (?,?,?,?)""",(pid,when.isoformat(),status,hospital))
        for i in range(6):
            consult('큰유입병원',recent-timedelta(days=i),'입원완료' if i<3 else '상담중')
        for i in range(2):
            consult('적지만입원병원',recent-timedelta(days=i))   # 상담 2건뿐이지만 입원 2건
        consult('한건병원',recent,'상담중')                        # 기준 미달
        consult('대구굿모닝병원',recent)                            # 이미 등록된 기관
        db.commit(); db.close()

        db=models.get_db()
        found,total=coop.partner_candidates(db)
        names=[h['name'] for h in found]
        db.close()
        self.assertIn('큰유입병원',names)
        self.assertIn('적지만입원병원',names)      # 입원 기준(2건)으로 걸린다
        self.assertNotIn('한건병원',names)         # 상담 1건·입원 1건은 양쪽 기준 미달
        self.assertNotIn('대구굿모닝병원',names)   # 이미 협력기관
        self.assertEqual(total,2)

        page=self.client.get('/partners').get_data(as_text=True)
        self.assertIn('협력기관으로 관리해야 할 곳',page)
        self.assertIn('큰유입병원',page)
        # 보류하면 후보에서 사라지고, 되살리면 다시 나온다
        self.client.post('/partners/skip-candidate',data={'csrf':'token','name':'적지만입원병원'})
        db=models.get_db(); found,_=coop.partner_candidates(db); db.close()
        self.assertNotIn('적지만입원병원',[h['name'] for h in found])
        self.client.post('/partners/restore-candidates',data={'csrf':'token'})
        db=models.get_db(); found,_=coop.partner_candidates(db); db.close()
        self.assertIn('적지만입원병원',[h['name'] for h in found])
        # 등록하면 협력기관이 되고 후보에서 빠진다
        response=self.client.post('/partners/add-candidate',data={'csrf':'token','name':'큰유입병원'})
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM cooperation_partners p JOIN source_hospitals h ON h.id=p.hospital_id WHERE h.name='큰유입병원'"),1)
        db=models.get_db(); found,_=coop.partner_candidates(db); db.close()
        self.assertNotIn('큰유입병원',[h['name'] for h in found])
        # 등록된 기관 목록에 최근 실적이 붙고, 접촉 기록이 없으니 방문 필요로 잡힌다
        listing=self.client.get('/partners?sort=performance').get_data(as_text=True)
        self.assertIn('최근 3개월 실적',listing)
        self.assertIn('방문 필요',listing)

    def test_candidate_routes_require_write_permission(self):
        models.set_user_permissions(self.user['id'],{k:(1 if k=='partners' else 0) for k in main.MENU_KEYS})
        with self.client.session_transaction() as s:
            s['perms']={k:(1 if k=='partners' else 0) for k in main.MENU_KEYS}
        for url in ('/partners/add-candidate','/partners/skip-candidate','/partners/restore-candidates'):
            self.assertIn(self.client.post(url,data={'csrf':'token','name':'무권한병원'}).status_code,(302,403))
        # 조회 권한만으로는 아무것도 쓰이지 않아야 한다
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM cooperation_candidate_skips"),0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM source_hospitals WHERE name='무권한병원'"),0)

    def test_directory_keeps_same_name_facilities_separate(self):
        entries=[
            {'official_code':'A1','name':'동명의원','kind':'의원','region':'서울특별시','address':'서울 A','phone':'1'},
            {'official_code':'B2','name':'동명의원','kind':'의원','region':'경상북도','address':'경북 B','phone':'2'},
        ]
        self.assertEqual(coop.import_facility_directory(entries,'test'),2)
        result=self.client.get('/partners/search?q=동명의원').get_json()
        self.assertEqual(len(result['items']),2)
        self.assertEqual({item['address'] for item in result['items']},{'서울 A','경북 B'})
        for item in result['items']:
            response=self.client.post('/partners/add',data={
                'csrf':'token','hospital':item['name'],'hospital_id':item['id']})
            self.assertEqual(response.status_code,302)
        db=models.get_db()
        rows=db.execute("SELECT official_name,directory_id FROM cooperation_partners WHERE official_name='동명의원'").fetchall()
        db.close()
        self.assertEqual(len(rows),2)
        self.assertEqual(len({r['directory_id'] for r in rows}),2)

    def test_official_details_default_list_and_agreements(self):
        coop.import_facility_directory([{
            'official_code':'DETAIL1','name':'상세병원','kind':'병원','region':'대구',
            'address':'대구 주소','phone':'053-000-0000'}],'test')
        coop.import_facility_details(
            departments={'DETAIL1':{'신경과','재활의학과'}}, beds={'DETAIL1':123},
            integrated_codes={'DETAIL1'}, updated_at='테스트 기준')
        result=self.client.get('/partners/search?q=상세병원').get_json()['items'][0]
        self.client.post('/partners/add',data={'csrf':'token','hospital':result['name'],'hospital_id':result['id']})
        db=models.get_db()
        pid=db.execute("SELECT id FROM cooperation_partners WHERE official_name='상세병원'").fetchone()['id']
        db.close()
        page=self.client.get('/partners').get_data(as_text=True)
        self.assertIn('coop-partner-table',page)
        self.assertIn('coop-directory-search',page)
        self.assertNotIn('<details class="card coop-section coop-directory-search"',page)
        self.assertIn('123',page)
        self.assertIn('간호간병통합',page)
        self.assertIn('coop-departments',page)
        response=self.client.post(f'/partners/{pid}/save',data={
            'csrf':'token','action':'agreement','title':'진료협력 업무협약서',
            'signed_on':'2026-01-01','expires_on':'2027-01-01','status':'유효',
            'counterpart':'협력팀장','document_location':'NAS 협약 폴더','notes':'자동연장'})
        self.assertEqual(response.status_code,302)
        detail=self.client.get(f'/partners/{pid}').get_data(as_text=True)
        self.assertIn('진료협력 업무협약서',detail)
        self.assertIn('NAS 협약 폴더',detail)
        self.assertIn('신경과',detail)
        agreements=self.client.get('/partners?tab=agreements').get_data(as_text=True)
        self.assertIn('업무협약 목록',agreements)
        self.assertIn('진료협력 업무협약서',agreements)
        self.assertIn('NAS 협약 폴더',agreements)
        filtered=self.client.get('/partners?tab=agreements&agreement_status=만료').get_data(as_text=True)
        self.assertNotIn('진료협력 업무협약서',filtered)
        invalid=self.client.post(f'/partners/{pid}/save',data={
            'csrf':'token','action':'agreement','title':'잘못된 협약',
            'signed_on':'2027-01-01','expires_on':'2026-01-01','status':'유효'})
        self.assertEqual(invalid.status_code,302)
        db=models.get_db(); count=db.execute('SELECT COUNT(*) FROM cooperation_agreements WHERE partner_id=?',(pid,)).fetchone()[0]; db.close()
        self.assertEqual(count,1)


if __name__=='__main__':unittest.main()
