"""상수·마스터 시드 데이터.

종이 상담일지 양식에 있는 항목과 옵션만 정의. 양식 외 추가 항목은 두지 않음.
"""

# 보험유형 — 사용자 정의 10종
INSURANCE_TYPES = [
    "건강보험",
    "보호1종",
    "보호2종",
    "차상위 1종",
    "차상위 2종",
    "자보",
    "장애",
    "암등록",
    "장기요양",
    "산정특례",
]

# 상담방법 — 양식의 2종
CONSULT_CHANNELS = ["전화상담", "내원상담"]

# 내원 유형 — (구) 상담 헤더 필드. 2026-05-23 UI 제거, 입원 중 이벤트로 일원화.
# 컬럼·과거 입력값은 보존하되 신규 입력은 받지 않는다.
ADMISSION_TYPES = ["일반", "응급이송", "전원"]

# 입원 중 이벤트 — 입원완료 환자가 입원 기간 중 응급전원·모병원 외래치료 등으로
# 외부 의료기관을 다녀온 내역. 상담 상세 페이지에서 기록·관리.
ADMISSION_EVENT_TYPES = ["응급전원", "모병원 외래치료", "복귀", "기타"]

# 주치의 — 6번 요청. 헤더에서 드롭다운으로 선택. 값=표시문자열 그대로 저장.
ATTENDING_DOCTORS = [
    "IM1 정기천 원장",
    "IM2 신현범 과장",
    "NE 변현숙 과장",
    "RM1 이성범 부장",
    "RM2 이석태 과장",
]

# ─── 상담 결과 — 2단계 분리 (7번 요청) ───
# ① 상담 진행 단계 (consult_result) — 상담 자체의 결과. 기본값 '상담완료'.
#    재입원 상담·상담요청·상담보류·상담취소 선택 시 사유 기재 필수.
CONSULT_RESULTS = ["상담완료", "재입원 상담", "상담요청", "상담보류", "상담취소"]
# 사유 입력이 필수인 상담 결과 → 사유 칸 라벨 (폼·API 검증)
CONSULT_RESULT_REASON_LABELS = {
    "재입원 상담": "재입원 사유 / 이전 입원 정보",
    "상담요청": "재연락 시기",
    "상담보류": "보류 사유",
    "상담취소": "취소 사유",
}

# ② 입원 진행 단계 (admission_status) — 상담이 입원으로 이어질 때만 사용.
#    빈값('') = 입원 단계 미진입. 입원보류·입원취소는 사유 기재 필수.
#    '입원예정' 선택 시 헤더 planned_admission_date 지정 권장 — 미지정이면
#    대시보드 액션큐에 "입원예정일 미지정" 알림으로 표시된다.
ADMISSION_STATUSES = [
    "입원대기",  # 입원 결정됐으나 병상 부족으로 순번 대기
    "입원예정",  # 입원 결정 — 실 입원 전 (예정일 지정 권장)
    "입원보류",  # 진행중 — 결과 미확정 (사유 필수)
    "입원취소",  # 입원 단계서 취소 (사유 필수)
    "입원완료",  # 실제 입원
]
# 입원 후 단계 — 폼 선택지는 아니며 상담목록 퇴원 워크플로에서만 설정.
DISCHARGE_COMPLETE = "퇴원완료"   # 실제 퇴원
DISCHARGE_PENDING = "퇴원예정"    # 파생 표시값 (DB에 저장하지 않음)
# admission_status 컬럼에 저장될 수 있는 전체 값 (통계 집계·검증용)
ADMISSION_STATUS_ALL = ADMISSION_STATUSES + [DISCHARGE_COMPLETE]

# 입원취소 사유 라벨 — 단일 풀, 환자측·병원측 사유 모두 포함.
# 사후 분석에서 "병원 거절(의료적 부적합)"·"환자 사정"으로 구분 가능.
REJECTION_REASONS = [
    "비용 부담",
    "거리 / 접근성",
    "타 병원 선택",
    "입원 지연 (대기 못함)",
    "환자/보호자 사정",
    "의료적 부적합 (병원 거절)",
    "사망",
    "기타",
]

# 상담유입경로 — 엑셀 마스터를 3그룹으로 정리 (사용자 결정)
REFERRAL_SOURCE_GROUPS = {
    "온라인": ["카페", "검색(블로그)", "유튜브", "SNS"],
    "소개": ["지인추천", "직원소개", "기관연계"],
    "기타": ["지역민", "현수막"],
}
REFERRAL_TYPES = list(REFERRAL_SOURCE_GROUPS.keys())  # ['온라인', '소개', '기타']

