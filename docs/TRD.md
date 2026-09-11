# 복주 상담실 CRM — TRD (기술 요구사항 정의서)

- 문서 버전: 1.0 (2026-09-10 현재 구현 기준 역작성)
- 대상 제품: bokju-crm v1.3.2
- 관련 문서: [docs/PRD.md](PRD.md)

---

## 1. 아키텍처 개요

```
[상담사 PC 브라우저] ──사내망 HTTP──> [시놀로지 NAS DS720+]
                                        └ Docker: bokju-crm 컨테이너 (:8003)
                                            ├ serve.py (waitress WSGI, 8 threads)
                                            ├ Flask app (app.py + Blueprints)
                                            ├ SQLite /data/bokju.db (WAL)   ← 볼륨 마운트
                                            ├ backup 스레드 → /backups      ← 볼륨 마운트
                                            └ IMAP 폴링 워커 (homepage_inbox)
                                        
[카카오 오픈빌더] ─┐
[홈페이지 서버]   ─┴─ 역프록시로 /api/webhook/* 만 외부 노출
[Claude API]      ←── 아웃바운드 (통계 인사이트 / 상담일지 AI 채움)
```

**설계 원칙**
1. **단일 프로세스, 단일 파일 DB** — 상담사 4명 규모에 분산 아키텍처는 불필요. 운영 부담 최소화가 우선.
2. **파일이 아니라 화면을 공유** — SQLite WAL은 SMB에서 손상되므로, 공유폴더의 DB 파일을 여러 PC가 여는 구조를 금지하고 컨테이너 1개만 DB를 연다.
3. **외부 노출 최소화** — 인바운드 경로는 `/api/webhook/*` 뿐. 나머지는 사내망 전용.
4. **의존성 최소화** — ORM·프론트 프레임워크·메시지 큐 없이 표준 라이브러리와 Flask/Jinja로 구성.

---

## 2. 기술 스택

| 레이어 | 채택 | 비고 |
|---|---|---|
| 언어 | Python 3.x | |
| 웹 프레임워크 | Flask ≥ 3.1 | Blueprint 3개(partners, support, 그 외 app 직속) |
| 템플릿 | Jinja2 (Flask 내장) | 서버 사이드 렌더링, SPA 아님 |
| WSGI (운영) | waitress ≥ 3.0 (`serve.py`) | Flask 개발서버 미사용 |
| DB | SQLite (WAL 모드) | ORM 없음. `sqlite3` + `Row` |
| 인증 | `werkzeug.security` 해시 + Flask 세션 | |
| 스케줄러 | `schedule` ≥ 1.2 | 백업 데몬 스레드 |
| 엑셀 | `openpyxl` ≥ 3.1 | 마이그레이션·심평원 명부 적재 |
| 달력 | `korean-lunar-calendar`, `holidays` | 음력·법정공휴일 |
| HTTP 클라이언트 | `requests` | Claude API 호출 |
| 프론트 | Vanilla JS + Chart.js v4 + html2canvas | 빌드 단계 없음 |
| 폰트/디자인 | Pretendard, `static/css/style.css` | |
| 컨테이너 | Docker + docker-compose (`version: "3.4"`) | 구형 DSM 호환 하한선 |

**빌드 단계가 없다** — 템플릿·JS·CSS를 그대로 서빙한다. 배포는 코드 복사 + 컨테이너 재빌드뿐.

---

## 3. 모듈 구조

