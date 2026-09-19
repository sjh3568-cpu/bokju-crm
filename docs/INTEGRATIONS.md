# INTEGRATIONS — 옴니채널 연동 상세

> 외부 접점(카카오·홈페이지·EasyQR·팩스·웹훅)을 한 환자·한 화면으로 모으는 계층의
> 동작 명세입니다. **연동 작업을 할 때만** 읽으면 됩니다.
> 항상 지켜야 할 규칙(개인정보 외부 전송 금지 등)은 [CLAUDE.md](../CLAUDE.md)에 있습니다.
>
> 새 채널을 붙일 때 공통으로 지킬 것:
> - 시각은 항상 **현지 시간**으로 기록한다 (UTC로 새면 '오늘'이 어긋난다)
> - 중복 방지는 워터마크(`data/<이름>_sync_status.json`) + 식별자 대조 **2중**으로
> - 키·토큰은 `.env`에만. **Git에 올리지 않는다**
> - 설정이 없으면 **조용히 비활성**된다 (에러로 앱을 막지 않는다)
> - 실패를 삼키지 않는다 — 화면에 상태를 표시한다
> - 외부로 여는 경로는 `/api/webhook/*` 하나뿐, 나머지 CRM은 사내망 유지

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

