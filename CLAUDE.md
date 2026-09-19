# CLAUDE.md — 복주 상담실 CRM (bokju-crm)

> **이 파일에는 "앞으로도 계속 지켜야 할 규칙"만 둡니다.** 매 세션·매 에이전트 호출마다
> 통째로 실리기 때문입니다. 새 기능을 만들었으면 경위는 [docs/HISTORY.md](docs/HISTORY.md)에
> 적고, 그중 **다음에도 지켜야 할 것만** 이 파일로 올립니다.
> (2026-09-19에 57KB → 이 크기로 정리. 연대기는 HISTORY, 연동 상세는 INTEGRATIONS로 분리)
## 프로젝트 미션

**세계 어느 병원의 상담관리 프로그램보다 쉽고 편리하게.**

복주회복병원 상담실에서 상담사가 **편리하게 기입**하고, **편리하게 통계·결과·관리**할 수 있도록 AI를 활용해 업무를 개선한다. 통화 → 자동 텍스트화 → 양식 자동 채움 → 통계·인사이트까지 한 흐름으로.

## 운영 컨텍스트

- **운영 주체**: 인덕의료재단 — 복주회복병원(재활병원) / 요양병원 / 요양원 3개 기관 운영
- **현 적용**: 복주회복병원 상담실 (4명 상담사, 1일 10-20건, 평균 15분/통화)
- **병원 특성**: **입원 전용 재활병원** — 외래 환자 없음
  - 따라서 외래 기능(예약/NO SHOW/HappyCall/ARS 진료안내) 절대 추가 금지
- **전화 환경**: LG 헬로비전 IP폰 4대 (현재 통화 녹음은 헬로비전 사이트에서 수동 다운로드)
- **저장 인프라**: 시놀로지 NAS (사내망 공유 폴더)
- **메신저**: 병원 공식 카카오 비즈채널 보유

## 사용

```bash
cd c:\Developer\bokju-crm
pip install -r requirements.txt
cp .env.example .env       # APP_PASSWORD, SECRET_KEY 입력
python app.py              # 개발 — http://127.0.0.1:8003
python serve.py            # 운영 — waitress, 0.0.0.0:8003 (NAS 컨테이너 진입점)
```

계정: 최초 부팅 시 `config.SEED_USERS` 6명 자동 생성 (어드민·관리자 + 상담사 4명).
초기 비밀번호는 `.env`의 `APP_PASSWORD`, 이후 어드민이 `/admin/users`에서 개별 변경.
`admin`은 비번 분실 대비 break-glass 계정. 부팅 시 동기화하지 않으며(바꾼 비번·표시명 유지),
분실 시 `.env`에 `APP_PASSWORD_RESET=1`을 넣고 재기동하면 1회 `APP_PASSWORD`로 되돌린 뒤 플래그를 지운다.

**권한 — 계정별 메뉴 권한 매트릭스** (`users.permissions` JSON = `{menu: level}`):
- 단계형 레벨: **미현시(0) < 조회(1) < 수정(2) < 등록(3)**, 상위가 하위 포함 (`config.PERM_*`).
- 메뉴 7종 (`config.MENUS`): 대시보드·상담·재원 관리·문자·통계·월간보고서·사용자 관리.
  조회 전용 메뉴(대시보드·통계·월간보고서)는 최대 레벨이 '조회'.
- **역할**(admin/staff/viewer)은 이제 '권한 프리셋' 이름일 뿐 (`config.ROLE_PRESETS`) —
  계정 생성/역할 변경 시 매트릭스 기본값을 채우고, 이후 `사용자 관리` 화면에서 메뉴별로 조정.
  기존 계정은 `permissions`가 비어 있으면 역할 프리셋으로 자동 판정(`models._hydrate_user`).
- **판정**: `app._route_requirement(path, method)`가 경로→메뉴→필요레벨을 정하고(경로 기준이라 Blueprint 분리와 무관),
  `app._enforce_menu_permissions`(before_request)가 일괄 차단(API 403 / 화면 403·되돌림).
  세부 라우트 방어로 `auth.admin_required`(=users 메뉴 수정↑)도 병용.
