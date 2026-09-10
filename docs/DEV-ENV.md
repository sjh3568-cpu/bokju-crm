# 운영 / 개발 환경 분리

상담사 4명이 실제로 쓰기 시작한 뒤로는, 고치던 코드를 바로 운영에 올릴 수 없다.
잘못된 스키마 변경 한 번이면 실제 상담 기록이 망가진다. 그래서 **같은 코드의 두 환경**을 둔다.

코드를 두 벌로 나누지는 않는다. 저장소는 하나, **브랜치와 컨테이너와 DB만** 나뉜다.
코드가 갈라지면 "개발에선 됐는데 운영에선 안 되는" 상황이 반드시 생긴다.

| | 운영 (prod) | 개발 (dev) |
|---|---|---|
| 브랜치 | `main` | `dev` |
| NAS 경로 | `/volume1/docker/bokju-crm` | `/volume1/docker/bokju-crm-dev` |
| 컨테이너 | `bokju-crm` | `bokju-crm-dev` |
| 주소 | http://172.16.1.250:8003 | http://172.16.1.250:8004 |
| DB | 실데이터 | 운영 스냅샷 (별도 파일) |
| 자동 백업 | 매일 03시 | 끔 |
| 화면 | 평소와 같음 | 상단 **빨간 띠** + 탭 제목 `[개발]` |

두 컨테이너는 `docker-compose.yml` **한 파일**을 같이 쓴다. 컨테이너 이름과 포트를
각 디렉터리의 `.env`(`CONTAINER_NAME`·`HOST_PORT`)에서 정하고, 기본값이 운영이다.

## 왜 빨간 띠와 쿠키 분리가 필요한가

- **빨간 띠**: 8003과 8004는 화면이 똑같다. 표시가 없으면 실제 상담을 개발 화면에
  입력하는 사고가 난다. 그 내용은 운영 DB에 없으므로 그대로 사라진다.
- **쿠키 이름 분리**(`app.py`의 `IS_DEV`): 브라우저 쿠키는 **포트를 구분하지 않는다.**
  `172.16.1.250:8003`과 `:8004`는 같은 쿠키 저장소를 쓴다. 이름을 나누지 않으면
  개발 화면에 로그인하는 순간 운영 화면 로그인이 풀린다.

## 평소 작업 흐름

```
1. dev 브랜치에서 개발
   git checkout dev
   ... 코드 수정 ...
   git add -A && git commit && git push origin dev

2. 개발 환경에 배포하고 확인
   ssh bokju-nas 'sudo -n /root/bokju-deploy-dev.sh'
   → http://172.16.1.250:8004 에서 확인 (운영은 안 건드림)

3. 이상 없으면 운영에 반영
   git checkout main && git merge dev && git push origin main
   ssh bokju-nas 'sudo -n /root/bokju-deploy.sh'
   → http://172.16.1.250:8003
```

운영 배포 스크립트는 **재빌드 전에 DB 스냅샷을 먼저 뜬다**
(`backups/predeploy-<날짜시각>.db`, 최근 10개 보관). 배포 후 문제가 보이면 그 파일로 되돌린다.

## 개발 DB를 운영 최신본으로 다시 채우기

개발 DB는 만든 시점의 스냅샷이라 시간이 지나면 운영과 벌어진다. 마이그레이션을
검증하기 전에는 반드시 최신본으로 다시 받는다.

```bash
ssh bokju-nas 'sudo -n /root/bokju-refresh-dev-db.sh'
```

운영은 멈추지 않는다 — SQLite backup API로 스냅샷을 뜨기 때문이다.
**운영 DB를 `cp`로 복사하면 안 된다.** WAL 모드로 돌고 있어 반쪽짜리 파일이 나온다.

## 스키마 변경은 반드시 개발에서 먼저

이 앱의 마이그레이션은 `models.py`의 `init_db()`가 **앱 시작 시 자동 실행**한다.
즉 배포하는 순간 운영 DB에 적용된다. 되돌리는 기능은 없다.

- `_ensure_columns()` — `ALTER TABLE ADD COLUMN`만 한다. 비교적 안전.
- `_migrate_legacy_stages()`, `_migrate_pair_legacy_returns()`,
  `_migrate_admission_episodes()` — **기존 데이터를 고쳐 쓴다.** 잘못되면 복구가 어렵다.

절차: 개발 DB를 운영 최신본으로 갱신 → dev 배포 → 8004에서 **건수와 내용을 직접 확인** →
그 다음에 main 병합.

컬럼 이름을 바꾸는 변경은 특히 위험하다. `_ensure_columns()`는 컬럼을 **추가만** 하고
이름은 못 바꾸므로, 옛 DB는 옛 이름을 그대로 들고 있게 되어 코드와 영영 어긋난다.
(실제로 `Z:\web\bokju-crm\bokju.db`가 `password_reset_requests.created_at`인 채로 남아
현재 코드로는 열리지 않는다. 운영 DB는 `requested_at`으로 정상.)

## 최초 구축

`tools/nas-setup-dev-env.sh`가 위 구조를 통째로 만든다. NAS에서 root로 1회 실행한다.

```bash
ssh -t admin@172.16.1.250 'sudo bash /volume1/docker/bokju-setup-dev.sh'
```

되돌리는 방법은 그 스크립트 맨 아래 주석에 있다.

관련 문서: [DEPLOY-NAS.md](DEPLOY-NAS.md)