# 보호자 관계 — 양식엔 자유 텍스트지만 자주 쓰는 값을 datalist로 제안
GUARDIAN_RELATION_SUGGESTIONS = ["배우자", "자녀", "형제", "부모", "친척", "본인"]

# 병실 기본 정원 — 병실 뷰의 침상 슬롯 수. 실제 인원이 더 많으면(5인실 등) 그만큼 늘어난다.
ROOM_CAPACITY = 4

# 운영 병동 — 재원 관리 빠른 조회 버튼 (호실 앞자리로 병동 판정)
WARDS = ["2병동", "3병동", "5병동", "6병동", "9병동", "10병동", "12병동", "13병동"]

# 재원 환자 관리 태그 — 소개 경로·특이사항. 정해진 프리셋 + 자유 추가 가능.
MGMT_TAG_PRESETS = ["이사장 소개", "원장 소개", "VIP", "직원 가족", "재입원", "기타 소개"]

# 상담자 — 현 직원 (선택 + 자유 입력 모두 가능)
COUNSELORS = ["박세연", "권창영", "김미화", "신재희"]

# ─── 계정 / 권한 ───
# 역할 3단계: viewer(조회) < staff(상담사) < admin(어드민)
#   · admin  = 전권. 사용자 관리·CSV 내보내기·환자 병합·빠른필터 편집
#   · staff  = 상담 입력·수정·조회 + 재원·문자 + 통계·월간보고서 (계정 관리는 불가)
#   · viewer = 읽기 전용 공통 계정. 병동 등 타 부서용. 모든 등록·수정·발송 차단
# 역할은 이제 '권한 프리셋'의 이름일 뿐 — 실제 접근은 users.permissions(메뉴별 레벨)로 판정.
ROLE_LABELS = {"admin": "어드민", "staff": "상담사", "viewer": "조회"}

# ─── 이력 관리(감사 로그) ───
# audit_log.action → 화면 표시 라벨. models.log_audit으로 새 action을 남기면 여기에도 추가한다.
# (라벨이 없으면 이력 관리 화면에 action 원문이 그대로 표시된다.)
AUDIT_ACTION_LABELS = {
    "login": "로그인",
    "login_fail": "로그인 실패",
    "logout": "로그아웃",
    "view_patient": "환자 조회",
    "view_consult": "상담 조회",
    "create_consult": "상담 등록",
    "update_consult": "상담 수정",
    "delete_consult": "상담 삭제",
    "confirm_admission": "입원 확정",
    "update_status": "입원 상태 변경",
    "update_stage": "생애주기 단계 변경",
    "update_room": "병실 변경",
    "update_discharge": "퇴원 예정일 변경",
    "add_admission_event": "재원 이벤트 추가",
    "return_admission_event": "외진 복귀 처리",
    "add_lifecycle_event": "생애주기 이벤트 추가",
    "delete_lifecycle_event": "생애주기 이벤트 삭제",
    "add_communication": "소통 기록 추가",
    "inbound_webhook": "인바운드 문의 수신(웹훅)",
    "close_communication": "소통 기록 종료",
    "update_blacklist": "블랙리스트 변경",
    "merge_patient": "환자 병합",
    "send_sms": "문자 발송",
    "export_csv": "CSV 내보내기",
    "report_insight": "월간보고서 AI 분석",
    "stats_insight": "통계 AI 분석",
    "quick_filters_update": "빠른 필터 수정",
    "create_user": "계정 생성",
    "update_user": "계정 수정",
    "delete_user": "계정 삭제",
    "reset_password": "비밀번호 초기화",
    "toggle_user_active": "계정 활성/비활성",
    "create_notice": "공지 등록",
    "update_notice": "공지 상태 변경",
    "ack_notice": "공지 확인",
}

# 이력 관리 화면의 '분류' 필터 — 키 → (표시 라벨, 해당 action 목록).
# 어느 분류에도 없는 action은 '기타'로 묶여 조회된다(AUDIT_CATEGORY_OTHER).
AUDIT_CATEGORIES = {
    "auth": ("로그인·인증", ["login", "login_fail", "logout"]),
    "create": ("등록", [
        "create_consult", "confirm_admission", "add_admission_event",
        "add_lifecycle_event", "add_communication", "inbound_webhook",
    ]),
    "update": ("수정", [
        "update_consult", "update_status", "update_stage", "update_room",
        "update_discharge", "update_blacklist", "return_admission_event",
        "close_communication", "merge_patient", "quick_filters_update",
    ]),
    "delete": ("삭제", ["delete_consult", "delete_lifecycle_event"]),
    "view": ("조회", ["view_patient", "view_consult"]),
    "outbound": ("반출·발송", ["send_sms", "export_csv", "report_insight", "stats_insight"]),
    "account": ("계정 관리", [
        "create_user", "update_user", "delete_user", "reset_password",
        "toggle_user_active",
    ]),
    "notice": ("공지사항", ["create_notice", "update_notice", "ack_notice"]),
}
AUDIT_CATEGORY_OTHER = "other"

