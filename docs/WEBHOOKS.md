# 옴니채널 인바운드 webhook 연동 가이드

카카오 비즈채널·홈페이지 문의폼에서 들어온 문의를 **사내망 CRM 인박스**로
자동 등록하기 위한 연동 규격이다.

> ⚠️ 보안 원칙 — CRM 본체는 사내망 전용이다. 외부로 여는 것은
> **`/api/webhook/*` 경로 단 하나뿐**이며, 나머지 경로는 절대 외부 노출하지 않는다.
> (역프록시 설정은 맨 아래 참조)

---

## 1. 공통 규칙

| 항목 | 값 |
|---|---|
| 메서드 | `POST` |
| 본문 | `application/json` (UTF-8), 최대 16KB |
| 인증 | 헤더 `X-Webhook-Token: <채널 토큰>` (또는 `?token=` 쿼리) |
| 성공 | `200 {"ok": true, "id": <comm_id>, "matched_patient": <환자id|null>}` |

**응답 코드**
- `401` 토큰 불일치 · `403` 화이트리스트 밖 IP · `413` 본문 초과
- `429` rate limit (IP당 60회/분) · `503` 해당 채널 토큰 미설정(=비활성)
- `400` 필수값(`message`) 누락

토큰은 `.env`에 설정한다(비어 있으면 그 채널은 503으로 비활성 — 기본 안전):
```bash
python -c "import secrets;print(secrets.token_urlsafe(32))"   # 토큰 생성
# .env
KAKAO_WEBHOOK_TOKEN=...
HOMEPAGE_WEBHOOK_TOKEN=...
# (선택) 호출 서버 고정 IP만 허용 — 노출 위험을 크게 줄인다
WEBHOOK_ALLOW_IPS=203.0.113.10,203.0.113.11
```

전화번호(`phone`)가 기존 환자 **보호자 연락처**와 일치하면 해당 환자에
자동 연결되고, 블랙리스트 환자면 대시보드 인박스에 ⚠ 경고로 뜬다.

---

## 2. 카카오톡 채널 (오픈빌더 챗봇) → `/api/webhook/kakao/skill`

병원 카카오톡 채널에 **카카오 i 오픈빌더 챗봇**이 이미 있고, 상담신청 블록에서
`성함·연락처·연락가능시간·거주지·환자나이·상담내용`을 받고 있다. 이 블록에
**스킬(Skill)** 을 붙여 우리 서버로 값을 넘기면 CRM 인박스에 자동 등록된다.

### 오픈빌더 설정
1. **봇 관리 → 스킬 → 스킬 만들기**
   - URL: `https://<웹훅도메인>/api/webhook/kakao/skill?token=<KAKAO_WEBHOOK_TOKEN>`
     (또는 커스텀 헤더 `X-Webhook-Token: <토큰>`)
   - 방식: POST
2. **상담신청 블록**에서 파라미터로 위 6개 항목을 수집(엔티티/파라미터명은
   `성함/연락처/…` 그대로거나 영문이어도 됨 — 서버가 별칭으로 흡수한다).
3. 그 블록의 **응답에 스킬 데이터를 연결**(스킬 실행) → 폼 제출 시 서버 호출.

### 서버가 받는 것 (오픈빌더 skill payload)
```json
{
  "userRequest": { "utterance": "상담 신청", "user": { "id": "..." } },
  "action": {
    "params": {
      "성함": "박보호", "연락처": "01012345678", "연락가능시간": "오후 2시 이후",
      "거주지": "경북 포항시", "환자나이": "78", "상담내용": "뇌졸중 재활 입원 문의"
    }
  }
}
```
### 서버가 돌려주는 것 (사용자에게 보일 말풍선)
```json
{ "version": "2.0",
  "template": { "outputs": [ { "simpleText": {
    "text": "상담 신청이 접수되었습니다 · 박보호.\n평일 09:00~17:30 ... 전화드리겠습니다." } } ] } }
```
- 채널 = **카카오**, 대시보드 "카카오채널" 탭 + 실시간 알림.
- 연락처는 `010-XXXX-XXXX`로 정규화해 기존 환자(보호자 번호)와 자동 매칭.
- 6개 항목은 인박스 본문에 라벨과 함께 그대로 보존(누락 없음).

> ⚠️ 스킬 URL은 외부에서 카카오가 호출하므로 **역프록시로 `/api/webhook/*`만 노출**
> (4번 참조). `KAKAO_WEBHOOK_TOKEN`이 비어 있으면 503으로 비활성.

### (참고) 범용 카카오 푸시 → `/api/webhook/kakao`
오픈빌더가 아니라 자체 서버에서 단순 전달할 때:
```
POST /api/webhook/kakao   (X-Webhook-Token 헤더)
{ "phone": "010-1234-5678", "name": "홍보호자", "message": "입원 문의드립니다" }
```

---

## 3. 홈페이지 문의폼 → `/api/webhook/homepage`

홈페이지 문의폼은 **브라우저에서 직접 호출하지 말 것** — 토큰이 노출된다.
반드시 홈페이지 **서버 측**에서 서버-투-서버로 전달한다.

```
POST /api/webhook/homepage
X-Webhook-Token: <HOMEPAGE_WEBHOOK_TOKEN>
Content-Type: application/json

{
  "name":    "김문의",
  "phone":   "010-1234-5678",
  "email":   "guardian@example.com",   // 선택
  "subject": "입원 상담 요청",           // 선택 (인박스 제목)
  "message": "부모님 재활 입원 가능한지 문의드립니다."   // 필수
}
```
- 채널 = **웹문의**, 대시보드 "홈페이지" 탭에 노출.
- `email`은 본문 하단에 함께 기록된다.

