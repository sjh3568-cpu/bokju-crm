# CLAUDE.md — 복주 상담실 CRM (bokju-crm)

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

## 향후 단계 (AI 옴니채널 로드맵)

세계 최고 수준의 상담관리 시스템을 향한 단계적 확장:

- **Phase 1+** — 디지털 상담일지 양식 + 자동완성 + 입력 UX 최적화 ✅
- **Phase 3** — 통계·인사이트 대시보드 ✅ (Chart.js v4 + Claude 인사이트 + 입원 진행 KPI)
  - 상단 KPI 6장: 총 상담 / 입원완료 / 전환율 / 보류 / 취소 / 일평균
  - 12+ 차트, 데이터 특성에 맞춰 라인/도넛/수평막대/수직막대 선택
  - 상담 상세·상담목록·상담일지 폼에서 4분류 상태 변경 가능 (입원완료 + 퇴원완료/입원연장 워크플로 포함)
- **Phase 3.5** — 임원용 월간 1페이지 보고서 ✅ (`/report/monthly`)
  - KPI 8장 (전월 대비 ±%) + 채널 ROI 표 + 모병원 Top 10 + 입원취소 사유 Top + 환자 포트폴리오 미니 도넛 3종
  - Claude 임원 요약 1단락 자동 생성 ([llm.py `summarize_monthly`](llm.py))
  - `@media print` CSS — A4 1장 인쇄 최적화
  - **모병원 매핑**: 폼 "현재 병원/요양원" 입력이 `current_location_type ∈ {입원중, 입소중}`일 때 `source_hospital` 컬럼에도 자동 기록 → 마스터 풀 자동 확장
  - **입원취소 사유 라벨링**: `REJECTION_REASONS` 8종 + 자유메모. 상담 상세 status 박스에서 입원취소 선택 시 inline picker
- **Phase 2** — 엑셀 마이그레이션 도구 (Google Sheets / .xlsx → DB)
- **Phase A (AI 옴니채널 핵심)** — 통화 녹음 → CLOVA/Whisper STT → Claude 양식 매핑 → 상담일지 자동 채움 → 상담사 5분 검토만
- **Phase B** — 시놀로지 NAS Container Manager로 bokju-crm 컨테이너화 + 폴더 감시 워커
- **Phase C** — 헬로비전 SIP 계정 받아 MicroSIP 소프트폰 → 발신번호 자동 환자 팝업
- **Phase D** — 카카오 비즈채널 webhook 연동 → 보호자 문의 자동 등록 + Claude 자동 응답
- **Phase E** — 웹 문의 폼, 팩스 OCR, 직원별 계정 분리, 첨부파일 관리

비전: 외부 솔루션(나스카랩 등) 도입 대신 **자체 구축으로 5년 ~2,500만원 절감 + 기능 우수성 확보**.

## 2026-05-22 7개 기능 확장 (사용자 명시 요청)

1. ~~**내원 유형** — `consultations.admission_type`~~ → 2026-05-23 폐지. UI 제거,
   **입원 중 이벤트**로 일원화 (아래 참조). `admission_type` 컬럼·과거값은 보존.
2. **상담목록 칼럼** — 성별·나이·보험유형 개별 컬럼 분리.
3. **환자 생애주기** — `/lifecycle` 단계 보드 + `patients.lifecycle_stage` + `lifecycle_events` 테이블
   (상담/입원/응급치료/복귀/회복기·비회복기 전환/보호자·환자 요구사항/퇴원/기타). 환자 상세에 타임라인.
4. **블랙리스트** — `patients.blacklist`/`blacklist_reason`/`blacklist_at`. 폼·상세·목록·보드에 ⚠ 표기,
   목록 필터, 환자 상세에서 지정/해제.
5. **문자 전송** — `/sms` 메뉴. `sms_templates`(환자군별 정형 문구)·`sms_log` 테이블. 토큰 치환
   ({환자명}{보호자명}{병원명}{입원예정일}{주치의}). 발송사 미정 → `sms.py` 게이트웨이 자리만 구축,
   현재 'manual' 모드(휴대폰 문자앱 `sms:` 링크). 발송사 결정 시 `sms.send_sms()`만 구현하면 자동 발송.
6. **주치의 드롭다운** — `config.ATTENDING_DOCTORS` 5명(IM1 정기천/IM2 신현범/NE 변현숙/RM1 이성범/RM2 이석태).
   콤보박스(선택+자유입력) — 기존 자유텍스트 값 보존.
7. **상담 결과 2단계 분리** — 위 '절대 하지 않는 것' 참조.

추가 연동 (시너지):
- **생애주기 자동 동기화** — 입원 진행 변경 시 단계 자동 전진(입원완료→입원, 입원보류→입원대기,
  퇴원완료→퇴원). 후진 없음(`app._sync_lifecycle_stage`). 이중 입력 제거.
- **블랙리스트 임상 안전** — 신규 상담 등록 시 같은 이름·연락처가 블랙리스트면 저장 직전 확인 모달
  (`/api/patient/blacklist-check`).
- **문자 발송 통계** — 통계 대시보드에 상태별·환자군별 발송 현황 차트 (`aggregate_stats`의 `sms`).

