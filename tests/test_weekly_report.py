"""주간 상담 현황 — 주를 골라 볼 수 있고 기본은 지난주(월~일) (2026-09-21 요청).
집계는 dashboard_summary 에서 떼어낸 models.weekly_report() 하나만 쓴다."""
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import app as main
import models


class WeeklyReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        db = patch.object(models, 'DB_PATH', os.path.join(self.tmp.name, 'test.db'))
        db.start(); self.addCleanup(db.stop)
        models.init_db()
        models.ensure_admin_user('wk', 'test-only-password', display_name='테스트')
        user = models.get_user('wk')
        boot = patch.object(main, '_db_initialized', True)
        boot.start(); self.addCleanup(boot.stop)
        main.app.config.update(TESTING=True)
        self.client = main.app.test_client()
        with self.client.session_transaction() as s:
            s.update(user_id=user['id'], username='wk', role='admin', perms={k: 3 for k in main.MENU_KEYS})
        self.today = date.today()
        self.this_mon = self.today - timedelta(days=self.today.weekday())
        self.last_mon = self.this_mon - timedelta(days=7)
        # 지난주 화요일 1건(입원완료), 이번 주 월요일 1건(상담완료)
        for day, status in ((self.last_mon + timedelta(days=1), '입원완료'), (self.this_mon, '상담완료')):
            pid = models.find_or_create_patient(name=f"주간{day}", guardian_phone=None)
            extra = {'actual_admission_date': (self.last_mon + timedelta(days=2)).isoformat()} if status == '입원완료' else {}
            models.create_consultation(patient_id=pid, consult_date=day.isoformat(), counselor='테스트',
                                       consult_channel='전화상담', admission_status=status, **extra)
        # 상담 없이 원무 명부로만 지난주 목요일에 들어온 사람 — '실제 입원'에는 잡히고 '전환'에는 안 잡혀야 한다
        rpid = models.find_or_create_patient(name="명부만", guardian_phone=None)
        conn = models.get_db()
        with conn:
            conn.execute("""INSERT INTO admission_episodes (patient_id,episode_no,status,admitted_at,room_number,ward,roster_key)
                            VALUES (?,1,'admitted',?,'401호','4병동',?)""",
                         (rpid, (self.last_mon + timedelta(days=3)).isoformat(), f'c{rpid}|x'))

    def test_default_is_last_week_monday_to_sunday(self):
        r = models.weekly_report()
        self.assertEqual(r['week_start'], self.last_mon.isoformat())
        self.assertEqual(r['week_end'], (self.last_mon + timedelta(days=6)).isoformat())
        self.assertEqual(date.fromisoformat(r['week_start']).weekday(), 0)          # 월요일
        t = r['current']['totals']
        self.assertEqual(t['total'], 1)                       # 지난주 상담만
        self.assertEqual(t['admitted'], 2)                    # 실제 입원 = 상담자(수) 1 + 명부만(목) 1 — admission_flow_events 기준
        self.assertEqual(t['conversion'], 1)                  # 상담→입원 전환은 상담자만
        self.assertEqual([d['admitted'] for d in r['current']['days']], [0, 0, 1, 1, 0, 0, 0])
        self.assertEqual(len(r['current']['days']), 7)

    def test_any_weekday_snaps_to_that_weeks_monday(self):
        thursday = self.last_mon + timedelta(days=3)
        r = models.weekly_report(thursday)
        self.assertEqual(r['week_start'], self.last_mon.isoformat())

    def test_page_picker_and_this_week_marked_in_progress(self):
        html = self.client.get('/report/weekly').get_data(as_text=True)
        self.assertIn('class="week-picker', html)
        self.assertIn(f'value="{self.last_mon.isoformat()}"', html)                   # 기본 선택 = 지난주
        self.assertIn(f'{self.last_mon.isoformat()} ~ {(self.last_mon + timedelta(days=6)).isoformat()}', html)
        cur = self.client.get(f'/report/weekly?week={self.this_mon.isoformat()}').get_data(as_text=True)
        self.assertIn('<b>진행 중</b>', cur)                                            # 이번 주는 진행 중 표시
        bad = self.client.get('/report/weekly?week=abc')
        self.assertEqual(bad.status_code, 200)                                          # 잘못된 값은 기본으로
        self.assertIn(f'value="{self.last_mon.isoformat()}"', bad.get_data(as_text=True))

    def test_headline_one_liner_uses_prev_week_wording_and_counts_resistant_per_consult(self):
        """한 줄 총평 — '전주 대비'로 쓴다(보고서가 지난주라 '지난주 대비'는 지지난주로 읽힘).
        내성균은 상담 단위로 센다(경로별 칸을 합치면 경로 2개인 상담이 두 번 잡힌다)."""
        import json
        # 지난주 수요일: 경로 2개(카페+SNS) + CRE → 내성균 상담 '1건'이어야 한다
        pid = models.find_or_create_patient(name="내성균검증", guardian_phone=None)
        models.create_consultation(patient_id=pid, consult_date=(self.last_mon + timedelta(days=2)).isoformat(),
                                   counselor='테스트', consult_channel='전화상담', admission_status='퇴원완료',
                                   referral_source_detail=json.dumps(["카페", "SNS"]),
                                   disease_detail="뇌경색 -OP, VRE 보균")          # 체크박스 아님 — 병명 상세에 글로
        r = models.weekly_report()
        h = r['headline']
        self.assertTrue(h.startswith('상담 2건(전주 대비 +2건), 입원 2명(+2명), 상담→입원 전환 2건(100.0%).'), h)
        self.assertIn('내성균 상담 1건', h)                    # 경로 합(2)이 아니라 상담 수(1), 그리고 disease_detail 에서 잡힘
        self.assertEqual(r['current']['totals']['resistant']['카페'], 1)
        self.assertEqual(r['current']['totals']['resistant']['SNS'], 1)   # 표는 경로별 그대로
        self.assertNotIn('지난주 대비', h)
        self.assertEqual(r['current']['totals']['resistant_total'], 1)
        html = self.client.get('/report/weekly').get_data(as_text=True)
        self.assertIn('class="weekly-headline"', html)
        self.assertIn('<small>전주 ', html)                     # 카드 라벨도 같은 말
        self.assertNotIn('<small>지난주 ', html)
        self.assertIn('const lines=[', html)                    # 요약문 복사 둘째 줄에 총평
        self.assertIn(r'전주 대비', html)   # '전주 대비' (tojson 이스케이프)

    def test_headline_when_week_is_empty(self):
        r = models.weekly_report(self.last_mon - timedelta(days=700))
        self.assertEqual(r['headline'], '이 주에는 상담도 입원도 없었습니다.')

    def test_dashboard_no_longer_computes_weekly_report(self):
        """대시보드가 열릴 때마다 1년치 상담을 읽던 낭비를 없앴다 — 키 자체가 없어야 한다."""
        self.assertNotIn('weekly_report', models.dashboard_summary())


if __name__ == '__main__':
    unittest.main()
