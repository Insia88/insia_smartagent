# 운영 안내 — 매주 쓰는 INSIA

이 문서는 INSIA 스마트에이전트를 **내 컴퓨터(또는 작은 서버)에 설치해 매주 콘텐츠를 만드는 분**을 위한 안내입니다. 개발 지식이 없어도 따라 할 수 있게 명령을 그대로 적었습니다. 명령은 터미널(Windows는 PowerShell, macOS는 터미널 앱)에 붙여 넣으면 됩니다.

INSIA가 하는 일은 셋입니다.

1. 회사 프로필과 참고 자료를 바탕으로 **한 주 콘텐츠 계획**을 세웁니다.
2. 계획한 날짜가 되면 총괄·리서치·검수 에이전트가 **초안을 쓰고 채점**합니다.
3. 사람이 **검토·승인한 뒤 직접 게시**하도록 붙여넣기용 파일을 만들어 줍니다.

> INSIA는 어떤 채널에도 **자동으로 게시하지 않습니다.** 게시 버튼은 언제나 사람이 누릅니다. "게시 완료" 표시는 내가 올렸다는 기록일 뿐입니다.

## 목차

1. [설치](#1-설치)
2. [API 키 설정](#2-api-키-설정)
3. [첫 설정: 프로필과 자료](#3-첫-설정-프로필과-자료)
4. [매주 운영 루틴](#4-매주-운영-루틴)
5. [비용 관리](#5-비용-관리)
6. [백업과 복원](#6-백업과-복원)
7. [업데이트](#7-업데이트)
8. [자동 실행 (cron · 작업 스케줄러)](#8-자동-실행-cron--작업-스케줄러)
9. [보안](#9-보안)
10. [문제 해결](#10-문제-해결)
11. [명령 한눈에 보기](#11-명령-한눈에-보기)

---

## 1. 설치

두 방법 중 하나를 고르세요. 혼자 내 PC에서 쓴다면 **파이썬 설치**가 가장 간단합니다. 늘 켜 둔 서버에서 팀이 함께 쓴다면 **Docker**가 편합니다.

### 1-1. 파이썬으로 설치 (Windows · macOS)

1. **파이썬 3.11 이상**을 설치합니다.
   - Windows: [python.org](https://www.python.org/downloads/)에서 받아 설치할 때 **"Add python.exe to PATH"를 꼭 체크**하세요.
   - macOS: python.org 설치 파일이나 `brew install python@3.11`.
2. **INSIA 파일을 받습니다.** GitHub 저장소에서 "Code → Download ZIP"으로 받아 원하는 곳(예: `문서/insia`)에 풀거나, git을 쓴다면 `git clone`합니다.
3. 그 폴더에서 터미널을 열고 **가상환경을 만든 뒤 설치**합니다.

   Windows (PowerShell):

   ```powershell
   cd $HOME\Documents\insia
   py -3.11 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -e ".[export,docs]"
   ```

   macOS:

   ```bash
   cd ~/Documents/insia
   python3.11 -m venv .venv
   source .venv/bin/activate
   pip install -e ".[export,docs]"
   ```

   `[export]`는 Word(.docx) 내보내기, `[docs]`는 PDF·Word 자료 읽기와 YAML 프로필 양식을 켭니다. 인스타그램 카드뉴스를 실제 PNG 이미지로 받고 싶으면 한 번 더 설치합니다(약 300MB).

   ```bash
   pip install -e ".[render]"
   python -m playwright install chromium
   ```

   없으면 카드뉴스는 인쇄용 `slides.html`로 나옵니다. 브라우저로 열어 한 장씩 캡처하면 됩니다.

4. **점검합니다.**

   ```bash
   insia doctor
   ```

   `[정상]`, `[주의]`, `[정보]`로 설치 상태, API 키, 워크스페이스 위치, 프로필, 선택 기능을 알려 줍니다. PowerShell에서 `Activate.ps1` 실행이 막히면 먼저 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`를 한 번 실행하세요.

   다음에 새 터미널을 열 때는 2번 폴더로 이동해 가상환경만 다시 켜면 됩니다(`.\.venv\Scripts\Activate.ps1` 또는 `source .venv/bin/activate`).

5. **대시보드를 엽니다.**

   ```bash
   insia serve
   ```

   브라우저에서 <http://127.0.0.1:8765>를 엽니다. 위쪽 메뉴에 **스튜디오 · 보관함 · 캘린더 · 브랜드·자료 · 사용량**이 있습니다. 끌 때는 터미널에서 `Ctrl+C`.

### 1-2. Docker로 설치 (늘 켜 두는 서버)

Docker Desktop(Windows·macOS) 또는 Linux의 Docker Engine + Compose가 필요합니다.

1. INSIA 폴더에 **`.env` 파일**을 만들고 값을 적습니다. 토큰은 `python -c "import secrets; print(secrets.token_urlsafe(32))"`로 만든 긴 무작위 문자열을 쓰세요.

   ```dotenv
   ANTHROPIC_API_KEY=sk-ant-...
   INSIA_ACCESS_TOKEN=여기에-만든-토큰
   INSIA_MAX_COST_USD=5
   ```

2. **Linux라면** 데이터 폴더의 주인을 컨테이너 사용자(10001)로 바꿔 둡니다. Docker Desktop은 필요 없습니다.

   ```bash
   mkdir -p workspace && sudo chown 10001:10001 workspace
   ```

3. **빌드하고 켭니다.**

   ```bash
   docker compose up -d --build
   ```

   <http://127.0.0.1:8765>를 열고 `.env`의 토큰으로 로그인합니다. 카드뉴스 PNG까지 원하면 `.env`에 `INSIA_WITH_RENDER=1`을 넣고 다시 빌드하세요(이미지가 약 600MB 커집니다).

   `docker compose stop`·`down`이나 업데이트(`up -d --build`)로 컨테이너가 멈출 때는 Ctrl+C를 누른 것처럼 정리돼요. 진행 중이던 실행은 다음 단계에서 멈추고 **멈춘 실행으로 저장**되며(대시보드 '멈춤', 터미널 '중단'), 끝낸 채널은 남아 있어서 나중에 대시보드나 `insia resume <실행 id>`로 이어서 할 수 있어요. `docker-compose.yml`의 `init: true`(신호를 제대로 전달하는 작은 init 프로세스)와 `stop_grace_period: 30s`(진행 중인 호출이 끝날 때까지 기다리는 여유)가 이 일을 해요. 두 줄을 지우지 마세요.

4. 컨테이너 안에서 명령을 쓸 때는 앞에 `docker compose exec insia`를 붙입니다.

   ```bash
   docker compose exec insia insia doctor
   docker compose exec insia insia items list
   ```

   데이터는 모두 호스트의 `./workspace` 폴더(컨테이너의 `/data`)에 쌓입니다. 파일을 주고받을 때도 이 폴더를 씁니다.

   ```bash
   # 자료 넣기: 호스트의 workspace/ 폴더에 파일을 복사한 뒤
   docker compose exec insia insia docs add /data/회사소개서.pdf
   # 프로필 양식: workspace/profile.yaml이 생겨요 → 호스트에서 편집 → 가져오기
   docker compose exec insia insia profile edit-template
   docker compose exec insia insia profile import /data/profile.yaml
   ```

   프로필과 자료는 대시보드 **브랜드·자료** 화면에서 넣는 것이 더 편합니다(PDF·Word는 위 명령으로).

## 2. API 키 설정

INSIA는 Anthropic의 Claude API로 리서치(웹 검색)·작성·검수를 합니다. [Anthropic 콘솔](https://console.anthropic.com/)에서 API 키를 만들어 환경 변수 `ANTHROPIC_API_KEY`에 넣습니다.

| 환경 | 방법 |
|---|---|
| Windows | PowerShell에서 `setx ANTHROPIC_API_KEY "sk-ant-..."` 실행 후 **터미널을 새로 엽니다** |
| macOS | `echo 'export ANTHROPIC_API_KEY="sk-ant-..."' >> ~/.zshrc` 후 터미널을 새로 엽니다 |
| Docker | `.env` 파일의 `ANTHROPIC_API_KEY=` |

- **키가 없으면 mock(데모) 모드**로 돕니다. API를 부르지 않아 무료이고, 결과에는 `[데모]` 표시와 `○○` 자리표시가 들어갑니다. 연습용으로 쓰세요.
- 키가 있으면 기본 **live 모드**입니다. 모드를 직접 정하려면 명령에 `--mode live` 또는 `--mode mock`을 붙입니다.
- 모델은 기본 `claude-opus-5`입니다. 바꾸려면 `INSIA_MODEL` 환경 변수나 `--model`을 씁니다.
- API 키는 다른 사람과 공유하지 말고, 파일에 적었다면 git에 올리지 마세요.

## 3. 첫 설정: 프로필과 자료

에이전트는 **회사 프로필**과 **참고 자료**를 "사용자가 준 사실"로 씁니다. 여기 적힌 만큼만 쓰고 부풀리지 않습니다. 처음 한 번 30분쯤 들여 채워 두면 매주 글이 회사에 맞춰집니다.

### 워크스페이스 위치

모든 데이터(프로필, 자료, 실행 기록, 보관함, 캘린더, 사용량)는 **워크스페이스 폴더** 하나에 저장됩니다.

- 기본: 명령을 실행한 폴더의 `workspace/` (INSIA 폴더에서 실행하면 `insia/workspace/`)
- 바꾸기: 환경 변수 `INSIA_HOME` 또는 명령마다 `--home <폴더>`
- `insia doctor`가 지금 쓰는 위치를 보여 줍니다. **자동 실행(cron·작업 스케줄러)은 다른 폴더에서 돌기 쉬우니 `INSIA_HOME`을 절대 경로로 정해 두길 권합니다.**

### 회사 프로필

대시보드의 **브랜드·자료** 화면에서 채우거나, 양식 파일로 채웁니다.

```bash
insia profile edit-template          # profile.yaml 양식이 생겨요 (지금 저장된 값이 채워진 채로)
# 메모장이나 텍스트 편집기로 profile.yaml을 열어 채우고 저장
insia profile import profile.yaml    # 저장
insia profile show                   # 확인 (채운 항목 수와 비어 있는 핵심 항목을 알려 줘요)
```

- 글은 `"큰따옴표"` 안에 적으면 콜론(`:`)이나 `#`이 있어도 안전합니다.
- 목록(차별점, 실적, 금지 표현 …)은 `- "내용"` 줄을 늘리거나 줄입니다.
- 특히 채우면 좋은 항목: **서비스 이름, 한 줄 소개, 목표 고객, 고객 문제, 해결 방법, 차별점, 브랜드 톤, 문의처, 금지 표현, 필수 문구**(예: 광고 표시).
- **팀원 실명은 사업계획서에 절대 나오지 않습니다**(블라인드 규정). 역할과 역량만 씁니다.
- 가격이 확정되지 않았다면 "가정"이라고 적어 두세요. 에이전트도 가정으로 표시합니다.
- 일부만 바꾸려면 바꿀 항목만 적은 파일로 `insia profile import 파일 --merge`.
- 백업·이전: `insia profile export --out profile-backup.json`.

### 참고 자료

회사 소개서, IR 자료, 보도자료, 서비스 설명서처럼 **사실이 적힌 자료**를 넣습니다.

```bash
insia docs add 회사소개서.pdf --title "회사 소개서 2026"
insia docs add 서비스설명.docx
insia docs add 보도자료.txt
insia docs list
```

- 읽을 수 있는 형식: `.txt` `.md` `.pdf` `.docx` (`.csv`도 텍스트로 읽어요). 한글(HWP) 파일은 한글에서 "다른 이름으로 저장 → PDF"로 바꿔 올리세요.
- 스캔한 이미지 PDF는 글자가 없어 거절됩니다. 글자를 복사해 `.txt`로 저장하거나 OCR을 거친 PDF를 올리세요.
- 자료는 실행할 때 리서치에 **"사용자 제공 자료"** 출처로 들어갑니다. 실행 1번에 모델로 보내는 자료는 모두 합쳐 60,000자까지이고, 넘는 부분은 잘라서 보내고 실행 기록에 알려 줍니다(`INSIA_MAX_DOCUMENT_CHARS`로 조절).
- 특정 자료만 쓰려면 `insia run --docs u1,u3`, 자료 없이 하려면 `--docs none`.

## 4. 매주 운영 루틴

| 언제 | 할 일 | 명령 | 대시보드 |
|---|---|---|---|
| 월요일 아침 | 한 주 계획 세우기 | `insia plan-week --theme "…"` (주말에도: `--weekend instagram`) | 캘린더 → 이번 주 계획 세우기 (주말에도 올리기 체크) |
| 매일 아침(자동) | 그날 올릴 초안 만들기 | `insia run-due` | 캘린더 → 초안 만들기 |
| 초안이 생기면 | 검토 · 수정 요청 · 승인 | `insia items show` · `revise` · `items approve` | 보관함 |
| 게시 직전 | 붙여넣기용 파일 받기 → 직접 게시 | `insia items export` | 보관함 → 내보내기 |
| 게시한 뒤 | 게시 완료 표시 | `insia items publish --url …` | 보관함 → 게시 완료 |

### ① 월요일: 계획 세우기

```bash
insia plan-week --theme "AI로 콘텐츠 마케팅 시간 줄이기" --blog 2 --linkedin 2 --instagram 2
```

- 기간은 기본 **이번 주 월요일부터 7일**(오늘이 월요일이 아니면 다음 월요일부터)이고, 게시일은 평일에 나눠 둡니다. `--start 2026-10-05 --days 7`로 바꿀 수 있습니다.
- **주말에도 올리고 싶은 채널**이 있으면 `--weekend instagram`(여러 개는 `--weekend blog,instagram`, 모두는 `--weekend all`)을 붙입니다. 대시보드에서는 계획 폼의 **"주말(토·일)에도 올리기"**에서 채널을 체크합니다. 기본은 모든 채널이 평일만이에요.
  - 주말 게시를 켰다면 **`insia run-due` 자동 실행을 매일 돌게** 바꿔 주세요. 평일에만 돌면 토·일 슬롯의 초안이 게시일이 지난 월요일에야 만들어집니다 → [8. 자동 실행](#8-자동-실행-cron--작업-스케줄러)의 "주말에도 올린다면".
- 같은 채널에 이미 계획이 있는 날은 비워 두고, 지난 게시물·기존 계획과 겹치는 주제는 넣지 않습니다. 그래서 요청한 개수보다 적게 계획될 수 있고, 그 이유는 계획 결과에 안내로 나옵니다.
- 채널 개수를 하나도 안 적으면 블로그 2 · 링크드인 2 · 인스타그램 2편입니다.
- 테마를 비우면 회사 프로필로 주제를 정합니다. 이미 게시한 주제는 피합니다(그래서 ⑤ "게시 완료 표시"가 중요합니다).
- 마음에 들지 않으면 `--replace`로 다시 짭니다. 개별 슬롯은 `insia calendar move <슬롯> --date …`, `insia calendar skip <슬롯>`.
- 계획 확인: `insia calendar list`.

### ② 초안 만들기

```bash
insia run-due                 # 오늘까지 예정된 슬롯의 초안
insia run-due --until tomorrow --limit 3
```

- 슬롯마다 리서치 → 작성 → 검수 → (필요하면 최대 2번) 수정을 거쳐 보관함에 넣습니다. 한 편에 몇 분 걸립니다.
- 매일 아침 자동으로 돌게 해 두면 편합니다 → [8. 자동 실행](#8-자동-실행-cron--작업-스케줄러).
- 계획 없이 바로 만들 때는 `insia run --topic "…" --channels naver_blog,linkedin`.
- Claude Code에서 `/content-studio` 등으로 만든 결과는 `insia import-run outputs/<날짜>-<주제>`로 보관함에 가져옵니다(같은 폴더를 다시 가져와도 중복되지 않고 갱신됩니다).

### ③ 검토 · 수정 요청 · 승인

```bash
insia items list                       # 보관함 (상태: 초안 · 수정 필요 · 승인 · 게시 예정 · 게시 완료)
insia items show <id>                  # 버전, 검수 점수·이슈, 본문
insia revise <id> -i "첫 문장을 질문으로 바꾸고 사례를 하나 더 넣어 주세요"
insia review <id>                      # 직접 고친 뒤 다시 채점
insia items approve <id>
```

- id는 길어서 **겹치지 않는 일부만** 적어도 됩니다(예: `items show 0928-1015_linkedin`).
- 직접 문장을 고치고 싶으면 대시보드 **보관함 → 편집**이 가장 편합니다. 저장하면 글자 수·형식 검사를 바로 다시 해 줍니다.
- **에이전트가 아직 그 콘텐츠를 쓰는 동안에는 저장이 거절돼요**(HTTP 409). 그 콘텐츠를 만든 실행이 아직 돌고 있거나, 그 콘텐츠에 재검수·수정 요청이 진행 중일 때예요. 고친 내용은 편집창과 브라우저에 그대로 남으니, 작업이 끝난 뒤 최신 버전을 확인하고 다시 저장하세요(그 사이 새 버전이 생겨도 고치던 내용을 되살려 줘요).
- 사람이 고친 버전은 에이전트 결과에 **묻히지 않아요.** 멈춘 실행을 고친 뒤 이어서 실행하거나, 터미널에서 돌던 실행(`insia run`·`resume`·`run-due`)이 끝나기 전에 대시보드에서 고쳐도, 에이전트가 그 뒤에 만든 결과는 버전 기록에만 남고 사람이 고친 내용이 현재 버전으로 유지돼요. 승인·게시 예정·게시 완료로 바꿔 둔 콘텐츠도 마찬가지예요. 터미널 실행이 도는 동안 대시보드에서 수정 요청을 보낸 경우에도 수정 결과가 현재 버전으로 남고, 재검수 점수도 그 버전에 붙어서 바로 승인할 수 있어요(터미널 실행의 결과는 기록에만 남아요). 실행 기록에 "사람이 고친 버전이 있어서 … 기록에만 남기고"라는 안내가 남고, 재검수·수정 요청이면 대시보드에 같은 안내가 떠요.
- 승인은 **최신 버전이 검수를 통과(80점 이상, 치명적 이슈 없음)**해야 됩니다. 내용을 직접 확인했다면 `--force`(대시보드: "그래도 승인")로 승인할 수 있습니다. 이렇게 승인한 콘텐츠는 보관함 목록과 상세 화면에 **"강제 승인"** 표시가 붙고(승인한 버전과 그때 점수도 남아요), 게시 예정·게시 완료로 넘어가도 표시가 남아요.
- 승인 전에 꼭 확인할 것: `[대표자 성명]`·`[확인 필요: …]`·`○○` 같은 **자리표시를 모두 채웠는지**, 핵심 수치의 **원문 출처**를 열어 봤는지, 과장 표현·금지 표현이 없는지.

### ④ 내보내기와 게시

```bash
insia items export <id>                # 채널별 추천 형식으로 워크스페이스 exports/ 폴더에 저장
insia items export <id> --format md --out $HOME/Desktop   # 원하는 폴더에
```

| 채널 | 추천 형식 | 쓰는 법 |
|---|---|---|
| 사업계획서 | `.docx` | Word·한글에서 열어 공고 양식에 옮겨 다듬어요 |
| 네이버 블로그 | `.html` | 브라우저로 열어 전체 선택·복사 → 스마트에디터에 붙여넣기. 이미지 자리에 사진을 넣어요 |
| 링크드인 | `.txt` | 그대로 붙여넣기. 링크는 첫 댓글에 |
| 인스타그램 | `.zip` | 카드 이미지(slide-01.png …) + 캡션(caption.txt) + 대체텍스트 |

### ⑤ 게시 완료 표시

직접 올린 뒤 주소와 함께 표시합니다. 다음 주 계획이 같은 주제를 피하는 데 쓰입니다.

```bash
insia items publish <id> --url https://blog.naver.com/...
insia items schedule <id> --date 2026-10-07     # 올릴 날을 미리 표시해 둘 때
insia items archive <id>                        # 쓰지 않을 초안은 보관 (되돌리기: items restore)
```

## 5. 비용 관리

- **mock 모드는 무료**입니다. live 모드는 사용한 토큰과 웹 검색 횟수만큼 Anthropic에 요금이 나갑니다.
- **예산 상한**: 실행 1번(초안 한 묶음, 재검수, 수정 요청 하나하나)의 상한을 달러로 정합니다.

  ```bash
  # 늘 적용: 환경 변수 (Windows: setx INSIA_MAX_COST_USD 5)
  export INSIA_MAX_COST_USD=5
  # 이번 한 번만
  insia run --topic "…" --max-cost-usd 3
  ```

  상한을 넘으면 새 API 호출을 멈추고 "예산 상한 $X를 넘어 실행을 멈췄어요"라고 알려 줍니다. 끝난 채널은 저장돼 있고, 상한을 올려 `insia resume <실행 id> --max-cost-usd 6`으로 남은 작업만 이어서 할 수 있습니다. (이미 진행 중이던 호출은 끝까지 가므로 최종 비용이 상한을 조금 넘을 수 있어요.)
- **사용량 보기**: `insia usage`(이번 달), `insia usage --since 2026-09-01 --until 2026-09-30`, 대시보드 **사용량** 화면. 날짜별·실행별·작업별 비용이 나옵니다.
- **가격표**: 모델 가격은 바뀔 수 있습니다. 기본값은 코드(`src/insia_agents/costs.py`)에 있고, 워크스페이스에 `prices.json`을 두면 그 값을 씁니다(USD / 100만 토큰).

  ```json
  {"claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
   "web_search_usd_per_1k": 10.0}
  ```

  환경 변수로도 바꿀 수 있습니다: `INSIA_PRICE_CLAUDE_OPUS_5_INPUT=5`, `INSIA_PRICE_WEB_SEARCH_PER_1K=10`. 가격을 모르는 모델은 비용이 0으로 잡혀 **예산 상한이 작동하지 않으니** 꼭 추가하세요(`insia doctor`가 알려 줍니다).
- **아끼는 요령**: `run-due --limit 3`으로 하루 개수를 제한하고, 꼭 필요한 채널만 계획하고, 자료는 핵심만 넣으세요.

## 6. 백업과 복원

모든 데이터는 **워크스페이스 폴더 하나**에 있습니다.

```
workspace/
  insia.db        프로필, 자료, 실행 기록, 보관함, 캘린더, 사용량 (SQLite)
  insia.db-wal    DB가 쓰는 중인 내용 (있으면 함께 복사)
  exports/        내보낸 파일
  uploads/        원본 자료 파일
  logs/           로그
  prices.json     (있다면) 가격표
```

- **백업**: `insia serve`와 자동 실행을 잠깐 멈춘 뒤 `workspace` 폴더를 통째로 복사합니다(USB, 클라우드 드라이브 등). Docker는 `docker compose stop` 후 `./workspace`를 복사하고 `docker compose start`.
- 서버를 끄기 어렵다면 SQLite 도구로 안전한 복사본을 만듭니다: `sqlite3 workspace/insia.db ".backup 'insia-backup.db'"`.
- **복원**: 서버를 끄고 `workspace` 폴더를 백업본으로 바꿔 넣은 뒤 다시 켭니다.
- 프로필만 따로: `insia profile export --out profile.json` / `insia profile import profile.json`.
- 실행 한 건의 결과 전체(채널 파일·출처 목록·리서치): `insia runs export <실행 id>`.

## 7. 업데이트

업데이트 전에 [백업](#6-백업과-복원)을 먼저 하세요. DB 구조가 바뀌면 처음 열 때 자동으로 올라가고(되돌릴 수 없음), 실패하면 변경 없이 멈춥니다.

- **파이썬 설치**: 새 ZIP으로 파일을 덮어쓰거나 `git pull` 한 뒤, 가상환경을 켜고 `pip install -e ".[export,docs]"`를 다시 실행합니다. `insia doctor`로 확인합니다.
- **Docker**: `git pull && docker compose up -d --build`. 이때 진행 중이던 실행은 깔끔하게 멈춘 실행으로 저장되고, 새 컨테이너가 뜬 뒤 대시보드나 `docker compose exec insia insia resume <실행 id>`로 이어서 할 수 있어요([1-2](#1-2-docker로-설치-늘-켜-두는-서버) 참고). 급하지 않으면 실행이 끝난 뒤 업데이트하세요.
- "이 워크스페이스는 더 새 버전의 INSIA로 만들어졌어요"가 나오면 INSIA를 최신으로 올리세요(옛 버전으로 새 DB를 열 수 없습니다).

## 8. 자동 실행 (cron · 작업 스케줄러)

`insia run-due`는 사람 입력 없이 돌도록 만들었습니다.

- 만들 초안이 없으면 **종료 코드 0**, 하나라도 실패하면 **1**을 돌려줍니다.
- API 키가 없으면 데모 초안을 몰래 만들지 않고 오류(종료 코드 1)로 알려 줍니다.
- `--limit`으로 하루 최대 개수를, `INSIA_MAX_COST_USD`로 편당 상한을 정해 두세요.
- 대시보드 서버와 동시에 돌아도 됩니다(같은 슬롯을 두 번 만들지 않아요).
- 자동 실행은 다른 폴더에서 시작되므로 **`INSIA_HOME`을 절대 경로로** 정하세요.

### macOS · Linux (cron)

1. 키와 설정을 파일 하나에 모읍니다: `~/.insia.env`

   ```bash
   export ANTHROPIC_API_KEY="sk-ant-..."
   export INSIA_HOME="$HOME/Documents/insia/workspace"
   export INSIA_MAX_COST_USD=5
   ```

2. `crontab -e`로 아래 줄을 넣습니다(평일 아침 7시 50분).

   ```cron
   50 7 * * 1-5 . $HOME/.insia.env; $HOME/Documents/insia/.venv/bin/insia run-due --limit 3 --quiet >> $HOME/Documents/insia/workspace/logs/run-due.log 2>&1
   ```

   macOS에서 cron이 문서 폴더에 접근하지 못하면 "시스템 설정 → 개인정보 보호 및 보안 → 전체 디스크 접근 권한"에 `/usr/sbin/cron`을 추가하세요.

   **주말에도 올린다면**(계획할 때 `--weekend`나 "주말에도 올리기"를 켰다면) 요일 칸을 `*`로 바꿔 매일 돌게 합니다: `50 7 * * * . $HOME/.insia.env; …`

### Windows (작업 스케줄러)

1. INSIA 폴더에 `run-due.bat` 파일을 만듭니다.

   ```bat
   @echo off
   chcp 65001 > nul
   set PYTHONUTF8=1
   set INSIA_HOME=%USERPROFILE%\Documents\insia\workspace
   "%USERPROFILE%\Documents\insia\.venv\Scripts\insia.exe" run-due --limit 3 --quiet >> "%INSIA_HOME%\logs\run-due.log" 2>&1
   ```

   API 키는 [2. API 키 설정](#2-api-키-설정)의 `setx`로 사용자 환경 변수에 넣어 두면 됩니다.

2. PowerShell에서 등록합니다(평일 아침 7시 50분).

   ```powershell
   schtasks /Create /TN "INSIA run-due" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 07:50 /TR "$HOME\Documents\insia\run-due.bat"
   ```

   (사용자 폴더 이름에 공백이 있으면 아래 화면 방식으로 등록하세요.) 또는 "작업 스케줄러 → 기본 작업 만들기 → 매주 → 월~금 → 프로그램 시작 → run-due.bat"을 골라도 됩니다. PC가 꺼져 있으면 돌지 않으니, 작업 속성에서 "예약된 시작 시간을 놓친 경우 가능한 대로 빨리 작업 시작"을 켜 두세요.

   **주말에도 올린다면** 매일 돌게 등록합니다(이미 등록했다면 `/F`로 덮어써요).

   ```powershell
   schtasks /Create /F /TN "INSIA run-due" /SC DAILY /ST 07:50 /TR "$HOME\Documents\insia\run-due.bat"
   ```

### Docker

호스트의 cron에서 컨테이너 안의 명령을 부릅니다.

```cron
50 7 * * 1-5 cd /srv/insia && docker compose exec -T insia insia run-due --limit 3 --quiet >> /srv/insia/workspace/logs/run-due.log 2>&1
```

주말에도 올린다면 `1-5`를 `*`로 바꿔 매일 돌게 합니다.

## 9. 보안

- **기본은 내 컴퓨터 전용**입니다. `insia serve`는 `127.0.0.1`에만 열려 같은 PC에서만 접속됩니다. Docker 설정도 `127.0.0.1:8765`로만 열어 둡니다.
- **다른 기기에서 쓰려면 접근 토큰이 꼭 필요합니다.** `--host 0.0.0.0`처럼 바깥에 열면 토큰 없이는 서버가 시작되지 않습니다.

  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(32))"   # 토큰 만들기
  export INSIA_ACCESS_TOKEN="만든-토큰"                           # Windows: setx INSIA_ACCESS_TOKEN "만든-토큰"
  insia serve --host 0.0.0.0
  ```

  대시보드는 처음에 토큰을 물어보고, 로그인하면 브라우저에 쿠키(`insia_token`, HttpOnly)로 기억합니다. 스크립트는 `Authorization: Bearer <토큰>` 헤더를 씁니다. 로그인을 여러 번 틀리면 잠시 막힙니다. 토큰은 명령 기록에 남지 않도록 `--token`보다 환경 변수를 쓰세요.
- **인터넷에 열 때는 HTTPS 리버스 프록시 뒤에** 두세요. 토큰이 암호화 없이 오가면 안 됩니다. 예: Caddy(인증서를 자동으로 받아요)

  ```caddyfile
  insia.example.com {
      reverse_proxy 127.0.0.1:8765
  }
  ```

  그리고 INSIA에 도메인과 프록시를 알려 줍니다. 프록시를 거치면 `127.0.0.1`로 열어도 바깥에서 들어올 수 있으니 **접근 토큰도 꼭** 설정하세요. `--public-host`나 `--trust-proxy`(`INSIA_TRUST_PROXY=1`)를 토큰 없이 쓰면 서버가 시작하지 않아요.

  ```bash
  export INSIA_ACCESS_TOKEN="만든-토큰"
  insia serve --host 127.0.0.1 --public-host insia.example.com --trust-proxy
  ```

  Docker는 `.env`에 `INSIA_PUBLIC_HOSTS=insia.example.com`, `INSIA_TRUST_PROXY=1`을 적습니다(토큰은 이미 필수예요).
- 프록시에 비밀번호를 한 겹 더 걸어도 됩니다(nginx `auth_basic`, Caddy `basicauth`). 프록시가 넘기는 `Authorization: Basic …` 헤더는 INSIA가 무시하고 로그인 쿠키로 인증해요.
- 로그인 제한은 IP마다 1분에 10번이에요(IPv6는 /64 네트워크마다). 연결을 여러 개 열어 한꺼번에 시도해도, IPv6 주소를 바꿔 가며 시도해도 10번보다 많이 확인하지 않아요. 토큰은 사람이 정한 짧은 말보다 `secrets.token_urlsafe(32)`로 만든 긴 값을 쓰세요.
- IPv6로도 열 수 있어요: `--host ::1`은 이 컴퓨터에서만, `--host ::`은 모든 주소(토큰 필요)예요. IPv6가 꺼진 컴퓨터에서는 시작할 때 `--host 127.0.0.1`을 쓰라고 알려 줘요.
- 이상한 요청(아주 긴 주소, 본문을 보내다 만 요청, 중간에 끊긴 연결)은 404·408 같은 답만 하고 오류 로그를 남기지 않아요. 로그에 오류가 쌓인다면 실제 문제이니 확인해 주세요.
- 같은 네트워크(사무실 와이파이)에서만 쓸 때도 토큰은 필요합니다. 공용 와이파이에서는 바깥에 열지 마세요.
- **API 키·토큰**은 `.env`나 환경 변수에만 두고 git·메신저에 올리지 마세요. 새어 나갔다면 Anthropic 콘솔에서 키를 지우고 새로 만드세요.
- **개인정보**: 팀원 실명은 사업계획서에 쓰지 않도록 검사합니다(블라인드). 고객 개인정보가 든 자료는 넣지 마세요. 자료와 초안은 워크스페이스(내 PC)에 저장되고, live 모드에서 작성에 필요한 부분이 Anthropic API로 전송됩니다.
- 자세한 API와 인증 규칙은 [api.md](api.md)를 보세요.

## 10. 문제 해결

먼저 `insia doctor`를 실행해 보세요. 설치·키·워크스페이스·선택 기능을 한 번에 점검합니다. 오류가 나면 명령은 한국어로 이유를 알려 주고 종료 코드 1(작업 실패) 또는 2(명령을 잘못 씀)를 돌려줍니다.

| 메시지 / 증상 | 원인 | 해결 |
|---|---|---|
| 결과에 `[데모]`와 `○○`가 보여요 · "API 키가 없어서 mock 모드로 실행해요" | API 키를 못 찾음 | [2. API 키 설정](#2-api-키-설정) 후 **새 터미널**에서 다시 실행 |
| "API 키가 없어서 데모(mock) 초안만 만들 수 있어요" (run-due) | 자동 실행 환경에 키가 없음 | cron은 `~/.insia.env`, Windows는 `setx`로 키 설정 |
| `insia`를 찾을 수 없다고 나와요 | 가상환경이 꺼져 있음 | INSIA 폴더에서 `.venv`를 다시 켜기 ([1-1](#1-1-파이썬으로-설치-windows--macos)) |
| 보관함이 비어 있어요 / 어제 만든 게 안 보여요 | 다른 폴더의 워크스페이스를 봄 | `insia doctor`로 위치 확인, `INSIA_HOME`을 절대 경로로 |
| "다른 기기에서 접속할 수 있는 주소…접근 토큰이 필요해요" | `--host 0.0.0.0`인데 토큰 없음 | `INSIA_ACCESS_TOKEN` 설정 ([9. 보안](#9-보안)) |
| "도메인(--public-host / INSIA_PUBLIC_HOSTS)으로 열면…접근 토큰이 꼭 필요해요" | 리버스 프록시 뒤인데 토큰 없음 | `INSIA_ACCESS_TOKEN` 설정 |
| "리버스 프록시(--trust-proxy / INSIA_TRUST_PROXY) 뒤에서 열면 … 접근 토큰이 꼭 필요해요" | 프록시 옵션을 켰는데 토큰 없음 (프록시를 거치면 바깥에서 들어올 수 있어요) | `INSIA_ACCESS_TOKEN` 설정 ([9. 보안](#9-보안)). 프록시를 쓰지 않는다면 `--trust-proxy`·`INSIA_TRUST_PROXY`를 빼기 |
| "이 컴퓨터에서는 IPv6 주소(…)로 서버를 열 수 없어요" | IPv6가 꺼진 컴퓨터에서 `--host ::1`·`::` | `--host 127.0.0.1`로 열기 (`insia healthcheck`도 같은 `--host`) |
| "접근 토큰이 너무 짧아요" | 토큰이 12자 미만 | `secrets.token_urlsafe(32)`로 새로 만들기 |
| 대시보드가 계속 로그인 화면이에요 | 토큰이 다름 / 서버 토큰을 바꿈 | 서버에 설정한 토큰을 그대로 입력 |
| "서버를 시작하지 못했어요 … Address already in use" | 8765 포트를 이미 씀 | 켜 둔 `insia serve`를 끄거나 `--port 8766` |
| "예산 상한 $X를 넘어 실행을 멈췄어요" | 편당 예산 초과 | `insia resume <실행 id> --max-cost-usd <더 큰 값>` |
| "다른 곳에서 아직 실행 중인 작업이에요 (…)" | 다른 프로그램(터미널의 `insia run`·`run-due`, 대시보드 서버, 다른 컴퓨터·컨테이너)이 그 실행을 아직 하고 있음 | 끝날 때까지 기다리세요. 그 프로그램이 이미 꺼졌다면 `insia resume`·`run-due`·`serve`가 알아서 '중단됨'으로 정리해요(프로세스가 사라졌거나 컴퓨터를 다시 켰으면 바로, 신호가 10분 넘게 끊기면 그때). 정말 멈춘 게 확실하면 `insia resume <실행 id> --force` |
| "에이전트가 아직 이 콘텐츠를 쓰고 검수하는 중이에요" · "에이전트가 이 콘텐츠를 수정하는 중이에요" (편집 저장) | 그 콘텐츠의 실행이나 재검수·수정 요청이 아직 진행 중 (409) | 고친 내용은 편집창에 남아 있어요. 작업이 끝나면 최신 버전을 확인하고 다시 저장 |
| "최신 버전(vN)이 검수를 통과하지 못했어요" | 승인 조건 미달 | `insia revise <id> -i "…"`로 고치거나, 직접 확인했다면 `items approve <id> --force` |
| "'초안' 상태에서 '게시 완료'(으)로 바꿀 수 없어요. 먼저 승인해 주세요." | 승인 전에 게시 표시 | `insia items approve <id>` 먼저 |
| "PDF에서 글자를 찾지 못했어요" | 스캔한 이미지 PDF | 글자를 복사해 `.txt`로 올리거나 OCR PDF 사용 |
| "PDF를 읽으려면 pypdf가 필요해요" / "python-docx가 필요해요" | 선택 기능 미설치 | `pip install -e ".[docs]"` |
| "YAML 형식이 올바르지 않아요 (N번째 줄 근처)" | 따옴표 없이 콜론·# 사용 | 그 줄의 글을 `"큰따옴표"`로 감싸기 |
| "YAML 파일을 읽으려면 PyYAML이 필요해요" | 선택 기능 미설치 | `pip install -e ".[docs]"` 또는 `insia profile edit-template --format json` |
| 인스타그램 내보내기에 PNG 대신 `slides.html`만 있어요 | Playwright·Chromium 없음 | `pip install -e ".[render]"` + `python -m playwright install chromium` (Docker: `INSIA_WITH_RENDER=1`) |
| 카드뉴스 글자가 네모로 깨져요 | 한글 글꼴 없음 (Linux) | `sudo apt install fonts-noto-cjk` |
| "동시에 실행할 수 있는 작업 수…를 넘었어요" | 대시보드에서 작업을 한꺼번에 많이 시작 | 진행 중인 작업이 끝난 뒤 다시 |
| "이 워크스페이스는 더 새 버전의 INSIA로 만들어졌어요" | 옛 버전으로 새 DB를 엶 | [7. 업데이트](#7-업데이트) |
| Windows에서 한글이 깨져 보여요 | 콘솔 인코딩 | `chcp 65001` 후 다시, 또는 환경 변수 `PYTHONUTF8=1` |
| Docker: `/data`에 쓸 수 없다는 오류 | 호스트 폴더 권한 (Linux) | `sudo chown -R 10001:10001 workspace` |
| 예상하지 못한 오류 | 버그일 수 있어요 | `INSIA_DEBUG=1`을 설정하고 다시 실행해 자세한 내용을 개발자에게 전달 |

## 11. 명령 한눈에 보기

모든 명령은 `-h`로 한국어 도움말을 봅니다(예: `insia run-due -h`). 스크립트용으로 `--json`을 붙이면 JSON만 출력합니다.

| 명령 | 하는 일 |
|---|---|
| `insia doctor` | 설치·설정 점검 |
| `insia serve` | 대시보드 (`--host`, `--port`, `--token`, `--public-host`, `--trust-proxy`; 바깥·프록시에 열면 토큰 필수) |
| `insia profile show / edit-template / import <파일> / export` | 회사 프로필 |
| `insia docs add <파일> / list / show <id> / rm <id>` | 참고 자료 |
| `insia plan-week --theme … [--start] [--days] [--blog N --linkedin N --instagram N] [--weekend 채널] [--replace]` | 한 주 계획 (기본 평일만, `--weekend`로 채널별 주말 허용) |
| `insia calendar list / generate <슬롯> / skip <슬롯> / move <슬롯> --date` | 캘린더 |
| `insia run-due [--until] [--limit] [--dry-run]` | 예정된 초안 만들기 (자동 실행용) |
| `insia run --topic … [--channels] [--docs] [--max-cost-usd] [--no-profile]` | 바로 실행 |
| `insia items list / show / approve / schedule / publish / archive / restore / export` | 보관함 |
| `insia review <id>` · `insia revise <id> -i "…"` | 재검수 · 수정 요청 |
| `insia resume <실행 id> [--max-cost-usd] [--force]` | 멈춘 실행 이어서 |
| `insia runs list [--item <콘텐츠 id>] / show / export` | 실행 기록 (`--item`: 그 콘텐츠에 돌린 작업만) |
| `insia usage [--since] [--until]` | 사용량·비용 |
| `insia import-run <폴더>` | Claude Code 실행 폴더 가져오기 |
| `insia check <draft.json>` | 초안 형식 검사 (프로필 규칙 포함) |
| `insia healthcheck [--host] [--port]` | 서버 상태 확인 (Docker용) |

종료 코드: `0` 성공 · `1` 작업 실패 · `2` 명령을 잘못 씀 · `130` 중단(Ctrl+C).
