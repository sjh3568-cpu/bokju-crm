"""배포에 포함된 사용자 안내를 서버 시작 시 버전당 한 번 게시한다."""
import models
from config import APP_DEVELOPER, APP_VERSION

# 새 버전 배포 시 기존 항목을 보존하고 사용자 관점의 변경 안내를 추가한다.
RELEASE_NOTES = (
    {
        'version': '1.1.0',
        'title': '복주 CRM v1.1.0 — 업데이트 안내를 공지사항에서 확인하세요',
        'body': '''상담실 여러분, 복주 CRM의 변경 내용을 공지사항에서 확인하실 수 있도록 개선했습니다.

무엇이 달라졌나요?
• 앞으로 새 버전이 서버에 적용되면, 해당 버전의 변경 안내가 공지사항에 등록됩니다.
• 로그인 화면과 CRM 화면 아래에서 현재 버전과 개발 담당자를 확인할 수 있습니다.

어떻게 사용하나요?
• 상단의 ‘공지사항’을 선택하면 변경 내용과 사용 방법을 확인할 수 있습니다.
• 사용 중 문의할 내용이 있으면 화면 아래의 버전 번호를 함께 알려주세요.

최근 개선한 메뉴 사용 방법
• 하위 메뉴가 있는 상단 메뉴와 우측 개인 메뉴에 마우스를 올리면 메뉴가 펼쳐집니다.
• 커서를 메뉴 밖으로 옮기거나, 열린 메뉴 제목을 다시 클릭하면 닫힙니다. 바깥 클릭이나 Esc 키로도 닫을 수 있습니다.

이번 업데이트 안내는 별도의 필수 확인 없이 읽으실 수 있습니다.
개발 · 미래전략실 신재희''',
    },
)


def publish_release_notes():
    if not RELEASE_NOTES or RELEASE_NOTES[-1]['version'] != APP_VERSION:
        raise ValueError('현재 APP_VERSION에 맞는 사용자용 변경 안내가 필요합니다.')
    conn = models.get_db()
    try:
        with conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS release_announcements (
                version TEXT PRIMARY KEY,
                announcement_id INTEGER NOT NULL
            )''')
            # 여러 서버 프로세스가 시작해도 공지와 버전 기록을 함께 한 번만 저장한다.
            conn.execute('BEGIN IMMEDIATE')
            for note in RELEASE_NOTES:
                if conn.execute('SELECT 1 FROM release_announcements WHERE version=?',
                                (note['version'],)).fetchone():
                    continue
                cur = conn.execute('''INSERT INTO announcements
                    (title, body, target_role, requires_ack, created_by_name)
                    VALUES (?, ?, 'all', 0, ?)''',
                    (note['title'], note['body'], APP_DEVELOPER))
                conn.execute('INSERT INTO release_announcements VALUES (?, ?)',
                             (note['version'], cur.lastrowid))
    finally:
        conn.close()
