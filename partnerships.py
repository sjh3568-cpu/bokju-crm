"""기관협력: 병원 마스터 연결, 접점 이력, 후속 일정, 기간별 상담·입원 조회."""
import csv
import io
import json
import secrets
import sqlite3
from contextlib import closing
from datetime import date, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for, Response, jsonify
import models
from auth import login_required, current_user

bp = Blueprint('partners', __name__)
KINDS = ('방문', '전화', '문자', '기타')
CYCLE_PRESETS = (('매주','7'),('2주','14'),('1개월','m1'),('2개월','m2'),('분기','m3'),('반기','m6'),('매년','m12'))


def _next_cycle(base, days, months):
    if months:
        import calendar
        y,m=divmod(base.month-1+months,12)
        y,m=base.year+y,m+1
        return date(y,m,min(base.day,calendar.monthrange(y,m)[1]))
    return base+timedelta(days=days) if days else None


def _cycle_values(prefix):
    preset=_text(prefix+'_preset',limit=20)
    if preset=='off': return None,None
    if preset.startswith('m') and preset in {value for _,value in CYCLE_PRESETS}:
        return None,int(preset[1:])
    if preset in ('7','14'): return int(preset),None
    return _cycle(prefix+'_cycle'),None


def _cycle_label(days, months):
    if months: return {1:'매월',2:'2개월',3:'분기',6:'반기',12:'매년'}.get(months,f'{months}개월')
    return f'{days}일' if days else '미설정'