- **UI 숨김**: `<body>`에 `cc-consult/cw-consult/cw-ward/cc-sms` 클래스를 권한에 따라 부여,
  style.css의 `body:not(.…) 셀렉터{display:none}`로 권한 없는 쓰기 버튼을 감춘다.
- `admin` break-glass 계정은 항상 전권(프리셋 admin) 고정, 화면에서 편집 불가.
- 안전장치: 사용자 관리 권한(users≥수정) 계정은 최소 1개 유지 (마지막 1개 삭제·강등·비활성 금지).

## 구조

```
app.py             Flask 앱 생성·공통 훅(권한 판정·필독 공지·gzip)·컨텍스트 프로세서·템플릿 필터·공용 도메인 헬퍼
                   (회복기/만료/퇴원 판정, 날짜 유틸) + 맨 아래에서 views/ Blueprint 등록. 라우트는 /api/global-search 하나만 남음
views/             화면·API 라우트 Blueprint (2026-09-15 app.py 7,400줄에서 분리). 엔드포인트는 "모듈.함수" (url_for("main.dashboard"))
  account.py       인증·계정 (login/logout/account/start-page/password-reset)      bp "account"
  notices.py       공지사항                                                          bp "notices"
  admin.py         사용자 관리·감사 로그·권한 요청                                     bp "admin"
  main.py          대시보드(/), 통합 달력, 주간 현황, healthz, help                    bp "main"
  todos.py         상담사 개인 To-Do                                                   bp "todos"
  stats.py         통계·보고서                                                        bp "stats"
  consult.py       상담일지 화면·목록·CSV·상담 CRUD API·결과/퇴원 워크플로·자동완성·기간계산기  bp "consult"
  ward.py          재원 관리·생애주기·입원 확정·호실·태그·블랙리스트                     bp "ward"
  inbound.py       옴니채널 인박스·홈페이지 게시판 답변·외진 이벤트 API·webhook          bp "inbound"
  sms_views.py     문자 발송                                                          bp "sms"
  documents.py     팩스·문서 자료함 (/documents, /api/documents/*)                        bp "documents"
                   규칙: 공용 헬퍼는 app.py에 두고 `from app import …`(app.py가 맨 아래에서 views를 import하므로 순환 없음).
                   views 모듈이 서로 쓰는 이름은 `from views.x import`(main→ward, consult→inbound·todos 방향만; 역방향 금지).
                   app.py에 남은 코드나 tests가 쓰는 views 이름은 app.py 맨 아래 재수출 블록에 추가.
                   `from app import X`는 값 복사라 tests에서 `patch.object(main, "X")`로는 views 코드에 안 먹는다 — views 모듈을 패치할 것
                   (tests/test_ward_census.py의 render_template, test_partnerships.py의 INBOX_ENABLED 참고).
models.py          SQLite 스키마 + 마이그레이션 (_ensure_columns) + JSON 직렬화
auth.py            인증 + @login_required / @menu_required / @admin_required (계정별 메뉴 권한)
config.py          상수 (보험·시도/시군구·병명 LAYOUT·입원경로 등)
dashboard_metrics.py 대시보드 KPI 보조 지표 — 지난주 같은 요일 비교·7일 스파크라인·병동별 재원·30일 입퇴원·요일 히트맵
templates/         base.html(좌측 사이드바 + 상단바), login, dashboard, consult_form/list/detail, patient_detail, error
static/css/        style.css (Pretendard, 4 그룹 박스, dx-stretch, 앱 셸=사이드바), dashboard.css (대시보드 전용 스킨)
static/js/         common.js, form.js (자동완성, 시군구→시도, 010 포맷, 콤보박스)
serve.py           운영 진입점 — waitress WSGI (개발용 app.run 대체)
backup.py          자동 백업 — 기동 시 1회 + 매일 03시, 보관기간 경과분 정리
homepage_inbox.py  홈페이지 문의 메일 브릿지 — IMAP 폴링 → communications(웹문의/in) 자동등록
homepage_board.py  홈페이지 상담게시판(bokjurh.co.kr) 연동 — 공개 목록 폴링 → 인박스, 인박스 '답변' → 관리자 화면에 답변 등록
fax_inbox.py       팩스 자료함 — NAS 수신 폴더 감시 → Claude 판독 → '날짜_이름_주병명' 정리 → patient_documents + 인박스(팩스)
Dockerfile         NAS Container Manager 배포용 이미지
docker-compose.yml NAS 프로젝트 정의 (볼륨·재시작·헬스체크)
bokju.db           SQLite (gitignore)
backups/           일일 자동 백업 (gitignore)
uploads/           마이그레이션·녹음 임시 (gitignore)
```

