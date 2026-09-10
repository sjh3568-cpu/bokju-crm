#!/usr/bin/env bash
# 운영에 내보낼 버전을 확정한다 (노트북에서 실행).
#
# 운영(NAS)은 main을 그대로 받지 않고 여기서 찍은 태그만 받는다.
# "지금 운영에 떠 있는 게 정확히 어느 버전인지"를 알 수 있어야
# 문제가 생겼을 때 이전 태그로 되돌릴 수 있다.
#
#   ./release.sh          검사 → 태그 생성 → push
#   ./release.sh --check  검사만 (태그를 만들지 않음)
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv-linux/bin/python
CHECK_ONLY=${1:-}

fail() { echo "  ✗ $1"; exit 1; }

echo "▶ 1/5 작업 내용이 모두 커밋되었는지"
[ -z "$(git status --porcelain)" ] || fail "커밋하지 않은 변경이 있습니다. 먼저 커밋하세요.
$(git status --short)"
echo "  ✓ 깨끗함"

echo "▶ 2/5 버전 번호"
VERSION=$($PY -c "from config import APP_VERSION; print(APP_VERSION)")
TAG="v${VERSION}"
echo "  ✓ ${TAG}"

echo "▶ 3/5 이 버전의 사용자 안내가 쓰여 있는지"
# 버전만 올리고 상담사 안내를 빠뜨리면, 화면은 바뀌었는데 아무도 이유를 모른다.
$PY -c "
import sys, release_notes
from config import APP_VERSION
versions = [n['version'] for n in release_notes.RELEASE_NOTES]
if APP_VERSION not in versions:
    sys.exit(f\"release_notes.py에 {APP_VERSION} 항목이 없습니다. 상담사용 변경 안내를 먼저 쓰세요.\")
if versions[-1] != APP_VERSION:
    sys.exit(f'마지막 항목이 {versions[-1]}입니다. {APP_VERSION} 항목을 맨 뒤에 두세요.')
" || fail "$($PY -c "
import release_notes
from config import APP_VERSION
print('release_notes.py에', APP_VERSION, '항목을 추가하세요.')" 2>/dev/null)"
echo "  ✓ 안내 있음"

echo "▶ 4/5 이미 나간 버전인지"
git fetch --tags --quiet 2>/dev/null || true
! git rev-parse "$TAG" >/dev/null 2>&1 || fail "${TAG}는 이미 있습니다. config.py의 APP_VERSION을 올리세요."
echo "  ✓ 새 버전"

echo "▶ 5/5 테스트"
$PY -m unittest discover -s tests --quiet 2>&1 | tail -3
echo "  ✓ 통과"

if [ "$CHECK_ONLY" = "--check" ]; then
    echo ""
    echo "검사만 했습니다. 내보내려면 인자 없이 다시 실행하세요."
    exit 0
fi

# 순서가 중요하다 — main을 먼저 올린다.
# 태그를 먼저 찍으면, push가 거절될 때(원격에 다른 작업이 올라와 있는 경우)
# 태그만 로컬에 남아 다음 실행이 "이미 있는 버전"으로 막힌다.
echo "▶ main 올리는 중"
git push origin main --quiet

git tag -a "$TAG" -m "복주 CRM ${VERSION}"
# 태그 push가 실패하면 로컬 태그를 지워 다음 실행이 막히지 않게 한다.
git push origin "$TAG" --quiet || { git tag -d "$TAG"; fail "태그를 올리지 못했습니다."; }

echo ""
echo "✅ ${TAG} 를 내보냈습니다."
echo ""
echo "이제 NAS에서 적용하세요 — docs/DEPLOY-NAS.md의 '운영 배포' 참고."
echo "되돌릴 때 쓸 직전 버전: $(git tag --sort=-creatordate | sed -n 2p || echo '없음(첫 배포)')"
