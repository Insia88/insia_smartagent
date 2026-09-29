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
8. [API 게시 (LinkedIn · 인스타그램)](#api-게시-linkedin--인스타그램) — 상태, 연결, 미리보기, 확인 게시, 게시 기록, 결과 정리
9. [캘린더](#캘린더)
10. [사용량](#사용량)
11. [파이썬에서 서버 띄우기](#파이썬에서-서버-띄우기)

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
| **사람 요청**: `Sec-Fetch-Site`가 있으면 `same-origin`만, 없으면 `Host`와 같은 `Origin`이 꼭 있어야 함. 토큰 모드에서는 `insia_token` 쿠키로 인증한 요청만(`Authorization: Bearer`는 거절) | `POST /api/items/<id>/publish`, `POST /api/publish/attempts/<pa>/resolve` | 403 `{"code": "not_human"}` |

`/api` 밖에서 서버가 직접 답하는 경로는 두 개예요. 둘 다 `Host` 검사는 하고, 접근 토큰은 받지 않아요.

| 경로 | 받는 것 | 답 |
|---|---|---|
| `GET /oauth/linkedin/callback` | LinkedIn이 돌려보낸 `code`·`state` + `insia_oauth` 쿠키 | 언제나 `303` 하나. 페이지를 그리지 않아요([LinkedIn 콜백](#get-oauthlinkedincallback)) |
| `GET`·`HEAD /pub/m/<32자 hex>/<NN>.jpg` | 인스타그램이 가져갈 공개 이미지 | **구성 B에서만**(미디어 도메인이 `--public-host`에 있고 미디어 포트가 없을 때) `200 image/jpeg`. 그 밖에는 본문 없는 `404`(1분에 30번 넘으면 `429`) |

`serve --media-port`로 여는 **미디어 전용 리스너**(구성 A)는 대시보드와 다른 소켓이에요. `/pub/m/<32자 hex>/<NN>.jpg` 말고는 모두 본문 없는 404(GET·HEAD가 아니면 405)이고, `/api`·`/oauth`·대시보드 파일이 아예 없어요.

다른 사이트의 페이지는 JSON 요청을 보내려면 CORS 사전 요청(preflight)을 거쳐야 하는데, 이 서버는 사전 요청을 절대 허락하지 않습니다. 그래서 로그인한 브라우저라도 다른 사이트가 대신 실행·승인·삭제를 할 수 없습니다. 정적 파일(대시보드 화면)은 토큰 없이도 열리고, `X-Frame-Options: DENY`로 다른 사이트에 끼워 넣을 수 없습니다.

### 본문 크기

| 경로 | 최대 |
|---|---|
| `POST /api/documents` | 8MB (텍스트는 200만 자까지 — 한글 200만 자가 UTF-8로 약 6MB) |
| `PUT /api/items/<id>/draft` | 1MB (본문은 10만 자까지) |
| `PUT /api/profile` | 256KB |
| `POST /api/login` · `/api/logout` | 4KB |
| `PUT /api/publish/instagram/token` | 8KB |
| 그 밖의 API 게시 경로(`/api/publish/**`, `/api/items/<id>/publish/**`) | 4KB |
| 나머지 JSON | 64KB |

`Content-Length`가 필요합니다(chunked 전송은 411). 한도를 넘으면 본문을 읽기 전에 413으로 답합니다. `NaN`, `Infinity`, `1e999`처럼 무한대가 되는 수, 절댓값이 9,007,199,254,740,991(2⁵³−1)보다 큰 정수, 너무 깊게 중첩된 JSON은 400입니다. 본문이 `Content-Length`보다 짧게 끝나면 400, 헤더만 보내고 본문을 120초 동안 보내지 않으면 408입니다.

### 동시 작업 수

실행과 작업(재검수·수정 요청·슬롯 초안·이어서 실행)은 백그라운드에서 돕니다. 한 번에 **live 2개, mock 4개**까지이고, 넘으면 `429`와 `Retry-After: 10`을 돌려줍니다. 캘린더 계획(`POST /api/calendar/plan`)도 도는 동안 한 자리를 씁니다. 환경 변수 `INSIA_MAX_LIVE_JOBS`, `INSIA_MAX_MOCK_JOBS`로 바꿀 수 있습니다.

```json
{"error": "동시에 실행할 수 있는 작업 수(live 2개)를 넘었어요. 진행 중인 작업이 끝난 뒤 다시 시도해 주세요.", "status": 429}
```

같은 콘텐츠에 재검수·수정 요청이 이미 돌고 있거나, 같은 캘린더 슬롯이 초안을 만드는 중이면 `409`와 진행 중인 `run_id`를 돌려줍니다.

에이전트가 아직 쓰고 있는 콘텐츠는 사람이 직접 고쳐 저장하는 것(`PUT /api/items/<id>/draft`)도 `409`입니다. 에이전트가 이전 버전으로 작업하는 중이라, 지금 저장하면 그 결과가 사람이 고친 버전 위에 현재 버전으로 올라가기 때문이에요. 이런 경우예요.

- 그 콘텐츠에 재검수·수정 요청 작업이 도는 중
- 그 콘텐츠를 만든 실행(`pipeline`·`slot`, 이어서 실행 포함)이 아직 도는 중. 실행 중에도 채널마다 v1이 먼저 보관함에 보이지만, 실행이 끝날 때까지 다음 수정본·최종본이 더 붙어요

같은 이유로 그 콘텐츠를 만든 실행이 도는 동안에는 재검수·수정 요청도 `409`이고, 실행의 콘텐츠 하나에 재검수·수정 요청이 돌거나 사람이 저장하는 중이면 그 실행을 이어서 실행하는 것도 `409`입니다. 모두 이 서버에서 도는 작업만 알 수 있어요(CLI로 따로 돌리는 실행은 모름).

API로 게시하는 중(`sending`)이거나 게시됐는지 확인이 필요한(`unknown`) 콘텐츠는 버전과 상태가 잠겨요. 직접 수정 저장, 재검수, 수정 요청, 상태 변경(승인 취소·보관·게시 완료 표시), 그 콘텐츠를 만든 실행의 이어서 실행이 모두 `409`이고, 이 잠금은 CLI·파이프라인이 쓰는 워크스페이스에서 걸려서 서버 밖에서도 같아요. 예정일·제목·메모 바꾸기는 막지 않아요.

```json
{"error": "게시됐는지 확인이 필요한 기록이 있어요. 먼저 정리해 주세요.", "status": 409, "code": "item_locked",
 "attempt_id": "pa_9338b06e91be3bc78267888e", "platform": "linkedin", "attempt_status": "unknown"}
```

반대로 에이전트가 그 콘텐츠를 쓰는 중이면(재검수·수정 요청, 또는 그 콘텐츠를 만든 실행) API 게시와 미리보기가 `409 {"code": "agent_job", "run_id": …}`예요.

### 상태 코드

| 코드 | 뜻 |
|---|---|
| 200 | 성공 |
| 201 | 새 실행·작업·자료·슬롯을 만들었어요 |
| 202 | 요청을 받았고 백그라운드에서 진행해요 (이어서 실행, 중단, API 게시) |
| 204 | SSE: 더 보낼 이벤트가 없어요 (EventSource가 재연결을 멈춤) |
| 400 | 입력이 잘못됐어요 |
| 401 | 로그인이 필요해요 (`"login": true`) |
| 303 | LinkedIn 연결 콜백 (`/oauth/linkedin/callback`, 언제나) |
| 403 | 허용되지 않은 Host·Origin, API 게시·결과 정리가 사람 요청이 아님(`"code": "not_human"`) |
| 404 | 없는 경로 또는 없는 id |
| 405 | 그 경로에서 지원하지 않는 메서드 (`Allow` 헤더 참고) |
| 409 | 지금 상태에서는 할 수 없어요 (승인 차단, 잘못된 상태 전환, 이미 실행 중, API 게시 잠금 등). API 게시 경로는 `code`로 이유를 알려 줘요 |
| 422 | API 게시: 고칠 부분이 있는 미리보기로 게시하려 함, 카드 이미지를 그리지 못함 |
| 408 | 요청 본문이 제시간(120초)에 도착하지 않았어요 |
| 413 · 415 · 411 | 본문이 너무 큼 · JSON이 아님 · Content-Length 없음 |
| 414 | 주소(URL)가 너무 길어요 (64KB 넘음) |
| 429 | 동시 작업 수 초과, 로그인 실패가 너무 많음, API 게시 미리보기·게시·연결 시도가 너무 많음 (`Retry-After`) |
| 501 | 선택 설치 패키지가 없음 (예: Word 내보내기에 python-docx 필요) |
| 502 | AI 백엔드(Anthropic API) 호출 실패, LinkedIn 코드 교환 실패(`"code": "exchange_failed"`) |

서버가 라우팅 전에 거절하는 요청(414, 잘못된 요청 줄 400, 헤더가 너무 큼 431, 지원하지 않는 메서드 501)도 같은 `{"error": "…", "status": N}` 모양으로 답합니다. 응답을 받기 전에 연결을 끊은 클라이언트는 오류로 기록하지 않습니다(디버그 로그만).

---

## 접근 토큰 (인증)

기본 실행(`insia serve`)은 내 PC(`127.0.0.1`)에서만 열리고 토큰이 없습니다. 다른 기기에서 접속하려면 토큰이 **반드시** 필요합니다.

- `--host 0.0.0.0`처럼 루프백이 아닌 주소로 열 때 토큰이 없으면 서버가 시작하지 않습니다.
- `--public-host`(`INSIA_PUBLIC_HOSTS`)나 `--trust-proxy`(`INSIA_TRUST_PROXY=1`)를 쓸 때도 토큰이 없으면 시작하지 않습니다. 리버스 프록시가 앞에 있으면 `127.0.0.1`로 열어도 바깥에서 접속할 수 있기 때문이에요.
- IPv6도 됩니다: `--host ::1`은 IPv6 루프백이라 토큰 없이 열리고, `--host ::`(IPv4·IPv6 모든 주소)는 `0.0.0.0`처럼 토큰이 필요합니다. IPv6를 쓸 수 없는 컴퓨터에서는 시작할 때 `--host 127.0.0.1`을 쓰라고 안내해요.
- 토큰은 `INSIA_ACCESS_TOKEN` 환경 변수나 `--token`으로 정합니다. 12자 이상, 공백 없는 영문·숫자·기호만 됩니다. 예: `python -c "import secrets; print(secrets.token_urlsafe(24))"`
- 토큰 모드에서는 `/api/login`, `/api/logout`을 뺀 모든 `/api` 요청에 아래 중 하나가 필요합니다.
  - `Authorization: Bearer <토큰>` 헤더 (스크립트용)
  - `insia_token` 쿠키 (`POST /api/login`이 설정, 브라우저용. SSE `EventSource`도 이 쿠키로 인증돼요)
- `Bearer`가 아닌 `Authorization` 헤더(예: 앞단 nginx `auth_basic`·Caddy `basicauth`가 그대로 넘기는 `Basic …`)는 이 서버의 토큰이 아니라서 무시하고 쿠키로 인증합니다. 틀린 시도로 세지도 않아요. 반대로 `Bearer` 헤더가 있으면 그 값이 틀렸을 때 쿠키가 맞아도 401입니다.
- 인증이 없으면 `/api/health`를 포함해 모두 `401`과 `{"login": true}`를 돌려줍니다. 대시보드는 이걸 보고 로그인 화면을 띄웁니다. 없는 경로도 로그인 전에는 404가 아니라 401입니다.
- 토큰 비교는 상수 시간(`hmac.compare_digest`)으로 합니다.
- 틀린 토큰(로그인, Bearer, 쿠키 모두)은 클라이언트 IP마다 **1분에 10번**까지입니다. 넘으면 맞는 토큰이라도 잠시 `429`(`Retry-After` 초)입니다.
  - IPv6 클라이언트는 주소 하나가 아니라 **/64 네트워크 하나**를 한 클라이언트로 셉니다. 가입자 한 명(집 회선, 서버 한 대)이 보통 /64 전체를 받아서, 주소마다 세면 주소를 바꿔 가며 계속 시도할 수 있기 때문이에요. `::ffff:203.0.113.9`처럼 IPv4를 담은 주소(`--host ::`로 열었을 때 IPv4 클라이언트가 이렇게 보여요)는 IPv4 주소 `203.0.113.9`와 같은 클라이언트예요.
  - 토큰을 비교하기 **전에** 시도 한 번을 먼저 셉니다. 그래서 연결을 여러 개 열어 한꺼번에 보내도 1분에 10번보다 많이 비교하지 않고, 나머지는 비교 없이 `429`입니다. 맞는 토큰이면 센 한 번을 돌려줘서, 정상 요청은 제한을 쓰지 않아요.
  - 로그인(`POST /api/login`)에 성공하면 그 IP의 실패 기록이 지워집니다.

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
- HTTPS를 앞단 프록시(Caddy, nginx)가 처리하면 `--trust-proxy`를 켜세요(토큰이 있어야 켜집니다). 그래야 `Origin: https://…`를 같은 출처로 인정하고, 쿠키에 `Secure`를 붙이고, 로그인 실패 제한에 `X-Forwarded-For`의 마지막 주소(프록시가 붙인 값)를 씁니다. 프록시 없이 `--trust-proxy`를 켜면 누구나 이 헤더를 꾸밀 수 있으니 켜지 마세요.
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
  "token_required": false,
  "publish": {"enabled": true, "configured": true, "fake": false, "linkedin": "connected", "instagram": "disabled"}
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
| `publish` | API 게시 요약(네트워크 호출 없음): 켜짐 여부(`INSIA_PUBLISH=0`이면 `false`), 설정했는지, 가짜 게시 모드인지, 플랫폼별 상태([상태 값](#get-apipublish)). 게시 기능이 답하지 못하면 `null` |

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

상태(`status`): `running` 실행 중 · `completed` 완료 · `failed` 실패 · `cancelled` 중단함 · `interrupted` 실행하던 프로그램이 멈춰서 중단됨.

실행마다 그 실행을 돌리는 프로세스(pid·호스트)와 신호(heartbeat, 30초마다 갱신)가 기록됩니다. 서버가 켜질 때, 그리고 `insia run-due`·`insia resume`이 시작할 때 `running`으로 남은 실행 중 **실행하던 프로세스가 없어진 것만** `interrupted`로 바꿉니다: 같은 컴퓨터에서 그 프로세스가 끝났거나, 컴퓨터가 다시 켜졌거나, 신호가 10분 넘게 끊긴 경우입니다. 다른 프로세스(예: cron의 `insia run-due`)가 돌리는 실행은 그대로 두니, 서버를 켜는 시점이 CLI 실행과 겹쳐도 됩니다. 정리된 실행은 이벤트 스트림 끝에 `run.failed`(`data.interrupted: true`, `data.resumable`)가 붙고, 그 실행이 만들던 캘린더 슬롯만 다시 `planned`로 돌아갑니다(이미 있던 초안이 있으면 `drafted` 유지). `pipeline`/`slot`은 [이어서 실행](#post-apirunsidresume)할 수 있고, 작업(review/revise/edit)은 오류 메시지대로 보관함에서 다시 시작합니다.

서버는 켜진 뒤에도 1분마다 다시 확인해서, 시작할 때는 판단할 수 없던 실행도 실행하던 프로세스가 없어지면 다시 켜지 않아도 `interrupted`로 정리합니다. 두 번 연달아 확인했을 때도 신호가 없어야 정리하니, 잠자기에서 깨어난 노트북의 CLI 실행은 신호를 다시 보낼 시간이 있어요. [이어서 실행](#post-apirunsidresume)·[중단](#post-apirunsidcancel)·[슬롯 초안 만들기](#post-apicalendarslotgenerate) 요청도 그 실행을 바로 한 번 확인하니, 같은 컴퓨터에서 강제 종료된 CLI 실행은 1분을 기다리지 않고 그 자리에서 정리됩니다.

서버가 멈출 때(Ctrl+C, `docker compose stop`·`down`·업데이트, systemd의 SIGTERM) 도는 실행은 다음 AI 호출 전에 멈추고 `cancelled`로 저장됩니다(최대 20초 기다림, 끝낸 채널은 남고 이어서 실행 가능). 그 20초 안에 끝나지 않은 live 호출이 있거나 프로세스가 강제로 죽으면(`kill -9`, 정전) 실행은 `running`으로 남습니다. 같은 컴퓨터·컨테이너에서 다시 켜면 바로 정리되고, 컨테이너가 새로 만들어진 경우(`docker compose up -d --build`)에는 이전 컨테이너의 실행이 다른 컴퓨터의 실행처럼 보여서 마지막 신호에서 10분쯤 지나 자동으로 `interrupted`가 됩니다(그동안은 `running`으로 보이고, 이어서 실행은 `force: true`로만 됩니다).

실행이 이렇게 정리되거나 다른 곳에서 `force`로 넘겨받으면, 그 실행을 아직 돌리고 있던 원래 프로세스(예: 잠자기에서 깨어난 노트북)는 다음 AI 호출 전에 멈추고, 그 뒤로는 그 실행의 이벤트·상태·버전·검수를 기록하지 않으며 캘린더 슬롯도 건드리지 않습니다. 새 주인이 만드는 중이거나 이미 초안을 연결한 슬롯이 다시 `planned`로 돌아가 같은 초안이 두 번 만들어지는 일은 없어요.

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

쿼리: `kind`, `status`, `limit`(1~200, 기본 50), `parent_item_id`(콘텐츠 id `it_…`: 그 콘텐츠에 돌린 작업 — 재검수·수정 요청·직접 수정 — 만, 형식이 틀리면 400). 최신순입니다. CLI에서는 `insia runs list --item <콘텐츠 id>`.

```json
{"runs": [{"run_id": "20260928-101039-7073", "kind": "pipeline", "status": "completed", "topic": "예시 주제",
           "channels": ["linkedin"], "mode": "mock", "model": "mock-template", "parent_item_id": "",
           "created_at": "2026-09-28T10:10:39.199Z", "updated_at": "2026-09-28T10:10:39.217Z",
           "finished_at": "2026-09-28T10:10:39.217Z", "cost_usd": 0.0, "error": null, "events": 49,
           "items": {"linkedin": "it_20260928-101039-7073_linkedin"}, "scores": {"linkedin": 86},
           "active": false, "resumable": false}]}
```

- `active`: 이 서버 프로세스에서 지금 돌고 있는지. `status`가 `running`인데 `active: false`면 다른 곳(CLI)에서 돌고 있거나, 실행하던 프로그램이 멈췄는데 아직 정리되기 전인 실행입니다.
- `resumable`: [이어서 실행](#post-apirunsidresume)할 수 있는지. `pipeline`/`slot` 실행이 `interrupted`·`failed`·`cancelled`로 멈췄거나, `completed`인데 결과 콘텐츠가 없는 채널이 있으면 `true`입니다. 대시보드 스튜디오의 **실행 기록**과 실행 바가 이 값으로 "이어서 실행" 버튼을 보여 줍니다.
- `items`: 채널 → 콘텐츠 id, `scores`: 채널 → 점수. 작업(review/revise)의 대상 콘텐츠는 `parent_item_id`.

### `GET /api/runs/<id>`

목록 항목에 아래 필드가 더 붙습니다.

| 필드 | 뜻 |
|---|---|
| `brief`, `options`, `profile` | 실행에 쓴 브리프, 옵션(`doc_ids`, `slot_id` 등), 프로필 스냅숏 |
| `plan`, `research` | 저장된 계획(`Plan`)과 리서치 팩(`ResearchPack`) |
| `progress` | `{"channels": {"linkedin": "completed", "bizplan": "failed"}, …}` — 이어서 실행의 근거 |
| `owner` | `{"pid", "host", "heartbeat_at"}` — 이 실행을 돌리는(돌렸던) 프로세스와 마지막 신호 시각 |
| `result` | 이 서버가 최근에 실행했다면 전체 `RunResult` (아니면 `null`; 결과물은 `items`로 보관함에서 보세요) |
| `job` | 작업이면 `JobResult`: `{run_id, kind, item, version, review, format_checks, slot, base_version, current_version, superseded, superseded_by_human_edit}` (이 서버가 최근에 실행한 것만). 작업이 도는 동안 사람이 새 버전을 저장했으면 `superseded`(사람이면 `superseded_by_human_edit`도) `true` — [재검수](#post-apiitemsidreview)·[수정 요청](#post-apiitemsidrevise) 참고 |
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
- 파이프라인 실행의 `run.completed`에는, 실행하는 동안(또는 멈춰 있던 동안) 사람이 콘텐츠를 고쳤거나 승인·게시 예정·게시 완료로 바꿨거나 다른 작업(예: 수정 요청)이 새 버전을 저장한 채널이 있으면 `superseded: true`가 붙습니다. 그중 사람이 저장한 버전이 있으면 `superseded_by_human_edit: true`도 붙고, 채널별 내용은 `superseded_channels: {"<채널>": {"version": 기록에만 남긴 에이전트 버전, "current_version": 현재 버전, "superseded_by_human_edit": bool, "reason": "human_edit" | "status" | "newer_version", "status": 콘텐츠 상태}}`로 옵니다. 그 채널의 에이전트 결과는 버전 기록에만 남고, 그때 현재였던 버전의 내용이 다시 맨 위로 올라갑니다(`change_log` 첫 줄에 이유). 같은 값이 `GET /api/runs/<id>`의 `progress.superseded_channels`에도 저장됩니다.
- 이벤트가 한동안 없으면 `: ping` 주석 줄을 보냅니다(기본 15초).

### `POST /api/runs/<id>/resume`

`interrupted` · `failed` · `cancelled` · 예산 초과로 멈춘 `pipeline`/`slot` 실행을 **같은 run id로** 이어서 실행합니다. 저장된 계획·리서치를 다시 쓰고, 끝난 채널은 건너뛰고, 나머지 채널은 마지막 초안·검수부터 계속합니다.

요청(모두 선택): `{"force": false, "options": {"speed": 0, "max_cost_usd": 5, "mode": "live"}}`

- `options.max_cost_usd`: 이 실행의 새 예산 상한(USD, `0`이면 상한 없음). 예산 초과로 멈췄다면 상한을 올려서 이어가세요. 낮추거나 없앨 수도 있어요. 안 주면 **그 실행을 시작할 때의 상한**을 그대로 씁니다. 서버 기본값은 시작할 때 상한이 없었거나, 예산 초과로 멈춘 실행인데 서버 기본값이 그 상한보다 높을 때만 씁니다.
- `running`으로 남은 실행은 먼저 실행하던 프로세스를 확인합니다. 그 프로세스가 없어졌으면(같은 컴퓨터에서 종료됨, 컴퓨터 재시작, 신호가 10분 넘게 없음) `interrupted`로 정리한 뒤 바로 이어서 실행합니다. `force`는 필요 없어요.
- `mode`를 안 주면 원래 실행의 모드를 씁니다(서버 기본값이 `auto`일 때).
- `force: true`: 다른 곳에서 `running`으로 표시된 실행을 억지로 넘겨받아 이어갑니다. 원래 프로세스가 살아 있으면 다음 AI 호출 전에 멈추고, 이벤트·상태·버전·검수·슬롯을 더는 기록하지 않습니다(그때까지 쓴 비용은 사용량에 남아요). 정말 멈춘 게 확실할 때만 쓰세요(실행하던 프로세스가 없어진 실행은 서버·CLI가 알아서 `interrupted`로 정리합니다).
- `slot` 실행은 이어서 도는 동안 슬롯을 다시 `generating`으로 잡아 같은 슬롯의 초안이 두 번 만들어지지 않게 하고, 실패하면 슬롯을 원래 상태로 돌려놓습니다. 그사이 다른 실행이 슬롯을 만드는 중이면 이어서 실행하지 않고, 더 나중에 만든 초안이 슬롯에 있으면 슬롯은 그대로 두고 이 실행의 콘텐츠만 끝냅니다.

응답 `202`: `POST /api/runs`와 같은 링크 + `"resumed": true`. 이벤트는 같은 `events_url`에서 이어집니다(`seq`도 이어짐).

| 오류 | 코드 |
|---|---|
| 없는 실행 | 404 |
| review/revise/edit 작업 | 400 |
| 이 서버에서 이미 실행 중 | 409 |
| 다른 곳에서 아직 실행 중(프로세스가 살아 있음) | 409 (`"can_force": true`, 메시지에 프로세스·마지막 신호와 언제 정리되는지: 같은 컴퓨터의 프로세스면 꺼진 뒤 다시 요청할 때 바로, 다른 컴퓨터·컨테이너면 마지막 신호에서 10분쯤 뒤) |
| 모든 채널을 이미 마침 | 409 |
| 이 실행의 콘텐츠에 재검수·수정 요청이 도는 중 | 409 (`run_id`는 그 작업, `item_id`) |
| 이 실행의 콘텐츠를 사람이 저장하는 중 | 409 (잠시 뒤 다시 시도) |

### `POST /api/runs/<id>/cancel`

이 서버에서 돌고 있는 실행·작업을 멈춥니다. 다음 AI 호출 전에 멈추고(진행 중인 live 호출은 끝날 때까지 기다림), mock 재생은 바로 멈춥니다. 끝난 채널은 저장된 채로 남고, 스트림은 `run.failed`로 끝나며 상태는 `cancelled`가 됩니다. `pipeline`/`slot`은 나중에 이어서 실행할 수 있습니다.

응답 `202`: `{"run_id": "…", "status": "cancelling", "cancel_requested": true, "events_url": "…"}`

재검수·수정 요청·슬롯 초안 작업도 파이프라인 실행과 같은 방식으로 멈춥니다(live 모드에서 AI 호출 재시도를 기다리는 중이어도 바로 멈춰요). 멈춘 슬롯 초안은 `cancelled`(이어서 실행 가능)가 되고 슬롯은 `planned`로 돌아갑니다.

| 오류 | 코드 |
|---|---|
| 없는 실행 | 404 |
| 이미 끝난 실행 | 409 (`run_status`) |
| 다른 곳(CLI 등)에서 아직 실행 중 | 409 (`"run_status": "running"`) — 그 프로그램에서 멈춰 주세요(Ctrl+C). 언제 정리되는지는 이어서 실행의 409와 같아요 |
| 실행하던 프로그램이 이미 멈춘 실행 | 409 (`"recovered": true`, `"run_status": "interrupted"`, `resumable`) — 멈출 게 없어서 409지만, 그 자리에서 `interrupted`로 정리했어요(`error`는 "이미 멈춰 있던 실행이에요. …"). `pipeline`/`slot`은 이어서 실행할 수 있어요. 클라이언트는 `recovered`를 보고 오류 대신 안내로 보여 주면 돼요 |

### `GET /api/runs/<id>/export`

실행 결과 전체를 zip 하나로 내려받습니다: 채널별 붙여넣기용 파일, `review.json`, `research.json`, `sources.md`(출처 목록), `brief.json`, `README.txt`. 헤더는 [콘텐츠 내보내기](#get-apiitemsidexport)와 같습니다. `?info=1`이면 파일 대신 `{filename, content_type, size, notes}` JSON.

---

## 콘텐츠 보관함

콘텐츠(item)는 채널 산출물 하나입니다. 파이프라인 결과는 `it_<run_id>_<channel>` id로 저장되고, 모든 초안 라운드와 사람 수정이 버전으로 쌓입니다.

상태: `draft` 초안 · `needs_changes` 수정 필요(최신 검수 미통과) · `approved` 승인 · `scheduled` 게시 예정 · `published` 게시 완료 · `archived` 보관. **자동 게시는 없어요.** 사람이 채널에 직접 올린 뒤 `published`로 표시하거나, 승인한 LinkedIn·인스타그램 콘텐츠를 대시보드에서 미리보기로 확인하고 [API로 게시](#api-게시-linkedin--인스타그램)할 때(사람이 누를 때 한 건씩)만 `published`가 돼요. `published_via`가 어떻게 올렸는지 알려 줘요: `""` 사람이 직접 올린 기록, `linkedin_api`·`instagram_api` 사람이 확인한 뒤 INSIA가 API로 올림, `fake` 가짜 게시 모드(테스트용, 실제로 올라가지 않음). `published_external_id`는 API로 올린 게시물 id(LinkedIn URN, 인스타그램 미디어 id)예요.

### `GET /api/items`

쿼리: `status`, `channel`, `limit`(기본 200). 최근 수정순입니다.

```json
{"items": [{"id": "it_20260928-101039-7073_linkedin", "run_id": "20260928-101039-7073", "channel": "linkedin",
            "title": "반복 업무를 덜어 낸 방법", "status": "draft", "version": 2, "score": 86, "passed": true,
            "scheduled_at": "", "published_at": "", "published_url": "", "note": "",
            "created_at": "2026-09-28T10:10:39.209Z", "updated_at": "2026-09-28T10:10:39.216Z",
            "approved_version": 0, "approval_forced": false, "approved_score": null, "approved_at": "",
            "published_via": "", "published_external_id": ""}]}
```

`approved_version`·`approved_score`·`approved_at`·`approval_forced`는 마지막 승인 기록입니다(승인한 버전, 그 버전의 검수 점수, 시각, 검수를 통과하지 못한 버전을 "그래도 승인"했는지). 게시한 뒤에도 남는 기록이라, `approval_forced`가 `true`이고 상태가 `approved`·`scheduled`·`published`면 대시보드는 "강제 승인"으로 표시합니다. 승인한 적이 없으면 `0`·`null`·`""`·`false`입니다.

잘못된 `status`/`channel`은 400.

### `GET /api/items/<id>`

`ContentItemDetail` + 내보내기 목록 + API 게시 블록(`publish`)입니다.

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
              "url": "…", "hint": "Word 파일을 만들려면 pip install \"insia-smartagent[export]\"로 python-docx를 설치해 주세요."}],
 "publish": null}
```

`versions`는 오래된 것부터, 마지막이 현재 버전입니다. `source`: `agent`(에이전트) · `human`(사람). `instructions`는 수정 요청 때 사람이 준 지시입니다. `publish`는 [콘텐츠의 API 게시 블록](#콘텐츠-상세의-publish-블록)이에요. API 게시를 한 번도 설정하지 않았거나 꺼져 있으면 `null`이라, 대시보드는 게시 UI를 전혀 그리지 않아요.

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

오류: 제목·본문이 비었거나 너무 김(400; 제목 300자, 본문 10만 자), 없는 콘텐츠(404), 에이전트가 그 콘텐츠를 아직 쓰는 중(409: 재검수·수정 요청 작업, 또는 그 콘텐츠를 만든 실행이 아직 도는 중).

작업이 도는 중이면 저장하지 않고 이렇게 답합니다. `job`은 도는 작업의 종류(`review`, `revise`, `pipeline`, `slot`)예요. 작업이 끝나면 새 버전을 확인한 뒤 다시 저장하세요(대시보드는 고치던 내용을 그대로 두고 오류를 보여 줘요).

```json
{"error": "에이전트가 이 콘텐츠를 수정하는 중이에요 (실행 20260928-101041-bb02). 작업이 끝나면 새 버전을 확인한 뒤 다시 저장해 주세요.",
 "status": 409, "run_id": "20260928-101041-bb02", "job": "revise", "item_id": "it_…_linkedin"}
```

그 콘텐츠를 만든 실행이 아직 도는 중이면:

```json
{"error": "에이전트가 아직 이 콘텐츠를 쓰고 검수하는 중이에요 (실행 20260928-101500-a1b2). 실행이 끝나면 최신 버전을 확인한 뒤 다시 저장해 주세요.",
 "status": 409, "run_id": "20260928-101500-a1b2", "job": "pipeline", "item_id": "it_20260928-101500-a1b2_linkedin"}
```

### `POST /api/items/<id>/review`

재검수 작업을 시작합니다. 본문 `{}` 또는 `{"options": {"mode": "mock", "speed": 1}}`.

응답 `201`: `{"run_id", "kind": "review", "mode", "events_url", "status_url", "cancel_url", "item_id"}`. 진행은 `events_url`로 보고, 끝나면 `GET /api/runs/<run_id>`의 `job.review`나 `GET /api/items/<id>`에서 결과를 봅니다. 검수는 버전을 새로 만들지 않고 작업을 시작할 때의 현재 버전(`job.base_version`)에 붙습니다. 검수하는 동안 사람이 새 버전을 저장했으면 점수는 검수한 버전에만 붙고, 새 버전이 현재 버전(`job.current_version`, 검수 전)으로 남습니다(`job.superseded: true`, 사람이 저장했으면 `superseded_by_human_edit: true`, `run.completed` 이벤트에도 같은 값).

오류: 없는 콘텐츠 404, 버전 없음 400, 같은 콘텐츠에 작업이 이미 도는 중 409, 그 콘텐츠를 만든 실행이 아직 도는 중 409(`run_id`는 그 실행), 사람이 고친 내용을 저장하는 중 409, 동시 작업 초과 429.

### `POST /api/items/<id>/revise`

수정 요청 작업: 최신 검수 의견과 사람의 지시로 새 버전을 쓰고 자동으로 다시 검수합니다.

요청: `{"instructions": "도입부를 두 문장으로 줄이고 사례를 하나 넣어 주세요", "options": {"speed": 1}}` (`instructions`는 선택, 4,000자까지)

응답 `201`: review와 같은 모양(`"kind": "revise"`). 새 버전은 `source: "agent"`, `instructions`에 지시가 저장됩니다. 오류는 review와 같습니다.

수정하는 동안 사람이 새 버전을 저장했으면(예: v3에서 시작했는데 v4가 저장됨) 수정 결과는 **현재 버전이 되지 않습니다**. 수정 결과는 v5로 기록에 남고, 사람이 저장한 v4의 내용이 v6으로 다시 올라가 현재 버전이 됩니다(`change_log` 첫 줄에 이유가 적힘). 내보내기·승인은 계속 사람이 고친 내용을 씁니다. 결과의 `job.superseded_by_human_edit: true`, `job.version`(수정 결과 v5), `job.current_version`(v6), `job.base_version`(v3)과 `run.completed` 이벤트의 같은 필드로 알 수 있습니다. 수정 결과를 쓰고 싶으면 v5 내용을 확인해서 직접 고쳐 저장하거나 다시 수정 요청을 보내세요.

수정본을 저장한 뒤 **재검수하는 동안** 사람이 새 버전을 저장해도 마찬가지입니다. 재검수 점수는 수정본에 붙고, 사람이 저장한 버전이 그보다 새 버전이라 그대로 현재 버전으로 남습니다. 이때도 `job.superseded`·`superseded_by_human_edit`가 `true`이고 `job.current_version`이 사람이 저장한 버전이라, 대시보드가 같은 안내를 보여 줄 수 있어요.

### `POST /api/items/<id>/status`

요청: `{"status": "approved", "force": false, "scheduled_at": "2026-10-05", "published_url": "https://…", "note": "메모"}` (`status`만 필수)

| 바꾸기 | 조건 |
|---|---|
| draft · needs_changes → `approved` | 최신 버전이 검수를 통과했거나 `force: true`("그래도 승인") |
| approved → `scheduled` | `scheduled_at` 필요 (`YYYY-MM-DD` 또는 ISO 시각) |
| approved · scheduled → `published` | `published_url`은 선택(`http(s)://`). `published_at`은 서버가 기록 |
| 무엇이든 → `archived`, archived → `draft` | 보관 · 복원 |
| approved → draft · needs_changes, scheduled → approved | 되돌리기 |

응답 `200 {"item": {…ContentItem…}}`. 승인하면 `approved_version`·`approved_score`·`approved_at`이 기록되고, `force: true`로 검수를 통과하지 못한(또는 검수 전) 버전을 승인하면 `approval_forced: true`로 남습니다.

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
Content-Disposition: attachment; filename="2026-09-28_instagram_v2.zip"; filename*=UTF-8''2026-09-28_instagram_%EB%8D%B0%EB%AA%A8-…_v2.zip
Cache-Control: no-store
X-Content-Type-Options: nosniff
Content-Security-Policy: default-src 'none'; sandbox
X-Insia-Notes: PNG%20%EB%8C%80%EC%8B%A0%20slides.html%EC%9D%84%20%EB%84%A3%EC%97%88%EC%96%B4%EC%9A%94…
```

- 파일 이름은 `<YYYY-MM-DD>_<채널>_<제목 슬러그>_v<버전>.<확장자>`이고(예전 버전을 받아도 현재 버전 파일과 이름이 겹치지 않아요), 한글 이름은 `filename*`(RFC 5987)에 있습니다. 대시보드는 `<a href="/api/items/<id>/export?format=…" download>`로 받으면 됩니다(같은 출처라 쿠키 인증도 됩니다).
- `X-Insia-Notes`: 함께 보여 줄 한국어 안내(예: PNG 대신 slides.html을 넣음, 사업계획서에 팀원 실명·학교명·직장명이 있음). 여러 줄을 `\n`으로 이은 뒤 퍼센트 인코딩했으니 `decodeURIComponent(value).split("\n")`로 읽으세요. 안내가 없으면 헤더도 없습니다.
- `?info=1` → `{"filename": "2026-09-28_instagram_데모-…_v2.zip", "content_type": "application/zip", "size": 5175, "notes": ["PNG 대신 slides.html을 넣었어요 — …"]}`

오류: 채널에 없는 형식·없는 버전(400), 없는 콘텐츠(404), python-docx 미설치(501 `{"error": "… pip install \"insia-smartagent[export]\" …", "package": "python-docx", "extra": "export"}`).

---

## API 게시 (LinkedIn · 인스타그램)

승인한 `linkedin` 콘텐츠를 LinkedIn 개인 프로필에, `instagram` 콘텐츠를 인스타그램 캐러셀로 올려요. 사용자마다 **자기 개발자 앱**(LinkedIn 앱, Meta의 Instagram API with Instagram Login)을 연결해서 쓰고, INSIA는 공용 앱 키를 싣지 않아요. 네이버 블로그·사업계획서는 API 게시가 없어요.

- **자동 게시·예약 게시는 없어요.** 한 번의 사람 확인이 게시물 하나예요. 흐름은 늘 `미리보기 → 사람이 보고 확인 → 게시`이고, 서버는 미리보기 때 저장한 바로 그 내용(본문·캡션·이미지 바이트의 해시)만 보내요. 백그라운드에서 스스로 다시 보내지 않아요.
- 게시와 결과 정리는 브라우저의 대시보드에서 사람이 누른 요청만 받아요([사람 요청](#요청-보안)). 스크립트(`Authorization: Bearer`)로 게시하는 것은 LinkedIn API 약관 위반이라 서버가 403으로 막아요. 터미널에서는 `insia publish send`가 TTY에서만, 미리보기마다 새로 만든 무작위 확인 코드를 입력해야 돌아요.
- 설정하지 않으면 지금과 똑같아요: 콘텐츠 상세의 `publish`가 `null`이고, 다른 응답도 그대로예요. `INSIA_PUBLISH=0`이면 기능 전체가 꺼져서 상태 조회 `GET /api/publish`만 `200 {"enabled": false, …}`로 답하고(대시보드가 이것을 보고 "API 게시 연결" 카드를 숨겨요), 플랫폼을 부를 수 있는 나머지 게시 경로는 모두 `409 {"code": "disabled"}`예요. 기록만 다루는 경로 — `GET /api/items/<id>/publish` · `GET /api/publish/attempts/<pa>` · `PUT …/permalink` · `POST …/resolve`(사람 요청만) — 는 꺼져 있어도 답해요. 끄기 전에 결과 불명(`unknown`)으로 남은 시도가 있으면 그 콘텐츠의 `publish` 블록이 `state: "disabled"`로 잠금과 결과 불명 카드를 보여 주고, 정리하면 잠금이 풀려요.
- 토큰·client secret·OAuth code·state는 어떤 응답·로그에도 나오지 않아요. 앱 정보는 `client_id_set`·`client_secret_set`처럼 있는지 여부만, 계정 id는 끝 3자(`id_hint`)만 보여요.
- 인스타그램은 시험 중이라 기본으로 꺼져 있고 `INSIA_PUBLISH_INSTAGRAM=1`일 때만 켜져요. 이미지를 인스타그램이 가져갈 **공개 HTTPS 주소**가 있어야 해요. 구성 A는 `serve --media-port … --media-base-url https://…` 또는 환경 변수 `INSIA_MEDIA_PORT`·`INSIA_MEDIA_BASE_URL`, 구성 B는 `INSIA_MEDIA_BASE_URL`과 `INSIA_PUBLIC_HOSTS`(미디어 포트 없이)로 정해요. 터미널 명령(`insia publish …`)은 `serve`의 옵션을 모르니 환경 변수로 저장해 두세요(운영 안내 12-4·12-5). 없으면 인스타그램은 `unavailable`이고 지금처럼 카드 묶음을 받아 앱에서 올리면 돼요.

아래 예시는 테스트 워크스페이스에서 가짜 게시 모드(`INSIA_PUBLISH_FAKE=1`)로 받은 실제 응답이에요(긴 글은 `…`로 줄였어요). 가짜 게시 모드는 메모리 속 LinkedIn·인스타그램이 늘 성공하는 모드라, 게시물 주소가 `https://example.invalid/…`이고 `published_via`가 `fake`예요. 임시 폴더 워크스페이스에서만 켜져요.

| 경로 | 하는 일 |
|---|---|
| `GET /api/publish[?check=1]` | 기능·플랫폼별 준비 상태 (`check=1`이면 토큰이 살아 있는지 플랫폼에 실제로 확인) |
| `PUT /api/publish/linkedin/app` | LinkedIn 앱 정보(Client ID·Secret·Redirect URI) 저장 |
| `POST /api/publish/linkedin/connect` | LinkedIn 동의 화면 주소 발급 (`redirect`/`paste` 모드) |
| `POST /api/publish/linkedin/complete` | 동의한 뒤 이동한 주소를 붙여 넣어 연결 마치기 |
| `GET /oauth/linkedin/callback` | LinkedIn이 돌려보내는 곳 (토큰 없음, 언제나 303) |
| `PUT /api/publish/instagram/token` | 인스타그램 토큰 확인·저장 (인스타그램이 켜져 있을 때만) |
| `DELETE /api/publish/<linkedin\|instagram>[?forget_app=1]` | 연결 해제 |
| `POST /api/items/<id>/publish/preview` | 미리보기 만들기 |
| `GET /api/publish/previews/<pv>/slides/<n>.jpg` | 인스타그램 미리보기 이미지 |
| `POST /api/items/<id>/publish` | **확인 게시 시작 (사람 요청만)** → 202 |
| `GET /api/items/<id>/publish` | 그 콘텐츠의 게시 기록 |
| `GET /api/publish/attempts/<pa>` | 게시 시도 하나 (진행 확인) |
| `PUT /api/publish/attempts/<pa>/permalink` | 게시했는데 주소가 빈 기록에 주소 넣기 |
| `POST /api/publish/attempts/<pa>/resolve` | **게시됐는지 모르는 기록 정리 (사람 요청만)** |
| `POST /api/publish/attempts/<pa>/check` | 인스타그램에서 다시 확인 (읽기만) |

### `GET /api/publish`

네트워크를 쓰지 않는 상태 조회예요(`?check=1`만 플랫폼에 물어봐요). 대시보드의 "API 게시 연결" 카드가 이 응답으로 그려져요.

```json
{"enabled": true, "configured": true, "fake": true,
 "media": {"url": "", "mode": "none", "port": null, "valid": false,
           "reason": "미디어 공개 주소(INSIA_MEDIA_BASE_URL / --media-base-url)가 없어요."},
 "platforms": {
  "linkedin": {"label": "LinkedIn", "channels": ["linkedin"], "state": "connected", "ready": true,
               "reason": "가짜 게시 계정(LinkedIn 개인 프로필)에 올려요.", "blockers": [],
               "app": {"client_id_set": true, "client_secret_set": true, "source": "env",
                       "redirect_uri": "http://localhost:8765/oauth/linkedin/callback", "redirect_uri_source": "workspace"},
               "account": {"id_hint": "…r01", "name": "가짜 게시 계정", "kind": "LinkedIn 개인 프로필"},
               "token": {"expires_at": "2026-11-27T23:48:01Z", "days_left": 59, "estimated": false,
                         "scopes": ["openid", "profile", "w_member_social"]},
               "api_version": "202609", "api_version_sunset": "2027-09-15", "api_version_warning": ""},
  "instagram": {"label": "인스타그램", "channels": ["instagram"], "state": "connected", "ready": true,
                "reason": "@insia.fake에 올려요.", "blockers": [], "beta": true, "enabled_by": "env",
                "account": {"id_hint": "…000", "username": "@insia.fake", "account_type": "BUSINESS",
                            "kind": "인스타그램 비즈니스 계정"},
                "token": {"expires_at": "2026-11-27T23:48:09Z", "days_left": 59, "estimated": false,
                          "refreshed_at": "2026-09-28T23:48:09Z", "auto_refresh": true, "refresh_failed": false},
                "api_version": "v25.0", "requirements": {"public_https": true, "render": true}}}}
```

| 필드 | 뜻 |
|---|---|
| `enabled` | API 게시가 켜져 있는지. `false`면 `{"enabled": false, "configured": false, "fake": false, "reason": "API 게시가 꺼져 있어요 (INSIA_PUBLISH=0).", "media": {…}, "platforms": {}}`만 와요 |
| `configured` | 앱 정보·연결이 하나라도 저장돼 있거나 관련 환경 변수(`INSIA_LINKEDIN_CLIENT_ID`, `INSIA_PUBLISH_INSTAGRAM=1`)가 있는지. `false`면 보관함 화면에 게시 UI를 그리지 않아요 |
| `fake` | 가짜 게시 모드(테스트용). 대시보드는 "실제로 올라가지 않아요" 띠를 띄워요 |
| `media` | 인스타그램이 이미지를 가져갈 공개 주소. `mode`: `listener`(미디어 전용 포트, 구성 A) · `main`(대시보드 포트, 구성 B) · `none`. `valid: false`면 `reason`에 한국어 이유. 구성 A는 `listening`(리스너가 열렸는지)도 와요 |
| `platforms.*.state` | `disabled` 꺼짐(인스타그램 시험 기능 꺼짐 포함) · `not_configured` 앱 정보 없음 · `not_connected` 계정 연결 안 됨 · `connected` 준비됨 · `expiring` 곧 만료(게시는 됨) · `needs_reconnect` 다시 연결 필요 · `unavailable` 요구 조건이 없음(`blockers`: `public_url_missing` 공개 주소 없음, `media_port_unavailable` 미디어 포트를 열 수 없음, `render_unavailable` 카드 이미지를 그릴 브라우저·한글 글꼴 없음) |
| `ready` · `reason` | 지금 게시할 수 있는지와 버튼 옆에 그대로 보여 줄 한국어 한 문장 |
| `app` | LinkedIn 앱 정보가 있는지만(`*_set`), 어디서 왔는지(`source`: `env` 환경 변수라 여기서 못 바꿈 · `workspace` · `""`), 지금 쓰는 Redirect URI와 그 출처(`env` · `workspace` · `default`). 기본 주소(`default`)는 앱 정보를 저장하거나 처음 연결할 때 워크스페이스에 저장돼서(`workspace`), 다른 포트로 켠 서버나 `insia publish connect linkedin`도 LinkedIn에 등록한 그 주소를 그대로 보내요 |
| `account` · `token` | 연결한 계정(이름은 LinkedIn 약관 때문에 저장하지 않고 메모리에만 둬서 서버를 다시 켜면 `""`일 수 있어요)과 토큰 만료. 연결 전에는 `null` |
| `api_version_*` | LinkedIn API 버전과 지원 종료일. 종료 60일 전부터 `api_version_warning`에 안내가 와요 |

### `PUT /api/publish/linkedin/app`

요청: `{"client_id": "86abcdefghijkl", "client_secret": "…", "redirect_uri": "http://localhost:8765/oauth/linkedin/callback"}`. 필드를 빼면 저장된 값을 그대로 두고, `""`이면 지워요. `redirect_uri`를 빼고 저장된 주소도 없으면 지금 보이는 기본 주소를 저장해요(나중에 다른 포트·CLI에서도 같은 주소). `client_secret`은 저장만 하고 다시 보여 주지 않아요.

응답 `200`: `GET /api/publish`의 `linkedin` 블록(비밀값 없음).

```json
{"label": "LinkedIn", "channels": ["linkedin"], "state": "not_connected", "ready": false,
 "reason": "LinkedIn 계정을 연결해야 해요.", "blockers": [],
 "app": {"client_id_set": true, "client_secret_set": true, "source": "workspace",
         "redirect_uri": "http://localhost:8765/oauth/linkedin/callback", "redirect_uri_source": "workspace"},
 "account": null, "token": null, "api_version": "202609", "api_version_sunset": "2027-09-15", "api_version_warning": ""}
```

오류: 환경 변수로 정한 값을 바꾸려 함 `409 {"code": "env_locked"}`, Redirect URI 형식(절대 주소, `#` 없음, `http://`는 `localhost`·`127.0.0.1`·`[::1]`만) `400 {"code": "invalid_input"}`, 모르는 필드 400.

### `POST /api/publish/linkedin/connect`

본문 `{}`. 1회용 state(10분)를 만들고 LinkedIn 동의 화면 주소를 줘요. 대시보드를 연 주소의 출처(스킴·호스트·포트)가 Redirect URI의 출처와 같으면 `redirect` 모드(LinkedIn이 INSIA 콜백으로 바로 돌려보냄), 다르면 `paste` 모드(동의한 뒤 이동한 주소창 주소를 붙여 넣음)예요.

```json
{"mode": "paste",
 "authorize_url": "https://www.linkedin.com/oauth/v2/authorization?response_type=code&client_id=86abcdefghijkl&redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Foauth%2Flinkedin%2Fcallback&state=…&scope=openid%20profile%20w_member_social",
 "expires_in": 600, "redirect_uri": "http://localhost:8765/oauth/linkedin/callback",
 "open_url": "http://localhost:8765/#/brand/connections"}
```

- `redirect` 모드는 응답에 쿠키가 붙어요: `Set-Cookie: insia_oauth=<state의 HMAC>; Path=/oauth/; Max-Age=600; HttpOnly; SameSite=Lax[; Secure]`. 콜백은 이 쿠키가 있는 브라우저에서 온 것만 받아요(로그인 CSRF 방지).
- `open_url`은 `paste` 모드이고 Redirect URI의 호스트가 루프백 이름이거나 `--public-host`에 있을 때만 와요("LinkedIn 앱에 등록한 주소로 대시보드를 열면 바로 연결돼요").
- 오류: 앱 정보 없음 `409 {"code": "not_configured"}`, Redirect URI 형식 400.

### `POST /api/publish/linkedin/complete`

요청: `{"url": "http://localhost:8765/oauth/linkedin/callback?code=…&state=…"}` (`code=…&state=…`만 붙여 넣어도 돼요). **이 서버가 만든 state만, 한 번만, 10분 안에만** 받아요. 출처 검사는 하지 않아요(대시보드를 LAN 주소로 열었을 때를 위한 경로라서). 응답 `200`: `GET /api/publish`의 `linkedin` 블록(`"state": "connected"`).

| 오류 | 응답 |
|---|---|
| state가 없음·만료·이미 씀·이 서버가 만든 것이 아님 | `400 {"error": "연결 요청이 만료됐거나 올바르지 않아요. 다시 연결해 주세요.", "code": "oauth_state"}` |
| LinkedIn에서 취소함 (`error=user_cancelled_*`) | `400 {"code": "oauth_cancelled"}` |
| 주소 모양이 다름(경로가 Redirect URI와 다름, `http(s)` 아님) | `400 {"code": "invalid_input"}` |
| 코드 교환 실패 | `502 {"code": "exchange_failed"}` |
| 잘못된 state가 1분에 10번 넘음 | `429` + `Retry-After` (로그인 제한과 따로 세요) |

### `GET /oauth/linkedin/callback`

인증 없이 여는 유일한 페이지라 HTML을 만들지 않아요. 무슨 일이 있었든 `303`으로 대시보드의 고정 주소로 보내고, 쿼리 값(`error_description` 등)이나 LinkedIn 이름을 응답에 넣지 않아요.

```
HTTP/1.1 303 See Other
Location: /#/brand/connections/linkedin/ok
Content-Security-Policy: default-src 'none'; frame-ancestors 'none'
X-Frame-Options: DENY
Referrer-Policy: no-referrer
Cache-Control: no-store
X-Content-Type-Options: nosniff
Set-Cookie: insia_oauth=; Path=/oauth/; Max-Age=0; HttpOnly; SameSite=Lax
Content-Type: text/plain; charset=utf-8
```

`Location` 끝의 결과는 `ok` · `cancelled` · `expired` · `exchange_failed` · `invalid` 중 하나예요(state 없음·재사용·쿠키 불일치는 `invalid`). 대시보드가 결과를 보고 한국어 문구를 그려요. 잘못된·만료된 state는 IP마다 1분에 10번까지이고, 넘으면 `429`(같은 보안 헤더 + `Retry-After`)예요. 접근 로그에는 `/oauth/` 요청의 쿼리(code·state)를 남기지 않아요.

### `PUT /api/publish/instagram/token`

요청: `{"access_token": "IGAA…"}` (Meta 개발자 앱의 Generate token으로 만든 장기 토큰). `/me`로 계정을 확인하고(프로페셔널 계정만), 바로 한 번 갱신을 시도해 정확한 만료일을 얻은 뒤 저장해요. 응답 `200`: `instagram` 블록(토큰 없음).

```json
{"label": "인스타그램", "channels": ["instagram"], "state": "connected", "ready": true, "reason": "@insia.fake에 올려요.",
 "blockers": [], "beta": true, "enabled_by": "env",
 "account": {"id_hint": "…000", "username": "@insia.fake", "account_type": "BUSINESS", "kind": "인스타그램 비즈니스 계정"},
 "token": {"expires_at": "2026-11-27T23:48:09Z", "days_left": 59, "estimated": false,
           "refreshed_at": "2026-09-28T23:48:09Z", "auto_refresh": true, "refresh_failed": false},
 "api_version": "v25.0", "requirements": {"public_https": true, "render": true}}
```

`estimated: true`는 발급한 지 24시간이 안 돼 아직 갱신할 수 없어서 만료일을 "붙여 넣은 시각 + 60일"로 추정했다는 뜻이에요(24시간 뒤 자동 갱신으로 맞춰요). 오류: 인스타그램이 꺼져 있음 `409 {"code": "disabled"}`, 토큰이 틀림·만료 `400 {"code": "invalid_token"}`, 개인 계정 `400 {"code": "account_type"}`.

### `DELETE /api/publish/<platform>`

INSIA에 저장한 그 플랫폼의 토큰을 지워요(`?forget_app=1`이면 LinkedIn 앱 정보도). 플랫폼 쪽 앱 권한은 사람이 거둬야 해서 응답에 방법이 와요.

```json
{"label": "LinkedIn", "state": "not_connected", "ready": false, "reason": "LinkedIn 계정을 연결해야 해요.", "...": "...",
 "account": null, "token": null,
 "revoke_hint": "INSIA에서 토큰을 지웠어요. LinkedIn 설정 → 데이터 개인정보 → 권한 있는 서비스에서 앱 권한도 지울 수 있어요."}
```

게시 중이거나 결과 확인이 필요한 기록이 있으면 `409 {"code": "item_locked", "attempt_id": …}`.

### 콘텐츠 상세의 `publish` 블록

`GET /api/items/<id>`에 붙어서, 대시보드가 요청 하나로 버튼 상태를 알 수 있어요. API 게시가 꺼져 있거나 한 번도 설정하지 않았으면 `null`이에요.

```json
"publish": {"platform": "linkedin", "available": true, "state": "connected",
            "reason": "가짜 게시 계정(LinkedIn 개인 프로필)에 올려요.", "blocked_by": "", "blockers": [],
            "active_attempt": null, "last_attempt": null}
```

- `platform`이 `null`이면(네이버 블로그·사업계획서) 버튼을 그리지 않아요.
- `available`: 지금 "API로 게시"를 누를 수 있는지(`state`가 `connected`·`expiring`이고, 막는 이유가 없고, 진행 중인 시도가 없음).
- `blocked_by`: `not_approved` 승인 전 · `version_changed` 승인한 뒤 새 버전 · `published` 게시 완료 · `published_attempt` 이 버전은 API로 올렸는데 보관함 상태를 바꾸지 못함(시도에 `item_update_error`) · `already_published` 이 버전은 이미 API로 올렸고 사람이 콘텐츠를 옮김(보관 → 복원 → 승인 등: ‘게시 완료 표시’로 다시 표시하거나, 고쳐서 새 버전으로 승인) · `archived` 보관 · `agent_job` 에이전트가 작업 중 · `""`.
- `active_attempt`: 게시 중(`sending`)이거나 결과 확인이 필요한(`unknown`) 시도. 있으면 편집·재검수·보관이 잠겨요. `last_attempt`: 이 플랫폼의 가장 최근 시도. 둘 다 [시도 JSON](#post-apiitemsidpublish) 모양이에요.

### `POST /api/items/<id>/publish/preview`

지금 올리면 보낼 내용을 그대로 만들어 30분 동안 저장해요. 요청:

- LinkedIn: `{"platform": "linkedin", "options": {"visibility": "PUBLIC"}}` — `visibility`는 `PUBLIC`(전체 공개, 기본) · `CONNECTIONS`(1촌 공개).
- 인스타그램: `{"platform": "instagram", "options": {"is_ai_generated": false}}` — **`is_ai_generated`는 꼭 `true`나 `false`로 보내야 해요**(게시마다 사람이 골라요, 기본값 없음). 빠지거나 `0`·`"false"`처럼 bool이 아니면 아무것도 그리기 전에 `400 {"error": "AI 정보 라벨을 붙일지 골라 주세요 (options.is_ai_generated: true 또는 false)", "code": "invalid_options"}`예요.
- `platform`은 빼도 돼요(콘텐츠 채널로 정해져요).

응답 `200` (LinkedIn):

```json
{"preview_id": "pv_7d518a8567bbfa100a024cea",
 "preview_hash": "sha256:fd273ee1593d3ab9a5c53bb0257036e1938b860bf8079fa487e9a8fd0a19ab34",
 "expires_at": "2026-09-29T00:18:01.996Z", "platform": "linkedin",
 "item": {"id": "it_0a803d87a98b", "version": 1, "title": "반복 업무를 덜어 낸 방법", "channel": "linkedin",
          "approved_version": 1, "approval_forced": true, "approved_score": null},
 "account": {"name": "가짜 게시 계정", "kind": "LinkedIn 개인 프로필", "id_hint": "…r01"},
 "content": {"text": "혼자 창업하면 마케팅은 늘 '이번 주만 넘기고'가 돼요.\n\n…\n\n#1인창업 #AI마케팅 #콘텐츠마케팅",
             "chars": 1014, "limit": 3000, "hashtags": ["#1인창업", "#AI마케팅", "#콘텐츠마케팅"],
             "options": {"visibility": "PUBLIC"}, "visibility_label": "전체 공개", "hashtag_mode": "plain"},
 "slides": [], "errors": [],
 "warnings": [{"level": "warning", "code": "format_length", "message": "분량(공백 포함): 992자 (기준 1,300~2,000자 (최대 3,000자))"},
              {"level": "warning", "code": "forced_approval", "message": "검수를 통과하지 않은 버전(검수 없음)을 그래도 승인했어요."}],
 "notices": [{"code": "single_post", "message": "지금 이 글 한 건만 올려요. INSIA는 예약·반복 게시를 하지 않아요. LinkedIn API 이용약관이 자동 게시를 금지하기 때문이에요."},
             {"code": "edit_on_platform", "message": "올린 뒤 고치거나 지우려면 LinkedIn에서 직접 해야 해요."},
             {"code": "manual_done", "message": "이미 LinkedIn에 직접 올렸다면 여기서 게시하지 말고 ‘게시 완료 표시’를 눌러 주세요."}],
 "quota": null, "first_comment_link": "",
 "request_preview": [{"method": "POST", "url": "https://api.linkedin.com/rest/posts",
                      "headers": {"Authorization": "Bearer ***", "Linkedin-Version": "202609",
                                  "X-Restli-Protocol-Version": "2.0.0", "Content-Type": "application/json"},
                      "body": {"author": "urn:li:person:…r01", "commentary": "혼자 창업하면 …", "visibility": "PUBLIC",
                               "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [],
                                                "thirdPartyDistributionChannels": []},
                               "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": false}}],
 "can_publish": true}
```

인스타그램은 실제로 보낼 JPEG(1080×1350)를 이때 한 번 그려서 저장하고, 게시할 때 다시 그리지 않아요(10~30초 걸릴 수 있어요). 달라지는 부분만 옮기면:

```json
{"platform": "instagram",
 "account": {"name": "@insia.fake", "kind": "인스타그램 비즈니스 계정", "id_hint": "…000", "username": "@insia.fake"},
 "content": {"text": "혼자 콘텐츠를 다 쓰는 대표님께: …\n#1인창업 #AI마케팅자동화 #SNS운영 #사업계획서 #AI에이전트",
             "chars": 923, "limit": 2200,
             "hashtags": ["#1인창업", "#AI마케팅자동화", "#SNS운영", "#사업계획서", "#AI에이전트"],
             "options": {"is_ai_generated": false}},
 "slides": [{"n": 1, "alt": "인디고 배경에 \"블로그·인스타·사업계획서, 혼자 다 쓰고 있다면\"이라는 큰 제목과 …",
             "bytes": 70966, "width": 1080, "height": 1350,
             "sha256": "9fca107012f5ff234e3f159891840010e0fc346d5ec5473d270ad272f7178334",
             "url": "/api/publish/previews/pv_b1f584cf8e18eaf96885ab13/slides/1.jpg"}],
 "notices": [{"code": "no_delete", "message": "인스타그램 API로 올린 게시물은 INSIA에서 지울 수 없어요. 잘못 올렸다면 인스타그램 앱에서 직접 삭제해야 해요."},
             {"code": "public_media", "message": "카드 이미지 9장을 잠깐 공개 주소(https://example.invalid/pub/m/…)에 올려 인스타그램이 가져가게 해요. 게시가 끝나면 바로 지워요(늦어도 24시간 안에)."},
             {"code": "quota", "message": "오늘 남은 게시 한도: 50/50개"},
             {"code": "no_tags", "message": "사람 태그·공동 작업자·유료 파트너십 표시는 API로 넣을 수 없어요. 필요하면 앱에서 올려 주세요."},
             {"code": "single_post", "message": "지금 이 게시물 한 건만 올려요. INSIA는 예약·반복 게시를 하지 않아요."},
             {"code": "manual_done", "message": "이미 인스타그램 앱에서 직접 올렸다면 여기서 게시하지 말고 ‘게시 완료 표시’를 눌러 주세요."}],
 "quota": {"used": 0, "total": 50},
 "request_preview": [{"method": "POST", "url": "https://graph.instagram.com/v25.0/<IG_ID>/media",
                      "fields": {"image_url": "https://example.invalid/pub/m/<token>/01.jpg", "is_carousel_item": "true",
                                 "alt_text": "인디고 배경에 …"}}]}
```

- 고칠 부분이 있으면 `200`에 `errors`가 담기고 `can_publish: false`예요(대화상자가 오류를 보여 주고 게시 버튼을 꺼요). 그런 미리보기도 저장하지만 게시에는 쓸 수 없어요.
- 미리보기를 만들 수 없을 때만 4xx: 앱 정보 없음 `409 not_configured` · 연결 안 됨 `409 not_connected` · 다시 연결 필요 `409 reconnect` · 요구 조건 없음 `409 unavailable`(`blockers`) · 승인 전·승인 뒤 새 버전·보관·게시 완료 `409 not_publishable`(`blocked_by`) · 이 버전을 이미 API로 올림 `409 already_published`(`permalink`) · 게시 중·결과 확인 필요 `409 item_locked` · 에이전트 작업 중 `409 agent_job` · 다른 미리보기의 카드를 그리는 중 `409 busy` · 카드 이미지를 그리지 못함 `422 render` · 네이버 블로그·사업계획서 `409 not_publishable`.
- 확인 코드는 이 응답에 **없어요.** 대시보드는 체크박스 + `preview_hash` + 사람 요청 검사로 확인하고, CLI의 확인 코드는 `insia publish send` 프로세스 안에서만 만들어 그 터미널에만 보여 줘요.
- IP마다 1분에 10번까지예요(넘으면 `429 {"code": "too_many"}` + `Retry-After`).

`GET /api/publish/previews/<pv>/slides/<n>.jpg`는 저장한 JPEG를 그대로 줘요(`Content-Type: image/jpeg`, `Cache-Control: no-store`). 만료됐거나 없는 미리보기·장 번호는 404예요.

### `POST /api/items/<id>/publish`

확인한 미리보기를 보내기 시작해요. **브라우저의 대시보드에서 사람이 누른 요청만** 받아요([사람 요청](#요청-보안)).

요청: `{"platform": "linkedin", "preview_id": "pv_7d518a8567bbfa100a024cea", "preview_hash": "sha256:fd273ee1…", "confirm": true}`

응답 `202`:

```json
{"attempt": {"id": "pa_370a9fd598f1aa12e278f8a2", "item_id": "it_0a803d87a98b", "version": 1, "platform": "linkedin",
             "preview_id": "pv_7d518a8567bbfa100a024cea", "payload_hash": "sha256:fd273ee1…",
             "account_id": "fakeMember01", "status": "sending", "step": "", "external_id": "", "permalink": "",
             "error_code": "", "error": "", "requested_by": "dashboard@127.0.0.1", "resolved_by": "",
             "created_at": "2026-09-28T23:48:02.000Z", "updated_at": "2026-09-28T23:48:02.000Z", "finished_at": "",
             "progress": {"done": 0, "total": 1}, "step_label": "", "visibility": "PUBLIC", "item_update_error": ""},
 "poll_url": "/api/publish/attempts/pa_370a9fd598f1aa12e278f8a2"}
```

게시는 백그라운드에서 돌아요. 대시보드는 `poll_url`을 2초마다 읽어요. 끝나면:

```json
{"attempt": {"id": "pa_370a9fd598f1aa12e278f8a2", "status": "published", "step": "write",
             "external_id": "urn:li:share:7000000000000000002",
             "permalink": "https://example.invalid/linkedin/feed/update/urn:li:share:7000000000000000002/",
             "finished_at": "2026-09-28T23:48:02.004Z", "progress": {"done": 1, "total": 1}, "...": "..."}}
```

그리고 콘텐츠가 `published`가 되고 `published_at`(게시 시각)·`published_url`(게시물 주소, 받지 못했으면 빈 값)·`published_via`(`linkedin_api`·`instagram_api`, 가짜 게시 모드는 `fake`)·`published_external_id`가 모두 이 게시의 값으로 바뀌어요. 보관 → 복원 → 고쳐서 다시 게시했을 때 예전 게시의 주소·시각은 남지 않아요.

시도 JSON:

| 필드 | 뜻 |
|---|---|
| `status` | `sending` 보내는 중 · `published` 게시됨 · `failed` 실패(아무것도 올라가지 않음) · `unknown` 결과 모름(올라갔을 수도 있음, 콘텐츠 잠김) · `abandoned` 사람이 "안 올라갔어요"로 정리 |
| `step` | 진행 단계: `check` · `media`·`self_check`(인스타그램 이미지 준비·공개 주소 확인) · `children 3/8`(인스타그램 이미지 등록) · `polling` · `carousel` · `write`(되돌릴 수 없는 게시 요청) · `permalink` |
| `step_label` | 그 단계를 사람이 읽는 말: "연결 확인" · "이미지 올릴 준비" · "공개 주소 확인" · "이미지 등록 3/8" · "게시 요청 보내는 중" …. 대시보드 진행 표시와 `insia publish send`가 같은 말을 써요. 말이 없는 단계는 `""` |
| `progress` | `{"done", "total"}`. `total`은 LinkedIn 1, 인스타그램 `슬라이드 수 + 3`(부모, 게시, 링크). 인스타그램 9장이면 끝났을 때 `{"done": 12, "total": 12}` |
| `error_code` · `error` | 실패·결과 모름의 이유(플랫폼 오류 코드와 한국어 문장). 예: `"timeout"` · "LinkedIn의 응답을 받지 못했어요. 글이 올라갔을 수도 있어요. LinkedIn 내 활동에서 확인한 뒤 알려 주세요." |
| `requested_by` · `resolved_by` | 누가 게시했는지(`dashboard@<클라이언트>`, `cli:<사용자>@<호스트>`)와 결과 불명을 누가 어떻게 정리했는지 |
| `visibility` · `is_ai_generated` | 그 게시에 고른 옵션 (LinkedIn · 인스타그램) |
| `item_update_error` | 게시는 됐지만 보관함 상태를 바꾸지 못했을 때의 안내("‘게시 완료 표시’를 눌러 주세요"). 그 버전의 API 게시 버튼은 `blocked_by: published_attempt`로 꺼져요 |
| `permalink_missing` | (있을 때만) 게시는 됐지만 주소를 받지 못함 → [주소 넣기](#put-apipublishattemptspapermalink) |
| `candidates` | (있을 때만, 인스타그램) 다시 확인해서 올라간 건 알았지만 그 시도 뒤에 올라온 게시물이 여러 개라 하나로 정하지 못했을 때의 후보 `[{"id", "permalink", "timestamp"}]`(최근순, 10개까지, 다른 INSIA 게시 기록의 게시물은 빼요). 대시보드는 목록으로 보여 주고 사람이 고른 주소를 [주소 넣기](#put-apipublishattemptspapermalink)로 보내요. 터미널은 `insia publish resolve <pa> --check`가 목록을 보여 주고 `--permalink <주소>`로 넣어요 |

| 오류 | 응답 |
|---|---|
| `confirm`이 `true`가 아님 | `400 {"code": "invalid_input"}` |
| 사람 요청이 아님(Bearer 토큰, 다른 사이트, `Sec-Fetch-Site`·`Origin` 없음) | `403 {"error": "API 게시는 대시보드에서 사람이 직접 눌러야 해요. 스크립트(Bearer 토큰)나 다른 사이트에서는 게시할 수 없어요.", "code": "not_human"}` |
| 미리보기가 30분 지남·이미 씀·없음 | `409 {"code": "preview_expired"}` |
| 해시 불일치, 확인한 뒤 내용·계정이 바뀜, CLI에서 만든 미리보기, 다른 콘텐츠·플랫폼의 미리보기 | `409 {"code": "changed"}` |
| 이미 게시 중·결과 확인 필요 | `409 {"code": "item_locked", "attempt_id": …, "attempt_status": …}` |
| 이 버전은 이미 게시함 | `409 {"code": "already_published", "permalink": …, "attempt_id": …}` |
| 연결·준비 안 됨 | `409` `not_connected` · `reconnect` · `unavailable` · `not_configured` |
| 같은 콘텐츠를 에이전트가 작업 중 | `409 {"code": "agent_job", "run_id": …}` |
| 같은 플랫폼의 다른 게시가 진행 중(대기열 없음) | `409 {"code": "busy"}` "다른 게시가 진행 중이에요. 끝난 뒤 다시 눌러 주세요." |
| 고칠 부분이 있는 미리보기 | `422 {"code": "validation", "errors": […]}` |
| IP마다 1분에 5번 넘음 | `429 {"code": "too_many"}` + `Retry-After` |

실제 전송 단계의 오류(플랫폼이 거절, 한도, 공개 주소에 접속 안 됨 등)는 HTTP 오류가 아니라 시도의 `status`·`error`로 와요.

### `GET /api/items/<id>/publish` · `GET /api/publish/attempts/<pa>`

`{"item_id": "it_0a803d87a98b", "attempts": [{…시도…}]}`(최근순, 50개까지) · `{"attempt": {…}}`. 없는 id는 404.

### `PUT /api/publish/attempts/<pa>/permalink`

게시는 됐는데 주소를 받지 못한 기록(LinkedIn이 201만 주고 게시물 id를 주지 않은 경우, 결과 불명을 주소 없이 "올라갔어요"로 정리한 경우)에 사람이 주소를 넣어요. 콘텐츠가 아직 이 게시를 가리키면(그 뒤에 새 버전을 API로 올리지 않았으면) 콘텐츠의 `published_url`도 채워요.

요청: `{"permalink": "https://www.linkedin.com/feed/update/urn:li:share:7243/"}` → `200 {"attempt": {…, "permalink": "https://www.linkedin.com/feed/update/urn:li:share:7243/"}}`

보낸 주소가 그 시도의 `candidates` 중 하나면 그 게시물의 id도 시도와 콘텐츠의 `external_id`·`published_external_id`(비어 있을 때)로 저장해요. API 게시가 꺼져 있어도(`INSIA_PUBLISH=0`) 돼요(기록만 바꿔요). `https` + 그 플랫폼의 도메인(LinkedIn `www.linkedin.com`·`linkedin.com`, 인스타그램 `www.instagram.com`·`instagram.com`)만 받아요(아니면 `400 {"error": "LinkedIn 게시물 주소(https://www.linkedin.com/…)를 넣어 주세요.", "code": "invalid_input"}`). 주소가 이미 있거나 게시되지 않은 기록은 `409 {"error": "게시가 끝났고 주소가 비어 있는 기록에만 주소를 넣을 수 있어요.", "code": "attempt_state"}`.

### `POST /api/publish/attempts/<pa>/resolve`

결과를 모르는(`unknown`) 시도를 사람이 플랫폼에서 직접 확인한 뒤 정리해요. **사람 요청만** 받아요. LinkedIn은 게시물을 다시 읽을 수 없어서 이 정리가 끝날 때까지 콘텐츠가 잠겨 있어요. 플랫폼을 부르지 않으니 API 게시를 끈 뒤(`INSIA_PUBLISH=0`)에도 정리할 수 있어요(그 사이 콘텐츠의 `publish` 블록은 `state: "disabled"`로 잠금과 결과 불명 카드만 보여 줘요).

요청: `{"outcome": "published", "url": "https://www.linkedin.com/feed/update/urn:li:share:…/"}`(주소는 선택) 또는 `{"outcome": "not_published"}`.

응답 `200` (주소 없이 "올라갔어요"):

```json
{"attempt": {"id": "pa_9338b06e91be3bc78267888e", "status": "published", "step": "write", "permalink": "",
             "error_code": "timeout", "resolved_by": "dashboard@127.0.0.1 · 올라갔어요",
             "finished_at": "2026-09-28T23:51:55.639Z", "progress": {"done": 1, "total": 1}, "...": "..."},
 "item": {"id": "it_ef68ccc1e09c", "status": "published", "published_at": "2026-09-28T23:51:55.639Z",
          "published_url": "", "published_via": "fake", "published_external_id": "", "...": "..."}}
```

`not_published`는 시도를 `abandoned`로 닫고 콘텐츠는 그대로 둬요(`"item": null`). 다시 게시하려면 새 미리보기부터 해요. 대시보드는 "LinkedIn 피드에서 먼저 확인했나요? 같은 글이 두 번 올라갈 수 있어요."를 한 번 더 물어요.

오류: `unknown`이 아님 `409 {"error": "결과 확인이 필요한 기록만 정리할 수 있어요.", "code": "attempt_state"}` · 사람 요청 아님 403 · `outcome`이 다름·`not_published`에 주소를 보냄·주소가 그 플랫폼 도메인이 아님 400.

### `POST /api/publish/attempts/<pa>/check`

인스타그램의 `unknown` 시도를 인스타그램에서 다시 확인해요(읽기만, 게시를 다시 부르지 않아요). 게시됐으면 `published`로, 게시되지 않았으면 `failed`로 닫고, 확인하지 못하면 `unknown`으로 남아요. 응답 `200 {"attempt": {…}}`. LinkedIn은 다시 읽을 수 없어서 `409 {"error": "LinkedIn 게시물은 INSIA가 다시 읽을 수 없어요. LinkedIn 내 활동에서 확인한 뒤 ‘올라갔어요’ 또는 ‘안 올라갔어요’를 눌러 주세요.", "code": "attempt_state"}`예요.

### 오류 `code` 한눈에 보기

API 게시 경로의 오류에는 늘 `code`가 붙어요. 대시보드는 문장(`error`)을 그대로 보여 주고, `code`로 버튼(다시 연결, 다시 확인 등)을 골라요.

| `code` | 상태 | 뜻 |
|---|---|---|
| `disabled` | 409 | API 게시가 꺼져 있음(`INSIA_PUBLISH=0`, 가짜 게시 모드를 임시 폴더가 아닌 곳에서 켬, 인스타그램 시험 기능 꺼짐) |
| `not_configured` · `not_connected` · `reconnect` · `unavailable` | 409 | 앱 정보 없음 · 계정 연결 안 됨 · 다시 연결 필요(`"reconnect": true`) · 요구 조건 없음(`blockers`) |
| `not_publishable` | 409 | 승인한 최신 버전이 아님, API 게시가 없는 채널(`blocked_by`) |
| `preview_expired` · `changed` | 409 | 미리보기 만료·사용됨 · 확인한 뒤 내용·계정이 바뀜 |
| `item_locked` | 409 | 게시 중·결과 확인이 필요한 콘텐츠(`attempt_id`, `platform`, `attempt_status`) |
| `already_published` | 409 | 이 버전은 이미 API로 게시함(`permalink`, `attempt_id`) |
| `busy` | 409 | 같은 플랫폼의 다른 게시·카드 렌더링이 진행 중 |
| `agent_job` · `editing` | 409 | 에이전트가 작업 중(`run_id`) · 사람이 고친 내용을 저장하는 중 |
| `attempt_state` | 409 | 그 시도 상태에서는 할 수 없음 |
| `env_locked` | 409 | 환경 변수로 정한 값 |
| `not_human` | 403 | 사람 요청이 아님 |
| `invalid_input` · `invalid_options` | 400 | 입력이 잘못됨 · 미리보기 옵션이 잘못됨(인스타그램 AI 정보 라벨을 고르지 않음 포함) |
| `oauth_state` · `oauth_cancelled` · `invalid_token` · `account_type` · `connect_failed` | 400 | 연결 실패(state 만료·재사용, 취소, 토큰 틀림, 개인 계정, 그 밖) |
| `validation` · `render` | 422 | 고칠 부분이 있는 미리보기 · 카드 이미지를 그리지 못함 |
| `too_many` | 429 | 미리보기·게시·연결 시도가 너무 많음(`Retry-After`) |
| `exchange_failed` | 502 | LinkedIn 코드 교환 실패 |

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

프로필, 주제, 지난 게시물(겹치는 주제 피하기)을 보고 기간 안에 게시물을 배치해 저장합니다. 기본은 평일만이고, `weekend_channels`로 채널별로 주말(토·일)도 허용할 수 있어요. **동기 요청**이라 계획이 끝나야 응답합니다(live 모드는 수십 초 걸릴 수 있어요).

요청:

```json
{"theme": "AI로 콘텐츠 운영 시간 줄이기", "start": "2026-10-05", "end": "2026-10-11",
 "counts": {"naver_blog": 2, "linkedin": 1, "instagram": 1}, "weekend_channels": ["instagram"],
 "replace": false, "options": {"mode": "mock"}}
```

- `end` 대신 `days`(1~31, 기본 7)를 줄 수 있습니다. 기간은 최대 31일.
- `counts` 키는 `naver_blog`/`blog`, `linkedin`, `instagram`/`ig`, `bizplan`. 채널마다 하루 한 편까지라 넘치면 줄이고 `notices`로 알려 줍니다(주말을 빼서 줄었는지, 이미 계획된 날이 있어서인지 이유도 함께).
- `weekend_channels`(선택): 주말에도 올릴 채널. 채널 id 목록(`["naver_blog", "instagram"]`, 별칭 `blog`·`ig`도 됨), `"all"`(모든 채널), `"none"`, `true`/`false`, 없으면(`null`) 평일만. 모르는 채널이면 400. 주말 슬롯을 만들면 예정일에 초안이 만들어지도록 `insia run-due` 예약도 매일 돌게 바꿔 주세요(운영 안내 참고).
- `replace`(선택, 기본 `false`): `true`면 기간 안의 `planned` 슬롯(초안 전)을 먼저 `skipped`로 바꾸고 새로 짭니다(`insia plan-week --replace`와 같음). 입력이 틀리거나, AI 호출이 실패하거나, 계획하는 도중 서버가 꺼지면(Ctrl+C, SIGTERM) 바꾼 슬롯은 다시 `planned`로 돌아가요. 초안을 만드는 중이거나 초안이 있는 슬롯은 건드리지 않습니다. 바꾸기와 되돌리기는 한 번에(한 트랜잭션으로) 하니, 같은 순간 `insia run-due`가 잡은 슬롯을 덮어쓰지 않고, 도중에 실패하면 아무것도 바뀌지 않아요. `insia run-due`도 초안을 만들기 직전에 슬롯 상태를 다시 확인해서, 그사이 건너뜀이 된 슬롯은 만들지 않아요.
- 같은 기간을 다시 계획해도 겹치지 않습니다: 채널마다 이미 슬롯(`planned`·`generating`·`drafted`)이 있는 날은 비워 두고 나머지 날에만 배치하며, 빈 날이 없으면 AI를 부르지 않고(비용 없음) `notices`로 알려 줍니다. 건너뛴(`skipped`) 슬롯의 날은 다시 쓸 수 있어요.
- 지난 게시물이나 기존 계획과 주제가 겹치는 슬롯, 새 계획 안에서 서로 겹치는 슬롯은 저장하지 않고 이유별로 `notices`에 적습니다. 그래서 요청한 개수보다 적게 계획될 수 있어요(그때도 `notices`에 나와요).

응답 `201`:

```json
{"summary": "2026-10-05~2026-10-11에 네이버 블로그 2편, 링크드인 1편, 인스타그램 1편을 배치했어요. …",
 "slots": [{"id": "sl_…", "date": "2026-10-10", "channel": "instagram", "topic": "…", "status": "planned", "...": "..."}],
 "notices": ["이 기간에 이미 계획된 일정 2개(링크드인 2)가 있어, 같은 채널은 그날을 비워 두고 계획했어요."],
 "mode": "mock", "start": "2026-10-05", "end": "2026-10-11", "replaced": []}
```

`replaced`: `replace: true`로 `skipped`가 된 기존 슬롯 id(그때는 `notices` 맨 앞에도 "…건너뜀으로 바꾸고 새로 짰어요"가 붙어요).

오류: 날짜·개수·`weekend_channels` 형식(400), 동시 작업 초과(429), AI 호출 실패(502), 서버를 끄는 중(503 — 서버가 꺼질 때 도는 계획은 최대 20초 기다렸다가, 못 끝내면 저장하지 않고 멈춰요. 기존 계획은 그대로예요).

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

`item_id`는 미리 정해지는 id라 작업이 끝나면 보관함에서 바로 열 수 있습니다. 실행하던 프로그램이 멈춰 `generating`으로 남은 슬롯은 이 요청이 먼저 정리하니(그 실행은 `interrupted`) `force` 없이 다시 만들 수 있어요.

| 오류 | 코드 |
|---|---|
| 없는 슬롯 | 404 |
| 다른 곳에서 살아 있는 실행이 만드는 중 | 409 (`run_id`) — `force: true`여도 두 번 만들지 않아요 |
| 방금 끝난 실행이 남긴 `generating`(1분 안) | 409 (`run_id`) — `force: true`로 다시 만들 수 있어요 |
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
           "started_at": "2026-09-28T10:10:39.199Z", "status": "completed", "mode": "live"}],
 "by_task": {"plan": {"usd": 0.05, "calls": 2, "input_tokens": 165, "output_tokens": 979},
             "plan_calendar": {"usd": 0.01, "calls": 1, "input_tokens": 233, "output_tokens": 138}},
 "by_day": [{"date": "2026-09-28", "usd": 1.2345, "calls": 13}],
 "budget_usd": 3.0, "currency": "USD"}
```

- 금액은 USD, 모델별 토큰 단가(`src/insia_agents/costs.py`, `prices.json`, `INSIA_PRICE_*`)로 계산한 **추정치**입니다. mock 모드는 0원입니다.
- `runs`는 최근순, `by_day`는 오래된 날짜부터(한국 날짜). 캘린더 계획처럼 실행 id가 없는 호출은 `run_id: ""`, `kind: "other"`로 묶입니다.
- `started_at`: 실행이 시작된 시각(실행 기록이 없으면 `first_at`), `status`: 그 실행의 현재 상태, `mode`: `live`·`mock`(실행 기록이 없으면 `null`). 대시보드는 모두 `mock`이면 "모의 실행은 비용이 들지 않아요"라고 알려 줍니다.
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
          trust_proxy=True,                    # --trust-proxy
          media_port=None,                     # --media-port (인스타그램 API 게시용 이미지 전용 포트)
          media_base_url=None)                 # --media-base-url (그 포트를 바깥에서 여는 https 주소)
except ServerConfigError as exc:               # ValueError: 토큰 없음·짧음, 잘못된 도메인, IPv6 불가, 워크스페이스 오류
    print(f"오류: {exc}")
except OSError as exc:                         # 포트 사용 중 등
    print(f"서버를 시작하지 못했어요: {exc}")
```

- `make_server(settings, host="127.0.0.1", port=8765, web_dir=None, heartbeat=15.0, quiet=True, *, token=None, public_hosts=(), trust_proxy=False, workspace=None, max_live=None, max_mock=None, media_port=None, media_base_url=None, publish_service=None)`는 서버 객체만 만듭니다(`serve_forever()`로 시작, `server_close()`로 정리 — 도는 작업을 중단하고 워크스페이스를 닫습니다). 루프백이 아닌 `host`, `public_hosts`, `trust_proxy` 중 하나라도 있는데 토큰이 없으면 `ServerConfigError`입니다. `host`는 IPv6 주소(`::1`, `::`)도 됩니다.
- API 게시: `make_server`는 같은 워크스페이스로 `PublishService`를 만들고(환경 변수 `INSIA_PUBLISH*`·`INSIA_LINKEDIN_*`·`INSIA_MEDIA_*`), 멈춘 게시 시도를 정리하고, 6시간마다 도는 정리·인스타그램 토큰 갱신 작업을 켜요(게시는 하지 않아요). `media_port`·`media_base_url`은 `serve --media-port`·`--media-base-url`(기본 `INSIA_MEDIA_PORT`·`INSIA_MEDIA_BASE_URL`)이고, 미디어 포트가 있으면 같은 `host`에 `/pub/m/`만 제공하는 두 번째 소켓을 열어요. 그 포트를 열 수 없어도 서버는 시작하고 인스타그램만 `unavailable`이 돼요. 환경 변수 값이 잘못돼도 서버는 시작하고 경고만 남겨요. `publish_service`는 테스트용(서비스 바꿔 끼우기)이에요. `server_close()`가 미디어 리스너와 게시 작업도 정리해요.
- `serve()`는 시작 안내(주소, 모드, 워크스페이스, 토큰 여부, 정리한 중단 실행 수, API 게시를 설정했다면 "API 게시: LinkedIn 연결됨 · 인스타그램 공개 주소 없음(수동 게시)" 같은 한 줄)를 출력하고 Ctrl+C나 SIGTERM까지 돕니다. SIGTERM(`docker stop`, systemd)도 Ctrl+C와 똑같이 처리해서(`sigterm_as_interrupt()`, 메인 스레드에서만) 도는 실행을 멈추고 저장한 뒤 종료 코드 0으로 끝납니다(`stop_serving()`: 최대 `SHUTDOWN_GRACE` = 20초 기다림). 직접 `serve_forever()`를 돌릴 때도 `with sigterm_as_interrupt():`로 감싸고 끝에 `stop_serving(server)`를 부르면 같아요.
- 서버가 켜질 때 `running`으로 남은 실행 중 실행하던 프로세스가 없어진 것만 `interrupted`로 정리합니다([실행](#실행) 참고). 같은 워크스페이스에서 CLI 실행(`insia run`, `insia run-due`)이 도는 중에 서버를 켜도 그 실행은 그대로 이어집니다.