## 주요 라우트

| 경로 | 동작 |
|---|---|
| `GET /login` `POST /login` | 인증 |
| `GET /admin/users` (+ create/update/password/active/delete POST) | **사용자 관리** (users 메뉴 수정↑) — 계정 추가·비번·역할·**메뉴별 권한**·활성/삭제 |
| `GET /admin/audit` `GET /admin/audit/export` | **이력 관리** (users 메뉴 수정↑) — audit_log 열람·필터·CSV. 누가 언제 무엇을 조회/입력/수정/삭제했는지 |
| `GET /` | 대시보드 (이번달 카드 + 오늘 등록 + 7일 추이) |
| `GET /consult/new` `POST /api/consult` | 상담일지 등록 |
| `GET /consult/<id>` `GET /consult/<id>/edit` `POST /api/consult/<id>` | 상세 / 수정 |
| `GET /consultations` `GET /consultations.csv` | 목록 / CSV (admin) |
| `GET /consultations/inquiries` (+`.csv`) | **채널 문의 내역** — 홈페이지·카카오톡·EasyQR 등 인바운드 문의(communications direction=in) 전체 기록·기간별 '문의→상담' 전환 집계 (상담목록 하위 메뉴). **2026-09-18부터 채널 문의의 단일 화면**(통합 인박스 폐기). 기본 기간은 이번 달이되, 더 오래된 **미처리** 또는 **최근 7일 안에 처리된** 문의가 있으면 그 접수일까지 자동 확장(`models.inquiry_default_start`) — 미처리는 기본 화면에서 빠지지 않고, 방금 완료한 건도 일주일은 남는다. 상단 카드(문의·미처리·처리완료·상담등록)는 그 조회 조건의 단계별 집계이며 누르면 단계 필터가 된다. 미처리 큐는 대시보드 '오늘 처리 필요'(0~7일)·'오래 방치'(8일+)가 같은 행을 보여준다 |
| `GET /patients/<id>` | 환자 상세 + 생애주기 타임라인 |
| `GET /ward` | **재원 관리** — 외진 중 · 재원 환자 · 입원일 미확정 3섹션 |
| `POST /api/bed-reservation` · `POST /api/bed-reservation/<id>/release` | **병상 예약(사용 예정자)** — `bed_reservations`. 빈 침상에 이름을 적어 자리만 잡아 둔다: 가용 병상(대시보드 띠 `ward_occupancy`·상담일지 `room_status`)에서는 빼고 재원(KPI·명부)에는 안 센다. 상담을 이어 두면 `/api/consult/<id>/admit`에서 자동 해제. 병실 뷰는 조건(q·doctor) 없을 때 `ROOM_BED_CAPACITIES`의 빈 방도 다 그린다(2026-09-17) |
| `POST /api/consult/<id>/admit` | 입원일 확정 (이 시점부터 재원 명부 + D-day 시작) |
| `GET /lifecycle` | → `/ward` 리다이렉트 (구 생애주기 보드는 `/lifecycle/board`) |
| `GET /inbox` | 통합 인박스 (재연락·인바운드·입원/퇴원 임박) — **2026-09-10 보류·숨김, `INBOX_ENABLED=0`이면 404. 2026-09-18 사용자 결정: 폐기 확정, 채널 문의(홈페이지·EasyQR)는 대시보드 '오늘 처리 필요' + `/consultations/inquiries`(채널 문의 내역)로 일원화. 운영 `.env`에 `INBOX_ENABLED=1`을 넣지 말 것(9/17에 들어갔다가 9/18 배포 재기동으로 메뉴가 갑자기 나타난 사고)** |
| `GET /sms` `GET /sms/templates` | 문자 전송 / 템플릿 관리 |
| `POST /api/communication` `POST /api/webhook/kakao` | 커뮤니케이션 기록 / 카카오 인바운드 |
| `POST /api/patient/<id>/{stage,blacklist}` `POST /api/patient/<id>/lifecycle/event` | 생애주기·블랙리스트 |
| `POST /api/consult/<id>/admission-event` `POST /api/admission-event/<id>/return` | 외진 나감·복귀 (出/歸 페어링) |
| `POST /api/consult/<id>/discharge` | 퇴원완료·입원연장 (외진 중이면 거부) |
| `POST /api/sms/{send,template}` | 문자 발송·템플릿 |
| `GET /api/autocomplete/{patient,hospital,diagnosis}` | 자동완성 |
| `GET /healthz` | `{"ok": true}` |