# 개인정보 열람·변경 이력 보관 기간(일). 화면 안내용 — 자동 삭제는 하지 않는다.
AUDIT_RETENTION_DAYS = 365

# 이력 관리 화면에서 '중요 변경'으로 강조할 action (조회·로그인 소음과 구분).
AUDIT_CRITICAL_ACTIONS = {
    "delete_consult", "delete_lifecycle_event", "merge_patient",
    "create_user", "update_user", "delete_user", "reset_password",
    "toggle_user_active", "export_csv", "update_blacklist", "login_fail",
}

# 메뉴별 세부 권한 — 단계형 레벨 (상위가 하위 포함). 계정별로 설정.
PERM_HIDDEN, PERM_VIEW, PERM_EDIT, PERM_CREATE = 0, 1, 2, 3
PERM_LEVELS = [PERM_HIDDEN, PERM_VIEW, PERM_EDIT, PERM_CREATE]
PERM_LEVEL_LABELS = {
    PERM_HIDDEN: "미현시", PERM_VIEW: "조회", PERM_EDIT: "수정", PERM_CREATE: "등록",
}

# 권한을 매길 메뉴 — (key, label, 지원 최대 레벨). 조회 전용 메뉴는 최대 '조회'.
MENUS = [
    ("dashboard", "대시보드",  PERM_VIEW),
    ("consult",   "상담",       PERM_CREATE),
    ("ward",      "재원 관리",  PERM_EDIT),
    ("sms",       "문자",       PERM_CREATE),
    ("stats",     "통계",       PERM_VIEW),
    ("report",    "월간보고서", PERM_VIEW),
    ("users",     "사용자 관리", PERM_EDIT),
]
MENU_KEYS = [m[0] for m in MENUS]
MENU_MAX_LEVEL = {k: mx for k, _, mx in MENUS}

# 역할 프리셋 — 계정 생성/역할 변경 시 권한 매트릭스 기본값.
ROLE_PRESETS = {
    "admin":  {k: MENU_MAX_LEVEL[k] for k in MENU_KEYS},  # 모든 메뉴 최대 권한
    "staff":  {
        "dashboard": PERM_VIEW, "consult": PERM_CREATE, "ward": PERM_EDIT,
        "sms": PERM_CREATE, "stats": PERM_VIEW, "report": PERM_VIEW, "users": PERM_HIDDEN,
    },
    "viewer": {
        "dashboard": PERM_VIEW, "consult": PERM_VIEW, "ward": PERM_VIEW,
        "sms": PERM_HIDDEN, "stats": PERM_VIEW, "report": PERM_VIEW, "users": PERM_HIDDEN,
    },
}


def role_preset(role: str) -> dict:
    """역할 프리셋 권한 매트릭스 (알 수 없는 역할이면 viewer 기준)."""
    return dict(ROLE_PRESETS.get(role, ROLE_PRESETS["viewer"]))


# 최초 부팅 시 없으면 자동 생성되는 계정 — (username, display_name, role).
# 초기 비밀번호는 .env의 APP_PASSWORD. 이후 어드민이 '사용자 관리'에서 개별 변경.
# (이미 존재하는 계정은 건드리지 않아 어드민이 설정한 비번이 보존된다.)
SEED_USERS = [
    ("어드민", "어드민", "admin"),
    ("박세연", "박세연", "staff"),
    ("권창영", "권창영", "staff"),
    ("김미화", "김미화", "staff"),
    ("신재희", "신재희", "staff"),
    ("조회", "조회 (공통)", "viewer"),
]