## 옴니채널 확장 (2026-05-22)

모든 접점을 한 환자·한 화면으로 모으는 통합 계층:
- **`communications` 테이블** — 인바운드/기타 접점(전화·문자·카카오·웹문의·팩스·부재중) 통합 로그.
- **통합 타임라인** — `models.patient_timeline()`이 상담·문자(sms_log)·생애주기·커뮤니케이션
  4개 소스를 시간순 병합. 환자 상세 페이지에 표시.
- **`/inbox` 통합 인박스** — 재연락 대기(상담요청)·미처리 인바운드·입원안내 예정(D-3)·퇴원 예정을
  채널 무관하게 한곳에. 상담사의 '오늘 할 일'.
  **2026-09-10부터 기능 보류로 숨김** — `INBOX_ENABLED`(기본 0)이 0이면 좌측 메뉴·통합검색·
  시작화면 선택지에서 빠지고 라우트는 404. 코드·데이터·회귀테스트는 그대로 두었으니
  `.env`에 `INBOX_ENABLED=1`만 넣으면 되살아난다. 그동안 미처리 인바운드·재연락은
  대시보드 `/#inbound` 카드에서 처리하고, 배지·알림 링크도 그쪽을 가리킨다.
- **카카오톡 채널(오픈빌더 챗봇) → `/api/webhook/kakao/skill`** — 병원 채널의 오픈빌더
  상담신청 폼(성함·연락처·연락가능시간·거주지·환자나이·상담내용)에 스킬을 붙이면,
  제출 값이 이 엔드포인트로 와서 communications(카카오/in)로 등록되고 사용자에겐
  접수 확인 말풍선(SkillResponse v2.0)을 응답. 필드는 별칭 매핑(`_KAKAO_FIELDS`)으로 흡수,
  연락처는 `_norm_phone`으로 정규화해 환자 자동매칭. 토큰은 스킬 URL `?token=` 또는
  헤더로 검증. 스킬 URL은 카카오가 외부에서 호출 → 역프록시로 `/api/webhook/*`만 노출.
- **인바운드 webhook (직수신)** — `/api/webhook/kakao`(범용 카카오 푸시)·`/api/webhook/homepage`(홈페이지
  문의폼). `.env`의 채널별 토큰(`KAKAO_WEBHOOK_TOKEN`/`HOMEPAGE_WEBHOOK_TOKEN`)으로 검증하고,
  토큰이 비면 503으로 비활성(기본 안전). 전화번호로 환자 자동매칭 → communications(인바운드) →
  대시보드 인박스(카카오채널/홈페이지 탭). 보안: `_webhook_guard`가 상수시간 토큰비교
  (`hmac.compare_digest`)·선택적 IP 화이트리스트(`WEBHOOK_ALLOW_IPS`)·16KB 크기제한·
  IP당 rate limit(60/분)·감사로그(식별정보 평문 미기록)를 일괄 처리.
  - **EasyQR '빠른 전화상담 신청'**(카카오 '전화하기(상담 예약)' 버튼이 여는 walk.induk.ai.kr/Developer/EasyQR/consult.html,
    직원 제작 PHP, NAS Web Station) → `consult.php`가 같은 NAS의 CRM `http://127.0.0.1:8003/api/webhook/homepage`로
    서버-투-서버 전달(외부 노출 불필요). 웹훅은 `receipt_no`→제목 `#번호`, `available_time/address/patient_age`→
    본문 라벨로 보존(`_HOMEPAGE_EXTRA_FIELDS`), 전화번호 `_norm_phone` 정규화. 절차·PHP 스니펫은 docs/WEBHOOKS.md §3-1.
    회귀: tests/test_webhook_homepage.py. (2026-09-15 기준 consult.php 쪽 연결·NAS .env 토큰은 아직 미적용)
  - **역프록시 노출 원칙**: 외부로 여는 것은 `/api/webhook/*` 한 경로뿐, 나머지 CRM은 사내망 유지.
    홈페이지폼은 브라우저가 아니라 **홈페이지 서버가 서버-투-서버**로 호출(토큰 노출 금지).
    연동 규격·nginx allowlist 설정은 [docs/WEBHOOKS.md](docs/WEBHOOKS.md).
- **빌더형 홈페이지(카페24·아임웹 등) → 이메일 브릿지** ([homepage_inbox.py](homepage_inbox.py)) —
  서버 코드를 못 건드리는 빌더는 웹훅 푸시가 불가하므로, 빌더의 '새 문의 관리자 메일 알림'을
  전용 메일함으로 받아 사내망 워커가 IMAP 폴링(`.env` IMAP_*)해 communications(웹문의/in)로 등록.
  아웃바운드 구조라 외부 포트 개방 불필요(역프록시보다 안전). 설정 없으면 조용히 비활성.
