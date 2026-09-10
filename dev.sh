#!/usr/bin/env bash
# 개발용 서버 실행 — 코드를 고치면 자동으로 다시 읽는다(--reload).
#
# 실사용(상담사 4명)은 이 스크립트가 아니라 NAS 컨테이너의 serve.py(waitress)를 쓴다.
# 여기는 노트북에서 혼자 확인하는 용도라, 저장할 때마다 잠깐 끊겨도 문제가 없다.
#
#   ./dev.sh          실행 (기존 서버는 자동 종료)
#   ./dev.sh stop     종료만
set -euo pipefail
cd "$(dirname "$0")"

PORT=8003
PY=.venv-linux/bin/python

stop_servers() {
    # 이 저장소의 flask 개발 서버만 골라 종료한다.
    pkill -f "flask --app app run .*--port ${PORT}" 2>/dev/null || true
    sleep 1
}

stop_servers
[ "${1:-}" = "stop" ] && { echo "서버를 종료했습니다."; exit 0; }

# WSL의 IP는 재부팅할 때마다 바뀌므로 그때그때 알아낸다.
WSL_IP=$(hostname -I | awk '{print $1}')

for HOST in 127.0.0.1 "$WSL_IP"; do
    nohup $PY -m flask --app app run --host "$HOST" --port $PORT --no-debugger --reload \
        >> server.log 2>&1 &
    sleep 1
done

sleep 2
echo "서버를 켰습니다 (코드 수정 시 자동 반영)."
echo "  이 PC에서:   http://127.0.0.1:${PORT}"
echo "  브라우저 주소: http://${WSL_IP}:${PORT}"
echo "  로그:         tail -f server.log"
