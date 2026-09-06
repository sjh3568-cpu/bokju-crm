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
import re
import sqlite3
from datetime import date, datetime, timedelta

from werkzeug.security import generate_password_hash

from config import (DIAGNOSIS_SEED, HOSPITAL_ALIASES, MENU_KEYS,
                    SOURCE_HOSPITAL_SEED, STAGE_STALE_DAYS, role_preset)

# DB 위치 — 기본은 코드 폴더 옆. 컨테이너 배포 시 BOKJU_DB_PATH로 마운트 볼륨을 가리킨다
# (예: /data/bokju.db). SQLite WAL은 SMB/NFS에서 동작하지 않으므로 반드시 로컬 파일시스템.
DB_PATH = os.getenv("BOKJU_DB_PATH") or os.path.join(os.path.dirname(__file__), "bokju.db")
_CHRONIC_DISEASE_PREFIXES = (
    "당뇨", "고혈압", "파킨슨", "희귀성난치질환",
    "치매", "인지기능저하", "이상행동", "탈출", "암",
    "마비-편마비", "편마비",
)
_CNS_DISEASE_FILTER_TERMS = (
    "뇌출혈", "뇌경색", "뇌손상", "척수손상", "뇌성마비",
    "마비", "편마비", "사지마비", "중추신경계",
)


def _hide_chronic_disease_labels(labels):
    out = []
    for label in labels or []:
        label = str(label).strip()
        if not label or label == "기저질환":
            continue
        if any(label == p or label.startswith(p + "-") or label.startswith(p + " ")
               for p in _CHRONIC_DISEASE_PREFIXES):
            continue
        out.append(label)
    return out


# 다중 선택 체크박스 → JSON 배열 텍스트로 저장
JSON_FIELDS = {
    "hearing_options", "activity_active", "activity_others",
    "diseases", "arrange_items", "diet_types", "wound_care", "special_care",
    "therapy", "documents_checklist", "transport_method",
    "cost_guidance", "info_provided",
    "referral_source_detail", "referral_source_type",  # 입원경로(다중)
    "external_referral",  # 회복기 불가 시 외부 시설 연계 (다중)
}


# 동시 쓰기 대기 한도(초). 상담사 4명이 같은 순간 저장해도 "database is locked"로
# 실패하지 않도록 넉넉히 잡는다. SQLite는 쓰기를 직렬화하되 대기 중 순서대로 처리한다.
BUSY_TIMEOUT = float(os.getenv("BOKJU_DB_TIMEOUT", "30"))


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT * 1000)}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_columns(conn, table: str, columns: dict[str, str]):
    """누락된 컬럼만 ALTER TABLE ADD COLUMN으로 추가. SQLite는 단순 추가만 지원."""
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migration_done(conn, key: str) -> bool:
    """1회성 마이그레이션 실행 여부. app_meta에 표식이 남으면 다시 돌지 않는다."""
    row = conn.execute("SELECT 1 FROM app_meta WHERE key = ?", (key,)).fetchone()
    return row is not None


def _mark_migration_done(conn, key: str):
    conn.execute(
        "INSERT OR REPLACE INTO app_meta (key, value, updated_at) "
        "VALUES (?, '1', CURRENT_TIMESTAMP)", (key,),
    )


def _migrate_legacy_stages(conn):
    """폐지된 생애주기 단계값(응급치료·회복기·비회복기) → '입원'으로 이관 (1회성).
    lifecycle_stage_changed_at은 건드리지 않는다 — 단계 진입일(재원 시작)이 곧
    정체·평균 재원일 계산의 기준이라 여기서 리셋되면 통계가 망가진다.
    """
    from config import LEGACY_STAGE_MAP
    for old_stage, new_stage in LEGACY_STAGE_MAP.items():
        conn.execute(
            "UPDATE patients SET lifecycle_stage = ? WHERE lifecycle_stage = ?",
            (new_stage, old_stage),
        )


def _migrate_pair_legacy_returns(conn):
    """기존 '복귀' 이벤트 행 → 직전 나감 이벤트의 returned_at으로 이관 (1회성).
    과거 데이터도 '미복귀' 판정에 바로 쓰이게 한다. 복귀 행 자체는 이력으로 보존.

    ※ 반드시 1회만 돈다 — 복귀 처리는 지금도 이력용 '복귀' 행을 계속 남기므로
      매번 돌면 대상 집합이 무한히 늘고, 날짜 없이 기록한 외진 건이 몇 달 전
      복귀 행과 짝지어져 조용히 닫힌다(외진 중 목록·퇴원 차단에서 사라짐).
    """
    if _migration_done(conn, "pair_legacy_returns"):
        return
    rows = conn.execute(
        "SELECT id, consultation_id, event_date, created_by FROM admission_events "
        "WHERE event_type = '복귀' ORDER BY consultation_id, "
        "(event_date IS NULL OR event_date = '') ASC, event_date ASC, id ASC"
    ).fetchall()
    for r in rows:
        conn.execute(
            """UPDATE admission_events SET returned_at = ?, returned_by = ?
               WHERE id = (
                 SELECT id FROM admission_events
                 WHERE consultation_id = ?
                   AND event_type IN ('응급전원', '모병원 외래치료')
                   AND returned_at IS NULL
                   AND (event_date IS NULL OR ? IS NULL OR event_date <= ?)
                 ORDER BY (event_date IS NULL OR event_date = '') ASC,
                          event_date DESC, id DESC LIMIT 1)""",
            (r["event_date"], r["created_by"], r["consultation_id"],
             r["event_date"], r["event_date"]),
        )
    _mark_migration_done(conn, "pair_legacy_returns")


def _episode_status(admission_status, admitted_at=None, discharged_at=None):
    if discharged_at or admission_status == "퇴원완료":
        return "discharged"
    if admitted_at or admission_status == "입원완료":
        return "admitted"
    if admission_status == "입원대기":
        return "waiting"
    return "planned"


