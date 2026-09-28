# INSIA 스마트에이전트: Claude Code 작업 안내

한국의 1인 창업자·소상공인을 위한 3-에이전트 콘텐츠 시스템이다. 브리프 하나로 **사업계획서**(예비창업패키지 PSST)와 **네이버 블로그·링크드인·인스타그램** 초안을 만들고, 리서치 → 작성 → 검수 → 수정 루프를 돌린다. 결과물은 초안이며, 사람이 최종 승인한 뒤 직접 게시한다. 데모가 아니라 **매주 실제로 쓰는 도구**다: 워크스페이스(SQLite)에 프로필·자료·실행·보관함·캘린더·사용량을 저장하고, 주간 계획 → 예정일 초안 → 검토·승인 → 내보내기 → 게시 완료 표시 흐름을 돈다.

같은 시스템을 두 방식으로 쓴다.
1. **Claude Code**: `.claude/agents/`의 서브에이전트와 `.claude/skills/`의 슬래시 명령. 웹 검색은 Claude Code의 WebSearch·WebFetch를 쓴다. 시작할 때 워크스페이스의 프로필·자료를 가져오고, 끝나면 `insia import-run`으로 보관함에 넣는다.
2. **Python 패키지 `insia_agents`**: Anthropic API로 도는 CLI(`insia`)와 로컬 대시보드(스튜디오 · 보관함 · 캘린더 · 브랜드·자료 · 사용량). API 자격 증명이 없으면 mock 모드로 돈다. mock은 샘플 브리프(`examples/sample-run/brief.json`)면 녹화된 실행을 재생하고, 다른 주제면 `[데모]` 템플릿으로 같은 흐름을 만든다.

## 에이전트 세 명

| id | 이름 | 마스코트(3D 카피바라) | 하는 일 |
|---|---|---|---|
| `orchestrator` | 총괄 에이전트 | 유자 디렉터 | 브리프 → 계획, 리서치·검수 위임, 채널별 작성, 피드백 반영, 최종 패키지, 보관함 가져오기 |
| `researcher` | 리서치 에이전트 | 돋보기 탐험가 | 한국어·영어 웹 검색, 출처 등급(Tier 1~3)이 붙은 리서치 팩 (사용자 자료는 origin user 출처) |
| `reviewer` | 검수 에이전트 | 꼼꼼 검수관 | 루브릭 채점, 리서치 팩·프로필 대조 사실 확인, 형식 검사, 수정 요청 (초안은 고치지 않음) |

## 실행 방법 (Claude Code)

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

실행 결과는 `outputs/<YYYY-MM-DD>-<slug>/`에 쌓인다: `brief.json`, `profile.json`·`documents.json`(워크스페이스에서 가져온 것, 있을 때), `plan.md`·`plan.json`, `research.json`, `drafts/<channel>.r<N>.json|.md`, `reviews/<channel>.r<N>.json`, `final/<channel>.md`, `final/summary.md`. 통과 기준은 80점 이상이면서 critical 이슈 0개, 수정은 채널당 최대 2회다. 에이전트가 쓰는 CLI 계약:

- `python -m insia_agents profile show --json` → Profile JSON 그대로(감싸지 않음, 비었으면 모든 필드가 빈 값)
- `python -m insia_agents docs list --json` → UserDocument 배열(본문 `text` 포함)
- `python -m insia_agents check <draft.json> [--brief …] [--profile <json>|--workspace-profile|--no-profile]` → 형식·브랜드 검사(근처 `brief.json`·`profile.json` 자동 사용, 실패하면 종료 코드 1)
- `python -m insia_agents import-run <run 폴더>` → 실행·콘텐츠·버전·검수로 저장(검수는 현재 코드로 다시 확정, 같은 폴더를 다시 가져오면 중복 없이 갱신, 잘못된 파일이 있으면 종료 코드 1)

## 규칙

