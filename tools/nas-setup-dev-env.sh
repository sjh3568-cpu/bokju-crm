#!/usr/bin/env bash
# 복주 CRM — NAS에 개발(dev) 환경을 만드는 1회용 스크립트. root로 실행한다.
#
#   ssh -t admin@172.16.1.250 'sudo bash /volume1/docker/bokju-setup-dev.sh'
#
# 하는 일
#   1) /volume1/docker/bokju-crm-dev 에 dev 브랜치를 clone
#   2) 개발용 .env 생성 (운영 값 복사 + APP_ENV=dev / 포트 8004 / 자동백업 끔)
#   3) 운영 DB를 SQLite 백업 API로 스냅샷 떠서 개발 DB로 심음 (운영 무중단)
#   4) 배포 스크립트 3종 설치 + sudoers 등록 → 이후 PC에서 무인 실행
#   5) 개발 컨테이너 기동 (http://172.16.1.250:8004)
#
# 몇 번을 다시 돌려도 안전하다(이미 있으면 갱신만 한다).
# 되돌리기는 이 파일 맨 아래 '되돌리기' 주석 참고.
set -euo pipefail

PROD=/volume1/docker/bokju-crm
DEV=/volume1/docker/bokju-crm-dev
REPO=https://github.com/sjh3568-cpu/bokju-crm.git
DEV_BRANCH=dev
DEPLOY_USER=admin

say() { echo; echo "== $*"; }

[ "$(id -u)" = "0" ] || { echo "root로 실행해야 합니다:  sudo bash $0"; exit 1; }
[ -d "$PROD/.git" ] || { echo "운영 저장소가 없습니다: $PROD"; exit 1; }

# ── 1. 개발 저장소 ────────────────────────────────────────────────
say "1/5  개발 저장소 준비 ($DEV_BRANCH 브랜치)"
if [ -d "$DEV/.git" ]; then
    git -C "$DEV" fetch origin
    git -C "$DEV" checkout -B "$DEV_BRANCH" "origin/$DEV_BRANCH"
    # 개발본은 언제든 버려도 되는 사본이라 원격 상태로 강제 정렬한다.
    git -C "$DEV" reset --hard "origin/$DEV_BRANCH"
else
    git clone -b "$DEV_BRANCH" "$REPO" "$DEV"
fi
mkdir -p "$DEV/data" "$DEV/backups"

# KRPG 조회표는 이미지 안으로 들어가지만, 운영이 data/에 받아둔 부속 파일이
# 있으면 같이 맞춰준다(빌드에는 영향 없음).
[ -f "$PROD/data/krpg_v22.json" ] && cp -n "$PROD/data/krpg_v22.json" "$DEV/data/" 2>/dev/null || true

# ── 2. 개발용 .env ────────────────────────────────────────────────
say "2/5  개발용 .env 생성"
if [ ! -f "$DEV/.env" ]; then
    # 운영 값을 그대로 가져오되 환경을 가르는 키는 걷어내고 새로 쓴다.
    grep -vE '^(APP_ENV|CONTAINER_NAME|HOST_PORT|BACKUP_ENABLED|SECRET_KEY)=' \
        "$PROD/.env" > "$DEV/.env"
    # 세션 서명키는 운영과 다른 값으로. 쿠키 이름도 갈라져 있지만(app.py),
    # 키까지 다르면 개발에서 만든 토큰이 운영에서 절대 통하지 않는다.
    NEWKEY=$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')
    {
        echo "SECRET_KEY=$NEWKEY"
        echo
        echo "# ── 개발 환경 표식 (운영본에는 없어야 한다) ──"
        echo "APP_ENV=dev"
        echo "CONTAINER_NAME=bokju-crm-dev"
        echo "HOST_PORT=8004"
        echo "BACKUP_ENABLED=0        # 개발본은 언제든 운영에서 다시 받아오면 된다"
    } >> "$DEV/.env"
    echo "   새로 만들었습니다."
