"""인증 — werkzeug 비밀번호 해시 + Flask 세션.

- MVP: .env의 APP_PASSWORD로 단일 admin 계정 자동 셋업
- Phase 4: users 테이블의 직원별 계정 분리

5회 실패 시 5분 잠금은 audit_log를 카운트해서 처리.
"""
from datetime import datetime, timedelta
from functools import wraps

from flask import abort, flash, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash

from models import get_db, get_user, log_audit, touch_user_login

LOCKOUT_THRESHOLD = 5
LOCKOUT_MINUTES = 5


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


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            if request.path.startswith("/api/"):
                abort(401)
            return redirect(url_for("login_view", next=request.path))
        if user.get("role") != "admin":
            abort(403)
        g.user = user
        return view(*args, **kwargs)
    return wrapped