# ─── 환자 생애주기 (3번 요청) ───
# 단계판(보드)의 컬럼 — 상담부터 퇴원까지 환자가 거치는 큰 단계.
# 2026-08-24 개편: 7 → 4단계. 컬럼은 '상담사가 무슨 액션을 하는가' 단위로만 둔다.
#   · 회복기/비회복기 = 입원 중 수가 구간 → 단계가 아니라 발병일+진단군으로 자동 판정,
#     '입원' 컬럼 안의 서브레인 + D-day로 표시 (CARE_PHASES)
#   · 응급치료(외진)   = 병상을 유지한 일시 이탈 → 단계가 아니라 미복귀 플래그
#     (admission_events.returned_at IS NULL) + 카드 배지로 표시
LIFECYCLE_STAGES = ["상담", "입원대기", "입원", "퇴원"]
# 폐지된 단계 → 신규 단계 매핑 (기존 데이터 마이그레이션·하위호환용)
LEGACY_STAGE_MAP = {"응급치료": "입원", "회복기": "입원", "비회복기": "입원"}
# '입원' 컬럼 내부 레인 — 자동 판정값(_recovery_status)에 대응. 표시 순서 = 리스트 순서.
CARE_PHASES = ["회복기", "비회복기", "단일구간", "미판정"]
# 단계별 정체(무응답) 경고 기준일. 입원·퇴원은 일수가 아니라 퇴원 D-day로 관리 → 제외.
STAGE_STALE_DAYS = {"상담": 7, "입원대기": 14}
# 생애주기 이벤트 유형 — 타임라인에 수동 기록. 상담·입원·퇴원은 자동 파생도 됨.
LIFECYCLE_EVENT_TYPES = [
    "상담", "입원", "응급치료", "복귀", "회복기 전환", "비회복기 전환",
    "보호자 요구사항", "환자 요구사항", "퇴원", "기타",
]

# ─── 옴니채널 — 커뮤니케이션 통합 ───
# 모든 접점(전화·문자·카카오·웹문의·팩스)을 한 환자 타임라인으로 모으는 통합 로그.
COMM_CHANNELS = ["전화", "문자", "카카오", "웹문의", "팩스", "부재중", "기타"]
# 인바운드 채널 — '받은 메시지' 기록 폼의 기본 목록 (COMM_CHANNELS 부분집합).
COMM_INBOUND_CHANNELS = ["문자", "카카오", "웹문의", "팩스", "부재중", "기타"]

# ─── 문자 발송 (5번 요청) ───
# 환자군별 템플릿 분류 — 병명 4그룹 + 공통.
SMS_TEMPLATE_GROUPS = ["공통", "중추신경계", "근골격계", "비사용증후군", "기저질환"]
# 템플릿 본문 치환 토큰 — 발송 시 실제 값으로 자동 치환.
SMS_PLACEHOLDERS = {
    "{환자명}": "환자 이름",
    "{보호자명}": "보호자 이름",
    "{병원명}": "복주회복병원",
    "{입원예정일}": "입원 예정일",
    "{주치의}": "주치의",
}

# ─── 환자 현재 상태 ───
CURRENT_LOCATION_TYPES = ["집", "입원중", "입소중"]

# 의식 — 양식대로 3그룹 분리
CONSCIOUSNESS_MAIN_OPTIONS = ["정상", "반혼수", "혼수"]
CONVERSATION_LEVEL_OPTIONS = ["가능", "조금", "불가능"]
HEARING_OPTIONS = ["잘 안들림", "알아들으심", "보청기"]

# 활동 — 양식 구조에 맞춰 단순 라벨로 분리
ACTIVITY_ACTIVE_OPTIONS = ["스스로", "도움", "지팡이", "워커"]  # 능동 (화장실 가능)
ACTIVITY_DIAPER_OPTIONS = ["유", "무"]                       # 기저귀
ACTIVITY_WHEELCHAIR_OPTIONS = ["스스로", "도움"]              # 휠체어
ACTIVITY_OTHERS_OPTIONS = ["와상", "에어매트리스 안내"]

CAREGIVER_OPTIONS = ["간병", "비간병"]
BED_OPTIONS = ["침대", "바닥생활"]