def _migrate_admission_episodes(conn):
    """기존 상담의 입원 사실을 회차 테이블에 멱등 이관한다."""
    rows = conn.execute("""
        SELECT c.* FROM consultations c
        WHERE c.admission_status IN ('입원대기','입원예정','입원완료','퇴원완료')
           OR COALESCE(c.actual_admission_date, c.admission_date, c.discharge_date) IS NOT NULL
        ORDER BY c.patient_id, COALESCE(c.actual_admission_date, c.admission_date,
                                        c.planned_admission_date, c.consult_date), c.id
    """).fetchall()
    for row in rows:
        r = dict(row)
        pid = r["patient_id"]
        existing = conn.execute("SELECT episode_no FROM admission_episodes WHERE consultation_id=?",
                                (r["id"],)).fetchone()
        episode_no = (existing[0] if existing else conn.execute(
            "SELECT COALESCE(MAX(episode_no),0)+1 FROM admission_episodes WHERE patient_id=?",
            (pid,)).fetchone()[0])
        admitted = r.get("actual_admission_date") or r.get("admission_date")
        conn.execute("""
            INSERT INTO admission_episodes
              (patient_id, consultation_id, episode_no, status, wait_started_at,
               planned_admission_date, planned_admission_time, admitted_at, discharged_at,
               room_number, discharge_due_date, discharge_destination, discharge_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(consultation_id) DO UPDATE SET
              status=excluded.status, wait_started_at=excluded.wait_started_at,
              planned_admission_date=excluded.planned_admission_date,
              planned_admission_time=excluded.planned_admission_time,
              admitted_at=excluded.admitted_at, discharged_at=excluded.discharged_at,
              room_number=excluded.room_number, discharge_due_date=excluded.discharge_due_date,
              discharge_destination=excluded.discharge_destination,
              discharge_reason=excluded.discharge_reason, updated_at=CURRENT_TIMESTAMP
        """, (pid, r["id"], episode_no,
              _episode_status(r.get("admission_status"), admitted, r.get("discharge_date")),
              r.get("wait_started_at") or (r.get("consult_date") if r.get("admission_status") == "입원대기" else None),
              r.get("planned_admission_date"), r.get("planned_admission_time"), admitted,
              r.get("discharge_date"), r.get("room_number"), r.get("discharge_due_date"),
              r.get("discharge_destination"), r.get("discharge_reason")))
    conn.execute("""
        UPDATE admission_events SET episode_id=(
          SELECT id FROM admission_episodes e
          WHERE e.consultation_id=admission_events.consultation_id)
        WHERE episode_id IS NULL
    """)


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
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

        -- 상담과 분리된 입원 회차. 기존 consultations 필드는 호환을 위해 유지하며
        -- 저장 시 이 테이블에도 동기화한다(1환자 N회 입·퇴원 이력 보존).
        CREATE TABLE IF NOT EXISTS admission_episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
            consultation_id INTEGER UNIQUE REFERENCES consultations(id) ON DELETE SET NULL,
            episode_no INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'waiting',
            wait_started_at DATE,
            planned_admission_date DATE,
            planned_admission_time TEXT,
            admitted_at DATE,
            discharged_at DATE,
            room_number TEXT,
            discharge_due_date DATE,
            discharge_destination TEXT,
            discharge_reason TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(patient_id, episode_no)
        );
        CREATE INDEX IF NOT EXISTS idx_episode_patient ON admission_episodes(patient_id, episode_no DESC);
        CREATE INDEX IF NOT EXISTS idx_episode_status ON admission_episodes(status, admitted_at);

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

        CREATE TABLE IF NOT EXISTS password_reset_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            requested_ip TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            requested_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            resolved_at DATETIME,
            resolved_by INTEGER REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_password_reset_pending
            ON password_reset_requests(status, requested_at DESC);

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

        -- 공지사항과 사용자별 필수 확인 기록.
        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            target_role TEXT DEFAULT 'staff',
            requires_ack INTEGER DEFAULT 1,
            active INTEGER DEFAULT 1,
            expires_at DATE,
            created_by INTEGER,
            created_by_name TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_announcements_active
            ON announcements(active, created_at DESC);
        CREATE TABLE IF NOT EXISTS announcement_reads (
            announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            read_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (announcement_id, user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_announcement_reads_user
            ON announcement_reads(user_id, announcement_id);

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

        -- 입원 중 이벤트 — 입원완료 환자가 입원 기간 중 응급전원·모병원 외래치료
        -- 등으로 외부 의료기관을 다녀온 내역. 상담 1건(입원 1건)에 N개.
        CREATE TABLE IF NOT EXISTS admission_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            consultation_id INTEGER NOT NULL REFERENCES consultations(id) ON DELETE CASCADE,
            event_type TEXT,
            event_date DATE,
            event_time TIME,
            hospital TEXT,
            memo TEXT,
            created_by TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_admevent_consult ON admission_events(consultation_id);

        CREATE TABLE IF NOT EXISTS quick_filters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            filter_json TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # ─── 마이그레이션: 종이 상담일지 항목 매핑 ───
    _ensure_columns(conn, "patients", {
        "address_full": "TEXT",
        "family_info": "TEXT",
        # 생애주기 (3번) — 현재 단계 + 마지막 단계 변경 시점(정체 감지·자동 정리용)
        "lifecycle_stage": "TEXT",
        "lifecycle_stage_changed_at": "DATETIME",
        # 블랙리스트 (4번)
        "blacklist": "INTEGER DEFAULT 0",
        "blacklist_reason": "TEXT",
        "blacklist_at": "DATETIME",
        # 관리 태그 (이사장 소개·VIP 등) — JSON 배열 텍스트
        "mgmt_tags": "TEXT",
    })
    _ensure_columns(conn, "consultations", {
        # 헤더
        "planned_admission_date": "DATE",
        "planned_admission_time": "TEXT",
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
        "discharge_destination": "TEXT",  # 퇴원 후 이동 기관·장소
        "discharge_reason": "TEXT",       # 퇴원 사유
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
        # ── 회복기 불가 → 같은 재단·외부 시설 연계 추적 (2026-05-24) ──
        # 매뉴얼 슬라이드 5: "회복기에 해당 안 되는 경우 의료필요도에 따라 경도요양병원 또는
        # 복주요양원 안내". 안내 시설을 다중 체크박스로 기록 → 수요 캡처율 KPI 산출.
        "external_referral": "TEXT",      # JSON 배열 (경도요양병원/복주요양원/타 요양병원/타 요양원/기타)
        "external_referral_note": "TEXT", # 연계 자유 메모
        # ── 대시보드 follow-up 추적 (2026-05-26) ──
        # 회복기→비회복기 전환 D-30 알림에 대한 보호자 전화 완료 시각.
        "recovery_call_at": "DATETIME",
        "recovery_call_by": "TEXT",
        # 퇴원예정 D-30 환자의 1차 병동 면담 완료 시각.
        "discharge_interview_at": "DATETIME",
        "discharge_sms_at": "DATETIME",
        "discharge_sms_by": "TEXT",
        # 입원 대기 관리 — 우선순위·병상 요구·연락 이력/다음 연락일
        "wait_started_at": "DATE",
        "wait_priority": "TEXT DEFAULT '일반'",
        "wait_preferred_ward": "TEXT",
        "wait_bed_requirements": "TEXT",
        "wait_last_contact_at": "DATETIME",
        "wait_next_contact_date": "DATE",
        "wait_contact_by": "TEXT",
        "wait_cancel_reason": "TEXT",
    })
    # ─── 외진(응급전원·모병원 외래치료) 出/歸 페어링 ───
    # 나감 이벤트 1행이 복귀일까지 들고 있는다 → '지금 나가 있는 환자'를
    # returned_at IS NULL 한 조건으로 판정. 별도 '복귀' 행에 의존하지 않는다.
    _ensure_columns(conn, "admission_events", {
        "episode_id": "INTEGER REFERENCES admission_episodes(id)",
        "event_time": "TIME",       # 이송 시각
        "returned_at": "DATE",      # 복귀일 (NULL = 아직 병원 밖)
        "returned_by": "TEXT",      # 복귀 처리한 상담사
        "stage_before": "TEXT",     # 나가기 직전 생애주기 단계 → 복귀 시 원상복구
    })
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admevent_episode ON admission_events(episode_id)")
    _migrate_pair_legacy_returns(conn)
    _migrate_legacy_stages(conn)
    _ensure_columns(conn, "source_hospitals", {
        "kind": "TEXT",           # 종별: 상급종합/종합병원/병원/요양병원 등
        "address": "TEXT",
        "phone": "TEXT",
        "official_code": "TEXT",  # HIRA/API 식별자(제공 시)
        "source": "TEXT",         # seed/hira_api/hira_csv 등
        "updated_at": "DATETIME",
        "use_count": "INTEGER DEFAULT 0",  # 폼 저장 시 증가. 자동완성 정렬 가중치.
    })
    # 사용자 메뉴별 세부 권한 — {menu_key: level} JSON. NULL이면 role 프리셋을 따른다.
    _ensure_columns(conn, "users", {
        "permissions": "TEXT",
    })
    # 요양원(노인의료복지시설) 별도 마스터 — 보건복지부·국민건강보험공단 데이터.
    # 자동완성·정식명 강제는 병원과 동일 룰을 공유하지만 마스터는 분리.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_nursing_homes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            region TEXT,
            kind TEXT,           -- 노인요양시설/노인요양공동생활가정 등
            address TEXT,
            phone TEXT,
            official_code TEXT,
            source TEXT,
            active INTEGER DEFAULT 1,
            use_count INTEGER DEFAULT 0,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # 상담사 개인 할 일(To-Do) — 계정별·일자별. 리마인드 시각(remind_at) 선택.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            due_date DATE NOT NULL,
            title TEXT NOT NULL,
            note TEXT,
            done INTEGER DEFAULT 0,
            done_at DATETIME,
            remind_at DATETIME,
            sort_order INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # 그룹웨어형 확장: 기간(due_date=시작일 ~ end_date=종료일)·진행률·D-day
    # start_time/end_time = 'HH:MM' (선택). 같은 날 정렬·제목 앞 시간 표시용.
    _ensure_columns(conn, "todos", {
        "end_date": "DATE",
        "progress": "INTEGER DEFAULT 0",
        "dday": "INTEGER DEFAULT 0",
        "start_time": "TEXT",
        "end_time": "TEXT",
        "patient_id": "INTEGER",     # 연결된 환자(선택) — 상담/환자 화면에서 만든 할 일
        "patient_name": "TEXT",      # 표시용 스냅샷 (조회 시 JOIN 없이 바로 사용)
        "repeat_group": "TEXT",      # 반복 일정 시리즈 묶음 id (같은 값=한 반복 그룹)
    })
    conn.execute("""
        CREATE TABLE IF NOT EXISTS todo_shares (
            todo_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            shared_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            seen_at DATETIME,
            PRIMARY KEY (todo_id, user_id),
            FOREIGN KEY (todo_id) REFERENCES todos(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_todo_shares_user ON todo_shares(user_id, seen_at)")
    # 할 일 알림 — 공유 할 일 완료 시 다른 참여자에게 전달('완료 알림').
    conn.execute("""
        CREATE TABLE IF NOT EXISTS todo_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            todo_id INTEGER,
            kind TEXT DEFAULT 'done',
            actor_name TEXT,
            todo_title TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            seen_at DATETIME
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_todo_notif_user ON todo_notifications(user_id, seen_at)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_todos_user_date ON todos(user_id, due_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_patients_lifecycle "
        "ON patients(lifecycle_stage, lifecycle_stage_changed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cons_patient_recent "
        "ON consultations(patient_id, consult_date DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cons_patient_doctor "
        "ON consultations(patient_id, attending_doctor)"
    )
    # 입원 진행 상태 정리 (2026-05): 구식 '입원확정'과 엑셀 구식 '방문예정'을
    # '입원예정'으로 통합. 멱등.
    # (2026-05-25 변경: '입원예정'은 정식 상태로 복원됨 — 더 이상 변환하지 않음)
    conn.execute(
        "UPDATE consultations SET admission_status = '입원예정' "
        "WHERE admission_status IN ('입원확정', '방문예정')"
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
    _migrate_admission_episodes(conn)

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
        conn.execute(
            "UPDATE source_hospitals SET region = ? "
            "WHERE name = ? AND (region IS NULL OR TRIM(region) = '')",
            (region, name),
        )
    for official, aliases in HOSPITAL_ALIASES.items():
        for alias in aliases:
            conn.execute(
                "UPDATE consultations SET source_hospital = ? "
                "WHERE source_hospital = ?",
                (official, alias),
            )
            conn.execute(
                "UPDATE consultations SET current_location_name = ? "
                "WHERE current_location_name = ?",
                (official, alias),
            )

    # 문자 템플릿 — 비어있을 때만 예시 시드 (실제 문구는 화면에서 수정).
    if conn.execute("SELECT COUNT(*) FROM sms_templates").fetchone()[0] == 0:
        for name, grp, body in _SMS_TEMPLATE_SEED:
            conn.execute(
                "INSERT INTO sms_templates (name, template_group, body) VALUES (?, ?, ?)",
                (name, grp, body),
            )

    if conn.execute("SELECT COUNT(*) FROM quick_filters").fetchone()[0] == 0:
        for idx, item in enumerate(_QUICK_FILTER_SEED, start=1):
            conn.execute(
                """
                INSERT INTO quick_filters (label, filter_json, sort_order, active)
                VALUES (?, ?, ?, 1)
                """,
                (item["label"], json.dumps(item["filter"], ensure_ascii=False), idx),
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


_QUICK_FILTER_SEED = [
    {"label": "오늘 상담", "filter": {"preset": "today"}},
    {"label": "입원예정", "filter": {"params": {"admission_status": "입원예정"}}},
    {"label": "입원완료", "filter": {"params": {"admission_status": "입원완료"}}},
    {"label": "입원보류", "filter": {"params": {"admission_status": "입원보류"}}},
    {"label": "입원취소", "filter": {"params": {"admission_status": "입원취소"}}},
    {"label": "퇴원완료", "filter": {"params": {"admission_status": "퇴원완료"}}},
    {"label": "회복기", "filter": {"params": {"recovery": "회복기"}}},
    {"label": "블랙리스트", "filter": {"params": {"blacklist": "1"}}},
    {"label": "상담요청", "filter": {"params": {"consult_result": "상담요청"}}},
]


def list_quick_filters(include_inactive: bool = False):
    conn = get_db()
    sql = "SELECT * FROM quick_filters"
    if not include_inactive:
        sql += " WHERE active = 1"
    sql += " ORDER BY sort_order ASC, id ASC"
    rows = conn.execute(sql).fetchall()
    conn.close()
    out = []
    for row in rows:
        item = dict(row)
        try:
            item["filter"] = json.loads(item.get("filter_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            item["filter"] = {}
        out.append(item)
    return out


def replace_quick_filters(items):
    conn = get_db()
    conn.execute("DELETE FROM quick_filters")
    for idx, item in enumerate(items, start=1):
        conn.execute(
            """
            INSERT INTO quick_filters (label, filter_json, sort_order, active)
            VALUES (?, ?, ?, ?)
            """,
            (
                item["label"],
                json.dumps(item["filter"], ensure_ascii=False),
                item.get("sort_order") or idx,
                1 if item.get("active", True) else 0,
            ),
        )
    conn.commit()
    conn.close()


# ─── 사용자 / 감사 로그 ───

def ensure_admin_user(username: str, password: str, display_name: str | None = None):
    conn = get_db()
    pw_hash = generate_password_hash(password)
    perms = json.dumps(role_preset("admin"))
    conn.execute(
        """
        INSERT INTO users (username, display_name, password_hash, role, permissions)
        VALUES (?, ?, ?, 'admin', ?)
        ON CONFLICT(username) DO UPDATE SET
            password_hash = excluded.password_hash,
            display_name = COALESCE(excluded.display_name, users.display_name),
            permissions = excluded.permissions,
            active = 1
        """,
        (username, display_name or username, pw_hash, perms),
    )
    conn.commit()
    conn.close()


def _hydrate_user(row) -> dict:
    """DB row → dict. permissions JSON을 파싱하고, 역할 프리셋 위에 덮어 '유효 권한'(perms)을 채운다.
    - perms_raw: DB에 저장된 값(부분적일 수 있음)
    - perms    : 모든 메뉴 키가 채워진 최종 판정용 매트릭스
    """
    u = dict(row)
    raw = {}
    if u.get("permissions"):
        try:
            parsed = json.loads(u["permissions"])
            if isinstance(parsed, dict):
                raw = {k: int(v) for k, v in parsed.items() if k in MENU_KEYS}
        except (ValueError, TypeError):
            raw = {}
    eff = role_preset(u.get("role", "viewer"))
    eff.update(raw)
    u["perms_raw"] = raw
    u["perms"] = {k: int(eff.get(k, 0)) for k in MENU_KEYS}
    return u


def ensure_seed_user(username: str, display_name: str, role: str, password: str):
    """시드 계정을 없을 때만 생성. 이미 있으면 건드리지 않아 설정된 비번·역할·권한이 보존된다."""
    conn = get_db()
    exists = conn.execute(
        "SELECT 1 FROM users WHERE username = ?", (username,)
    ).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO users (username, display_name, password_hash, role, permissions) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, display_name, generate_password_hash(password), role,
             json.dumps(role_preset(role))),
        )
        conn.commit()
    conn.close()


def list_users():
    """전체 계정 목록 (역할 서열 내림차순 → 이름 순). 각 항목에 유효 권한(perms) 포함."""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT * FROM users
        ORDER BY CASE role WHEN 'admin' THEN 3 WHEN 'staff' THEN 2 ELSE 1 END DESC,
                 display_name COLLATE NOCASE
        """
    ).fetchall()
    conn.close()
    return [_hydrate_user(r) for r in rows]


def get_user_by_id(user_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return _hydrate_user(row) if row else None


def create_user(username: str, display_name: str, role: str, password: str,
                permissions: dict | None = None):
    """새 계정 생성. 중복 아이디면 ValueError. 권한 미지정 시 역할 프리셋 사용."""
    conn = get_db()
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        conn.close()
        raise ValueError("이미 존재하는 아이디입니다.")
    perms = permissions if permissions is not None else role_preset(role)
    conn.execute(
        "INSERT INTO users (username, display_name, password_hash, role, permissions) "
        "VALUES (?, ?, ?, ?, ?)",
        (username, display_name or username, generate_password_hash(password), role,
         json.dumps(perms)),
    )
    conn.commit()
    conn.close()


def set_user_password(user_id: int, password: str):
    conn = get_db()
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(password), user_id),
    )
    conn.commit()
    conn.close()


def set_user_permissions(user_id: int, permissions: dict):
    """메뉴별 권한 매트릭스 저장 (MENU_KEYS만, 0~3으로 정규화)."""
    clean = {k: max(0, min(3, int(permissions.get(k, 0)))) for k in MENU_KEYS}
    conn = get_db()
    conn.execute(
        "UPDATE users SET permissions = ? WHERE id = ?",
        (json.dumps(clean), user_id),
    )
    conn.commit()
    conn.close()


def update_user(user_id: int, display_name: str, role: str,
                permissions: dict | None = None):
    conn = get_db()
    if permissions is not None:
        clean = {k: max(0, min(3, int(permissions.get(k, 0)))) for k in MENU_KEYS}
        conn.execute(
            "UPDATE users SET display_name = ?, role = ?, permissions = ? WHERE id = ?",
            (display_name, role, json.dumps(clean), user_id),
        )
    else:
        conn.execute(
            "UPDATE users SET display_name = ?, role = ? WHERE id = ?",
            (display_name, role, user_id),
        )
    conn.commit()
    conn.close()


def set_user_active(user_id: int, active: bool):
    conn = get_db()
    conn.execute(
        "UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, user_id)
    )
    conn.commit()
    conn.close()


def delete_user(user_id: int):
    conn = get_db()
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()


def count_active_admins() -> int:
    conn = get_db()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND active = 1"
    ).fetchone()["n"]
    conn.close()
    return n


def get_user(username: str):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM users WHERE username = ? AND active = 1",
        (username,),
    ).fetchone()
    conn.close()
    return _hydrate_user(row) if row else None


def create_password_reset_request(username: str, requested_ip: str | None = None) -> bool:
    """활성 계정의 미처리 초기화 요청을 1건만 유지한다. 반환값은 외부에 노출하지 않는다."""
    conn = get_db()
    user = conn.execute(
        "SELECT id FROM users WHERE username = ? AND active = 1", (username,)
    ).fetchone()
    if not user:
        conn.close()
        return False
    exists = conn.execute(
        "SELECT 1 FROM password_reset_requests WHERE user_id = ? AND status = 'pending'",
        (user["id"],),
    ).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO password_reset_requests (user_id, requested_ip) VALUES (?, ?)",
            (user["id"], requested_ip),
        )
        conn.commit()
    conn.close()
    return True


def list_pending_password_reset_requests():
    conn = get_db()
    rows = conn.execute("""
        SELECT r.id, r.user_id, r.requested_at,
               u.username, u.display_name
        FROM password_reset_requests r
        JOIN users u ON u.id = r.user_id
        WHERE r.status = 'pending' AND u.active = 1
        ORDER BY r.requested_at ASC, r.id ASC
    """).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def pending_password_reset_count() -> int:
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM password_reset_requests WHERE status = 'pending'"
    ).fetchone()
    conn.close()
    return int(row["n"] or 0)


def resolve_password_reset_requests(user_id: int, resolved_by: int) -> int:
    conn = get_db()
    cur = conn.execute("""
        UPDATE password_reset_requests
        SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP, resolved_by = ?
        WHERE user_id = ? AND status = 'pending'
    """, (resolved_by, user_id))
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return changed


def resolve_password_reset_request(request_id: int, resolved_by: int) -> bool:
    conn = get_db()
    cur = conn.execute("""
        UPDATE password_reset_requests
        SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP, resolved_by = ?
        WHERE id = ? AND status = 'pending'
    """, (resolved_by, request_id))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


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


# ─── 공지사항 ───

def create_announcement(*, title: str, body: str, target_role: str = "staff",
                        requires_ack: bool = True, expires_at: str | None = None,
                        created_by: int | None = None,
                        created_by_name: str | None = None) -> int:
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO announcements
           (title, body, target_role, requires_ack, expires_at, created_by, created_by_name)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (title, body, target_role, 1 if requires_ack else 0, expires_at or None,
         created_by, created_by_name),
    )
    conn.commit()
    notice_id = cur.lastrowid
    conn.close()
    return notice_id


def list_announcements(user_id: int, role: str, *, include_inactive: bool = False,
                       date_from: str | None = None, date_to: str | None = None):
    conn = get_db()
    where = [] if include_inactive else ["a.active = 1"]
    if not include_inactive:
        where.append("(a.expires_at IS NULL OR a.expires_at = '' OR a.expires_at >= date('now', 'localtime'))")
        where.append("(a.target_role = 'all' OR a.target_role = ?)")
    vals = [] if include_inactive else [role]
    if date_from:
        where.append("date(a.created_at, 'localtime') >= date(?)")
        vals.append(date_from)
    if date_to:
        where.append("date(a.created_at, 'localtime') <= date(?)")
        vals.append(date_to)
    rows = conn.execute(f"""
        SELECT a.*,
               EXISTS(SELECT 1 FROM announcement_reads r
                      WHERE r.announcement_id = a.id AND r.user_id = ?) AS acknowledged,
               (SELECT datetime(r.read_at, 'localtime') FROM announcement_reads r
                WHERE r.announcement_id = a.id AND r.user_id = ?) AS acknowledged_at,
               (SELECT COUNT(*) FROM announcement_reads r
                WHERE r.announcement_id = a.id) AS ack_count,
               (SELECT COUNT(*) FROM users u
                WHERE u.active = 1 AND (a.target_role = 'all' OR u.role = a.target_role)) AS target_count
        FROM announcements a
        {('WHERE ' + ' AND '.join(where)) if where else ''}
        ORDER BY a.active DESC, a.created_at DESC, a.id DESC
    """, [user_id, user_id, *vals]).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def first_unread_required_announcement(user_id: int, role: str):
    conn = get_db()
    row = conn.execute("""
        SELECT a.* FROM announcements a
        WHERE a.active = 1 AND a.requires_ack = 1
          AND (a.target_role = 'all' OR a.target_role = ?)
          AND (a.expires_at IS NULL OR a.expires_at = '' OR a.expires_at >= date('now', 'localtime'))
          AND NOT EXISTS (
              SELECT 1 FROM announcement_reads r
              WHERE r.announcement_id = a.id AND r.user_id = ?
          )
        ORDER BY a.created_at ASC, a.id ASC LIMIT 1
    """, (role, user_id)).fetchone()
    conn.close()
    return dict(row) if row else None


def acknowledge_announcement(announcement_id: int, user_id: int, role: str) -> bool:
    conn = get_db()
    allowed = conn.execute("""
        SELECT 1 FROM announcements
        WHERE id = ? AND active = 1
          AND (target_role = 'all' OR target_role = ?)
          AND (expires_at IS NULL OR expires_at = '' OR expires_at >= date('now', 'localtime'))
    """, (announcement_id, role)).fetchone()
    if not allowed:
        conn.close()
        return False
    conn.execute(
        "INSERT OR IGNORE INTO announcement_reads (announcement_id, user_id) VALUES (?, ?)",
        (announcement_id, user_id),
    )
    conn.commit()
    conn.close()
    return True


def set_announcement_active(announcement_id: int, active: bool) -> bool:
    conn = get_db()
    cur = conn.execute(
        "UPDATE announcements SET active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (1 if active else 0, announcement_id),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


# ─── 이력 관리 (audit_log 조회) ───
# audit_log.created_at은 SQLite CURRENT_TIMESTAMP = UTC로 저장된다.
# 화면·필터는 모두 한국시간 기준이므로 읽을 때 datetime(...,'localtime')으로 변환하고,
# 기간 필터는 반대로 입력값을 datetime(...,'utc')로 바꿔 비교한다
# (created_at 원본과 비교해야 idx_audit_created 인덱스를 탄다).

def _build_audit_where(*, date_from=None, date_to=None, username=None,
                       actions=None, exclude_actions=None, target_type=None, q=None):
    where, vals = [], []
    if date_from:
        where.append("a.created_at >= datetime(?, 'utc')")
        vals.append(f"{date_from} 00:00:00")
    if date_to:
        where.append("a.created_at <= datetime(?, 'utc')")
        vals.append(f"{date_to} 23:59:59")
    if username:
        where.append("a.username = ?")
        vals.append(username)
    if actions:
        where.append(f"a.action IN ({','.join('?' * len(actions))})")
        vals.extend(actions)
    if exclude_actions:
        where.append(f"(a.action IS NULL OR a.action NOT IN ({','.join('?' * len(exclude_actions))}))")
        vals.extend(exclude_actions)
    if target_type:
        where.append("a.target_type = ?")
        vals.append(target_type)
    if q:
        like = f"%{q.strip()}%"
        where.append("(a.detail LIKE ? OR a.username LIKE ? OR a.ip LIKE ? "
                     "OR a.action LIKE ? OR CAST(a.target_id AS TEXT) = ?)")
        vals.extend([like, like, like, like, q.strip()])
    return ("WHERE " + " AND ".join(where) if where else ""), vals


def list_audit_logs(*, date_from=None, date_to=None, username=None, actions=None,
                    exclude_actions=None, target_type=None, q=None,
                    limit=100, offset=0):
    """이력 목록 (최신순). created_at은 한국시간 문자열로 변환해 돌려준다."""
    where_sql, vals = _build_audit_where(
        date_from=date_from, date_to=date_to, username=username, actions=actions,
        exclude_actions=exclude_actions, target_type=target_type, q=q)
    conn = get_db()
    rows = conn.execute(
        f"""
        SELECT a.id, a.user_id, a.username, a.action, a.target_type, a.target_id,
               a.detail, a.ip,
               datetime(a.created_at, 'localtime') AS created_at,
               u.display_name AS display_name, u.role AS role
        FROM audit_log a LEFT JOIN users u ON u.id = a.user_id
        {where_sql}
        ORDER BY a.created_at DESC, a.id DESC
        LIMIT ? OFFSET ?
        """,
        [*vals, limit, offset],
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_audit_logs(*, date_from=None, date_to=None, username=None, actions=None,
                     exclude_actions=None, target_type=None, q=None):
    where_sql, vals = _build_audit_where(
        date_from=date_from, date_to=date_to, username=username, actions=actions,
        exclude_actions=exclude_actions, target_type=target_type, q=q)
    conn = get_db()
    n = conn.execute(
        f"SELECT COUNT(*) AS n FROM audit_log a {where_sql}", vals).fetchone()["n"]
    conn.close()
    return n


def audit_action_counts(*, date_from=None, date_to=None, username=None, actions=None,
                        exclude_actions=None, target_type=None, q=None):
    """현재 필터 조건에서 action별 건수 — 화면 상단 요약칩용. {action: n}."""
    where_sql, vals = _build_audit_where(
        date_from=date_from, date_to=date_to, username=username, actions=actions,
        exclude_actions=exclude_actions, target_type=target_type, q=q)
    conn = get_db()
    rows = conn.execute(
        f"SELECT a.action AS action, COUNT(*) AS n FROM audit_log a {where_sql} "
        "GROUP BY a.action ORDER BY n DESC", vals).fetchall()
    conn.close()
    return {r["action"]: r["n"] for r in rows}


def audit_usernames():
    """이력에 등장한 사용자 목록 (필터 드롭다운용). 삭제된 계정도 포함된다."""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT a.username AS username, MAX(u.display_name) AS display_name,
               COUNT(*) AS n
        FROM audit_log a LEFT JOIN users u ON u.id = a.user_id
        WHERE a.username IS NOT NULL AND a.username <> ''
        GROUP BY a.username ORDER BY a.username COLLATE NOCASE
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def audit_target_types():
    """이력에 등장한 대상 종류 목록 (필터 드롭다운용)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT target_type FROM audit_log "
        "WHERE target_type IS NOT NULL AND target_type <> '' ORDER BY target_type"
    ).fetchall()
    conn.close()
    return [r["target_type"] for r in rows]


def audit_log_span():
    """보관 중인 이력의 전체 건수와 가장 오래된/최신 시각 (한국시간)."""
    conn = get_db()
    r = conn.execute(
        "SELECT COUNT(*) AS n, datetime(MIN(created_at), 'localtime') AS oldest, "
        "datetime(MAX(created_at), 'localtime') AS newest FROM audit_log"
    ).fetchone()
    conn.close()
    return dict(r)


# ─── 상담사 개인 할 일(To-Do) ───
# 모든 함수는 user_id로 소유자를 강제해 남의 할 일에 접근하지 못하게 한다.

def _clamp_progress(v):
    try:
        return max(0, min(100, int(v)))
    except (TypeError, ValueError):
        return 0


def create_todo(user_id: int, title: str, due_date: str, *, note: str = "",
                remind_at: str | None = None, end_date: str | None = None,
                progress: int = 0, dday: bool = False,
                start_time: str | None = None, end_time: str | None = None,
                patient_id: int | None = None, patient_name: str | None = None,
                repeat_group: str | None = None) -> int:
    """due_date=시작일(기간 시작), end_date=종료일(선택). start_time/end_time='HH:MM'(선택).
    progress 0~100(100=완료). patient_id/patient_name = 연결 환자(선택).
    repeat_group = 반복 시리즈 묶음 id(선택)."""
    conn = get_db()
    nxt = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM todos WHERE user_id = ? AND due_date = ?",
        (user_id, due_date),
    ).fetchone()["n"]
    progress = _clamp_progress(progress)
    done = 1 if progress >= 100 else 0
    cur = conn.execute(
        "INSERT INTO todos (user_id, due_date, end_date, start_time, end_time, title, note, "
        "remind_at, progress, dday, done, done_at, sort_order, patient_id, patient_name, repeat_group) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, due_date, end_date or None, start_time or None, end_time or None,
         title, note or None, remind_at or None,
         progress, 1 if dday else 0, done,
         datetime.now().isoformat(timespec="seconds") if done else None, nxt,
         patient_id or None, patient_name or None, repeat_group or None),
    )
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


def delete_todo_series(user_id: int, repeat_group: str) -> int:
    """반복 시리즈 전체 삭제. 삭제된 개수 반환."""
    if not repeat_group:
        return 0
    conn = get_db()
    cur = conn.execute(
        "DELETE FROM todos WHERE user_id = ? AND repeat_group = ?",
        (user_id, repeat_group),
    )
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def list_todos_for_patient(user_id: int, patient_id: int):
    """특정 환자에 연결된 내 할 일 (최근 시작일 순)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM todos WHERE user_id = ? AND patient_id = ? "
        "ORDER BY done ASC, due_date DESC, (start_time IS NULL) ASC, start_time ASC, id DESC",
        (user_id, patient_id),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_todo(todo_id: int, user_id: int):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM todos WHERE id = ? AND user_id = ?", (todo_id, user_id)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_todo_access(todo_id: int, user_id: int):
    """소유하거나 공유받은 할 일. is_owner와 공유자 정보를 함께 반환한다."""
    conn = get_db()
    row = conn.execute(
        "SELECT t.*, u.display_name AS owner_name, (t.user_id = ?) AS is_owner "
        "FROM todos t JOIN users u ON u.id = t.user_id "
        "WHERE t.id = ? AND (t.user_id = ? OR EXISTS "
        "(SELECT 1 FROM todo_shares s WHERE s.todo_id=t.id AND s.user_id=?))",
        (user_id, todo_id, user_id, user_id),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def sync_todo_shares(todo_id: int, owner_id: int, user_ids) -> None:
    """공유 대상 동기화. 새 대상은 미확인 알림 상태로 등록한다."""
    clean = sorted({int(x) for x in (user_ids or []) if str(x).isdigit() and int(x) != owner_id})
    conn = get_db()
    valid = {r["id"] for r in conn.execute(
        "SELECT id FROM users WHERE active=1 AND role IN ('admin','staff') AND id != ?",
        (owner_id,)).fetchall()}
    clean = [x for x in clean if x in valid]
    if clean:
        marks = ",".join("?" for _ in clean)
        conn.execute(
            f"DELETE FROM todo_shares WHERE todo_id=? AND user_id NOT IN ({marks})",
            (todo_id, *clean),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO todo_shares(todo_id,user_id) VALUES (?,?)",
            [(todo_id, x) for x in clean],
        )
    else:
        conn.execute("DELETE FROM todo_shares WHERE todo_id=?", (todo_id,))
    conn.commit()
    conn.close()


def todo_share_user_ids(todo_id: int) -> list[int]:
    conn = get_db()
    rows = conn.execute("SELECT user_id FROM todo_shares WHERE todo_id=? ORDER BY user_id", (todo_id,)).fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def mark_todo_shares_seen(user_id: int) -> None:
    conn = get_db()
    conn.execute("UPDATE todo_shares SET seen_at=CURRENT_TIMESTAMP WHERE user_id=? AND seen_at IS NULL", (user_id,))
    conn.commit()
    conn.close()


def unread_shared_todos(user_id: int):
    conn = get_db()
    rows = conn.execute(
        "SELECT t.id, t.title, t.due_date, u.display_name AS owner_name "
        "FROM todo_shares s JOIN todos t ON t.id=s.todo_id JOIN users u ON u.id=t.user_id "
        "WHERE s.user_id=? AND s.seen_at IS NULL ORDER BY s.shared_at",
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def todo_participants(todo_id: int) -> list[int]:
    """할 일의 참여자 = 소유자 + 공유 대상 user_id 목록."""
    conn = get_db()
    ids = set()
    r = conn.execute("SELECT user_id FROM todos WHERE id=?", (todo_id,)).fetchone()
    if r:
        ids.add(r["user_id"])
    for s in conn.execute("SELECT user_id FROM todo_shares WHERE todo_id=?", (todo_id,)):
        ids.add(s["user_id"])
    conn.close()
    return list(ids)


def set_todo_done_any(todo_id: int, done: bool) -> bool:
    """소유자 제한 없이 todo_id로 완료 상태 변경 (호출 측에서 권한 확인). 진행률도 동기화."""
    conn = get_db()
    cur = conn.execute(
        "UPDATE todos SET done=?, progress=?, done_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (1 if done else 0, 100 if done else 0,
         datetime.now().isoformat(timespec="seconds") if done else None, todo_id),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def add_todo_notification(user_id: int, todo_id: int, actor_name: str, todo_title: str,
                          kind: str = "done") -> None:
    conn = get_db()
    conn.execute(
        "INSERT INTO todo_notifications (user_id, todo_id, kind, actor_name, todo_title) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, todo_id, kind, actor_name, todo_title),
    )
    conn.commit()
    conn.close()


def pop_unseen_todo_notifications(user_id: int):
    """미확인 알림을 반환하고 즉시 확인 처리(중복 전송 방지)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, kind, actor_name, todo_title FROM todo_notifications "
        "WHERE user_id=? AND seen_at IS NULL ORDER BY created_at",
        (user_id,),
    ).fetchall()
    if rows:
        conn.execute(
            "UPDATE todo_notifications SET seen_at=CURRENT_TIMESTAMP "
            "WHERE user_id=? AND seen_at IS NULL", (user_id,))
        conn.commit()
    conn.close()
    return [dict(r) for r in rows]


def update_todo(todo_id: int, user_id: int, *, title=None, note=None,
                due_date=None, end_date=None, remind_at=None,
                progress=None, dday=None, start_time=None, end_time=None) -> bool:
    """전달된 필드만 수정. note/end_date/remind_at/시간은 빈 문자열이면 비운다(None).
    progress를 주면 done/done_at도 함께 동기화(100=완료)."""
    sets, vals = [], []
    if title is not None:
        sets.append("title = ?"); vals.append(title)
    if note is not None:
        sets.append("note = ?"); vals.append(note or None)
    if due_date is not None:
        sets.append("due_date = ?"); vals.append(due_date)
    if end_date is not None:
        sets.append("end_date = ?"); vals.append(end_date or None)
    if start_time is not None:
        sets.append("start_time = ?"); vals.append(start_time or None)
    if end_time is not None:
        sets.append("end_time = ?"); vals.append(end_time or None)
    if remind_at is not None:
        sets.append("remind_at = ?"); vals.append(remind_at or None)
    if dday is not None:
        sets.append("dday = ?"); vals.append(1 if dday else 0)
    if progress is not None:
        p = _clamp_progress(progress)
        sets.append("progress = ?"); vals.append(p)
        sets.append("done = ?"); vals.append(1 if p >= 100 else 0)
        sets.append("done_at = ?")
        vals.append(datetime.now().isoformat(timespec="seconds") if p >= 100 else None)
    if not sets:
        return False
    sets.append("updated_at = CURRENT_TIMESTAMP")
    conn = get_db()
    cur = conn.execute(
        f"UPDATE todos SET {', '.join(sets)} WHERE id = ? AND user_id = ?",
        [*vals, todo_id, user_id],
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def set_todo_done(todo_id: int, user_id: int, done: bool) -> bool:
    """완료 토글 — 진행률도 함께 동기화(완료=100%, 해제=0%)."""
    conn = get_db()
    cur = conn.execute(
        "UPDATE todos SET done = ?, progress = ?, done_at = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND (user_id = ? OR EXISTS "
        "(SELECT 1 FROM todo_shares s WHERE s.todo_id=todos.id AND s.user_id=?))",
        (1 if done else 0, 100 if done else 0,
         datetime.now().isoformat(timespec="seconds") if done else None,
         todo_id, user_id, user_id),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def delete_todo(todo_id: int, user_id: int) -> bool:
    conn = get_db()
    cur = conn.execute(
        "DELETE FROM todos WHERE id = ? AND user_id = ?", (todo_id, user_id))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def carry_todo_to(todo_id: int, user_id: int, due_date: str) -> bool:
    """지난 미완료 할 일을 지정 날짜(보통 오늘)로 이월."""
    conn = get_db()
    cur = conn.execute(
        "UPDATE todos SET due_date = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND user_id = ? AND done = 0",
        (due_date, todo_id, user_id),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def list_todos(user_id: int, due_date: str):
    """특정 날짜의 할 일 (미완료 먼저, 시작시간 순, 시간없는 항목은 뒤로)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT t.*, u.display_name AS owner_name, (t.user_id = ?) AS is_owner FROM todos t "
        "JOIN users u ON u.id=t.user_id WHERE (t.user_id = ? OR EXISTS "
        "(SELECT 1 FROM todo_shares s WHERE s.todo_id=t.id AND s.user_id=?)) AND due_date = ? "
        "ORDER BY done ASC, (start_time IS NULL) ASC, start_time ASC, sort_order ASC, id ASC",
        (user_id, user_id, user_id, due_date),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_overdue_todos(user_id: int, before_date: str):
    """지난 날짜의 미완료 할 일 (오래된 순)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT t.*, u.display_name AS owner_name, (t.user_id = ?) AS is_owner FROM todos t "
        "JOIN users u ON u.id=t.user_id WHERE (t.user_id = ? OR EXISTS "
        "(SELECT 1 FROM todo_shares s WHERE s.todo_id=t.id AND s.user_id=?)) "
        "AND done = 0 AND due_date < ? "
        "ORDER BY due_date ASC, (start_time IS NULL) ASC, start_time ASC, sort_order ASC, id ASC",
        (user_id, user_id, user_id, before_date),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def todo_reminder_count(user_id: int, today: str) -> int:
    """리마인드 배지용 — 오늘 이하(오늘+지난)의 미완료 할 일 개수."""
    conn = get_db()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM todos t WHERE (t.user_id = ? OR EXISTS "
        "(SELECT 1 FROM todo_shares s WHERE s.todo_id=t.id AND s.user_id=?)) "
        "AND done = 0 AND due_date <= ?",
        (user_id, user_id, today),
    ).fetchone()["n"]
    conn.close()
    return n


def todo_badge_count(user_id: int, today: str) -> int:
    """오늘까지 미완료 또는 아직 확인하지 않은 공유 할 일의 중복 없는 수."""
    conn = get_db()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM ("
        "SELECT t.id FROM todos t WHERE (t.user_id=? OR EXISTS "
        "(SELECT 1 FROM todo_shares s WHERE s.todo_id=t.id AND s.user_id=?)) "
        "AND t.done=0 AND t.due_date<=? UNION "
        "SELECT todo_id FROM todo_shares WHERE user_id=? AND seen_at IS NULL)",
        (user_id, user_id, today, user_id),
    ).fetchone()["n"]
    conn.close()
    return n


def due_reminder_todos(user_id: int, now_iso: str):
    """리마인드 시각이 지났고 아직 미완료인 할 일 (브라우저 알림용)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, due_date, remind_at FROM todos "
        "WHERE user_id = ? AND done = 0 AND remind_at IS NOT NULL AND remind_at <= ? "
        "ORDER BY remind_at ASC",
        (user_id, now_iso),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_todos_range(user_id: int, first_day: str, last_day: str):
    """달력용 — [first_day, last_day] 기간과 겹치는 모든 할 일.
    기간(due_date~end_date, end_date 없으면 하루)이 조회 구간과 하루라도 겹치면 포함."""
    conn = get_db()
    rows = conn.execute(
        "SELECT t.*, u.display_name AS owner_name, (t.user_id = ?) AS is_owner FROM todos t "
        "JOIN users u ON u.id=t.user_id WHERE (t.user_id = ? OR EXISTS "
        "(SELECT 1 FROM todo_shares s WHERE s.todo_id=t.id AND s.user_id=?)) "
        "AND due_date <= ? AND COALESCE(end_date, due_date) >= ? "
        "ORDER BY due_date ASC, (start_time IS NULL) ASC, start_time ASC, sort_order ASC, id ASC",
        (user_id, user_id, user_id, last_day, first_day),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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


def _parse_tags(raw):
    try:
        v = json.loads(raw) if raw else []
        return [str(t) for t in v] if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def get_patient(pid: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    conn.close()
    if not row:
        return None
    p = dict(row)
    p["mgmt_tags"] = _parse_tags(p.get("mgmt_tags"))
    return p


def set_patient_tags(pid: int, tags) -> None:
    """관리 태그 저장 (공백 제거·중복 제거, 최대 12개)."""
    clean, seen = [], set()
    for t in (tags or []):
        s = str(t).strip()
        if s and s not in seen:
            seen.add(s); clean.append(s)
        if len(clean) >= 12:
            break
    conn = get_db()
    conn.execute("UPDATE patients SET mgmt_tags = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                 (json.dumps(clean, ensure_ascii=False), pid))
    conn.commit()
    conn.close()


def patient_tags_map(patient_ids):
    """여러 환자의 관리 태그를 한 번에 조회 — {pid: [tags]}."""
    ids = [int(x) for x in patient_ids if x]
    if not ids:
        return {}
    conn = get_db()
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, mgmt_tags FROM patients WHERE id IN ({marks})", ids).fetchall()
    conn.close()
    return {r["id"]: _parse_tags(r["mgmt_tags"]) for r in rows}


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
    "planned_admission_date", "planned_admission_time",
    "attending_doctor", "room_number",
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
    "admission_purpose", "admission_purpose_category", "diet_types",
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
    "discharge_due_date", "discharge_date", "discharge_destination", "discharge_reason",
    "recovery_call_at", "recovery_call_by", "discharge_interview_at",
    "discharge_sms_at", "discharge_sms_by",
    "wait_started_at", "wait_priority", "wait_preferred_ward",
    "wait_bed_requirements", "wait_last_contact_at", "wait_next_contact_date",
    "wait_contact_by", "wait_cancel_reason",
    "disuse_screening_note",
    # 회복기 불가 → 같은 재단·외부 시설 연계 안내 (수요 캡처율 KPI)
    "external_referral", "external_referral_note",
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


def _disease_group_clause(column: str, disease_group: str):
    # `column` is supplied by local call sites only.
    from config import DISEASES_GROUPS
    members = DISEASES_GROUPS.get(disease_group, [])
    clauses = [f"{column} LIKE ?"]
    vals = [f'%"{disease_group}"%']
    for member in members:
        clauses.append(f"{column} LIKE ?")
        vals.append(f'%"{member}"%')
    return "(" + " OR ".join(clauses) + ")", vals


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
    sync_admission_episode(cid)
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
    sync_admission_episode(cid)
    _ensure_master_entry(valid.get("source_hospital"), valid.get("primary_diagnosis"))


# 결과(상담·입원 진행 단계) 변경 전용 — 폼 필드와 분리해서 실수 방지
_META_FIELDS = ("consult_result", "consult_result_reason",
                "admission_status", "admission_date", "rejection_reason",
                "rejection_reason_detail", "hold_reason",
                "discharge_due_date", "discharge_date",
                "discharge_destination", "discharge_reason",
                "recovery_call_at", "recovery_call_by", "discharge_interview_at",
                "discharge_sms_at", "discharge_sms_by",
                "wait_started_at", "wait_priority", "wait_preferred_ward",
                "wait_bed_requirements", "wait_last_contact_at",
                "wait_next_contact_date", "wait_contact_by", "wait_cancel_reason")


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
    sync_admission_episode(cid)


def sync_admission_episode(cid: int):
    """상담의 입원 관련 필드를 정규화 회차에 dual-write한다."""
    conn = get_db()
    row = conn.execute("SELECT * FROM consultations WHERE id=?", (cid,)).fetchone()
    if not row:
        conn.close()
        return None
    r = dict(row)
    relevant = (r.get("admission_status") in ("입원대기", "입원예정", "입원완료", "퇴원완료")
                or r.get("actual_admission_date") or r.get("admission_date") or r.get("discharge_date"))
    if not relevant:
        conn.close()
        return None
    existing = conn.execute("SELECT id, episode_no FROM admission_episodes WHERE consultation_id=?",
                            (cid,)).fetchone()
    if existing:
        episode_no = existing["episode_no"]
    else:
        episode_no = conn.execute(
            "SELECT COALESCE(MAX(episode_no),0)+1 FROM admission_episodes WHERE patient_id=?",
            (r["patient_id"],)).fetchone()[0]
    admitted = r.get("actual_admission_date") or r.get("admission_date")
    wait_start = r.get("wait_started_at")
    if r.get("admission_status") == "입원대기" and not wait_start:
        wait_start = r.get("consult_date")
        conn.execute("UPDATE consultations SET wait_started_at=? WHERE id=?", (wait_start, cid))
    conn.execute("""
        INSERT INTO admission_episodes
          (patient_id, consultation_id, episode_no, status, wait_started_at,
           planned_admission_date, planned_admission_time, admitted_at, discharged_at,
           room_number, discharge_due_date, discharge_destination, discharge_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(consultation_id) DO UPDATE SET
          status=excluded.status, wait_started_at=excluded.wait_started_at,
          planned_admission_date=excluded.planned_admission_date,
          planned_admission_time=excluded.planned_admission_time,
          admitted_at=excluded.admitted_at, discharged_at=excluded.discharged_at,
          room_number=excluded.room_number, discharge_due_date=excluded.discharge_due_date,
          discharge_destination=excluded.discharge_destination,
          discharge_reason=excluded.discharge_reason, updated_at=CURRENT_TIMESTAMP
    """, (r["patient_id"], cid, episode_no,
          _episode_status(r.get("admission_status"), admitted, r.get("discharge_date")),
          wait_start, r.get("planned_admission_date"), r.get("planned_admission_time"),
          admitted, r.get("discharge_date"), r.get("room_number"),
          r.get("discharge_due_date"), r.get("discharge_destination"), r.get("discharge_reason")))
    eid = conn.execute("SELECT id FROM admission_episodes WHERE consultation_id=?", (cid,)).fetchone()[0]
    conn.execute("UPDATE admission_events SET episode_id=? WHERE consultation_id=? AND episode_id IS NULL",
                 (eid, cid))
    conn.commit()
    conn.close()
    return eid


def list_admission_episodes(patient_id=None):
    conn = get_db()
    sql = "SELECT * FROM admission_episodes"
    vals = []
    if patient_id is not None:
        sql += " WHERE patient_id=?"
        vals.append(patient_id)
    rows = conn.execute(sql + " ORDER BY patient_id, episode_no DESC", vals).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_app_meta(key, value):
    conn = get_db()
    conn.execute("""INSERT INTO app_meta(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP""",
                 (key, json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value))
    conn.commit(); conn.close()


def get_app_meta(key, default=None):
    conn = get_db()
    row = conn.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    conn.close()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return row[0]


def data_quality_report():
    """업무에 영향을 주는 데이터 오류·누락을 환자 단위로 묶어 반환."""
    conn = get_db()
    base = """SELECT c.id, c.patient_id, p.name AS patient_name, c.consult_date,
                     c.admission_status, c.actual_admission_date, c.admission_date,
                     c.discharge_date, c.discharge_due_date, c.room_number,
                     p.guardian_phone, c.attending_doctor
              FROM consultations c JOIN patients p ON p.id=c.patient_id """
    checks = [
        ("입원일 누락", "입원완료인데 실제 입원일이 없습니다.",
         "WHERE c.admission_status='입원완료' AND COALESCE(c.actual_admission_date,c.admission_date,'')=''"),
        ("호실 누락", "재원 중이지만 호실이 없습니다.",
         "WHERE c.admission_status='입원완료' AND COALESCE(c.actual_admission_date,c.admission_date,'')<>'' AND COALESCE(c.discharge_date,'')='' AND COALESCE(c.room_number,'')=''"),
        ("보호자 연락처 누락", "진행 중인 환자의 연락처가 없습니다.",
         "WHERE c.admission_status IN ('입원대기','입원예정','입원완료') AND COALESCE(p.guardian_phone,'')=''"),
        ("퇴원일 누락", "퇴원완료인데 실제 퇴원일이 없습니다.",
         "WHERE c.admission_status='퇴원완료' AND COALESCE(c.discharge_date,'')=''"),
        ("날짜 역전", "퇴원·예정일이 입원일보다 빠릅니다.",
         "WHERE COALESCE(c.actual_admission_date,c.admission_date,'')<>'' AND ((COALESCE(c.discharge_date,'')<>'' AND c.discharge_date < COALESCE(c.actual_admission_date,c.admission_date)) OR (COALESCE(c.discharge_due_date,'')<>'' AND c.discharge_due_date < COALESCE(c.actual_admission_date,c.admission_date)))"),
    ]
    result = []
    for title, description, where in checks:
        count = conn.execute("SELECT COUNT(*) FROM consultations c JOIN patients p ON p.id=c.patient_id " + where).fetchone()[0]
        rows = [dict(r) for r in conn.execute(base + where + " ORDER BY c.consult_date DESC LIMIT 100").fetchall()]
        result.append({"title": title, "description": description, "count": count,
                       "rows": rows, "truncated": count > len(rows)})
    dupes = [dict(r) for r in conn.execute("""
        SELECT MIN(c.id) AS id, c.patient_id, p.name AS patient_name,
               COUNT(*) AS duplicate_count, MAX(c.consult_date) AS consult_date
        FROM consultations c JOIN patients p ON p.id=c.patient_id
        WHERE c.admission_status='입원완료' AND COALESCE(c.discharge_date,'')=''
        GROUP BY c.patient_id HAVING COUNT(*) > 1 ORDER BY COUNT(*) DESC
    """).fetchall()]
    result.append({"title": "중복 재원", "description": "한 환자에게 미퇴원 입원 회차가 둘 이상입니다.",
                   "count": len(dupes), "rows": dupes})
    conn.close()
    return {"total": sum(x["count"] for x in result), "checks": result,
            "episode_count": len(list_admission_episodes())}


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
                         admission_type=None, consult_result=None, blacklist=None,
                         gender=None, age_min=None, age_max=None,
                         guardian=None, hospital=None, q_scope=None,
                         stay_period=None):
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
        clause, clause_vals = _disease_group_clause("c.diseases", disease_group)
        where.append(clause)
        vals.extend(clause_vals)
    if residence_sido:
        where.append("p.residence_sido = ?"); vals.append(residence_sido)
    if recovery:
        # admission_purpose_category 우선, 없으면 admission_purpose 폴백
        if recovery == "회복기":
            where.append("(c.admission_purpose_category = '회복기' OR (c.admission_purpose_category IS NULL AND (c.admission_purpose LIKE '회복기재활%' OR c.admission_purpose = '회복기')))")
        elif recovery == "비회복기":
            where.append("(c.admission_purpose_category = '비회복기' OR (c.admission_purpose_category IS NULL AND (c.admission_purpose LIKE '비회복기재활%' OR c.admission_purpose = '비회복기')))")
        elif recovery == "일반재활":
            where.append("(c.admission_purpose_category = '일반재활' OR (c.admission_purpose_category IS NULL AND c.admission_purpose LIKE '일반재활%'))")
        elif recovery == "요양":
            where.append("(c.admission_purpose_category = '요양' OR (c.admission_purpose_category IS NULL AND c.admission_purpose IN ('요양', '요양병원')))")
    if consult_channel:
        where.append("c.consult_channel = ?"); vals.append(consult_channel)
    if referral_type:
        # JSON 배열에 그룹명 포함 검색
        where.append("c.referral_source_type LIKE ?")
        vals.append(f'%"{referral_type}"%')
    if q:
        # 기본(상담목록)은 환자명·모병원 — 목록 화면에서 이 입력칸은 환자명 컬럼
        # 헤더에 붙어 있어 의미를 넓히면 컬럼 필터가 어긋난다.
        # 재원 관리(q_scope='ward')만 검색창 안내대로 호실·연락처까지 넓힌다.
        like = f"%{q}%"
        if q_scope == "ward":
            digits = re.sub(r"\D", "", q)
            # 재원관리 통합검색: 표에 보이는 환자·호실뿐 아니라 진단/주치의/
            # 보호자/모병원/보험/수가구분/균·관리태그까지 한 검색어로 찾는다.
            search_cols = (
                "p.name", "c.room_number", "c.attending_doctor", "c.diseases",
                "c.primary_diagnosis", "c.secondary_diagnosis", "c.special_care",
                "c.source_hospital", "c.admission_purpose", "c.admission_purpose_category",
                "c.admission_status", "p.guardian_name", "p.guardian_relation",
                "p.guardian_phone", "p.address_full", "p.residence_sido",
                "p.residence_sigungu", "p.insurance_type", "p.mgmt_tags",
            )
            cols = [f"COALESCE({col}, '') LIKE ?" for col in search_cols]
            qvals = [like] * len(search_cols)
            cols.append("(CASE p.gender WHEN 'M' THEN '남' WHEN 'F' THEN '여' ELSE '미상' END) LIKE ?")
            qvals.append(like)
            cols.append("CAST(COALESCE(c.patient_age, '') AS TEXT) LIKE ?")
            qvals.append(like)
            if digits:
                # 하이픈 없이 친 번호도 잡는다 (01012345678 → 010-1234-5678)
                cols.append("REPLACE(REPLACE(p.guardian_phone, '-', ''), ' ', '') LIKE ?")
                qvals.append(f"%{digits}%")
            where.append("(" + " OR ".join(cols) + ")")
            vals.extend(qvals)
        else:
            where.append("(p.name LIKE ? OR c.source_hospital LIKE ?)")
            vals.extend([like, like])
    # 컬럼별 필터 — 성별·나이 범위·보호자·모병원
    if gender:
        where.append("p.gender = ?"); vals.append(gender)
    if age_min is not None:
        where.append("c.patient_age >= ?"); vals.append(int(age_min))
    if age_max is not None:
        where.append("c.patient_age <= ?"); vals.append(int(age_max))
    if guardian:
        where.append("(p.guardian_name LIKE ? OR p.guardian_phone LIKE ?)")
        like = f"%{guardian}%"
        vals.extend([like, like])
    if hospital:
        where.append("c.source_hospital LIKE ?"); vals.append(f"%{hospital}%")
    if stay_period == "extended_6m":
        cns_clause = " OR ".join("c.diseases LIKE ?" for _ in _CNS_DISEASE_FILTER_TERMS)
        where.append(
            "(c.admission_status = '입원완료' "
            "AND (c.discharge_date IS NULL OR c.discharge_date = '') "
            "AND date(COALESCE(NULLIF(c.actual_admission_date, ''), NULLIF(c.admission_date, '')), '+364 days') < date('now', 'localtime') "
            "AND date('now', 'localtime') <= date(COALESCE(NULLIF(c.actual_admission_date, ''), NULLIF(c.admission_date, '')), '+544 days') "
            f"AND ({cns_clause}))"
        )
        vals.extend(f"%{term}%" for term in _CNS_DISEASE_FILTER_TERMS)
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
                       gender=None, age_min=None, age_max=None,
                       guardian=None, hospital=None, q_scope=None,
                       stay_period=None,
                       sort=None, sort_dir=None,
                       limit=200, offset=0):
    """상담 목록.
    검색·필터: 기간·보험·상담자·입원여부·병명그룹·검색어·성별·나이범위·보호자·모병원.
    정렬: sort(date/patient/residence/hospital/channel/status/counselor) + sort_dir(asc/desc).
    """
    where_sql, vals = _build_consult_where(
        date_from=date_from, date_to=date_to, insurance=insurance, q=q,
        counselor=counselor, admission_status=admission_status,
        disease_group=disease_group, residence_sido=residence_sido,
        recovery=recovery, consult_channel=consult_channel,
        referral_type=referral_type, admission_type=admission_type,
        consult_result=consult_result, blacklist=blacklist,
        gender=gender, age_min=age_min, age_max=age_max,
        guardian=guardian, hospital=hospital, q_scope=q_scope,
        stay_period=stay_period,
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
               c.discharge_destination, c.discharge_reason,
               c.recovery_call_at, c.recovery_call_by,
               c.discharge_interview_at, c.discharge_sms_at, c.discharge_sms_by,
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


def patient_minicard(pid: int) -> dict | None:
    """환자 미니카드 — 상담목록에서 환자명 hover 시 요약 표시용.
    이전 상담 N회, 최근 상담일/결과·입원진행, 마지막 모병원, 블랙리스트, 보호자.
    """
    conn = get_db()
    p = conn.execute(
        """SELECT id, name, gender, residence_sido, residence_sigungu,
                  insurance_type, guardian_name, guardian_relation, guardian_phone,
                  blacklist, blacklist_reason, lifecycle_stage, family_info
           FROM patients WHERE id = ?""",
        (pid,),
    ).fetchone()
    if not p:
        conn.close()
        return None
    n_total = conn.execute(
        "SELECT COUNT(*) FROM consultations WHERE patient_id = ?", (pid,)
    ).fetchone()[0]
    last = conn.execute(
        """SELECT id, consult_date, consult_channel, counselor,
                  consult_result, consult_result_reason,
                  admission_status, source_hospital,
                  planned_admission_date, actual_admission_date
           FROM consultations WHERE patient_id = ?
           ORDER BY consult_date DESC, id DESC LIMIT 1""",
        (pid,),
    ).fetchone()
    conn.close()
    out = dict(p)
    out["total_consultations"] = n_total
    out["prior_count"] = max(n_total - 1, 0)
    out["last"] = dict(last) if last else None
    if last and last["consult_date"]:
        try:
            from datetime import date
            y, m, d = (int(x) for x in last["consult_date"].split("-"))
            delta = (date.today() - date(y, m, d)).days
            out["last_days_ago"] = delta
        except Exception:
            out["last_days_ago"] = None
    else:
        out["last_days_ago"] = None
    return out


def patient_blacklist_info(pid: int) -> dict | None:
    """블랙리스트 환자 상세 — ⚠블랙 마크 클릭 시 모달용.
    환자 기본 정보 + 블랙리스트 사유·등록일 + 전체 상담 이력 요약.
    """
    conn = get_db()
    p = conn.execute(
        """SELECT id, name, gender, residence_sido, residence_sigungu, address_full,
                  insurance_type, guardian_name, guardian_relation, guardian_phone,
                  blacklist, blacklist_reason, blacklist_at, lifecycle_stage
           FROM patients WHERE id = ?""",
        (pid,),
    ).fetchone()
    if not p:
        conn.close()
        return None
    rows = conn.execute(
        """SELECT id, consult_date, consult_channel, counselor,
                  consult_result, consult_result_reason,
                  admission_status, source_hospital,
                  planned_admission_date, actual_admission_date
           FROM consultations WHERE patient_id = ?
           ORDER BY consult_date DESC, id DESC""",
        (pid,),
    ).fetchall()
    conn.close()
    out = dict(p)
    out["consultations"] = [dict(r) for r in rows]
    return out


def merge_patients(source_id: int, target_id: int) -> dict:
    """동명이인 병합 — source 환자의 모든 데이터를 target에 통합 후 source 삭제.
    FK 일괄 이전: consultations / lifecycle_events / sms_log / communications /
    patient_documents. target에 비어있는 환자 필드는 source 값으로 채움.
    반환: {moved: {table: n}, filled_fields: [...], source_name, target_name}
    """
    if source_id == target_id:
        raise ValueError("source와 target이 동일합니다")
    conn = get_db()
    try:
        s = conn.execute("SELECT * FROM patients WHERE id = ?", (source_id,)).fetchone()
        t = conn.execute("SELECT * FROM patients WHERE id = ?", (target_id,)).fetchone()
        if not s or not t:
            raise ValueError("환자를 찾을 수 없습니다")
        if (s["name"] or "") != (t["name"] or ""):
            raise ValueError(f"이름이 다릅니다: '{s['name']}' vs '{t['name']}'")

        moved = {}
        for table in ("consultations", "lifecycle_events", "sms_log",
                      "communications", "patient_documents"):
            cur = conn.execute(
                f"UPDATE {table} SET patient_id = ? WHERE patient_id = ?",
                (target_id, source_id),
            )
            if cur.rowcount:
                moved[table] = cur.rowcount

        FILLABLE = (
            "gender", "residence_sido", "residence_sigungu", "address_full",
            "insurance_type", "guardian_name", "guardian_relation", "guardian_phone",
            "family_info", "lifecycle_stage",
        )
        filled = []
        fill_sets = []
        fill_vals = []
        for col in FILLABLE:
            t_val = t[col]
            is_empty = t_val is None or (isinstance(t_val, str) and not t_val.strip())
            if not is_empty:
                continue
            src_val = s[col]
            if src_val is None:
                continue
            if isinstance(src_val, str) and not src_val.strip():
                continue
            fill_sets.append(f"{col} = ?")
            fill_vals.append(src_val)
            filled.append(col)

        if not t["blacklist"] and s["blacklist"]:
            fill_sets.append("blacklist = ?")
            fill_vals.append(1)
            filled.append("blacklist")
            if s["blacklist_reason"]:
                fill_sets.append("blacklist_reason = ?")
                fill_vals.append(s["blacklist_reason"])
            if s["blacklist_at"]:
                fill_sets.append("blacklist_at = ?")
                fill_vals.append(s["blacklist_at"])

        if fill_sets:
            fill_sets.append("updated_at = CURRENT_TIMESTAMP")
            fill_vals.append(target_id)
            conn.execute(
                f"UPDATE patients SET {', '.join(fill_sets)} WHERE id = ?",
                fill_vals,
            )

        conn.execute("DELETE FROM patients WHERE id = ?", (source_id,))
        conn.commit()
        return {
            "moved": moved,
            "filled_fields": filled,
            "source_id": source_id,
            "target_id": target_id,
            "source_name": s["name"],
            "target_name": t["name"],
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def patients_by_name(name: str) -> list[dict]:
    """같은 이름 환자 전부 — 동명이인 비교 모달용.
    보호자 정보·최근 상담일·최근 모병원·블랙리스트 요약 포함.
    """
    name = (name or "").strip()
    if not name:
        return []
    conn = get_db()
    rows = conn.execute(
        """SELECT id, name, gender, residence_sido, residence_sigungu,
                  insurance_type, guardian_name, guardian_relation, guardian_phone,
                  blacklist, blacklist_reason, address_full, family_info,
                  lifecycle_stage
           FROM patients WHERE name = ? ORDER BY updated_at DESC""",
        (name,),
    ).fetchall()
    if not rows:
        conn.close()
        return []
    items = []
    for r in rows:
        d = dict(r)
        last = conn.execute(
            """SELECT consult_date, consult_channel, counselor,
                      consult_result, admission_status, source_hospital
               FROM consultations WHERE patient_id = ?
               ORDER BY consult_date DESC, id DESC LIMIT 1""",
            (d["id"],),
        ).fetchone()
        n = conn.execute(
            "SELECT COUNT(*) FROM consultations WHERE patient_id = ?", (d["id"],)
        ).fetchone()[0]
        d["last"] = dict(last) if last else None
        d["consultation_count"] = n
        items.append(d)
    conn.close()
    return items


# ─── 자동완성 ───

def _hospital_search_key(value: str | None) -> str:
    value = (value or "").strip().lower()
    for ch in (" ", "\t", "\n", "-", "_", ".", "·", "(", ")", "[", "]"):
        value = value.replace(ch, "")
    for suffix in ("상급종합병원", "종합병원", "대학교병원", "대학병원", "병원", "의료원"):
        value = value.replace(suffix, "")
    return value


def canonical_hospital_name(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    key = _hospital_search_key(raw)
    for official, aliases in HOSPITAL_ALIASES.items():
        keys = {_hospital_search_key(official)}
        keys.update(_hospital_search_key(alias) for alias in aliases)
        if key in keys:
            return official
    return raw


def _hospital_substring_key(value: str | None) -> str:
    """원본 부분 매칭용 — 공백·문장부호만 제거. 접미사는 보존하여
    '경북대'(입력)가 '경북대학교병원'·'칠곡경북대학교병원'에 부분 문자열로 잡히도록 한다."""
    value = (value or "").strip().lower()
    for ch in (" ", "\t", "\n", "-", "_", ".", "·", "(", ")", "[", "]"):
        value = value.replace(ch, "")
    return value


def _hospital_match_score(query: str, row: dict) -> int | None:
    """병원 자동완성 매칭 점수. 낮을수록 우선.

    룰 1 (가장 직관적) — 원본 이름에 입력어가 부분 문자열로 포함되면 매칭.
      예: '경북대' → 경북대학교병원, 경북대학교치과병원, 칠곡경북대학교병원 등 전부.
    룰 2 — 별칭(HOSPITAL_ALIASES)에 부분 일치.
      예: '아산강릉' → 별칭으로 등록된 강릉아산병원.
    룰 3 — 접미사 정규화 후 글자 집합 일치 (순서 무관).
      예: '아산강릉' → '강릉아산병원'(별칭 미등록 시에도).
    """
    q_raw = _hospital_substring_key(query)
    if not q_raw:
        return None
    name = row.get("name") or ""
    name_raw = _hospital_substring_key(name)

    # 룰 1 — 원본 부분 매칭
    if q_raw == name_raw:
        return 0
    if q_raw in name_raw:
        return 5

    # 룰 1b — 입력어 prefix 매칭. 사용자가 '경북대치'(4자)처럼 약칭+카테고리 식으로
    # 짧게 합쳐 쳐도, 의미 있는 prefix(3자 이상)가 이름에 포함되면 후보로 노출.
    # '경북대치' → prefix '경북대' → 경북대학교병원·경북대학교치과병원·칠곡경북대학교병원 모두 매칭.
    if name_raw and len(q_raw) >= 4:
        for L in range(len(q_raw) - 1, 2, -1):  # 길이 N-1부터 3까지
            if q_raw[:L] in name_raw:
                # score는 prefix가 짧을수록(=더 모호) 약해짐: 7 + (q_raw 길이 - L)
                return 7 + (len(q_raw) - L)

    # 룰 2 — 별칭 부분 매칭
    aliases = HOSPITAL_ALIASES.get(name, [])
    for alias in aliases:
        alias_raw = _hospital_substring_key(alias)
        if not alias_raw:
            continue
        if q_raw == alias_raw:
            return 0
        if q_raw in alias_raw or alias_raw in q_raw:
            return 10

    # 글자 집합(흩어진 글자) 매칭은 노이즈 유발로 사용 안 함 — '복주' 검색 시
    # '진주복음병원'·'제주복지요양병원'처럼 단어가 안 들어간 병원이 잡히는 부작용.
    # 매칭은 부분 문자열(룰 1·1b) 또는 별칭(룰 2)으로만 한정.
    return None


def _autocomplete_facilities(q: str, *, table: str, aliases: dict, limit: int = 50):
    """병원·요양원 공통 자동완성. table별로 마스터 로드 후 동일 매칭·정렬 룰 적용.
    같은 score 안에서는 (1) 최근 사용 빈도(use_count) 많은 게 위 (2) 이름 짧은 순.
    """
    conn = get_db()
    rows = conn.execute(
        f"SELECT name, region, kind, address, COALESCE(use_count, 0) AS use_count "
        f"FROM {table} WHERE active = 1 ORDER BY name"
    ).fetchall()
    total_active = conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE active = 1"
    ).fetchone()[0]
    conn.close()
    matches = []
    seen = set()
    for r in rows:
        row = dict(r)
        score = _facility_match_score(q, row, aliases)
        if score is None:
            continue
        # 공식명 정규화는 병원 마스터에서만 (요양원은 별칭 사전 별도). 호출자가 결정.
        canonical = _canonical_from_aliases(row["name"], aliases) or row["name"]
        if canonical in seen:
            continue
        seen.add(canonical)
        row["name"] = canonical
        row["official"] = True
        matches.append((score, -row["use_count"], len(row["name"]), row["name"], row))
    matches.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
    return {
        "items": [row for _, _, _, _, row in matches[:limit]],
        "master_size": total_active,
    }


def _canonical_from_aliases(value, aliases):
    """주어진 별칭 사전에서 정식명을 찾음 (없으면 None)."""
    raw = (value or "").strip()
    if not raw:
        return None
    key = _hospital_search_key(raw)
    for official, alts in aliases.items():
        keys = {_hospital_search_key(official)}
        keys.update(_hospital_search_key(a) for a in alts)
        if key in keys:
            return official
    return raw


def _facility_match_score(query, row, aliases):
    """매칭 룰 — 병원·요양원 공통. 별칭 사전만 다름."""
    q_raw = _hospital_substring_key(query)
    if not q_raw:
        return None
    name = row.get("name") or ""
    name_raw = _hospital_substring_key(name)
    if q_raw == name_raw:
        return 0
    if q_raw in name_raw:
        return 5
    if name_raw and len(q_raw) >= 4:
        for L in range(len(q_raw) - 1, 2, -1):
            if q_raw[:L] in name_raw:
                return 7 + (len(q_raw) - L)
    alts = aliases.get(name, [])
    for alt in alts:
        alt_raw = _hospital_substring_key(alt)
        if not alt_raw:
            continue
        if q_raw == alt_raw:
            return 0
        if q_raw in alt_raw or alt_raw in q_raw:
            return 10
    return None


def autocomplete_hospitals(q: str, limit: int = 50):
    """병원 자동완성. items + master_size 반환."""
    return _autocomplete_facilities(
        q, table="source_hospitals", aliases=HOSPITAL_ALIASES, limit=limit,
    )


# 요양원 별칭 사전 — 사용자 등록 또는 추후 데이터 기반으로 채울 수 있음. 현재 빈 dict.
NURSING_ALIASES: dict[str, list[str]] = {}


def canonical_nursing_name(value):
    """요양원 별칭→공식명 정규화. 마스터에 없으면 입력 그대로 반환."""
    return _canonical_from_aliases(value, NURSING_ALIASES)


def autocomplete_nursing_homes(q: str, limit: int = 50):
    """요양원 자동완성. items + master_size 반환."""
    return _autocomplete_facilities(
        q, table="source_nursing_homes", aliases=NURSING_ALIASES, limit=limit,
    )


def upsert_source_nursing_homes(entries, *, source="import"):
    """요양원 마스터 대량 등록/갱신. upsert_source_hospitals와 동일 시그니처."""
    cleaned = []
    seen = set()
    for item in entries or []:
        name = canonical_nursing_name(item.get("name"))
        if not name or name in seen:
            continue
        seen.add(name)
        cleaned.append({
            "name": name,
            "region": (item.get("region") or "").strip() or None,
            "kind": (item.get("kind") or "").strip() or None,
            "address": (item.get("address") or "").strip() or None,
            "phone": (item.get("phone") or "").strip() or None,
            "official_code": (item.get("official_code") or "").strip() or None,
        })
    if not cleaned:
        return 0
    conn = get_db()
    for item in cleaned:
        conn.execute(
            """
            INSERT INTO source_nursing_homes
                (name, region, kind, address, phone, official_code, source, active, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(name) DO UPDATE SET
                region = COALESCE(excluded.region, source_nursing_homes.region),
                kind = COALESCE(excluded.kind, source_nursing_homes.kind),
                address = COALESCE(excluded.address, source_nursing_homes.address),
                phone = COALESCE(excluded.phone, source_nursing_homes.phone),
                official_code = COALESCE(excluded.official_code, source_nursing_homes.official_code),
                source = excluded.source,
                active = 1,
                updated_at = CURRENT_TIMESTAMP
            """,
            (item["name"], item["region"], item["kind"], item["address"],
             item["phone"], item["official_code"], source),
        )
    conn.commit()
    conn.close()
    return len(cleaned)


def bump_facility_use_count(name, *, table):
    """폼 저장 시 호출. 정식명과 정확히 일치하는 마스터 row의 use_count + 1.
    table은 'source_hospitals' 또는 'source_nursing_homes'."""
    if not name or table not in ("source_hospitals", "source_nursing_homes"):
        return
    conn = get_db()
    conn.execute(
        f"UPDATE {table} SET use_count = COALESCE(use_count, 0) + 1 WHERE name = ?",
        (name,),
    )
    conn.commit()
    conn.close()


def backfill_facility_use_counts():
    """기존 consultations.source_hospital/current_nursing_name 카운트로 use_count 초기화.
    한 번만 실행하면 됨. 마스터 정식명과 정확히 일치하는 건만 반영."""
    conn = get_db()
    conn.execute("""
        UPDATE source_hospitals
        SET use_count = COALESCE((
            SELECT COUNT(*) FROM consultations
            WHERE source_hospital = source_hospitals.name
        ), 0)
    """)
    conn.execute("""
        UPDATE source_nursing_homes
        SET use_count = COALESCE((
            SELECT COUNT(*) FROM consultations
            WHERE current_nursing_name = source_nursing_homes.name
        ), 0)
    """)
    conn.commit()
    conn.close()


def upsert_source_hospitals(entries, *, source="import"):
    """공식 병원명 마스터 대량 등록/갱신.

    entries item keys: name, region, kind, address, phone, official_code.
    같은 이름은 하나의 마스터로 합치고, 비어 있던 부가 정보는 import 값으로 보강한다.
    """
    cleaned = []
    seen = set()
    for item in entries or []:
        name = canonical_hospital_name(item.get("name"))
        if not name or name in seen:
            continue
        seen.add(name)
        cleaned.append({
            "name": name,
            "region": (item.get("region") or "").strip() or None,
            "kind": (item.get("kind") or "").strip() or None,
            "address": (item.get("address") or "").strip() or None,
            "phone": (item.get("phone") or "").strip() or None,
            "official_code": (item.get("official_code") or "").strip() or None,
        })
    if not cleaned:
        return 0

    conn = get_db()
    for item in cleaned:
        conn.execute(
            """
            INSERT INTO source_hospitals
                (name, region, kind, address, phone, official_code, source, active, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(name) DO UPDATE SET
                region = COALESCE(excluded.region, source_hospitals.region),
                kind = COALESCE(excluded.kind, source_hospitals.kind),
                address = COALESCE(excluded.address, source_hospitals.address),
                phone = COALESCE(excluded.phone, source_hospitals.phone),
                official_code = COALESCE(excluded.official_code, source_hospitals.official_code),
                source = excluded.source,
                active = 1,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                item["name"], item["region"], item["kind"], item["address"],
                item["phone"], item["official_code"], source,
            ),
        )
    conn.commit()
    conn.close()
    return len(cleaned)


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
        hospital = canonical_hospital_name(hospital)
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

def dashboard_calendar_rows(first_day: str, last_day: str, counselor: str | None = None):
    """대시보드 통합 달력용 상담·입원·퇴원 관련 날짜 자료.
    counselor 지정 시 해당 상담사 담당 건만 (내 담당만 보기)."""
    conn = get_db()
    where_dates = (
        "(date(c.consult_date) BETWEEN date(?) AND date(?)"
        " OR date(NULLIF(c.planned_admission_date,'')) BETWEEN date(?) AND date(?)"
        " OR date(NULLIF(c.actual_admission_date,'')) BETWEEN date(?) AND date(?)"
        " OR date(NULLIF(c.admission_date,'')) BETWEEN date(?) AND date(?)"
        " OR date(NULLIF(c.discharge_due_date,'')) BETWEEN date(?) AND date(?)"
        " OR date(NULLIF(c.discharge_date,'')) BETWEEN date(?) AND date(?))"
    )
    params = list((first_day, last_day) * 6)
    counselor_sql = ""
    if counselor:
        counselor_sql = " AND c.counselor = ?"
        params.append(counselor)
    rows = conn.execute(
        f"""
        SELECT c.id, c.consult_date, c.consult_time, c.counselor,
               c.admission_status, c.planned_admission_date, c.planned_admission_time,
               c.actual_admission_date, c.admission_date,
               c.discharge_due_date, c.discharge_date,
               p.name AS patient_name
        FROM consultations c JOIN patients p ON p.id=c.patient_id
        WHERE {where_dates}{counselor_sql}
        ORDER BY c.consult_date, c.consult_time, c.id
        """,
        params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def dashboard_summary(admission_lookup_from: str | None = None,
                      admission_lookup_to: str | None = None,
                      admission_lookup_scope: str = "all"):
    """양식 기반 통계 — 상담 건수와 입원예정일 등록 건수."""
    conn = get_db()
    now = datetime.now()
    ym = now.strftime("%Y-%m")
    today = now.strftime("%Y-%m-%d")
    today_d = now.date()
    admission_lookup_from = admission_lookup_from or today
    admission_lookup_to = admission_lookup_to or admission_lookup_from
    try:
        admission_lookup_from_d = datetime.strptime(admission_lookup_from, "%Y-%m-%d").date()
        admission_lookup_to_d = datetime.strptime(admission_lookup_to, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        admission_lookup_from_d = admission_lookup_to_d = today_d
        admission_lookup_from = admission_lookup_to = today
    if admission_lookup_from_d > admission_lookup_to_d:
        admission_lookup_from_d, admission_lookup_to_d = admission_lookup_to_d, admission_lookup_from_d
        admission_lookup_from, admission_lookup_to = admission_lookup_to, admission_lookup_from
    admission_window_days = 15
    week_until_d = today_d + timedelta(days=admission_window_days)
    week_until = week_until_d.isoformat()

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
        SELECT c.id, c.consult_date, c.consult_time, c.counselor,
               c.consult_result, c.consult_result_reason,
               c.admission_status, c.hold_reason,
               c.attending_doctor, c.room_number,
               c.planned_admission_date, c.planned_admission_time,
               c.primary_diagnosis, c.secondary_diagnosis,
               c.diseases, c.disease_detail,
               p.id AS patient_id, p.name AS patient_name, p.gender, p.blacklist
        FROM consultations c JOIN patients p ON p.id = c.patient_id
        WHERE c.consult_date = ?
        ORDER BY COALESCE(c.consult_time, '') DESC, c.id DESC
        """,
        (today,),
    ).fetchall()

    counselor_rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(counselor, ''), '미지정') AS counselor,
               COUNT(*) AS total,
               SUM(CASE WHEN consult_result = '상담보류' THEN 1 ELSE 0 END) AS consult_hold,
               SUM(CASE WHEN admission_status = '입원보류' THEN 1 ELSE 0 END) AS admission_hold,
               SUM(CASE WHEN admission_status = '입원완료' THEN 1 ELSE 0 END) AS admitted,
               SUM(CASE WHEN planned_admission_date IS NOT NULL
                         AND planned_admission_date != '' THEN 1 ELSE 0 END) AS planned
        FROM consultations
        WHERE consult_date = ?
        GROUP BY COALESCE(NULLIF(counselor, ''), '미지정')
        ORDER BY total DESC, counselor
        """,
        (today,),
    ).fetchall()

    admission_schedule_rows = conn.execute(
        """
        SELECT c.id, c.consult_date, c.consult_time, c.counselor,
               c.planned_admission_date, c.planned_admission_time,
               c.actual_admission_date, c.admission_date, c.admission_status,
               c.attending_doctor, c.room_number, c.patient_age,
               c.primary_diagnosis, c.secondary_diagnosis,
               c.diseases, c.disease_detail, c.disease_onset, c.special_care,
               c.special_mrsa_note, c.special_vre_note, c.special_cre_note,
               c.admission_purpose, c.external_referral_note,
               p.id AS patient_id, p.name AS patient_name, p.gender,
               p.insurance_type,
               p.guardian_name, p.guardian_phone, p.blacklist
        FROM consultations c JOIN patients p ON p.id = c.patient_id
        WHERE COALESCE(c.admission_status, '') != '입원취소'
          AND ((COALESCE(c.admission_status, '') != '퇴원완료' AND (
                date(NULLIF(c.planned_admission_date, '')) BETWEEN date(?) AND date(?)
                OR date(NULLIF(c.actual_admission_date, '')) BETWEEN date(?) AND date(?)
                OR date(NULLIF(c.admission_date, '')) BETWEEN date(?) AND date(?)))
               OR date(COALESCE(NULLIF(c.actual_admission_date, ''),
                                NULLIF(c.admission_date, '')))
                  BETWEEN date(?) AND date(?))
        ORDER BY date(COALESCE(
                   CASE WHEN c.admission_status = '입원완료' THEN NULLIF(c.actual_admission_date, '') END,
                   CASE WHEN c.admission_status = '입원완료' THEN NULLIF(c.admission_date, '') END,
                   NULLIF(c.planned_admission_date, ''),
                   NULLIF(c.actual_admission_date, ''),
                   NULLIF(c.admission_date, '')
                 )),
                 COALESCE(c.planned_admission_time, c.consult_time, ''),
                 c.id
        """,
        (today, week_until, today, week_until, today, week_until,
         admission_lookup_from, admission_lookup_to),
    ).fetchall()

    hold_rows = conn.execute(
        """
        SELECT c.id, c.consult_date, c.consult_time, c.counselor,
               c.consult_result, c.consult_result_reason,
               c.admission_status, c.hold_reason,
               c.planned_admission_date, c.attending_doctor, c.updated_at,
               p.id AS patient_id, p.name AS patient_name, p.gender,
               p.guardian_name, p.guardian_phone, p.blacklist
        FROM consultations c JOIN patients p ON p.id = c.patient_id
        WHERE c.consult_result = '상담보류' OR c.admission_status = '입원보류'
        ORDER BY c.updated_at DESC, c.consult_date DESC, c.id DESC
        LIMIT 30
        """).fetchall()

    week_flow_rows = conn.execute(
        """
        SELECT consult_date AS d,
               COUNT(*) AS n,
               SUM(CASE WHEN admission_status = '입원완료' THEN 1 ELSE 0 END) AS admitted,
               SUM(CASE WHEN COALESCE(admission_status, '') != '입원완료'
                         AND (COALESCE(consult_result, '') = '상담취소'
                              OR COALESCE(admission_status, '') = '입원취소')
                        THEN 1 ELSE 0 END) AS canceled,
               SUM(CASE WHEN COALESCE(admission_status, '') != '입원완료'
                         AND NOT (COALESCE(consult_result, '') = '상담취소'
                                  OR COALESCE(admission_status, '') = '입원취소')
                         AND planned_admission_date IS NOT NULL AND planned_admission_date != ''
                        THEN 1 ELSE 0 END) AS planned,
               SUM(CASE WHEN COALESCE(admission_status, '') != '입원완료'
                         AND NOT (COALESCE(consult_result, '') = '상담취소'
                                  OR COALESCE(admission_status, '') = '입원취소')
                         AND (planned_admission_date IS NULL OR planned_admission_date = '')
                         AND (COALESCE(consult_result, '') = '상담보류'
                              OR COALESCE(admission_status, '') = '입원보류')
                        THEN 1 ELSE 0 END) AS hold,
               SUM(CASE WHEN COALESCE(admission_status, '') != '입원완료'
                         AND NOT (COALESCE(consult_result, '') = '상담취소'
                                  OR COALESCE(admission_status, '') = '입원취소')
                         AND (planned_admission_date IS NULL OR planned_admission_date = '')
                         AND NOT (COALESCE(consult_result, '') = '상담보류'
                                  OR COALESCE(admission_status, '') = '입원보류')
                         AND COALESCE(consult_result, '') = '상담요청'
                        THEN 1 ELSE 0 END) AS callback
        FROM consultations
        WHERE consult_date >= date(?, '-6 days') AND consult_date <= ?
        GROUP BY consult_date ORDER BY consult_date
        """,
        (today, today),
    ).fetchall()

    report_week_start_d = today_d - timedelta(days=today_d.weekday())
    report_ranges = {
        "current": (report_week_start_d, report_week_start_d + timedelta(days=6)),
        "previous": (report_week_start_d - timedelta(days=7), report_week_start_d - timedelta(days=1)),
        "year_ago": (report_week_start_d - timedelta(days=364), report_week_start_d - timedelta(days=358)),
    }
    report_from = report_ranges["year_ago"][0].isoformat()
    report_to = report_ranges["current"][1].isoformat()
    weekly_report_rows = conn.execute(
        """
        SELECT c.consult_date, c.consult_channel, c.referral_source_detail,
               c.special_care, c.special_vre_note, c.special_cre_note,
               c.admission_status,
               p.residence_sido, p.residence_sigungu
        FROM consultations c JOIN patients p ON p.id = c.patient_id
        WHERE date(c.consult_date) BETWEEN date(?) AND date(?)
        """,
        (report_from, report_to),
    ).fetchall()

    conn.close()
    summary = dict(row) if row else {}
    total = summary.get("total") or 0
    planned = summary.get("planned") or 0
    summary["plan_rate"] = round(100.0 * planned / total, 1) if total else 0.0
    week_flow_by_date = {r["d"]: dict(r) for r in week_flow_rows}
    week_trend = []
    for offset in range(6, -1, -1):
        d = today_d - timedelta(days=offset)
        key = d.isoformat()
        item = {
            "d": key,
            "label": d.strftime("%m-%d"),
            "n": 0,
            "planned": 0,
            "admitted": 0,
            "hold": 0,
            "callback": 0,
            "canceled": 0,
            "conversion_rate": 0.0,
        }
        item.update({k: (v or 0) for k, v in week_flow_by_date.get(key, {}).items() if k != "d"})
        item["active"] = item["planned"] + item["admitted"]
        item["conversion_rate"] = round(100.0 * item["active"] / item["n"], 1) if item["n"] else 0.0
        week_trend.append(item)
    week_flow_summary = {
        "total": sum(item["n"] for item in week_trend),
        "planned": sum(item["planned"] for item in week_trend),
        "admitted": sum(item["admitted"] for item in week_trend),
        "hold": sum(item["hold"] for item in week_trend),
        "callback": sum(item["callback"] for item in week_trend),
        "canceled": sum(item["canceled"] for item in week_trend),
    }
    week_flow_summary["active"] = week_flow_summary["planned"] + week_flow_summary["admitted"]
    week_flow_summary["conversion_rate"] = (
        round(100.0 * week_flow_summary["active"] / week_flow_summary["total"], 1)
        if week_flow_summary["total"]
        else 0.0
    )

    report_source_keys = ["카페", "검색(블로그)", "유튜브", "SNS", "지인추천", "직원소개", "기관연계", "지역민"]

    def _empty_report_day(day_value):
        return {
            "date": day_value.isoformat(), "label": day_value.strftime("%m/%d"),
            "weekday": "월화수목금토일"[day_value.weekday()], "total": 0,
            "sources": {key: 0 for key in report_source_keys},
            "resistant": {key: 0 for key in report_source_keys},
            "andong": 0, "outside": 0, "phone": 0, "visit": 0,
            "home_channel": 0, "admitted": 0, "admission_rate": 0.0,
        }

    def _report_period(start_d, end_d):
        days = [_empty_report_day(start_d + timedelta(days=i)) for i in range(7)]
        by_date = {item["date"]: item for item in days}
        for raw in weekly_report_rows:
            if not (start_d.isoformat() <= (raw["consult_date"] or "")[:10] <= end_d.isoformat()):
                continue
            item = by_date.get((raw["consult_date"] or "")[:10])
            if not item:
                continue
            item["total"] += 1
            try:
                details = json.loads(raw["referral_source_detail"] or "[]")
            except (TypeError, json.JSONDecodeError):
                details = []
            try:
                special = json.loads(raw["special_care"] or "[]")
            except (TypeError, json.JSONDecodeError):
                special = []
            is_resistant = ("CRE" in special or "VRE" in special
                            or bool((raw["special_cre_note"] or "").strip())
                            or bool((raw["special_vre_note"] or "").strip()))
            for detail in details:
                if detail in item["sources"]:
                    item["sources"][detail] += 1
                    if is_resistant:
                        item["resistant"][detail] += 1
            if "안동" in (raw["residence_sigungu"] or ""):
                item["andong"] += 1
            else:
                item["outside"] += 1
            channel = (raw["consult_channel"] or "").strip()
            if channel == "전화상담":
                item["phone"] += 1
            elif channel == "내원상담":
                item["visit"] += 1
            else:
                item["home_channel"] += 1
            if (raw["admission_status"] or "").strip() == "입원완료":
                item["admitted"] += 1
        for item in days:
            item["admission_rate"] = round(100.0 * item["admitted"] / item["total"], 1) if item["total"] else 0.0
        totals = _empty_report_day(start_d)
        totals["label"], totals["weekday"] = "소계", ""
        for item in days:
            totals["total"] += item["total"]
            totals["andong"] += item["andong"]
            totals["outside"] += item["outside"]
            totals["phone"] += item["phone"]
            totals["visit"] += item["visit"]
            totals["home_channel"] += item["home_channel"]
            totals["admitted"] += item["admitted"]
            for key in report_source_keys:
                totals["sources"][key] += item["sources"][key]
                totals["resistant"][key] += item["resistant"][key]
        totals["admission_rate"] = round(100.0 * totals["admitted"] / totals["total"], 1) if totals["total"] else 0.0
        return {"days": days, "totals": totals}

    weekly_report = {key: _report_period(*dates) for key, dates in report_ranges.items()}
    current_total = weekly_report["current"]["totals"]
    weekly_report["period_label"] = f"{report_ranges['current'][0].strftime('%Y.%m.%d')} ~ {report_ranges['current'][1].strftime('%Y.%m.%d')}"
    weekly_report["source_keys"] = report_source_keys
    weekly_report["comparisons"] = []
    for key, label in (("total", "상담"), ("admitted", "입원"), ("admission_rate", "입원율")):
        current_value = current_total[key]
        row = {"key": key, "label": label, "current": current_value}
        for period_key, prefix in (("previous", "previous"), ("year_ago", "year")):
            base = weekly_report[period_key]["totals"][key]
            row[prefix] = base
            row[prefix + "_diff"] = round(current_value - base, 1)
            row[prefix + "_rate"] = (round(100.0 * (current_value - base) / base, 1) if base else None)
        weekly_report["comparisons"].append(row)

    def _day_label(value):
        if not value:
            return ""
        try:
            d = datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return str(value)
        delta = (d - today_d).days
        if delta == 0:
            return "오늘"
        if delta == 1:
            return "내일"
        if delta == 2:
            return "모레"
        return f"D+{delta}"

    def _date_value(value):
        if not value:
            return None
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None

    def _in_admission_window(value):
        d = _date_value(value)
        return bool(d and (today_d <= d <= week_until_d
                           or admission_lookup_from_d <= d <= admission_lookup_to_d))

    def _consult_disease_labels(item):
        labels = []
        diseases = item.get("diseases") or []
        if isinstance(diseases, list):
            labels.extend(str(v).strip() for v in diseases if str(v).strip())
        elif str(diseases).strip():
            labels.append(str(diseases).strip())
        for key in ("primary_diagnosis", "secondary_diagnosis"):
            value = (item.get(key) or "").strip()
            if value and value not in labels:
                labels.append(value)
        return _hide_chronic_disease_labels(labels)

    def _time_slot(value):
        value = (value or "").strip()
        if not value:
            return "시간 미지정"
        head = value.split(":", 1)[0].strip()
        try:
            hour = int(head)
        except ValueError:
            return "시간 미지정"
        if 0 <= hour <= 23:
            return f"{hour:02d}시대"
        return "시간 미지정"

    today_list = []
    for r in today_rows:
        d = _deserialize_consultation(dict(r))
        d["disease_labels"] = _consult_disease_labels(d)
        d["disease_summary"] = ", ".join(d["disease_labels"][:3])
        d["time_slot"] = _time_slot(d.get("consult_time"))
        today_list.append(d)

    def _group_today(key):
        grouped = {}
        for item in today_list:
            if key == "disease":
                labels = item.get("disease_labels") or ["병명 미지정"]
            else:
                label = (item.get(key) or "").strip() if isinstance(item.get(key), str) else item.get(key)
                labels = [label or "미지정"]
            for label in labels:
                grouped.setdefault(label, []).append(item)
        groups = [
            {"label": label, "count": len(rows), "rows": rows}
            for label, rows in grouped.items()
        ]
        if key == "time_slot":
            return sorted(groups, key=lambda g: (g["label"] == "시간 미지정", g["label"]))
        return sorted(groups, key=lambda g: (-g["count"], g["label"]))

    def _ward_label(room_number):
        room = (room_number or "").strip()
        if not room:
            return "병동 미지정"
        if "병동" in room:
            return room
        digits = ""
        started = False
        for ch in room:
            if ch.isdigit():
                digits += ch
                started = True
            elif started:
                break
        if len(digits) >= 3:
            return f"{digits[0]}병동"
        if digits:
            return f"{digits}병동"
        return room

    def _admission_disease_summary(item):
        """입원 현황에는 대표 질환 한 건과 감염균 표지만 간결하게 표시한다."""
        main_disease = (item.get("primary_diagnosis") or "").strip()
        if not main_disease:
            main_disease = next(iter(_consult_disease_labels(item)), "")

        special_care = item.get("special_care") or []
        if isinstance(special_care, str):
            special_care = [special_care]
        special_text = " ".join(str(value).upper() for value in special_care)
        organisms = []
        for organism, note_key in (
            ("MRSA", "special_mrsa_note"),
            ("VRE", "special_vre_note"),
            ("CRE", "special_cre_note"),
        ):
            if organism in special_text or (item.get(note_key) or "").strip():
                organisms.append(organism)

        return " · ".join(value for value in [main_disease, *organisms] if value)

    admission_schedule = []
    for r in admission_schedule_rows:
        d = _deserialize_consultation(dict(r))
        status = (d.get("admission_status") or "").strip()
        actual_date = d.get("actual_admission_date") or d.get("admission_date") or ""
        planned_date = d.get("planned_admission_date") or ""
        is_completed = status in ("입원완료", "퇴원완료")
        display_date = actual_date if is_completed and actual_date else planned_date
        if not _in_admission_window(display_date):
            continue
        d["admission_bucket"] = "completed" if is_completed else "planned"
        d["admission_bucket_label"] = "입원완료" if is_completed else "입원예정"
        d["admission_date_kind"] = "실입원" if is_completed and actual_date else "예정"
        d["admission_display_date"] = display_date
        d["day_label"] = _day_label(display_date)
        d["admission_time"] = d.get("planned_admission_time") or ""
        d["admission_disease_summary"] = _admission_disease_summary(d)
        d["other_note"] = (
            d.get("external_referral_note")
            or d.get("disease_detail")
            or d.get("admission_purpose")
            or ""
        )
        d["ward"] = _ward_label(d.get("room_number"))
        admission_schedule.append(d)

    # 대시보드 KPI·업무 큐의 기존 15일 범위에는 과거 날짜 조회용 행이 섞이지 않게 분리한다.
    admission_window_schedule = [
        row for row in admission_schedule
        if (value_date := _date_value(row.get("admission_display_date")))
        and today_d <= value_date <= week_until_d
    ]
    admission_by_status = {
        "all": admission_window_schedule,
        "planned": [r for r in admission_window_schedule if r.get("admission_bucket") == "planned"],
        "completed": [r for r in admission_window_schedule if r.get("admission_bucket") == "completed"],
    }

    def _group_admissions(rows, key):
        grouped = {}
        for item in rows:
            label = (item.get(key) or "").strip() if isinstance(item.get(key), str) else item.get(key)
            if not label:
                label = "미지정"
            grouped.setdefault(label, []).append(item)
        return [
            {"label": label, "count": len(rows), "rows": rows}
            for label, rows in grouped.items()
        ]

    admission_groups_by_status = {
        name: {
            "counselor": _group_admissions(rows, "counselor"),
            "doctor": _group_admissions(rows, "attending_doctor"),
            "ward": _group_admissions(rows, "ward"),
        }
        for name, rows in admission_by_status.items()
    }
    admission_selected = [
        row for row in admission_schedule
        if admission_lookup_from <= (row.get("admission_display_date") or "") <= admission_lookup_to
        and (_date_value(row.get("admission_display_date")) >= today_d
             or row.get("admission_bucket") == "completed")
        and (admission_lookup_scope == "all" or row.get("admission_bucket") == admission_lookup_scope)
        and (admission_lookup_scope != "planned"
             or row.get("admission_status") in ("입원예정", "입원대기"))
    ]
    admission_selected_groups = {
        "counselor": _group_admissions(admission_selected, "counselor"),
        "doctor": _group_admissions(admission_selected, "attending_doctor"),
        "ward": _group_admissions(admission_selected, "ward"),
    }

    hold_list = []
    for r in hold_rows:
        d = dict(r)
        d["hold_kind"] = "입원보류" if d.get("admission_status") == "입원보류" else "상담보류"
        d["hold_reason_text"] = d.get("hold_reason") or d.get("consult_result_reason") or ""
        hold_list.append(d)

    summary["today_consults"] = len(today_list)
    summary["admission_window_days"] = admission_window_days
    summary["admission_week"] = len(admission_window_schedule)
    summary["admission_planned_week"] = len(admission_by_status["planned"])
    summary["admission_completed_week"] = len(admission_by_status["completed"])
    summary["admission_today"] = sum(
        1 for r in admission_schedule if r.get("admission_display_date") == today)
    summary["admission_today_planned"] = sum(
        1 for r in admission_by_status["planned"] if r.get("admission_display_date") == today)
    summary["admission_today_completed"] = sum(
        1 for r in admission_by_status["completed"] if r.get("admission_display_date") == today)
    summary["holds"] = len(hold_list)

    def _count_rows(rows, key, default_label):
        grouped = {}
        for item in rows:
            label = (item.get(key) or "").strip() if isinstance(item.get(key), str) else item.get(key)
            if not label:
                label = default_label
            grouped.setdefault(label, []).append(item)
        return [
            {"label": label, "count": len(group_rows), "rows": group_rows}
            for label, group_rows in sorted(
                grouped.items(),
                key=lambda x: (-len(x[1]), x[0]),
            )
        ]

    today_follow_up = sum(
        1 for r in today_list
        if (r.get("consult_result") or "").strip() in ("상담요청", "상담보류")
        or (r.get("admission_status") or "").strip() == "입원보류"
    )
    return {
        "summary": summary,
        "today": today_list,
        "today_groups": {
            "disease": _group_today("disease"),
            "counselor": _group_today("counselor"),
            "time": _group_today("time_slot"),
        },
        "today_outcome": {
            "consult_results": _count_rows(today_list, "consult_result", "상담완료"),
            "admission_statuses": _count_rows(today_list, "admission_status", "미정"),
            "follow_up": today_follow_up,
        },
        "counselor_today": [dict(r) for r in counselor_rows],
        "admission_schedule": admission_window_schedule,
        "admission_by_status": admission_by_status,
        "admission_groups": admission_groups_by_status["all"],
        "admission_groups_by_status": admission_groups_by_status,
        "admission_selected": admission_selected,
        "admission_selected_groups": admission_selected_groups,
        "holds": hold_list,
        "week_trend": week_trend,
        "week_flow_summary": week_flow_summary,
        "weekly_report": weekly_report,
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
    """상담일별 상담·입원 건수와 전환율."""
    by_day = {}
    for r in rows:
        d = (r["consult_date"] or "")[:10]
        if not d:
            continue
        item = by_day.setdefault(d, {"count": 0, "admissions": 0})
        item["count"] += 1
        if (r["admission_status"] or "").strip() in ("입원완료", "퇴원완료"):
            item["admissions"] += 1
    return [{
        "date": d,
        "count": item["count"],
        "admissions": item["admissions"],
        "rate": round(100.0 * item["admissions"] / item["count"], 1) if item["count"] else 0.0,
    } for d, item in sorted(by_day.items())]


def _consult_admission_trend(rows, period="month", limit=None):
    """상담일 기준 상담 건수와 최종 입원 성사 건수를 월/연도별로 집계."""
    buckets = {}
    key_len = 7 if period == "month" else 4
    for row in rows:
        key = (row["consult_date"] or "")[:key_len]
        if len(key) != key_len:
            continue
        item = buckets.setdefault(key, {"label": key, "consults": 0, "admissions": 0})
        item["consults"] += 1
        if (row["admission_status"] or "").strip() in ("입원완료", "퇴원완료"):
            item["admissions"] += 1
    if not buckets:
        return []

    keys = sorted(buckets)
    if period == "month":
        start_year, start_month = map(int, keys[0].split("-"))
        end_year, end_month = map(int, keys[-1].split("-"))
        continuous = []
        year, month = start_year, start_month
        while (year, month) <= (end_year, end_month):
            continuous.append(f"{year:04d}-{month:02d}")
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        keys = continuous[-limit:] if limit else continuous
    else:
        start_year, end_year = int(keys[0]), int(keys[-1])
        keys = [str(year) for year in range(start_year, end_year + 1)]

    result = []
    for key in keys:
        item = buckets.get(key, {"label": key, "consults": 0, "admissions": 0})
        item["rate"] = round(100.0 * item["admissions"] / item["consults"], 1) if item["consults"] else 0.0
        result.append(item)
    return result


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
          c.consult_date, c.consult_time, c.consult_channel, c.counselor, c.patient_age,
          c.referral_source_type, c.referral_source_detail, c.diseases,
          c.planned_admission_date, c.admission_status, c.admission_date,
          c.consult_result, c.admission_type,
          c.source_hospital, c.rejection_reason, c.disuse_screening_note,
          c.external_referral,
          p.gender, p.insurance_type,
          p.residence_sido, p.residence_sigungu
        FROM consultations c JOIN patients p ON p.id = c.patient_id
        {where_sql}
    """
    conn = get_db()
    rows = conn.execute(sql, vals).fetchall()
    all_period_rows = conn.execute(
        "SELECT consult_date, admission_status FROM consultations "
        "WHERE consult_date IS NOT NULL AND TRIM(consult_date) != ''"
    ).fetchall()
    conn.close()

    from config import ADMISSION_STATUS_ALL, CONSULT_RESULTS

    total = len(rows)
    planned = sum(1 for r in rows if (r["planned_admission_date"] or "").strip())

    # 입원 진행 단계 — NULL/빈값('미정')은 입원 단계 미진입. 퇴원완료 포함.
    undecided_status = "상담완료(입원 미정)"
    status_counts = {s: 0 for s in ADMISSION_STATUS_ALL}
    status_counts[undecided_status] = 0
    for r in rows:
        s = (r["admission_status"] or "").strip()
        status_counts[s if s in ADMISSION_STATUS_ALL else undecided_status] += 1
    # 입원완료 + 퇴원완료 = 입원 성사
    completed = status_counts["입원완료"] + status_counts["퇴원완료"]
    cancelled = status_counts["입원취소"]
    # 진행중 = 미정 + 입원보류 (입원/취소로 미확정)
    pending = status_counts[undecided_status] + status_counts["입원보류"]
    conversion_rate = round(100.0 * completed / total, 1) if total else 0.0
    active_days = len({(r["consult_date"] or "")[:10] for r in rows
                       if (r["consult_date"] or "")[:10]})

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

    insurance_counts = {}
    for row in rows:
        label = (row["insurance_type"] or "").strip()
        if not label:
            continue
        normalized = "건강보험" if label in ("보험", "건강보험") else label
        insurance_counts[normalized] = insurance_counts.get(normalized, 0) + 1

    gender_counts = {
        "남": sum(1 for r in rows if (r["gender"] or "").strip() == "M"),
        "여": sum(1 for r in rows if (r["gender"] or "").strip() == "F"),
        "미상": sum(1 for r in rows if (r["gender"] or "").strip() not in ("M", "F")),
    }

    sigungu_top = _sort_desc(_count_simple(rows, "residence_sigungu"))[:10]

    # 요일별 상담량과 전화상담 시간대. 시간 미입력도 누락 현황으로 표시한다.
    weekday_names = ["월", "화", "수", "목", "금", "토", "일"]
    weekday_counts = {name: 0 for name in weekday_names}
    phone_hour_counts = {f"{hour:02d}시": 0 for hour in range(8, 19)}
    phone_hour_counts["기타 시간"] = 0
    phone_hour_counts["시간 미지정"] = 0
    weekday_hour_counts = {}
    from datetime import datetime as _datetime
    for r in rows:
        raw_date = (r["consult_date"] or "")[:10]
        try:
            weekday_counts[weekday_names[_datetime.strptime(raw_date, "%Y-%m-%d").weekday()]] += 1
        except (ValueError, TypeError):
            pass
        if (r["consult_channel"] or "").strip() != "전화상담":
            continue
        raw_time = (r["consult_time"] or "").strip()
        try:
            hour = int(raw_time.split(":", 1)[0])
        except (ValueError, TypeError):
            phone_hour_counts["시간 미지정"] += 1
            continue
        key = f"{hour:02d}시" if 8 <= hour <= 18 else "기타 시간"
        phone_hour_counts[key] += 1
        if 8 <= hour <= 18 and raw_date:
            try:
                weekday = weekday_names[_datetime.strptime(raw_date, "%Y-%m-%d").weekday()]
                weekday_hour_counts[(weekday, hour)] = weekday_hour_counts.get((weekday, hour), 0) + 1
            except (ValueError, TypeError):
                pass

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
        if ((r["admission_status"] or "").strip() != "입원취소" and
                (r["consult_result"] or "").strip() != "상담취소"):
            continue
        v = (r["rejection_reason"] or "").strip() if isinstance(r["rejection_reason"], str) else None
        if v:
            reason_counts[v] = reason_counts.get(v, 0) + 1
    by_reason = _sort_desc(reason_counts)

    def _performance(field, *, limit=10):
        totals, completes = {}, {}
        for row in rows:
            label = (row[field] or "").strip()
            if not label:
                continue
            totals[label] = totals.get(label, 0) + 1
            if (row["admission_status"] or "").strip() in ("입원완료", "퇴원완료"):
                completes[label] = completes.get(label, 0) + 1
        result = [{"label": label, "total": count,
                   "completed": completes.get(label, 0),
                   "rate": round(100.0 * completes.get(label, 0) / count, 1)}
                  for label, count in totals.items()]
        return sorted(result, key=lambda x: (-x["total"], -x["rate"], x["label"]))[:limit]

    def _region_performance_by_sido():
        grouped = {}
        for row in rows:
            sido = (row["residence_sido"] or "").strip()
            sigungu = (row["residence_sigungu"] or "").strip()
            if not sido or not sigungu:
                continue
            item = grouped.setdefault(sido, {}).setdefault(sigungu, {"total": 0, "completed": 0})
            item["total"] += 1
            if (row["admission_status"] or "").strip() in ("입원완료", "퇴원완료"):
                item["completed"] += 1
        result = {}
        for sido, items in grouped.items():
            values = [{
                "label": sigungu,
                "total": counts["total"],
                "completed": counts["completed"],
                "rate": round(100.0 * counts["completed"] / counts["total"], 1),
            } for sigungu, counts in items.items()]
            result[sido] = sorted(values, key=lambda x: (-x["total"], -x["rate"], x["label"]))
        return result

    def _referral_performance_by_type():
        grouped = {}
        for row in rows:
            try:
                types = json.loads(row["referral_source_type"] or "[]")
                details = json.loads(row["referral_source_detail"] or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            is_completed = (row["admission_status"] or "").strip() in ("입원완료", "퇴원완료")
            for type_label in types or []:
                if not type_label:
                    continue
                for detail in (details or ["세부경로 미입력"]):
                    if not detail:
                        continue
                    item = grouped.setdefault(type_label, {}).setdefault(detail, {"total": 0, "completed": 0})
                    item["total"] += 1
                    if is_completed:
                        item["completed"] += 1
        return _nested_performance(grouped)

    def _disease_performance_by_group():
        grouped = {}
        for row in rows:
            try:
                labels = json.loads(row["diseases"] or "[]")
            except (json.JSONDecodeError, TypeError):
                labels = []
            is_completed = (row["admission_status"] or "").strip() in ("입원완료", "퇴원완료")
            for label in labels or []:
                for group, members in DISEASES_GROUPS.items():
                    if label == group or label in members:
                        detail = "세부질환 미입력" if label == group else label
                        item = grouped.setdefault(group, {}).setdefault(detail, {"total": 0, "completed": 0})
                        item["total"] += 1
                        if is_completed:
                            item["completed"] += 1
                        break
        return _nested_performance(grouped)

    def _nested_performance(grouped):
        result = {}
        for parent, children in grouped.items():
            items = [{
                "label": label,
                "total": counts["total"],
                "completed": counts["completed"],
                "rate": round(100.0 * counts["completed"] / counts["total"], 1),
            } for label, counts in children.items()]
            result[parent] = sorted(items, key=lambda x: (-x["total"], -x["rate"], x["label"]))
        return result

    def _disease_group_performance():
        totals = {group: 0 for group in DISEASES_GROUPS}
        completes = {group: 0 for group in DISEASES_GROUPS}
        for row in rows:
            try:
                labels = json.loads(row["diseases"] or "[]")
            except (json.JSONDecodeError, TypeError):
                labels = []
            matched_groups = set()
            for label in labels or []:
                for group, members in DISEASES_GROUPS.items():
                    if label == group or label in members:
                        matched_groups.add(group)
                        break
            is_completed = (row["admission_status"] or "").strip() in ("입원완료", "퇴원완료")
            for group in matched_groups:
                totals[group] += 1
                if is_completed:
                    completes[group] += 1
        result = [{"label": group, "total": totals[group], "completed": completes[group],
                   "rate": round(100.0 * completes[group] / totals[group], 1)}
                  for group in DISEASES_GROUPS if totals[group]]
        return sorted(result, key=lambda x: (-x["total"], -x["rate"], x["label"]))

    # 상담일부터 실제 입원일까지 걸린 기간
    lead_buckets = {"당일": 0, "1~3일": 0, "4~7일": 0, "8~14일": 0, "15일 이상": 0}
    for r in rows:
        if (r["admission_status"] or "").strip() not in ("입원완료", "퇴원완료"):
            continue
        try:
            lead_days = (_datetime.strptime((r["admission_date"] or "")[:10], "%Y-%m-%d") -
                         _datetime.strptime((r["consult_date"] or "")[:10], "%Y-%m-%d")).days
        except (ValueError, TypeError):
            continue
        bucket = ("당일" if lead_days <= 0 else "1~3일" if lead_days <= 3 else
                  "4~7일" if lead_days <= 7 else "8~14일" if lead_days <= 14 else "15일 이상")
        lead_buckets[bucket] += 1

    missing_fields = {
        "상담시간": sum(1 for r in rows if not (r["consult_time"] or "").strip()),
        "유입경로": sum(1 for r in rows if (r["referral_source_detail"] or "").strip() in ("", "[]")),
        "거주지": sum(1 for r in rows if not (r["residence_sigungu"] or "").strip()),
        "질환명": sum(1 for r in rows if (r["diseases"] or "").strip() in ("", "[]")),
    }


    # 회복기 불가 → 외부 시설 연계 (수요 캡처율 KPI)
    # "입원 불가/일반재활 안내" 상태 = 입원취소 OR 상담취소 (회복기 입원으로 안 이어진 케이스)
    # 그 중 external_referral 기록된 비율 = 같은 재단·외부 시설로 안내한 비율
    referral_counts = _count_json_multi(rows, "external_referral")
    by_external_referral = _sort_desc(referral_counts)
    # 입원 불가 케이스 = 입원취소 + 상담취소 + (입원시기 초과로 회복기 불가 케이스는 별도 추적 어려움)
    no_admit_rows = [r for r in rows
                     if (r["admission_status"] or "").strip() == "입원취소"
                     or (r["consult_result"] or "").strip() == "상담취소"]
    no_admit_total = len(no_admit_rows)
    no_admit_with_referral = sum(
        1 for r in no_admit_rows
        if (r["external_referral"] or "").strip() not in ("", "[]"))
    referral_capture_rate = (round(100.0 * no_admit_with_referral / no_admit_total, 1)
                             if no_admit_total else 0.0)

    return {
        "summary": {
            "total": total,
            "planned": planned,
            "plan_rate": round(100.0 * planned / total, 1) if total else 0.0,
            "completed": completed,
            "cancelled": cancelled,
            "pending": pending,
            "conversion_rate": conversion_rate,
            "active_days": active_days,
            "active_day_avg": round(total / active_days, 1) if active_days else 0.0,
            "disuse_screening": disuse_screening,
            "from": date_from,
            "to": date_to,
        },
        # 진행 단계는 정해진 순서 그대로 유지 (도넛/막대 안정 표기). 퇴원완료 포함.
        "by_status": [{"label": s, "count": status_counts[s]}
                      for s in ([undecided_status] + ADMISSION_STATUS_ALL)],
        # 상담 진행 단계 (Tier 1)
        "by_consult_result": [{"label": s, "count": result_counts[s]} for s in CONSULT_RESULTS],
        "by_source_hospital": by_hospital,
        "by_rejection_reason": by_reason,
        "by_referral_conversion": _channel_conversion_table(rows)["details"][:10],
        "by_referral_conversion_by_type": _referral_performance_by_type(),
        "by_disease_group_performance": _disease_group_performance(),
        "by_disease_performance_by_group": _disease_performance_by_group(),
        "by_region_performance": _performance("residence_sigungu"),
        "by_region_performance_by_sido": _region_performance_by_sido(),
        "by_hospital_performance": _performance("source_hospital"),
        "by_counselor_performance": _performance("counselor"),
        "by_admission_lead": [{"label": label, "count": count}
                              for label, count in lead_buckets.items()],
        "by_missing_field": [{"label": label, "count": count,
                              "rate": round(100.0 * count / total, 1) if total else 0.0}
                             for label, count in missing_fields.items()],
        "by_external_referral": by_external_referral,
        "referral_capture": {
            "no_admit_total": no_admit_total,
            "with_referral": no_admit_with_referral,
            "rate": referral_capture_rate,
        },
        "by_referral_type": _sort_desc(_count_json_multi(rows, "referral_source_type")),
        "by_referral_detail": _sort_desc(_count_json_multi(rows, "referral_source_detail")),
        "by_sido": _sort_desc(_count_simple(rows, "residence_sido")),
        "by_sigungu_top": sigungu_top,
        "by_disease": _sort_desc(by_disease_individual)[:15],
        "by_disease_group": [{"label": k, "count": v} for k, v in by_group.items() if v],
        "by_insurance": _sort_desc(insurance_counts),
        "by_channel": _sort_desc(_count_simple(rows, "consult_channel")),
        "by_counselor": _sort_desc(_count_simple(rows, "counselor")),
        "by_age": by_age,
        "by_gender": _sort_desc({label: count for label, count in gender_counts.items() if count}),
        "daily_trend": _daily_trend(rows),
        # 기본 조회가 한 달이어도 장기 흐름을 볼 수 있도록 전체 누적 데이터를 사용한다.
        "monthly_trend": _consult_admission_trend(all_period_rows, "month", limit=18),
        "yearly_trend": _consult_admission_trend(all_period_rows, "year"),
        "by_weekday": [{"label": name, "count": weekday_counts[name]}
                       for name in weekday_names],
        "by_phone_hour": [{"label": name, "count": count}
                          for name, count in phone_hour_counts.items() if count],
        "weekday_hour": [{"weekday": weekday, "hour": hour, "count": count}
                         for (weekday, hour), count in weekday_hour_counts.items()],
    }


def hospital_admission_analysis(date_from=None, date_to=None, hospital=None, q=None):
    """모병원별 실제 입원 환자 집계와 원자료 목록."""
    effective_date = ("COALESCE(NULLIF(c.actual_admission_date, ''), "
                      "NULLIF(c.admission_date, ''), NULLIF(c.planned_admission_date, ''), "
                      "c.consult_date)")
    where = ["c.admission_status IN ('입원완료', '퇴원완료')",
             "c.source_hospital IS NOT NULL", "TRIM(c.source_hospital) != ''"]
    vals = []
    if date_from:
        where.append(f"{effective_date} >= ?"); vals.append(date_from)
    if date_to:
        where.append(f"{effective_date} <= ?"); vals.append(date_to)
    if hospital:
        where.append("c.source_hospital = ?"); vals.append(hospital)
    if q:
        where.append("(p.name LIKE ? OR c.source_hospital LIKE ?)")
        vals.extend([f"%{q}%", f"%{q}%"])
    conn = get_db()
    rows = conn.execute(f"""
        SELECT c.id AS consultation_id, p.id AS patient_id, p.name AS patient_name,
               p.gender, c.patient_age, c.source_hospital, c.consult_date,
               {effective_date} AS admission_date, c.admission_status,
               c.diseases, c.counselor
        FROM consultations c JOIN patients p ON p.id = c.patient_id
        WHERE {' AND '.join(where)}
        ORDER BY admission_date DESC, c.id DESC
    """, vals).fetchall()
    conn.close()

    seen, details, counts = set(), [], {}
    for row in rows:
        item = dict(row)
        key = (item["patient_id"], item["source_hospital"], item["admission_date"])
        if key in seen:
            continue
        seen.add(key)
        try:
            disease_items = json.loads(item.get("diseases") or "[]")
        except (json.JSONDecodeError, TypeError):
            disease_items = []
        item["disease_summary"] = ", ".join(str(x) for x in (disease_items or [])[:3])
        details.append(item)
        counts[item["source_hospital"]] = counts.get(item["source_hospital"], 0) + 1
    hospitals = [{"name": name, "count": count}
                 for name, count in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]
    return {"hospitals": hospitals, "rows": details, "total": len(details),
            "hospital_count": len(hospitals)}


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
        # 회복기 불가 → 같은 재단·외부 시설 연계 (수요 캡처율 KPI)
        "by_external_referral": this_data.get("by_external_referral", []),
        "referral_capture": this_data.get("referral_capture",
                                          {"no_admit_total": 0, "with_referral": 0, "rate": 0}),
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
    """환자 생애주기 단계 변경. 빈값이면 보드에서 제외(미설정).
    단계가 실제로 바뀐 경우 lifecycle_stage_changed_at도 갱신 — 정체 감지·자동 정리용.
    """
    conn = get_db()
    # 현재 단계와 비교 — 같은 단계로 재설정은 changed_at 갱신 안 함
    cur = conn.execute("SELECT lifecycle_stage FROM patients WHERE id = ?",
                       (patient_id,)).fetchone()
    new_stage = stage or None
    cur_stage = (cur["lifecycle_stage"] if cur else None) or None
    if cur_stage == new_stage:
        conn.execute(
            "UPDATE patients SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (patient_id,))
    else:
        conn.execute(
            "UPDATE patients SET lifecycle_stage = ?, "
            "lifecycle_stage_changed_at = CURRENT_TIMESTAMP, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_stage, patient_id),
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


def lifecycle_board(*, q=None, period_days=None, stages=None, disease_group=None,
                    doctor=None, include_archived=False,
                    stale_only=False, new_30d_only=False):
    """생애주기 보드 — 단계 지정 환자와 환자별 최신 상담을 같은 SQL에서 필터링.
    추가 필터(KPI 클릭 연동):
    - stale_only: 정체 90일+ 환자만 (퇴원 제외)
    - new_30d_only: 단계 진입 후 30일 이내 환자만
    """
    conn = get_db()
    where = ["p.lifecycle_stage IS NOT NULL", "p.lifecycle_stage != ''"]
    vals = []
    if q:
        where.append("(p.name LIKE ? OR p.guardian_phone LIKE ?)")
        vals += [f"%{q}%", f"%{q}%"]
    if stages:
        placeholders = ",".join("?" * len(stages))
        where.append(f"p.lifecycle_stage IN ({placeholders})")
        vals += list(stages)
    if stale_only:
        # 정체 = 단계별 기준일 초과 (상담 7일·입원대기 14일). 입원·퇴원은 대상 아님.
        parts = []
        for stg, days in STAGE_STALE_DAYS.items():
            parts.append("(p.lifecycle_stage = ? AND p.lifecycle_stage_changed_at IS NOT NULL "
                         " AND p.lifecycle_stage_changed_at < datetime('now', ?))")
            vals += [stg, f"-{int(days)} days"]
        where.append("(" + " OR ".join(parts) + ")" if parts else "0")
    if new_30d_only:
        # 최근 30일 이내 단계 진입 (changed_at 기준)
        where.append(
            "p.lifecycle_stage_changed_at IS NOT NULL "
            "AND p.lifecycle_stage_changed_at >= datetime('now', '-30 days')"
        )
    if period_days and period_days > 0:
        # 최근 N일 이내 단계 변경 OR 최근 상담 (보호: 변경 시점 없는 레거시는 포함).
        # '입원'은 기간 필터에서 제외한다 — 반년 전에 입원했어도 지금 병상에 있는
        # 환자다. 기간 필터의 목적은 오래된 상담 케이스 정리이지 재원자 숨기기가 아니다.
        where.append(
            "(p.lifecycle_stage = '입원' "
            " OR p.lifecycle_stage_changed_at IS NULL "
            " OR p.lifecycle_stage_changed_at >= datetime('now', ?) "
            " OR EXISTS (SELECT 1 FROM consultations c2 WHERE c2.patient_id = p.id "
            "            AND c2.consult_date >= date('now', ?)))"
        )
        vals += [f"-{int(period_days)} days", f"-{int(period_days)} days"]
    if doctor:
        where.append("lc.attending_doctor = ?")
        vals.append(doctor)
    if disease_group:
        clause, clause_vals = _disease_group_clause("lc.diseases", disease_group)
        where.append(clause)
        vals.extend(clause_vals)
    if not include_archived:
        # 퇴원 30일 자동 제외 / 상담취소·입원취소 7일 자동 제외
        where.append(
            "NOT (p.lifecycle_stage = ? AND p.lifecycle_stage_changed_at IS NOT NULL"
            "     AND p.lifecycle_stage_changed_at < datetime('now', '-30 days'))"
        )
        vals.append("퇴원")
        where.append(
            "NOT ((COALESCE(lc.admission_status, '') = ? "
            "      OR COALESCE(lc.consult_result, '') = ?)"
            "     AND p.lifecycle_stage_changed_at IS NOT NULL"
            "     AND p.lifecycle_stage_changed_at < datetime('now', '-7 days'))"
        )
        vals.extend(["입원취소", "상담취소"])
    where_sql = "WHERE " + " AND ".join(where)
    rows = conn.execute(f"""
        SELECT p.id, p.name, p.gender, p.guardian_name, p.guardian_phone,
               p.lifecycle_stage, p.lifecycle_stage_changed_at,
               p.blacklist, p.blacklist_reason,
               (SELECT COUNT(*) FROM lifecycle_events e WHERE e.patient_id = p.id) AS event_count,
               (SELECT COUNT(*) FROM consultations c WHERE c.patient_id = p.id) AS consult_count,
               lc.consult_date AS last_consult_date,
               lc.id AS last_consult_id,
               lc.diseases AS last_diseases,
               lc.patient_age AS patient_age,
               lc.attending_doctor AS last_doctor,
               -- 트리거 ④ — 최근 상담의 결과/사유 (보드 카드에 '왜 끊겼나' 라벨 표시)
               lc.consult_result AS last_consult_result,
               lc.consult_result_reason AS last_consult_result_reason,
               lc.admission_status AS last_admission_status,
               lc.hold_reason AS last_hold_reason,
               lc.rejection_reason AS last_rejection_reason,
               (julianday('now') - julianday(p.lifecycle_stage_changed_at)) AS stage_days
        FROM patients p
        LEFT JOIN consultations lc ON lc.id = (
            SELECT c0.id FROM consultations c0
            WHERE c0.patient_id = p.id
            ORDER BY c0.consult_date DESC, c0.id DESC
            LIMIT 1
        )
        {where_sql}
        -- 단계 우선순위를 1차 정렬키로 둔다. 보드 카드는 app.py에서 단계별로
        -- 다시 정렬하므로 화면 순서는 그대로이고, LIMIT에 걸려 잘려나가는 쪽이
        -- 항상 '퇴원·기타'가 되게 만드는 안전장치다. (이게 없으면 마지막 상담이
        -- 오래된 장기재원 환자가 보드와 KPI에서 통째로 사라진다.)
        ORDER BY CASE p.lifecycle_stage
                     WHEN '입원' THEN 0 WHEN '입원대기' THEN 1
                     WHEN '상담' THEN 2 ELSE 3 END ASC,
                 (lc.consult_date IS NULL OR lc.consult_date = '') ASC,
                 lc.consult_date DESC, lc.id DESC, p.id DESC
        LIMIT 5000
    """, vals).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["last_diseases"] = json.loads(d["last_diseases"]) if d.get("last_diseases") else []
        except (json.JSONDecodeError, TypeError):
            d["last_diseases"] = []
        # 정체 일수 계산 — 단계 진입 후 N일
        sd = d.get("stage_days")
        d["stage_days_int"] = int(sd) if sd is not None else None
        # 정체 기준은 단계마다 다르다 — 상담 7일·입원대기 14일.
        # 입원은 재원 자체가 수개월이라 일수 정체가 무의미(퇴원 D-day로 관리),
        # 퇴원은 종료 케이스라 둘 다 정체 판정에서 제외한다.
        limit = STAGE_STALE_DAYS.get((d.get("lifecycle_stage") or "").strip())
        d["stale_limit"] = limit
        d["is_stale"] = (limit is not None and d["stage_days_int"] is not None
                         and d["stage_days_int"] >= limit)
        out.append(d)
    return out


def lifecycle_board_kpis(board_rows):
    """보드 KPI — 액션 필요(정체·퇴원임박·응급복귀미기록) + 단계별 평균·카운트.
    이미 lifecycle_board()에서 필터링된 rows를 받아 계산 (DB 재조회 X).
    discharge_dday는 라우트 후처리로 채워진 값을 사용 (입원완료 환자 한정)."""
    active = len(board_rows)
    new_30d = 0
    stale = 0
    discharge_imminent = 0   # 입원완료 + 퇴원 D-3 이내
    stage_days_acc = {}  # stage -> [days, ...]
    stage_counts = {}    # stage -> count (카테고리 카드용)
    phase_counts = {}    # 입원 컬럼 내 수가 구간(회복기/비회복기/단일구간) 카운트
    for r in board_rows:
        sd = r.get("stage_days_int")
        if sd is not None and sd <= 30:
            new_30d += 1
        # 퇴원 임박 — 입원완료 환자의 D-day 3일 이내 (음수=만료 초과 포함)
        dd = r.get("discharge_dday")
        if dd is not None and dd <= 3:
            discharge_imminent += 1
        if r.get("is_stale"):
            stale += 1
        stg = r.get("lifecycle_stage") or "기타"
        stage_counts[stg] = stage_counts.get(stg, 0) + 1
        if stg == "입원" and r.get("care_phase"):
            phase_counts[r["care_phase"]] = phase_counts.get(r["care_phase"], 0) + 1
        if sd is not None:
            stage_days_acc.setdefault(stg, []).append(sd)
    avg_by_stage = {
        s: round(sum(v) / len(v), 1) if v else None
        for s, v in stage_days_acc.items()
    }
    return {
        "active": active, "new_30d": new_30d, "stale": stale,
        "discharge_imminent": discharge_imminent,
        "avg_by_stage": avg_by_stage,
        "stage_counts": stage_counts,
        "phase_counts": phase_counts,
    }


def lifecycle_board_side(board_rows, *, hospital_top=8, recent_event_days=7):
    """생애주기 보드 사이드 패널 데이터 — 모병원 Top + 최근 응급전원·외래치료.
    - 보드에 표시되는 환자들의 모병원(source_hospital) 분포 Top N
    - 최근 N일 내 admission_events('응급전원'·'모병원 외래치료') 리스트
    """
    pids = [r["id"] for r in board_rows if r.get("id")]
    if not pids:
        return {"hospitals": [], "recent_events": []}
    conn = get_db()
    placeholders = ",".join("?" * len(pids))
    # 모병원 — 환자별 source_hospital이 기록된 가장 최근 상담
    hrows = conn.execute(f"""
        SELECT c.source_hospital, p.id AS pid
        FROM patients p
        JOIN consultations c ON c.id = (
            SELECT c2.id FROM consultations c2
            WHERE c2.patient_id = p.id AND c2.source_hospital IS NOT NULL
              AND c2.source_hospital != ''
            ORDER BY c2.consult_date DESC, c2.id DESC LIMIT 1)
        WHERE p.id IN ({placeholders})
    """, pids).fetchall()
    hosp_counts = {}
    for r in hrows:
        h = (r["source_hospital"] or "").strip()
        if h:
            hosp_counts[h] = hosp_counts.get(h, 0) + 1
    hospitals = sorted(hosp_counts.items(), key=lambda x: -x[1])[:hospital_top]

    # 최근 응급전원·외래치료
    erows = conn.execute(f"""
        SELECT ae.id, ae.event_type, ae.event_date, ae.hospital, ae.memo,
               c.patient_id AS pid, p.name AS pname
        FROM admission_events ae
        JOIN consultations c ON c.id = ae.consultation_id
        JOIN patients p ON p.id = c.patient_id
        WHERE ae.event_type IN ('응급전원', '모병원 외래치료')
          AND ae.event_date >= date('now', ?)
          AND c.patient_id IN ({placeholders})
        ORDER BY ae.event_date DESC, ae.id DESC
        LIMIT 10
    """, [f"-{int(recent_event_days)} days"] + pids).fetchall()
    conn.close()
    return {
        "hospitals": [{"name": k, "count": v} for k, v in hospitals],
        "recent_events": [dict(r) for r in erows],
    }


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


def list_sms_log(limit: int = 100, *, date_from: str | None = None,
                 date_to: str | None = None):
    conn = get_db()
    where, vals = [], []
    if date_from:
        where.append("date(created_at, 'localtime') >= date(?)")
        vals.append(date_from)
    if date_to:
        where.append("date(created_at, 'localtime') <= date(?)")
        vals.append(date_to)
    rows = conn.execute(f"""SELECT * FROM sms_log
        {('WHERE ' + ' AND '.join(where)) if where else ''}
        ORDER BY created_at DESC, id DESC LIMIT ?""", [*vals, limit]).fetchall()
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
             if k in ("status", "summary", "body", "follow_up_at",
                      "patient_id", "consultation_id", "channel")}
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


# ─── 입원 중 이벤트 (응급전원·모병원 외래치료 등) ───

AWAY_EVENT_TYPES = ("응급전원", "모병원 외래치료")


def create_admission_event(*, consultation_id, event_type=None, event_date=None,
                           event_time=None,
                           hospital=None, memo=None, created_by=None,
                           stage_before=None) -> int:
    episode_id = sync_admission_episode(consultation_id)
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO admission_events
           (consultation_id, episode_id, event_type, event_date, event_time, hospital, memo, created_by,
            stage_before)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (consultation_id, episode_id, event_type, event_date, event_time, hospital, memo, created_by,
         stage_before),
    )
    eid = cur.lastrowid
    conn.commit()
    conn.close()
    return eid


def list_admission_events(consultation_id):
    """상담(입원) 1건의 입원 중 이벤트 — 발생일 오름차순."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM admission_events WHERE consultation_id = ? "
        "ORDER BY (event_date IS NULL OR event_date = '') ASC, event_date ASC, id ASC",
        (consultation_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_admission_event(event_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM admission_events WHERE id = ?",
                       (event_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_admission_event(event_id):
    conn = get_db()
    conn.execute("DELETE FROM admission_events WHERE id = ?", (event_id,))
    conn.commit()
    conn.close()


def open_away_event(consultation_id):
    """이 입원 건에서 아직 복귀 기록이 없는 외진 1건 (없으면 None)."""
    conn = get_db()
    ph = ",".join("?" * len(AWAY_EVENT_TYPES))
    row = conn.execute(
        f"""SELECT * FROM admission_events
            WHERE consultation_id = ? AND event_type IN ({ph})
              AND returned_at IS NULL
            ORDER BY (event_date IS NULL OR event_date = '') ASC,
                     event_date DESC, id DESC LIMIT 1""",
        [consultation_id, *AWAY_EVENT_TYPES],
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_admission_event_returned(event_id, *, return_date=None, returned_by=None):
    """외진 이벤트에 복귀일을 찍는다. return_date 미지정이면 오늘.

    ※ 오늘은 파이썬(서버 로컬=KST)에서 만든다. SQLite date('now')는 UTC라
      KST 오전 9시 이전에는 하루 전 날짜가 찍혔다 — 외진 복귀를 실제로
      처리하는 시간대가 하필 그 아침이다. event_date도 화면에서 로컬
      날짜로 들어오므로 기준을 로컬로 통일한다.
    """
    conn = get_db()
    conn.execute(
        "UPDATE admission_events SET returned_at = ?, returned_by = ? WHERE id = ?",
        (return_date or date.today().isoformat(), returned_by, event_id),
    )
    conn.commit()
    conn.close()


def away_history(consultation_ids):
    """입원 건별 외진 이력 요약 — 횟수 · 누적 일수 · 마지막 외진일.

    누적 일수는 복귀한 건만 합산하고, 아직 나가 있는 건은 오늘까지로 센다.
    ※ 외진 기간은 재원 기간에서 빼지 않는다 — 회복기/비회복기 수가 기간과
      입원 경과일은 외진을 나가 있는 동안에도 계속 흘러가기 때문. 이력은
      '참고 정보'일 뿐 D-day 계산에 관여하지 않는다.
    """
    if not consultation_ids:
        return {}
    conn = get_db()
    ph_id = ",".join("?" * len(consultation_ids))
    ph_type = ",".join("?" * len(AWAY_EVENT_TYPES))
    rows = conn.execute(f"""
        SELECT consultation_id AS cid, COUNT(*) AS n,
               MAX(event_date) AS last_date,
               SUM(MAX(0, CAST(julianday(COALESCE(returned_at, ?))
                             - julianday(event_date) AS INTEGER))) AS days
        FROM admission_events
        WHERE consultation_id IN ({ph_id}) AND event_type IN ({ph_type})
        GROUP BY consultation_id
    """, [date.today().isoformat()] + list(consultation_ids)
         + list(AWAY_EVENT_TYPES)).fetchall()
    conn.close()
    return {r["cid"]: {"count": r["n"], "days": int(r["days"] or 0),
                       "last_date": r["last_date"]} for r in rows}


def away_now(patient_ids=None):
    """현재 외진 중(미복귀) 환자 목록 — 나간 날짜·기관·경과일 포함.
    보드 배지와 '현재 외진 중' 패널이 같은 데이터를 쓴다.
    patient_ids=None이면 전 환자 대상.
    """
    conn = get_db()
    ph_type = ",".join("?" * len(AWAY_EVENT_TYPES))
    # 첫 값은 SELECT 절의 julianday(?) — 오늘(서버 로컬=KST) 기준 경과일
    vals = [date.today().isoformat(), *AWAY_EVENT_TYPES]
    where_pid = ""
    if patient_ids is not None:
        if not patient_ids:
            conn.close()
            return []
        where_pid = f"AND c.patient_id IN ({','.join('?' * len(patient_ids))})"
        vals += list(patient_ids)
    rows = conn.execute(f"""
        SELECT ae.id, ae.event_type, ae.event_date, ae.hospital, ae.memo,
               ae.stage_before, ae.consultation_id,
               c.patient_id AS pid, c.attending_doctor, c.room_number,
               p.name AS pname, p.guardian_name, p.guardian_phone,
               CAST(julianday(?) - julianday(ae.event_date) AS INTEGER) AS days_out
        FROM admission_events ae
        JOIN consultations c ON c.id = ae.consultation_id
        JOIN patients p ON p.id = c.patient_id
        WHERE ae.event_type IN ({ph_type}) AND ae.returned_at IS NULL {where_pid}
        ORDER BY (ae.event_date IS NULL OR ae.event_date = '') ASC,
                 ae.event_date ASC, ae.id ASC
    """, vals).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["days_out"] = int(d["days_out"]) if d.get("days_out") is not None else None
        # 미복귀 경고 — 당일 왕복인 외래치료는 1일, 응급전원은 3일 기준
        limit = 1 if d["event_type"] == "모병원 외래치료" else 3
        d["overdue"] = d["days_out"] is not None and d["days_out"] >= limit
        d["overdue_limit"] = limit
        out.append(d)
    return out


def patient_timeline(patient_id, viewer_id=None):
    """환자의 모든 접점을 시간순(최신순)으로 병합 — 상담/문자/생애주기/커뮤니케이션.
    viewer_id 지정 시 그 사용자의 '완료된 이 환자 할 일'도 개인적으로 함께 표시(본인만)."""
    conn = get_db()
    items = []
    if viewer_id:
        for r in conn.execute(
            "SELECT id, title, due_date, start_time, done_at FROM todos "
            "WHERE user_id = ? AND patient_id = ? AND done = 1",
            (viewer_id, patient_id)):
            da = (r["done_at"] or "")
            items.append({
                "kind": "todo", "channel": "할 일", "direction": "",
                "date": (da[:10] or r["due_date"] or ""),
                "time": (da[11:16] if len(da) >= 16 else (r["start_time"] or "")),
                "title": "✓ 완료 — " + (r["title"] or "할 일"),
                "detail": "", "ref": None,
                "del_kind": None, "del_id": None, "status": None,
            })
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
                  c.primary_diagnosis, c.secondary_diagnosis, c.diseases,
                  c.disease_detail,
                  p.id AS patient_id, p.name AS patient_name, p.guardian_name,
                  p.guardian_phone, p.blacklist
           FROM consultations c JOIN patients p ON p.id = c.patient_id
           WHERE c.consult_result = '상담요청'
           ORDER BY c.consult_date DESC, c.id DESC""").fetchall()
    conn.close()
    return [_deserialize_consultation(dict(r)) for r in rows]


def open_inbound_count():
    """미처리 인바운드(status='open', 방향 in) 개수 — 전역 배지·알림용 경량 카운트."""
    conn = get_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM communications WHERE status='open' "
        "AND (direction='in' OR direction IS NULL)").fetchone()[0]
    conn.close()
    return n


def inbox_open_communications():
    """미처리 인바운드 — status='open'인 커뮤니케이션.
    환자 미연결이라도 contact 전화번호로 블랙리스트 환자 매칭을 시도해
    임상 안전 경고(⚠)를 사전에 표시할 수 있게 한다."""
    conn = get_db()
    rows = conn.execute(
        """SELECT m.*, p.name AS patient_name,
                  p.blacklist, p.blacklist_reason
           FROM communications m LEFT JOIN patients p ON p.id = m.patient_id
           WHERE m.status = 'open'
           ORDER BY COALESCE(m.occurred_at, m.created_at) DESC""").fetchall()
    result = []
    for r in rows:
        d = dict(r)
        # 환자 미연결 + contact(전화번호)가 있으면 보호자 전화 매칭으로 블랙리스트 사전 조회
        if not d.get("patient_id") and (d.get("contact") or "").strip():
            match = conn.execute(
                """SELECT id, name, blacklist_reason FROM patients
                   WHERE guardian_phone = ? AND blacklist = 1 LIMIT 1""",
                (d["contact"].strip(),),
            ).fetchone()
            if match:
                d["blacklist"] = 1
                d["blacklist_reason"] = match["blacklist_reason"]
                d["matched_patient_id"] = match["id"]
                d["matched_patient_name"] = match["name"]
        result.append(d)
    conn.close()
    return result


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