- **EasyQR 전화상담 접수 연동 (2026-09-17, 2026-09-18 API 전환)** ([easyqr_inbox.py](easyqr_inbox.py)) —
  마케팅 랜딩페이지 walk.induk.ai.kr의 '빠른 전화상담 신청'(`Developer/EasyQR/api/consult.php`)은 접수를
  같은 NAS의 MariaDB `easyqr_db.consultations`에 쌓는다. **EasyQR은 기획실이 관리하며 필요한 기능은 요청하면
  추가해 준다**(처음엔 '별도 소관이라 못 고친다'고 보고 DB 직접 폴링으로 만들었으나, 기획실이 DB 계정 대신
  읽기 전용 JSON API `GET /Developer/EasyQR/api/consult_export.php`(헤더 `X-API-Key`, `after_id`/`since`/`limit`)를
  열어 줘 접속부만 API로 바꿨다. 명세: NAS 미전실 공유폴더 `EasyQR_상담데이터_API명세.md`).
  워커가 **3분마다 `after_id=last_id`로 신규 접수만 조회**해 communications(**카카오**/in, created_by=EasyQR)로
  등록한다. 컨테이너가 같은 NAS에 있으므로 내부 IP(172.16.1.250)로 부른다 — 외부 도메인은 Cloudflare가
  User-Agent 없는 요청을 403으로 막는다(워커는 UA를 항상 보냄). **실패 응답도 HTTP 200**(서버 nginx가 PHP 4xx를
  가로챔)이라 `success` 필드로 판단한다. `created_at`은 문자열로 온다(`_register`가 datetime·문자열 둘 다 받음).
  `.env` `EASYQR_API_URL`·`EASYQR_API_KEY`(+`EASYQR_POLL_SECONDS`) — 미설정이면 조용히 비활성. **키는 Git에 올리지 말 것.**
  중복 방지는 `data/easyqr_sync_status.json`의 `last_id` 워터마크 + 요약의 접수번호(`#N`) 대조 2중.
  첫 기동은 현재 최대 id부터(옛 접수 폭탄 방지; API엔 MAX가 없어 500건씩 페이지를 넘겨 끝을 찾는다) —
  과거분은 `EASYQR_BACKFILL_FROM=<id>`(0이면 전체). 카드 모양은 `/api/webhook/homepage`와 동일.
  **채널은 '카카오'**(2026-09-18 사용자 결정) — EasyQR 페이지는 카카오 비즈채널의 '전화하기(상담 예약)' 버튼이 여는 것이라
  홈페이지 게시판(웹문의)과 구분한다. 대시보드 '카카오채널' 탭·채널 문의 내역 '카카오톡'·유입경로 '카카오톡 채널'로 이어진다.
  첫 동기화(9/18 오전) 10건은 웹문의로 들어갔던 것을 `models._migrate_easyqr_channel`(init_db 1회성)이 카카오로 이관.
  **주의: 단방향이다.** CRM에서 처리 완료해도 EasyQR `consult_admin`에는 pending으로 남는다(API가 읽기 전용).
  양방향이 필요하면 '어느 쪽이 원본인가'부터 기획실과 협의. 조회 이력은 EasyQR 쪽 `export_log`에 남는다.
- **홈페이지 상담게시판 직접 연동 (2026-09-15)** ([homepage_board.py](homepage_board.py)) — 병원 홈페이지
  bokjurh.co.kr는 제작사 자체 PHP(카페24 호스팅)라 API·메일 알림이 없다. 공개 목록
  `/sub/07_community/guide_01`(번호·제목·접수/답변완료·가린 이름·날짜)을 3분마다 읽어 새 글을
  communications(웹문의/in, created_by=홈페이지 게시판)로 등록하고, `.env`의 `HOMEPAGE_ADMIN_ID/PW`로
  관리자(`/adm/sub/counsel/counselV.php`)에 로그인해 이름·연락처·본문을 채운다(글은 비밀글이라 공개
  화면에선 못 읽음). 매핑은 `homepage_posts`(idx↔comm_id, site_status, detail_ok). 대시보드 인박스의
  **✎ 답변** 버튼 → `/api/homepage-board/<comm_id>`(원문+기본 문안) → `/reply`(POST) →
  `AdminSession.reply()`가 `counselU.php`의 `group_idx/answer_idx`를 읽어 `counselUP.php`에 multipart로
  올리고 상세 화면에 답변이 붙었는지 확인한 뒤 인박스 완료. 홈페이지 관리자에서 누가 직접 답변해
  '답변완료'가 되면 다음 폴링에서 인박스도 자동 완료. 관리자 로그인이 깨져도 새 글 감지·알림은
  공개 목록만으로 계속 된다(본문은 다음 주기에 보충). 최초 기동 시 이미 답변완료인 과거 글은
  인박스에 쌓지 않고 매핑만 남긴다. 권한은 커뮤니케이션과 동일(sms 조회/등록). `HOMEPAGE_BOARD_ENABLED=0`로 끔.
  화면 구조가 바뀌면 `parse_public_list/parse_admin_view/parse_admin_update_form`만 고치면 된다
  (tests/test_homepage_board.py가 합성 HTML로 검증).
