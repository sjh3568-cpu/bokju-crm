"""홈페이지(bokjurh.co.kr) 상담게시판 ↔ CRM 인박스 연동 (옴니채널).

병원 홈페이지는 제작사가 만든 자체 PHP 사이트(카페24 호스팅)라 API가 없다.
대신 두 경로를 쓴다.
  · 새 글 감지: 공개 목록(/sub/07_community/guide_01)은 로그인 없이 보인다.
    번호·제목·상담상태(접수/답변완료)·가린 이름·등록일까지 나오므로 이것만으로
    '새 글 알림'은 항상 살아 있다(관리자 로그인이 깨져도 알림은 된다).
  · 본문·연락처 읽기, 답변 달기: 관리자(/adm) 로그인 세션으로 처리.
    본문은 비밀글이라 공개 화면에선 "접근권한이 없습니다"가 뜬다.

동작:
  · HOMEPAGE_ADMIN_ID/PW 가 있어야 본문·답변이 된다. 없으면 감지·알림만.
  · HOMEPAGE_BOARD_POLL_SECONDS(기본 180초)마다 공개 목록 1페이지 확인
  · 새 idx → 관리자 화면에서 본문·연락처 → communications(웹문의/in) 등록
    → 기존 인바운드 알림(/api/inbound/alerts) 체계가 상담사 브라우저에 띄운다.
  · 홈페이지 관리자에서 누가 직접 답변해 '답변완료'가 되면 CRM 인박스도 자동 완료.
  · CRM 인박스 '답변' → reply_to_post()가 관리자 폼(counselUP.php)에 등록.
주의: 실패가 상담 업무를 막지 않도록 예외는 로그만 남기고 다음 주기 재시도.
      관리자 자격증명은 .env에만 두고 로그·감사에 남기지 않는다.
"""
import html as _html
import logging
import os
import re
import threading
import time
from datetime import date

import requests

import models

logger = logging.getLogger(__name__)

BASE_URL = (os.getenv("HOMEPAGE_BASE_URL") or "https://bokjurh.co.kr").rstrip("/")
POLL_SECONDS = int(os.getenv("HOMEPAGE_BOARD_POLL_SECONDS", "180"))
TIMEOUT = 20
CHANNEL = "웹문의"
CREATED_BY = "홈페이지 게시판"

PUBLIC_LIST = "/sub/07_community/guide_01"
ADMIN_LOGIN_PAGE = "/adm/account/login.php"
ADMIN_LOGIN_AJAX = "/adm/ajax/account/login.php"
ADMIN_LIST = "/adm/sub/counsel/counselL.php"
ADMIN_VIEW = "/adm/sub/counsel/counselV.php"
ADMIN_UPDATE = "/adm/sub/counsel/counselU.php"
ADMIN_UPDATE_AJAX = "/adm/ajax/community/counselUP.php"

# 답변 기본 문안 — 상담실이 실제로 쓰던 답변을 그대로 기본값으로 둔다(화면에서 수정 가능).
REPLY_DEFAULT_TITLE = "문의해 주셔서 감사드립니다"
REPLY_DEFAULT_BODY = (
    "안녕하세요. 복주회복병원입니다.\n"
    "저희가 전화로 상담을 진행하였는데, 문의 주신 내용에는 만족 하셨나요?\n"
    "혹시 더 궁금한 사항 있으시면 입원 문의 전화 직통 번호 054-851-5070으로 전화 주시면 상담이 가능합니다.\n"
    "오늘도 평안한 하루 되세요. 문의 주셔서 감사합니다."
)

_PHONE_RE = re.compile(r"01[016-9]-?\d{3,4}-?\d{4}")
_TAG_RE = re.compile(r"<[^>]+>")


class BoardError(Exception):
    """홈페이지 연동 실패 — 사용자에게 그대로 보여줄 수 있는 한국어 메시지."""


def admin_configured() -> bool:
    return bool(os.getenv("HOMEPAGE_ADMIN_ID") and os.getenv("HOMEPAGE_ADMIN_PW"))


def admin_view_url(idx: int) -> str:
    return f"{BASE_URL}{ADMIN_VIEW}?idx={int(idx)}"