else
    echo "   이미 있어 건드리지 않습니다: $DEV/.env"
fi
chmod 600 "$DEV/.env"

# ── 3. 운영 DB → 개발 DB ──────────────────────────────────────────
# cp는 안 된다. 운영이 WAL 모드로 돌고 있어 파일만 복사하면 반쪽짜리가 된다.
# SQLite backup API는 운영 컨테이너를 멈추지 않고 일관된 스냅샷을 뜬다.
say "3/5  운영 DB 스냅샷 → 개발 DB"
if [ -f "$DEV/data/bokju.db" ]; then
    echo "   개발 DB가 이미 있습니다. 덮어쓰지 않습니다."
    echo "   최신 운영 데이터로 다시 받으려면: sudo /root/bokju-refresh-dev-db.sh"
else
    docker exec bokju-crm python -c "
import sqlite3
src = sqlite3.connect('/data/bokju.db')
dst = sqlite3.connect('/data/_dev_snapshot.db')
src.backup(dst); dst.close(); src.close()"
    mv "$PROD/data/_dev_snapshot.db" "$DEV/data/bokju.db"
    echo "   상담 $(sqlite3 "$DEV/data/bokju.db" 'SELECT COUNT(*) FROM consultations')건 복사됨"
fi

# ── 4. 배포 스크립트 3종 ──────────────────────────────────────────
say "4/5  배포 스크립트 설치"

# (a) 운영 배포 — 기존 스크립트에 '배포 전 DB 스냅샷'을 더한 판.
if [ -f /root/bokju-deploy.sh ] && [ ! -f /root/bokju-deploy.sh.orig ]; then
    cp /root/bokju-deploy.sh /root/bokju-deploy.sh.orig
    echo "   기존 운영 스크립트를 /root/bokju-deploy.sh.orig 로 보관"
fi
cat > /root/bokju-deploy.sh <<'PROD_EOF'
#!/usr/bin/env bash
# 운영 배포 — main 브랜치를 받아 재빌드한다.
# 실행하면 컨테이너가 재기동되므로(상담사 화면이 잠깐 끊긴다) 상태 확인용으로
# 돌리지 말 것. 살아있는지만 볼 때는  curl http://172.16.1.250:8003/login
set -euo pipefail
cd /volume1/docker/bokju-crm

# 배포 전 스냅샷. 스키마 변경이 잘못돼도 여기로 되돌릴 수 있다.
# WAL 때문에 cp가 아니라 SQLite backup API를 쓴다.
STAMP=$(date +%Y%m%d-%H%M%S)
if docker ps --format '{{.Names}}' | grep -qx bokju-crm; then
    docker exec bokju-crm python -c "
import sqlite3
src = sqlite3.connect('/data/bokju.db')
dst = sqlite3.connect('/backups/predeploy-${STAMP}.db')
src.backup(dst); dst.close(); src.close()"
    echo "배포 전 백업: backups/predeploy-${STAMP}.db"
fi

git pull
docker-compose up -d --build

# 배포 전 백업은 최근 10개만 남긴다(자동 일일백업과 별개).
ls -1t backups/predeploy-*.db 2>/dev/null | tail -n +11 | xargs -r rm -f

sleep 5
docker ps --filter name=bokju-crm --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
docker logs --tail 30 bokju-crm
PROD_EOF

# (b) 개발 배포 — dev 브랜치. 개발본은 버려도 되는 사본이라 reset --hard.
cat > /root/bokju-deploy-dev.sh <<'DEV_EOF'
#!/usr/bin/env bash
# 개발 배포 — dev 브랜치를 받아 개발 컨테이너(8004)만 재빌드한다.
# 운영(8003)은 전혀 건드리지 않는다.
set -euo pipefail
cd /volume1/docker/bokju-crm-dev

git fetch origin
git checkout -B dev origin/dev
git reset --hard origin/dev

docker-compose up -d --build