## 데이터 모델 핵심

**`patients`** — 환자 마스터 (이름+연락처 자동 매칭)
- 신원: name, gender, residence_sido/sigungu, address_full
- 보호자: guardian_name/relation/phone
- 보험: insurance_type (건강보험/의료급여/보호1종/보호2종/차상위 1·2종/자보/산재/장애/암등록/장기요양/산정특례).
  **2026-09-14부터 복수 선택** — 폼은 체크박스(`patient.insurance_type[]`), 저장은 `', '`로 이어 한 칸에
  (예: `건강보험, 장애`). 목록 필터는 포함 여부(`', '||값||', ' LIKE`), 통계는 유형별로 각각 1건. 정규화는 `app._insurance_text`.
- family_info

**`consultations`** — 상담 1건 (1환자 N상담)
- 헤더: consult_date, consult_time, counselor, planned_admission_date, attending_doctor, room_number, consult_channel(전화상담/내원상담), admission_route
- 상담유입경로: referral_source_type/_detail (다중, 온라인/소개/기타 그룹), referrer_person/institution
- 환자상태:
  - 의식: consciousness_main(정상/반혼수/혼수) + conversation_level(가능/조금/불가능) + hearing_options(JSON) + hearing_note
  - 활동: activity_active(JSON 능동 4종) + activity_diaper(유/무) + activity_wheelchair(스스로/도움) + activity_others(JSON 와상/에어매트리스)
  - caregiver_status, bed_type, patient_age
- 병명 4그룹 (다중 체크 + 수기 입력):
  - **기저질환**: 당뇨(인슐린:유/무) · 고혈압 · 파킨슨[상세] · 희귀성난치질환[질환명] · 치매(경/중/고) · 인지기능저하 · 이상행동(소리지름/폭력적) · 탈출 위험 · 암 + cancer_site/onset/metastasis/pain/patch · **기타[chronic_other]**(자유 기재, `DISEASES_LAYOUT`의 `kind: text` — 병명 목록에 안 들어감)
  - **중추신경계** (1줄 stretch): 뇌출혈[수술] · 뇌경색[부위] · 척수손상[부위] · 뇌성마비 / 마비(사지/편마비좌/편마비우/하지)[상세=paralysis_detail]
  - **근골격계**: 대퇴부 · 고관절 · 골반 골절(단일/다발) · 하지 부위 절단(다음줄)
  - **비사용증후군**: 폐질환[상세] · 심장질환[상세] · 신생물[상세]
- 발병일: **disease_onset** (병명 섹션 상단, 1차 진단 단일 필드)
- 처치: admission_purpose, diet_types(JSON), wound_care(JSON)+wound_site, special_care(JSON)+oxygen_lpm, swallow_test(유/무)+swallow_test_dates, therapy(JSON)
- 입원확인: documents_checklist, admission_period, transport_method, cost_guidance, info_provided
- 기타(ARRANGE): arrange_items(JSON) — DNR(agree/consult), hopeless 확인
- 상세 메모: disease_detail (-PO/-OP 등 자유)