# ───────────────────── HTML 파싱 ─────────────────────

def _text(fragment: str) -> str:
    """HTML 조각 → 평문. <br>·<p>는 줄바꿈으로."""
    s = re.sub(r"(?i)<br\s*/?>", "\n", fragment or "")
    s = re.sub(r"(?i)</p\s*>", "\n", s)
    s = _TAG_RE.sub("", s)
    s = _html.unescape(s).replace("\xa0", " ")
    lines = [ln.strip() for ln in s.splitlines()]
    out, blank = [], 0
    for ln in lines:                       # 3줄 이상 연속 빈 줄은 1줄로
        if ln:
            out.append(ln); blank = 0
        else:
            blank += 1
            if blank == 1:
                out.append("")
    return "\n".join(out).strip()


def parse_public_list(page_html: str) -> list[dict]:
    """공개 목록 → [{idx, board_no, title, status, name, reg_date}] (최신순).
    목록은 <ul class="contentWrap"><a href="…guide_01_view?idx=N"><li class="no|title|status|name|date">…"""
    posts = []
    for m in re.finditer(r'<a[^>]*guide_01_view\?idx=(\d+)[^>]*>(.*?)</a>', page_html, re.S):
        idx, inner = int(m.group(1)), m.group(2)
        cells = {k: _text(v) for k, v in re.findall(r'<li class="(\w+)"[^>]*>(.*?)</li>', inner, re.S)}
        if "title" not in cells and "no" not in cells:
            continue
        no = cells.get("no", "")
        status = cells.get("status", "")
        posts.append({
            "idx": idx,
            "board_no": int(no) if no.isdigit() else None,
            "title": cells.get("title", ""),
            "status": "답변완료" if "답변완료" in status else ("접수" if "접수" in status else status),
            "name": cells.get("name", ""),
            "reg_date": cells.get("date", ""),
        })
    return posts


def _field(page: str, label: str) -> str:
    """관리자 상세 화면의 <th>라벨</th><td>값</td> 한 칸."""
    m = re.search(r"<th[^>]*>\s*" + re.escape(label) + r"\s*</th>\s*<td[^>]*>(.*?)</td>", page, re.S)
    return _text(m.group(1)) if m else ""


def parse_admin_view(page_html: str) -> dict:
    """관리자 글 상세(counselV.php) → 이름·연락처·이메일·제목·본문·기존 답변."""
    top = page_html.split("고객의소리 답변")[0]
    ans = page_html.split("고객의소리 답변")[1] if "고객의소리 답변" in page_html else ""
    return {
        "name": _field(top, "이름"),
        "phone": _field(top, "연락처"),
        "email": _field(top, "이메일"),
        "title": _field(top, "제목"),
        "content": _field(top, "내용"),
        "answer_title": _field(ans, "제목") if ans else "",
        "answer_date": _field(ans, "등록날짜") if ans else "",
        "answer_content": _field(ans, "내용") if ans else "",
    }


def parse_admin_update_form(page_html: str) -> dict:
    """답변 폼(counselU.php)의 hidden 값 — group_idx·answer_idx(기존 답변이면 채워져 있음)."""
    def hidden(name):
        m = re.search(r'name="' + name + r'"[^>]*value="([^"]*)"', page_html)
        return m.group(1) if m else ""
    return {"group_idx": hidden("group_idx"), "answer_idx": hidden("answer_idx")}


def text_to_html(text: str) -> str:
    """답변 평문 → 게시판 에디터가 저장하는 형태의 단순 HTML(<p> 단락)."""
    paras = []
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        lines = [_html.escape(ln.strip()) for ln in block.splitlines() if ln.strip()]
        if lines:
            paras.append("<p>" + "<br>".join(lines) + "</p>")
    return "".join(paras) or "<p></p>"


# ───────────────────── 관리자 세션 ─────────────────────