- **채널 문의 내역 (2026-09-15)** `/consultations/inquiries` ([inquiries.html](templates/inquiries.html)) — 문의(communications
  인바운드)의 전체 기록. 대시보드 '오늘 처리 필요'는 미처리만 보이는 큐라 완료하면 사라지므로, 완료·상담등록까지
  포함해 기간·채널·단계(미처리/상담등록/처리완료)·키워드로 본다. 요약 카드(문의·미처리·상담 등록+전환율·처리
  완료(상담 미등록)·평균 처리 시간), 채널별 표, 최근 12개월 월별 막대(필터 무관), 목록(전화 링크·✎ 답변·상담 등록·완료).
  `models.inquiry_rows/inquiry_summary/inquiry_monthly`. 답변 대화상자는 `_hp_reply_dialog.html`로 대시보드와 공용.
  **집계 원칙**: 여기서는 '문의 → 상담' 깔때기만 센다. 상담·입원 통계는 상담일지 하나만 기준(같은 건 이중 계산 금지).
  문의에서 '상담 등록'(`/consult/new?comm_id=`)으로 넘어가면 `views.inbound.inquiry_prefill`이 요약의 이름·본문의
  `[환자나이]`·`[거주지]`(→ 시/도·시/군/구, config 명부 대조)·연락처를 폼에 바로 채우고, 문의 내용은 'AI로 채우기'
  입력(`ai_memo_prefill`, `#ai-memo[data-autofill=1]`)에 넣어 페이지 로드 시 한 번 자동 실행해 병명·상태·입원목적까지
  채운다(2026-09-18 사용자 요청; AI 미설정이면 메모만 남음). `INBOUND_CHANNEL_REFERRAL`(웹문의→홈페이지, 카카오→카카오톡 채널)로 유입경로가
  자동 체크되고 상담방법은 전화상담으로 프리필 → 기존 통계 유입경로 차트에서 채널 문의의 상담·입원 전환이 그대로 비교된다.
  원칙: 전화를 했으면 상담일지를 등록한다(그래야 '통화 완료'='상담등록'). 답변만 남긴 문의는 '처리완료'로 남는다.
- **인바운드 알림** — 새 문의(홈페이지·카카오)가 들어오면 로그인한 상담사 브라우저가
  `/api/inbound/alerts`를 폴링(1분)해 화면 토스트 + 브라우저 알림 + 상단 '대시보드' 배지로 통지.
  `models.open_inbound_count()` = 전역 배지, `_dashboard_inbound_bucket`로 채널 분류. 상담사가
  홈페이지 관리자에 직접 들어가 확인하지 않아도 되게 하는 것이 목적.
- **부재중 → 재연락 예약** (2026-09-15) — 인바운드 카드·액션큐 행의 `부재중` 버튼 →
  `POST /api/communication/<id>/missed {follow_up_at}` → `status='waiting'` + `follow_up_at`,
  body 끝에 `[부재중 N회 MM-DD HH:MM 담당자 → 재연락 …]` 한 줄 누적(`models.mark_communication_missed`).
  시각 전엔 배지·알림·액션큐에서 빠지고(카드엔 🔁 재연락 배지로 남음), 시각이 되면
  `open_inbound_count`·`/api/inbound/alerts`(id `cb<id>@<시각>`으로 재통지, bucket 재연락)·
  액션큐(종류 '재연락', meta '부재 N회')에 다시 올라온다. 회귀: tests/test_inbound_missed.py.
- **팩스·문서 자료함 (2026-09-17)** ([fax_inbox.py](fax_inbox.py) · [views/documents.py](views/documents.py) · `/documents`) — 모병원 팩스가
  NAS 공유폴더(`FAX_INBOX_DIR`)에 PDF로 떨어지면 60초 워커가 감지(해시 중복 제외, 15초 미만 파일은 복사 중으로 대기) →
  `llm.analyze_document`(Claude, PDF/이미지 통째로 + 구조화 출력 `FAX_SCHEMA`)가 환자 이름·주병명·보낸 곳·핵심 요약·주의사항을 읽음 →
  `FAX_ARCHIVE_DIR/YYYY-MM-DD_이름_주병명.pdf`로 **이동**(삭제 없음; 못 읽으면 `날짜_미확인_원본명`) → `patient_documents`(source=팩스,
  status pending→analyzed→done) + `communications`(채널 팩스) → 대시보드 처리 큐·알림에 '📠 원본·요약' 링크. 자료함 상세는 PDF 뷰어 +
  AI 요약 + 판독값 수정→파일명 재정리 + 동명 환자 연결 + '상담 등록'(`/consult/new?doc_id=` → 이름·주병명·모병원·기관연계 prefill,
  저장 시 문서·카드 자동 완료). 파일 직접 올리기도 됨. AI 실패는 3회 재시도 후 [다시 판독]. `.tif`는 판독 불가(PC에서 PDF 저장으로).
  판독은 앞 `FAX_AI_MAX_PAGES`(50)쪽만 보낸다(pypdf로 자름, 크기 한도는 자른 뒤 적용, 파일은 통째로 보관 — 보통 10~20장, 책 두께로도 옴).
  원본은 `FAX_KEEP_DAYS`(**10일**, 사용자 결정) 지나면 매일 04시
  파일만 삭제(`purge_expired`, 수신·정리 폴더 안 경로만), 판독값·요약·연결은 DB에 남아 `file_deleted_at`로 표시 — 자료함은 사본·상담 참고용이고
  정식 보존은 모병원·EMR 몫. **주의: 팩스 문서 자체(환자 식별정보 포함)가 Claude API로 나간다** — `FAX_AI_ENABLED=0`이면 감지·자료함·수동 연결만.
  권한 consult(조회=열람, 수정=판독·연결·업로드), 감사 `*_document`. 현장 설정 순서·확인 항목은 docs/FAX-NAS-PLAN.md. 회귀 tests/test_fax_inbox.py.