# ─── 병명 체크리스트 (4그룹, 인라인 수기·서브그룹 포함) ───
# kind:
#   "checkbox"             - 단순 체크박스
#   "checkbox+radio"       - 체크박스 + 인라인 라디오 (당뇨 인슐린 유/무)
#   "checkbox+text"        - 체크박스 + 인라인 텍스트 (파킨슨 상세 등)
#   "group"                - 부모 라벨 + 서브 체크박스 (치매(경도/중도/고도) 등)
DISEASES_LAYOUT = {
    "기저질환": [
        {"kind": "checkbox+radio", "value": "당뇨", "addon_label": "인슐린",
         "addon_field": "insulin_use", "addon_options": ["유", "무"]},
        {"kind": "checkbox", "value": "고혈압"},
        {"kind": "checkbox+text", "value": "파킨슨", "stretch": True,
         "addon_field": "parkinson_detail", "placeholder": "단계/약물"},
        {"kind": "checkbox+text", "value": "희귀성난치질환", "stretch": True,
         "addon_field": "rare_disease_name", "placeholder": "질환명"},
        {"kind": "rowbreak"},
        {"kind": "group", "label": "치매", "items": [
            {"value": "치매-경도", "label": "경도"},
            {"value": "치매-중도", "label": "중도"},
            {"value": "치매-고도", "label": "고도"},
        ]},
        {"kind": "checkbox", "value": "인지기능저하"},
        {"kind": "group", "label": "이상행동", "items": [
            {"value": "이상행동-소리지름", "label": "소리지름"},
            {"value": "이상행동-폭력적", "label": "폭력적"},
        ]},
        {"kind": "checkbox", "value": "탈출 위험(escape)"},
        {"kind": "checkbox", "value": "암"},
    ],
    "중추신경계": [
        {"kind": "checkbox+text", "value": "뇌출혈", "stretch": True,
         "addon_field": "hemorrhage_surgery", "placeholder": "수술 정보"},
        {"kind": "checkbox+text", "value": "뇌경색", "stretch": True,
         "addon_field": "infarction_site", "placeholder": "부위/혈관"},
        {"kind": "checkbox+text", "value": "척수손상", "stretch": True,
         "addon_field": "spinal_injury_level", "placeholder": "부위/레벨"},
        {"kind": "checkbox", "value": "뇌성마비"},
        {"kind": "rowbreak"},
        {"kind": "group", "label": "마비", "stretch": True,
         "addon_field": "paralysis_detail", "addon_placeholder": "상세",
         "items": [
            {"value": "마비-사지마비", "label": "사지마비"},
            {"value": "마비-편마비 좌", "label": "편마비 좌"},
            {"value": "마비-편마비 우", "label": "편마비 우"},
            {"value": "마비-하지마비", "label": "하지마비"},
        ]},
    ],
    "근골격계": [
        {"kind": "checkbox+group", "value": "단일부위", "items": [
            {"value": "고관절 골절", "label": "고관절 골절"},
            {"value": "대퇴부 골절", "label": "대퇴부 골절"},
            {"value": "골반 골절", "label": "골반 골절"},
        ]},
        {"kind": "rowbreak"},
        {"kind": "checkbox+group", "value": "다발부위", "items": [
            {"value": "다발부위-고관절 골절", "label": "고관절 골절"},
            {"value": "다발부위-대퇴부 골절", "label": "대퇴부 골절"},
            {"value": "다발부위-골반 골절", "label": "골반 골절"},
        ]},
        {"kind": "rowbreak"},
        # 보조 항목 — 둥근 체크 모양으로 구분 (입원 기간 60일 가산 요인)
        {"kind": "checkbox", "value": "골유합 지연", "shape": "round"},
        {"kind": "checkbox", "value": "내고정술", "shape": "round"},
        {"kind": "checkbox", "value": "전치환술", "shape": "round"},
        {"kind": "rowbreak"},
        {"kind": "checkbox", "value": "하지 부위 절단"},
        {"kind": "checkbox", "value": "양측슬관절치환술",
         "help_title": "복잡기준",
         "help_items": [
             "아래 기준 중 하나라도 해당하면 복잡수술로 볼 수 있음",
             {"text": "1. 전문의 협진으로 확인된 중증 동반질환", "subitems": [
                 "만성 신부전",
                 "장기이식을 받았거나 장기이식이 필요한 경우",
                 "심혈관 스텐트 삽입으로 항혈전제를 복용 중인 경우",
                 "고도 심근경색 또는 협심증(Goldman cardiac risk III 이상)",
                 "조절되지 않는 당뇨(HbA1C > 7.0)",
                 "간경화",
                 "혈액암",
                 "혈우병 또는 혈액응고 이상",
                 "고도 폐쇄성 폐질환",
                 "정맥혈전색전증 치료 과거력",
                 "뇌경색 등으로 aspirin보다 상위 항혈전제를 복용 중인 경우",
             ]},
             "2. 치료 중인 류마티스 질환자로 DAS 28이 5.1 초과",
             "5. 병적 골절 동반: 원발성 골암, 전이성 골암, 골다공증 등이 동반된 골절",
             "6. 감염성 후유증 또는 삽입물 주위 감염 후 시행하는 인공관절치환술",
             "7. 장축 1 inch 이상의 골결손이 동반된 인공관절치환술",
             "10. 관절구축이 20도 이상인 경우",
             "11. 인공관절재치환술을 다시 시행하는 경우",
             "입원시기: 동일 입원기간 내 첫 번째 수술일로부터 30일 이내",
             "진료기록부 등 객관적 자료 첨부 필요",
         ]},
    ],
    "비사용증후군": [
        # 한 줄에 2개씩 — stretch 항목이라 절반 너비로 나뉘고 상세칸도 그만큼 줄어듦.
        # 파킨슨(신규) — 최근 진단. 기저질환의 '파킨슨'(올드, 60일 초과)과 구분되는 별도 값.
        {"kind": "checkbox+text", "value": "파킨슨(신규)", "label": "파킨슨", "stretch": True,
         "addon_field": "parkinson_new_detail", "placeholder": "상세"},
        {"kind": "checkbox+text", "value": "길랑바레증후군", "stretch": True,
         "addon_field": "gbs_detail", "placeholder": "상세"},
        {"kind": "rowbreak"},
        {"kind": "checkbox+text", "value": "호흡질환", "stretch": True,
         "addon_field": "lung_detail", "placeholder": "상세"},
        {"kind": "checkbox+text", "value": "심장질환", "stretch": True,
         "addon_field": "heart_detail", "placeholder": "상세"},
        {"kind": "rowbreak"},
        {"kind": "checkbox+text", "value": "신생물", "label": "신생물(암)", "stretch": True,
         "addon_field": "neoplasm_detail", "placeholder": "상세"},
    ],
}