| 파일 | 역할 | 규모 |
|---|---|---|
| `app.py` | Flask 진입점, 라우트 98개, before_request 권한 판정 | 약 259KB |
| `models.py` | 스키마 정의 + 마이그레이션(`_ensure_columns`) + 조회/집계/직렬화 | 약 253KB |
| `config.py` | 상수 — 보험·시도/시군구·병명 LAYOUT·입원경로·권한·주치의·상담사 | 약 33KB |
| `auth.py` | 로그인, `@login_required` / `@menu_required` / `@admin_required` | |
| `partnerships.py` | Blueprint `/partners` — 기관협력, 라우트 11개 | 약 56KB |
| `llm.py` | Claude API — `summarize_monthly`, `extract_consultation` | |
| `support_requests.py` | Blueprint `/support` — 개선 요청·문의 (자체 스키마·CSRF) | |
| `release_notes.py` | 버전별 사용자 안내를 공지사항에 1회 게시 | |
| `homepage_inbox.py` | IMAP 폴링 → communications(웹문의/in) 브릿지 | |
| `backup.py` | 기동 시 1회 + 매일 03시 온라인 백업, 보관기간 경과분 정리 | |
| `sms.py` | 문자 게이트웨이 자리 (현재 manual 모드) | |
| `serve.py` | 운영 진입점 — waitress | |
| `tools/` | 엑셀 마이그레이션, 심평원 명부·상세 적재, KRPG JSON 빌드, 레거시 보정 | 7개 스크립트 |
| `tests/` | `test_partnerships.py`, `test_release_notes.py`, `test_support_requests.py` | unittest |

**규모 요약** — Python 14,032줄 / 템플릿 37개 10,813줄 / 라우트 109개 / 테이블 37개 / 커밋 170개.

### 3.1 초기화 순서 (`app.initialize()`)
1. `models.init_db()` — 테이블 생성 + `_ensure_columns` 마이그레이션 + 인덱스
2. `partnerships.init_schema()` — `cooperation_*` 테이블
3. `support_requests.init_schema()` — `support_requests`, `support_replies`
4. `release_notes.publish_release_notes()` — 미게시 버전 공지 삽입 (`BEGIN IMMEDIATE`로 다중 프로세스 중복 방지)
5. `config.SEED_USERS` 6계정 보장, `admin`은 매 부팅 시 `APP_PASSWORD`로 동기화
6. 백업 스레드 · IMAP 워커 기동

---

## 4. 데이터 모델

### 4.1 테이블 분류 (37개)

**핵심 도메인**
| 테이블 | 설명 |
|---|---|
| `patients` | 환자 마스터. 이름+연락처 자동 매칭. 신원·보호자·보험유형·생애주기 단계·블랙리스트 |
| `consultations` | 상담 1건 (1환자 N상담). 폼 8섹션 전 필드 |
| `admission_episodes` | 입원 에피소드 — `status`(waiting/…), 대기 시작·입원예정·실제 입원·퇴원일·병실·퇴원 사유 |
| `admission_events` | 입원 기간 중 외부 의료기관 이용(응급전원/모병원 외래치료/복귀/기타). 出/歸 페어링 |
| `lifecycle_events` | 생애주기 이벤트 타임라인 |
| `attachments`, `patient_documents` | 첨부·문서(OCR 텍스트·AI 요약·상태 필드 준비됨) |

**마스터**
| 테이블 | 설명 |
|---|---|
| `source_hospitals`, `source_nursing_homes` | 모병원/요양원 정식명 마스터. 자유 텍스트 저장 금지의 근거 |
| `diagnoses` | 진단명 자동완성 마스터 |
| `cooperation_facility_directory` | 심평원 2026.3 명부 39,490곳. 공식기관코드로 식별, 동명 기관은 주소별 보존 |

**기관협력** — `cooperation_partners`, `cooperation_contacts`, `cooperation_activities`, `cooperation_tasks`, `cooperation_visit_plans`, `cooperation_documents`, `cooperation_agreements`, `cooperation_candidate_skips`

**커뮤니케이션** — `communications`, `sms_log`, `sms_templates`

**업무 보조** — `todos`, `todo_shares`, `todo_notifications`, `quick_filters`, `consultation_drafts`

**운영·관리** — `users`, `audit_log`, `announcements`, `announcement_reads`, `release_announcements`, `support_requests`, `support_replies`, `permission_requests`, `password_reset_requests`, `app_meta`

### 4.2 설계 규칙

