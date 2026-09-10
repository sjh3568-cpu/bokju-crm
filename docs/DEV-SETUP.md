# 개발 환경 만들기 — 두 번째 PC (병원 등)

집 노트북 말고 다른 PC에서도 기능을 만들고 확인하려면 이 문서대로 준비한다.
**개발은 어느 PC에서 해도 된다. 하지 말아야 할 것은 NAS 안의 파일을 직접 고치는 것뿐이다**
— 다음 배포 때 덮어써져 조용히 사라지고, 되돌릴 기록도 남지 않는다.

```
집 노트북 ──┐
            ├── GitHub ──→ NAS (운영)
병원 PC   ──┘
   (둘 다 개발용)          (여기만 운영)
```

## 준비물

| | 내용 |
|---|---|
| Python | 3.12 이상 ([python.org](https://www.python.org/downloads/) — 설치 시 **Add python.exe to PATH** 체크) |
| Git | [git-scm.com](https://git-scm.com/download/win) |
| GitHub 접근 권한 | 저장소가 비공개이므로 로그인 필요 |

## 설치 (Windows 기준, 한 번만)

명령 프롬프트(cmd)를 열고 순서대로.

### 1. 저장소 받기

```cmd
cd c:\Developer
git clone https://github.com/sjh3568-cpu/bokju-crm.git
cd bokju-crm
```

처음 받을 때 GitHub 로그인 창이 뜬다. 브라우저로 로그인하면 이후로는 묻지 않는다.

### 2. 파이썬 꾸러미 설치

```cmd
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

`.venv`는 이 PC 전용이라 GitHub에 올라가지 않는다. PC마다 따로 만든다.

### 3. `.env` 만들기

```cmd
copy .env.example .env
```

`.env`를 메모장으로 열어 두 줄을 채운다.

```ini
APP_PASSWORD=개발용비밀번호
SECRET_KEY=<아래 명령 결과를 붙여넣기>
```

`SECRET_KEY` 생성:

```cmd
python -c "import secrets; print(secrets.token_hex(32))"
```

> **운영(NAS)의 비밀번호를 쓰지 말 것.** 개발용은 아무 값이나 상관없다.
> `.env`는 GitHub에 올라가지 않으므로 PC마다 따로 만든다.

AI 자동채움 기능까지 확인하려면 `ANTHROPIC_API_KEY`도 채운다. 없어도 나머지 기능은 전부 동작한다.

### 4. 실행

```cmd
python app.py
```

브라우저에서 `http://127.0.0.1:8003`. 첫 실행 때 빈 DB와 계정이 자동으로 만들어진다
(어드민 + 상담사 4명, 초기 비밀번호는 `.env`의 `APP_PASSWORD`).

`python app.py`는 **내 PC에서만** 열린다. 공용 PC에서도 다른 사람이 접속할 수 없다.

## 데이터는 어떻게 할까

**빈 DB로 시작하는 것을 권한다.** 화면 배치·버튼·흐름 확인은 데이터가 없어도 대부분 된다.

실제 데이터가 있어야 확인되는 작업(통계·집계·표기 흔들림 등)은 집 노트북에서 하거나,
NAS 백업을 병원 PC로 가져와 쓴다. 다만 **환자 데이터가 든 PC가 늘어나는 만큼 위험도 늘어난다.**

- 공용 PC에는 환자 데이터를 두지 않는다
- 두더라도 로그인 걸린 개인 계정에서만 접근되게 한다
- 확인이 끝나면 지운다

`bokju.db`는 `.gitignore`에 걸려 있어 GitHub로는 절대 오가지 않는다. 옮긴다면 사람이 직접 복사하는 경우뿐이다.

## 매일 쓰는 순서

```cmd
git pull                  ← 작업 시작 전 (다른 PC에서 한 것 받아오기)
   ... 작업 ...
git add -A
git commit -m "무엇을 했는지"
git push                  ← 작업 끝나고 (올려두기)
```

**`git pull`을 빠뜨리면 두 PC가 서로 다른 방향으로 벌어진다.** 오래 방치할수록 합치기 어려워지므로
작업 시작 전에 습관처럼 받아온다.

## 운영에 반영하기

병원 PC는 NAS와 같은 망에 있으므로 **개발부터 배포까지 한자리에서 된다.**

```cmd
git pull
python -m unittest discover -s tests     테스트 통과 확인
```

이후 버전 확정과 NAS 적용 절차는 [DEPLOY-NAS.md](DEPLOY-NAS.md)의 '운영 배포' 참조.
`release.sh`는 리눅스용이므로 Windows에서는 그 문서의 검사 항목을 손으로 확인하고
`git tag -a v<버전> -m "복주 CRM <버전>"` → `git push origin v<버전>` 로 대신한다.

## 자주 막히는 곳

| 증상 | 원인·해결 |
|---|---|
| `python`을 찾을 수 없다 | 설치 때 PATH 체크를 빠뜨림. 재설치하거나 `py -3.12` 사용 |
| 포트 8003이 이미 사용 중 | 이 CRM이 이미 떠 있다. 기존 창을 닫거나 `.env`의 `PORT`를 바꾼다 |
| 화면을 고쳤는데 안 바뀐다 | `app.py`·`models.py`·`config.py`는 서버를 껐다 켜야 반영된다 (템플릿은 즉시) |
| `git push`가 거절된다 | 다른 PC에서 올린 작업이 있다. `git pull --rebase` 후 다시 push |
| 로그인이 안 된다 | `.env`의 `APP_PASSWORD`를 바꾼 뒤에는 기존 DB의 비밀번호가 그대로다. `admin` 계정은 매 기동 시 `.env` 값으로 동기화되므로 `admin`으로 로그인한다 |
