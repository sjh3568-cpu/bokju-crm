"""통합 검색 정규화 — 화면 어디서 검색하든 같은 규칙으로 찾는다 (2026-09-30).

'대구굿모닝병원'·'대구 굿모닝병원'·'대구굿모닝'이 서로 다른 글자열이라 검색에서 따로
놀았다(사용자: 어디서 검색하든 같이 나와야 한다). 그래서 SQL `LIKE`를 직접 쓰지 않고
이 모듈의 `search_match`/`search_match_fuzzy`를 쓴다 — `models.get_db()`가 두 함수를
SQLite 사용자 함수로 같은 이름으로 등록하므로 SQL 안에서 `search_match(?, 컬럼, 컬럼…)`,
파이썬 쪽 필터에서는 `search_match(q, 값, 값…)`으로 부른다.

규칙(검색어와 대상 양쪽에 같이 적용):
  1. 띄어쓰기·문장부호(- _ . · 괄호)·대소문자를 무시한다.
  2. 검색어가 `config.HOSPITAL_ALIASES`의 정식명·별칭과 같으면 그 기관의 모든 표기로 찾는다.
  3. '대학교병원'·'대학병원'·'대병원'은 같은 말로 본다 (경북대학교병원 = 경북대병원).
  4. 검색어 끝의 종별 접미사(병원·의료원·요양병원…)를 뗀 몸통(3자 이상)으로도 찾는다.
     '대구굿모닝병원' → '대구굿모닝'도 검색 → '대구굿모닝'·'대구 굿모닝'이 같이 나온다.
  5. `search_match_fuzzy`만: 기관명 몸통(4자 이상)에서 한 글자 오타(바꿈·빠짐·더함)를 허용한다.
     단 종별 접미사 부분은 정확히 맞아야 한다 — '안동병원'을 찾는데 '안동의원'이 나오면 안 된다.
     모병원·협력기관처럼 **기관명 칸**에만 쓴다(환자명·주소·진단 같은 긴 칸에는 `search_match`).

여기는 '검색'만 다룬다. 통계에서 표기를 한 기관으로 합치는 기준은 `models.hospital_group_key`
(접미사를 절대 지우지 않는다 — 안동병원과 안동의료원은 다른 기관)이고 이 모듈과 무관하다.
"""
import functools

from config import HOSPITAL_ALIASES

# 종별 접미사 — 긴 것부터. models._HOSPITAL_KIND_SUFFIXES가 이것을 그대로 쓴다.
HOSPITAL_KIND_SUFFIXES = ("상급종합병원", "종합병원", "대학교병원", "대학병원",
                          "한방병원", "요양병원", "재활병원", "의료원", "병원", "의원")
_UNIV_FORMS = ("대학교병원", "대학병원", "대병원")
_MIN_STEM = 3        # 접미사를 뗀 몸통이 이보다 짧으면 너무 넓어져('대구') 변형에 넣지 않는다
_MIN_FUZZY_NAME = 4  # 오타 허용은 기관명 몸통이 이 길이 이상일 때만 ('안동' vs '안산' 혼동 방지)

_STRIP_TABLE = {ord(ch): None for ch in " \t\n\r-_.·()[]"}


def search_key(value) -> str:
    """검색 비교용 키 — 공백·문장부호 제거 + 소문자. 접미사는 보존한다."""
    if value is None:
        return ""
    return str(value).translate(_STRIP_TABLE).lower()


def _kind_suffix(key: str) -> str:
    for s in HOSPITAL_KIND_SUFFIXES:
        if key.endswith(s) and len(key) > len(s):
            return s
    return ""


@functools.lru_cache(maxsize=1024)
def search_variants(q) -> tuple[str, ...]:
    """검색어 하나를 '같이 찾아야 할 표기' 묶음으로 넓힌다. 첫 항목은 검색어 자체의 키."""
    key = search_key(q)
    if not key:
        return ()
    out = [key]

    def add(v):
        if v and len(v) >= 2 and v not in out:
            out.append(v)

    # 2. 별칭 사전
    for official, aliases in HOSPITAL_ALIASES.items():
        group = {search_key(official), *(search_key(a) for a in aliases)}
        group.discard("")
        if key in group:
            for g in sorted(group):
                add(g)
    # 3. 대학교병원 = 대학병원 = 대병원
    for v in list(out):
        for form in _UNIV_FORMS:
            if v.endswith(form) and len(v) > len(form):
                stem = v[:-len(form)]
                for other in _UNIV_FORMS:
                    add(stem + other)
                break
    # 4. 종별 접미사를 뗀 몸통
    for v in list(out):
        s = _kind_suffix(v)
        if s and len(v) - len(s) >= _MIN_STEM:
            add(v[:-len(s)])
    return tuple(out)


def search_match(q, *fields) -> bool:
    """fields 중 하나라도 검색어(변형 포함)를 부분 문자열로 품고 있으면 True. 검색어가 비면 True."""
    variants = search_variants(q)
    if not variants:
        return True
    for f in fields:
        if f is None:
            continue
        hay = search_key(f)
        if not hay:
            continue
        for v in variants:
            if v in hay:
                return True
    return False


def _edit_position(a: str, b: str):
    """a와 b가 한 글자 차이면 그 위치(b 기준 인덱스), 같으면 -1, 두 글자 이상 다르면 None."""
    la, lb = len(a), len(b)
    if la == lb:
        pos = -1
        for i in range(la):
            if a[i] != b[i]:
                if pos >= 0:
                    return None
                pos = i
        return pos
    if abs(la - lb) != 1:
        return None
    short, long_ = (a, b) if la < lb else (b, a)
    i = 0
    while i < len(short) and short[i] == long_[i]:
        i += 1
    return i if short[i:] == long_[i + 1:] else None


def _near(hay: str, variant: str) -> bool:
    """hay 안에 variant와 한 글자 차이 나는 구간이 있나. 종별 접미사 구간의 차이는 인정하지 않는다."""
    suffix = _kind_suffix(variant)
    name_len = len(variant) - len(suffix)
    if name_len < _MIN_FUZZY_NAME:
        return False
    m, n = len(variant), len(hay)
    for L in (m - 1, m, m + 1):
        if L < 1 or L > n:
            continue
        for i in range(n - L + 1):
            pos = _edit_position(hay[i:i + L], variant)
            if pos is None:
                continue
            if pos < name_len:          # -1(동일) 또는 기관명 몸통 안의 오타
                return True
    return False


def search_match_fuzzy(q, *fields) -> bool:
    """search_match + 기관명 몸통의 한 글자 오타 허용. 모병원·협력기관 이름 칸 전용."""
    if search_match(q, *fields):
        return True
    variants = search_variants(q)
    if not variants:
        return True
    for f in fields:
        if f is None:
            continue
        hay = search_key(f)
        if not hay:
            continue
        for v in variants:
            if _near(hay, v):
                return True
    return False


def _sql_search_match(q, *fields):
    return 1 if search_match(q, *fields) else 0


def _sql_search_match_fuzzy(q, *fields):
    return 1 if search_match_fuzzy(q, *fields) else 0


def register_sql_functions(conn):
    """SQLite 연결에 search_match / search_match_fuzzy 를 가변 인자 함수로 등록한다."""
    conn.create_function("search_match", -1, _sql_search_match, deterministic=True)
    conn.create_function("search_match_fuzzy", -1, _sql_search_match_fuzzy, deterministic=True)