- **JSON_FIELDS** — 다중 선택 항목(청력·활동·식사·상처·특수처치·치료·ARRANGE 등)은 TEXT 컬럼에 JSON으로 저장하고, 읽을 때 `_deserialize_consultation`이 자동 디시리얼라이즈한다. 정규화 대신 폼 1:1 매칭을 우선한 결정.
- **마이그레이션** — 별도 마이그레이션 프레임워크 없이 `models._ensure_columns(conn, table, {col: ddl})`로 부팅 시 멱등 적용. 컬럼 삭제는 하지 않는다(예: 폐지된 `admission_type`은 값 보존, UI에서만 제거).
- **시각 처리** — `created_at`은 SQLite `CURRENT_TIMESTAMP` = **UTC**. 표시 시 `datetime(created_at,'localtime')`, 기간 필터는 `datetime(?, 'utc')`로 변환해 비교(인덱스 유지). 컨테이너 TZ는 `Asia/Seoul`.
- **인덱스** — `idx_todos_user_date`, `idx_patients_lifecycle`, `idx_cons_patient_recent`, `idx_cons_patient_doctor`, `idx_episode_patient/status`, `idx_doc_patient/status`, `idx_todo_shares_user`, `idx_todo_notif_user`, `support_user_updated` 등.
- **집계 기준의 명시적 분리** — 상담 집계는 `consult_date`, 입원 집계는 **실제 입원일**. 입원일 미확정은 기간 집계에서 제외하고 별도 표시. `referrer_institution`(실제 연계)과 `source_hospital`(이전 병원)을 혼용하지 않는다.

---

## 5. 인증·인가 구현

### 5.1 인증
- 세션 기반. `SESSION_HOURS`(기본 4시간) 후 자동 로그아웃
- 로그인 5회 실패 시 5분 잠금
- 아이디 기억 / 자동 로그인 쿠키(`AUTO_LOGIN_DAYS`, 기본 30일) — 비밀번호 변경·계정 비활성화·로그아웃 시 무효화
- 비밀번호 초기화는 사용자가 요청(`password_reset_requests`) → 관리자가 승인·처리
- `admin` break-glass 계정은 매 부팅 시 `.env`의 `APP_PASSWORD`로 재동기화

### 5.2 인가 (2단 방어)
1. **일괄 차단** — `app._route_requirement(path, method)`가 경로 → 메뉴 → 필요 레벨을 결정하고, `app._enforce_menu_permissions`(before_request)가 API는 403, 화면은 403/되돌림 처리
2. **라우트 단위 데코레이터** — `auth.admin_required`(= users 메뉴 수정 이상) 병용
3. **UI 숨김** — `<body>`에 `cc-consult` / `cw-consult` / `cw-ward` / `cc-sms` 클래스를 권한에 따라 부여하고 `body:not(.…) 셀렉터{display:none}`로 쓰기 버튼 비표시 (표시 제어일 뿐, 실제 차단은 서버가 한다)

`permissions`가 비어 있는 기존 계정은 `models._hydrate_user`가 역할 프리셋으로 자동 판정한다. 새 메뉴 키가 추가되면 세션에 키가 없을 때 DB에서 권한을 다시 로드한다.

### 5.3 CSRF
`support_requests`는 `secrets.token_hex(32)` 세션 토큰 + `secrets.compare_digest` 검증(`check_csrf`).

---

## 6. 외부 연동

### 6.1 인바운드 웹훅
| 엔드포인트 | 용도 | 인증 |
|---|---|---|
| `POST /api/webhook/kakao` | 범용 카카오 푸시 | `KAKAO_WEBHOOK_TOKEN` |
| `POST /api/webhook/kakao/skill` | 오픈빌더 상담신청 폼 → SkillResponse v2.0 응답 | 동일 (쿼리 `?token=` 또는 헤더) |
| `POST /api/webhook/homepage` | 홈페이지 서버-투-서버 문의 | `HOMEPAGE_WEBHOOK_TOKEN` |

