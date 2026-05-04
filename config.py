"""상수·마스터 시드 데이터.

종이 상담일지 양식에 있는 항목과 옵션만 정의. 양식 외 추가 항목은 두지 않음.
"""

# 보험유형 — 사용자 정의 10종
INSURANCE_TYPES = [
    "보험",
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

# 상담 결과(입원 진행 단계) — 5분류. 기본값=상담완료. 통계 KPI/전환율 산출용.
# 사용자 요청으로 결정 워크플로 추가 (CLAUDE.md 절대금지에서 해제됨).
ADMISSION_STATUSES = [
    "상담완료",  # 기본 — 결과 미확정/단순 상담
    "입원예정",  # 입원 방향, 날짜 미확정
    "입원확정",  # 입원일 확정
    "입원완료",  # 실제 입원
    "입원취소",  # 어느 단계서든 취소
]
# 보류(진행중·결과 미확정)로 묶이는 상태 — KPI 전환율 분모 계산에 사용
ADMISSION_STATUS_PENDING = ("상담완료", "입원예정", "입원확정")

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

# 상담자 — 현 직원 (선택 + 자유 입력 모두 가능)
COUNSELORS = ["박세연", "권창영", "김미화", "신재희"]

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
        {"kind": "checkbox", "value": "대퇴부"},
        {"kind": "checkbox", "value": "고관절"},
        {"kind": "checkbox+group", "value": "골반 골절", "items": [
            {"value": "골반-단일 부위", "label": "단일 부위"},
            {"value": "골반-다발 부위", "label": "다발 부위"},
        ]},
        {"kind": "rowbreak"},
        {"kind": "checkbox", "value": "하지 부위 절단"},
    ],
    "비사용증후군": [
        {"kind": "checkbox+text", "value": "폐질환", "stretch": True,
         "addon_field": "lung_detail", "placeholder": "상세"},
        {"kind": "rowbreak"},
        {"kind": "checkbox+text", "value": "심장질환", "stretch": True,
         "addon_field": "heart_detail", "placeholder": "상세"},
        {"kind": "rowbreak"},
        {"kind": "checkbox+text", "value": "신생물", "stretch": True,
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