def _flatten_layout(layout):
    result = []
    for items in layout.values():
        for it in items:
            if it["kind"] == "group":
                result.extend(s["value"] for s in it["items"])
            elif it["kind"] == "checkbox+group":
                result.append(it["value"])
                result.extend(s["value"] for s in it["items"])
            elif it["kind"] == "rowbreak":
                continue
            else:
                result.append(it["value"])
    return result


DISEASES_CHECKLIST = _flatten_layout(DISEASES_LAYOUT)
DISEASES_GROUPS = {  # 호환용 (다른 코드가 이름만 참조할 수 있음)
    name: [
        it.get("value") or it.get("label", "")
        for it in items
        if it["kind"] != "rowbreak"
    ]
    for name, items in DISEASES_LAYOUT.items()
}

# ─── 기타 (ARRANGE) — DNR(agree/consult) 그룹 + hopeless 확인 ───
OTHERS_LAYOUT = [
    {"kind": "group", "label": "DNR", "items": [
        {"value": "DNR(agree)", "label": "agree"},
        {"value": "DNR(consult)", "label": "consult"},
    ]},
    {"kind": "checkbox", "value": "hopeless 확인"},
]
OTHERS_CHECKLIST = []
for _it in OTHERS_LAYOUT:
    if _it["kind"] == "group":
        OTHERS_CHECKLIST.extend(s["value"] for s in _it["items"])
    else:
        OTHERS_CHECKLIST.append(_it["value"])

# ─── 식사 ───
DIET_TYPES = [
    "밥",
    "죽",
    "미음-도움",
    "미음-스스로",
    "미음-틀니",
    "미음-다지기",
    "비강영양(L-tube)",
    "위루술(PEG)",
]

# 식사종류 레이아웃 — 미음은 네모체크 부모 + 둥근체크 4종(도움/스스로/틀니/다지기)
DIET_LAYOUT = [
    {"kind": "checkbox", "value": "밥"},
    {"kind": "checkbox", "value": "죽"},
    {"kind": "checkbox+group", "value": "미음", "items": [
        {"value": "미음-도움", "label": "도움"},
        {"value": "미음-스스로", "label": "스스로"},
        {"value": "미음-틀니", "label": "틀니"},
        {"value": "미음-다지기", "label": "다지기"},
    ]},
    {"kind": "checkbox", "value": "비강영양(L-tube)"},
    {"kind": "checkbox", "value": "위루술(PEG)"},
]

# ─── 상처/소독 ───
WOUND_CARE_OPTIONS = [
    "욕창",
    "수술절상",
    "기관절개",
    "Foley cath(유치도뇨)",
    "당뇨발",
    "화상",
    "단순상처",
    "요루(ureterostomy)",
    "장루(colostomy)",
]

