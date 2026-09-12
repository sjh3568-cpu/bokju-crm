#!/usr/bin/env bash
# 운영(NAS) 배포 — 태그 하나를 받아 [백업 → 코드 교체 → 재빌드 → 재시작 → 확인]을
# 한 번에 한다. Synology DS720+ / DSM 7.2 Container Manager(= docker compose v2) 기준.
#
#   sudo ./deploy.sh v1.8.4      지정한 태그로 배포
#   sudo ./deploy.sh             원격의 최신 태그로 배포
#   sudo ./deploy.sh --rollback  직전 태그로 되돌리기
#   sudo ./deploy.sh --check     점검만 (아무것도 바꾸지 않음)
#   sudo ./deploy.sh --yes       업무시간 확인 질문 생략 (무인 실행용, 태그와 같이 써도 됨)
#
# 안전장치:
# - git·docker·curl이 없거나 저장소가 아니면 "아무것도 건드리기 전에" 멈춘다.
# - .env·data(실환자 DB)·backups는 git이 추적하지 않으므로 checkout이 건드리지 않는다.
# - 배포 전 DB를 backups/ 로 복사한다. 기동이 확인되지 않으면 롤백 방법을 안내한다.
# 이 스크립트는 프로젝트 폴더 안에서도, 그 상위 폴더(옆)에서도, /root 처럼 아예 다른
# 곳에서도 실행할 수 있다(그때는 BOKJU_PROJ 또는 기본 /volume1/docker/bokju-crm).
# 프로젝트 밖에 두고 실행하면 checkout이 이 파일을 건드리지 않아 롤백까지 안전하고,
# root 전용 위치(/root, 700)에 두면 sudoers NOPASSWD 대상으로 삼아도 안전하다.
set -euo pipefail

# sudo 로 돌면 PATH 가 /usr/bin:/bin 정도로 줄어 DSM 패키지 명령(docker-compose 등)을
# 못 찾는다. 시놀로지가 심볼릭 링크를 두는 /usr/local/bin 을 뒤에 붙여 둔다.
export PATH="$PATH:/usr/local/bin"

# ── 프로젝트 폴더 찾기 (이 스크립트와 같은 폴더 → 그 안 bokju-crm/ → 환경변수/기본 경로) ──
HERE="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_PROJ="${BOKJU_PROJ:-/volume1/docker/bokju-crm}"
if [ -f "$HERE/docker-compose.yml" ]; then PROJ="$HERE"
elif [ -f "$HERE/bokju-crm/docker-compose.yml" ]; then PROJ="$HERE/bokju-crm"
elif [ -f "$DEFAULT_PROJ/docker-compose.yml" ]; then PROJ="$DEFAULT_PROJ"
else echo "✗ bokju-crm 프로젝트 폴더(docker-compose.yml)를 찾지 못했습니다. BOKJU_PROJ=<경로> 로 알려주세요."; exit 1; fi
cd "$PROJ"

# ── 인자: --yes 는 어디에 와도 되고, 나머지 하나가 모드/태그 ──
MODE=""; YES=0
for a in "$@"; do
    case "$a" in
        --yes|-y) YES=1 ;;
        *) MODE="$a" ;;
    esac
done
DB="data/bokju.db"
BACKUP_DIR="backups"
HEALTH="http://127.0.0.1:8003/healthz"
LOGIN="http://127.0.0.1:8003/login"

fail() { echo "✗ $1" >&2; exit 1; }

# ── 0. 사전 점검 — 없으면 멈춘다 ──
command -v git  >/dev/null 2>&1 || fail "git이 없습니다. 패키지센터에서 'Git Server'를 설치하세요."
command -v curl >/dev/null 2>&1 || fail "curl이 없습니다."
[ -d .git ] || fail "여기가 git 저장소가 아닙니다. 최초 1회 전환이 필요합니다(docs/DEPLOY-NAS.md '한 줄 배포 준비')."
if docker compose version >/dev/null 2>&1; then DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then DC="docker-compose"
else fail "docker compose 명령을 찾지 못했습니다. Container Manager가 설치돼 있는지, sudo로 실행했는지 확인하세요."; fi
# 추적 파일만 본다 — data/ 아래 적재용 엑셀 같은 미추적 파일은 배포와 무관하다.
[ -z "$(git status --porcelain --untracked-files=no 2>/dev/null)" ] || fail "NAS 저장소에 손댄 흔적이 있습니다(운영 폴더는 직접 수정 금지). 원인 확인 후 다시 시도하세요.
$(git status --short --untracked-files=no)"