### 예시 (PHP — 문의폼 처리 스크립트에 추가)
```php
$payload = json_encode([
  "name"    => $_POST["name"],
  "phone"   => $_POST["phone"],
  "email"   => $_POST["email"] ?? "",
  "subject" => $_POST["subject"] ?? "홈페이지 문의",
  "message" => $_POST["message"],
], JSON_UNESCAPED_UNICODE);

$ch = curl_init("https://<사내망-공개도메인>/api/webhook/homepage");
curl_setopt_array($ch, [
  CURLOPT_POST => true,
  CURLOPT_POSTFIELDS => $payload,
  CURLOPT_HTTPHEADER => [
    "Content-Type: application/json",
    "X-Webhook-Token: " . getenv("BOKJU_HOMEPAGE_TOKEN"),  // 서버 환경변수
  ],
  CURLOPT_RETURNTRANSFER => true,
]);
curl_exec($ch); curl_close($ch);
// 홈페이지 DB에도 그대로 저장하고, 이 호출은 실패해도 무시(비동기/try 권장)
```

### 예시 (테스트 — curl)
```bash
curl -X POST https://<도메인>/api/webhook/homepage \
  -H "X-Webhook-Token: $HOMEPAGE_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"테스트","phone":"010-0000-0000","message":"연동 테스트"}'
```

---

## 4. 역프록시 노출 (시놀로지 NAS / nginx)

목표: **`/api/webhook/` 로 시작하는 경로만** 외부에서 도달, 그 외 전부 차단.

### Synology 리버스 프록시 + 커스텀 헤더/규칙
Synology DSM 리버스 프록시는 경로 화이트리스트가 약하므로,
**앞단 nginx(또는 DSM의 Web Station nginx)** 에 location 규칙을 두는 것을 권장한다.

### nginx 예시
```nginx
server {
    listen 443 ssl;
    server_name webhook.example.com;

    # 웹훅 경로만 사내망 CRM으로 전달
    location /api/webhook/ {
        proxy_pass http://<NAS-내부IP>:8003;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;      # rate limit·화이트리스트용
        client_max_body_size 32k;                     # 본문 크기 1차 차단
    }

    # 그 외 모든 경로는 외부 접근 차단
    location / { return 404; }
}
```
- CRM 본체(`http://<NAS>:8003`)는 **사내망에서만** 접속 유지.
- 위 nginx는 웹훅 수신 전용 도메인으로만 쓴다.
- `X-Real-IP`가 전달되어야 `WEBHOOK_ALLOW_IPS`·rate limit이 정상 동작한다
  (프록시 뒤라면 앱에서 `X-Forwarded-For` 신뢰 설정 필요 시 별도 조정).

---

## 4-b. 빌더형 홈페이지(카페24·아임웹 등) — 이메일 브릿지

홈페이지가 **빌더형이라 서버 코드를 못 건드리는 경우**, 위 웹훅을 직접 호출할 수
없다. 대신 이들 빌더는 대부분 **새 문의 접수 시 관리자 이메일 알림**을 보낸다.
그 메일함을 사내망 워커가 IMAP으로 읽어 인박스에 자동 등록한다.

> ✅ 이 방식은 **사내망 → 메일서버로 나가서 당겨오는 아웃바운드**라
> 외부 포트를 열 필요가 없다 (역프록시 노출 불필요 — 보안상 더 안전).

**설정** (`.env`) — 셋 다 있어야 활성, 없으면 조용히 비활성:
```bash
IMAP_HOST=imap.naver.com
IMAP_PORT=993
IMAP_USER=inquiry@yourhospital.com
IMAP_PASS=            # 메일 앱 비밀번호 권장
IMAP_FOLDER=INBOX
IMAP_POLL_SECONDS=120
IMAP_FROM_FILTER=no-reply@imweb.me   # (선택) 빌더 알림 발신주소만 처리
```

**준비 절차**
1. 문의 알림을 받을 **전용 메일 계정** 준비(또는 기존 관리자 메일함 사용).
2. 홈페이지 빌더 관리자에서 "새 문의 알림 메일" 수신 주소를 그 계정으로 지정.
3. 위 `.env` 채우고 CRM 재시작 → `homepage_inbox` 워커가 기동 시 1회 + 주기 폴링.

**동작** ([homepage_inbox.py](../homepage_inbox.py))
- UNSEEN 메일을 읽어 제목·본문에서 전화번호(정규식)·이름(이름/성함/성명 라벨) 추출.
- `communications`(채널=웹문의, 인바운드, open)로 등록, 전화번호로 환자 자동매칭.
- 성공한 메일만 읽음 처리 → 중복 등록 방지. 실패 시 다음 주기 재시도.
- 파싱이 완벽하지 않아도 **본문 전체가 인박스에 들어오고 알림이 뜨므로**, 상담사가
  읽고 `[상담 등록]`으로 전환하면 된다.

> 파싱 정확도를 높이려면 실제 빌더의 알림 메일 예시 1건이 필요하다
> (제목·본문 형식에 맞춰 파서를 조정).

---

## 5. 받은 뒤 흐름

1. 대시보드 **"미처리 인바운드"** 섹션에 채널별 탭(카카오채널/홈페이지/기타)으로 표시.
2. `[상담 등록]` — 연락처·메시지가 상담 폼에 prefill, 저장 시 자동 '처리완료'+연결.
3. `[완료]`/`[삭제]` — 상담화가 필요 없는 문의 정리.
4. 환자와 연결되면 환자 상세 **통합 타임라인**에 시간순으로 남는다.