- 인프라 의존 미구현: STT 자동 상담일지(NAS·음성캡처 확정 필요), 인박스에서 카톡/문자 직접 회신(아웃바운드). 팩스는 CRM 쪽 완료, 현장(복합기·PC·NAS 폴더) 설정 대기.

## 2026-05-23 상담일지 폼 개선 5종 (사용자 명시 요청)

1. **날짜 요일 표시** — 폼의 모든 `type=date` 입력칸에 'YYYY-MM-DD(요일)' 태그 자동
   표시 (`form.js` 제너릭 핸들러, `.weekday-tag`). 방치 위젯 `date-combo` 정리.
2. **내원유형 → 입원 중 이벤트** — 헤더 `admission_type` 필드 폐지. 입원완료 환자가
   입원 기간 중 응급전원·모병원 외래치료 등으로 외부 의료기관을 다녀온 내역을
   상담 상세 페이지 **'입원 중 이벤트'** 섹션에서 기록·관리.
   - `admission_events` 테이블 (consultation_id FK), `config.ADMISSION_EVENT_TYPES`
     (응급전원/모병원 외래치료/복귀/기타)
   - `POST /api/consult/<id>/admission-event`, `DELETE /api/admission-event/<id>`
   - `admission_type` 컬럼·과거값은 보존, UI(폼·목록·상세·CSV·통계)에서만 제거
3. **환자 상태 정렬** — `.inline-pair` flex-end→flex-start. 부가 요소(모병원
   빠른선택)가 붙은 칸이 옆 칸 정렬을 깨지 않도록.
4. **회복기 미니가이드** — 재활의료기관 환자구성의 기준 공식 표(가/나/다/라/마)로 교체.
5. **식사종류 배치** — `checkbox-grid`→`checkbox-flow`. '미음' 그룹 격침 해소.

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

## 2026-09-07 기관협력 관리