**JSON_FIELDS** — DB는 TEXT로 저장, 읽을 때 자동 디시리얼라이즈 (`_deserialize_consultation`)

**`source_hospitals`, `diagnoses`** — 마스터 자동완성용 (신규 입력 시 자동 추가)
**`users`, `audit_log`, `attachments`** — 인증/감사/첨부
- `audit_log`는 `models.log_audit()`으로만 쌓이고 화면에서는 `/admin/audit`로 읽기만 한다(수정·삭제 경로 없음).
- `created_at`은 SQLite `CURRENT_TIMESTAMP` = **UTC**. 읽을 땐 `datetime(created_at, 'localtime')`,
  기간 필터는 `datetime(?, 'utc')`로 변환해 비교한다 (인덱스 유지). 새 action은 `config.AUDIT_ACTION_LABELS`에 라벨 추가.

## 폼 UX 원칙

- 종이 상담일지 양식 + 엑셀 마스터 양쪽에 1:1 매칭
- 입력칸 `flex-shrink:0`, 라벨 `white-space:nowrap` — 한 줄 안 끊김
- `option-group` + `opt-box` fieldset — 카테고리별 시각 박스
- `dx-stretch` 클래스 — 남은 공간 자동 분배 (파킨슨/희귀/뇌출혈/뇌경색/척수손상/마비/비사용증후군 inputs)
- `rowbreak` 토큰 — 강제 줄바꿈 (치매 줄바꿈, 마비 줄바꿈, 비사용증후군 세로 배치)
- 010- 자동, 시군구→시도 자동, 콤보박스(상담자), 환자명 자동완성

## 코드 패턴 (재사용 출처)

| 출처 | 패턴 |
|---|---|
| `cafe-helper/db.py` | `get_db()` (sqlite3 + Row + WAL), `init_db()` |
| `cafe-helper/app.py` | Flask 앱, `load_dotenv()`, `/healthz` |
| `cafe-helper/llm.py` | Claude API 호출 + JSON 검증 (Phase 5에서 활용 예정) |
| `keyword-monitor/models.py` | `INSERT OR IGNORE` 중복 처리 |
| `keyword-monitor/static/css/style.css` | Pretendard, primary 컬러, badge 클래스 |

## 보안 원칙 (의료기관 — 절대 준수)

- **사내망 한정** (`127.0.0.1` 또는 사내 서브넷). 외부 인터넷 노출 금지
- **외부 클라우드 DB·호스팅 절대 금지** (AWS RDS, GCP, Atlas 등)
- 외부 API 호출은 처리 목적 한정 (Claude, CLOVA STT 등). 환자 식별 정보 최소화
- 인증: `werkzeug.security` 비밀번호 해시, 세션 4시간 자동 로그아웃, 5회 실패 시 5분 잠금
- 응답 헤더 `Cache-Control: no-store, private` (뒤로가기 노출 방지)
- 감사 로그: login, view_consult, view_patient, create/update_consult, export 모두 기록
- 백업: 매일 03시 + import 직전 자동, 주 1회 NAS/USB

## 절대 하지 않는 것

- ❌ 외래 환자 기능 추가 (예약/NO SHOW/HappyCall/ACS 진료 안내) — **입원 전용 병원**
- ❌ 환자 개인정보를 외부 클라우드/SaaS로 전송
- ❌ 외부 노출용 호스팅 (Vercel, Heroku, Render 등)
- ❌ 환자 식별정보(이름/주민번호/연락처)를 로그·텔레메트리에 평문 출력
- ❌ 양식에 없는 임의 필드 추가 — 양식·엑셀 마스터에 매칭되는 항목만
- ❌ 모병원(`current_location_name` / `source_hospital`)·추천기관(`referrer_institution`)에
  마스터에 없는 자유 텍스트 저장 금지. 통계 분산을 막기 위해 항상 `source_hospitals`
  마스터의 정식명만 사용. 폼은 blur/submit 시점에 마스터 매칭이 안 되면 차단
  (`.hosp-invalid`), 신규 병원은 admin이 마스터에 추가 후 입력.