sleep 5
docker ps --filter name=bokju-crm-dev --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
docker logs --tail 30 bokju-crm-dev
DEV_EOF

# (c) 개발 DB를 운영 최신본으로 다시 채우기
cat > /root/bokju-refresh-dev-db.sh <<'REFRESH_EOF'
#!/usr/bin/env bash
# 개발 DB를 운영 최신 데이터로 갈아끼운다. 개발 DB의 기존 내용은 버려진다.
# 운영은 멈추지 않는다(SQLite backup API).
set -euo pipefail
PROD=/volume1/docker/bokju-crm
DEV=/volume1/docker/bokju-crm-dev

docker exec bokju-crm python -c "
import sqlite3
src = sqlite3.connect('/data/bokju.db')
dst = sqlite3.connect('/data/_dev_snapshot.db')
src.backup(dst); dst.close(); src.close()"

# 개발 컨테이너가 DB를 연 채로 파일을 바꾸면 깨진다. 잠깐 멈춘다.
docker stop bokju-crm-dev >/dev/null 2>&1 || true
rm -f "$DEV/data/bokju.db" "$DEV/data/bokju.db-wal" "$DEV/data/bokju.db-shm"
mv "$PROD/data/_dev_snapshot.db" "$DEV/data/bokju.db"
docker start bokju-crm-dev >/dev/null 2>&1 || true

echo "개발 DB 갱신 완료 — 상담 $(sqlite3 "$DEV/data/bokju.db" 'SELECT COUNT(*) FROM consultations')건"
REFRESH_EOF

chown root:root /root/bokju-deploy.sh /root/bokju-deploy-dev.sh /root/bokju-refresh-dev-db.sh
chmod 700 /root/bokju-deploy.sh /root/bokju-deploy-dev.sh /root/bokju-refresh-dev-db.sh

# sudoers — 이 3개만 비밀번호 없이. 스크립트가 root:700이라 admin이 내용을
# 바꿔치기할 수 없으므로 임의 root 실행으로 번지지 않는다.
# DSM에는 visudo가 없어 문법 검사는 아래 sudo -n -l 로 대신한다.
cat > /etc/sudoers.d/bokju-deploy <<'SUDO_EOF'
admin ALL=(root) NOPASSWD: /root/bokju-deploy.sh, /root/bokju-deploy-dev.sh, /root/bokju-refresh-dev-db.sh
SUDO_EOF
chmod 440 /etc/sudoers.d/bokju-deploy
echo "   sudoers 등록 확인:"
sudo -n -u "$DEPLOY_USER" -l 2>/dev/null | grep -i bokju || \
    su - "$DEPLOY_USER" -c 'sudo -n -l' 2>/dev/null | grep -i bokju || \
    echo "   (확인 실패 — PC에서 ssh bokju-nas 'sudo -n -l' 로 직접 확인하세요)"

# ── 5. 개발 컨테이너 기동 ─────────────────────────────────────────
say "5/5  개발 컨테이너 빌드·기동 (첫 빌드는 5~10분)"
cd "$DEV"
docker-compose up -d --build

sleep 8
docker ps --filter name=bokju-crm --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
echo
echo "완료."
echo "  운영: http://172.16.1.250:8003   (main 브랜치)"
echo "  개발: http://172.16.1.250:8004   (dev 브랜치, 화면 상단에 빨간 띠)"

# ── 되돌리기 ──────────────────────────────────────────────────────
#   docker stop bokju-crm-dev && docker rm bokju-crm-dev
#   rm -rf /volume1/docker/bokju-crm-dev
#   mv /root/bokju-deploy.sh.orig /root/bokju-deploy.sh      # 백업 없던 판으로
#   rm -f /root/bokju-deploy-dev.sh /root/bokju-refresh-dev-db.sh
#   그리고 /etc/sudoers.d/bokju-deploy 를 원래 한 줄로 되돌린다:
#   admin ALL=(root) NOPASSWD: /root/bokju-deploy.sh
