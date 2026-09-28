# 이벤트 스키마

백엔드(파이썬 파이프라인)가 대시보드로 보내는 이벤트 계약입니다. live 모드, mock 모드, 녹화된 데모 트레이스가 모두 같은 형식을 씁니다. 대시보드는 이 이벤트만으로 화면 전체를 다시 그릴 수 있습니다.

## 공통 봉투

```json
{"seq": 12, "t": 14.2, "ts": "2026-09-28T06:58:40.002Z", "run_id": "20260928-065825-c03c",
 "type": "research.finding", "agent": "researcher", "data": {"finding": {"id": "f1", "...": "..."}}}
```

| 필드 | 타입 | 뜻 |
|---|---|---|
| `seq` | int | 실행 안에서 1부터 1씩 늘어나는 순번. 빠지거나 겹치지 않음 |
| `t` | float | 실행 시작 후 경과 초. live는 실제 시간, mock은 시뮬레이션 시간(`--speed 0`이어도 현실적인 값). `seq` 순서대로 줄어들지 않음 |
| `ts` | string | ISO-8601 UTC. 시작 시각 + `t` |
| `run_id` | string | `YYYYMMDD-HHMMSS-xxxx` |
| `type` | string | 아래 표의 16가지 |
| `agent` | string | `orchestrator` · `researcher` · `reviewer` · `system` |
| `data` | object | 타입별 내용 |

- 한 실행은 항상 `run.started`로 시작해 `run.completed` 또는 `run.failed`로 끝납니다. 이 둘 뒤에는 이벤트가 없습니다.
- 채널 작업은 live 모드에서 동시에 돌기 때문에 채널별 이벤트가 섞여 옵니다. 채널 이벤트는 `data.channel`로 구분합니다.
- `data` 안의 `Source`, `Finding`, `RubricScore`, `ReviewIssue`, `FormatCheck`, `ResearchQuestion`, `ChannelOutline`은 `src/insia_agents/models.py`의 필드를 그대로 씁니다.

## 전송 방식

**SSE** — `GET /api/runs/<run_id>/events`

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

**데모 트레이스** — `web/demo/demo-run.json`, `insia run --record <파일>`로 만듭니다.

```json
{"version": 1,
 "meta": {"title": "INSIA 실행 기록 — …", "recorded_at": "2026-09-28T07:01:03Z", "mode": "mock", "model": "claude-opus-5", "brief": {"topic": "…"}},
 "events": [ {"seq": 1, "...": "..."} ]}
```

**JSONL** — 실행마다 `outputs/<run_id>/events.jsonl`에 한 줄에 이벤트 하나씩 저장됩니다.

## 타입별 내용

| type | agent | data |
|---|---|---|
| `run.started` | system | `brief`, `channels`, `mode`, `model`, `max_rounds`, `pass_score` |
| `agent.status` | 각 에이전트 | `status`, `message` |
| `handoff` | 보내는 쪽 | `from`, `to`, `kind`, `label`, `channel`(선택) |
| `plan.created` | orchestrator | `summary`, `key_messages`, `questions`, `outlines` |
| `research.query` | researcher | `question_id`, `query` |
| `research.source` | researcher | `source` |
| `research.finding` | researcher | `finding` |
| `research.completed` | researcher | `findings`, `sources`, `gaps`, `followup` |
| `draft.created` | orchestrator | `channel`, `round`, `title`, `chars`, `chars_no_space`, `excerpt`, `hashtags`, `change_log` |
| `review.started` | reviewer | `channel`, `round` |
| `review.completed` | reviewer | `channel`, `round`, `score`, `passed`, `rubric`, `issues`, `format_checks`, `fact_checks`, `needs_research`, `summary` |
| `revision.requested` | reviewer | `channel`, `round`, `issues`, `top_issue` |
| `channel.completed` | orchestrator | `channel`, `passed`, `score`, `rounds`, `title`, `content`, `hashtags` |
| `run.completed` | system | `duration_s`, `scores`, `passed`, `output_dir`, `errors`(선택) |
| `run.failed` | system | `error` |
| `log` | 누구나 | `level`, `message` |

아래 예시는 `data`만 보여 줍니다(봉투 필드는 생략).

### `run.started`

실행 설정 전체. `model`은 mock 템플릿이면 `mock-template`, 샘플 재생이면 녹화한 모델입니다.

```json
{"brief": {"topic": "테스트 주제", "goal": "", "audience": "", "channels": ["linkedin", "bizplan"], "tone": "",
           "keywords": ["AI 에이전트"], "notes": "", "language": "ko"},
 "channels": ["linkedin", "bizplan"], "mode": "mock", "model": "mock-template", "max_rounds": 2, "pass_score": 80}
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
            "published": "2025", "tier": 1, "accessed": "2026-09-28"}}
```

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

### `draft.created`

첫 초안(`round: 0`)과 수정본(`round ≥ 1`) 모두 이 이벤트입니다. `chars`는 공백 포함, `chars_no_space`는 공백 제외 글자수(`channels.py`의 정의)이고, `excerpt`는 마크다운을 걷어 낸 160자 이내 미리보기입니다. 본문 전체는 `channel.completed`에 옵니다.

```json
{"channel": "linkedin", "round": 1, "title": "반복 업무를 덜어 낸 방법", "chars": 1465, "chars_no_space": 1089,
 "excerpt": "혼자 사업하면 홍보는 늘 '이번 주만 넘기고'가 됩니다. 그런데 고객은 이번 주에도 검색하고 있습니다. …",
 "hashtags": ["#1인창업", "#콘텐츠마케팅", "#AI에이전트", "#스타트업"],
 "change_log": ["[major] 훅: 첫 두 줄을 독자의 상황으로 바꾸고 57자로 줄임"]}
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

### `channel.completed`

라운드 중 가장 좋은 초안(통과 여부 → 점수 → 나중 라운드 순)이 최종본입니다. 최대 수정 횟수까지 통과하지 못하면 `passed: false`로 끝납니다. `rounds`는 수정 횟수(첫 초안만으로 끝나면 0)입니다.

```json
{"channel": "linkedin", "passed": true, "score": 86, "rounds": 1, "title": "반복 업무를 덜어 낸 방법",
 "content": "혼자 사업하면 홍보는 늘 '이번 주만 넘기고'가 됩니다.\n그런데 고객은 이번 주에도 검색하고 있습니다.\n\n…\n\n#1인창업 #콘텐츠마케팅 #AI에이전트 #스타트업",
 "hashtags": ["#1인창업", "#콘텐츠마케팅", "#AI에이전트", "#스타트업"]}
```

### `run.completed`

`output_dir`는 저장하지 않았으면 `null`입니다. 일부 채널만 실패했다면 `errors`에 채널별 오류 메시지가 붙고, 나머지 채널 결과는 그대로 옵니다.

```json
{"duration_s": 157.3, "scores": {"linkedin": 86, "bizplan": 85}, "passed": {"linkedin": true, "bizplan": true},
 "output_dir": "outputs/20260928-065825-c03c"}
```

### `run.failed`

계획·리서치 단계에서 멈췄거나 모든 채널이 실패했을 때. `error`는 화면에 그대로 띄울 한국어 문장입니다.

```json
{"error": "API 키가 올바르지 않아요 (401). ANTHROPIC_API_KEY를 확인해 주세요."}
```

### `log`

실행 모드 안내, 거절 시 대체 모델 사용, 채널 실패 같은 알림입니다. `level`: `info` · `warn` · `error`.

```json
{"level": "warn", "message": "안전 분류기가 요청을 거절해 claude-opus-4-8 모델이 대신 응답했어요"}
```