- ❌ 모병원 주소를 환자 거주지로 자동 prefill 금지 — 둘이 다른 케이스가 많아 데이터 오염 위험.
  자동완성 메타(region·kind) 표시로만 식별을 돕고, 거주지는 보호자에게 직접 확인.
- ❌ 양식에 없는 임의 워크플로 — 사용자 명시적 요청 외 추가 금지
  - **단**, 상담 결과 워크플로는 사용자 명시 요청으로 추가됨. 2026-05-22 **2단계로 분리**:
    - ① 상담 진행 `consult_result` (5종: 상담완료/재입원 상담/상담요청/상담보류/상담취소).
      재입원·요청·보류·취소는 `consult_result_reason` 필수 (`CONSULT_RESULT_REASON_LABELS`).
    - ② 입원 진행 `admission_status` (입원보류/입원취소/입원완료, 빈값='미정'). 입원보류·입원취소 사유 필수.
    - 입원완료 후 `퇴원완료`(저장)·`퇴원예정`(파생). 폼 '상담 결과' 섹션·상담상세·상담목록 인라인에서 변경.
- ❌ 의료법 위반 표현 (효과 단정·완치 보장 등) — 자동 응답·메시지에서 주의

## 화면 기준

- **기준 화면 폭은 1440px** (사용자 노트북, 고해상도 2배 스케일). 레이아웃 검증은 **1440×850**으로 한다 —
  1920에서 멀쩡해도 1440에서 잘리거나 두 줄로 꺾이면 안 된다. 가로 스크롤은 실패로 본다.
- **앱 셸**: `base.html` = `body.has-sidebar > aside.sidebar + div.app-main(header.topbar + main + footer)`.
  1024px 미만은 ☰ 서랍. `body.has-sidebar{height:auto}`가 없으면 사이드바 sticky가 안 붙는다.
- **CSS 파일 역할**: `style.css`(전역·폼·표) / `dashboard.css`(대시보드 전용 스킨) / `partners.css` / `support.css`.
- ⚠ **클래스를 쓰기 전에 그 화면에 정의가 있는지 먼저 확인한다** — `grep -n "\.클래스명" static/css/*.css`.
  다른 화면에만 있는 클래스를 써서 아무 효과가 없었던 사고가 반복됐다(`nowrap`, 작은 글자·버튼 클래스).
- 셀렉터를 고칠 때는 **그 셀렉터가 걸리는 템플릿을 전부 세어 본다** — 의도한 카드 하나만 바뀌는지 확인.
- ⚠ **폰트가 CDN에서 온다** — `style.css:1`이 Pretendard를 jsdelivr `@import`로 받는다.
  사내망에서 CDN으로 못 나가면 조용히 실패해 맑은 고딕으로 떨어진다(`docs/work/2026-09-20-폰트정리.md`).
  저장소에 폰트 파일이 없어, PC에 Pretendard가 깔려 있느냐로 화면이 갈린다.
  이미지·인쇄물을 만들기 전에 `@font-face`로 실을 것. 화면에 CDN 주소를 새로 쓰지 않는다.
- 자동 갱신 페이지(`data-autorefresh`)의 본문 스크립트는 IIFE·함수 선언만 두고,
  `window` 이벤트는 대입형(`window.onresize=`)으로 — 부분 갱신 시 중복 등록을 막는다.
- 개편 경위·세부 배치는 [docs/HISTORY.md](docs/HISTORY.md).

## 데이터 집계 원칙

숫자가 화면마다 다르면 아무도 안 믿는다. 아래는 **근거를 하나로 묶는 규칙**이다.

- **입·퇴원 명단의 단일 근거는 `models.admission_flow_events(from, to)`.**
  대시보드 입·퇴원 현황과 재원관리 입원·퇴원 이력이 **둘 다 이 함수만** 쓴다.
  따로 세다가 사람을 놓친 적이 있다(2026-09-18 사용자: 어느 화면에서도 놓치면 안 된다).
- 값이 엇갈리면 **원무 명부가 이긴다.** 명부에 없는 것(상담사·나이·유입경로)만 상담에서 가져온다.
  명부에 아직 없는 건은 숨기지 말고 '명부 전' 표식으로 보여준다.
