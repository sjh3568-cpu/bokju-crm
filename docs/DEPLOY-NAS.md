# 시놀로지 NAS 배포 — 사내망 웹 접속

상담사 4명이 각자 PC 브라우저로 접속해 동시에 상담을 등록하는 구성.
NAS에서 앱 컨테이너 1개만 돌고, DB는 그 안에서만 열린다.

## 왜 이 구조인가

**NAS 공유폴더(SMB)에 있는 `bokju.db`를 여러 PC가 직접 여는 방식은 쓰면 안 된다.**
`models.get_db()`는 `PRAGMA journal_mode=WAL`을 켜는데, WAL은 `-shm` 공유메모리로
프로세스 간 잠금을 조율하므로 **네트워크 파일시스템에서 동작하지 않는다**(SQLite 공식 제약).
여러 PC가 SMB 너머로 같은 파일을 열면 서로의 잠금이 보이지 않아 조용히 덮어쓰거나
파일이 손상된다. 그래서 **파일이 아니라 화면을 공유**한다 — 앱은 한 곳에서만 돌린다.

같은 이유로 `BOKJU_DB_PATH`에 SMB 경로(`/volume1/미전실/...`를 마운트한 형태 포함)를
넣지 말 것. 반드시 NAS 로컬 볼륨(`/volume1/docker/...`)이어야 한다.

## 사전 준비

- 시놀로지 DSM 7.x + **Container Manager** 패키지 설치 (DS+ 계열)
- NAS 고정 IP (예: `172.16.1.250`)
- 방화벽에서 사내망 대역의 TCP **8003** 인바운드 허용

## 설치

### 1. 파일 올리기

File Station에서 `docker` 공유폴더 아래에 `bokju-crm` 폴더를 만들고,
이 저장소 전체를 복사한다.

```
/volume1/docker/bokju-crm/
├── Dockerfile
├── docker-compose.yml
├── app.py, models.py, serve.py, backup.py, ...
├── templates/, static/, tools/
├── .env          ← 직접 만든다 (아래)
├── data/         ← 자동 생성. DB가 여기 쌓인다
└── backups/      ← 자동 생성. 일일 백업
```

### 2. `.env` 작성

`.env.example`을 복사해 `.env`로 만들고 최소 두 줄을 채운다.

```ini
APP_PASSWORD=병원에서정한초기비밀번호
SECRET_KEY=<아래 명령으로 생성한 64자리>
```

`SECRET_KEY` 생성 (PC에서 한 번만):

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

`BOKJU_DB_PATH`·`BACKUP_DIR`은 `docker-compose.yml`이 덮어쓰므로 `.env`에서는 건드리지 않는다.

### 3. 컨테이너 생성

Container Manager → **프로젝트** → **생성**

| 항목 | 값 |
|---|---|
| 프로젝트 이름 | `bokju-crm` |
| 경로 | `/volume1/docker/bokju-crm` |
| 소스 | **기존 docker-compose.yml 사용** |

**빌드** 후 시작. 첫 빌드는 파이썬 이미지를 받느라 몇 분 걸린다.

### 4. 접속 확인

브라우저에서 `http://<NAS주소>:8003` — 예: `http://172.16.1.250:8003`

로그인 계정은 첫 기동 시 `config.SEED_USERS`대로 자동 생성된다
(어드민 + 상담사 4명 + 조회). **초기 비밀번호는 전원 `.env`의 `APP_PASSWORD`이므로,
접속 직후 어드민이 `/admin/users`에서 개인별 비밀번호로 반드시 변경할 것.**

상담사 PC 브라우저에 이 주소를 즐겨찾기/시작페이지로 걸어두면 된다.

## 기존 데이터 이전

이미 쓰던 `bokju.db`가 있으면 컨테이너를 **멈춘 상태에서** 옮긴다.

1. Container Manager에서 `bokju-crm` 프로젝트 중지
2. File Station으로 기존 `bokju.db`를 `/volume1/docker/bokju-crm/data/bokju.db`로 복사
   (`-wal`·`-shm` 파일이 같이 있으면 함께 복사)
3. 프로젝트 다시 시작 — 기동 시 `_ensure_columns()`가 누락 컬럼을 자동 추가한다

## 운영

**자동 백업** — 기동 직후 1회 + 매일 03시에 `backups/`로 스냅샷.
SQLite 온라인 백업 API를 쓰므로 상담사가 저장 중이어도 앱을 멈출 필요가 없다.
30일 지난 파일은 자동 삭제(최근 1개는 항상 보존). `.env`로 조정:

```ini
BACKUP_HOUR=3          # 백업 시각
BACKUP_KEEP_DAYS=30    # 보관 일수
BACKUP_ENABLED=1       # 0이면 끔
```

주 1회는 `backups/`를 USB나 다른 공유폴더로 복사해 **NAS 밖에도** 한 벌 둘 것.
NAS 자체가 고장나면 안에 있는 백업도 같이 사라진다.

**복구** — 프로젝트 중지 → `backups/bokju_daily_YYYYMMDD_HHMMSS.db`를
`data/bokju.db`로 복사 → 재시작.

**자동 재시작** — `restart: unless-stopped`라 NAS 재부팅·앱 오류 종료 시 알아서 다시 뜬다.

**로그** — Container Manager → 컨테이너 → `bokju-crm` → 로그.

**코드 업데이트** — 저장소 파일 갱신 후 프로젝트 **빌드 → 재시작**.
`data/`·`backups/`는 마운트 볼륨이라 이미지가 바뀌어도 그대로 남는다.

## 동시 사용

- `serve.py`가 waitress를 8스레드로 띄운다 (`WAITRESS_THREADS`). 4명 + 조회 계정에 충분.
- 쓰기는 SQLite가 직렬화하되 `busy_timeout` 30초 안에서 순서대로 처리되므로,
  같은 순간에 저장해도 실패하거나 유실되지 않는다 (`BOKJU_DB_TIMEOUT`).
- 대시보드·재원 화면은 30초마다 자동 새로고침된다. 입력 중이거나 계산기 모달이
  열려 있거나 탭이 백그라운드면 건너뛴다 — 작성 중인 내용은 날아가지 않는다.
- 상담 등록·수정 화면은 자동 새로고침하지 않는다. 저장 후 목록으로 돌아가면 최신 상태가 보인다.