- **한국어 산출물.** 사용자에게 보이는 모든 글(프롬프트, 문서, UI 문구, CLI 도움말·오류, 결과물)은 자연스러운 한국어(독자용 해요체, 보고체 아님). 코드 식별자와 주석은 영어.
- **자동 게시 금지.** 어떤 채널에도 자동으로 올리지 않는다. 승인(`approved`)은 최신 버전의 검수 통과가 필요하고(아니면 사람이 `force`), `published`는 사람이 직접 올린 뒤 남기는 기록이다. 최종 패키지에는 출처 목록과 사람 최종 승인 체크리스트가 들어간다.
- **사실은 리서치 팩·회사 프로필·사용자 자료에서만.** 모든 수치에 출처와 기준시점. 통계, 사람, 고객사, 후기, 실적을 지어내지 않고 `[대표자 성명]`, `[확인 필요: …]`, `○○` 같은 자리표시를 쓴다. 가정은 가정이라고 밝힌다. 프로필 사실은 적힌 만큼만 쓰고(사업계획서는 "(자사 자료)"), 사업계획서에 팀원 실명을 쓰지 않는다(블라인드).
- **채널 가이드가 단일 기준.** `src/insia_agents/prompts/channels/<channel>.md`가 채널 형식의 기준이고, API 백엔드와 Claude Code 스킬이 함께 읽는다. 형식 검사와 루브릭 배점은 `src/insia_agents/channels.py`의 `CHANNELS`와 `check_format`(프로필이 있으면 금지 표현·필수 문구·블라인드 검사 포함)에 있다. 플랫폼 정책(해시태그 한도, 글자수 등)이 바뀌면 **가이드와 `channels.py`를 같이** 고치고, 가이드에 확인한 날짜를 적는다.
- **에이전트 프롬프트도 한 곳.** `src/insia_agents/prompts/agents/{orchestrator,researcher,reviewer,planner}.md`는 API 백엔드의 시스템 프롬프트이자 `.claude/agents/*.md`가 참조하는 세부 기준이다. 원칙을 바꾸면 양쪽을 함께 맞춘다.
- **데이터 계약.** `src/insia_agents/models.py`의 필드 이름·타입은 백엔드, DB, API, 대시보드, 녹화 데모가 함께 쓴다. 새 필드는 기본값과 함께 추가하고, 기존 필드는 바꾸기 전에 전체 영향을 확인한다.
- **워크스페이스.** `Settings.home` = 환경 변수 `INSIA_HOME`(CLI는 `--home`), 기본 `./workspace`(git 제외). `insia.db`(WAL, 스키마는 `db.MIGRATIONS`에 뒤로만 추가 — 배포된 마이그레이션은 절대 고치지 않는다), `exports/`, `uploads/`, `logs/`, 선택 `prices.json`. `insia run`은 기본으로 워크스페이스에 저장한다(`--no-workspace`로 끔).
- **비용.** 모든 API 응답의 사용량이 `UsageRecord`로 기록되고, 실행마다 `INSIA_MAX_COST_USD`/`--max-cost-usd` 상한이 걸린다. 가격표는 `costs.py` 기본값 + `prices.json` + `INSIA_PRICE_*`.
- **보안.** 서버 기본 바인드는 `127.0.0.1`. 루프백이 아닌 주소나 `--public-host`로 열 때는 접근 토큰(`INSIA_ACCESS_TOKEN`/`--token`)이 필수다.
- **테스트는 임시 워크스페이스로.** 테스트와 수동 실행은 항상 `tmp_path`/임시 `INSIA_HOME`을 쓰고 저장소의 `workspace/`를 건드리지 않는다. 테스트는 오프라인(mock 백엔드, 가짜 클라이언트)이고 전체가 1분 안에 끝나야 한다. 선택 라이브러리(pypdf, python-docx, PyYAML, playwright)가 없으면 해당 테스트는 건너뛴다.
- **`outputs/`와 `workspace/`는 git에 올리지 않는다**(`.gitignore`에 있음). 데모용 녹화 실행은 `examples/sample-run/`에 둔다.

## 개발 명령

