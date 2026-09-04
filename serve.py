"""운영 진입점 — waitress WSGI 서버.

Flask 개발 서버(app.py의 app.run)는 단일 개발자용이라 운영에 쓰지 않는다.
waitress는 순수 파이썬 멀티스레드 WSGI 서버로 Windows·리눅스 컨테이너 모두에서
동일하게 돌고, 상담사 4명이 동시에 저장해도 요청이 큐잉되어 유실되지 않는다.

실행:  python serve.py       (환경변수는 .env 또는 컨테이너 env에서)
"""
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from app import app, initialize  # noqa: E402  (load_dotenv 이후 임포트해야 설정이 반영됨)

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    from waitress import serve

    # DB 초기화·계정 시드·백업 스케줄러를 첫 요청 전에 끝내둔다.
    initialize()

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8003"))
    # 상담사 4명 + 조회 계정 + 여유. 스레드 수가 동시 처리 가능한 요청 수다.
    threads = int(os.getenv("WAITRESS_THREADS", "8"))

    logging.getLogger().info("bokju-crm 시작 — http://%s:%d (threads=%d)", host, port, threads)
    serve(app, host=host, port=port, threads=threads, ident="bokju-crm")