# 상처소독 항목별 인라인 수기 메모 — (옵션 → DB 컬럼). 폼은 이 순서로 렌더.
WOUND_CARE_NOTE_FIELDS = {
    "욕창": "wound_site",                    # 기존 컬럼 재사용
    "수술절상": "wound_op_note",
    "기관절개": "tracheostomy_detail",       # 기존 컬럼 재사용
    "Foley cath(유치도뇨)": "wound_foley_note",
    "당뇨발": "wound_dmfoot_note",
    "화상": "wound_burn_note",
    "단순상처": "wound_simple_note",
    "요루(ureterostomy)": "wound_urostomy_note",
    "장루(colostomy)": "wound_colostomy_note",
}

# ─── 특수처치 ───
SPECIAL_CARE_OPTIONS = [
    "중심정맥영양",
    "인공호흡기",
    "산소요법",
    "흡인",
    "수혈",
    "PICC",
    "수액요법",
    "네블라이저",
    "기관내삽관",
    "MRSA",
    "VRE",
    "CRE",
]

# 특수처치 항목별 인라인 수기 메모 — (옵션 → DB 컬럼). 폼은 이 순서로 렌더.
SPECIAL_CARE_NOTE_FIELDS = {
    "중심정맥영양": "special_tpn_note",
    "인공호흡기": "special_vent_note",
    "산소요법": "oxygen_lpm",                # 기존 컬럼 재사용
    "흡인": "special_suction_note",
    "수혈": "special_transfusion_note",
    "PICC": "special_picc_note",
    "수액요법": "special_fluid_note",
    "네블라이저": "special_nebulizer_note",
    "기관내삽관": "special_intubation_note",
    "MRSA": "special_mrsa_note",
    "VRE": "special_vre_note",
    "CRE": "special_cre_note",
}

# ─── 치료 ───
THERAPY_OPTIONS = [
    "언어치료",
    "로봇치료",
    "전문재활-QD",
    "전문재활-BID",
    "도수치료(비보험)",
]

# ─── 입원시 확인 서류 ───
ADMISSION_DOCS = [
    "의사소견서 및 처방전, MRI/CT CD, 신분증",
    "의료급여의뢰서(보호환자)",
    "연하검사 결과지(VFSS)",
    "지불보증서(자보환자)",
    "심장초음파 결과지",
    "다제내성균 결과지",
]

# ─── 이송 수단 ───
TRANSPORT_OPTIONS = ["보호자차량(self)", "앰뷸런스 이용", "사설앰뷸 이용"]

# ─── 진료비 안내 ───
COST_GUIDANCE_OPTIONS = [
    "회복 본인부담상한제 (843만원/년)",
    "요양병원 본인부담상한제 (1096만원/년)",
]

# ─── 안내사항 ───
INFO_PROVIDED_OPTIONS = [
    "입원 필요서류 안내",
    "뇌척수(일반) 입원 체크사항 안내",
    "중증환자 입원 체크사항 안내",
    "균환자 입원 체크사항 안내",
]

# ─── 마스터 시드 ───
# 자동완성용 시드. 양식에 자동완성 input은 없지만 검색·통계 단계에서 사용.
DIAGNOSIS_SEED = [
    ("뇌경색", "I63", "뇌혈관"),
    ("뇌출혈", "I61", "뇌혈관"),
    ("지주막하출혈", "I60", "뇌혈관"),
    ("외상성 뇌손상", "S06", "외상"),
    ("척수손상", "T09", "외상"),
    ("고관절 골절", "S72", "정형(고관절)"),
    ("대퇴골 골절", "S72", "정형(고관절)"),
    ("슬관절 치환술", "Z96", "정형(슬관절)"),
    ("척추 압박골절", "S22", "정형(척추)"),
    ("요추 추간판탈출증", "M51", "정형(척추)"),
    ("파킨슨병", "G20", "퇴행성"),
    ("치매", "F03", "퇴행성"),
    ("폐렴 후 회복", "J18", "기타"),
]

SOURCE_HOSPITAL_SEED = [
    ("강릉아산병원", "강원"),
    ("경북대학교병원", "대구·경북"),
    ("칠곡경북대학교병원", "대구·경북"),
    ("동산병원", "대구·경북"),
    ("계명대학교 동산병원", "대구·경북"),
    ("영남대학교병원", "대구·경북"),
    ("대구가톨릭대학교병원", "대구·경북"),
    ("안동병원", "대구·경북"),
    ("포항성모병원", "대구·경북"),
    ("포항세명기독병원", "대구·경북"),
    ("구미차병원", "대구·경북"),
    ("순천향대학교 구미병원", "대구·경북"),
]