```bash
pip install -e ".[export,docs]" pytest        # 패키지(개발 모드) + Word 내보내기·자료 읽기 + pytest
pytest                                         # 테스트 (오프라인)
python -m insia_agents doctor                  # 설치·설정 점검
python -m insia_agents run --brief examples/sample-run/brief.json --mode mock --speed 0   # 키 없이 샘플 실행 재생
python -m insia_agents run --topic "…" --channels bizplan,naver_blog --max-cost-usd 5   # 실제 실행 (ANTHROPIC_API_KEY 필요)
python -m insia_agents serve                   # 대시보드 http://127.0.0.1:8765
python -m insia_agents plan-week --theme "…"   # 주간 계획 → run-due로 예정일 초안
python -m insia_agents items list              # 보관함 (show / approve / export / publish …)
python -m insia_agents import-run examples/sample-run   # Claude Code 형식 실행 폴더 가져오기
python -m insia_agents check <draft.json>      # 초안 형식·브랜드 검사
python scripts/build_artifact.py               # 대시보드 단일 페이지 빌드 (dist/artifact)
```

설치 전에는 `PYTHONPATH=src python -m insia_agents …`로 실행한다. 명령마다 `-h`로 한국어 도움말을 보고, 스크립트용은 `--json`. 종료 코드는 0 성공, 1 작업 실패, 2 잘못된 사용법. `run`에는 `--brief` 파일이나 `--topic`이 꼭 있어야 한다. `--speed`는 mock 재생 배속이다(`run` 기본 1 = 실제 시간, 작업·`run-due`는 기본 0). 대시보드는 `file://`로 열면 기록을 불러오지 못하니 `serve`나 `web/`에서 띄운 `python3 -m http.server`로 연다. push·PR마다 `.github/workflows/ci.yml`이 `pytest`와 `build_artifact.py`를 돌린다. 운영(설치·백업·업데이트·cron·보안·문제 해결)은 `docs/operations.md`, HTTP API는 `docs/api.md`.

## 파일 지도

| 경로 | 내용 |
|---|---|
| `src/insia_agents/models.py` | Brief, Plan, ResearchPack, Draft, Review, Profile, UserDocument, ContentItem, CalendarSlot 등 데이터 계약 |
| `src/insia_agents/channels.py` | 채널 루브릭, 결정적 형식·브랜드 검사, `finalize_review`(점수 확정) |
| `src/insia_agents/db.py` | 워크스페이스(SQLite): 프로필, 자료, 실행·이벤트, 콘텐츠·버전, 사용량, 캘린더 |
| `src/insia_agents/pipeline.py` | 실행 루프, 이어서 실행(`resume_run`), 예산 상한, 실행 컨텍스트(`build_context`) |
| `src/insia_agents/actions.py`, `planner.py` | 재검수·수정 요청·직접 수정·슬롯 초안, 주간 계획 |
| `src/insia_agents/exporters/` | docx, 네이버 html, txt, 카드뉴스 zip, 실행 묶음 |
| `src/insia_agents/costs.py` | 토큰 → 비용, 가격표 |
| `src/insia_agents/cli.py`, `server.py` | 명령줄(`insia`)과 대시보드 서버(REST + SSE, 접근 토큰) |
| `src/insia_agents/prompts/agents/` | 에이전트 시스템 프롬프트 (planner 포함) |
| `src/insia_agents/prompts/channels/` | 채널 가이드 (형식의 단일 기준) |
| `.claude/agents/`, `.claude/skills/` | Claude Code 서브에이전트와 슬래시 명령 |
| `web/` | 대시보드 (정적 파일, 3D 카피바라 에셋은 `web/assets/`) |
| `examples/sample-run/` | 샘플 브리프와 녹화된 실행 (Claude Code 실행 폴더와 같은 구조) |
| `docs/operations.md`, `docs/api.md` | 운영 안내(사용자용), HTTP API 레퍼런스 |
| `Dockerfile`, `docker-compose.yml` | 서버 배포 (비루트, `/data` 볼륨, 접근 토큰 필수, 기본 `127.0.0.1:8765`) |