- `partnerships.py` Blueprint `/partners`: 마스터 병원 연결, 협력 담당자, 방문·연락 이력, 다음 일정, 기간별 상담·입원 명단/CSV.
- 초기화: `app.initialize()`에서 `models.init_db()` 다음 `partnerships.init_schema()` 호출. `cooperation_*` 4개 테이블, 핵심 2기관 마스터 기반 등록. 기존 상담 데이터는 변경하지 않음.
- `partners` 메뉴 권한 추가(조회/수정), 기존 로그인 세션은 새 키 누락 시 DB의 권한을 다시 로드.
- 집계: `referrer_institution`(실제 연계)와 `source_hospital`(이전 병원)을 명시적으로 구분. 상담은 상담일, 입원은 실제 입원일 기준. 환자+입원일로 입원 중복 제거.
- 후속 일정: 7일 전~기한 초과를 대시보드/기관목록에서 표시. 활동 저장 시 설정 주기로 후속 일정 생성(기존 동일 유형 미완료 일정은 중복 생성하지 않음). 명시 날짜는 별도 일정 생성.
- 검증: `.venv-linux/bin/python -m unittest discover -s tests -v` (임시 DB, 실제 자료 사용하지 않음).
- 전국 기관 검색은 `cooperation_facility_directory`에서 공식기관코드로 구분한다. 2026.3 심평원 원본 중 의원·병원·종합병원·상급종합 39,490곳을 적재했다. 동명 기관을 주소별로 보존한다.
- **전국 요양원 명부는 심평원이 아니라 국민건강보험공단 장기요양기관 API**(`ltci_sync.py`, 2026-09-16). 심평원 명부엔 요양원이 없다. 공공데이터포털 '국민건강보험공단_장기요양기관 검색 서비스'(`B550928/searchLtcInsttService02/getLtcInsttSeachList02`)는 시도코드가 필수·응답 XML·주소 없음이라 시도 16코드×입소 유형(A03 노인요양시설·A04 공동생활가정)을 돌아 `source_nursing_homes`에 넣는다(재가 B·C 유형 제외; 광주·전남은 2026년 통합으로 시도코드 12 '전남광주'). 동명 요양원은 '이름 (시도)'·'이름 (시도 2)'로 구분. 키는 `LTCI_SERVICE_KEY`(없으면 `HIRA_SERVICE_KEY` 겸용이지만 **이 서비스도 활용신청이 따로 필요**). 상담일지 '환자상태 → 요양원' 칸은 병원 칸과 같은 도움 상자(`hospCfg`)로 [공단(장기요양기관)에서 찾기]·[이름 그대로 등록]을 쓴다(`/api/nursing/lookup`·`/api/nursing/register`). 기관협력 화면 상단에 상태·[요양원 명부 지금 갱신].
- 기관 목록은 피드/표 전환, 상세는 동일 출처 iframe 모달. 기간 입력을 포함한 모든 날짜 입력은 공통 `date-presets.js` 빠른 달력을 사용한다.
- 상담사 역할의 기관협력 기본 권한은 조회. 방문 담당자/관리자에게만 수정 권한을 부여한다.
- ~~통계 모병원 분석도 기관협력과 같이 실제 입원일만 사용~~ → 2026-09-10 변경. **모병원 분석(`/stats/hospitals`)은 상담일 기준**이다. 입원일이 534건 중 2건(0.4%)만 채워져 있어 입원일 기준으로는 화면이 사실상 비었다. 기관협력의 입원 명단·기간 집계는 그대로 **실제 입원일 기준**이므로 두 화면의 기준이 다르다 — 입원일이 채워지면 모병원 분석에도 입원 기준을 선택지로 되돌릴 것.
- 모병원 집계 3종(모병원 분석·통계 대시보드·월간보고서)은 `models.hospital_display_map()`을 단일 기준으로 쓴다. 띄어쓰기·약칭 표기는 한 기관으로 합치되(`_fold_hospital_abbreviations`는 접미사형 후보가 **하나일 때만** 접는다 — 안동병원과 안동의료원은 다른 기관), 대표 표기는 조회 기간이 아니라 **DB 전체 사용 빈도**로 정한다. 기간별로 뽑으면 전월·전년 비교가 어긋나고 이름으로 맞추는 쪽(협력기관 등록 여부)이 샌다.
- `models.is_institution_source()` — 자택 거주가 모병원 칸에 '집'으로 적힌 과거 적재값을 기관 집계 전체에서 제외한다.
- **기관연계 vs 직접 방문** — `referral_source_detail`의 `기관연계` 표식으로 구분한다. 협력 활동의 성과는 이쪽으로 본다. 최근 1년 실측: 기관연계 94건→입원 32건(34.0%), 직접 1322건→344건(26.0%). 안동병원은 상담 313건이 직접유입이라 상담량 순위가 협력 성과를 뜻하지 않는다.
- 협력기관 후보(`partnerships.partner_candidates`) — 최근 1년 상담 5건 이상 **또는** 입원 2건 이상인데 미등록인 모병원을 입원 기여 순으로 제시한다. 자동 등록하지 않는다(표기 오류·1건짜리가 섞여 목록이 오염된다). 보류는 `cooperation_candidate_skips`.

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
- 협력기관 기본 보기는 목록형. 공식 상세정보는 2026.3 시설·진료과목·특수진료 XLSX를 기관코드로 연결해 진료과목, 입원 병상 합계, 간호간병통합서비스(KH)를 표시한다. 수동 `specialties`/`strengths`와 공식정보를 덮어쓰지 않고 함께 표시한다.
- `cooperation_agreements`: 기관별 업무협약서 메타데이터(문서명, 체결/만료, 상태, 상대 담당자, 문서 보관 위치, 비고). 파일 자체를 DB에 저장하지 않는다.
- 상세자료 갱신: `.venv-linux/bin/python tools/import_cooperation_facility_details.py <2026.3 XLSX 폴더> --updated-at 2026-03`.

## 2026-09-15 속도·크기 개선 (재원 관리·대시보드)

- **상담 대량 조회는 ids로 끊는다** — `models.list_consultations(ids=[...])`(`_build_consult_where` `c.id IN`, 900개 초과 분할).
  재원 관리·대시보드·회복기 추이·스파크라인이 요청마다 상담 8천 건 전체를 읽고 파이썬에서 거르던 것을
  census(`by_consultation` 키)·회차 `consultation_id`만 읽게 바꿨다. 실측: 대시보드 299→119ms, 재원 393→136ms.
  새 대량 조회를 짤 때 `list_consultations(limit=10000)`을 그대로 부르지 말 것.
- **재원 명단 지연 로딩** — `/ward` 첫 화면은 명단(`#wd-roster-body`)이 접혀 있으므로 렌더하지 않고(`data-lazy`),
  '명단 펼치기'에서 `/ward?partial=roster&<같은 쿼리>`로 [_ward_roster.html](templates/_ward_roster.html)만 받아 끼운다
  (같은 `ward_view`가 partial 요청엔 그 템플릿만 돌려줌). 검색·필터·view가 있으면 예전처럼 즉시 렌더.
  침상 카드 매크로는 [_ward_bed.html](templates/_ward_bed.html)로 분리(본문·부분 공용). 985KB→172KB(gzip 34KB).
- **침상 카드 편집 폼 1벌** — 외진·퇴원·복귀·호실 폼을 카드 263장마다 넣지 않고 `#rm-editor-tpl`에서 클릭 시
  복제(`ensureEditor`). 카드는 값만 `data-room/aevent/return-date/event-date/return-room`로 들고 있다.
  명단 바인딩은 `bindRoster(root)` 함수 — 지연 로딩된 조각에 다시 걸 수 있게.
- **부분 갱신** — `data-autorefresh` 페이지(대시보드·재원)는 30초마다 `location.reload()` 대신 같은 주소를 fetch해
  `main.container`만 바꿔 끼우고 본문 인라인 스크립트를 다시 실행한다(base.html `partialRefresh`). 스크롤·'더보기'·
  details 펼침·재원 명단 펼침(sessionStorage)이 유지되고 깜빡이지 않는다. 본문 스크립트는 IIFE·함수 선언만 두고,
  `window.addEventListener`는 대입형(`window.onresize=`)으로 — 재실행 시 중복 등록 방지. 입력 중·모달 열림이면 건너뜀.