def init_schema():
    with closing(models.get_db()) as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS cooperation_partners (
            id INTEGER PRIMARY KEY, hospital_id INTEGER NOT NULL UNIQUE REFERENCES source_hospitals(id),
            important INTEGER NOT NULL DEFAULT 0, owner TEXT NOT NULL DEFAULT '',
            visit_cycle INTEGER, contact_cycle INTEGER, notes TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS cooperation_contacts (
            id INTEGER PRIMARY KEY, partner_id INTEGER NOT NULL REFERENCES cooperation_partners(id),
            name TEXT NOT NULL, department TEXT NOT NULL DEFAULT '', position TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS cooperation_activities (
            id INTEGER PRIMARY KEY, partner_id INTEGER NOT NULL REFERENCES cooperation_partners(id),
            happened_on TEXT NOT NULL, kind TEXT NOT NULL, met TEXT NOT NULL DEFAULT '',
            owner TEXT NOT NULL DEFAULT '', content TEXT NOT NULL, created_by INTEGER REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS cooperation_tasks (
            id INTEGER PRIMARY KEY, partner_id INTEGER NOT NULL REFERENCES cooperation_partners(id),
            due_on TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '',
            completed_on TEXT, created_by INTEGER REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS cooperation_activity_date ON cooperation_activities(partner_id,happened_on);
        CREATE INDEX IF NOT EXISTS cooperation_task_due ON cooperation_tasks(completed_on,due_on);
        CREATE INDEX IF NOT EXISTS cooperation_referrer ON consultations(referrer_institution,consult_date);
        CREATE TABLE IF NOT EXISTS cooperation_facility_directory (
            id INTEGER PRIMARY KEY, official_code TEXT UNIQUE, name TEXT NOT NULL,
            kind TEXT, region TEXT, address TEXT, phone TEXT, source TEXT,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS cooperation_directory_name ON cooperation_facility_directory(name);
        CREATE INDEX IF NOT EXISTS cooperation_directory_kind_region ON cooperation_facility_directory(kind,region);
        CREATE TABLE IF NOT EXISTS cooperation_agreements (
            id INTEGER PRIMARY KEY, partner_id INTEGER NOT NULL REFERENCES cooperation_partners(id),
            title TEXT NOT NULL, signed_on TEXT NOT NULL, expires_on TEXT,
            status TEXT NOT NULL DEFAULT '유효', document_location TEXT NOT NULL DEFAULT '',
            counterpart TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
            created_by INTEGER REFERENCES users(id), created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS cooperation_agreement_partner ON cooperation_agreements(partner_id,signed_on);
        CREATE TABLE IF NOT EXISTS cooperation_documents (
            id INTEGER PRIMARY KEY, partner_id INTEGER NOT NULL REFERENCES cooperation_partners(id),
            category TEXT NOT NULL, title TEXT NOT NULL, location TEXT NOT NULL DEFAULT '',
            expires_on TEXT, notes TEXT NOT NULL DEFAULT '', created_by INTEGER REFERENCES users(id),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS cooperation_visit_plans (
            id INTEGER PRIMARY KEY, partner_id INTEGER NOT NULL REFERENCES cooperation_partners(id),
            visit_on TEXT NOT NULL, sequence INTEGER NOT NULL DEFAULT 1, purpose TEXT NOT NULL,
            owner TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '예정', notes TEXT NOT NULL DEFAULT '',
            created_by INTEGER REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS cooperation_candidate_skips (
            name TEXT PRIMARY KEY, skipped_on TEXT NOT NULL, created_by INTEGER REFERENCES users(id)
        );
        ''')
        models._ensure_columns(db, 'cooperation_partners', {
            'specialties': "TEXT NOT NULL DEFAULT ''", 'strengths': "TEXT NOT NULL DEFAULT ''",
            'visit_months': 'INTEGER', 'contact_months': 'INTEGER', 'remind_days': 'INTEGER NOT NULL DEFAULT 7',
            'directory_id': 'INTEGER', 'official_name': 'TEXT'
            ,'relationship_stage': "TEXT NOT NULL DEFAULT '신규'"
        })
        models._ensure_columns(db, 'cooperation_contacts', {
            'status': "TEXT NOT NULL DEFAULT '재직'", 'is_primary': 'INTEGER NOT NULL DEFAULT 0'
        })
        models._ensure_columns(db, 'cooperation_facility_directory', {
            'departments': "TEXT NOT NULL DEFAULT ''", 'bed_count': 'INTEGER',
            'integrated_nursing': 'INTEGER', 'detail_updated_at': 'TEXT'
        })
        for name in ('대구굿모닝병원', '에스포항병원'):
            db.execute('''INSERT OR IGNORE INTO cooperation_partners(hospital_id,important)
                          SELECT id,1 FROM source_hospitals WHERE name=?''', (name,))
        # 공식명과 일치하는 기관이 명부에 하나뿐이면 기존 협력기관도 주소·종별과 연결한다.
        db.execute('''UPDATE cooperation_partners SET
            directory_id=(SELECT MIN(d.id) FROM cooperation_facility_directory d
                          JOIN source_hospitals h ON h.id=cooperation_partners.hospital_id
                          WHERE d.name=h.name),
            official_name=(SELECT h.name FROM source_hospitals h WHERE h.id=cooperation_partners.hospital_id)
            WHERE directory_id IS NULL AND 1=(SELECT COUNT(*) FROM cooperation_facility_directory d
                JOIN source_hospitals h ON h.id=cooperation_partners.hospital_id WHERE d.name=h.name)''')
        db.commit()


def import_facility_directory(entries, source='hira'):
    """공식기관코드 기준 검색 명부를 갱신한다. 병원명 중복은 합치지 않는다."""
    cleaned=[]
    for item in entries or []:
        code=(item.get('official_code') or '').strip()
        name=(item.get('name') or '').strip()
        if not code or not name:
            continue
        cleaned.append((code,name,(item.get('kind') or '').strip() or None,
                        (item.get('region') or '').strip() or None,
                        (item.get('address') or '').strip() or None,
                        (item.get('phone') or '').strip() or None,source))
    with closing(models.get_db()) as db:
        db.executemany('''INSERT INTO cooperation_facility_directory
            (official_code,name,kind,region,address,phone,source,active)
            VALUES (?,?,?,?,?,?,?,1)
            ON CONFLICT(official_code) DO UPDATE SET name=excluded.name,kind=excluded.kind,
            region=excluded.region,address=excluded.address,phone=excluded.phone,
            source=excluded.source,active=1''',cleaned)
        db.commit()
    return len(cleaned)


def import_facility_details(*, departments=None, beds=None, integrated_codes=None, updated_at=None):
    """공식기관코드에 진료과목·병상수·간호간병통합서비스 여부를 연결한다."""
    departments=departments or {}
    beds=beds or {}
    integrated_codes=set(integrated_codes or [])
    codes=set(departments)|set(beds)|integrated_codes
    rows=[]
    for code in codes:
        names=sorted({str(name).strip() for name in departments.get(code,[]) if str(name).strip()})
        rows.append((', '.join(names),beds.get(code),1 if code in integrated_codes else 0,
                     updated_at,code))
    with closing(models.get_db()) as db:
        db.executemany('''UPDATE cooperation_facility_directory
            SET departments=?,bed_count=?,integrated_nursing=?,detail_updated_at=?
            WHERE official_code=?''',rows)
        db.commit()
    return len(rows)


def _audit(action, partner_id=None):
    models.log_audit(user_id=g.user['id'], username=g.user['username'], action=action,
                     target_type='cooperation', target_id=partner_id, ip=request.remote_addr)


def _text(key, required=False, limit=4000):
    value = request.form.get(key, '').strip()
    if (required and not value) or len(value) > limit:
        raise ValueError('필수 항목과 입력 길이를 확인해주세요.')
    return value


def _date(value, optional=False):
    if not value and optional:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except (ValueError, TypeError):
        raise ValueError('날짜를 올바르게 입력해주세요.')


def _cycle(key):
    value = _text(key)
    if not value:
        return None
    try:
        value = int(value)
    except ValueError:
        raise ValueError('주기는 1~730일로 입력해주세요.')
    if not 1 <= value <= 730:
        raise ValueError('주기는 1~730일로 입력해주세요.')
    return value


def _partner(db, pid):
    row = db.execute('''SELECT p.*,COALESCE(p.official_name,h.name) name,
                        COALESCE(d.kind,h.kind) kind,COALESCE(d.region,h.region) region,
                        COALESCE(d.address,h.address) address,COALESCE(d.phone,h.phone) phone,d.departments,d.bed_count,
                        d.integrated_nursing,d.detail_updated_at
                        FROM cooperation_partners p JOIN source_hospitals h ON h.id=p.hospital_id
                        LEFT JOIN cooperation_facility_directory d ON d.id=p.directory_id
                        WHERE p.id=?''', (pid,)).fetchone()
    if row is None:
        abort(404)
    return dict(row)


def _csrf():
    token = session.setdefault('cooperation_csrf', secrets.token_hex(32))
    return token


@bp.before_request
def protect_writes():
    if request.method == 'POST':
        user = current_user()
        if not user:
            abort(401)
        if user.get('perms', {}).get('partners', 0) < 2:
            abort(403)
        if not secrets.compare_digest(session.get('cooperation_csrf', ''), request.form.get('csrf', '')) or not request.form.get('csrf'):
            abort(400)


def reminders(limit=None):
    with closing(models.get_db()) as db:
        rows = db.execute('''SELECT t.*,h.name,p.important,p.remind_days FROM cooperation_tasks t
            JOIN cooperation_partners p ON p.id=t.partner_id
            JOIN source_hospitals h ON h.id=p.hospital_id
            WHERE t.completed_on IS NULL AND t.due_on<=date(?, '+' || p.remind_days || ' days')
            ORDER BY t.due_on,p.important DESC,h.name,t.id''',
            (date.today().isoformat(),)).fetchall()
    result = [dict(r) for r in rows]
    for r in result:
        r['delta'] = (date.today()-date.fromisoformat(r['due_on'])).days
        r['dday'] = '오늘' if r['delta']==0 else f"D{r['delta']:+d}"
    return result if limit is None else result[:limit]


@bp.app_context_processor
def dashboard_reminders():
    user = current_user()
    if request.path == '/' and user and user.get('perms', {}).get('partners', 0) >= 1:
        return {'cooperation_reminders': reminders(),'cooperation_csrf':_csrf()}
    return {}


def _period():
    today = date.today()
    preset = request.args.get('period', 'month')
    if preset == 'all':
        return '', '', preset
    if preset == 'year':
        return today.replace(month=1,day=1).isoformat(), today.isoformat(), preset
    if preset == 'last_month':
        end=today.replace(day=1)-timedelta(days=1)
        return end.replace(day=1).isoformat(), end.isoformat(), preset
    if preset == 'custom':
        start=_date(request.args.get('start'), True) or ''
        end=_date(request.args.get('end'), True) or ''
        if start and end and start>end:
            raise ValueError('조회 시작일은 종료일보다 늦을 수 없습니다.')
        return start,end,preset
    return today.replace(day=1).isoformat(),today.isoformat(),'month'


# 협력기관 후보 기준 — 최근 1년 상담 5건 이상 '또는' 입원 2건 이상.
# 상담 건수만 보면 문경제일병원(상담 12·입원 7·전환율 58%)처럼 적게 보내도
# 확실히 입원으로 이어지는 기관을 놓친다. 두 조건은 OR로 건다.
CANDIDATE_MIN_REFERRALS = 5
CANDIDATE_MIN_ADMISSIONS = 2
# 비기관 값('집' 등) 제외는 models.is_institution_source가 단일 기준이다.
CANDIDATE_WINDOW_DAYS = 365
PERFORMANCE_WINDOW_DAYS = 91  # 최근 3개월


def _registered_names(db):
    """등록된 협력기관 이름을 모병원 표기 통합 기준의 대표명으로 바꾼 집합.

    '대구굿모닝병원'으로 등록해 두고 상담에는 '대구 굿모닝병원'으로 적혀 있으면
    이름만 비교했을 때 미등록으로 잘못 잡힌다.
    """
    mapping = models.hospital_display_map()
    rows = db.execute('''SELECT COALESCE(p.official_name,h.name) name FROM cooperation_partners p
                         JOIN source_hospitals h ON h.id=p.hospital_id''')
    return {mapping.get(r['name'], r['name']) for r in rows}


def partner_candidates(db, limit=20):
    """실적은 있는데 협력기관으로 등록되지 않은 모병원.

    등록이 담당자 기억에만 의존하면 실제로 환자를 보내주는 기관이 목록에서 통째로
    빠진다 — 도입 시점 기준 상담의 98%가 미등록 기관에서 왔다. 데이터가 후보를
    먼저 제시하고, 등록 여부는 담당자가 판단한다(자동 등록하지 않는다).
    """
    start = (date.today() - timedelta(days=CANDIDATE_WINDOW_DAYS)).isoformat()
    data = models.hospital_referral_overview(start, date.today().isoformat())
    registered = _registered_names(db)
    skipped = {r['name'] for r in db.execute('SELECT name FROM cooperation_candidate_skips')}
    found = [h for h in data['hospitals']
             if h['name'] not in registered and h['name'] not in skipped
             and (h['referrals'] >= CANDIDATE_MIN_REFERRALS
                  or h['admissions'] >= CANDIDATE_MIN_ADMISSIONS)]
    # 방문 우선순위는 상담량보다 실제 입원 기여가 앞선다.
    found.sort(key=lambda h: (-h['admissions'], -h['referrals'], h['name']))
    return found[:limit], len(found)


def recent_performance():
    """최근 3개월 모병원 실적 — 등록 기관 목록에 붙여 '실적 대비 방치'를 드러낸다."""
    start = (date.today() - timedelta(days=PERFORMANCE_WINDOW_DAYS)).isoformat()
    data = models.hospital_referral_overview(start, date.today().isoformat())
    return models.hospital_display_map(), {h['name']: h for h in data['hospitals']}


def patient_report(db, name, basis, start, end):
    # 실제 추천기관과 이전 병원은 서로 대체하지 않는다.
    field = 'source_hospital' if basis == 'source' else 'referrer_institution'
    rows = db.execute(f'''SELECT c.id,c.patient_id,c.consult_date,c.admission_status,
        c.primary_diagnosis,c.diseases,c.attending_doctor,c.discharge_date,
        COALESCE(NULLIF(TRIM(c.actual_admission_date),''),NULLIF(TRIM(c.admission_date),'')) admitted_on,
        p.name patient_name FROM consultations c JOIN patients p ON p.id=c.patient_id
        WHERE TRIM(c.{field})=? ORDER BY c.consult_date DESC,c.id DESC''', (name,)).fetchall()
    rows=[dict(r) for r in rows]
    for r in rows:
        try:
            diseases=json.loads(r.get('diseases') or '[]')
        except (ValueError,TypeError):
            diseases=[]
        r['diagnosis_label']=(r.get('primary_diagnosis') or '').strip()
        if not r['diagnosis_label'] and isinstance(diseases,list):
            labels=[str(v) for v in diseases if str(v).strip()]
            r['diagnosis_label']=('질환: '+', '.join(labels)) if labels else '미기재'
    def inside(d):
        return bool(d) and (not start or d>=start) and (not end or d<=end)
    consultations=[dict(r) for r in rows if inside(r['consult_date'])]
    # 같은 환자의 같은 입원일은 한 입원으로 집계. 재입원은 다른 입원일로 집계한다.
    admissions={}
    for r in rows:
        if r['admission_status'] not in ('입원완료','퇴원완료') or not inside(r['admitted_on']):
            continue
        admissions.setdefault((r['patient_id'],r['admitted_on']),dict(r))
    return consultations, sorted(admissions.values(),key=lambda r:(r['admitted_on'],r['id']),reverse=True)


def undated_admissions(db, name, basis):
    field='source_hospital' if basis=='source' else 'referrer_institution'
    return [dict(r) for r in db.execute(f"""SELECT c.id,p.name patient_name,c.consult_date,
        c.primary_diagnosis,c.admission_status FROM consultations c JOIN patients p ON p.id=c.patient_id
        WHERE TRIM(c.{field})=? AND c.admission_status IN ('입원완료','퇴원완료')
        AND COALESCE(NULLIF(TRIM(c.actual_admission_date),''),NULLIF(TRIM(c.admission_date),'')) IS NULL
        ORDER BY c.consult_date DESC,c.id DESC""",(name,))]


@bp.route('/partners')
@login_required
def index():
    q=request.args.get('q','').strip()
    user_prefs=models.get_user_by_id(g.user['id']).get('preferences_data',{})
    tab='agreements' if request.args.get('tab')=='agreements' else 'institutions'
    with closing(models.get_db()) as db:
        rows=db.execute('''SELECT p.*,COALESCE(p.official_name,h.name) name,
          COALESCE(d.kind,h.kind) kind,COALESCE(d.region,h.region) region,
          COALESCE(d.address,h.address) address,d.departments,d.bed_count,d.integrated_nursing,
          (SELECT COUNT(*) FROM cooperation_agreements ag WHERE ag.partner_id=p.id) agreement_count,
          (SELECT COUNT(*) FROM cooperation_agreements ag WHERE ag.partner_id=p.id
             AND ag.status='유효' AND (ag.expires_on IS NULL OR ag.expires_on='' OR ag.expires_on>=date('now'))) active_agreement_count,
          (SELECT MAX(happened_on) FROM cooperation_activities a WHERE a.partner_id=p.id AND a.kind='방문') last_visit,
          (SELECT MAX(happened_on) FROM cooperation_activities a WHERE a.partner_id=p.id AND a.kind<>'방문') last_contact,
          (SELECT content FROM cooperation_activities a WHERE a.partner_id=p.id ORDER BY happened_on DESC,id DESC LIMIT 1) last_content,
          (SELECT MIN(due_on) FROM cooperation_tasks t WHERE t.partner_id=p.id AND t.completed_on IS NULL) next_due,
          (SELECT COUNT(*) FROM cooperation_contacts c WHERE c.partner_id=p.id AND c.status='재직') contact_count
          FROM cooperation_partners p JOIN source_hospitals h ON h.id=p.hospital_id
          LEFT JOIN cooperation_facility_directory d ON d.id=p.directory_id
          WHERE (h.name LIKE ? OR p.specialties LIKE ? OR p.strengths LIKE ?) ORDER BY p.important DESC,h.name''', tuple('%'+q+'%' for _ in range(3))).fetchall()
        partners=[dict(r) for r in rows]
        directory_count=db.execute('SELECT COUNT(*) FROM cooperation_facility_directory WHERE active=1').fetchone()[0]
        master_table='cooperation_facility_directory' if directory_count else 'source_hospitals'
        master_count=directory_count or db.execute('SELECT COUNT(*) FROM source_hospitals WHERE active=1').fetchone()[0]
        master_kinds=[r[0] for r in db.execute(f"SELECT DISTINCT kind FROM {master_table} WHERE active=1 AND kind IS NOT NULL ORDER BY kind")]
        master_regions=[r[0] for r in db.execute(f"SELECT DISTINCT region FROM {master_table} WHERE active=1 AND region IS NOT NULL ORDER BY region")]
        agreement_rows=db.execute('''SELECT ag.*,p.id partner_id,
            COALESCE(p.official_name,h.name) partner_name
            FROM cooperation_agreements ag JOIN cooperation_partners p ON p.id=ag.partner_id
            JOIN source_hospitals h ON h.id=p.hospital_id
            ORDER BY ag.signed_on DESC,ag.id DESC''').fetchall()
        agreements=[dict(row) for row in agreement_rows]
        agreement_total=len(agreements)
    partner_kinds=sorted({p['kind'] for p in partners if p['kind']})
    partner_regions=sorted({p['region'] for p in partners if p['region']})
    perf_map,perf_stats=recent_performance()
    for p in partners:
        for key in ('last_visit','last_contact'):
            p[key+'_days']=(date.today()-date.fromisoformat(p[key])).days if p[key] else None
        p['visit_cycle_label']=_cycle_label(p['visit_cycle'],p['visit_months'])
        p['contact_cycle_label']=_cycle_label(p['contact_cycle'],p['contact_months'])
        # 최근 3개월 실적 — 표기 통합 기준으로 상담 데이터와 연결한다.
        stat=perf_stats.get(perf_map.get(p['name'],p['name'])) or {}
        p['recent_referrals']=stat.get('referrals',0)
        p['recent_admissions']=stat.get('admissions',0)
        last=max(filter(None,(p['last_visit'],p['last_contact'])),default=None)
        p['inactive_days']=(date.today()-date.fromisoformat(last)).days if last else None
        # 실적이 있는데 접촉이 끊긴 곳 = 방문 우선순위. 기록이 아예 없으면 가장 위.
        p['neglected']=bool(p['recent_referrals']) and (p['inactive_days'] is None or p['inactive_days']>=60)
    due=request.args.get('due','')
    cycle_type=request.args.get('cycle_type','visit')
    cycle_filter=request.args.get('cycle','')
    start,end=request.args.get('from',''),request.args.get('to','')
    try:
        start,end=_date(start,True) or '',_date(end,True) or ''
        if start and end and start>end: raise ValueError('날짜 순서를 확인해주세요.')
    except ValueError as e:
        flash(str(e),'error');start,end='',''
    today=date.today().isoformat()
    selected=[]
    for p in partners:
        kind_filter=request.args.get('partner_kind','')
        region_filter=request.args.get('partner_region','')
        nursing_filter=request.args.get('nursing','')
        agreement_filter=request.args.get('agreement','')
        stage_filter=request.args.get('stage','')
        quality_filter=request.args.get('quality','')
        if kind_filter and p['kind']!=kind_filter: continue
        if region_filter and p['region']!=region_filter: continue
        if nursing_filter in ('1','0') and p['integrated_nursing'] != int(nursing_filter): continue
        if nursing_filter=='unknown' and p['integrated_nursing'] is not None: continue
        if agreement_filter=='active' and not p['active_agreement_count']: continue
        if agreement_filter=='none' and p['agreement_count']: continue
        if agreement_filter=='ended' and (not p['agreement_count'] or p['active_agreement_count']): continue
        if stage_filter and p['relationship_stage']!=stage_filter: continue
        missing_quality=not p['owner'] or not p['contact_count'] or not (p['visit_cycle'] or p['visit_months']) or not p['strengths']
        if quality_filter=='missing' and not missing_quality: continue
        if request.args.get('important')=='1' and not p['important']: continue
        if due=='overdue' and not (p['next_due'] and p['next_due']<today): continue
        if due=='today' and p['next_due']!=today: continue
        if due in ('7','30') and not (p['next_due'] and today<=p['next_due']<=(date.today()+timedelta(days=int(due))).isoformat()): continue
        if due=='unset' and p['next_due']: continue
        if start and (not p['next_due'] or p['next_due']<start): continue
        if end and (not p['next_due'] or p['next_due']>end): continue
        prefix='contact' if cycle_type=='contact' else 'visit'
        if cycle_filter=='off' and (p[prefix+'_cycle'] or p[prefix+'_months']): continue
        if cycle_filter.startswith('m') and str(p[prefix+'_months'])!=cycle_filter[1:]: continue
        if cycle_filter.isdigit() and str(p[prefix+'_cycle'])!=cycle_filter: continue
        selected.append(p)
    agreement_q=request.args.get('agreement_q','').strip().lower()
    agreement_status=request.args.get('agreement_status','')
    today_iso=date.today().isoformat()
    for agreement in agreements:
        agreement['effective']=agreement['status']=='유효' and (not agreement['expires_on'] or agreement['expires_on']>=today_iso)
        agreement['display_status']='유효' if agreement['effective'] else ('만료' if agreement['status']=='유효' else '종료')
    agreements=[agreement for agreement in agreements
        if (not agreement_q or agreement_q in ' '.join((agreement['partner_name'] or '',agreement['title'] or '',agreement['counterpart'] or '',agreement['document_location'] or '')).lower())
        and (not agreement_status or agreement['display_status']==agreement_status)]
    attention=[p for p in selected
               if (p['important'] or p['neglected'])
               and (p['inactive_days'] is None or p['inactive_days']>=60)]
    attention.sort(key=lambda p:(-p['recent_referrals'],-(p['inactive_days'] or 9999)))
    sort=request.args.get('sort','')
    if sort=='performance':
        selected.sort(key=lambda p:(-p['recent_admissions'],-p['recent_referrals'],p['name']))
    elif sort=='neglected':
        selected.sort(key=lambda p:(not p['neglected'],-p['recent_referrals'],p['name']))
    with closing(models.get_db()) as db:
        candidates,candidate_total=partner_candidates(db)
        skipped_total=db.execute('SELECT COUNT(*) FROM cooperation_candidate_skips').fetchone()[0]
    _audit('view_cooperation')
    return render_template('partners.html',partners=selected,q=q,master_count=master_count,
        master_kinds=master_kinds,master_regions=master_regions,
        partner_kinds=partner_kinds,partner_regions=partner_regions,
        reminders=reminders(),csrf=_csrf(),view=('feed' if request.args.get('view',user_prefs.get('partner_view','list'))=='feed' else 'list'),
        due=due,cycle_type=cycle_type,cycle_filter=cycle_filter,cycle_presets=CYCLE_PRESETS,start=start,end=end,
        tab=tab,agreements=agreements,agreement_total=agreement_total,attention=attention,
        agreement_q=agreement_q,agreement_status=agreement_status,sort=sort,
        candidates=candidates,candidate_total=candidate_total,skipped_total=skipped_total,
        candidate_min_referrals=CANDIDATE_MIN_REFERRALS,candidate_min_admissions=CANDIDATE_MIN_ADMISSIONS)



@bp.route('/partners/search')
@login_required
def search():
    q=request.args.get('q','').strip()[:100]
    if not q: return jsonify(items=[],more=False)
    terms=q.split()[:5]
    conditions=["h.active=1"]
    args=[]
    for term in terms:
        conditions.append("(h.name LIKE ? ESCAPE '\\' OR h.address LIKE ? ESCAPE '\\' OR h.region LIKE ? ESCAPE '\\')")
        term=term.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        args.extend(['%'+term+'%']*3)
    for key in ('kind','region'):
        value=request.args.get(key,'').strip()
        if value: conditions.append('h.'+key+'=?');args.append(value)
    try: offset=max(0,min(int(request.args.get('offset','0')),100000))
    except ValueError: offset=0
    with closing(models.get_db()) as db:
        # 공식기관코드 기반 명부는 동명 의원도 주소별로 각각 유지한다.
        directory_count=db.execute('SELECT COUNT(*) FROM cooperation_facility_directory WHERE active=1').fetchone()[0]
        if directory_count:
            directory_conditions=[condition.replace('h.','d.') for condition in conditions]
            rows=db.execute("""SELECT d.id,d.name,d.kind,d.region,d.address,d.phone,
                (SELECT p.id FROM cooperation_partners p JOIN source_hospitals h ON h.id=p.hospital_id
                 WHERE p.directory_id=d.id OR (p.directory_id IS NULL AND COALESCE(p.official_name,h.name)=d.name)
                 ORDER BY p.id LIMIT 1) partner_id
                FROM cooperation_facility_directory d WHERE """+' AND '.join(directory_conditions)+" ORDER BY d.name,d.address,d.id LIMIT 21 OFFSET ?",(*args,offset)).fetchall()
        else:
            rows=db.execute("SELECT h.id,h.name,h.kind,h.region,h.address,h.phone,p.id partner_id FROM source_hospitals h LEFT JOIN cooperation_partners p ON p.hospital_id=h.id WHERE "+' AND '.join(conditions)+" ORDER BY h.name,h.id LIMIT 21 OFFSET ?",(*args,offset)).fetchall()
    return jsonify(items=[dict(r) for r in rows[:20]],more=len(rows)>20)


@bp.route('/partners/add',methods=['POST'])
@login_required
def add():
    try:
        name=_text('hospital',True,200)
    except ValueError as e:
        flash(str(e),'error')
        return redirect(url_for('partners.index'))
    with closing(models.get_db()) as db:
        directory_id=request.form.get('hospital_id','')
        directory=db.execute('SELECT * FROM cooperation_facility_directory WHERE id=? AND active=1',(directory_id,)).fetchone() if directory_id else None
        if directory and directory['name'] == name:
            # 기존 상담 입력과 연결할 병원명 마스터는 공식명을 유지한다. 동명 기관은
            # directory_id로 구분되며 기관협력 카드에는 공식명·주소가 표시된다.
            db.execute('INSERT OR IGNORE INTO source_hospitals(name,kind,region,address,phone,active) VALUES (?,?,?,?,?,1)',
                       (directory['name'],directory['kind'],directory['region'],directory['address'],directory['phone']))
            h=db.execute('SELECT id FROM source_hospitals WHERE name=?',(directory['name'],)).fetchone()
            existing=db.execute('SELECT id,directory_id FROM cooperation_partners WHERE hospital_id=?',(h['id'],)).fetchone()
            if existing and not existing['directory_id']:
                db.execute('UPDATE cooperation_partners SET directory_id=?,official_name=? WHERE id=?',
                           (directory['id'],directory['name'],existing['id']))
                db.commit()
                _audit('update_cooperation',existing['id'])
                return redirect(url_for('partners.detail',pid=existing['id']))
            if existing:
                internal_name=f"{directory['name']} [{directory['official_code'] or directory['id']}]"
                db.execute('INSERT OR IGNORE INTO source_hospitals(name,kind,region,address,phone,active) VALUES (?,?,?,?,?,1)',
                           (internal_name,directory['kind'],directory['region'],directory['address'],directory['phone']))
                h=db.execute('SELECT id FROM source_hospitals WHERE name=?',(internal_name,)).fetchone()
        else:
            h=db.execute("SELECT id FROM source_hospitals WHERE name=? AND active=1 AND (?='' OR id=?)",(name,directory_id,directory_id)).fetchone()
        if not h:
            flash('병원 마스터에 등록된 정식 병원명을 선택해주세요.','error')
            return redirect(url_for('partners.index'))
        if directory:
            db.execute('INSERT OR IGNORE INTO cooperation_partners(hospital_id,directory_id,official_name) VALUES (?,?,?)',(h['id'],directory['id'],directory['name']))
        else:
            db.execute('INSERT OR IGNORE INTO cooperation_partners(hospital_id) VALUES (?)',(h['id'],))
        db.commit()
        pid=db.execute('SELECT id FROM cooperation_partners WHERE directory_id=?',(directory['id'],)).fetchone()['id'] if directory else db.execute('SELECT id FROM cooperation_partners WHERE hospital_id=?',(h['id'],)).fetchone()['id']
    _audit('update_cooperation',pid)
    return redirect(url_for('partners.detail',pid=pid,embedded='1' if request.values.get('embedded')=='1' else None))


@bp.route('/partners/add-manual',methods=['POST'])
@login_required
def add_manual():
    if current_user().get('perms',{}).get('partners',0)<2:
        abort(403)
    if request.form.get('csrf') != session.get('cooperation_csrf'):
        abort(400)
    try:
        name=_text('name',True,200)
        kind=_text('kind',True,60)
        region=_text('region',limit=100)
        address=_text('address',limit=300)
        phone=_text('phone',limit=100)
    except ValueError as e:
        flash(str(e),'error')
        return redirect(url_for('partners.index')+'#manual-institution')
    with closing(models.get_db()) as db:
        existing=db.execute('SELECT id FROM source_hospitals WHERE name=?',(name,)).fetchone()
        if existing:
            h_id=existing['id']
            db.execute('''UPDATE source_hospitals SET kind=COALESCE(NULLIF(?,''),kind),
                region=COALESCE(NULLIF(?,''),region),address=COALESCE(NULLIF(?,''),address),
                phone=COALESCE(NULLIF(?,''),phone),active=1 WHERE id=?''',(kind,region,address,phone,h_id))
        else:
            h_id=db.execute('''INSERT INTO source_hospitals(name,kind,region,address,phone,active)
                VALUES (?,?,?,?,?,1)''',(name,kind,region,address,phone)).lastrowid
        db.execute('INSERT OR IGNORE INTO cooperation_partners(hospital_id,official_name) VALUES (?,?)',(h_id,name))
        db.commit()
        pid=db.execute('SELECT id FROM cooperation_partners WHERE hospital_id=?',(h_id,)).fetchone()['id']
    _audit('update_cooperation',pid)
    flash('기관을 협력기관으로 등록했습니다.','success')
    return redirect(url_for('partners.detail',pid=pid))


@bp.route('/partners/add-candidate',methods=['POST'])
@login_required
def add_candidate():
    """후보 목록에서 바로 협력기관으로 등록. 마스터에 없는 병원명이면 함께 만든다."""
    if current_user().get('perms',{}).get('partners',0)<2:
        abort(403)
    if request.form.get('csrf') != session.get('cooperation_csrf'):
        abort(400)
    try:
        name=_text('name',True,200)
    except ValueError as e:
        flash(str(e),'error')
        return redirect(url_for('partners.index'))
    with closing(models.get_db()) as db:
        row=db.execute('SELECT id FROM source_hospitals WHERE name=?',(name,)).fetchone()
        if row:
            h_id=row['id']
            db.execute('UPDATE source_hospitals SET active=1 WHERE id=?',(h_id,))
        else:
            h_id=db.execute('INSERT INTO source_hospitals(name,active) VALUES (?,1)',(name,)).lastrowid
        db.execute('INSERT OR IGNORE INTO cooperation_partners(hospital_id,official_name) VALUES (?,?)',(h_id,name))
        db.execute('DELETE FROM cooperation_candidate_skips WHERE name=?',(name,))
        db.commit()
        pid=db.execute('SELECT id FROM cooperation_partners WHERE hospital_id=?',(h_id,)).fetchone()['id']
    _audit('update_cooperation',pid)
    flash(f'{name}을(를) 협력기관으로 등록했습니다. 담당자·방문 주기를 채워주세요.','success')
    return redirect(url_for('partners.detail',pid=pid))


@bp.route('/partners/skip-candidate',methods=['POST'])
@login_required
def skip_candidate():
    """후보에서 제외(보류). 관리 대상이 아닌 기관이 매번 다시 뜨지 않게 한다."""
    if current_user().get('perms',{}).get('partners',0)<2:
        abort(403)
    if request.form.get('csrf') != session.get('cooperation_csrf'):
        abort(400)
    try:
        name=_text('name',True,200)
    except ValueError as e:
        flash(str(e),'error')
        return redirect(url_for('partners.index'))
    with closing(models.get_db()) as db:
        db.execute('INSERT OR REPLACE INTO cooperation_candidate_skips(name,skipped_on,created_by) VALUES (?,?,?)',
                   (name,date.today().isoformat(),g.user['id']))
        db.commit()
    _audit('update_cooperation')
    flash(f'{name}을(를) 후보에서 제외했습니다.','success')
    return redirect(url_for('partners.index')+'#partner-candidates')


@bp.route('/partners/restore-candidates',methods=['POST'])
@login_required
def restore_candidates():
    """보류한 후보를 모두 되살린다."""
    if current_user().get('perms',{}).get('partners',0)<2:
        abort(403)
    if request.form.get('csrf') != session.get('cooperation_csrf'):
        abort(400)
    with closing(models.get_db()) as db:
        db.execute('DELETE FROM cooperation_candidate_skips')
        db.commit()
    flash('보류한 후보를 모두 되살렸습니다.','success')
    return redirect(url_for('partners.index')+'#partner-candidates')


@bp.route('/partners/<int:pid>')
@login_required
def detail(pid):
    try:
        start,end,period=_period()
    except ValueError as e:
        flash(str(e),'error')
        return redirect(url_for('partners.detail',pid=pid,embedded='1' if request.values.get('embedded')=='1' else None))
    basis='source' if request.args.get('basis')=='source' else 'referral'
    with closing(models.get_db()) as db:
        p=_partner(db,pid)
        activities=[dict(r) for r in db.execute('SELECT * FROM cooperation_activities WHERE partner_id=? ORDER BY happened_on DESC,id DESC',(pid,))]
        contacts=[dict(r) for r in db.execute('SELECT * FROM cooperation_contacts WHERE partner_id=? ORDER BY is_primary DESC,status="재직" DESC,id',(pid,))]
        tasks=[dict(r) for r in db.execute('SELECT * FROM cooperation_tasks WHERE partner_id=? ORDER BY completed_on IS NOT NULL,due_on,id',(pid,))]
        agreements=[dict(r) for r in db.execute('SELECT * FROM cooperation_agreements WHERE partner_id=? ORDER BY signed_on DESC,id DESC',(pid,))]
        documents=[dict(r) for r in db.execute('SELECT * FROM cooperation_documents WHERE partner_id=? ORDER BY expires_on IS NULL,expires_on,id DESC',(pid,))]
        visit_plans=[dict(r) for r in db.execute('SELECT * FROM cooperation_visit_plans WHERE partner_id=? ORDER BY visit_on DESC,sequence,id',(pid,))]
        consultations,admissions=patient_report(db,p['name'],basis,start,end)
        undated=undated_admissions(db,p['name'],basis)
        last_visit=next((a for a in activities if a['kind']=='방문'),None)
        performance=None
        if last_visit:
            visit_day=date.fromisoformat(last_visit['happened_on'])
            before_start=(visit_day-timedelta(days=30)).isoformat()
            before_end=(visit_day-timedelta(days=1)).isoformat()
            after_end=min(date.today(),visit_day+timedelta(days=30)).isoformat()
            before_consults,before_admissions=patient_report(db,p['name'],'referral',before_start,before_end)
            after_consults,after_admissions=patient_report(db,p['name'],'referral',visit_day.isoformat(),after_end)
            performance={'visit_on':visit_day.isoformat(),'before_start':before_start,'before_end':before_end,
                         'after_start':visit_day.isoformat(),'after_end':after_end,
                         'before_consults':len(before_consults),'before_admissions':len(before_admissions),
                         'after_consults':len(after_consults),'after_admissions':len(after_admissions),
                         'after_days':(date.fromisoformat(after_end)-visit_day).days+1}
    for task in tasks:
        delta=(date.today()-date.fromisoformat(task['due_on'])).days
        task['dday']='오늘' if delta==0 else f'D{delta:+d}'
    _audit('view_cooperation',pid)
    today_iso=date.today().isoformat()
    for agreement in agreements:
        agreement['effective']=agreement['status']=='유효' and (not agreement['expires_on'] or agreement['expires_on']>=today_iso)
    quality=[]
    if not p['owner']: quality.append('담당 직원')
    if not contacts: quality.append('협력 담당자')
    elif not any(c['is_primary'] and c['status']=='재직' for c in contacts): quality.append('대표 담당자')
    if not (p['visit_cycle'] or p['visit_months']): quality.append('방문 주기')
    if not p['strengths']: quality.append('핵심 강점')
    if not p['phone']: quality.append('대표 연락처')
    return render_template('partner_detail.html',partner=p,activities=activities,contacts=contacts,tasks=tasks,agreements=agreements,
        documents=documents,visit_plans=visit_plans,quality=quality,
        consultations=consultations,admissions=admissions,undated=undated,start=start,end=end,period=period,basis=basis,
        csrf=_csrf(),today=date.today().isoformat(),kinds=KINDS,performance=performance,
        embedded=request.args.get('embedded')=='1',cycle_presets=CYCLE_PRESETS)


@bp.route('/partners/<int:pid>/save',methods=['POST'])
@login_required
def save(pid):
    try:
        with closing(models.get_db()) as db:
            p=_partner(db,pid)
            action=_text('action',True,30)
            with db:
                if action=='profile':
                    visit_days,visit_months=_cycle_values('visit')
                    contact_days,contact_months=_cycle_values('contact')
                    try: remind_days=int(request.form.get('remind_days','7'))
                    except ValueError: raise ValueError('알림 시점을 확인해주세요.')
                    if remind_days not in (0,1,3,7,14,30): raise ValueError('알림 시점을 확인해주세요.')
                    stage=_text('relationship_stage',limit=20) or p['relationship_stage'] or '신규'
                    if stage not in ('신규','접촉 중','협력 중','핵심기관','휴면'): raise ValueError('관계 단계를 확인해주세요.')
                    db.execute("""UPDATE cooperation_partners SET important=?,owner=?,visit_cycle=?,contact_cycle=?,notes=?,
                        visit_months=?,contact_months=?,remind_days=?,specialties=?,strengths=?,relationship_stage=? WHERE id=?""",
                        (int(request.form.get('important')=='1'),_text('owner',limit=100),visit_days,contact_days,_text('notes'),
                         visit_months,contact_months,remind_days,_text('specialties',limit=500),_text('strengths'),stage,pid))
                elif action=='contact':
                    status=_text('status',limit=20) or '재직'
                    if status not in ('재직','부서 이동','퇴사'): raise ValueError('담당자 상태를 확인해주세요.')
                    primary=int(request.form.get('is_primary')=='1' and status=='재직')
                    if primary: db.execute('UPDATE cooperation_contacts SET is_primary=0 WHERE partner_id=?',(pid,))
                    values=(_text('name',True,100),_text('department',limit=100),_text('position',limit=100),_text('phone',limit=100),status,primary)
                    if request.form.get('item_id'):
                        cur=db.execute('UPDATE cooperation_contacts SET name=?,department=?,position=?,phone=?,status=?,is_primary=? WHERE id=? AND partner_id=?',(*values,request.form['item_id'],pid))
                        if not cur.rowcount: abort(404)
                    else:
                        db.execute('INSERT INTO cooperation_contacts(name,department,position,phone,status,is_primary,partner_id) VALUES (?,?,?,?,?,?,?)',(*values,pid))
                elif action=='document':
                    values=(_text('category',True,30),_text('title',True,300),_text('location',limit=500),_date(_text('expires_on'),True),_text('notes'))
                    db.execute('INSERT INTO cooperation_documents(category,title,location,expires_on,notes,partner_id,created_by) VALUES (?,?,?,?,?,?,?)',(*values,pid,g.user['id']))
                elif action=='visit_plan':
                    status=_text('status',True,10)
                    if status not in ('예정','완료','취소'): raise ValueError('방문계획 상태를 확인해주세요.')
                    try: sequence=max(1,min(99,int(request.form.get('sequence','1'))))
                    except ValueError: raise ValueError('방문 순서를 확인해주세요.')
                    db.execute('INSERT INTO cooperation_visit_plans(partner_id,visit_on,sequence,purpose,owner,status,notes,created_by) VALUES (?,?,?,?,?,?,?,?)',
                        (pid,_date(_text('visit_on',True)),sequence,_text('purpose',True,500),_text('owner',limit=100),status,_text('notes'),g.user['id']))
                elif action == 'agreement':
                    status=_text('status',True,10)
                    if status not in ('유효','종료'): raise ValueError('협약 상태를 확인해주세요.')
                    values=(_text('title',True,300),_date(_text('signed_on',True)),
                            _date(_text('expires_on'),True),status,_text('document_location',limit=500),
                            _text('counterpart',limit=200),_text('notes'))
                    if values[2] and values[2] < values[1]:
                        raise ValueError('협약 만료일은 체결일보다 빠를 수 없습니다.')
                    if request.form.get('item_id'):
                        cur=db.execute('''UPDATE cooperation_agreements SET title=?,signed_on=?,expires_on=?,
                            status=?,document_location=?,counterpart=?,notes=? WHERE id=? AND partner_id=?''',
                            (*values,request.form['item_id'],pid))
                        if not cur.rowcount: abort(404)
                    else:
                        db.execute('''INSERT INTO cooperation_agreements
                            (title,signed_on,expires_on,status,document_location,counterpart,notes,partner_id,created_by)
                            VALUES (?,?,?,?,?,?,?,?,?)''',(*values,pid,g.user['id']))
                elif action in ('activity','task'):
                    kind=_text('kind',True,10)
                    if kind not in KINDS: raise ValueError('활동 유형을 선택해주세요.')
                    owner=_text('owner',limit=100)
                    if action=='activity':
                        happened=_date(_text('happened_on',True))
                        if happened>date.today().isoformat(): raise ValueError('미래 활동은 다음 일정으로 등록해주세요.')
                        values=(happened,kind,_text('met',limit=200),owner,_text('content',True))
                        if request.form.get('item_id'):
                            cur=db.execute('UPDATE cooperation_activities SET happened_on=?,kind=?,met=?,owner=?,content=? WHERE id=? AND partner_id=?',(*values,request.form['item_id'],pid))
                            if not cur.rowcount: abort(404)
                        else:
                            db.execute('INSERT INTO cooperation_activities(happened_on,kind,met,owner,content,partner_id,created_by) VALUES (?,?,?,?,?,?,?)',(*values,pid,g.user['id']))
                            next_due=_date(_text('next_due'),True)
                            explicit_next=bool(next_due)
                            cycle=p['visit_cycle'] if kind=='방문' else p['contact_cycle']
                            months=p['visit_months'] if kind=='방문' else p['contact_months']
                            if not next_due and (cycle or months):
                                next_due=_next_cycle(date.fromisoformat(happened),cycle,months).isoformat()
                            if next_due and (explicit_next or not db.execute('SELECT 1 FROM cooperation_tasks WHERE partner_id=? AND kind=? AND completed_on IS NULL',(pid,kind)).fetchone()):
                                db.execute('INSERT INTO cooperation_tasks(partner_id,due_on,kind,title,owner,created_by) VALUES (?,?,?,?,?,?)',
                                    (pid,next_due,kind,'정기 '+kind,owner,g.user['id']))
                    else:
                        due=_date(_text('due_on',True))
                        title=_text('title',True,300)
                        db.execute('INSERT INTO cooperation_tasks(partner_id,due_on,kind,title,owner,created_by) VALUES (?,?,?,?,?,?)',(pid,due,kind,title,owner,g.user['id']))
                elif action in ('complete','reschedule','reopen'):
                    task=db.execute('SELECT * FROM cooperation_tasks WHERE id=? AND partner_id=?',(_text('item_id',True),pid)).fetchone()
                    if not task: abort(404)
                    if action=='reschedule':
                        db.execute('UPDATE cooperation_tasks SET due_on=? WHERE id=?',(_date(_text('due_on',True)),task['id']))
                    else:
                        db.execute('UPDATE cooperation_tasks SET completed_on=? WHERE id=?',(date.today().isoformat() if action=='complete' else None,task['id']))
                else:
                    abort(400)
        _audit('update_cooperation',pid)
        flash('저장했습니다.','success')
    except (ValueError,sqlite3.IntegrityError) as e:
        flash(str(e) if isinstance(e,ValueError) else '입력 내용을 확인해주세요.','error')
    return redirect(url_for('partners.detail',pid=pid,embedded='1' if request.values.get('embedded')=='1' else None))


@bp.route('/partners/<int:pid>/export')
@login_required
def export(pid):
    try:
        start,end,_=_period()
    except ValueError as e:
        abort(400,description=str(e))
    basis='source' if request.args.get('basis')=='source' else 'referral'
    with closing(models.get_db()) as db:
        p=_partner(db,pid)
        consultations,admissions=patient_report(db,p['name'],basis,start,end)
    kind='consult' if request.args.get('kind')=='consult' else 'admission'
    rows=consultations if kind=='consult' else admissions
    def safe(value):
        value=str(value or '')
        return "'"+value if value.lstrip().startswith(('=','+','-','@')) else value
    output=io.StringIO(); writer=csv.writer(output)
    writer.writerow(['병원','연결 기준','환자명','상담일','입원일','주진단명·질환 기록','주치의','입원 상태','퇴원일'])
    for r in rows:
        writer.writerow([safe(v) for v in (p['name'],'이전 병원' if basis=='source' else '연계기관',r['patient_name'],r['consult_date'],r['admitted_on'],r['diagnosis_label'],r['attending_doctor'],r['admission_status'],r['discharge_date'])])
    _audit('export_cooperation',pid)
    return Response('\ufeff'+output.getvalue(),mimetype='text/csv',headers={'Content-Disposition':f'attachment; filename=partner_{pid}_{kind}.csv'})