git fetch --tags --quiet || fail "원격에서 태그를 받지 못했습니다(네트워크·인증 확인)."
PREV_TAG=$(git tag --sort=-creatordate | sed -n 2p || true)

if [ "$MODE" = "--check" ]; then
    echo "✓ 점검 통과 — git·$DC·curl 준비됨, 저장소 깨끗함."
    echo "  현재: $(git describe --tags --exact-match 2>/dev/null || echo '태그 아님')"
    echo "  최신 태그: $(git tag --sort=-creatordate | sed -n 1p || echo '없음')"
    exit 0
fi

# ── 1. 대상 태그 결정 ──
if [ "$MODE" = "--rollback" ]; then
    TAG="$PREV_TAG"
    [ -n "$TAG" ] || fail "되돌릴 직전 태그가 없습니다."
    echo "↩ 롤백 대상: $TAG"
elif [ -n "$MODE" ]; then
    TAG="$MODE"
else
    TAG=$(git tag --sort=-creatordate | sed -n 1p || true)
    [ -n "$TAG" ] || fail "배포할 태그가 없습니다."
fi
git rev-parse "$TAG" >/dev/null 2>&1 || fail "태그 '$TAG'를 찾을 수 없습니다. (sudo ./deploy.sh --check 로 최신 태그 확인)"

CUR=$(git describe --tags --exact-match 2>/dev/null || echo "(태그 아님)")
echo "현재: $CUR   →   배포: $TAG"

# ── 업무시간 경고 (재시작 순간 저장 유실 위험) ──
# 평일 09~18시엔 한 번 묻는다. 터미널이 아니면(ssh 무인 실행) 물을 수 없으므로
# --yes 가 없는 한 조용히 취소되지 않고 "왜 안 됐는지"를 남기며 멈춘다.
H=$(date +%H); DOW=$(date +%u)
if [ "$DOW" -le 5 ] && [ "$H" -ge 9 ] && [ "$H" -lt 18 ] && [ "$YES" != 1 ]; then
    if [ -t 0 ]; then
        read -r -p "⚠ 지금은 상담 업무시간(평일 09~18시)입니다. 재시작 순간 입력이 유실될 수 있습니다. 계속할까요? (y/N) " ans
        [ "$ans" = "y" ] || [ "$ans" = "Y" ] || { echo "취소했습니다."; exit 0; }
    else
        fail "상담 업무시간(평일 09~18시)입니다. 그래도 배포하려면 --yes 를 붙이세요."
    fi
fi

# ── 2. DB 백업 먼저 ──
if [ -f "$DB" ]; then
    mkdir -p "$BACKUP_DIR"
    B="$BACKUP_DIR/manual_배포전_$(date +%Y%m%d_%H%M%S).db"
    cp "$DB" "$B"
    echo "✓ DB 백업: $PROJ/$B"
fi

# ── 3. 태그 파일로 교체 (.env·data·backups는 gitignore라 안 건드림) ──
git checkout -q "$TAG" || fail "코드 교체(checkout) 실패."
echo "✓ 코드 교체: $TAG"

# ── 4. 재빌드 → 재시작 ──
echo "▶ 빌드·재시작 중... ($DC up -d --build)"
$DC up -d --build || fail "빌드/재시작 실패. 직전으로 되돌리려면:  sudo $0 --rollback"

# ── 5. 기동 확인 — /healthz 가 뜰 때까지 최대 60초 ──
echo -n "▶ 기동 확인"
for _ in $(seq 1 30); do
    if curl -fs "$HEALTH" >/dev/null 2>&1; then
        echo " ✓"
        VER=$(curl -fs "$LOGIN" | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
        echo ""
        echo "✅ 배포 완료 — $TAG   (화면 표시 버전: ${VER:-확인필요})"
        echo "   문제 시 되돌리기:  sudo $0 --rollback   (직전 태그: ${PREV_TAG:-없음})"
        exit 0
    fi
    echo -n "."; sleep 2
done
echo ""
echo "✗ 60초 안에 기동이 확인되지 않았습니다."
echo "   로그 보기:   $DC logs --tail 60"
echo "   되돌리기:    sudo $0 --rollback"
exit 1