- **gzip** — `app._gzip_response`(after_request)가 4KB 이상 text/html·json·css·js·csv를 압축(waitress 앞에 프록시가 없어
  앱이 직접). 파일 전송(passthrough)·이미 인코딩된 응답은 제외. 회귀: tests/test_perf_lazy.py.

## 2026-09-13~14 대시보드 개편 · 좌측 사이드바

- **기준 화면 폭은 1440px**(사용자 노트북, 고해상도 2배 스케일). 레이아웃 검증은 1440×850으로 한다 — 1920에서 멀쩡해도 1440에서 잘리거나 두 줄로 꺾이면 안 된다.

- **앱 셸**: `base.html`이 `body.has-sidebar > aside.sidebar + div.app-main(header.topbar + main + footer)` 구조. 주 메뉴는 사이드바(하위 메뉴는 ▾로 고정 펼침 + 마우스를 올리면 안쪽으로 펼침, 현재 그룹은 자동 펼침; '관리'도 같은 구조), 상단바는 옅은 바탕에 흰 검색창·오늘 현황·알림·화면설정·새 상담. 1024px 미만은 ☰ 서랍. 접기(아이콘만)는 `localStorage bokju:sidebar`, **상담목록(`/consultations`)만** 항상 접힌 채로 시작(2026-09-14; 상담일지 폼은 사용자 취향 유지). 접힌 채로 커서를 150ms 올리면 본문 위로 덮어 펼치는 hover peek(`body.sidebar-peek`), ⟨ 버튼이 고정. `body.has-sidebar{height:auto}`가 없으면 `html,body{height:100%}` 때문에 사이드바 sticky가 안 붙는다.
- **좌측 하단 도구 칸** `#sidebar-tools`(`position:fixed`, 폭 `--sbw`): 통합 달력 링크(오늘 일정+ToDo 배지 `calendar_badge`) + 기간 계산기·To-DO·환자분류체계. ≥1024px에서 JS가 우하단 플로팅 버튼을 이 칸으로 옮기고, 좁은 화면은 플로팅으로 되돌린다. 사이드바엔 `padding-bottom:140px`로 자리 확보.
- **대시보드**(`dashboard.html` + `static/css/dashboard.css`, 청록 포인트) — 내 담당 한 줄(청록 띠) → KPI 4장×2줄(오늘 상담·오늘 입원/퇴원·이번달 상담 유입·이번달 입원 성사+전환율 / 현재 재원·병상 가동률·외진 환자·회복기 비율; SVG 선 아이콘 배지, 설명은 핵심 숫자만 `<b>`) → **2×2 격자 `.dash-grid2`**(폭·높이 동일): 입·퇴원 현황 · 병동별 재원 / 오늘 처리 필요 · 기한 임박. 목록은 `data-limit="5"` + 더보기.
- **오늘 처리 필요 vs 기한 임박**: 전자는 지금 손이 가야 하는 일(문의·재연락·보류·입원 준비 누락·운행·퇴원 예정일 초과)이고 구분 탭(`item.group`, 구분별 고정 색)이 있다. 후자는 앞으로 올 날짜 예고 — 회복기 전환 D-30(전환 전 0~30일만, 보호자에게 치료시간·비용 안내)과 퇴원 예정 D-30. 퇴원 예정일이 지난 재원은 전자(퇴원예정 탭)로 간다. 재원 목록은 `_dashboard_residents()`(명부 census 기준)를 같이 쓴다.
- 입원 환자 현황 빠른 조회는 범위다: 과거 3·7일 이내(실제 입원), 오늘, 미래 3·7일 이내(입원 예정).
- **입·퇴원 현황**(2026-09-18) — 한 표에 입원예정·실제 입원·외진 복귀·**퇴원**을 같이 담고 구분 탭(`admission_scope`: all/planned/completed/`discharged`)으로 거른다. 퇴원 행은 (환자, 퇴원일) 하나가 한 줄 — 앱에서 퇴원 처리하면 상담 `discharge_date`·CRM 회차·명부 회차가 다 닫혀 셋으로 보였다. 병실·병동·주치의·행선지는 명부 값이 상담 값을 덮고, 상태·처치·상담사·보험·유입경로는 상담에서 온다. 일시 칸의 시각 자리에 재원일수를 쓴다.
  - 퇴원 행은 `admission_selected`(조회 기간 표)에만 넣는다. KPI·업무 큐가 쓰는 `admission_window_schedule`에 섞으면 '오늘 입원' 수가 부풀어서다. 탭 숫자는 기간만 적용한 `summary.admission_selected_counts`.
  - 고친 증상: 9/17에 퇴원 처리한 환자가 어디에도 없었다. 표는 입원일만 보고, '오늘 퇴원' 소제목은 오늘 날짜만 봤다 (9/18에 9/17 퇴원을 적으면 둘 다 빗나간다). 그 소제목은 표에 합쳐 없앴다.