**`_webhook_guard` 공통 처리** — 상수시간 토큰 비교(`hmac.compare_digest`), 선택적 IP 화이트리스트(`WEBHOOK_ALLOW_IPS`), 16KB 본문 크기 제한, IP당 rate limit 60/분, 감사 로그(식별정보 평문 미기록). **토큰이 비어 있으면 503으로 비활성** — 기본이 안전한 쪽.

오픈빌더 필드는 별칭 매핑(`_KAKAO_FIELDS`)으로 흡수하고, 연락처는 `_norm_phone`으로 정규화해 환자 자동 매칭에 사용한다.

### 6.2 메일 브릿지 (`homepage_inbox.py`)
빌더형 홈페이지(카페24·아임웹)는 서버 코드를 못 건드려 웹훅 푸시가 불가하다. 대신 빌더의 '새 문의 관리자 메일 알림'을 전용 메일함으로 받고, 사내망 워커가 IMAP 폴링(`IMAP_*`, 기본 120초)해 `communications(웹문의/in)`로 등록한다. **아웃바운드 구조라 외부 포트 개방이 필요 없다**(역프록시보다 안전). 설정이 없으면 조용히 비활성.

### 6.3 Claude API (`llm.py`)
- 엔드포인트 `https://api.anthropic.com/v1/messages`
- `summarize_monthly(data)` — 월간보고서 임원 요약 1단락
- `extract_consultation(memo, enums=...)` — 통화 메모 → 상담일지 필드. 모병원·추천기관은 **마스터 대조 방식**으로만 채운다
- `_parse_json_object` — 응답 JSON 검증. 모델은 `CLAUDE_MODEL_INSIGHT`(기본 `claude-sonnet-5`)
- 전송 데이터는 처리 목적에 필요한 최소한으로 제한

### 6.4 SMS
`sms.py` — 발송사 무관 공통부(EUC-KR 바이트 기준 SMS 90/LMS 2000 자동 판별, 휴대폰 번호 정규화, `SMS_TEST_TO` 테스트 전환)와 발송사 어댑터(`_PROVIDERS`, 현재 `aligo`). `.env`에 `SMS_PROVIDER/SMS_API_KEY/SMS_SENDER`가 없으면 `manual` 모드(발송 이력 기록 + `sms:` 링크). 다른 발송사는 `(receiver, body, msg_type, title) → {ok, error, provider_msg_id}` 함수 하나를 `_PROVIDERS`에 등록하면 된다. `sms_log`에 `msg_type/provider/provider_msg_id/sent_to/error`를 남긴다(status: manual/sent/test/failed).

---

## 7. 배포·운영

### 7.1 컨테이너
```yaml
version: "3.4"            # 구형 DSM의 docker-compose 1.x는 이 키가 없으면 v1로 오독
services:
  bokju-crm:
    build: .
    ports: ["8003:8003"]
    restart: unless-stopped
    volumes: ["./data:/data", "./backups:/backups"]
    environment: { TZ: Asia/Seoul, BOKJU_DB_PATH: /data/bokju.db, BACKUP_DIR: /backups }
    healthcheck: interval 60s / timeout 10s / retries 3 / start_period 30s
```
- `3.4`를 쓰는 이유 — `healthcheck.start_period`가 3.4부터 지원되고, 3.4는 docker-compose 1.16+에서 모두 읽힌다. 구형 DSM에서 안전한 하한선.
- Dockerfile은 `COPY *.py ./` + `COPY data/ ./data/` — 모듈을 추가해도 COPY 목록을 다시 손댈 필요가 없게 했다(과거 누락으로 컨테이너가 즉사한 이력).

### 7.2 배포 환경
| 항목 | 값 |
|---|---|
| 호스트 | 시놀로지 DS720+, DSM 7.0/7.1 계열, RAM 10GB |
| Docker | 20.10.3-0554 / docker-compose 1.28.5 |
| 경로 | `/volume1/docker/bokju-crm` |
| 접속 | `http://172.16.1.250:8003` (사내망) |

**제약** — DSM 7.2+의 Container Manager '프로젝트' 메뉴가 없어 compose를 UI로 올릴 수 없다. **배포는 반드시 SSH.**

