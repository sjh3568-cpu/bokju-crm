"""SQLite 스키마 + CRUD — 복주 상담실 CRM.

테이블:
  patients          환자 마스터 (이름+연락처로 자동 매칭)
  consultations     상담 (1환자 N상담, 종이 상담일지 모든 항목 포함)
  source_hospitals  모병원 마스터 (자동완성 + 신규 자동 등록)
  diagnoses         질환 마스터
  users             직원 계정
  audit_log         개인정보 접근/수정 추적
  attachments       팩스 스캔 등 첨부 (Phase 4)

JSON_FIELDS는 다중 체크박스 결과를 JSON 배열 텍스트로 저장. 읽을 때 자동 디시리얼라이즈.
스키마 변경은 _ensure_columns()가 ALTER TABLE ADD COLUMN으로 점진 적용.
"""
import json
import os
import sqlite3
from datetime import datetime

from werkzeug.security import generate_password_hash

from config import DIAGNOSIS_SEED, SOURCE_HOSPITAL_SEED

DB_PATH = os.path.join(os.path.dirname(__file__), "bokju.db")


# 다중 선택 체크박스 → JSON 배열 텍스트로 저장
JSON_FIELDS = {
    "hearing_options", "activity_active", "activity_others",
    "diseases", "arrange_items", "diet_types", "wound_care", "special_care",
    "therapy", "documents_checklist", "transport_method",
    "cost_guidance", "info_provided",
    "referral_source_detail", "referral_source_type",  # 입원경로(다중)
}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_columns(conn, table: str, columns: dict[str, str]):
    """누락된 컬럼만 ALTER TABLE ADD COLUMN으로 추가. SQLite는 단순 추가만 지원."""
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS patients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            birth_year INTEGER,
            age_at_first_consult INTEGER,
            gender TEXT CHECK(gender IN ('M','F','U')) DEFAULT 'U',
            residence_sido TEXT,
            residence_sigungu TEXT,
            insurance_type TEXT,
            guardian_name TEXT,
            guardian_relation TEXT,
            guardian_phone TEXT,
            note TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_patients_name ON patients(name);
        CREATE INDEX IF NOT EXISTS idx_patients_phone ON patients(guardian_phone);

        CREATE TABLE IF NOT EXISTS consultations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
            consult_date DATE NOT NULL,
            consult_channel TEXT,
            target_facility TEXT DEFAULT '재활병원',
            source_hospital TEXT,
            source_hospital_dept TEXT,
            primary_diagnosis TEXT,
            diagnosis_code TEXT,
            secondary_diagnosis TEXT,
            patient_status TEXT,
            adl_score INTEGER,
            needs_summary TEXT,
            doctor_confirmed INTEGER DEFAULT 0,
            doctor_confirmed_at DATETIME,
            doctor_name TEXT,
            decision TEXT CHECK(decision IN ('대기','입원확정','거절','보류','타기관전원')) DEFAULT '대기',
            decided_at DATETIME,
            admission_date DATE,
            rejection_reason TEXT,
            rejection_reason_detail TEXT,
            counselor TEXT,
            note TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_cons_date ON consultations(consult_date);
        CREATE INDEX IF NOT EXISTS idx_cons_patient ON consultations(patient_id);
        CREATE INDEX IF NOT EXISTS idx_cons_decision ON consultations(decision);
        CREATE INDEX IF NOT EXISTS idx_cons_hospital ON consultations(source_hospital);
        CREATE INDEX IF NOT EXISTS idx_cons_diagnosis ON consultations(primary_diagnosis);

        CREATE TABLE IF NOT EXISTS source_hospitals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            region TEXT,
            active INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS diagnoses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            icd10 TEXT,
            category TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            display_name TEXT,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'staff',
            active INTEGER DEFAULT 1,
            last_login_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            action TEXT,
            target_type TEXT,
            target_id INTEGER,
            detail TEXT,
            ip TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);

        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            consultation_id INTEGER NOT NULL REFERENCES consultations(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            mime TEXT,
            size_bytes INTEGER,
            uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        -- 환자 생애주기 이벤트 (3번 요청) — 1환자 N이벤트, 타임라인 구성
        CREATE TABLE IF NOT EXISTS lifecycle_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
            consultation_id INTEGER REFERENCES consultations(id) ON DELETE SET NULL,
            event_type TEXT NOT NULL,
            event_date DATE,
            title TEXT,
            detail TEXT,
            created_by TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_lc_patient ON lifecycle_events(patient_id);
        CREATE INDEX IF NOT EXISTS idx_lc_date ON lifecycle_events(event_date);

        -- 문자 템플릿 (5번 요청) — 환자군별 정형 안내문
        CREATE TABLE IF NOT EXISTS sms_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            template_group TEXT DEFAULT '공통',
            body TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        -- 문자 발송 이력 (5번 요청)
        CREATE TABLE IF NOT EXISTS sms_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            consultation_id INTEGER REFERENCES consultations(id) ON DELETE SET NULL,
            patient_id INTEGER,
            template_id INTEGER,
            to_name TEXT,
            to_phone TEXT,
            body TEXT,
            status TEXT,
            sent_by TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_sms_created ON sms_log(created_at);

        -- 옴니채널 통합 커뮤니케이션 로그 — 인바운드/기타 접점 (전화/문자/카카오/웹문의/팩스).
        -- 발신 문자는 sms_log, 통화 상담은 consultations에 — 타임라인이 4개 소스를 병합.
        CREATE TABLE IF NOT EXISTS communications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER REFERENCES patients(id) ON DELETE CASCADE,
            consultation_id INTEGER REFERENCES consultations(id) ON DELETE SET NULL,
            channel TEXT,
            direction TEXT,
            contact TEXT,
            summary TEXT,
            body TEXT,
            status TEXT DEFAULT 'open',
            follow_up_at DATE,
            created_by TEXT,
            occurred_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_comm_patient ON communications(patient_id);
        CREATE INDEX IF NOT EXISTS idx_comm_status ON communications(status);

        -- 환자 문서 (팩스/스캔/업로드) — OCR 텍스트 + Claude 환자상태 분석.
        CREATE TABLE IF NOT EXISTS patient_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER REFERENCES patients(id) ON DELETE SET NULL,
            consultation_id INTEGER REFERENCES consultations(id) ON DELETE SET NULL,
            filename TEXT,
            stored_path TEXT,
            mime TEXT,
            source TEXT,
            ocr_text TEXT,
            ai_summary TEXT,
            status TEXT DEFAULT 'pending',
            created_by TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_doc_patient ON patient_documents(patient_id);
        CREATE INDEX IF NOT EXISTS idx_doc_status ON patient_documents(status);
    """)

    # ─── 마이그레이션: 종이 상담일지 항목 매핑 ───
    _ensure_columns(conn, "patients", {
        "address_full": "TEXT",
        "family_info": "TEXT",
        # 생애주기 (3번) — 현재 단계
        "lifecycle_stage": "TEXT",
        # 블랙리스트 (4번)
        "blacklist": "INTEGER DEFAULT 0",
        "blacklist_reason": "TEXT",
        "blacklist_at": "DATETIME",
    })
    _ensure_columns(conn, "consultations", {
        # 헤더
        "planned_admission_date": "DATE",
        "attending_doctor": "TEXT",
        "room_number": "TEXT",
        "consult_time": "TEXT",  # 'HH:MM'
        "admission_route": "TEXT",
        # 양식의 '나이'는 상담 시점 스냅샷이므로 상담 단위로 저장
        "patient_age": "INTEGER",
        # 환자 현재 상태
        "current_location_type": "TEXT",  # 집/입원중/입소중
        "current_location_name": "TEXT",  # 병원·요양원 이름
        # 의식 — 3그룹 분리
        "consciousness_main": "TEXT",     # 정상/반혼수/혼수 (radio)
        "conversation_level": "TEXT",     # 가능/조금/불가능 (radio)
        "hearing_options": "TEXT",        # JSON 다중 (잘 안들림/알아들으심/보청기)
        "hearing_note": "TEXT",           # 의식 기타 자유 메모
        # 활동 — 양식 구조 그대로
        "activity_active": "TEXT",        # JSON 다중 (스스로/도움/지팡이/워커)
        "activity_diaper": "TEXT",        # 유/무
        "activity_wheelchair": "TEXT",    # 스스로/도움
        "activity_others": "TEXT",        # JSON 다중 (와상/에어매트리스)
        "caregiver_status": "TEXT",
        "bed_type": "TEXT",
        # 병명/진단
        "diseases": "TEXT",  # JSON
        "arrange_items": "TEXT",  # JSON (ARRANGE — DNR, hopeless 등 케어 방향 결정)
        "disease_detail": "TEXT",  # -PO/-OP/기타 자유 메모
        # 양식 수기 입력란: 당뇨(인슐린: 유/무)
        "insulin_use": "TEXT",
        # 양식 수기 입력란: 파킨슨 / 희귀성난치질환
        "parkinson_detail": "TEXT",
        "rare_disease_name": "TEXT",
        # 양식 수기 입력란: 암 행 (부위/발병일/전이/통증/patch)
        "cancer_site": "TEXT",
        "cancer_onset": "TEXT",
        "cancer_metastasis": "TEXT",
        "cancer_pain": "TEXT",
        "cancer_patch": "TEXT",
        # 양식 수기 입력란: 중추신경계 인라인 (뇌출혈 수술 / 뇌경색 부위 / 척수손상 부위)
        "hemorrhage_surgery": "TEXT",
        "infarction_onset": "TEXT",  # 사용 안함 (호환용)
        "infarction_site": "TEXT",
        "spinal_injury_level": "TEXT",
        # 발병일 — 중앙 집중 (병명 섹션 상단, 1차 진단 발병일)
        "disease_onset": "TEXT",
        # 비사용증후군 인라인 상세
        "lung_detail": "TEXT",            # 호흡질환(구 폐질환) 상세
        "heart_detail": "TEXT",
        "neoplasm_detail": "TEXT",
        "parkinson_new_detail": "TEXT",   # 비사용증후군 파킨슨(신규) 상세
        "gbs_detail": "TEXT",             # 길랑바레증후군 상세
        # 마비 그룹 상세
        "paralysis_detail": "TEXT",
        # 처치/치료
        "admission_purpose": "TEXT",
        "diet_types": "TEXT",  # JSON
        "diet_note": "TEXT",
        "wound_care": "TEXT",  # JSON
        "wound_site": "TEXT",  # 욕창 부위 등
        "special_care": "TEXT",  # JSON
        "oxygen_lpm": "TEXT",  # 산소요법 L/min
        "swallow_test": "TEXT",  # 유/무
        "swallow_test_dates": "TEXT",  # 검사일 메모
        "therapy": "TEXT",  # JSON
        # 입원시 확인
        "documents_checklist": "TEXT",  # JSON
        "admission_period": "TEXT",  # 입원가능기간
        "transport_method": "TEXT",  # JSON
        "cost_guidance": "TEXT",  # JSON
        "info_provided": "TEXT",  # JSON
        # 통계 분류 (엑셀 마스터)
        "patient_group": "TEXT",          # 중추신경계/근골격계/그 외/요양
        "primary_condition": "TEXT",      # 8종
        "admission_purpose_category": "TEXT",  # 6종 (자유 텍스트 admission_purpose와 분리)
        "admission_status": "TEXT",       # 상담완료/입원예정/입원완료
        "referral_source_type": "TEXT",   # 온라인/오프라인 (다중 JSON)
        "referral_source_detail": "TEXT", # 카페/유튜브/지인추천 등 (다중 JSON)
        "referrer_person": "TEXT",        # 소개 — 추천한 사람
        "referrer_institution": "TEXT",   # 소개 — 추천 기관
        # 엑셀 마이그레이션 (Phase 2)
        "actual_admission_date": "TEXT",  # 실제 입원일 (엑셀 27번 입원일/비고)
        "recontact_memo": "TEXT",         # 재접촉 관리 자유 메모 (엑셀 28번)
        "import_source": "TEXT",          # NULL=웹폼, 'excel'=신식(B/C), 'excel_legacy'=구식(A)
        # ── 상담일지 폼 11개 항목 개선 (2026-05) ──
        "current_nursing_name": "TEXT",   # 3번 — 현재 거주 요양원명 (병원명과 분리)
        "memo_po": "TEXT",                # 5번 — 기타 메모 PO
        "memo_op": "TEXT",                # 5번 — 기타 메모 OP
        "tracheostomy_detail": "TEXT",    # 7번 — 기관절개 인라인 수기
        "disuse_screening_note": "TEXT",  # 11번 — 비사용증후군 발굴 대상 수기
        "hold_reason": "TEXT",            # 10번 — 입원보류 사유 (필수)
        "discharge_due_date": "DATE",     # 10번 — 입원연장 시 새 퇴원예정일
        "discharge_date": "DATE",         # 10번 — 실제 퇴원일 (퇴원완료)
        # ── 폼 추가 개선 2차 (2026-05) ──
        "referral_online_note": "TEXT",   # 입원경로 온라인 박스 수기 입력
        "referral_etc_note": "TEXT",      # 입원경로 기타 박스 수기 입력
        # 상처소독 항목별 인라인 메모 (욕창=wound_site, 기관절개=tracheostomy_detail 재사용)
        "wound_op_note": "TEXT",          # 수술절상
        "wound_foley_note": "TEXT",       # Foley cath
        "wound_dmfoot_note": "TEXT",      # 당뇨발
        "wound_burn_note": "TEXT",        # 화상
        "wound_simple_note": "TEXT",      # 단순상처
        "wound_urostomy_note": "TEXT",    # 요루
        "wound_colostomy_note": "TEXT",   # 장루
        # 특수처치 항목별 인라인 메모 (산소요법=oxygen_lpm 재사용)
        "special_tpn_note": "TEXT",       # 중심정맥영양
        "special_vent_note": "TEXT",      # 인공호흡기
        "special_suction_note": "TEXT",   # 흡인
        "special_transfusion_note": "TEXT",  # 수혈
        "special_picc_note": "TEXT",      # PICC
        "special_fluid_note": "TEXT",     # 수액요법
        "special_nebulizer_note": "TEXT",  # 네블라이저
        "special_intubation_note": "TEXT",  # 기관내삽관
        "special_mrsa_note": "TEXT",      # MRSA
        "special_vre_note": "TEXT",       # VRE
        "special_cre_note": "TEXT",       # CRE
        # ── 7개 신규 기능 (2026-05-22) ──
        "admission_type": "TEXT",         # 1번 — 내원 유형 (일반/응급이송/전원)
        "consult_result": "TEXT",         # 7번 — 상담 진행 단계 (2단계 중 ①)
        "consult_result_reason": "TEXT",  # 7번 — 상담 결과 사유 (재입원/요청/보류/취소 시 필수)
    })
    # 입원 진행 상태 4종 개편 (2026-05): 구 5종의 '입원예정'·'입원확정'과
    # 엑셀 구식 '방문예정'을 '입원보류'(진행중)로 통합. 멱등.
    conn.execute(
        "UPDATE consultations SET admission_status = '입원보류' "
        "WHERE admission_status IN ('입원예정', '입원확정', '방문예정')"
    )
    # 상담 결과 2단계 분리 (2026-05-22): consult_result(상담 진행) /
    # admission_status(입원 진행)로 분리. 기존 admission_status '상담완료'는
    # 입원 단계 값이 아니므로 비우고, consult_result 기본값을 '상담완료'로. 멱등.
    conn.execute(
        "UPDATE consultations SET consult_result = '상담완료' "
        "WHERE consult_result IS NULL OR consult_result = ''"
    )
    conn.execute(
        "UPDATE consultations SET admission_status = NULL "
        "WHERE admission_status = '상담완료'"
    )
    # 내원 유형 기본값 — 미입력분은 '일반'. 멱등.
    conn.execute(
        "UPDATE consultations SET admission_type = '일반' "
        "WHERE admission_type IS NULL OR admission_type = ''"
    )

    for name, icd10, category in DIAGNOSIS_SEED:
        conn.execute(
            "INSERT OR IGNORE INTO diagnoses (name, icd10, category) VALUES (?, ?, ?)",
            (name, icd10, category),
        )
    for name, region in SOURCE_HOSPITAL_SEED:
        conn.execute(
            "INSERT OR IGNORE INTO source_hospitals (name, region) VALUES (?, ?)",
            (name, region),
        )

    # 문자 템플릿 — 비어있을 때만 예시 시드 (실제 문구는 화면에서 수정).
    if conn.execute("SELECT COUNT(*) FROM sms_templates").fetchone()[0] == 0:
        for name, grp, body in _SMS_TEMPLATE_SEED:
            conn.execute(
                "INSERT INTO sms_templates (name, template_group, body) VALUES (?, ?, ?)",
                (name, grp, body),
            )

    conn.commit()
    conn.close()


# 문자 템플릿 예시 시드 — 환자군별 정형 안내문. {토큰}은 발송 시 자동 치환.
_SMS_TEMPLATE_SEED = [
    ("상담 감사 안내", "공통",
     "[복주회복병원] {보호자명}님, {환자명} 환자분 상담해 주셔서 감사합니다. "
     "입원 관련 추가 문의는 상담실로 연락 주시기 바랍니다."),
    ("입원 예정 안내", "공통",
     "[복주회복병원] {환자명} 환자분 입원 예정일은 {입원예정일}입니다. "
     "입원 시 필요 서류는 안내드린 목록을 준비해 주세요. 문의: 상담실"),
    ("회복기 재활 입원 안내", "중추신경계",
     "[복주회복병원] {환자명} 환자분 회복기 재활 입원 상담 안내드립니다. "
     "주치의 {주치의} · 입원 예정 {입원예정일}. 자세한 사항은 상담실로 문의 주세요."),
    ("골절 재활 입원 안내", "근골격계",
     "[복주회복병원] {환자명} 환자분 재활 입원 상담 안내드립니다. "
     "입원 예정 {입원예정일}. 준비 서류 문의는 상담실로 연락 주세요."),
]


# ─── 사용자 / 감사 로그 ───

def ensure_admin_user(username: str, password: str, display_name: str | None = None):
    conn = get_db()
    pw_hash = generate_password_hash(password)
    conn.execute(
        """
        INSERT INTO users (username, display_name, password_hash, role)
        VALUES (?, ?, ?, 'admin')
        ON CONFLICT(username) DO UPDATE SET
            password_hash = excluded.password_hash,
            display_name = COALESCE(excluded.display_name, users.display_name),
            active = 1
        """,
        (username, display_name or username, pw_hash),
    )
    conn.commit()
    conn.close()


def get_user(username: str):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM users WHERE username = ? AND active = 1",
        (username,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def touch_user_login(user_id: int):
    conn = get_db()
    conn.execute(
        "UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()


def log_audit(*, user_id=None, username=None, action, target_type=None, target_id=None, detail=None, ip=None):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO audit_log (user_id, username, action, target_type, target_id, detail, ip)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, username, action, target_type, target_id, detail, ip),
    )
    conn.commit()
    conn.close()


# ─── 환자 ───

def find_or_create_patient(*, name: str, guardian_phone: str | None, **fields) -> int:
    conn = get_db()
    row = None
    if guardian_phone:
        row = conn.execute(
            "SELECT id FROM patients WHERE name = ? AND guardian_phone = ? LIMIT 1",
            (name, guardian_phone),
        ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT id FROM patients WHERE name = ? AND (guardian_phone IS NULL OR guardian_phone = '') LIMIT 1",
            (name,),
        ).fetchone()
    patient_cols = (
        "birth_year", "age_at_first_consult", "gender",
        "residence_sido", "residence_sigungu", "address_full",
        "insurance_type", "guardian_name", "guardian_relation",
        "guardian_phone", "family_info", "note",
    )
    if row:
        pid = row["id"]
        sets, vals = [], []
        for col in patient_cols:
            v = fields.get(col)
            if v not in (None, ""):
                sets.append(f"{col} = ?")
                vals.append(v)
        if guardian_phone is not None and guardian_phone != "":
            sets.append("guardian_phone = ?")
            vals.append(guardian_phone)
        if sets:
            sets.append("updated_at = CURRENT_TIMESTAMP")
            vals.append(pid)
            conn.execute(f"UPDATE patients SET {', '.join(sets)} WHERE id = ?", vals)
            conn.commit()
        conn.close()
        return pid

    cur = conn.execute(
        f"""
        INSERT INTO patients (name, guardian_phone, {','.join(patient_cols)})
        VALUES (?, ?, {','.join(['?'] * len(patient_cols))})
        """,
        [name, guardian_phone] + [fields.get(c) for c in patient_cols],
    )
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid


def get_patient(pid: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_patient(pid: int, **fields):
    if not fields:
        return
    sets = [f"{k} = ?" for k in fields.keys()]
    sets.append("updated_at = CURRENT_TIMESTAMP")
    vals = list(fields.values()) + [pid]
    conn = get_db()
    conn.execute(f"UPDATE patients SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    conn.close()


# ─── 상담 ───

CONSULT_FIELDS = (
    # 헤더
    "consult_date", "consult_time", "counselor",
    "planned_admission_date", "attending_doctor", "room_number",
    "consult_channel", "admission_route", "admission_type",
    # 상담유입경로 + 소개 추천인/기관 + 온라인·기타 박스 수기 입력
    "referral_source_type", "referral_source_detail",
    "referrer_person", "referrer_institution",
    "referral_online_note", "referral_etc_note",
    # 환자 현재 상태
    "patient_age",
    "current_location_type", "current_location_name", "current_nursing_name",
    # 모병원 — current_location_name이 입원중/입소중일 때 자동 채움 (app.py에서)
    "source_hospital",
    "consciousness_main", "conversation_level", "hearing_options", "hearing_note",
    "activity_active", "activity_diaper", "activity_wheelchair", "activity_others",
    "caregiver_status", "bed_type",
    # 병명·기타(ARRANGE)
    "diseases", "arrange_items", "disease_detail",
    "memo_po", "memo_op",
    "insulin_use", "parkinson_detail", "rare_disease_name",
    "cancer_site", "cancer_onset", "cancer_metastasis", "cancer_pain", "cancer_patch",
    "hemorrhage_surgery", "infarction_site", "spinal_injury_level",
    "disease_onset",
    "lung_detail", "heart_detail", "neoplasm_detail",
    "parkinson_new_detail", "gbs_detail",
    "paralysis_detail",
    # 처치/치료
    "admission_purpose", "diet_types",
    "wound_care", "wound_site", "tracheostomy_detail",
    "wound_op_note", "wound_foley_note", "wound_dmfoot_note", "wound_burn_note",
    "wound_simple_note", "wound_urostomy_note", "wound_colostomy_note",
    "special_care", "oxygen_lpm",
    "special_tpn_note", "special_vent_note", "special_suction_note",
    "special_transfusion_note", "special_picc_note", "special_fluid_note",
    "special_nebulizer_note", "special_intubation_note",
    "special_mrsa_note", "special_vre_note", "special_cre_note",
    "swallow_test", "swallow_test_dates",
    "therapy",
    # 입원시 확인
    "documents_checklist", "admission_period",
    "transport_method", "cost_guidance", "info_provided",
    # 상담 결과 2단계 — ① 상담 진행 (consult_result) ② 입원 진행 (admission_status)
    "consult_result", "consult_result_reason",
    "admission_status", "admission_date",
    "rejection_reason", "rejection_reason_detail", "hold_reason",
    "discharge_due_date", "discharge_date",
    "disuse_screening_note",
    # 엑셀 마이그레이션
    "actual_admission_date", "recontact_memo", "import_source",
)


def _serialize(field: str, value):
    if field in JSON_FIELDS:
        if value is None:
            return None
        if isinstance(value, str):
            return value  # 이미 직렬화된 상태
        return json.dumps(value, ensure_ascii=False)
    return value


def _deserialize_consultation(d: dict) -> dict:
    for k in JSON_FIELDS:
        if k in d:
            v = d[k]
            if isinstance(v, str) and v.strip():
                try:
                    d[k] = json.loads(v)
                except json.JSONDecodeError:
                    d[k] = []
            else:
                d[k] = []
    return d


def create_consultation(*, patient_id: int, **fields) -> int:
    cols = ["patient_id"]
    vals = [patient_id]
    for c in CONSULT_FIELDS:
        if c in fields:
            cols.append(c)
            vals.append(_serialize(c, fields[c]))
    placeholders = ",".join(["?"] * len(cols))
    conn = get_db()
    cur = conn.execute(
        f"INSERT INTO consultations ({','.join(cols)}) VALUES ({placeholders})",
        vals,
    )
    cid = cur.lastrowid
    conn.commit()
    conn.close()
    _ensure_master_entry(fields.get("source_hospital"), fields.get("primary_diagnosis"))
    return cid


def update_consultation(cid: int, **fields):
    valid = {k: v for k, v in fields.items() if k in CONSULT_FIELDS}
    if not valid:
        return
    sets = [f"{k} = ?" for k in valid.keys()]
    sets.append("updated_at = CURRENT_TIMESTAMP")
    vals = [_serialize(k, v) for k, v in valid.items()] + [cid]
    conn = get_db()
    conn.execute(f"UPDATE consultations SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    conn.close()
    _ensure_master_entry(valid.get("source_hospital"), valid.get("primary_diagnosis"))


# 결과(상담·입원 진행 단계) 변경 전용 — 폼 필드와 분리해서 실수 방지
_META_FIELDS = ("consult_result", "consult_result_reason",
                "admission_status", "admission_date", "rejection_reason",
                "rejection_reason_detail", "hold_reason",
                "discharge_due_date", "discharge_date")


def delete_consultation(cid: int):
    """상담 1건 삭제. 첨부파일은 ON DELETE CASCADE로 함께 삭제됨."""
    conn = get_db()
    conn.execute("DELETE FROM consultations WHERE id = ?", (cid,))
    conn.commit()
    conn.close()


def update_consultation_meta(cid: int, **fields):
    # None 허용 — admission_status를 '미정'(NULL)으로 되돌리는 등 명시적 비우기 가능.
    valid = {k: v for k, v in fields.items() if k in _META_FIELDS}
    if not valid:
        return
    sets = [f"{k} = ?" for k in valid.keys()]
    sets.append("updated_at = CURRENT_TIMESTAMP")
    vals = list(valid.values()) + [cid]
    conn = get_db()
    conn.execute(f"UPDATE consultations SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    conn.close()


def get_consultation(cid: int):
    conn = get_db()
    row = conn.execute(
        """
        SELECT c.*, p.name AS patient_name, p.gender, p.address_full,
               p.residence_sido, p.residence_sigungu,
               p.insurance_type, p.guardian_name, p.guardian_relation,
               p.guardian_phone, p.family_info,
               p.blacklist, p.blacklist_reason, p.lifecycle_stage
        FROM consultations c JOIN patients p ON p.id = c.patient_id
        WHERE c.id = ?
        """,
        (cid,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return _deserialize_consultation(dict(row))


# 정렬 가능 컬럼 화이트리스트 (SQL 인젝션 방지 — 키만 외부 입력 허용)
_SORT_COLUMNS = {
    "date": "c.consult_date",
    "patient": "p.name",
    "residence": "p.residence_sido",
    "hospital": "c.source_hospital",
    "channel": "c.consult_channel",
    "status": "c.admission_status",
    "counselor": "c.counselor",
}


def _build_consult_where(*, date_from=None, date_to=None, insurance=None, q=None,
                         counselor=None, admission_status=None, disease_group=None,
                         residence_sido=None, recovery=None,
                         consult_channel=None, referral_type=None,
                         admission_type=None, consult_result=None, blacklist=None):
    """list_consultations / count_consultations 공용 WHERE 절 빌더 → (where_sql, vals)."""
    where, vals = [], []
    if date_from:
        where.append("c.consult_date >= ?"); vals.append(date_from)
    if date_to:
        where.append("c.consult_date <= ?"); vals.append(date_to)
    if insurance:
        where.append("p.insurance_type = ?"); vals.append(insurance)
    if counselor:
        where.append("c.counselor = ?"); vals.append(counselor)
    if admission_status:
        # '미정' = 입원 단계 미진입 (NULL/빈값)
        if admission_status == "미정":
            where.append("(c.admission_status IS NULL OR c.admission_status = '')")
        else:
            where.append("c.admission_status = ?")
            vals.append(admission_status)
    if consult_result:
        # NULL/빈값은 '상담완료'로 간주
        if consult_result == "상담완료":
            where.append("(c.consult_result IS NULL OR c.consult_result = '' OR c.consult_result = ?)")
            vals.append("상담완료")
        else:
            where.append("c.consult_result = ?")
            vals.append(consult_result)
    if admission_type:
        # NULL/빈값은 '일반'으로 간주
        if admission_type == "일반":
            where.append("(c.admission_type IS NULL OR c.admission_type = '' OR c.admission_type = ?)")
            vals.append("일반")
        else:
            where.append("c.admission_type = ?")
            vals.append(admission_type)
    if blacklist:
        where.append("p.blacklist = 1")
    if disease_group:
        # diseases JSON에 그룹명 또는 그룹 멤버가 포함된 행
        from config import DISEASES_GROUPS
        members = DISEASES_GROUPS.get(disease_group, [])
        like_clauses = ["c.diseases LIKE ?"]  # 그룹명 자체 (마이그레이션 폴백)
        like_vals = [f'%"{disease_group}"%']
        for m in members:
            like_clauses.append("c.diseases LIKE ?")
            like_vals.append(f'%"{m}"%')
        where.append("(" + " OR ".join(like_clauses) + ")")
        vals.extend(like_vals)
    if residence_sido:
        where.append("p.residence_sido = ?"); vals.append(residence_sido)
    if recovery:
        # 저장값 매핑 (자동 계산값은 SQL로 못 잡으므로 저장값 기준).
        # 자동 기입값이 '회복기재활 및 간호간병 통합서비스' 형태일 수 있어 접두 LIKE 사용.
        if recovery == "회복기":
            where.append("(c.admission_purpose LIKE '회복기재활%' OR c.admission_purpose = '회복기')")
        elif recovery == "비회복기":
            where.append("(c.admission_purpose LIKE '비회복기재활%' OR c.admission_purpose = '비회복기')")
        elif recovery == "일반재활":
            where.append("c.admission_purpose LIKE '일반재활%'")
        elif recovery == "요양":
            where.append("c.admission_purpose IN ('요양', '요양병원')")
    if consult_channel:
        where.append("c.consult_channel = ?"); vals.append(consult_channel)
    if referral_type:
        # JSON 배열에 그룹명 포함 검색
        where.append("c.referral_source_type LIKE ?")
        vals.append(f'%"{referral_type}"%')
    if q:
        where.append("(p.name LIKE ? OR c.source_hospital LIKE ?)")
        like = f"%{q}%"
        vals.extend([like, like])
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return where_sql, vals


def count_consultations(**filters):
    """필터 조건에 맞는 상담 총 건수 (페이지네이션 total용)."""
    where_sql, vals = _build_consult_where(**filters)
    conn = get_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM consultations c "
        f"JOIN patients p ON p.id = c.patient_id {where_sql}",
        vals,
    ).fetchone()[0]
    conn.close()
    return n


def list_consultations(*, date_from=None, date_to=None,
                       insurance=None, q=None,
                       counselor=None, admission_status=None, disease_group=None,
                       residence_sido=None, recovery=None,
                       consult_channel=None, referral_type=None,
                       admission_type=None, consult_result=None, blacklist=None,
                       sort=None, sort_dir=None,
                       limit=200, offset=0):
    """상담 목록.
    검색·필터: 기간, 보험, 상담자, 입원여부, 병명그룹, 검색어(환자명·모병원).
    정렬: sort(date/patient/residence/hospital/channel/status/counselor) + sort_dir(asc/desc).
    추가 반환: residence_sido/sigungu, source_hospital, diseases, admission_purpose,
              admission_status, prior_consult_count(같은 환자의 다른 상담 수),
              homonym_count(같은 이름·다른 환자 수).
    """
    where_sql, vals = _build_consult_where(
        date_from=date_from, date_to=date_to, insurance=insurance, q=q,
        counselor=counselor, admission_status=admission_status,
        disease_group=disease_group, residence_sido=residence_sido,
        recovery=recovery, consult_channel=consult_channel,
        referral_type=referral_type, admission_type=admission_type,
        consult_result=consult_result, blacklist=blacklist,
    )
    sort_col = _SORT_COLUMNS.get(sort or "date", "c.consult_date")
    direction = "ASC" if str(sort_dir or "").lower() == "asc" else "DESC"
    # NULL·빈값은 항상 뒤로, 동률은 최신(id 큰 순)
    order_sql = (f"({sort_col} IS NULL OR {sort_col} = '') ASC, "
                 f"{sort_col} {direction}, c.id DESC")
    sql = f"""
        SELECT c.id, c.consult_date, c.consult_time, c.consult_channel,
               c.attending_doctor, c.room_number,
               c.planned_admission_date, c.actual_admission_date,
               c.admission_route, c.admission_type, c.counselor, c.patient_age,
               c.referral_source_type, c.referral_source_detail,
               c.source_hospital, c.diseases, c.disease_detail, c.disease_onset,
               c.admission_purpose, c.consult_result, c.consult_result_reason,
               c.admission_status, c.admission_date,
               c.discharge_due_date, c.discharge_date,
               c.import_source,
               p.id AS patient_id, p.name AS patient_name, p.gender,
               p.address_full, p.residence_sido, p.residence_sigungu,
               p.insurance_type, p.blacklist, p.blacklist_reason, p.lifecycle_stage,
               p.guardian_name, p.guardian_relation, p.guardian_phone
        FROM consultations c JOIN patients p ON p.id = c.patient_id
        {where_sql}
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
    """
    vals.extend([limit, offset])
    conn = get_db()
    rows = conn.execute(sql, vals).fetchall()
    if not rows:
        conn.close()
        return []

    # 환자별 총 상담 수, 동명이인(이름 같고 patient_id 다름) 카운트
    patient_ids = list({r["patient_id"] for r in rows})
    names = list({r["patient_name"] for r in rows if r["patient_name"]})

    consult_counts = {}
    if patient_ids:
        placeholders = ",".join("?" * len(patient_ids))
        for pid, n in conn.execute(
            f"SELECT patient_id, COUNT(*) FROM consultations WHERE patient_id IN ({placeholders}) GROUP BY patient_id",
            patient_ids,
        ).fetchall():
            consult_counts[pid] = n

    homonym_counts = {}  # name -> distinct patient_id 수
    if names:
        placeholders = ",".join("?" * len(names))
        for name, n in conn.execute(
            f"SELECT name, COUNT(DISTINCT id) FROM patients WHERE name IN ({placeholders}) GROUP BY name",
            names,
        ).fetchall():
            homonym_counts[name] = n
    conn.close()

    out = []
    for r in rows:
        d = _deserialize_consultation(dict(r))
        d["prior_consult_count"] = consult_counts.get(r["patient_id"], 1) - 1  # 자신 제외
        d["homonym_count"] = homonym_counts.get(r["patient_name"], 1)  # 본인 포함
        out.append(d)
    return out


def patient_consultations(patient_id: int):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM consultations WHERE patient_id = ? ORDER BY consult_date DESC",
        (patient_id,),
    ).fetchall()
    conn.close()
    return [_deserialize_consultation(dict(r)) for r in rows]


# ─── 자동완성 ───

def autocomplete_hospitals(q: str, limit: int = 10):
    conn = get_db()
    rows = conn.execute(
        "SELECT name, region FROM source_hospitals WHERE active = 1 AND name LIKE ? ORDER BY name LIMIT ?",
        (f"%{q}%", limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def top_source_hospitals(limit: int = 5):
    """가장 자주 입력된 모병원 Top N — 상담일지 폼 빠른 선택 버튼용."""
    conn = get_db()
    rows = conn.execute(
        "SELECT source_hospital, COUNT(*) AS n FROM consultations "
        "WHERE source_hospital IS NOT NULL AND TRIM(source_hospital) != '' "
        "GROUP BY source_hospital ORDER BY n DESC, source_hospital LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [r["source_hospital"] for r in rows]


def autocomplete_diagnoses(q: str, limit: int = 10):
    conn = get_db()
    rows = conn.execute(
        "SELECT name, icd10, category FROM diagnoses WHERE name LIKE ? ORDER BY name LIMIT ?",
        (f"%{q}%", limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def autocomplete_patients(q: str, limit: int = 10):
    conn = get_db()
    rows = conn.execute(
        """
        SELECT id, name, gender, guardian_phone, guardian_name, guardian_relation,
               address_full, insurance_type, family_info,
               blacklist, blacklist_reason
        FROM patients WHERE name LIKE ? ORDER BY updated_at DESC LIMIT ?
        """,
        (f"%{q}%", limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def match_patient_by_phone(phone: str | None):
    """보호자 연락처로 환자 1명 매칭 (옴니채널 인바운드 자동 연결용). 없으면 None."""
    if not phone:
        return None
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM patients WHERE guardian_phone = ? AND guardian_phone != '' "
        "ORDER BY updated_at DESC LIMIT 1",
        (phone,),
    ).fetchone()
    conn.close()
    return row["id"] if row else None


def find_blacklisted(*, name: str | None = None, phone: str | None = None):
    """이름 또는 보호자 연락처가 일치하는 블랙리스트 환자 1명 반환 (없으면 None).
    신규 상담 등록 시 임상 안전 경고용 (제안 2)."""
    if not name and not phone:
        return None
    conn = get_db()
    row = None
    if phone:
        row = conn.execute(
            "SELECT id, name, blacklist_reason FROM patients "
            "WHERE blacklist = 1 AND guardian_phone = ? AND guardian_phone != '' LIMIT 1",
            (phone,),
        ).fetchone()
    if not row and name:
        row = conn.execute(
            "SELECT id, name, blacklist_reason FROM patients "
            "WHERE blacklist = 1 AND name = ? LIMIT 1",
            (name,),
        ).fetchone()
    conn.close()
    return dict(row) if row else None


def _ensure_master_entry(hospital: str | None, diagnosis: str | None):
    if not (hospital or diagnosis):
        return
    conn = get_db()
    if hospital:
        conn.execute(
            "INSERT OR IGNORE INTO source_hospitals (name) VALUES (?)",
            (hospital.strip(),),
        )
    if diagnosis:
        conn.execute(
            "INSERT OR IGNORE INTO diagnoses (name) VALUES (?)",
            (diagnosis.strip(),),
        )
    conn.commit()
    conn.close()


# ─── 대시보드 ───

def dashboard_summary():
    """양식 기반 통계 — 상담 건수와 입원예정일 등록 건수."""
    conn = get_db()
    now = datetime.now()
    ym = now.strftime("%Y-%m")
    today = now.strftime("%Y-%m-%d")

    row = conn.execute(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN planned_admission_date IS NOT NULL AND planned_admission_date != '' THEN 1 ELSE 0 END) AS planned
        FROM consultations
        WHERE strftime('%Y-%m', consult_date) = ?
        """,
        (ym,),
    ).fetchone()

    today_rows = conn.execute(
        """
        SELECT c.id, c.consult_date, c.consult_time, c.attending_doctor, c.room_number,
               c.planned_admission_date, p.name AS patient_name
        FROM consultations c JOIN patients p ON p.id = c.patient_id
        WHERE c.consult_date = ?
        ORDER BY c.id DESC
        """,
        (today,),
    ).fetchall()

    week_trend = conn.execute(
        """
        SELECT consult_date AS d, COUNT(*) AS n
        FROM consultations
        WHERE consult_date >= date(?, '-6 days')
        GROUP BY consult_date ORDER BY consult_date
        """,
        (today,),
    ).fetchall()

    conn.close()
    summary = dict(row) if row else {}
    total = summary.get("total") or 0
    planned = summary.get("planned") or 0
    summary["plan_rate"] = round(100.0 * planned / total, 1) if total else 0.0
    return {
        "summary": summary,
        "today": [dict(r) for r in today_rows],
        "week_trend": [dict(r) for r in week_trend],
    }


