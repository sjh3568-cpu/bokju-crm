"""인증 — werkzeug 비밀번호 해시 + Flask 세션.

- 직원별 계정: config.SEED_USERS가 최초 부팅 시 자동 생성 (초기 비번=.env APP_PASSWORD).
  이후 어드민이 '사용자 관리'(/admin/users)에서 계정별 비번·역할·메뉴 권한 관리.
- 접근 판정은 계정별 '메뉴 권한 매트릭스'(users.permissions)로 한다. 단계형 레벨:
  미현시(0) < 조회(1) < 수정(2) < 등록(3). 상위 레벨은 하위를 포함.
  · 역할(admin/staff/viewer)은 이제 권한 '프리셋' 이름일 뿐 — 실제 판정은 perms.
  · 경로→메뉴→필요레벨 매핑과 일괄 차단은 app._enforce_menu_permissions에서 처리.
  · admin_required = 사용자 관리(users) 메뉴 '수정' 이상 (사용자 관리 화면 방어).
- admin 계정은 비번 분실 대비 break-glass (매 부팅 시 .env APP_PASSWORD로 동기화).

5회 실패 시 5분 잠금은 audit_log를 카운트해서 처리.
"""
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import abort, flash, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash

from config import MENU_KEYS, PERM_EDIT
from models import get_db, get_user, log_audit, touch_user_login

LOCKOUT_THRESHOLD = 5
LOCKOUT_MINUTES = 5


def is_locked_out(ip: str) -> bool:
    if not ip:
        return False
    # audit_log.created_at은 CURRENT_TIMESTAMP = UTC로 저장된다.
    # 로컬 시각으로 비교하면 KST 기준 9시간 어긋나 잠금이 아예 걸리지 않으므로 UTC로 맞춘다.
    since = (datetime.now(timezone.utc) - timedelta(minutes=LOCKOUT_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action='login_fail' AND ip = ? AND created_at >= ?",
        (ip, since),
    ).fetchone()["n"]
    conn.close()
    return n >= LOCKOUT_THRESHOLD


def authenticate(username: str, password: str, ip: str | None = None):
    user = get_user(username)
    if not user or not check_password_hash(user["password_hash"], password):
        log_audit(username=username, action="login_fail", ip=ip,
                  detail="bad credentials")
        return None
    touch_user_login(user["id"])
    log_audit(user_id=user["id"], username=username, action="login", ip=ip)
    return user


def login_user(user: dict):
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["display_name"] = user.get("display_name") or user["username"]
    session["role"] = user.get("role", "staff")
    # 유효 권한 매트릭스(모든 메뉴 채워진 값)를 세션에 저장 → 요청마다 DB 조회 불필요
    session["perms"] = {k: int(user.get("perms", {}).get(k, 0)) for k in MENU_KEYS}
    session["login_at"] = datetime.now().isoformat()


def logout_user():
    user_id = session.get("user_id")
    username = session.get("username")
    if user_id:
        log_audit(user_id=user_id, username=username, action="logout",
                  ip=request.remote_addr if request else None)
    session.clear()


def current_user():
    if not session.get("user_id"):
        return None
    return {
        "id": session["user_id"],
        "username": session["username"],
        "display_name": session.get("display_name"),
        "role": session.get("role", "staff"),
        "perms": session.get("perms", {}),
    }


def menu_level(user, menu_key: str) -> int:
    """user가 해당 메뉴에 대해 가진 권한 레벨 (0~3)."""
    return int((user or {}).get("perms", {}).get(menu_key, 0))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            if request.path.startswith("/api/"):
                abort(401)
            flash("로그인이 필요합니다.", "warn")
            return redirect(url_for("login_view", next=request.path))
        g.user = user
        return view(*args, **kwargs)
    return wrapped


def menu_required(menu_key: str, min_level: int):
    """특정 메뉴 min_level 이상이어야 접근 가능한 뷰 데코레이터 (라우트별 방어용)."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                if request.path.startswith("/api/"):
                    abort(401)
                flash("로그인이 필요합니다.", "warn")
                return redirect(url_for("login_view", next=request.path))
            if menu_level(user, menu_key) < min_level:
                abort(403)
            g.user = user
            return view(*args, **kwargs)
        return wrapped
    return decorator


# 사용자 관리 화면 — users 메뉴 '수정' 이상. (전역 훅과 별개로 라우트에도 방어를 건다.)
admin_required = menu_required("users", PERM_EDIT)