```bash
ssh admin@172.16.1.250 && sudo -i
cd /volume1/docker/bokju-crm && git pull && docker-compose up -d --build
docker logs --tail 30 bokju-crm
```

**코드 수정은 파일 복사만으로 반영되지 않는다** — 코드는 빌드 시점에 이미지로 들어가므로 `--build` 재빌드가 필수.
반영 확인: `docker exec bokju-crm python -c "from config import COUNSELORS; print(len(COUNSELORS))"`.

`data/`·`backups/`는 마운트 볼륨이라 재빌드해도 보존된다. 절차 전문은 [docs/DEPLOY-NAS.md](DEPLOY-NAS.md).

### 7.3 개발 환경
```bash
pip install -r requirements.txt
cp .env.example .env          # APP_PASSWORD, SECRET_KEY 입력
python app.py                 # 개발 http://127.0.0.1:8003 (FLASK_DEBUG=1)
python serve.py               # 운영 진입점과 동일
```
- 저장소는 `Z:\web\bokju-crm` (NAS `\\172.16.1.250\미전실` 매핑), 원격 `github.com/sjh3568-cpu/bokju-crm`
- PC·노트북 2대에서 개발. 시작 시 `git pull`, 종료 시 commit & push
- 템플릿 변경은 즉시 반영(Jinja 자동 리로드), `config.py`·`models.py` 변경은 재시작 필요
- `dev.sh` — 개발 중 자동 반영 실행 스크립트

### 7.4 백업
- `backup.py` — 기동 시 1회 + 매일 `BACKUP_HOUR`(기본 3시), `BACKUP_KEEP_DAYS`(기본 30일) 경과분 자동 삭제
- SQLite **온라인 백업 API** 사용 → 무중단
- 엑셀 import 직전 자동 백업 (경로는 `models.DB_PATH` 기준이라 컨테이너에서도 정합)
- 관리자 화면에서 즉시 백업 실행·검증 가능
- 주 1회 NAS/USB 외부 보관 권장

### 7.5 데이터 마이그레이션 도구
```bash
python tools/excel_import.py "uploads/상담내역 종합.xlsx" --sheet 26.5   # dry-run 리포트
python tools/excel_import.py ... --apply                                  # 실제 적재(직전 백업)
python tools/excel_import.py ... --all                                    # 전 시트
```
시트 스키마 자동 감지(A=구식 2023.8~2025.4 / B=중간 / C=신식+요일) + outlier 보정. fuzzy 매칭은 표준 라이브러리 `difflib`(rapidfuzz 미사용).

기타: `import_hospital_master.py`, `import_nursing_master.py`, `import_cooperation_facility_details.py`(심평원 시설·진료과목·특수진료 XLSX를 기관코드로 연결), `build_krpg_data.py`(KRPG 2.2 3개 시트 → JSON), `fix_legacy_data.py`, `fix_admission_purpose.py`.

---

## 8. 설정 (.env)