# ─── 통계 (Phase 3) ───

# 연령대 버킷 — 50대 미만은 한 칸으로 묶어 노이즈 줄임
_AGE_BUCKETS = [
    ("~49", lambda a: a < 50),
    ("50대", lambda a: 50 <= a < 60),
    ("60대", lambda a: 60 <= a < 70),
    ("70대", lambda a: 70 <= a < 80),
    ("80대", lambda a: 80 <= a < 90),
    ("90+", lambda a: a >= 90),
]


def _age_bucket(age: int | None) -> str:
    if age is None:
        return "미상"
    for name, pred in _AGE_BUCKETS:
        if pred(age):
            return name
    return "미상"


def _count_simple(rows, key):
    """단일 텍스트 컬럼 빈도. 빈값/None은 '미상'."""
    out = {}
    for r in rows:
        v = r[key] if key in r.keys() else None
        v = (v or "").strip() if isinstance(v, str) else v
        if not v:
            v = "미상"
        out[v] = out.get(v, 0) + 1
    return out


def _count_json_multi(rows, key):
    """JSON 배열 다중선택 컬럼(diseases, referral_source_*)의 항목별 빈도."""
    out = {}
    for r in rows:
        raw = r[key] if key in r.keys() else None
        if not raw:
            continue
        try:
            items = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(items, list):
            continue
        for it in items:
            it = (it or "").strip() if isinstance(it, str) else it
            if not it:
                continue
            out[it] = out.get(it, 0) + 1
    return out