- **모병원 집계 3종**(모병원 분석·통계 대시보드·월간보고서)은 `models.hospital_display_map()` 하나를 기준으로 쓴다.
  대표 표기는 조회 기간이 아니라 **DB 전체 사용 빈도**로 정한다(기간별로 뽑으면 전월·전년 비교가 어긋난다).
- **기준이 다른 두 화면**: 모병원 분석(`/stats/hospitals`)은 **상담일** 기준, 기관협력 입원 명단은 **실제 입원일** 기준.
  입원일이 거의 안 채워져 있어서다. 입원일이 쌓이면 되돌릴 것.
- **이중 계산 금지** — 채널 문의 내역에서는 '문의 → 상담' 깔때기만 센다. 상담·입원 통계는 상담일지 하나만 기준.
- `models.is_institution_source()` — 자택 거주가 모병원 칸에 '집'으로 적힌 과거 적재값은 기관 집계 전체에서 제외.
- **앱은 중증도를 평가하지 않는다.** 상태·처치는 상담일지에 적힌 표기 그대로 칩으로 올린다(`models.care_items()`).
- ⚠ **시각은 항상 현지 시간.** `audit_log.created_at`은 SQLite `CURRENT_TIMESTAMP` = **UTC**다.
  읽을 땐 `datetime(created_at,'localtime')`, 기간 필터는 `datetime(?,'utc')`로 변환해 비교한다.
  '오늘'을 UTC로 판정해 날짜가 하루 어긋난 사고가 두 번 있었다.
- ⚠ **성능** — 새 대량 조회에서 `list_consultations(limit=10000)`을 그대로 부르지 말 것.
  `models.list_consultations(ids=[...])`로 끊는다(900개 초과는 분할).

## 외진 복귀 = 입원 (2026-09-15 사용자 명시 요청)

- **외진(응급전원·모병원 외래치료) 복귀는 그날의 입원으로 센다.** 원무 명부는 보통 나간 날 퇴원 회차를 닫고 복귀한 날 새 회차를 여니 명부만으로도 잡히지만, 명부에 복귀일 회차가 없으면 `admission_events.returned_at`(outcome 복귀)로 보충한다 — `models._AWAY_RETURN_NOT_IN_ROSTER`(같은 환자의 명부 회차가 복귀일에 시작하지 않는 건만) 한 조건을 세 곳이 같이 쓴다: 입원·퇴원 이력 탭(`ward_moves._return_rows`, '입원 복귀' 배지·엑셀 '입원(복귀)'), 대시보드 이번주/이번달 입원(`models.admission_flow_counts`), 명부 입원 KPI·30일 추이(`dashboard_metrics.admission_flow_by_date`). 타 병원 전원으로 끝난 건은 입원이 아니다. 나간 날을 퇴원으로 만들지는 않는다(명부 퇴원 회차가 이미 들고 있음).
- **명부 회차(`roster_key`)의 입·퇴원일·병실·status는 상담값으로 덮지 않는다.** `sync_admission_episode`와 기동마다 도는 `_migrate_admission_episodes`의 `ON CONFLICT(consultation_id) DO UPDATE`가 상담 입원일로 덮어써서, 외진 복귀로 새로 열린 회차(박성락 9/11)의 입원일이 첫 입원일(8/10)로 되돌아가 지난주 입원에서 빠졌다. 두 곳 다 `CASE WHEN roster_key IS NOT NULL THEN 기존값` 으로 지키고, `_repair_roster_admitted_at`(기동마다, 멱등)이 `roster_key`의 입원일과 `admitted_at`이 어긋난 회차를 명부 값으로 되돌린다 — NAS는 재배포(재기동) 때 자동 복구된다.

## 운영 메모

- **포트**: 8003 (cafe-helper 8001 / keyword-monitor 8002와 충돌 회피)
- **진입점**: `python app.py` (디버그 모드는 `FLASK_DEBUG=1`)
- **DB 위치**: `bokju.db`. 경로는 `BOKJU_DB_PATH`로 지정 (컨테이너 배포 시 `/data/bokju.db`).
  반드시 로컬 파일시스템 — SMB 공유폴더에 두면 WAL이 깨져 동시 사용 시 데이터가 손상된다.
