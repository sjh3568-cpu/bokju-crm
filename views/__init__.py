"""화면·API 라우트 Blueprint 묶음 — app.py(앱 생성·공통 훅·템플릿 필터·공용 헬퍼)에서 맨 아래에 등록한다.
각 모듈은 `bp = Blueprint(...)` 하나와 그 구역의 라우트·전용 헬퍼만 가진다. 공용 헬퍼는 app.py에 두고 `from app import`.
"""