class AdminSession:
    """홈페이지 /adm 로그인 세션. 로그인 응답이 'success'가 아니면 BoardError."""

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (bokju-crm homepage bridge)",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": BASE_URL + ADMIN_LOGIN_PAGE,
        })
        self._logged_in = False

    def login(self):
        uid = os.getenv("HOMEPAGE_ADMIN_ID", "").strip()
        pw = os.getenv("HOMEPAGE_ADMIN_PW", "").strip()
        if not uid or not pw:
            raise BoardError("홈페이지 관리자 계정이 설정되지 않았습니다 (.env HOMEPAGE_ADMIN_ID/PW).")
        self.s.get(BASE_URL + ADMIN_LOGIN_PAGE, timeout=TIMEOUT)
        r = self.s.post(BASE_URL + ADMIN_LOGIN_AJAX, timeout=TIMEOUT,
                        data={"username": uid, "userpassword": pw, "saveUserInfo": "false"})
        result = r.text.strip().lstrip("﻿")
        if result != "success":
            hint = {"return user": "존재하지 않는 아이디",
                    "return password": "비밀번호 불일치",
                    "return auth": "로그인 불가 계정"}.get(result, "응답 이상")
            raise BoardError(f"홈페이지 관리자 로그인 실패 — {hint}")
        self._logged_in = True

    def _get(self, path: str, **params) -> str:
        if not self._logged_in:
            self.login()
        r = self.s.get(BASE_URL + path, params=params or None, timeout=TIMEOUT)
        if ADMIN_LOGIN_PAGE in r.url or 'id="username"' in r.text:   # 세션 만료 → 재로그인 1회
            self.login()
            r = self.s.get(BASE_URL + path, params=params or None, timeout=TIMEOUT)
        return r.text

    def fetch_post(self, idx: int) -> dict:
        page = self._get(ADMIN_VIEW, idx=idx)
        data = parse_admin_view(page)
        if not data["title"] and not data["content"]:
            raise BoardError(f"홈페이지 글 #{idx} 내용을 읽지 못했습니다 (화면 구조 변경?)")
        data["idx"] = idx
        return data

    def reply(self, idx: int, title: str, body_text: str) -> dict:
        """답변 등록/수정. 기존 답변이 있으면 그 answer_idx로 덮어쓴다(관리자 화면과 동일)."""
        form = parse_admin_update_form(self._get(ADMIN_UPDATE, idx=idx))
        if form["group_idx"] != str(idx):
            raise BoardError(f"홈페이지 답변 폼을 열지 못했습니다 (#{idx})")
        payload = {
            "group_idx": form["group_idx"],
            "answer_idx": form["answer_idx"],
            "title": title.strip(),
            "insert_date": date.today().isoformat(),
            "contents": text_to_html(body_text),
        }
        # 관리자 화면은 FormData(multipart)로 보낸다 — 같은 형식으로.
        files = {k: (None, v) for k, v in payload.items()}
        r = self.s.post(BASE_URL + ADMIN_UPDATE_AJAX, files=files, timeout=TIMEOUT,
                        headers={"Referer": f"{BASE_URL}{ADMIN_UPDATE}?idx={idx}"})
        result = r.text.strip().lstrip("﻿")
        if result != "success":
            raise BoardError(f"홈페이지 답변 등록 실패 — 사이트 응답: {result[:80] or '(빈 응답)'}")
        # 등록 확인 — 상세 화면에 답변이 실제로 붙었는지 본다.
        after = parse_admin_view(self._get(ADMIN_VIEW, idx=idx))
        if not after.get("answer_content"):
            raise BoardError("홈페이지가 success를 돌려줬지만 답변이 보이지 않습니다. 홈페이지 관리자에서 확인해 주세요.")
        return after


_session_lock = threading.Lock()
_session: AdminSession | None = None


def _admin() -> AdminSession:
    global _session
    with _session_lock:
        if _session is None:
            _session = AdminSession()
        return _session


def _reset_admin():
    global _session
    with _session_lock:
        _session = None


# ───────────────────── 폴링 ─────────────────────

def fetch_public_list() -> list[dict]:
    r = requests.get(BASE_URL + PUBLIC_LIST, timeout=TIMEOUT,
                     headers={"User-Agent": "Mozilla/5.0 (bokju-crm homepage bridge)"})
    r.raise_for_status()
    return parse_public_list(r.text)


