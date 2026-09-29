# 이벤트 스키마

백엔드(파이썬 파이프라인)가 대시보드로 보내는 이벤트 계약입니다. live 모드, mock 모드, 녹화된 데모 트레이스가 모두 같은 형식을 씁니다. 대시보드는 이 이벤트만으로 화면 전체를 다시 그릴 수 있습니다.

## 공통 봉투

```json
{"seq": 12, "t": 14.2, "ts": "2026-09-28T06:58:40.002Z", "run_id": "20260928-065825-c03c",
 "type": "research.finding", "agent": "researcher", "data": {"finding": {"id": "f1", "...": "..."}}}
```

| 필드 | 타입 | 뜻 |
|---|---|---|
| `seq` | int | 실행 안에서 1부터 1씩 늘어나는 순번. 저장된 스트림에서는 빠지거나 겹치지 않음(SSE는 이어서 실행한 실행의 앞 시도 종료 이벤트만 건너뜀 — [이어서 실행한 실행](#이어서-실행한-실행)) |
| `t` | float | 실행 시작 후 경과 초. live는 실제 시간, mock은 시뮬레이션 시간(`--speed 0`이어도 현실적인 값). `seq` 순서대로 줄어들지 않음 |
| `ts` | string | ISO-8601 UTC. 시작 시각 + `t` |
| `run_id` | string | `YYYYMMDD-HHMMSS-xxxx` |
| `type` | string | 아래 표의 16가지 |
| `agent` | string | `orchestrator` · `researcher` · `reviewer` · `system` |
| `data` | object | 타입별 내용 |

- 한 번의 시도는 항상 `run.started`로 시작해 `run.completed` 또는 `run.failed`로 끝납니다. 이 둘 뒤에는 그 시도의 이벤트가 없습니다. 중단된 실행을 이어서 실행하면 같은 `run_id`로 새 `run.started`(`resumed: true`)부터 다시 이어집니다(아래 [이어서 실행한 실행](#이어서-실행한-실행)).
- 채널 작업은 live 모드에서 동시에 돌기 때문에 채널별 이벤트가 섞여 옵니다. 채널 이벤트는 `data.channel`로 구분합니다.
- `data` 안의 `Source`, `Finding`, `RubricScore`, `ReviewIssue`, `FormatCheck`, `ResearchQuestion`, `ChannelOutline`은 `src/insia_agents/models.py`의 필드를 그대로 씁니다.

## 전송 방식

**SSE** — `GET /api/runs/<run_id>/events` (자세한 규칙은 [api.md](api.md#get-apirunsidevents-sse))

```text
retry: 3000

id: 1
data: {"seq": 1, "t": 0.0, "type": "run.started", ...}

: ping
```

- 연결하면 지난 이벤트를 모두 다시 보낸 뒤 새 이벤트를 이어서 보냅니다(`event:` 이름 없음 → `EventSource.onmessage`).
- 15초 동안 이벤트가 없으면 `: ping` 주석 줄을 보냅니다.
- 종료 이벤트를 보낸 뒤 서버가 연결을 닫습니다. 클라이언트는 그 이벤트를 받으면 `EventSource.close()`를 호출하세요.
- 다시 연결할 때 브라우저가 보내는 `Last-Event-ID`(또는 `?after=<seq>`) 이후 이벤트만 보냅니다. 이미 끝난 실행에서 더 보낼 것이 없으면 `204 No Content`로 답해 재연결을 멈춥니다.
- 모든 이벤트는 워크스페이스 DB(`events` 표)에도 저장됩니다. 끝난 실행은 DB에서 다시 보내므로 서버를 다시 켠 뒤에도 같은 스트림을 받을 수 있고, CLI처럼 다른 프로세스가 같은 워크스페이스에서 돌리는 실행도 따라갈 수 있습니다.
- 토큰 모드에서는 `insia_token` 쿠키로 인증합니다(`EventSource`는 같은 출처 쿠키를 자동으로 보냅니다).

**데모 트레이스** — `web/demo/demo-run.json`, `insia run --record <파일>`로 만듭니다.

```json
{"version": 1,
 "meta": {"title": "INSIA 실행 기록 — …", "recorded_at": "2026-09-28T07:01:03Z", "mode": "mock", "model": "claude-opus-5", "brief": {"topic": "…"}},
 "events": [ {"seq": 1, "...": "..."} ]}
```

**JSONL** — 실행마다 `outputs/<run_id>/events.jsonl`에 한 줄에 이벤트 하나씩 저장됩니다(이어서 실행하면 같은 파일에 덧붙임).

**워크스페이스** — `insia.db`의 `events` 표에 `(run_id, seq)`마다 이벤트 원문이 저장됩니다. `insia serve`와 CLI가 같은 표를 씁니다.

## 실행 종류와 작업

파이프라인 실행 말고도 콘텐츠 작업이 각자 실행 id와 이벤트 스트림을 가집니다. 이벤트 타입은 같아서 대시보드가 같은 캐릭터 애니메이션을 보여 줄 수 있습니다. 콘텐츠 작업(`review` · `revise` · `edit`)의 `run.started`·`run.completed`·`run.failed`에는 `kind`와 `item_id`가 붙습니다.

| kind | 주로 나오는 이벤트 |
|---|---|
| `pipeline` | 전체 (계획 → 리서치 → 채널별 초안·검수·수정) |
| `slot` | 파이프라인과 같음 (캘린더 슬롯 하나, 채널 하나). `kind`는 붙지 않고 실행 정보(`GET /api/runs/<id>`)의 `kind`로 구분 |
| `review` | `run.started` → `agent.status` → `review.started` → `review.completed` → `handoff` → `run.completed` |
| `revise` | (검수 결과가 없으면 먼저 `review.*`) → `revision.requested`(`instructions`) → (필요하면 `research.*` 추가 조사) → `draft.created` → `review.*` → `run.completed` |
| `edit` | `run.started`(`mode: "human"`) → `draft.created`(`agent: "system"`, `source: "human"`) → `log` → `run.completed`(`format_checks`) |

## 이어서 실행한 실행

서버가 꺼지거나(`interrupted`), 실패하거나, 사용자가 멈추거나, 예산 상한에 걸린 `pipeline`/`slot` 실행은 `POST /api/runs/<id>/resume`(또는 `insia resume <id>`)으로 같은 `run_id`를 이어 갑니다. 그러면 한 실행의 저장된 스트림에 시도가 여러 번 들어갑니다.

```text
seq 1  run.started
seq 2… (첫 시도의 이벤트)
seq 7  run.failed    {"error": "프로그램이 다시 시작되면서 실행이 중단됐어요. …", "interrupted": true}   ← SSE에서는 빠짐
seq 8  run.started   {"resumed": true, …}
seq 9… (저장된 계획·리서치를 다시 보여 주고, 끝난 채널은 건너뛰고, 나머지를 이어서)
seq 40 run.completed
```

- `seq`는 끊기지 않고 이어지고, `t`도 줄어들지 않습니다.
- 서버가 켜질 때 `running`으로 남은 실행에는 `run.failed`(`data.interrupted: true`)가 붙어 스트림이 깔끔하게 끝납니다.
- SSE는 뒤에 이벤트가 더 있는 종료 이벤트(앞 시도의 끝)를 보내지 않고 그 `seq`를 건너뜁니다. 그래서 "종료 이벤트를 받으면 닫는다"는 클라이언트도 이어서 실행한 부분까지 끝까지 받습니다. 앞 시도가 멈춘 이유는 저장된 스트림(`events` 표, `events.jsonl`)과 `GET /api/runs/<id>`에 남아 있습니다.
- 실행 중일 때 앞 시도의 끝은 새 시도의 `run.started`(`resumed: true`)와 "중단된 실행을 이어서 진행해요" 로그로 알 수 있습니다.
- 이어서 실행한 시도의 `research.completed`에는 `resumed: true`가 붙습니다(저장된 리서치를 다시 보여 준 것이라 새 검색이 아님).

사용량(토큰·비용)은 이벤트로 보내지 않습니다. 실행이 끝날 때 `run.completed.cost_usd`(비용이 있었을 때)와 `GET /api/usage`, `GET /api/runs/<id>`의 `cost_usd`로 확인합니다.

## 타입별 내용

| type | agent | data |
|---|---|---|
| `run.started` | system | `brief`, `channels`, `mode`, `model`, `max_rounds`, `pass_score`, `resumed`(선택), `budget_usd`(선택), `kind`·`item_id`(작업) |
| `agent.status` | 각 에이전트 | `status`, `message` |
| `handoff` | 보내는 쪽 | `from`, `to`, `kind`, `label`, `channel`(선택) |
| `plan.created` | orchestrator | `summary`, `key_messages`, `questions`, `outlines` |
| `research.query` | researcher | `question_id`, `query` |
| `research.source` | researcher | `source` (`origin`: `web` · `user`) |
| `research.finding` | researcher | `finding` |
| `research.completed` | researcher | `findings`, `sources`, `gaps`, `followup`, `resumed`(선택) |
| `draft.created` | orchestrator (사람 수정은 system) | `channel`, `round`, `title`, `chars`, `chars_no_space`, `excerpt`, `hashtags`, `change_log`, `source`·`version`(사람 수정) |
| `review.started` | reviewer | `channel`, `round` |
| `review.completed` | reviewer | `channel`, `round`, `score`, `passed`, `rubric`, `issues`, `format_checks`, `fact_checks`, `needs_research`, `summary` |
| `revision.requested` | reviewer | `channel`, `round`, `issues`, `top_issue`, `instructions`(사람 지시가 있을 때) |
| `channel.completed` | orchestrator | `channel`, `passed`, `score`, `rounds`, `final_round`, `title`, `content`, `hashtags` |
| `channel.store_skipped` | orchestrator | `channel`, `item_id`, `attempt_id`, `platform`, `status`, `message` |
| `run.completed` | system | `duration_s`, `scores`, `passed`, `output_dir`, `errors`(선택), `items`(워크스페이스), `cost_usd`(선택), 작업이면 `kind`·`item_id`·`version`·`status`·`format_checks`(edit) |
| `run.failed` | system | `error`, 예산 초과면 `budget_exceeded`·`budget_usd`·`cost_usd`·`completed_channels`·`stopped_channels`, 서버 재시작이면 `interrupted`, 작업이면 `kind`·`item_id` |
| `log` | 누구나 | `level`, `message` |

아래 예시는 `data`만 보여 줍니다(봉투 필드는 생략).

### `run.started`

실행 설정 전체. `model`은 mock 템플릿이면 `mock-template`, 샘플 재생이면 녹화한 모델입니다.

```json
{"brief": {"topic": "테스트 주제", "goal": "", "audience": "", "channels": ["linkedin", "bizplan"], "tone": "",
           "keywords": ["AI 에이전트"], "notes": "", "language": "ko"},
 "channels": ["linkedin", "bizplan"], "mode": "mock", "model": "mock-template", "max_rounds": 2, "pass_score": 80}
```

- `resumed: true` — 중단된 실행을 이어서 시작했을 때만.
- `budget_usd` — 예산 상한이 있을 때만(USD).
- `kind`, `item_id` — 콘텐츠 작업(`review` · `revise` · `edit`)일 때. 사람 수정(`edit`)은 `mode: "human"`, `model: ""`.

```json
{"brief": {"topic": "…", "channels": ["naver_blog"], "...": "..."}, "channels": ["naver_blog"], "mode": "live",
 "model": "claude-opus-5", "max_rounds": 2, "pass_score": 80, "kind": "revise", "item_id": "it_20260928-065825-c03c_naver_blog"}
```

### `agent.status`

`status`는 `idle` 대기 · `planning` 기획 중 · `searching` 검색 중 · `reading` 자료 읽는 중 · `writing` 작성 중 · `reviewing` 검수 중 · `revising` 수정 중 · `waiting` 대기 · `done` 완료 · `error` 오류 중 하나입니다. `message`는 말풍선에 그대로 띄울 한국어 한 문장입니다.

```json
{"status": "writing", "message": "링크드인 초안을 쓰는 중이에요"}
```

### `handoff`

에이전트 사이에 무언가를 넘길 때. 대시보드는 `from` → `to` 경로로 패킷 애니메이션을 띄웁니다. `kind`: `task` 작업 요청, `result` 결과 전달, `feedback` 수정 요청.

```json
{"from": "reviewer", "to": "orchestrator", "kind": "feedback", "label": "링크드인 수정 요청 3건", "channel": "linkedin"}
```

### `plan.created`

```json
{"summary": "공식 통계로 문제를 먼저 확인하고 같은 핵심 메시지를 채널 형식에 맞게 풀어 씁니다.",
 "key_messages": ["모든 수치는 출처와 기준 시점을 밝히고, 최종 게시는 사람이 승인한다"],
 "questions": [{"id": "q1", "question": "국내 소상공인 사업체 수와 최근 추이(최신 기준연도)", "why": "사업계획서 문제인식 근거",
                "channels": ["bizplan", "linkedin"], "priority": "high"}],
 "outlines": [{"channel": "linkedin", "sections": ["훅 2줄", "맥락", "관점 3가지", "근거", "질문", "해시태그"]}]}
```

### `research.query`

live 모드에서는 모델이 실제로 보낸 검색어가 검색 순간에 옵니다. `question_id`는 검색어와 가장 가까운 질문으로 추정한 값이라 빈 문자열일 수도 있습니다.

```json
{"question_id": "q1", "query": "소상공인 실태조사 2025 사업체 수"}
```

### `research.source`

리서치 팩에 들어간 출처만 옵니다(검색 결과 전체가 아님). `tier`: 1 공식·공공, 2 언론·리서치, 3 기타.

```json
{"source": {"id": "s1", "title": "소상공인 실태조사", "url": "https://kosis.kr", "publisher": "통계청",
            "published": "2025", "tier": 1, "accessed": "2026-09-28", "origin": "web"}}
```

사용자가 올린 자료는 `origin: "user"`, `url: "user://u3"`, `tier: 1`, `publisher: "사용자 제공 자료"`로 옵니다.

### `research.finding`

해당 finding이 인용하는 출처의 `research.source`가 항상 먼저 옵니다.

```json
{"finding": {"id": "f1", "question_id": "q1", "claim": "2024년 기준 국내 소상공인 사업체 수는 ○○○만 개다.",
             "source_ids": ["s1"], "confidence": "high", "note": "표 2-1"}}
```

### `research.completed`

`findings`·`sources`·`gaps`는 추가 조사를 합친 뒤의 **누적** 값입니다. 검수 요청으로 다시 조사했으면 `followup: true`.

```json
{"findings": 5, "sources": 5, "gaps": ["AI 마케팅 자동화 시장 규모: 공식 통계 없음"], "followup": true}
```

이어서 실행할 때 저장된 리서치를 다시 보여 준 경우 `followup: false, resumed: true`입니다.

### `draft.created`

첫 초안(`round: 0`)과 수정본(`round ≥ 1`) 모두 이 이벤트입니다. `chars`는 공백 포함, `chars_no_space`는 공백 제외 글자수(`channels.py`의 정의)이고, `excerpt`는 마크다운을 걷어 낸 160자 이내 미리보기입니다. 본문 전체는 `channel.completed`에 옵니다.

```json
{"channel": "linkedin", "round": 1, "title": "반복 업무를 덜어 낸 방법", "chars": 1465, "chars_no_space": 1089,
 "excerpt": "혼자 사업하면 홍보는 늘 '이번 주만 넘기고'가 됩니다. 그런데 고객은 이번 주에도 검색하고 있습니다. …",
 "hashtags": ["#1인창업", "#콘텐츠마케팅", "#AI에이전트", "#스타트업"],
 "change_log": ["[major] 훅: 첫 두 줄을 독자의 상황으로 바꾸고 57자로 줄임"]}
```

사람이 직접 고친 버전(`PUT /api/items/<id>/draft`)은 `agent: "system"`이고 `source: "human"`, `version`(보관함의 버전 번호)이 붙습니다.

```json
{"channel": "linkedin", "round": 1, "title": "사람이 고친 제목", "chars": 1320, "chars_no_space": 990, "excerpt": "…",
 "hashtags": ["#1인창업"], "change_log": ["사람이 직접 수정함"], "source": "human", "version": 4}
```

### `review.started`

```json
{"channel": "linkedin", "round": 0}
```

### `review.completed`

코드가 `finalize_review`로 확정한 결과입니다. `format` 루브릭 항목과 `format_checks`는 코드가 계산하고, `score`는 루브릭 합계를 100점으로 환산한 값, `passed`는 `score >= pass_score`이면서 critical 이슈가 없을 때 참입니다. `fact_checks`는 판정별 개수만 보냅니다.

```json
{"channel": "linkedin", "round": 0, "score": 68, "passed": false,
 "rubric": [{"id": "hook", "label": "훅(첫 2줄)", "score": 13, "max": 25, "comment": "첫 두 줄이 길어 '더 보기' 전에 잘림"},
            {"id": "format", "label": "형식(자동)", "score": 5, "max": 10, "comment": "미충족: 첫 2줄 길이, 해시태그 수"}],
 "issues": [{"severity": "major", "location": "형식 — 첫 2줄 길이", "problem": "첫 2줄 길이 245자, 기준(210자 이하) 미충족",
             "fix": "첫 2줄 길이: 현재 245자 → 210자 이하로 맞추기"}],
 "format_checks": [{"id": "hook_length", "label": "첫 2줄 길이", "passed": false, "value": "245자", "expected": "210자 이하"}],
 "fact_checks": {"supported": 4, "unsupported": 0, "needs_source": 0},
 "needs_research": [],
 "summary": "주요 이슈 3건이 있어 수정이 필요해요."}
```

### `revision.requested`

통과하지 못했고 다음 라운드가 있을 때만 옵니다(`round < max_rounds`). `round`는 방금 검수한 초안의 라운드입니다.

```json
{"channel": "linkedin", "round": 0, "issues": 3, "top_issue": "첫 2줄 길이 245자, 기준(210자 이하) 미충족"}
```

수정 요청 작업(`revise`)에서 사람이 지시를 주면 `instructions`가 붙고, `top_issue`가 그 지시(200자까지)이며 `issues`에 1건이 더해집니다.

```json
{"channel": "naver_blog", "round": 2, "issues": 4, "top_issue": "도입부를 두 문장으로 줄여 주세요", "instructions": "도입부를 두 문장으로 줄여 주세요"}
```

### `channel.completed`

라운드 중 가장 좋은 초안(통과 여부 → 점수 → 나중 라운드 순)이 최종본입니다. 최대 수정 횟수까지 통과하지 못하면 `passed: false`로 끝납니다. `rounds`는 수정 횟수(첫 초안만으로 끝나면 0)입니다.

`final_round`는 최종본으로 고른 초안의 라운드입니다(마지막 라운드보다 앞 라운드가 점수가 높으면 그 라운드).

```json
{"channel": "linkedin", "passed": true, "score": 86, "rounds": 1, "final_round": 1, "title": "반복 업무를 덜어 낸 방법",
 "content": "혼자 사업하면 홍보는 늘 '이번 주만 넘기고'가 됩니다.\n그런데 고객은 이번 주에도 검색하고 있습니다.\n\n…\n\n#1인창업 #콘텐츠마케팅 #AI에이전트 #스타트업",
 "hashtags": ["#1인창업", "#콘텐츠마케팅", "#AI에이전트", "#스타트업"]}
```


### `channel.store_skipped`

그 채널의 콘텐츠를 API로 게시하는 중이라(게시 시도가 `sending`이거나 결과 확인이 필요한 `unknown`) 이번 실행의 결과를 보관함에 넣지 않았다는 알림이에요. 채널마다 한 번만 오고, 다른 채널은 그대로 진행해요. 그 채널의 초안·검수는 실행 기록에는 남고, 이어서 실행하면 다시 저장을 시도해요.

```json
{"channel": "linkedin", "item_id": "it_20260928-120000-ab12_linkedin", "attempt_id": "pa_3f2a…", "platform": "linkedin",
 "status": "sending", "message": "이 콘텐츠를 API로 게시하는 중이라 이번 결과를 보관함에 넣지 않았어요. 실행 기록에는 남아요."}
```

### `run.completed`

`output_dir`는 저장하지 않았으면 `null`입니다. 일부 채널만 실패했다면 `errors`에 채널별 오류 메시지가 붙고, 나머지 채널 결과는 그대로 옵니다.

```json
{"duration_s": 157.3, "scores": {"linkedin": 86, "bizplan": 85}, "passed": {"linkedin": true, "bizplan": true},
 "output_dir": "outputs/20260928-065825-c03c",
 "items": {"linkedin": "it_20260928-065825-c03c_linkedin", "bizplan": "it_20260928-065825-c03c_bizplan"},
 "cost_usd": 0.8421}
```

- `items` — 워크스페이스에 저장한 채널별 콘텐츠 id(워크스페이스가 있을 때).
- `cost_usd` — 이 실행의 누적 추정 비용(USD, 0보다 클 때만).
- 작업이면 `kind`, `item_id`와 `version`(작업 뒤 버전 번호), `status`(작업 뒤 콘텐츠 상태)가 붙고, 사람 수정(`edit`)은 `format_checks`(형식 검사 결과 목록)도 붙습니다.

```json
{"duration_s": 3.2, "scores": {"linkedin": 88}, "passed": {"linkedin": true}, "output_dir": null,
 "kind": "review", "item_id": "it_20260928-065825-c03c_linkedin", "version": 3, "status": "draft"}
```

### `run.failed`

계획·리서치 단계에서 멈췄거나 모든 채널이 실패했을 때. `error`는 화면에 그대로 띄울 한국어 문장입니다.

```json
{"error": "API 키가 올바르지 않아요 (401). ANTHROPIC_API_KEY를 확인해 주세요."}
```

예산 상한에 걸리면 끝난 채널과 멈춘 채널을 알려 줍니다(끝난 채널은 보관함에 저장돼 있고, 상한을 올려 이어서 실행할 수 있어요).

```json
{"error": "예산 상한 $1.00를 넘어 실행을 멈췄어요 (지금까지 $1.12 사용). 끝난 채널(링크드인)은 저장해 뒀어요. 상한을 올린 뒤 이어서 실행하면 남은 작업만 마저 해요.",
 "budget_exceeded": true, "budget_usd": 1.0, "cost_usd": 1.1234, "completed_channels": ["linkedin"], "stopped_channels": ["bizplan"]}
```

서버가 다시 시작되면서 멈춘 실행에는 서버가 켜질 때 이 이벤트가 붙습니다.

```json
{"error": "프로그램이 다시 시작되면서 실행이 중단됐어요. '이어서 실행'으로 남은 작업을 마칠 수 있어요.", "interrupted": true}
```

사용자가 중단(`POST /api/runs/<id>/cancel`)하면 `{"error": "실행을 중단했어요"}`, 작업이면 `kind`·`item_id`가 붙습니다.

### `log`

실행 모드 안내, 거절 시 대체 모델 사용, 채널 실패 같은 알림입니다. `level`: `info` · `warn` · `error`.

```json
{"level": "warn", "message": "안전 분류기가 요청을 거절해 claude-opus-4-8 모델이 대신 응답했어요"}
```
