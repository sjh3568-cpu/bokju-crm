"""인증 — werkzeug 비밀번호 해시 + Flask 세션.

- 직원별 계정: config.SEED_USERS가 최초 부팅 시 자동 생성 (초기 비번=.env APP_PASSWORD).
  이후 어드민이 '사용자 관리'(/admin/users)에서 개별 비번·역할 관리.
- 역할 3단계: viewer(조회) < staff(상담사) < admin(어드민). ROLE_RANK 참고.
  · writer_required = 상담사 이상 (쓰기 가능 화면·폼). viewer 차단.
  · admin_required  = 어드민 전용 (사용자 관리·CSV 내보내기·환자 병합)
  · viewer(조회 공통 계정)의 모든 쓰기는 app._enforce_readonly_viewer에서 일괄 차단.
- admin 계정은 비번 분실 대비 break-glass (매 부팅 시 .env APP_PASSWORD로 동기화).

5회 실패 시 5분 잠금은 audit_log를 카운트해서 처리.
"""
from datetime import datetime, timedelta
from functools import wraps

from flask import abort, flash, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash

from models import get_db, get_user, log_audit, touch_user_login

LOCKOUT_THRESHOLD = 5
LOCKOUT_MINUTES = 5

# 역할 서열 — 숫자가 클수록 권한이 넓다. viewer < staff < admin
ROLE_RANK = {"viewer": 0, "staff": 1, "admin": 2}


def has_min_role(user, min_role: str) -> bool:
    """user의 역할이 min_role 이상인지."""
    return ROLE_RANK.get((user or {}).get("role", "staff"), 0) >= ROLE_RANK.get(min_role, 99)


def is_locked_out(ip: str) -> bool:
    if not ip:
        return False
    since = (datetime.now() - timedelta(minutes=LOCKOUT_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
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
    }


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


def role_required(min_role: str):
    """min_role 이상 권한이 있어야 접근 가능한 뷰 데코레이터."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                if request.path.startswith("/api/"):
                    abort(401)
                flash("로그인이 필요합니다.", "warn")
                return redirect(url_for("login_view", next=request.path))
            if not has_min_role(user, min_role):
                abort(403)
            g.user = user
            return view(*args, **kwargs)
        return wrapped
    return decorator


# staff 이상(상담사·어드민 — 쓰기 가능) / admin 전용(어드민)
# viewer(조회)는 아래 두 데코레이터에서 모두 차단된다.
writer_required = role_required("staff")
admin_required = role_required("admin")
