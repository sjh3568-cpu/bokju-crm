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

## 대상 장비 — Synology DS720+

| 항목 | 사양 | 비고 |
|---|---|---|
| CPU | Intel Celeron J4125 4코어 (x86_64) | `python:3.12-slim` 이미지가 그대로 돌아간다 (ARM 에뮬레이션 불필요) |
| RAM | 2GB DDR4 (최대 6GB) | 앱 컨테이너는 약 150~200MB. 이 앱 하나면 충분 |
| 컨테이너 | **Docker 20.10.3-0554 설치 완료** (볼륨 1) | DSM 7.0/7.1 계열. 7.2+에서는 같은 것이 **Container Manager**로 이름만 바뀐다 |

상담사 4명 + SQLite 규모에서 J4125는 여유가 많다. 다만 **RAM 2GB에서 DSM이
이미 1GB 가까이 쓰므로**, 나중에 다른 컨테이너(STT·OCR 등)를 얹을 계획이면
SODIMM 슬롯에 4GB를 추가해 6GB로 올려두는 편이 낫다. 지금 이 앱만 돌린다면 그대로 가도 된다.

## 사전 준비

- **Docker 패키지 설치 완료** — 패키지 센터에서 확인함(20.10.3-0554, 볼륨 1).
  추가 설치 불필요. DSM 7.2+로 올리면 Container Manager로 표시된다
- NAS 고정 IP (예: `172.16.1.250`) — DHCP로 주소가 바뀌면 상담사 즐겨찾기가 끊긴다
- DSM **제어판 > 보안 > 방화벽**에서 사내망 대역의 TCP **8003** 인바운드 허용
- 저장소는 **로컬 볼륨** `/volume1/docker/...` 에 둘 것 (SMB 공유폴더 아님)

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

**Docker**(또는 Container Manager) 앱 → **프로젝트** → **생성**

구형 Docker 패키지(20.10.3)의 프로젝트 기능도 docker-compose를 지원한다. 단
`docker-compose.yml`에 `version` 키가 반드시 있어야 한다 — 없으면 구형(v1) 포맷으로
해석해 실패하므로 `version: "3.4"`를 명시해 두었다(start_period를 쓰려면 3.4가 하한선).

| 항목 | 값 |
|---|---|
| 프로젝트 이름 | `bokju-crm` |
| 경로 | `/volume1/docker/bokju-crm` |
| 소스 | **기존 docker-compose.yml 사용** |

**빌드** 후 시작. DS720+ 기준 첫 빌드는 파이썬 이미지 다운로드 + pip 설치로
5~10분쯤 걸린다. 이후 코드만 바뀐 빌드는 훨씬 빠르다.

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
구체적인 절차는 아래 '운영 배포' 참조.

## 운영 배포

개발은 노트북(WSL)에서, 운영은 이 NAS에서 돈다. **운영은 `main`을 그대로 받지 않고
노트북에서 확정한 태그만 받는다.** 지금 떠 있는 게 어느 버전인지 알 수 있어야
문제가 생겼을 때 되돌릴 지점을 찾을 수 있다.

```
노트북(개발)  코드 수정 → ./dev.sh 로 확인 → 커밋
                ↓ ./release.sh  (테스트·안내 확인 후 태그 생성)
GitHub        태그 v1.4.0
                ↓ 아래 절차
NAS(운영)     그 태그의 파일로 교체 → 빌드 → 재시작
```

### 1. 노트북에서 버전 확정

```bash
./release.sh          # 검사 5종 통과해야 태그가 찍힌다
```

검사 항목 — 커밋 누락, 버전 중복, **해당 버전의 상담사 안내 작성 여부**, 테스트 통과.
버전을 올릴 때는 `config.py`의 `APP_VERSION`과 `release_notes.py`의 안내를 함께 고친다.
안내는 서버가 뜰 때 공지사항에 자동 게시되므로, 상담사가 변경 이유를 화면에서 알 수 있다.

### 2. 배포 시각

**상담 업무 시간(09:00~18:00)을 피한다.** 컨테이너 재시작은 수 초지만,
하필 상담사가 저장하는 순간이면 그 입력이 유실될 수 있다.

### 3. NAS에 적용

1. **DB 백업 먼저** — Container Manager에서 프로젝트를 중지하지 말고,
   File Station에서 `data/bokju.db`를 `backups/manual_배포전_YYYYMMDD.db`로 복사.
   (기동 시 자동 백업도 남지만, 되돌릴 지점을 배포 직전으로 명확히 해두는 편이 낫다.)
2. **파일 교체** — 해당 태그의 저장소 파일을 `/volume1/docker/bokju-crm/`에 덮어쓴다.
   `.env`·`data/`·`backups/`는 건드리지 않는다.
3. **빌드 → 재시작** — Container Manager → 프로젝트 → 빌드 후 시작.
4. **확인** — `http://<NAS주소>:8003/login` 접속. 화면 아래 버전이 새 번호인지 본다.
   로그인 후 공지사항에 새 버전 안내가 올라와 있으면 정상이다.

### 4. 문제가 생기면 되돌린다

증상이 무엇이든 **먼저 되돌리고 원인은 나중에 찾는다.** 상담 업무가 우선이다.

1. 직전 태그의 파일로 다시 교체 → 빌드 → 재시작
2. 데이터까지 이상하면 프로젝트 중지 → 3‑1에서 만든 백업을 `data/bokju.db`로 복사 → 재시작

직전 태그는 `git tag --sort=-creatordate | sed -n 2p`로 확인한다.

### DB 구조 변경 시 주의

칸(컬럼)을 **추가**하는 변경은 기동 시 `_ensure_columns()`가 자동 처리하므로 안전하다.
칸을 없애거나 이름을 바꾸거나 기존 값을 변환하는 변경은 자동으로 되돌릴 수 없다.
그런 배포는 반드시 **백업 확인 → 업무 시간 외 → 배포 직후 데이터 건수 확인** 순으로 한다.

## 동시 사용

- `serve.py`가 waitress를 8스레드로 띄운다 (`WAITRESS_THREADS`). 4명 + 조회 계정에 충분.
- 쓰기는 SQLite가 직렬화하되 `busy_timeout` 30초 안에서 순서대로 처리되므로,
  같은 순간에 저장해도 실패하거나 유실되지 않는다 (`BOKJU_DB_TIMEOUT`).
- 대시보드·재원 화면은 30초마다 자동 새로고침된다. 입력 중이거나 계산기 모달이
  열려 있거나 탭이 백그라운드면 건너뛴다 — 작성 중인 내용은 날아가지 않는다.
- 상담 등록·수정 화면은 자동 새로고침하지 않는다. 저장 후 목록으로 돌아가면 최신 상태가 보인다.
