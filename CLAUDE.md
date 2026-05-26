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
python app.py              # http://127.0.0.1:8003
```

기본 계정: ID `admin` / PW = `.env`의 `APP_PASSWORD`

## 구조

```
app.py             Flask 진입점, 모든 라우트, API
models.py          SQLite 스키마 + 마이그레이션 (_ensure_columns) + JSON 직렬화
auth.py            인증 + @login_required / @admin_required
config.py          상수 (보험·시도/시군구·병명 LAYOUT·입원경로 등)
templates/         base.html, login, dashboard, consult_form/list/detail, patient_detail, error
static/css/        style.css (Pretendard, 4 그룹 박스, dx-stretch 등)
static/js/         common.js, form.js (자동완성, 시군구→시도, 010 포맷, 콤보박스)
bokju.db           SQLite (gitignore)
backups/           일일 자동 백업 (gitignore)
uploads/           마이그레이션·녹음 임시 (gitignore)
```

## 주요 라우트

| 경로 | 동작 |
|---|---|
| `GET /login` `POST /login` | 인증 |
| `GET /` | 대시보드 (이번달 카드 + 오늘 등록 + 7일 추이) |
| `GET /consult/new` `POST /api/consult` | 상담일지 등록 |
| `GET /consult/<id>` `GET /consult/<id>/edit` `POST /api/consult/<id>` | 상세 / 수정 |
| `GET /consultations` `GET /consultations.csv` | 목록 / CSV (admin) |
| `GET /patients/<id>` | 환자 상세 + 생애주기 타임라인 |
| `GET /lifecycle` | 환자 생애주기 보드 (단계별 컬럼) |
| `GET /inbox` | 통합 인박스 (재연락·인바운드·입원/퇴원 임박) |
| `GET /sms` `GET /sms/templates` | 문자 전송 / 템플릿 관리 |
| `POST /api/communication` `POST /api/webhook/kakao` | 커뮤니케이션 기록 / 카카오 인바운드 |
| `POST /api/patient/<id>/{stage,blacklist}` `POST /api/patient/<id>/lifecycle/event` | 생애주기·블랙리스트 |
| `POST /api/sms/{send,template}` | 문자 발송·템플릿 |
| `GET /api/autocomplete/{patient,hospital,diagnosis}` | 자동완성 |
| `GET /healthz` | `{"ok": true}` |

## 데이터 모델 핵심

**`patients`** — 환자 마스터 (이름+연락처 자동 매칭)
- 신원: name, gender, residence_sido/sigungu, address_full
- 보호자: guardian_name/relation/phone
- 보험: insurance_type (보험/보호1종/보호2종/차상위 1·2종/자보/장애/암등록/장기요양/산정특례)
- family_info

**`consultations`** — 상담 1건 (1환자 N상담)
- 헤더: consult_date, consult_time, counselor, planned_admission_date, attending_doctor, room_number, consult_channel(전화상담/내원상담), admission_route
- 상담유입경로: referral_source_type/_detail (다중, 온라인/소개/기타 그룹), referrer_person/institution
- 환자상태:
  - 의식: consciousness_main(정상/반혼수/혼수) + conversation_level(가능/조금/불가능) + hearing_options(JSON) + hearing_note
  - 활동: activity_active(JSON 능동 4종) + activity_diaper(유/무) + activity_wheelchair(스스로/도움) + activity_others(JSON 와상/에어매트리스)
  - caregiver_status, bed_type, patient_age
- 병명 4그룹 (다중 체크 + 수기 입력):
  - **기저질환**: 당뇨(인슐린:유/무) · 고혈압 · 파킨슨[상세] · 희귀성난치질환[질환명] · 치매(경/중/고) · 인지기능저하 · 이상행동(소리지름/폭력적) · 탈출 위험 · 암 + cancer_site/onset/metastasis/pain/patch
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
- **카카오 webhook** — `/api/webhook/kakao` (구조만, `.env` KAKAO_WEBHOOK_TOKEN으로 검증).
  비즈채널 연동 시 보호자 메시지가 인박스에 자동 등록. 실제 페이로드 형식은 연동 시 확정.
- 인프라 의존 미구현: STT 자동 상담일지(NAS·음성캡처 확정 필요), 팩스 OCR, 웹문의 폼.

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
- **DB 위치**: `bokju.db` (현재 로컬). 향후 시놀로지 NAS 또는 MariaDB 이주 검토
- **첫 셋업**: `.env`에 `APP_PASSWORD`/`SECRET_KEY` 설정 → `python app.py` → admin 계정 자동 생성 (.env의 `APP_PASSWORD` 사용)
- **재시작 시 주의**: 템플릿 변경은 즉시 반영 (Jinja 자동 리로드), config.py·models.py 변경은 서버 재시작 필요
