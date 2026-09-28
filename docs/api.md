# HTTP API 레퍼런스

`insia serve`가 여는 서버의 API입니다. 대시보드(`web/`)는 이 API만으로 동작하고, 스크립트나 다른 도구도 같은 API를 쓸 수 있습니다. 서버 코드는 `src/insia_agents/server.py`, 이벤트 형식은 [event-schema.md](event-schema.md)에 있습니다.

- 기본 주소: `http://127.0.0.1:8765` (`insia serve --host/--port`로 바꿉니다)
- 모든 데이터는 워크스페이스(`INSIA_HOME`, 기본 `./workspace`)의 SQLite에 저장됩니다. 서버를 다시 켜도 실행 기록, 이벤트, 콘텐츠, 캘린더가 그대로 남습니다.
- 요청과 응답은 JSON(UTF-8)입니다. 파일 내려받기와 SSE만 예외입니다.
- 오류는 항상 `{"error": "<한국어 문장>", "status": <코드>}` 모양이고, 경우에 따라 필드가 더 붙습니다. `error`는 화면에 그대로 띄워도 되는 문장입니다.

## 목차

1. [공통 규칙](#공통-규칙)
2. [접근 토큰 (인증)](#접근-토큰-인증)
3. [상태](#상태) — health, sample-brief
4. [회사 프로필](#회사-프로필)
5. [참고 자료](#참고-자료)
6. [실행](#실행) — 시작, 목록, 상세, SSE, 이어서 실행, 중단, 묶음 내려받기
7. [콘텐츠 보관함](#콘텐츠-보관함) — 목록, 상세, 직접 수정, 재검수, 수정 요청, 상태 변경, 내보내기
8. [캘린더](#캘린더)
9. [사용량](#사용량)
10. [파이썬에서 서버 띄우기](#파이썬에서-서버-띄우기)

---

## 공통 규칙

### 요청 보안

| 규칙 | 적용 대상 | 어기면 |
|---|---|---|
| `Host` 헤더가 허용된 이름이어야 함: `127.0.0.1`, `localhost`, `::1`, 서버가 바인드한 주소, `--public-host`로 추가한 도메인. `0.0.0.0`으로 열었으면 IP 주소도 허용 | 모든 `/api` | 403 |
| `Origin`을 보냈다면 요청한 `Host`와 스킴·이름·포트가 정확히 같아야 함 (`http://`, 신뢰하는 HTTPS 프록시 뒤에서는 `https://`) | POST · PUT · DELETE | 403 |
| `Sec-Fetch-Site`를 보냈다면 `same-origin` 또는 `none`이어야 함 | POST · PUT · DELETE | 403 |
| `Content-Type: application/json` | POST · PUT (본문이 없어도) | 415 |
| 접근 토큰 (토큰 모드일 때) | `/api/login`, `/api/logout`을 뺀 모든 `/api` | 401 |

다른 사이트의 페이지는 JSON 요청을 보내려면 CORS 사전 요청(preflight)을 거쳐야 하는데, 이 서버는 사전 요청을 절대 허락하지 않습니다. 그래서 로그인한 브라우저라도 다른 사이트가 대신 실행·승인·삭제를 할 수 없습니다. 정적 파일(대시보드 화면)은 토큰 없이도 열리고, `X-Frame-Options: DENY`로 다른 사이트에 끼워 넣을 수 없습니다.

### 본문 크기

| 경로 | 최대 |
|---|---|
| `POST /api/documents` | 8MB (텍스트는 200만 자까지 — 한글 200만 자가 UTF-8로 약 6MB) |
| `PUT /api/items/<id>/draft` | 1MB (본문은 10만 자까지) |
| `PUT /api/profile` | 256KB |
| `POST /api/login` · `/api/logout` | 4KB |
| 나머지 JSON | 64KB |

`Content-Length`가 필요합니다(chunked 전송은 411). 한도를 넘으면 본문을 읽기 전에 413으로 답합니다. `NaN`, `Infinity`, 너무 깊게 중첩된 JSON은 400입니다.

### 동시 작업 수

실행과 작업(재검수·수정 요청·슬롯 초안·이어서 실행)은 백그라운드에서 돕니다. 한 번에 **live 2개, mock 4개**까지이고, 넘으면 `429`와 `Retry-After: 10`을 돌려줍니다. 캘린더 계획(`POST /api/calendar/plan`)도 도는 동안 한 자리를 씁니다. 환경 변수 `INSIA_MAX_LIVE_JOBS`, `INSIA_MAX_MOCK_JOBS`로 바꿀 수 있습니다.

```json
{"error": "동시에 실행할 수 있는 작업 수(live 2개)를 넘었어요. 진행 중인 작업이 끝난 뒤 다시 시도해 주세요.", "status": 429}
```

같은 콘텐츠에 재검수·수정 요청이 이미 돌고 있거나, 같은 캘린더 슬롯이 초안을 만드는 중이면 `409`와 진행 중인 `run_id`를 돌려줍니다.

### 상태 코드

| 코드 | 뜻 |
|---|---|
| 200 | 성공 |
| 201 | 새 실행·작업·자료·슬롯을 만들었어요 |
| 202 | 요청을 받았고 백그라운드에서 진행해요 (이어서 실행, 중단) |
| 204 | SSE: 더 보낼 이벤트가 없어요 (EventSource가 재연결을 멈춤) |
| 400 | 입력이 잘못됐어요 |
| 401 | 로그인이 필요해요 (`"login": true`) |
| 403 | 허용되지 않은 Host·Origin |
| 404 | 없는 경로 또는 없는 id |
| 405 | 그 경로에서 지원하지 않는 메서드 (`Allow` 헤더 참고) |
| 409 | 지금 상태에서는 할 수 없어요 (승인 차단, 잘못된 상태 전환, 이미 실행 중 등) |
| 413 · 415 · 411 | 본문이 너무 큼 · JSON이 아님 · Content-Length 없음 |
| 429 | 동시 작업 수 초과, 또는 로그인 실패가 너무 많음 (`Retry-After`) |
| 501 | 선택 설치 패키지가 없음 (예: Word 내보내기에 python-docx 필요) |
| 502 | AI 백엔드(Anthropic API) 호출 실패 |

---

## 접근 토큰 (인증)

기본 실행(`insia serve`)은 내 PC(`127.0.0.1`)에서만 열리고 토큰이 없습니다. 다른 기기에서 접속하려면 토큰이 **반드시** 필요합니다.

- `--host 0.0.0.0`처럼 루프백이 아닌 주소로 열 때 토큰이 없으면 서버가 시작하지 않습니다.
- 토큰은 `INSIA_ACCESS_TOKEN` 환경 변수나 `--token`으로 정합니다. 12자 이상, 공백 없는 영문·숫자·기호만 됩니다. 예: `python -c "import secrets; print(secrets.token_urlsafe(24))"`
- 토큰 모드에서는 `/api/login`, `/api/logout`을 뺀 모든 `/api` 요청에 아래 중 하나가 필요합니다.
  - `Authorization: Bearer <토큰>` 헤더 (스크립트용)
  - `insia_token` 쿠키 (`POST /api/login`이 설정, 브라우저용. SSE `EventSource`도 이 쿠키로 인증돼요)
- 인증이 없으면 `/api/health`를 포함해 모두 `401`과 `{"login": true}`를 돌려줍니다. 대시보드는 이걸 보고 로그인 화면을 띄웁니다. 없는 경로도 로그인 전에는 404가 아니라 401입니다.
- 토큰 비교는 상수 시간(`hmac.compare_digest`)으로 합니다.
- 틀린 토큰(로그인, Bearer, 쿠키 모두)은 클라이언트 IP마다 **1분에 10번**까지입니다. 넘으면 맞는 토큰이라도 잠시 `429`(`Retry-After` 초)입니다.

### 쿠키

```
Set-Cookie: insia_token=<세션 값>; Path=/; Max-Age=2592000; HttpOnly; SameSite=Strict[; Secure]
```

- 쿠키에는 토큰 원문이 아니라 토큰으로 만든 HMAC 값이 들어갑니다. 토큰을 바꾸면 모든 쿠키가 무효가 됩니다(로그아웃은 그 브라우저의 쿠키만 지웁니다).
- 30일 동안 유지되고, 서버를 다시 켜도 그대로입니다.
- `Secure`는 `--trust-proxy`로 켰고 프록시가 `X-Forwarded-Proto: https`를 보낸 요청에서만 붙습니다.
- 잘못된 쿠키로 요청하면 401과 함께 쿠키를 지웁니다.

### 도메인과 HTTPS 리버스 프록시

도메인으로 접속하려면 그 이름을 허용 목록에 넣습니다. 이름만 적고 `https://`나 포트는 빼세요.

```bash
INSIA_ACCESS_TOKEN=... insia serve --host 127.0.0.1 --port 8765 --public-host insia.example.com --trust-proxy
```

- `--public-host`는 여러 번 쓸 수 있습니다.
- 컨테이너처럼 환경 변수로만 설정할 때는 `INSIA_PUBLIC_HOSTS=insia.example.com,www.example.com`(쉼표로 구분)과 `INSIA_TRUST_PROXY=1`을 쓰면 됩니다. 명령줄 옵션을 주면 그쪽이 먼저입니다.
- HTTPS를 앞단 프록시(Caddy, nginx)가 처리하면 `--trust-proxy`를 켜세요. 그래야 `Origin: https://…`를 같은 출처로 인정하고, 쿠키에 `Secure`를 붙이고, 로그인 실패 제한에 `X-Forwarded-For`의 마지막 주소(프록시가 붙인 값)를 씁니다. 프록시 없이 `--trust-proxy`를 켜면 누구나 이 헤더를 꾸밀 수 있으니 켜지 마세요.
- 프록시는 원래 `Host`를 그대로 넘겨야 합니다. nginx 예:

```nginx
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_buffering off;          # SSE가 바로바로 전달되게
    proxy_read_timeout 1h;
}
```

### `POST /api/login`

요청: `{"token": "<접근 토큰>"}`

| 결과 | 응답 |
|---|---|
| 성공 | `200 {"ok": true, "token_required": true}` + `Set-Cookie` |
| 토큰이 틀림 | `401 {"error": "토큰이 맞지 않아요.", "login": true}` |
| 토큰이 비었음 | `400` |
| 실패가 너무 많음 | `429` + `Retry-After` |
| 토큰 없이 도는 서버 | `200 {"ok": true, "token_required": false}` |

### `POST /api/logout`

본문 없음(`{}`). `200 {"ok": true}` + 쿠키를 지우는 `Set-Cookie`(`Max-Age=0`).

---

## 상태

### `GET /api/health`

```json
{
  "mode": "mock", "model": "claude-opus-5", "version": "0.1.0", "default_mode": "auto",
  "live_available": false,
  "workspace": "/home/me/insia/workspace",
  "profile_complete": false,
  "budget_usd": 0.0,
  "max_document_chars": 60000,
  "capabilities": {"docx": true, "png": false},
  "formats": {"bizplan": ["docx", "md", "txt", "zip"], "naver_blog": ["html", "md", "txt", "docx", "zip"],
              "linkedin": ["txt", "md", "docx", "zip"], "instagram": ["zip", "txt", "md", "docx"]},
  "active_jobs": {"live": 0, "mock": 1},
  "limits": {"live": 2, "mock": 4},
  "token_required": false
}
```

| 필드 | 뜻 |
|---|---|
| `mode` | `default_mode`(서버 기본값 auto/live/mock)를 풀어 쓴 실제 모드 |
| `live_available` | Anthropic API 자격 증명이 있는지 |
| `workspace` | 워크스페이스 폴더의 절대 경로 |
| `profile_complete` | 프로필 필수 12항목(회사명, 서비스명, 한 줄 소개, 서비스 설명, 타깃 고객, 고객이 겪는 문제, 해결 방법, 차별점, 비즈니스 모델, 팀 역할, 톤앤매너, 기본 행동 유도 문구)이 모두 찼는지 |
| `budget_usd` | 실행 1회 예산 상한(USD). `0`이면 상한 없음 |
| `max_document_chars` | 실행 1회에 모델로 보내는 참고 자료 글자 수 상한 |
| `capabilities` | `docx`: Word 내보내기 가능(python-docx), `png`: 카드뉴스 PNG 렌더링 가능(Playwright) |
| `formats` | 채널별 내보내기 형식 (첫 번째가 추천) |
| `active_jobs` · `limits` | 지금 도는 작업 수와 동시 작업 한도 |
| `token_required` | 토큰 모드인지 |

토큰 모드에서 로그인 전이면 `401 {"error": "로그인이 필요해요. …", "status": 401, "login": true}`.

### `GET /api/sample-brief`

샘플 브리프(`examples/sample-run/brief.json`)를 `Brief` 모양 그대로 돌려줍니다.

---

## 회사 프로필

모든 에이전트가 참고하는 회사·브랜드 정보입니다. 필드는 `src/insia_agents/models.py`의 `Profile`과 같습니다.

### `GET /api/profile`

```json
{
  "profile": {"company_name": "인시아", "service_name": "스마트에이전트", "one_liner": "", "...": "...",
              "team": [{"role": "대표", "name": "홍길동", "background": "", "hiring": false}],
              "updated_at": "2026-09-28T01:02:03.000Z"},
  "profile_complete": false,
  "missing": ["한 줄 소개", "서비스 설명"],
  "completeness": 83
}
```

저장한 적이 없으면 모든 필드가 빈 값인 프로필이 옵니다.

### `PUT /api/profile`

프로필 전체를 바꿉니다. 본문은 `Profile` 객체 그대로 또는 `{"profile": {...}}`. 빠진 필드는 빈 값이 되고, `updated_at`은 서버가 정합니다. 응답은 `GET`과 같은 모양입니다.

```json
{"company_name": "인시아", "service_name": "스마트에이전트", "one_liner": "1인 창업자의 콘텐츠 팀",
 "differentiators": ["사람 최종 승인"], "team": [{"role": "대표", "name": "홍길동"}],
 "banned_words": ["최고", "1위"], "default_hashtags": ["#1인창업"], "brand_colors": ["#3B5BDB"]}
```

형식이 틀리면 `400 {"error": "형식이 올바르지 않아요: team"}`. 팀원 실명은 사업계획서에 절대 나가지 않습니다(블라인드 규정).

---

## 참고 자료

회사 소개서, IR 자료 같은 사용자 자료입니다. 리서치 팩에 `origin: "user"` 출처(`url: "user://u1"`)로 들어갑니다. API는 **텍스트만** 받습니다. PDF·DOCX는 `insia docs add <파일>`로 올리거나 텍스트를 붙여 넣으세요.

### `GET /api/documents`

```json
{"documents": [{"id": "u1", "title": "회사 소개서", "kind": "text", "filename": "intro.txt",
                "text": "베타 사용자 120명 (2026-08 기준) …", "chars": 5210, "created_at": "2026-09-28T01:00:00.000Z"}],
 "total_chars": 5210, "max_document_chars": 60000}
```

`?text=0`이면 `text`를 빼고 보냅니다.

### `POST /api/documents`

요청: `{"title": "회사 소개서", "text": "…", "kind": "text", "filename": "intro.txt"}`

- `text` 필수(빈 글 불가, 200만 자까지). `kind`: `text`(기본) · `markdown` · `pdf` · `docx`. `filename`은 경로를 떼고 이름만 저장합니다.
- 응답: `201 {"document": {…UserDocument…}}`. id는 `u1, u2, …`로 늘어나고 지운 번호는 다시 쓰지 않습니다.

### `GET /api/documents/<id>` · `DELETE /api/documents/<id>`

`GET` → `{"document": {…}}`. `DELETE` → `200 {"deleted": true, "id": "u1"}`. 없으면 404.

---

## 실행

"실행(run)"은 파이프라인 실행과 콘텐츠 작업을 모두 가리킵니다. 모두 워크스페이스의 `runs` 표에 저장되고 같은 이벤트 스트림을 씁니다.

| `kind` | 무엇 | 시작하는 곳 | 이어서 실행 |
|---|---|---|---|
| `pipeline` | 브리프 → 계획 → 리서치 → 채널별 초안·검수 | `POST /api/runs`, `insia run` | 가능 |
| `slot` | 캘린더 슬롯 하나의 초안 (단일 채널 파이프라인) | `POST /api/calendar/<slot>/generate`, `insia run-due` | 가능 |
| `review` | 재검수 | `POST /api/items/<id>/review` | 불가 (다시 요청) |
| `revise` | 수정 요청 → 새 버전 → 자동 재검수 | `POST /api/items/<id>/revise` | 불가 |
| `edit` | 사람이 직접 고친 버전 저장 (즉시 끝남) | `PUT /api/items/<id>/draft` | — |

상태(`status`): `running` 실행 중 · `completed` 완료 · `failed` 실패 · `cancelled` 중단함 · `interrupted` 서버가 꺼져서 멈춤.
서버가 켜질 때 이전 프로세스가 남긴 `running` 실행은 모두 `interrupted`로 바뀌고, 이벤트 스트림 끝에 `run.failed`(`data.interrupted: true`)가 붙습니다.

### `POST /api/runs`

브리프로 파이프라인을 시작합니다. 본문은 `Brief` 그대로이거나 `{"brief": {...}}`로 감싸도 됩니다. `options`는 선택입니다.

```json
{"topic": "1인 창업자를 위한 AI 콘텐츠 에이전트", "goal": "서비스 런칭 홍보", "audience": "1인 창업자",
 "channels": ["naver_blog", "linkedin", "instagram"], "keywords": ["AI 마케팅 자동화", "1인 창업"],
 "tone": "친근한 전문가 톤", "notes": "",
 "options": {"mode": "auto", "speed": 2, "max_rounds": 2, "pass_score": 80, "max_cost_usd": 3,
             "use_profile": true, "docs": "all"}}
```

| option | 값 | 기본 |
|---|---|---|
| `mode` | `auto` · `live` · `mock` | 서버 기본값 (`insia serve --mode`) |
| `speed` | mock 재생 배속: `0`(기다리지 않음) 또는 `0.1`~`100` | 1 |
| `max_rounds` | 최대 수정 횟수 0~5 | 2 |
| `pass_score` | 통과 점수 0~100 | 80 |
| `max_cost_usd` | 이 실행의 예산 상한(USD) 0~10000, `0`이면 상한 없음 | 서버의 `INSIA_MAX_COST_USD` |
| `use_profile` | 회사 프로필을 쓸지 | true |
| `docs` | 참고 자료: `"all"` · `"none"` · `["u1", "u3"]` · `"u1,u3"` | `"all"` |

응답 `201`:

```json
{"run_id": "20260928-101039-7073", "kind": "pipeline", "mode": "mock",
 "events_url": "/api/runs/20260928-101039-7073/events",
 "status_url": "/api/runs/20260928-101039-7073",
 "cancel_url": "/api/runs/20260928-101039-7073/cancel"}
```

실행 기록은 응답을 보내기 전에 저장되므로 바로 목록과 상세에 보입니다. 오류: 브리프 형식(400), 없는 자료 id(400), API 키 없이 `live`(400), 동시 작업 초과(429).

### `GET /api/runs`

쿼리: `kind`, `status`, `limit`(1~200, 기본 50). 최신순입니다.

```json
{"runs": [{"run_id": "20260928-101039-7073", "kind": "pipeline", "status": "completed", "topic": "예시 주제",
           "channels": ["linkedin"], "mode": "mock", "model": "mock-template", "parent_item_id": "",
           "created_at": "2026-09-28T10:10:39.199Z", "updated_at": "2026-09-28T10:10:39.217Z",
           "finished_at": "2026-09-28T10:10:39.217Z", "cost_usd": 0.0, "error": null, "events": 49,
           "items": {"linkedin": "it_20260928-101039-7073_linkedin"}, "scores": {"linkedin": 86},
           "active": false}]}
```

- `active`: 이 서버 프로세스에서 지금 돌고 있는지. `status`가 `running`인데 `active: false`면 다른 곳(CLI)에서 돌고 있거나 비정상 종료된 실행입니다.
- `items`: 채널 → 콘텐츠 id, `scores`: 채널 → 점수. 작업(review/revise)의 대상 콘텐츠는 `parent_item_id`.

### `GET /api/runs/<id>`

목록 항목에 아래 필드가 더 붙습니다.

| 필드 | 뜻 |
|---|---|
| `brief`, `options`, `profile` | 실행에 쓴 브리프, 옵션(`doc_ids`, `slot_id` 등), 프로필 스냅숏 |
| `plan`, `research` | 저장된 계획(`Plan`)과 리서치 팩(`ResearchPack`) |
| `progress` | `{"channels": {"linkedin": "completed", "bizplan": "failed"}, …}` — 이어서 실행의 근거 |
| `result` | 이 서버가 최근에 실행했다면 전체 `RunResult` (아니면 `null`; 결과물은 `items`로 보관함에서 보세요) |
| `job` | 작업이면 `JobResult`: `{run_id, kind, item, version, review, format_checks, slot}` (이 서버가 최근에 실행한 것만) |
| `resumable` | 이어서 실행할 수 있는지 |
| `cancel_requested` | 중단을 요청했는지 |
| `events_url` | SSE 주소 |

### `GET /api/runs/<id>/events` (SSE)

`text/event-stream`. 저장된 이벤트를 먼저 모두 보내고, 실행 중이면 새 이벤트를 이어서 보냅니다.

```text
retry: 3000

id: 1
data: {"seq": 1, "t": 0.0, "type": "run.started", ...}

: ping
```

- `Last-Event-ID` 헤더(브라우저 자동) 또는 `?after=<seq>` 이후 이벤트만 보냅니다.
- 끝난 실행은 워크스페이스에서 다시 보냅니다. 서버를 다시 켠 뒤에도 똑같은 스트림을 받을 수 있습니다.
- 실행이 끝나면(마지막 이벤트가 `run.completed`/`run.failed`) 서버가 연결을 닫습니다. 끝난 실행에서 더 보낼 것이 없으면 `204`를 돌려줘 `EventSource` 재연결을 멈춥니다.
- **이어서 실행한 실행**은 저장된 스트림 중간에 앞 시도의 종료 이벤트(`run.failed`)가 있습니다. SSE는 이 이벤트를 빼고 보내므로(그 `seq`는 건너뜀) 스트림은 끝까지 이어지고, 클라이언트는 지금처럼 `run.completed`/`run.failed`를 받으면 `EventSource.close()`하면 됩니다. 앞 시도가 멈춘 이유는 `GET /api/runs/<id>`나 저장된 이벤트(`insia` CLI, `events.jsonl`)에서 볼 수 있습니다.
- CLI 등 다른 프로세스가 같은 워크스페이스에서 돌리는 실행도 이 주소로 따라갈 수 있습니다(0.5초마다 워크스페이스를 확인).
- 이벤트가 한동안 없으면 `: ping` 주석 줄을 보냅니다(기본 15초).

### `POST /api/runs/<id>/resume`

`interrupted` · `failed` · `cancelled` · 예산 초과로 멈춘 `pipeline`/`slot` 실행을 **같은 run id로** 이어서 실행합니다. 저장된 계획·리서치를 다시 쓰고, 끝난 채널은 건너뛰고, 나머지 채널은 마지막 초안·검수부터 계속합니다.

요청(모두 선택): `{"force": false, "options": {"speed": 0, "max_cost_usd": 5, "mode": "live"}}`

- `options.max_cost_usd`: 예산 초과로 멈췄다면 상한을 올려서 이어가세요.
- `mode`를 안 주면 원래 실행의 모드를 씁니다(서버 기본값이 `auto`일 때).
- `force: true`: 다른 곳에서 `running`으로 표시된 실행을 억지로 이어갑니다. 정말 멈춘 게 확실할 때만 쓰세요.

응답 `202`: `POST /api/runs`와 같은 링크 + `"resumed": true`. 이벤트는 같은 `events_url`에서 이어집니다(`seq`도 이어짐).

| 오류 | 코드 |
|---|---|
| 없는 실행 | 404 |
| review/revise/edit 작업 | 400 |
| 이 서버에서 이미 실행 중 | 409 |
| 다른 곳에서 `running`으로 표시됨 | 409 (`"can_force": true`) |
| 모든 채널을 이미 마침 | 409 |

### `POST /api/runs/<id>/cancel`

이 서버에서 돌고 있는 실행·작업을 멈춥니다. 다음 AI 호출 전에 멈추고(진행 중인 live 호출은 끝날 때까지 기다림), mock 재생은 바로 멈춥니다. 끝난 채널은 저장된 채로 남고, 스트림은 `run.failed`로 끝나며 상태는 `cancelled`가 됩니다. `pipeline`/`slot`은 나중에 이어서 실행할 수 있습니다.

응답 `202`: `{"run_id": "…", "status": "cancelling", "cancel_requested": true, "events_url": "…"}`

오류: 없는 실행 404, 이미 끝난 실행 409(`run_status` 포함), 다른 프로세스의 실행 409.

### `GET /api/runs/<id>/export`

실행 결과 전체를 zip 하나로 내려받습니다: 채널별 붙여넣기용 파일, `review.json`, `research.json`, `sources.md`(출처 목록), `brief.json`, `README.txt`. 헤더는 [콘텐츠 내보내기](#get-apiitemsidexport)와 같습니다. `?info=1`이면 파일 대신 `{filename, content_type, size, notes}` JSON.

---

## 콘텐츠 보관함

콘텐츠(item)는 채널 산출물 하나입니다. 파이프라인 결과는 `it_<run_id>_<channel>` id로 저장되고, 모든 초안 라운드와 사람 수정이 버전으로 쌓입니다.

상태: `draft` 초안 · `needs_changes` 수정 필요(최신 검수 미통과) · `approved` 승인 · `scheduled` 게시 예정 · `published` 게시 완료 · `archived` 보관. **자동 게시는 없습니다.** 게시는 사람이 채널에 올린 뒤 `published`로 표시합니다.

### `GET /api/items`

쿼리: `status`, `channel`, `limit`(기본 200). 최근 수정순입니다.

```json
{"items": [{"id": "it_20260928-101039-7073_linkedin", "run_id": "20260928-101039-7073", "channel": "linkedin",
            "title": "반복 업무를 덜어 낸 방법", "status": "draft", "version": 2, "score": 86, "passed": true,
            "scheduled_at": "", "published_at": "", "published_url": "", "note": "",
            "created_at": "2026-09-28T10:10:39.209Z", "updated_at": "2026-09-28T10:10:39.216Z"}]}
```

잘못된 `status`/`channel`은 400.

### `GET /api/items/<id>`

`ContentItemDetail` + 내보내기 목록입니다.

```json
{"item": {"id": "it_…_linkedin", "...": "..."},
 "versions": [{"id": "dv_3f2a…", "item_id": "it_…_linkedin", "version": 1, "source": "agent",
               "draft": {"channel": "linkedin", "round": 0, "title": "…", "content": "…", "hashtags": ["#1인창업"],
                         "used_finding_ids": ["f1"], "change_log": []},
               "review": {"channel": "linkedin", "round": 0, "score": 68, "passed": false, "rubric": [], "issues": [],
                          "fact_checks": [], "format_checks": [], "needs_research": [], "summary": "…"},
               "instructions": "", "created_at": "…"}],
 "brief": {"topic": "…", "channels": ["linkedin"], "...": "..."},
 "exports": [{"format": "txt", "label": "붙여넣기용 텍스트 (.txt)", "available": true,
              "url": "/api/items/it_…_linkedin/export?format=txt"},
             {"format": "docx", "label": "Word·한글 문서 (.docx)", "available": false,
              "url": "…", "hint": "Word 파일을 만들려면 pip install \"insia-smartagent[export]\"로 python-docx를 설치해 주세요."}]}
```

`versions`는 오래된 것부터, 마지막이 현재 버전입니다. `source`: `agent`(에이전트) · `human`(사람). `instructions`는 수정 요청 때 사람이 준 지시입니다.

### `PUT /api/items/<id>/draft`

사람이 직접 고친 내용을 새 버전으로 저장합니다. AI 호출 없이 코드로 형식 검사만 다시 하고, 상태는 `draft`로 돌아갑니다(승인하려면 재검수나 "그래도 승인"). 게시 완료된 콘텐츠는 상태가 그대로입니다.

요청: `{"title": "고친 제목", "content": "본문 마크다운", "hashtags": ["#AI", "창업"]}` (`hashtags`는 선택, `"#AI #창업"` 문자열도 됨. `#`이 없으면 붙이고 공백을 뺍니다)

응답 `200` (`JobResult`):

```json
{"run_id": "20260928-101041-aa01", "kind": "edit",
 "item": {"id": "it_…_linkedin", "status": "draft", "version": 3, "score": null, "passed": null, "...": "..."},
 "version": {"id": "dv_…", "version": 3, "source": "human", "draft": {"...": "..."}, "review": null, "...": "..."},
 "review": null,
 "format_checks": [{"id": "length", "label": "분량(공백 포함)", "passed": false, "value": "12자", "expected": "1,300~2,000자 (최대 3,000자)"},
                   {"id": "hook_length", "label": "첫 2줄 길이", "passed": true, "value": "8자", "expected": "210자 이하"}],
 "slot": null}
```

오류: 제목·본문이 비었거나 너무 김(400; 제목 300자, 본문 10만 자), 없는 콘텐츠(404).

### `POST /api/items/<id>/review`

재검수 작업을 시작합니다. 본문 `{}` 또는 `{"options": {"mode": "mock", "speed": 1}}`.

응답 `201`: `{"run_id", "kind": "review", "mode", "events_url", "status_url", "cancel_url", "item_id"}`. 진행은 `events_url`로 보고, 끝나면 `GET /api/runs/<run_id>`의 `job.review`나 `GET /api/items/<id>`에서 결과를 봅니다. 검수는 버전을 새로 만들지 않고 현재 버전에 붙습니다.

오류: 없는 콘텐츠 404, 버전 없음 400, 같은 콘텐츠에 작업이 이미 도는 중 409, 동시 작업 초과 429.

### `POST /api/items/<id>/revise`

수정 요청 작업: 최신 검수 의견과 사람의 지시로 새 버전을 쓰고 자동으로 다시 검수합니다.

요청: `{"instructions": "도입부를 두 문장으로 줄이고 사례를 하나 넣어 주세요", "options": {"speed": 1}}` (`instructions`는 선택, 4,000자까지)

응답 `201`: review와 같은 모양(`"kind": "revise"`). 새 버전은 `source: "agent"`, `instructions`에 지시가 저장됩니다.

### `POST /api/items/<id>/status`

요청: `{"status": "approved", "force": false, "scheduled_at": "2026-10-05", "published_url": "https://…", "note": "메모"}` (`status`만 필수)

| 바꾸기 | 조건 |
|---|---|
| draft · needs_changes → `approved` | 최신 버전이 검수를 통과했거나 `force: true`("그래도 승인") |
| approved → `scheduled` | `scheduled_at` 필요 (`YYYY-MM-DD` 또는 ISO 시각) |
| approved · scheduled → `published` | `published_url`은 선택(`http(s)://`). `published_at`은 서버가 기록 |
| 무엇이든 → `archived`, archived → `draft` | 보관 · 복원 |
| approved → draft · needs_changes, scheduled → approved | 되돌리기 |

응답 `200 {"item": {…ContentItem…}}`.

승인이 막히면 `409`:

```json
{"error": "최신 버전(v3)은 아직 검수를 받지 않았어요. 재검수를 먼저 돌리거나, 내용을 직접 확인했다면 '그래도 승인'을 눌러 주세요.",
 "status": 409, "blocked": true, "can_force": true, "item_id": "it_…_linkedin", "version": 3, "score": null,
 "hint": "내용을 직접 확인했다면 force: true로 다시 보내면 그래도 승인돼요 (그래도 승인)."}
```

허용되지 않는 전환(예: 게시 완료 → 승인)은 `409 {"error": "'게시 완료' 상태에서 '승인'(으)로 바꿀 수 없어요."}`. 형식 오류(날짜, URL, 알 수 없는 상태)는 400.

### `GET /api/items/<id>/export`

쿼리: `format`(생략하면 채널 추천 형식), `version`(버전 번호, 생략하면 현재 버전), `info=1`(파일 대신 정보 JSON).

| 채널 | 형식 (첫 번째가 추천) |
|---|---|
| 사업계획서 `bizplan` | `docx` · `md` · `txt` · `zip` |
| 네이버 블로그 `naver_blog` | `html`(스마트에디터 붙여넣기용) · `md` · `txt` · `docx` · `zip` |
| 링크드인 `linkedin` | `txt`(그대로 붙여넣기) · `md` · `docx` · `zip` |
| 인스타그램 `instagram` | `zip`(캐러셀 PNG 또는 slides.html + caption.txt + alt-text.txt) · `txt` · `md` · `docx` |

응답 헤더:

```
Content-Type: application/zip
Content-Disposition: attachment; filename="2026-09-28_instagram_-3.zip"; filename*=UTF-8''2026-09-28_instagram_%EB%8D%B0%EB%AA%A8-…
Cache-Control: no-store
X-Content-Type-Options: nosniff
Content-Security-Policy: default-src 'none'; sandbox
X-Insia-Notes: PNG%20%EB%8C%80%EC%8B%A0%20slides.html%EC%9D%84%20%EB%84%A3%EC%97%88%EC%96%B4%EC%9A%94…
```

- 파일 이름은 `<YYYY-MM-DD>_<채널>_<제목 슬러그>.<확장자>`이고, 한글 이름은 `filename*`(RFC 5987)에 있습니다. 대시보드는 `<a href="/api/items/<id>/export?format=…" download>`로 받으면 됩니다(같은 출처라 쿠키 인증도 됩니다).
- `X-Insia-Notes`: 함께 보여 줄 한국어 안내(예: PNG 대신 slides.html을 넣음, 사업계획서에 팀원 실명이 있음). 여러 줄을 `\n`으로 이은 뒤 퍼센트 인코딩했으니 `decodeURIComponent(value).split("\n")`로 읽으세요. 안내가 없으면 헤더도 없습니다.
- `?info=1` → `{"filename": "2026-09-28_instagram_데모-….zip", "content_type": "application/zip", "size": 5175, "notes": ["PNG 대신 slides.html을 넣었어요 — …"]}`

오류: 채널에 없는 형식·없는 버전(400), 없는 콘텐츠(404), python-docx 미설치(501 `{"error": "… pip install \"insia-smartagent[export]\" …", "package": "python-docx", "extra": "export"}`).

---

## 캘린더

### `GET /api/calendar`

쿼리: `from`, `to` (`YYYY-MM-DD`, 둘 다 선택). 날짜 → 채널 순입니다.

```json
{"slots": [{"id": "sl_fbd138f1587d", "date": "2026-10-09", "channel": "linkedin", "topic": "AI 운영, 시작 전에 확인할 5가지",
            "angle": "체크리스트", "keywords": ["AI 운영", "체크리스트"], "goal": "전문성 인지와 대화",
            "status": "drafted", "item_id": "it_20260928-101040-0398_linkedin", "run_id": "20260928-101040-0398",
            "created_at": "2026-09-28T10:10:40.223Z", "item_status": "draft"}],
 "from": "2026-10-05", "to": "2026-10-11"}
```

슬롯 상태: `planned` 계획 · `generating` 초안 만드는 중 · `drafted` 초안 있음 · `skipped` 건너뜀. `item_status`는 연결된 콘텐츠의 현재 상태입니다(없으면 `null`).

### `POST /api/calendar/plan`

프로필, 주제, 지난 게시물(겹치는 주제 피하기)을 보고 기간 안의 평일에 게시물을 배치해 저장합니다. **동기 요청**이라 계획이 끝나야 응답합니다(live 모드는 수십 초 걸릴 수 있어요).

요청:

```json
{"theme": "AI로 콘텐츠 운영 시간 줄이기", "start": "2026-10-05", "end": "2026-10-09",
 "counts": {"naver_blog": 2, "linkedin": 1, "instagram": 1}, "options": {"mode": "mock"}}
```

- `end` 대신 `days`(1~31, 기본 7)를 줄 수 있습니다. 기간은 최대 31일.
- `counts` 키는 `naver_blog`/`blog`, `linkedin`, `instagram`/`ig`, `bizplan`. 채널마다 하루 한 편까지라 넘치면 줄이고 `notices`로 알려 줍니다.

응답 `201`:

```json
{"summary": "2026-10-05~2026-10-09 평일에 네이버 블로그 2편, 링크드인 1편, 인스타그램 1편을 배치했어요. …",
 "slots": [{"id": "sl_…", "date": "2026-10-05", "channel": "instagram", "topic": "…", "status": "planned", "...": "..."}],
 "notices": ["네이버 블로그은(는) 3편을 요청했지만 2편만 계획됐어요."],
 "mode": "mock", "start": "2026-10-05", "end": "2026-10-09"}
```

오류: 날짜·개수 형식(400), 동시 작업 초과(429), AI 호출 실패(502).

### `POST /api/calendar/<slot>/generate`

슬롯 주제로 단일 채널 파이프라인을 돌려 초안을 만듭니다(`kind: "slot"`). 끝나면 슬롯이 `drafted`가 되고 콘텐츠와 연결되며, 콘텐츠의 `scheduled_at`에 슬롯 날짜가 들어갑니다. 실패하면 슬롯은 `planned`로 돌아갑니다.

요청: `{}` 또는 `{"force": true, "options": {"speed": 1, "docs": "all"}}`

응답 `201`:

```json
{"run_id": "20260928-101040-0398", "kind": "slot", "mode": "mock",
 "events_url": "/api/runs/20260928-101040-0398/events", "status_url": "…", "cancel_url": "…",
 "slot_id": "sl_fbd138f1587d", "item_id": "it_20260928-101040-0398_linkedin",
 "slot": {"id": "sl_fbd138f1587d", "status": "generating", "run_id": "20260928-101040-0398", "...": "..."}}
```

`item_id`는 미리 정해지는 id라 작업이 끝나면 보관함에서 바로 열 수 있습니다.

| 오류 | 코드 |
|---|---|
| 없는 슬롯 | 404 |
| 이미 만드는 중 | 409 (`run_id`) |
| 이미 초안이 있음 | 409 (`item_id`, `"can_force": true`) — `force: true`로 다시 만들기 |
| 건너뛴 슬롯 | 409 (`"can_force": true`) — 먼저 `planned`로 되돌리기 |

### `POST /api/calendar/<slot>`

슬롯의 일부만 바꿉니다. 바꿀 수 있는 항목: `date`, `topic`, `angle`, `keywords`(목록 또는 쉼표 문자열), `goal`, `status`(`planned` · `skipped`만).

```json
{"date": "2026-10-08"}
{"status": "skipped"}
```

응답 `200 {"slot": {…CalendarSlot…}}`. 오류: 없는 슬롯 404, 다른 항목·잘못된 날짜·다른 상태 400, 초안을 만드는 중인 슬롯 409, 초안이 있는 슬롯을 `planned`로 409.

---

## 사용량

### `GET /api/usage`

쿼리: `since`, `until` — `YYYY-MM-DD`(한국 날짜, 그날 포함) 또는 ISO 시각. 둘 다 선택.

```json
{"since": "2026-09-01", "until": "2026-09-30",
 "total_usd": 1.2345, "calls": 13,
 "input_tokens": 18050, "output_tokens": 10090, "cache_read_tokens": 0, "cache_write_tokens": 0, "web_search_requests": 4,
 "runs": [{"run_id": "20260928-101039-7073", "kind": "pipeline", "topic": "예시 주제", "usd": 0.84, "calls": 12,
           "input_tokens": 17817, "output_tokens": 9952, "cache_read_tokens": 0, "cache_write_tokens": 0,
           "web_search_requests": 4, "first_at": "2026-09-28T10:10:39.000Z", "last_at": "2026-09-28T10:10:39.000Z",
           "started_at": "2026-09-28T10:10:39.199Z", "status": "completed"}],
 "by_task": {"plan": {"usd": 0.05, "calls": 2, "input_tokens": 165, "output_tokens": 979},
             "plan_calendar": {"usd": 0.01, "calls": 1, "input_tokens": 233, "output_tokens": 138}},
 "by_day": [{"date": "2026-09-28", "usd": 1.2345, "calls": 13}],
 "budget_usd": 3.0, "currency": "USD"}
```

- 금액은 USD, 모델별 토큰 단가(`src/insia_agents/costs.py`, `prices.json`, `INSIA_PRICE_*`)로 계산한 **추정치**입니다. mock 모드는 0원입니다.
- `runs`는 최근순, `by_day`는 오래된 날짜부터(한국 날짜). 캘린더 계획처럼 실행 id가 없는 호출은 `run_id: ""`, `kind: "other"`로 묶입니다.
- `started_at`: 실행이 시작된 시각(실행 기록이 없으면 `first_at`), `status`: 그 실행의 현재 상태.
- `budget_usd`: 실행 1회 예산 상한(`0`이면 없음).

---

## 파이썬에서 서버 띄우기

CLI(`insia serve`)가 쓰는 함수입니다.

```python
from insia_agents.config import Settings
from insia_agents.server import ServerConfigError, make_server, serve

settings = Settings.from_env()
try:
    serve(settings, host="0.0.0.0", port=8765, web_dir=None, quiet=True,
          token=None,                          # None이면 INSIA_ACCESS_TOKEN
          public_hosts=["insia.example.com"],  # --public-host (여러 개)
          trust_proxy=True)                    # --trust-proxy
except ServerConfigError as exc:               # ValueError: 토큰 없음·짧음, 잘못된 도메인, 워크스페이스 오류
    print(f"오류: {exc}")
except OSError as exc:                         # 포트 사용 중 등
    print(f"서버를 시작하지 못했어요: {exc}")
```

- `make_server(settings, host="127.0.0.1", port=8765, web_dir=None, heartbeat=15.0, quiet=True, *, token=None, public_hosts=(), trust_proxy=False, workspace=None, max_live=None, max_mock=None)`는 서버 객체만 만듭니다(`serve_forever()`로 시작, `server_close()`로 정리 — 도는 작업을 중단하고 워크스페이스를 닫습니다).
- `serve()`는 시작 안내(주소, 모드, 워크스페이스, 토큰 여부, 정리한 중단 실행 수)를 출력하고 Ctrl+C까지 돕니다.
- 서버가 켜질 때 `running`으로 남은 실행을 `interrupted`로 정리합니다. 같은 워크스페이스에서 CLI 실행(`insia run`, `insia run-due`)이 도는 중에 서버를 켜면 그 실행도 중단됨으로 표시되니, 서버는 CLI 작업이 없을 때 켜세요.