def _sort_desc(d: dict) -> list[dict]:
    return [{"label": k, "count": v} for k, v in sorted(d.items(), key=lambda x: (-x[1], x[0]))]


def _daily_trend(rows):
    """consult_date별 건수. 이미 SQL에서 정렬되어 들어옴."""
    by_day = {}
    for r in rows:
        d = (r["consult_date"] or "")[:10]
        if not d:
            continue
        by_day[d] = by_day.get(d, 0) + 1
    return [{"date": d, "count": n} for d, n in sorted(by_day.items())]


def aggregate_stats(date_from: str | None, date_to: str | None) -> dict:
    """기간 내 모든 통계 카운트를 한 번에 조회.

    JSON 다중필드는 SQLite 집계가 까다롭고 데이터 규모도 작아 Python에서 처리.
    Returns: {summary, by_referral_type, by_referral_detail, by_sido, by_sigungu_top,
              by_disease, by_disease_group, by_insurance, by_channel, by_counselor,
              by_age, by_gender, daily_trend}
    """
    where, vals = [], []
    if date_from:
        where.append("c.consult_date >= ?"); vals.append(date_from)
    if date_to:
        where.append("c.consult_date <= ?"); vals.append(date_to)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
        SELECT
          c.consult_date, c.consult_channel, c.counselor, c.patient_age,
          c.referral_source_type, c.referral_source_detail, c.diseases,
          c.planned_admission_date, c.admission_status, c.admission_date,
          c.consult_result, c.admission_type,
          c.source_hospital, c.rejection_reason, c.disuse_screening_note,
          p.gender, p.insurance_type,
          p.residence_sido, p.residence_sigungu
        FROM consultations c JOIN patients p ON p.id = c.patient_id
        {where_sql}
    """
    conn = get_db()
    rows = conn.execute(sql, vals).fetchall()
    conn.close()

    from config import ADMISSION_STATUS_ALL, CONSULT_RESULTS

    total = len(rows)
    planned = sum(1 for r in rows if (r["planned_admission_date"] or "").strip())

    # 입원 진행 단계 — NULL/빈값('미정')은 입원 단계 미진입. 퇴원완료 포함.
    status_counts = {s: 0 for s in ADMISSION_STATUS_ALL}
    status_counts["미정"] = 0
    for r in rows:
        s = (r["admission_status"] or "").strip()
        status_counts[s if s in ADMISSION_STATUS_ALL else "미정"] += 1
    # 입원완료 + 퇴원완료 = 입원 성사
    completed = status_counts["입원완료"] + status_counts["퇴원완료"]
    cancelled = status_counts["입원취소"]
    # 진행중 = 미정 + 입원보류 (입원/취소로 미확정)
    pending = status_counts["미정"] + status_counts["입원보류"]
    conversion_rate = round(100.0 * completed / total, 1) if total else 0.0

    # 상담 진행 단계 (Tier 1) — NULL/빈값은 '상담완료'.
    result_counts = {s: 0 for s in CONSULT_RESULTS}
    for r in rows:
        cr = (r["consult_result"] or "").strip() or "상담완료"
        result_counts[cr if cr in result_counts else "상담완료"] += 1
    # 비사용증후군 발굴 대상 — disuse_screening_note 기재 건수
    disuse_screening = sum(
        1 for r in rows if (r["disuse_screening_note"] or "").strip())

    by_disease = _count_json_multi(rows, "diseases")
    # 그룹별 합계 (config DISEASES_GROUPS 매핑)
    from config import DISEASES_GROUPS
    group_names = set(DISEASES_GROUPS.keys())
    by_group = {g: 0 for g in DISEASES_GROUPS.keys()}
    for label, n in by_disease.items():
        # 1순위: specific 라벨이 그룹 멤버에 속하면 그 그룹에 가산
        matched = False
        for group, members in DISEASES_GROUPS.items():
            if label in members:
                by_group[group] += n
                matched = True
                break
        # 2순위: 라벨 자체가 그룹명이면 그 그룹에 가산 (엑셀 마이그레이션 fallback)
        if not matched and label in group_names:
            by_group[label] += n
    # by_disease(개별 Top)에는 group 이름은 노출 안 함 (의미 혼동 방지)
    by_disease_individual = {k: v for k, v in by_disease.items() if k not in group_names}

    by_age_raw = {}
    for r in rows:
        bucket = _age_bucket(r["patient_age"])
        by_age_raw[bucket] = by_age_raw.get(bucket, 0) + 1
    # 정해진 순서대로
    age_order = [name for name, _ in _AGE_BUCKETS] + ["미상"]
    by_age = [{"label": k, "count": by_age_raw.get(k, 0)} for k in age_order if by_age_raw.get(k)]

    sigungu_top = _sort_desc(_count_simple(rows, "residence_sigungu"))[:10]

    # 모병원 — 빈값/None 제외 (자택 거주 환자는 모병원 없음)
    hospital_counts = {}
    for r in rows:
        v = (r["source_hospital"] or "").strip() if isinstance(r["source_hospital"], str) else None
        if v:
            hospital_counts[v] = hospital_counts.get(v, 0) + 1
    by_hospital = _sort_desc(hospital_counts)[:10]

    # 거절·취소 사유 — 입원취소 상태에서만 의미. NULL/빈값 제외.
    reason_counts = {}
    for r in rows:
        v = (r["rejection_reason"] or "").strip() if isinstance(r["rejection_reason"], str) else None
        if v:
            reason_counts[v] = reason_counts.get(v, 0) + 1
    by_reason = _sort_desc(reason_counts)

    # 문자 발송 현황 (제안 3) — sms_log를 기간(created_at)으로 집계
    sms_where, sms_vals = [], []
    if date_from:
        sms_where.append("date(s.created_at) >= ?"); sms_vals.append(date_from)
    if date_to:
        sms_where.append("date(s.created_at) <= ?"); sms_vals.append(date_to)
    sms_where_sql = ("WHERE " + " AND ".join(sms_where)) if sms_where else ""
    conn2 = get_db()
    sms_rows = conn2.execute(
        f"SELECT s.status, t.template_group FROM sms_log s "
        f"LEFT JOIN sms_templates t ON t.id = s.template_id {sms_where_sql}",
        sms_vals,
    ).fetchall()
    conn2.close()
    _SMS_STATUS_LABEL = {"sent": "발송", "manual": "수동(문자앱)", "failed": "실패"}
    sms_status, sms_group = {}, {}
    for r in sms_rows:
        sl = _SMS_STATUS_LABEL.get(r["status"] or "manual", "수동(문자앱)")
        sms_status[sl] = sms_status.get(sl, 0) + 1
        gl = r["template_group"] or "기타"
        sms_group[gl] = sms_group.get(gl, 0) + 1

    return {
        "summary": {
            "total": total,
            "planned": planned,
            "plan_rate": round(100.0 * planned / total, 1) if total else 0.0,
            "completed": completed,
            "cancelled": cancelled,
            "pending": pending,
            "conversion_rate": conversion_rate,
            "disuse_screening": disuse_screening,
            "from": date_from,
            "to": date_to,
        },
        # 진행 단계는 정해진 순서 그대로 유지 (도넛/막대 안정 표기). 퇴원완료 포함.
        "by_status": [{"label": s, "count": status_counts[s]}
                      for s in (["미정"] + ADMISSION_STATUS_ALL)],
        # 상담 진행 단계 (Tier 1) + 내원 유형
        "by_consult_result": [{"label": s, "count": result_counts[s]} for s in CONSULT_RESULTS],
        "by_admission_type": _sort_desc(_count_simple(rows, "admission_type")),
        "by_source_hospital": by_hospital,
        "by_rejection_reason": by_reason,
        "by_referral_type": _sort_desc(_count_json_multi(rows, "referral_source_type")),
        "by_referral_detail": _sort_desc(_count_json_multi(rows, "referral_source_detail")),
        "by_sido": _sort_desc(_count_simple(rows, "residence_sido")),
        "by_sigungu_top": sigungu_top,
        "by_disease": _sort_desc(by_disease_individual)[:15],
        "by_disease_group": [{"label": k, "count": v} for k, v in by_group.items() if v],
        "by_insurance": _sort_desc(_count_simple(rows, "insurance_type")),
        "by_channel": _sort_desc(_count_simple(rows, "consult_channel")),
        "by_counselor": _sort_desc(_count_simple(rows, "counselor")),
        "by_age": by_age,
        "by_gender": _sort_desc(_count_simple(rows, "gender")),
        "daily_trend": _daily_trend(rows),
        "sms": {
            "total": len(sms_rows),
            "by_status": _sort_desc(sms_status),
            "by_group": _sort_desc(sms_group),
        },
    }


# ─── 임원 월간 보고서 (Phase 3.5) ───

def _month_range(year: int, month: int) -> tuple[str, str]:
    """주어진 연·월의 시작·끝 날짜(YYYY-MM-DD)."""
    from calendar import monthrange
    from datetime import date
    last = monthrange(year, month)[1]
    return date(year, month, 1).isoformat(), date(year, month, last).isoformat()


def _prev_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _delta_pct(curr: float, prev: float) -> float | None:
    """전월 대비 변화율(%). 전월이 0이면 None (신규 등장)."""
    if prev == 0:
        return None
    return round(100.0 * (curr - prev) / prev, 1)


def _channel_conversion_table(rows):
    """입원경로 그룹별·세부별 (상담수, 입원완료, 전환율). 한 상담이 다중 채널이면 각각 +1."""
    by_group_total, by_group_completed = {}, {}
    by_detail_total, by_detail_completed = {}, {}
    for r in rows:
        is_completed = (r["admission_status"] or "").strip() in ("입원완료", "퇴원완료")
        # group
        try:
            groups = json.loads(r["referral_source_type"] or "[]")
        except (json.JSONDecodeError, TypeError):
            groups = []
        for g in groups or []:
            if not g:
                continue
            by_group_total[g] = by_group_total.get(g, 0) + 1
            if is_completed:
                by_group_completed[g] = by_group_completed.get(g, 0) + 1
        # detail
        try:
            details = json.loads(r["referral_source_detail"] or "[]")
        except (json.JSONDecodeError, TypeError):
            details = []
        for d in details or []:
            if not d:
                continue
            by_detail_total[d] = by_detail_total.get(d, 0) + 1
            if is_completed:
                by_detail_completed[d] = by_detail_completed.get(d, 0) + 1

    def _build(totals, completes):
        out = []
        for label, total in totals.items():
            done = completes.get(label, 0)
            out.append({
                "label": label,
                "total": total,
                "completed": done,
                "rate": round(100.0 * done / total, 1) if total else 0.0,
            })
        out.sort(key=lambda x: (-x["total"], x["label"]))
        return out

    return {
        "groups": _build(by_group_total, by_group_completed),
        "details": _build(by_detail_total, by_detail_completed),
    }


def _new_hospitals_count(year: int, month: int) -> int:
    """이번 달 처음 등장한 모병원 수. (이전 어느 시점에도 없었던 source_hospital)."""
    f, t = _month_range(year, month)
    conn = get_db()
    rows = conn.execute(
        """
        SELECT DISTINCT source_hospital FROM consultations
        WHERE consult_date BETWEEN ? AND ?
          AND source_hospital IS NOT NULL AND source_hospital != ''
        """,
        (f, t),
    ).fetchall()
    new_count = 0
    for r in rows:
        h = r["source_hospital"]
        prev = conn.execute(
            "SELECT 1 FROM consultations WHERE source_hospital = ? AND consult_date < ? LIMIT 1",
            (h, f),
        ).fetchone()
        if not prev:
            new_count += 1
    conn.close()
    return new_count


def aggregate_monthly(year: int, month: int) -> dict:
    """임원용 월간 보고서 — 이번 달·전월 집계 + 채널 ROI + 신규 모병원 수."""
    f, t = _month_range(year, month)
    py, pm = _prev_month(year, month)
    pf, pt = _month_range(py, pm)

    this_data = aggregate_stats(f, t)
    prev_data = aggregate_stats(pf, pt)

    # 채널 ROI는 이번 달 데이터에서만
    where_sql = "WHERE c.consult_date >= ? AND c.consult_date <= ?"
    sql = f"""
        SELECT c.referral_source_type, c.referral_source_detail, c.admission_status
        FROM consultations c {where_sql}
    """
    conn = get_db()
    chan_rows = conn.execute(sql, (f, t)).fetchall()
    conn.close()
    channel = _channel_conversion_table(chan_rows)

    new_hospitals = _new_hospitals_count(year, month)

    # 활성 채널 수 = 이번 달 referral_source_detail 고유값 개수
    active_channels = len(this_data["by_referral_detail"])

    s_this = this_data["summary"]
    s_prev = prev_data["summary"]

    # KPI 8장 (절대값 + 전월 대비 ±)
    def kpi(name, curr, prev, *, suffix=""):
        return {
            "label": name,
            "value": curr,
            "prev": prev,
            "delta_pct": _delta_pct(float(curr), float(prev)),
            "suffix": suffix,
        }

    kpis = [
        kpi("총 상담", s_this["total"], s_prev["total"], suffix="건"),
        kpi("입원완료", s_this["completed"], s_prev["completed"], suffix="건"),
        kpi("전환율", s_this["conversion_rate"], s_prev["conversion_rate"], suffix="%"),
        kpi("입원취소", s_this["cancelled"], s_prev["cancelled"], suffix="건"),
        kpi("입원보류", s_this["pending"], s_prev["pending"], suffix="건"),
        # 일평균 = total / 월 일수
        {
            "label": "일평균",
            "value": round(s_this["total"] / 30, 1),
            "prev": round(s_prev["total"] / 30, 1),
            "delta_pct": _delta_pct(s_this["total"] / 30, s_prev["total"] / 30),
            "suffix": "건/일",
        },
        kpi("신규 모병원", new_hospitals, 0, suffix="곳"),  # 전월 비교 의미 약해 prev 0 표기
        kpi("활성 채널", active_channels, len(prev_data["by_referral_detail"]), suffix="종"),
    ]

    return {
        "year": year,
        "month": month,
        "from": f, "to": t,
        "prev_from": pf, "prev_to": pt,
        "kpis": kpis,
        "this": this_data,
        "prev": prev_data,
        "channel": channel,
        "by_source_hospital": this_data["by_source_hospital"],
        "by_rejection_reason": this_data["by_rejection_reason"],
        "by_disease_group": this_data["by_disease_group"],
        "by_insurance": this_data["by_insurance"],
        "by_age": this_data["by_age"],
    }


# ─── 환자 생애주기 (3번 요청) ───

def list_lifecycle_events(patient_id: int):
    """환자 1명의 생애주기 이벤트 — 이벤트일 내림차순."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM lifecycle_events WHERE patient_id = ? "
        "ORDER BY COALESCE(event_date, date(created_at)) DESC, id DESC",
        (patient_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_lifecycle_event(*, patient_id, event_type, event_date=None,
                        title=None, detail=None, consultation_id=None,
                        created_by=None) -> int:
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO lifecycle_events
           (patient_id, consultation_id, event_type, event_date, title, detail, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (patient_id, consultation_id, event_type, event_date, title, detail, created_by),
    )
    eid = cur.lastrowid
    conn.commit()
    conn.close()
    return eid


def get_lifecycle_event(event_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM lifecycle_events WHERE id = ?", (event_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_lifecycle_event(event_id: int):
    conn = get_db()
    conn.execute("DELETE FROM lifecycle_events WHERE id = ?", (event_id,))
    conn.commit()
    conn.close()


def set_patient_stage(patient_id: int, stage: str | None):
    """환자 생애주기 단계 변경. 빈값이면 보드에서 제외(미설정)."""
    conn = get_db()
    conn.execute(
        "UPDATE patients SET lifecycle_stage = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (stage or None, patient_id),
    )
    conn.commit()
    conn.close()


def set_patient_blacklist(patient_id: int, on: bool, reason: str | None = None):
    """블랙리스트 지정/해제 (4번 요청)."""
    conn = get_db()
    conn.execute(
        """UPDATE patients SET blacklist = ?, blacklist_reason = ?,
           blacklist_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
           updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
        (1 if on else 0, reason if on else None, 1 if on else 0, patient_id),
    )
    conn.commit()
    conn.close()


def lifecycle_board(*, q=None):
    """생애주기 보드 — 단계(lifecycle_stage)가 지정된 환자만. 단계별 그룹핑은 라우트에서.
    각 dict: 환자 기본 + 최근 상담 요약 + 이벤트 수.
    """
    conn = get_db()
    where = ["p.lifecycle_stage IS NOT NULL", "p.lifecycle_stage != ''"]
    vals = []
    if q:
        where.append("(p.name LIKE ? OR p.guardian_phone LIKE ?)")
        vals += [f"%{q}%", f"%{q}%"]
    where_sql = "WHERE " + " AND ".join(where)
    rows = conn.execute(f"""
        SELECT p.id, p.name, p.gender, p.guardian_name, p.guardian_phone,
               p.lifecycle_stage, p.blacklist, p.blacklist_reason,
               (SELECT COUNT(*) FROM lifecycle_events e WHERE e.patient_id = p.id) AS event_count,
               (SELECT COUNT(*) FROM consultations c WHERE c.patient_id = p.id) AS consult_count,
               (SELECT c.consult_date FROM consultations c WHERE c.patient_id = p.id
                ORDER BY c.consult_date DESC, c.id DESC LIMIT 1) AS last_consult_date,
               (SELECT c.id FROM consultations c WHERE c.patient_id = p.id
                ORDER BY c.consult_date DESC, c.id DESC LIMIT 1) AS last_consult_id,
               (SELECT c.diseases FROM consultations c WHERE c.patient_id = p.id
                ORDER BY c.consult_date DESC, c.id DESC LIMIT 1) AS last_diseases,
               (SELECT c.patient_age FROM consultations c WHERE c.patient_id = p.id
                ORDER BY c.consult_date DESC, c.id DESC LIMIT 1) AS patient_age
        FROM patients p
        {where_sql}
        ORDER BY last_consult_date DESC, p.id DESC
        LIMIT 2000
    """, vals).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["last_diseases"] = json.loads(d["last_diseases"]) if d.get("last_diseases") else []
        except (json.JSONDecodeError, TypeError):
            d["last_diseases"] = []
        out.append(d)
    return out


# ─── 문자 발송 (5번 요청) ───

def list_sms_templates(*, group=None, active_only=True):
    conn = get_db()
    where, vals = [], []
    if active_only:
        where.append("active = 1")
    if group:
        where.append("template_group = ?"); vals.append(group)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT * FROM sms_templates {where_sql} ORDER BY template_group, name",
        vals,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_sms_template(tid: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM sms_templates WHERE id = ?", (tid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_sms_template(*, name, body, template_group="공통") -> int:
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO sms_templates (name, template_group, body) VALUES (?, ?, ?)",
        (name, template_group, body),
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def update_sms_template(tid: int, **fields):
    valid = {k: v for k, v in fields.items()
             if k in ("name", "template_group", "body", "active")}
    if not valid:
        return
    sets = [f"{k} = ?" for k in valid] + ["updated_at = CURRENT_TIMESTAMP"]
    conn = get_db()
    conn.execute(f"UPDATE sms_templates SET {', '.join(sets)} WHERE id = ?",
                 list(valid.values()) + [tid])
    conn.commit()
    conn.close()


def delete_sms_template(tid: int):
    conn = get_db()
    conn.execute("DELETE FROM sms_templates WHERE id = ?", (tid,))
    conn.commit()
    conn.close()


def log_sms(*, consultation_id=None, patient_id=None, template_id=None,
            to_name=None, to_phone=None, body=None, status="manual",
            sent_by=None) -> int:
    """문자 발송 이력 1건 기록. status='manual'(휴대폰 문자앱)|'sent'(게이트웨이)|'failed'."""
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO sms_log
           (consultation_id, patient_id, template_id, to_name, to_phone, body, status, sent_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (consultation_id, patient_id, template_id, to_name, to_phone, body, status, sent_by),
    )
    sid = cur.lastrowid
    conn.commit()
    conn.close()
    return sid


def list_sms_log(limit: int = 100):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM sms_log ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── 옴니채널 — 통합 커뮤니케이션 (인바운드/기타 접점) ───

def create_communication(*, patient_id=None, consultation_id=None, channel=None,
                         direction="in", contact=None, summary=None, body=None,
                         follow_up_at=None, occurred_at=None, created_by=None,
                         status="open") -> int:
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO communications
           (patient_id, consultation_id, channel, direction, contact, summary, body,
            status, follow_up_at, occurred_at, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (patient_id, consultation_id, channel, direction, contact, summary, body,
         status, follow_up_at, occurred_at, created_by),
    )
    cid = cur.lastrowid
    conn.commit()
    conn.close()
    return cid


def get_communication(comm_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM communications WHERE id = ?", (comm_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_communication(comm_id, **fields):
    valid = {k: v for k, v in fields.items()
             if k in ("status", "summary", "body", "follow_up_at", "patient_id", "channel")}
    if not valid:
        return
    sets = [f"{k} = ?" for k in valid]
    conn = get_db()
    conn.execute(f"UPDATE communications SET {', '.join(sets)} WHERE id = ?",
                 list(valid.values()) + [comm_id])
    conn.commit()
    conn.close()


def delete_communication(comm_id):
    conn = get_db()
    conn.execute("DELETE FROM communications WHERE id = ?", (comm_id,))
    conn.commit()
    conn.close()


def patient_timeline(patient_id):
    """환자의 모든 접점을 시간순(최신순)으로 병합 — 상담/문자/생애주기/커뮤니케이션."""
    conn = get_db()
    items = []
    for r in conn.execute(
        "SELECT id, consult_date, consult_time, consult_channel, consult_result "
        "FROM consultations WHERE patient_id = ?", (patient_id,)):
        items.append({
            "kind": "consult", "channel": r["consult_channel"] or "전화",
            "direction": "in", "date": (r["consult_date"] or "")[:10],
            "time": r["consult_time"] or "",
            "title": "상담 — " + (r["consult_result"] or "상담완료"),
            "detail": "", "ref": f"/consult/{r['id']}",
            "del_kind": None, "del_id": None, "status": None,
        })
    for r in conn.execute(
        "SELECT id, created_at, body FROM sms_log WHERE patient_id = ?", (patient_id,)):
        ca = r["created_at"] or ""
        items.append({
            "kind": "sms", "channel": "문자", "direction": "out",
            "date": ca[:10], "time": ca[11:16],
            "title": "문자 발송", "detail": r["body"] or "",
            "ref": None, "del_kind": None, "del_id": None, "status": None,
        })
    for r in conn.execute(
        "SELECT * FROM lifecycle_events WHERE patient_id = ?", (patient_id,)):
        items.append({
            "kind": "lifecycle", "channel": r["event_type"], "direction": "",
            "date": r["event_date"] or (r["created_at"] or "")[:10],
            "time": "", "title": r["title"] or r["event_type"],
            "detail": r["detail"] or "", "ref": None,
            "del_kind": "lifecycle", "del_id": r["id"], "status": None,
        })
    for r in conn.execute(
        "SELECT * FROM communications WHERE patient_id = ?", (patient_id,)):
        oc = r["occurred_at"] or r["created_at"] or ""
        items.append({
            "kind": "comm", "channel": r["channel"] or "기타",
            "direction": r["direction"] or "in",
            "date": oc[:10], "time": oc[11:16],
            "title": r["summary"] or (r["channel"] or "커뮤니케이션"),
            "detail": r["body"] or "", "ref": None,
            "del_kind": "comm", "del_id": r["id"], "status": r["status"],
        })
    for r in conn.execute(
        "SELECT * FROM patient_documents WHERE patient_id = ?", (patient_id,)):
        ca = r["created_at"] or ""
        items.append({
            "kind": "doc", "channel": r["source"] or "문서", "direction": "in",
            "date": ca[:10], "time": ca[11:16],
            "title": "문서 — " + (r["filename"] or "첨부"),
            "detail": r["ai_summary"] or (r["ocr_text"] or "")[:300],
            "ref": f"/documents#doc-{r['id']}",
            "del_kind": "doc", "del_id": r["id"], "status": r["status"],
        })
    conn.close()
    items.sort(key=lambda x: (x["date"] or "", x["time"] or ""), reverse=True)
    return items


def inbox_callbacks():
    """재연락 대기 — consult_result='상담요청'인 상담. 재연락 시기=consult_result_reason."""
    conn = get_db()
    rows = conn.execute(
        """SELECT c.id, c.consult_date, c.consult_result_reason, c.counselor,
                  p.id AS patient_id, p.name AS patient_name, p.guardian_name,
                  p.guardian_phone, p.blacklist
           FROM consultations c JOIN patients p ON p.id = c.patient_id
           WHERE c.consult_result = '상담요청'
           ORDER BY c.consult_date DESC, c.id DESC""").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def inbox_open_communications():
    """미처리 인바운드 — status='open'인 커뮤니케이션."""
    conn = get_db()
    rows = conn.execute(
        """SELECT m.*, p.name AS patient_name, p.blacklist
           FROM communications m LEFT JOIN patients p ON p.id = m.patient_id
           WHERE m.status = 'open'
           ORDER BY COALESCE(m.occurred_at, m.created_at) DESC""").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def inbox_upcoming_admissions(within_days: int = 3):
    """입원예정 임박 — planned_admission_date가 오늘~within_days 이내이고
    아직 입원완료/취소/퇴원완료가 아닌 상담 (입원 안내 문자 대상)."""
    from datetime import date, timedelta
    today = date.today().isoformat()
    until = (date.today() + timedelta(days=within_days)).isoformat()
    conn = get_db()
    rows = conn.execute(
        """SELECT c.id, c.consult_date, c.planned_admission_date, c.attending_doctor,
                  c.admission_status, p.id AS patient_id, p.name AS patient_name,
                  p.guardian_name, p.guardian_phone, p.blacklist
           FROM consultations c JOIN patients p ON p.id = c.patient_id
           WHERE c.planned_admission_date >= ? AND c.planned_admission_date <= ?
             AND (c.admission_status IS NULL OR c.admission_status NOT IN
                  ('입원완료', '입원취소', '퇴원완료'))
           ORDER BY c.planned_admission_date""", (today, until)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── 환자 문서 (팩스/스캔 — OCR + AI 분석) ───

def create_document(*, patient_id=None, consultation_id=None, filename=None,
                    stored_path=None, mime=None, source="업로드",
                    ocr_text=None, ai_summary=None, status="pending",
                    created_by=None) -> int:
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO patient_documents
           (patient_id, consultation_id, filename, stored_path, mime, source,
            ocr_text, ai_summary, status, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (patient_id, consultation_id, filename, stored_path, mime, source,
         ocr_text, ai_summary, status, created_by),
    )
    did = cur.lastrowid
    conn.commit()
    conn.close()
    return did


def get_document(doc_id):
    conn = get_db()
    row = conn.execute(
        "SELECT d.*, p.name AS patient_name FROM patient_documents d "
        "LEFT JOIN patients p ON p.id = d.patient_id WHERE d.id = ?", (doc_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_documents(limit: int = 200):
    conn = get_db()
    rows = conn.execute(
        "SELECT d.*, p.name AS patient_name FROM patient_documents d "
        "LEFT JOIN patients p ON p.id = d.patient_id "
        "ORDER BY d.created_at DESC, d.id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_document(doc_id, **fields):
    valid = {k: v for k, v in fields.items()
             if k in ("patient_id", "consultation_id", "ocr_text", "ai_summary",
                      "status", "source")}
    if not valid:
        return
    sets = [f"{k} = ?" for k in valid]
    conn = get_db()
    conn.execute(f"UPDATE patient_documents SET {', '.join(sets)} WHERE id = ?",
                 list(valid.values()) + [doc_id])
    conn.commit()
    conn.close()


def delete_document(doc_id):
    conn = get_db()
    conn.execute("DELETE FROM patient_documents WHERE id = ?", (doc_id,))
    conn.commit()
    conn.close()


def inbox_documents():
    """인박스용 — 처리 대기(분석 전) 또는 환자 미연결 문서."""
    conn = get_db()
    rows = conn.execute(
        "SELECT d.*, p.name AS patient_name FROM patient_documents d "
        "LEFT JOIN patients p ON p.id = d.patient_id "
        "WHERE d.status = 'pending' OR d.patient_id IS NULL "
        "ORDER BY d.created_at DESC LIMIT 50").fetchall()
    conn.close()
    return [dict(r) for r in rows]