- **상태·처치 칸**(옛 '중증도', 2026-09-18) — `models.care_items()`. 상담일지에 적힌 항목을 **적힌 표기 그대로** 칩으로 올리고 등급·점수는 매기지 않는다(앱이 중증도를 평가하지 않는다). 근거가 둘: 체크박스(의식·활동·식사·상처소독·특수처치)와 병명 상세 자유 기재(`disease_detail`). 운영 자료는 거의 전부 후자다 — 체크박스는 상담 8천 건 중 1건, 자유 기재엔 섬망 55·홈벤트 33·목관 23건이 글로 적혀 있다. `CARE_TERM_EXCLUDES`는 같은 글자가 진단명인 경우를 막는다 (저산소성 뇌손상·무산소증≠산소요법, 흡인성폐렴≠석션, 발목관절≠목관). 내성균은 `detected_organisms`+격리 해제 반영 배지가 따로 맡는다.
- 통합 달력(`/calendar`): 구분 칩을 눌러 켜고 끔(`localStorage bokju:cal-hide`), 회복기 전환일 구분 포함.
- 대시보드에서 분리한 것: 통합 달력 → `/calendar`(`calendar.html`, 대시보드 메뉴 하위·좌측 하단 도구 칸·대시보드 머리글 버튼), 주간 상담 현황 → `/report/weekly`(`report_weekly.html`, 통계·보고 메뉴 하위, report 권한), 요일×시간대 상담 히트맵 → `/stats` 2번 섹션(`dashboard_metrics.consult_weekday_hour_matrix`, 상담 시각이 입력되면 시간대 칸이 채워짐). 30일 입퇴원 추이·보류 목록·오늘 상담 병명별은 뺐다(재원 관리·상담목록에 있음).
- KPI 비교값은 **지난주 같은 요일**(요일 편차 때문에 어제 대비는 쓰지 않는다). 재원·입퇴원·외진 스파크라인은 원무 명부 회차(`roster_key`)·`admission_events` 기준(`dashboard_metrics.py`).
- 회복기 비율 7일 전망(`app._recovery_projection`)은 만료·퇴원 예정만 뺀 보수적 값 — 입원 예정은 회복기 확정이 아니라 넣지 않는다.
- **병동별 재원**: 허가 병상 `config.WARD_BED_CAPACITIES`(8개 병동, 합 355 = `app.WARD_BED_CAPACITY`) → 가동률·여유/빡빡/포화 색. 남/여 빈 병상은 `config.ROOM_BED_CAPACITIES`(병실별 병상 수, 기본 4인실)로 센다 — 남자만 있는 방의 빈자리=남, 여자만=여, 빈 방=빈방, 병동 허가와 방 합계 차이=미확인, 명부에 병실이 없는 재원=병실 미기재. 방 번호는 `_norm_room`으로 숫자만 남겨 맞춘다('316호 ★' 같은 표기). 미해결: 13병동 방 합계 28 vs 허가 27, 2병동 4·12병동 3병상은 방 번호 미확인, 9병동은 방 정보 없음.
- 상담 시각(`consult_time`)이 입력되지 않아 시간대별 히트맵은 만들지 않았다 — 입력이 쌓이면 요일×시간대로 바꿀 것.


## 2026-09-15 외진 복귀 = 입원 (사용자 명시 요청)

- **외진(응급전원·모병원 외래치료) 복귀는 그날의 입원으로 센다.** 원무 명부는 보통 나간 날 퇴원 회차를 닫고 복귀한 날 새 회차를 여니 명부만으로도 잡히지만, 명부에 복귀일 회차가 없으면 `admission_events.returned_at`(outcome 복귀)로 보충한다 — `models._AWAY_RETURN_NOT_IN_ROSTER`(같은 환자의 명부 회차가 복귀일에 시작하지 않는 건만) 한 조건을 세 곳이 같이 쓴다: 입원·퇴원 이력 탭(`ward_moves._return_rows`, '입원 복귀' 배지·엑셀 '입원(복귀)'), 대시보드 이번주/이번달 입원(`models.admission_flow_counts`), 명부 입원 KPI·30일 추이(`dashboard_metrics.admission_flow_by_date`). 타 병원 전원으로 끝난 건은 입원이 아니다. 나간 날을 퇴원으로 만들지는 않는다(명부 퇴원 회차가 이미 들고 있음).
- **명부 회차(`roster_key`)의 입·퇴원일·병실·status는 상담값으로 덮지 않는다.** `sync_admission_episode`와 기동마다 도는 `_migrate_admission_episodes`의 `ON CONFLICT(consultation_id) DO UPDATE`가 상담 입원일로 덮어써서, 외진 복귀로 새로 열린 회차(박성락 9/11)의 입원일이 첫 입원일(8/10)로 되돌아가 지난주 입원에서 빠졌다. 두 곳 다 `CASE WHEN roster_key IS NOT NULL THEN 기존값` 으로 지키고, `_repair_roster_admitted_at`(기동마다, 멱등)이 `roster_key`의 입원일과 `admitted_at`이 어긋난 회차를 명부 값으로 되돌린다 — NAS는 재배포(재기동) 때 자동 복구된다.