| 키 | 기본 | 설명 |
|---|---|---|
| `APP_PASSWORD` | — | admin break-glass 초기 비밀번호 (필수) |
| `SECRET_KEY` | — | Flask 세션 서명 키, 64바이트 랜덤 (필수) |
| `PORT` | 8003 | cafe-helper 8001 / keyword-monitor 8002와 충돌 회피 |
| `BOKJU_DB_PATH` | 코드 폴더 옆 `bokju.db` | 컨테이너는 `/data/bokju.db`. **SMB 경로 금지** |
| `BOKJU_DB_TIMEOUT` | 30 | 동시 저장 대기 한도(초) |
| `WAITRESS_THREADS` | 8 | 운영 서버 스레드 수 |
| `ALLOW_LAN` | 0 | `python app.py` 개발서버 전용. 컨테이너는 serve.py가 0.0.0.0 바인딩 |
| `SESSION_HOURS` | 4 | 자동 로그아웃 |
| `AUTO_LOGIN_DAYS` | 30 | 자동 로그인 쿠키 유지 |
| `BACKUP_ENABLED` / `BACKUP_HOUR` / `BACKUP_KEEP_DAYS` / `BACKUP_DIR` | 1 / 3 / 30 / `./backups` | 자동 백업 |
| `INBOX_ENABLED` | 0 | 통합 인박스 표시. 0이면 메뉴·검색에서 제거되고 라우트 404 |
| `KAKAO_WEBHOOK_TOKEN` / `HOMEPAGE_WEBHOOK_TOKEN` | 미설정 | 비면 해당 채널 503 |
| `WEBHOOK_ALLOW_IPS` | 미설정 | 쉼표 구분 화이트리스트 |
| `IMAP_HOST/PORT/USER/PASS/FOLDER/POLL_SECONDS/FROM_FILTER` | 미설정 | 셋 이상 설정돼야 브릿지 활성 |
| `SMS_PROVIDER` / `SMS_API_KEY` / `SMS_API_USER` / `SMS_SENDER` | 미설정 | 비면 manual 모드. 발송사 `aligo` 지원 |
| `SMS_TEST_TO` | 미설정 | 채우면 모든 문자가 이 번호로만 발송(연동 검증용) |
| `CLAUDE_MODEL_INSIGHT` | `claude-sonnet-5` | 구조화 출력 지원 모델이어야 함 |

---

## 9. 테스트·검증

```bash
.venv-linux/bin/python -m unittest discover -s tests -v
```
- 임시 DB 사용, **실제 환자 자료를 쓰지 않는다**
- 현재 커버리지 — `partnerships`(기관협력 집계·후속일정), `release_notes`(버전 1회 게시 멱등성), `support_requests`(권한·CSRF)
- 헬스체크 — `GET /healthz` → `{"ok": true}`, 컨테이너 healthcheck는 `/login` 200 확인
- 운영 상태 점검은 `curl http://172.16.1.250:8003/login` (배포 스크립트는 컨테이너를 재기동하므로 상태 확인 용도로 실행하지 않는다)

---

## 10. 기술 부채·알려진 제약

| 항목 | 내용 | 영향 / 대응 |
|---|---|---|
| `app.py`·`models.py` 비대 | 각각 250KB 이상 단일 파일 | 도메인별 Blueprint/모듈 분리 여지. 현재는 검색 가능성 유지로 감내 |
| 테스트 커버리지 | 3개 모듈만 | 상담 저장·통계 집계 회귀 테스트 부재. 폼 필드 변경 시 수동 확인 의존 |
| SQLite 단일 파일 | 동시 쓰기 확장 한계 | 4명 규모에서는 충분. 기관 확대 시 재검토 항목 |
| 컨테이너 CRLF | Windows에서 복사된 파일이 리눅스 git에서 변경으로 인식 | 최초 1회 `git reset --hard origin/main`으로 LF 재기록 후 해소됨 |
| sudoers·SSH 키 무인 배포 | DSM 업데이트 시 초기화될 수 있음 | `/root/bokju-deploy.sh` + `/etc/sudoers.d/bokju-deploy` 재설치 필요 |
| 폐지 컬럼 잔존 | `admission_type` 등 | 값 보존 정책상 의도된 것 |
| 벤더 JS 번들 | Chart.js·html2canvas를 저장소에 포함 | 오프라인 사내망 전제이므로 CDN 미사용(의도) |

---

## 11. 변경 관리

- `config.APP_VERSION`을 올릴 때 `release_notes.RELEASE_NOTES`에 **해당 버전의 사용자 안내를 반드시 추가**한다(없으면 부팅 시 `ValueError`로 실패한다 — 안내 누락 방지 장치).
- 새 감사 action을 추가하면 `config.AUDIT_ACTION_LABELS`에 라벨을 추가한다.
- 모듈을 추가해도 Dockerfile 수정은 불필요(`COPY *.py`), 단 새 데이터 폴더를 쓰면 COPY 확인이 필요하다.
- 새 메뉴 추가 시 `config.MENUS` · `ROLE_PRESETS` · `_route_requirement` 3곳을 함께 갱신한다.