def _register(post: dict, detail: dict | None) -> int:
    """게시글 1건 → communications 인바운드 1건 + homepage_posts 매핑."""
    no = post.get("board_no")
    head = f"[상담게시판{(' #' + str(no)) if no else ''}] {post.get('title') or '제목 없음'}"
    if detail:
        phone = detail.get("phone") or ""
        phone_m = _PHONE_RE.search(phone.replace(" ", ""))
        phone_norm = None
        if phone_m:
            digits = re.sub(r"\D", "", phone_m.group(0))
            phone_norm = f"{digits[:3]}-{digits[3:-4]}-{digits[-4:]}"
        name = detail.get("name") or post.get("name") or ""
        body = detail.get("content") or ""
        if detail.get("email"):
            body = f"{body}\n\n[이메일] {detail['email']}"
        pid = models.match_patient_by_phone(phone_norm) if phone_norm else None
        contact = phone_norm or name or None
    else:
        name = post.get("name") or ""
        body = "(본문은 홈페이지 관리자 로그인 후 자동으로 채워집니다)"
        pid, contact = None, name or None
    summary = head + (f" · {name}" if name else "")
    comm_id = models.create_communication(
        patient_id=pid, channel=CHANNEL, direction="in", contact=contact,
        summary=summary[:200], body=body[:4000], status="open", created_by=CREATED_BY,
        # 목록엔 날짜만 있다 — 오늘 글은 감지 시각(created_at, 3분 이내)이 더 정확하니 비워 둔다.
        occurred_at=(post.get("reg_date") if post.get("reg_date") and post["reg_date"] != date.today().isoformat() else None),
    )
    models.homepage_post_upsert(
        post["idx"], board_no=no, comm_id=comm_id, title=post.get("title") or "",
        site_status=post.get("status") or "", detail_ok=1 if detail else 0,
        reg_date=post.get("reg_date") or "",
    )
    try:
        models.log_audit(username="homepage-board", action="inbound_webhook",
                         target_type="communication", target_id=comm_id,
                         detail=f"웹문의/in(board idx={post['idx']})")
    except Exception:
        pass
    return comm_id


def _fill_detail(idx: int, comm_id: int, detail: dict) -> None:
    """관리자 로그인이 나중에 성공했을 때 본문·연락처를 채운다."""
    phone_m = _PHONE_RE.search((detail.get("phone") or "").replace(" ", ""))
    contact = None
    if phone_m:
        d = re.sub(r"\D", "", phone_m.group(0))
        contact = f"{d[:3]}-{d[3:-4]}-{d[-4:]}"
    body = detail.get("content") or ""
    if detail.get("email"):
        body = f"{body}\n\n[이메일] {detail['email']}"
    comm = models.get_communication(comm_id) or {}
    fields = {"body": body[:4000]}
    if detail.get("name") and comm.get("summary"):
        fields["summary"] = re.sub(r" · .*$", "", comm["summary"]) + f" · {detail['name']}"
    if contact and not comm.get("patient_id"):
        pid = models.match_patient_by_phone(contact)
        if pid:
            fields["patient_id"] = pid
    models.update_communication(comm_id, **fields)
    if contact:
        conn = models.get_db()
        conn.execute("UPDATE communications SET contact = ? WHERE id = ?", (contact, comm_id))
        conn.commit(); conn.close()
    models.homepage_post_upsert(idx, detail_ok=1)


