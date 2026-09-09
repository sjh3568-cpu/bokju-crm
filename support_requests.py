"""로그인 사용자 문의 및 관리자 답변. 자료는 CRM DB에만 저장한다."""
import secrets
from contextlib import closing
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
import models
from auth import current_user, login_required
from config import APP_VERSION

bp = Blueprint('support', __name__, url_prefix='/support')
CATEGORIES = ('기능 개선', '오류 신고', '사용 문의', '기타')
STATUSES = ('접수', '검토 중', '진행 중', '완료', '보류')


def init_schema():
    with closing(models.get_db()) as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS support_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            category TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
            version TEXT NOT NULL, status TEXT NOT NULL DEFAULT '접수',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS support_user_updated ON support_requests(user_id, updated_at);
        CREATE TABLE IF NOT EXISTS support_replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL REFERENCES support_requests(id),
            user_id INTEGER NOT NULL REFERENCES users(id),
            body TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        ''')
        db.commit()


def csrf_token():
    if 'support_csrf' not in session:
        session['support_csrf'] = secrets.token_hex(32)
    return session['support_csrf']


def check_csrf():
    token = session.get('support_csrf')
    if not token or not secrets.compare_digest(token, request.form.get('csrf', '')):
        abort(403)


def is_manager():
    return current_user().get('role') == 'admin'


def ticket_or_404(db, ticket_id):
    row = db.execute('''SELECT s.*, u.display_name AS author,
        datetime(s.created_at,'localtime') AS created_local,
        datetime(s.updated_at,'localtime') AS updated_local
        FROM support_requests s JOIN users u ON u.id=s.user_id WHERE s.id=?''', (ticket_id,)).fetchone()
    if not row or (not is_manager() and row['user_id'] != current_user()['id']):
        abort(404)
    return row


def audit(action, ticket_id):
    user = current_user()
    models.log_audit(user_id=user['id'], username=user['username'], action=action,
                     target_type='support_request', target_id=ticket_id, ip=request.remote_addr)


@bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    values = request.form if request.method == 'POST' else {}
    error = None
    if request.method == 'POST':
        check_csrf()
        category = values.get('category', '')
        title, body = values.get('title', '').strip(), values.get('body', '').strip()
        if category not in CATEGORIES or not (1 <= len(title) <= 120) or not (1 <= len(body) <= 5000):
            error = '유형을 선택하고 제목(1~120자)과 내용(1~5,000자)을 입력해주세요.'
        else:
            with closing(models.get_db()) as db:
                with db:
                    cur = db.execute('''INSERT INTO support_requests
                        (user_id, category, title, body, version) VALUES (?, ?, ?, ?, ?)''',
                        (current_user()['id'], category, title, body, APP_VERSION))
                    ticket_id = cur.lastrowid
            audit('create_support', ticket_id)
            flash('요청을 접수했습니다. 이 화면에서 답변과 처리 상태를 확인할 수 있습니다.', 'success')
            return redirect(url_for('support.detail', ticket_id=ticket_id))
    status = request.args.get('status', '')
    if status and status not in STATUSES:
        abort(400)
    page = max(1, request.args.get('page', 1, type=int))
    where, args = [], []
    if not is_manager():
        where.append('s.user_id=?'); args.append(current_user()['id'])
    if status:
        where.append('s.status=?'); args.append(status)
    clause = ' WHERE ' + ' AND '.join(where) if where else ''
    with closing(models.get_db()) as db:
        rows = db.execute('''SELECT s.*, u.display_name AS author,
            datetime(s.updated_at,'localtime') AS updated_local
            FROM support_requests s JOIN users u ON u.id=s.user_id''' + clause +
            ' ORDER BY s.updated_at DESC,s.id DESC LIMIT 21 OFFSET ?', (*args, (page-1)*20)).fetchall()
    return render_template('support.html', tickets=rows[:20], has_next=len(rows)>20,
        page=page, status=status, categories=CATEGORIES, statuses=STATUSES,
        manager=is_manager(), csrf=csrf_token(), values=values, error=error), (400 if error else 200)


@bp.route('/<int:ticket_id>', methods=['GET', 'POST'])
@login_required
def detail(ticket_id):
    error = None
    with closing(models.get_db()) as db:
        ticket = ticket_or_404(db, ticket_id)
        if request.method == 'POST':
            check_csrf()
            if not is_manager():
                abort(403)
            body, status = request.form.get('body', '').strip(), request.form.get('status', '')
            if status not in STATUSES or not (1 <= len(body) <= 5000):
                error = '처리 상태와 답변(1~5,000자)을 입력해주세요.'
            else:
                with db:
                    db.execute('INSERT INTO support_replies (request_id,user_id,body,status) VALUES (?,?,?,?)',
                               (ticket_id, current_user()['id'], body, status))
                    db.execute('UPDATE support_requests SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                               (status, ticket_id))
                audit('reply_support', ticket_id)
                flash('답변과 처리 상태를 저장했습니다.', 'success')
                return redirect(url_for('support.detail', ticket_id=ticket_id))
        replies = db.execute('''SELECT r.*,u.display_name AS author,
            datetime(r.created_at,'localtime') AS created_local
            FROM support_replies r JOIN users u ON u.id=r.user_id
            WHERE request_id=? ORDER BY r.id''', (ticket_id,)).fetchall()
    return render_template('support_detail.html', ticket=ticket, replies=replies,
        manager=is_manager(), statuses=STATUSES, csrf=csrf_token(), error=error,
        values=request.form), (400 if error else 200)
