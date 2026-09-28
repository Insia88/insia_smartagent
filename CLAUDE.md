# INSIA 스마트에이전트: Claude Code 작업 안내

한국의 1인 창업자·소상공인을 위한 3-에이전트 콘텐츠 시스템이다. 브리프 하나로 **사업계획서**(예비창업패키지 PSST)와 **네이버 블로그·링크드인·인스타그램** 초안을 만들고, 리서치 → 작성 → 검수 → 수정 루프를 돌린다. 결과물은 초안이며, 사람이 최종 승인한 뒤 직접 게시한다.

같은 시스템을 두 방식으로 쓴다.
1. **Claude Code**: `.claude/agents/`의 서브에이전트와 `.claude/skills/`의 슬래시 명령. 웹 검색은 Claude Code의 WebSearch·WebFetch를 쓴다.
2. **Python 패키지 `insia_agents`**: Anthropic API로 도는 CLI와 로컬 대시보드(INSIA 에이전트 스튜디오). API 키가 없으면 녹화된 실행을 재생하는 mock 모드로 돈다.

## 에이전트 세 명

| id | 이름 | 마스코트(3D 카피바라) | 하는 일 |
|---|---|---|---|
| `orchestrator` | 총괄 에이전트 | 유자 디렉터 | 브리프 → 계획, 리서치·검수 위임, 채널별 작성, 피드백 반영, 최종 패키지 |
| `researcher` | 리서치 에이전트 | 돋보기 탐험가 | 한국어·영어 웹 검색, 출처 등급(Tier 1~3)이 붙은 리서치 팩 |
| `reviewer` | 검수 에이전트 | 꼼꼼 검수관 | 루브릭 채점, 리서치 팩 대조 사실 확인, 형식 검사, 수정 요청 (초안은 고치지 않음) |

## 실행 방법

```bash
claude --agent orchestrator          # 세션 전체를 총괄 에이전트로 실행
```

슬래시 명령(메인 스레드가 총괄 역할을 하고 researcher·reviewer를 서브에이전트로 부름):

| 명령 | 용도 |
|---|---|
| `/content-studio <주제 또는 brief.json>` | 네 채널을 같은 핵심 메시지로 한 번에 (원 소스 멀티 유즈) |
| `/bizplan <아이템>` | 사업계획서 (PSST, 개조식) |
| `/naver-blog <주제> [메인 키워드]` | 네이버 블로그 |
| `/linkedin <주제> [관점]` | 링크드인 |
| `/instagram <주제>` | 인스타그램 캐러셀 + 캡션 |

실행 결과는 `outputs/<YYYY-MM-DD>-<slug>/`에 쌓인다: `brief.json`, `plan.md`, `research.json`, `drafts/<channel>.r<N>.json|.md`, `reviews/<channel>.r<N>.json`, `final/<channel>.md`, `final/summary.md`. 통과 기준은 80점 이상이면서 critical 이슈 0개, 수정은 채널당 최대 2회다.

## 규칙

- **한국어 산출물.** 사용자에게 보이는 모든 글(프롬프트, 문서, UI 문구, 결과물)은 한국어. 코드 식별자와 주석은 영어.
- **모든 수치에 출처와 기준시점.** 사실은 리서치 팩에 있는 것만 쓴다. 통계, 사람, 고객사, 후기, 실적을 지어내지 않고 `[대표자 성명]`, `[확인 필요: …]`, `○○` 같은 자리표시를 쓴다. 가정은 가정이라고 밝힌다.
- **채널 가이드가 단일 기준.** `src/insia_agents/prompts/channels/<channel>.md`가 채널 형식의 기준이고, API 백엔드와 Claude Code 스킬이 함께 읽는다. 형식 검사와 루브릭 배점은 `src/insia_agents/channels.py`의 `CHANNELS`와 `check_format`에 있다. 플랫폼 정책(해시태그 한도, 글자수 등)이 바뀌면 **가이드와 `channels.py`를 같이** 고치고, 가이드에 확인한 날짜를 적는다.
- **에이전트 프롬프트도 한 곳.** `src/insia_agents/prompts/agents/{orchestrator,researcher,reviewer}.md`는 API 백엔드의 시스템 프롬프트이자 `.claude/agents/*.md`가 참조하는 세부 기준이다. 원칙을 바꾸면 양쪽을 함께 맞춘다.
- **데이터 계약.** `src/insia_agents/models.py`의 필드 이름·타입은 백엔드, 대시보드, 녹화 데모가 함께 쓴다. 바꾸기 전에 전체 영향을 확인한다.
- **자동 게시 금지.** 어떤 채널에도 자동으로 올리지 않는다. 최종 패키지에는 출처 목록과 사람 최종 승인 체크리스트가 들어간다.
- **`outputs/`는 git에 올리지 않는다**(`.gitignore`에 있음). 데모용 녹화 실행은 `examples/sample-run/`에 둔다.

## 개발 명령

```bash
pip install -e .                               # 패키지 설치 (개발 모드)
pytest                                         # 테스트 (오프라인)
python -m insia_agents run --mode mock         # 키 없이 샘플 실행 재생
python -m insia_agents run --topic "…" --channels bizplan,naver_blog   # 실제 실행 (ANTHROPIC_API_KEY 필요)
python -m insia_agents serve                   # 대시보드 http://127.0.0.1:8765
python -m insia_agents check <draft.json>      # 초안 형식 검사
```

설치 전에는 `PYTHONPATH=src python -m insia_agents …`로 실행한다.

## 파일 지도

| 경로 | 내용 |
|---|---|
| `src/insia_agents/models.py` | Brief, Plan, ResearchPack, Draft, Review 등 데이터 계약 |
| `src/insia_agents/channels.py` | 채널 루브릭, 결정적 형식 검사, `finalize_review`(점수 확정) |
| `src/insia_agents/prompts/agents/` | 에이전트 시스템 프롬프트 |
| `src/insia_agents/prompts/channels/` | 채널 가이드 (형식의 단일 기준) |
| `.claude/agents/`, `.claude/skills/` | Claude Code 서브에이전트와 슬래시 명령 |
| `web/` | 대시보드 (정적 파일, 3D 카피바라 에셋은 `web/assets/`) |
| `examples/sample-run/` | 샘플 브리프와 녹화된 실행 |