# 병원명 별칭/통칭 → 공식명 자동완성용.
# 예: "아산강릉"처럼 순서가 뒤섞인 검색어도 "강릉아산병원"을 제안한다.
HOSPITAL_ALIASES = {
    "강릉아산병원": [
        "강릉아산",
        "아산강릉",
        "아산 강릉",
        "강릉 아산",
        "강릉아산병원",
    ],
}

# ─── 시/도 → 시/군/구 ───
_SIDO_SIGUNGU = {
    "서울특별시": ["강남구", "강동구", "강북구", "강서구", "관악구", "광진구", "구로구", "금천구", "노원구", "도봉구", "동대문구", "동작구", "마포구", "서대문구", "서초구", "성동구", "성북구", "송파구", "양천구", "영등포구", "용산구", "은평구", "종로구", "중구", "중랑구"],
    "부산광역시": ["강서구", "금정구", "기장군", "남구", "동구", "동래구", "부산진구", "북구", "사상구", "사하구", "서구", "수영구", "연제구", "영도구", "중구", "해운대구"],
    "대구광역시": ["남구", "달서구", "달성군", "동구", "북구", "서구", "수성구", "중구", "군위군"],
    "인천광역시": ["강화군", "계양구", "남동구", "동구", "미추홀구", "부평구", "서구", "연수구", "옹진군", "중구"],
    "광주광역시": ["광산구", "남구", "동구", "북구", "서구"],
    "대전광역시": ["대덕구", "동구", "서구", "유성구", "중구"],
    "울산광역시": ["남구", "동구", "북구", "울주군", "중구"],
    "세종특별자치시": ["세종시"],
    "경기도": ["가평군", "고양시", "과천시", "광명시", "광주시", "구리시", "군포시", "김포시", "남양주시", "동두천시", "부천시", "성남시", "수원시", "시흥시", "안산시", "안성시", "안양시", "양주시", "양평군", "여주시", "연천군", "오산시", "용인시", "의왕시", "의정부시", "이천시", "파주시", "평택시", "포천시", "하남시", "화성시"],
    "강원특별자치도": ["강릉시", "고성군", "동해시", "삼척시", "속초시", "양구군", "양양군", "영월군", "원주시", "인제군", "정선군", "철원군", "춘천시", "태백시", "평창군", "홍천군", "화천군", "횡성군"],
    "충청북도": ["괴산군", "단양군", "보은군", "영동군", "옥천군", "음성군", "제천시", "증평군", "진천군", "청주시", "충주시"],
    "충청남도": ["계룡시", "공주시", "금산군", "논산시", "당진시", "보령시", "부여군", "서산시", "서천군", "아산시", "예산군", "천안시", "청양군", "태안군", "홍성군"],
    "전북특별자치도": ["고창군", "군산시", "김제시", "남원시", "무주군", "부안군", "순창군", "완주군", "익산시", "임실군", "장수군", "전주시", "정읍시", "진안군"],
    "전라남도": ["강진군", "고흥군", "곡성군", "광양시", "구례군", "나주시", "담양군", "목포시", "무안군", "보성군", "순천시", "신안군", "여수시", "영광군", "영암군", "완도군", "장성군", "장흥군", "진도군", "함평군", "해남군", "화순군"],
    "경상북도": ["경산시", "경주시", "고령군", "구미시", "김천시", "문경시", "봉화군", "상주시", "성주군", "안동시", "영덕군", "영양군", "영주시", "영천시", "예천군", "울릉군", "울진군", "의성군", "청도군", "청송군", "칠곡군", "포항시"],
    "경상남도": ["거제시", "거창군", "고성군", "김해시", "남해군", "밀양시", "사천시", "산청군", "양산시", "의령군", "진주시", "창녕군", "창원시", "통영시", "하동군", "함안군", "함양군", "합천군"],
    "제주특별자치도": ["서귀포시", "제주시"],
}

# 시/군/구 → [가능한 시/도 목록]. 단일이면 자동 채움, 다수면 사용자가 선택.
SIGUNGU_INDEX = {}
for _sido, _sigungus in _SIDO_SIGUNGU.items():
    for _sg in _sigungus:
        SIGUNGU_INDEX.setdefault(_sg, []).append(_sido)

SIDO_LIST = list(_SIDO_SIGUNGU.keys())
SIGUNGU_LIST = sorted({s for sigungus in _SIDO_SIGUNGU.values() for s in sigungus})