- **배포**: 시놀로지 NAS Container Manager + `docker-compose.yml`. 사내망 `http://<NAS>:8003`으로
  상담사 4명이 브라우저 접속. 운영 서버는 `serve.py`(waitress). 절차는 `docs/DEPLOY-NAS.md`.
- **백업**: `backup.py`가 기동 시 1회 + 매일 `BACKUP_HOUR`(기본 03시) 스냅샷을 `BACKUP_DIR`에 저장,
  `BACKUP_KEEP_DAYS`(기본 30일) 경과분 자동 삭제. SQLite 온라인 백업 API라 무중단.
- **첫 셋업**: `.env`에 `APP_PASSWORD`/`SECRET_KEY` 설정 → `python app.py` → admin 계정 자동 생성 (.env의 `APP_PASSWORD` 사용)
- **재시작 시 주의**: 템플릿 변경은 즉시 반영 (Jinja 자동 리로드), config.py·models.py 변경은 서버 재시작 필요

## 개발·운영 분리 (2026-09-10)

- **개발** = 노트북 WSL, `./dev.sh` (Flask `--reload`). **운영** = NAS 컨테이너, `serve.py`(waitress).
- 운영은 `main`이 아니라 **`./release.sh`가 찍은 태그만** 받는다. 절차·롤백은 `docs/DEPLOY-NAS.md`의 '운영 배포'.
- **"배포해줘"** — 사용자가 배포/deploy를 요청하면(병원 PC에서 한 마디로 배포하고 싶을 때)
  `ssh bokju-nas "sudo -n /root/deploy.sh --yes"` 한 줄을 실행하고 결과를 정리해 보여준다.
  이 한 줄이 NAS에서 [백업 → 최신 태그 교체 → 재빌드 → 재시작 → 확인]까지 한다. 되돌리기는
  `--yes` 대신 `--rollback`. 자세한 지침은 `/배포` 슬래시 명령(`.claude/commands/배포.md`).
- 버전을 올릴 때는 `config.APP_VERSION`과 `release_notes.py` 안내를 **함께** 고친다. `release.sh`가 누락을 막는다.
- 개발 DB(`bokju.db`)에는 실환자 데이터가 그대로 있다(사용자 결정). `.gitignore`가 `*.db`·`backups/`·`uploads/`·`.env`를 막고 있어 저장소에는 올라가지 않는다.
- 개발 PC는 여럿이어도 된다(집 노트북·병원 PC). 준비 절차는 `docs/DEV-SETUP.md`. **금지는 NAS 안의 파일을 직접 고치는 것 하나뿐** — 다음 배포에 덮어써져 사라지고 되돌릴 기록도 없다.
- PC가 여럿이면 작업 시작 전 `git pull`을 먼저 한다. 2026-09-10에 원격에 11개 커밋이 쌓인 채로 push해 거절됐고, 그때 `release.sh`가 태그를 먼저 찍는 바람에 로컬에 찌꺼기 태그가 남는 문제까지 드러났다(수정 완료).

## 참고 문서

| 문서 | 언제 읽나 |
|---|---|
| [docs/HISTORY.md](docs/HISTORY.md) | "왜 이렇게 됐지?" — 기능 확장 경위, 대시보드 개편, 기관협력, 성능 개선 |
| [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md) | 채널 연동 작업 — 카카오·홈페이지 게시판·EasyQR·팩스·웹훅 |
| [docs/DEPLOY-NAS.md](docs/DEPLOY-NAS.md) | 운영 배포·롤백 절차 |
| [docs/DEV-SETUP.md](docs/DEV-SETUP.md) | 새 PC에서 개발 환경 준비 |
| [docs/PRD.md](docs/PRD.md) · [docs/TRD.md](docs/TRD.md) | 최초 기획·기술 요건 |
| [docs/MANUAL.md](docs/MANUAL.md) | 상담사용 사용법 |
| [AGENTS.md](AGENTS.md) | 릴리스 작업 규칙 (버전·안내문) |