def poll_once() -> int:
    """공개 목록 1회 확인 → 새 글 등록·직접 답변된 글 완료 처리. 등록 건수 반환."""
    try:
        posts = fetch_public_list()
    except Exception:
        logger.exception("홈페이지 상담게시판 목록 조회 실패 — 다음 주기 재시도")
        return 0
    if not posts:
        logger.warning("홈페이지 상담게시판 목록에서 글을 못 찾았습니다 (화면 구조 변경?)")
        return 0
    known = models.homepage_post_known()
    first_run = not known
    count = 0
    admin_ok = admin_configured()
    for post in posts:
        idx = post["idx"]
        rec = known.get(idx)
        if rec is None:
            # 최초 기동 시 이미 답변완료인 과거 글은 인박스에 쌓지 않고 매핑만 남긴다.
            if first_run and post["status"] == "답변완료":
                models.homepage_post_upsert(idx, board_no=post.get("board_no"), title=post.get("title"),
                                            site_status=post["status"], detail_ok=1,
                                            reg_date=post.get("reg_date"))
                continue
            detail = None
            if admin_ok:
                try:
                    detail = _admin().fetch_post(idx)
                except Exception as e:
                    _reset_admin()
                    admin_ok = False
                    logger.warning("홈페이지 관리자에서 본문을 못 읽어 제목만 등록합니다: %s", e)
            comm_id = _register(post, detail)
            count += 1
            if post["status"] == "답변완료":      # 등록 직후 이미 답변된 글(드묾) — 열어두지 않는다
                models.update_communication(comm_id, status="done")
            continue
        # 이미 아는 글 — 본문 미수집분 보충
        if not rec.get("detail_ok") and rec.get("comm_id") and admin_ok:
            try:
                _fill_detail(idx, rec["comm_id"], _admin().fetch_post(idx))
            except Exception as e:
                _reset_admin(); admin_ok = False
                logger.warning("홈페이지 본문 보충 실패(#%s): %s", idx, e)
        # 홈페이지 관리자에서 직접 답변한 경우 → CRM 인박스도 완료
        if post["status"] != rec.get("site_status"):
            models.homepage_post_upsert(idx, site_status=post["status"])
            if post["status"] == "답변완료" and rec.get("comm_status") == "open" and rec.get("comm_id"):
                models.update_communication(rec["comm_id"], status="done")
                models.homepage_post_upsert(idx, answered_by="홈페이지 관리자(직접)")
                logger.info("홈페이지에서 직접 답변된 글 #%s → 인박스 완료 처리", idx)
    if count:
        logger.info("홈페이지 상담게시판 새 글 %d건 인박스 등록", count)
    return count


def reply_to_post(comm_id: int, title: str, body_text: str, username: str) -> dict:
    """CRM 인박스 '답변' → 홈페이지 게시판에 답변 등록 → 인박스 완료. 실패 시 BoardError."""
    rec = models.homepage_post_by_comm(comm_id)
    if not rec:
        raise BoardError("홈페이지 게시판 글과 연결되지 않은 문의입니다.")
    if not (title or "").strip():
        raise BoardError("답변 제목을 입력하세요.")
    if not (body_text or "").strip():
        raise BoardError("답변 내용을 입력하세요.")
    try:
        after = _admin().reply(int(rec["idx"]), title, body_text)
    except BoardError:
        _reset_admin()
        raise
    except requests.RequestException as e:
        _reset_admin()
        raise BoardError(f"홈페이지 접속 실패 — {e.__class__.__name__}") from e
    models.homepage_post_upsert(int(rec["idx"]), site_status="답변완료", answered_by=username,
                                answered_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    models.update_communication(comm_id, status="done")
    try:
        models.log_audit(username=username, action="homepage_reply",
                         target_type="communication", target_id=comm_id,
                         detail=f"board idx={rec['idx']} title={title[:40]}")
    except Exception:
        pass
    return after


# ───────────────────── 워커 ─────────────────────

def _loop():
    while True:
        time.sleep(POLL_SECONDS)
        try:
            poll_once()
        except Exception:
            logger.exception("홈페이지 상담게시판 폴링 예외")


def start_worker():
    """데몬 스레드 시작. HOMEPAGE_BOARD_ENABLED=0 이면 끈다."""
    if os.getenv("HOMEPAGE_BOARD_ENABLED", "1") != "1":
        logger.info("홈페이지 상담게시판 연동 비활성 (HOMEPAGE_BOARD_ENABLED=0)")
        return
    if not admin_configured():
        logger.info("홈페이지 관리자 계정 미설정 — 새 글 감지·알림만 하고 본문·답변은 비활성")
    threading.Thread(target=_bootstrap, name="homepage-board", daemon=True).start()


def _bootstrap():
    try:
        poll_once()          # 기동 시 1회 (외부 접속이라 부팅을 막지 않도록 스레드 안에서)
    except Exception:
        logger.exception("홈페이지 상담게시판 최초 폴링 실패")
    logger.info("홈페이지 상담게시판 연동 시작 — %d초마다 확인", POLL_SECONDS)
    _loop()
