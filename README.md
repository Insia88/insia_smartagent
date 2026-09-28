# INSIA 스마트에이전트

![카피바라 에이전트 3인방: 리서치, 총괄, 검수](web/assets/hero/team.webp)

총괄·리서치·검수 에이전트 셋이 한 팀으로 **사업계획서**와 **네이버 블로그 · 링크드인 · 인스타그램** 콘텐츠를 만듭니다. 리서치 에이전트가 출처 등급을 붙여 근거를 모으고, 총괄 에이전트가 채널 형식에 맞춰 쓰고, 검수 에이전트가 루브릭과 사실 확인으로 점수를 매겨 되돌려 보냅니다. 기준을 넘길 때까지 최대 두 번 고치고, 마지막 게시는 사람이 합니다.

| | 에이전트 | 하는 일 |
|---|---|---|
| <img src="web/assets/agents/orchestrator-cutout.webp" width="72" alt="총괄 에이전트 카피바라"> | **총괄 에이전트** · 유자 디렉터 | 브리프를 받아 계획을 세우고, 리서치를 맡기고, 채널별 초안을 쓰고, 검수 의견을 반영해 최종본을 묶습니다. |
| <img src="web/assets/agents/researcher-cutout.webp" width="72" alt="리서치 에이전트 카피바라"> | **리서치 에이전트** · 돋보기 탐험가 | 한국어·영어로 웹을 검색해 공식 통계·공공기관 자료를 먼저 찾고, 주장마다 출처와 기준 시점을 붙입니다. |
| <img src="web/assets/agents/reviewer-cutout.webp" width="72" alt="검수 에이전트 카피바라"> | **검수 에이전트** · 꼼꼼 검수관 | 채널 루브릭으로 채점하고, 모든 수치를 리서치 자료와 대조하고, 형식 규칙을 코드로 검사해 수정 요청을 보냅니다. |

매주 쓰는 흐름은 이렇습니다. 월요일에 한 주 콘텐츠를 계획하면, 게시일마다 에이전트가 초안을 쓰고 채점해 **보관함**에 넣습니다. 사람은 보관함에서 읽고, 수정을 요청하거나 직접 고치고, 승인한 뒤 붙여넣기용 파일로 내보내 직접 게시합니다. 모든 기록은 내 컴퓨터의 워크스페이스(SQLite)에 남고, 실행마다 비용이 보이고 상한을 걸 수 있습니다.

> **자동 게시는 하지 않습니다.** 어떤 채널에도 사람의 승인 없이 올리지 않습니다.

## 빠른 시작

파이썬 3.10 이상이 필요합니다. 처음 설치라면 [운영 안내 · 설치](docs/operations.md#1-설치)를 따라 하세요(Windows·macOS 가상환경 포함).

```bash
pip install -e ".[export,docs]"   # Word 내보내기 + PDF·Word 자료 읽기 + YAML 프로필
insia doctor                      # 설치·API 키·워크스페이스 점검
insia serve                       # 대시보드 http://127.0.0.1:8765 (API 키 없어도 데모로 열려요)
```

API 키 없이 녹화된 샘플 실행을 재생해 보고(무료), 키를 넣어 실제로 돌려 봅니다.

```bash
insia run --brief examples/sample-run/brief.json --mode mock --speed 0

export ANTHROPIC_API_KEY=sk-ant-...          # Windows: setx ANTHROPIC_API_KEY "sk-ant-..."
insia run --topic "동네 베이커리 온라인 주문 서비스" --channels naver_blog,linkedin,instagram --max-cost-usd 5
```

결과는 워크스페이스 **보관함**(`insia items list`, 대시보드 "보관함")과 `outputs/<실행 id>/` 파일로 저장됩니다. 늘 켜 두는 서버라면 Docker로 띄웁니다: `.env`에 키와 접근 토큰을 적고 `docker compose up -d --build` ([운영 안내 · Docker](docs/operations.md#1-2-docker로-설치-늘-켜-두는-서버)).

## 첫 설정 (한 번)

에이전트는 **회사 프로필**과 **참고 자료**를 사용자가 준 사실로 씁니다. 적힌 만큼만 쓰고 부풀리지 않습니다.

```bash
insia profile edit-template          # profile.yaml 양식 → 메모장으로 채우기
insia profile import profile.yaml    # 저장 (대시보드 "브랜드·자료"에서도 채울 수 있어요)
insia docs add 회사소개서.pdf         # 참고 자료 (txt, md, pdf, docx)
export INSIA_MAX_COST_USD=5          # 실행 1번의 예산 상한(달러)
```

- 프로필에는 서비스 설명·목표 고객·차별점·실적 같은 사실과 브랜드 규칙(톤, **금지 표현**, **필수 문구**, 기본 해시태그, CTA)이 들어갑니다. 팀원 실명은 사업계획서에 절대 쓰지 않습니다(블라인드 규정).
- 데이터는 워크스페이스 폴더(`INSIA_HOME`, 기본 `./workspace`) 하나에 모입니다. 이 폴더를 복사하면 백업입니다.

## 매주 쓰는 법

| 언제 | 명령 | 대시보드 |
|---|---|---|
| 월요일 | `insia plan-week --theme "…" --blog 2 --linkedin 2 --instagram 2` | 캘린더 → 이번 주 계획 세우기 |
| 매일 아침 (cron·작업 스케줄러) | `insia run-due --limit 3` | 캘린더 → 초안 만들기 |
| 초안이 생기면 | `insia items show <id>` → `insia revise <id> -i "…"` → `insia items approve <id>` | 보관함 (편집 · 재검수 · 수정 요청 · 승인) |
| 게시 직전 | `insia items export <id>` (블로그 html · 링크드인 txt · 인스타그램 카드 zip · 사업계획서 docx) | 보관함 → 내보내기 |
| 게시한 뒤 | `insia items publish <id> --url https://…` | 보관함 → 게시 완료 |
| 수시로 | `insia usage` · `insia runs list` · `insia resume <실행 id>` | 사용량 · 스튜디오 |

- 승인은 최신 버전이 검수를 통과해야 되고, 직접 확인했다면 `--force`("그래도 승인")로 승인할 수 있습니다.
- 예산 상한을 넘으면 끝난 채널을 저장한 채 멈추고, `insia resume`으로 남은 작업만 이어서 합니다.
- 설치, 비용 관리, 백업, 업데이트, 자동 실행 예시, 보안(접근 토큰), 문제 해결은 **[docs/operations.md](docs/operations.md)**에 있습니다. HTTP API는 [docs/api.md](docs/api.md)를 보세요.

## Claude Code에서 쓰기

이 저장소를 Claude Code로 열면 `.claude/agents/`의 세 에이전트와 `.claude/skills/`의 슬래시 명령을 바로 쓸 수 있습니다. 이때 리서치는 Claude Code의 WebSearch·WebFetch로 합니다.

```bash
claude --agent orchestrator        # 메인 세션을 총괄 에이전트로 시작
```

| 명령 | 용도 |
|---|---|
| `/content-studio <주제>` | 한 브리프로 네 채널을 한 번에 (원 소스 멀티 유즈) |
| `/bizplan <아이템>` | 예비창업패키지 PSST 양식 사업계획서 초안 |
| `/naver-blog <주제>` | 검색 의도에 맞춘 네이버 블로그 글 |
| `/linkedin <주제>` | '더 보기' 전 두 줄 훅이 있는 링크드인 게시물 |
| `/instagram <주제>` | 7~10장 캐러셀 문구·비주얼 지시 + 캡션 |

에이전트는 시작할 때 워크스페이스의 프로필과 자료를 가져오고(`insia profile show --json`, `insia docs list --json`), 작업 파일을 `outputs/<날짜>-<슬러그>/`에 쌓은 뒤 마지막에 보관함으로 가져옵니다.

```bash
insia check outputs/<실행>/drafts/linkedin.r0.json   # 형식 검사 (근처 brief.json·profile.json을 찾아 프로필 규칙까지)
insia import-run outputs/<실행>                      # 실행 폴더 → 보관함 (다시 가져와도 중복 없이 갱신)
```

## 데모

`web/`의 대시보드 **스튜디오**가 세 에이전트의 작업을 실시간으로 보여 줍니다. 활성화된 에이전트는 모션 영상으로 움직이고, 작업이 넘어갈 때마다 패킷이 에이전트 사이를 오가며, 채널 카드에 점수와 수정 라운드가 채워집니다. API 키가 없어도 실행 기록을 재생하는 데모 모드로 볼 수 있습니다.

- **데모 모드**: 페이지를 열면 실행 기록을 재생합니다. `web/demo/demo-run.json`(`insia run --record`로 녹화한 기록)이 있으면 그 파일을, 없으면 손으로 만든 예시 `web/demo/sample-trace.json`을 씁니다. 1×/2×/4× 속도, 일시정지, 구간 이동이 됩니다. 브라우저는 `file://` 페이지의 `fetch()`를 막으므로 `web/index.html`을 파일로 바로 열면 기록을 불러오지 못합니다. 아래 셋 중 하나로 여세요.
  - `insia serve`
  - 정적 서버: `cd web && python3 -m http.server 8000` 뒤 http://127.0.0.1:8000
  - 단일 페이지 빌드: `python scripts/build_artifact.py`로 만든 `dist/artifact/index.html`. 기록과 에셋 목록이 페이지 안에 들어 있어 파일로 열어도 재생됩니다(보관함은 샘플 결과를 읽기 전용으로 보여 줘요).
- **라이브 모드**: `insia serve`로 열면 **새 실행** 버튼(브리프 폼)으로 작업을 시작하고, 이벤트가 SSE로 들어오는 대로 화면에 그립니다. API 자격 증명(`ANTHROPIC_API_KEY` 등)이 있으면 Claude API로 실제 실행하고, 없으면 모의 실행으로 같은 흐름을 보여 줍니다.
- **3D로 보기**: 에이전트 카드의 3D 버튼을 누르면 카피바라 3D 모델(GLB)을 돌려 볼 수 있습니다. 뷰어 스크립트를 CDN에서 불러오므로 인터넷 연결이 필요합니다.
- `examples/sample-run/`은 샘플 브리프로 실제 리서치·작성·검수를 돌린 기록입니다. `insia import-run examples/sample-run`으로 보관함에 넣어 승인·내보내기 흐름을 연습할 수 있습니다.

## 구조

### 파이프라인

```mermaid
sequenceDiagram
    autonumber
    participant U as 사용자
    participant O as 총괄 에이전트
    participant R as 리서치 에이전트
    participant V as 검수 에이전트
    U->>O: 브리프 (주제·목적·독자·채널·키워드) + 회사 프로필·자료
    O->>O: 계획 (리서치 질문, 채널별 개요)
    O->>R: 리서치 질문 전달
    R-->>O: 리서치 팩 (출처 Tier 1~3, 사용자 자료, 근거 문장)
    loop 채널마다
        O->>V: 초안 (라운드 0)
        V-->>O: 루브릭 점수 · 사실 확인 · 형식·브랜드 검사 · 수정 요청
        opt 80점 미만 또는 치명적 이슈 (최대 2회)
            O->>R: 추가 조사 요청 (검수가 근거 부족을 지적했을 때)
            R-->>O: 추가 근거
            O->>V: 수정본 (라운드 1, 2)
        end
    end
    O-->>U: 보관함의 채널별 초안 + 점수 + 출처 목록 → 사람이 승인·게시
```

### 채널별 기준

검수 점수는 100점 만점이고, **80점 이상이면서 치명적 이슈가 없어야** 통과합니다. `형식` 항목은 모델이 아니라 코드(`src/insia_agents/channels.py`)가 채점하므로 같은 초안은 언제나 같은 형식 점수를 받습니다. 회사 프로필이 있으면 금지 표현·필수 문구·사업계획서 블라인드(실명 미노출)도 코드가 검사합니다.

| 채널 | 루브릭 (배점) | 코드가 검사하는 형식 |
|---|---|---|
| 사업계획서 | 문제인식 20 · 실현가능성 20 · 성장전략 20 · 팀 구성 10 · 근거·출처 20 · 형식 10 | PSST 4개 섹션 제목, 공백 제외 3,000~15,000자 |
| 네이버 블로그 | 검색 의도·키워드 20 · 경험·독창성 20 · 가독성 20 · 정확성·출처 20 · 마무리 10 · 형식 10 | 공백 제외 1,500~3,000자, 소제목 3개+, 이미지 자리 3개+, 태그 5~10개, 제목 40자 이내·메인 키워드 포함 |
| 링크드인 | 훅 25 · 인사이트 25 · 구조 15 · 정확성 15 · 대화 유도 10 · 형식 10 | 1,300~2,000자, 첫 두 줄 210자 이하, 해시태그 3~5개, 본문 링크 없음 |
| 인스타그램 | 훅 25 · 캐러셀 흐름 25 · 비주얼 지시 15 · 정확성 15 · 저장·공유 유도 10 · 형식 10 | 슬라이드 7~10장, 캡션 2,200자 이하, 캡션 첫 줄 125자 이하, 해시태그 3~5개 |

플랫폼 정책은 바뀝니다. 기준을 고칠 때는 `channels.py`의 숫자와 `src/insia_agents/prompts/channels/<채널>.md` 가이드를 **함께** 고치세요. 두 파일이 API 백엔드와 Claude Code 에이전트 양쪽의 단일 기준입니다.

### 폴더

```
.claude/agents/          Claude Code 서브에이전트 (orchestrator, researcher, reviewer)
.claude/skills/          슬래시 명령 (/content-studio, /bizplan, /naver-blog, /linkedin, /instagram)
src/insia_agents/
  models.py              데이터 계약 (Brief, Plan, ResearchPack, Draft, Review, Profile, ContentItem …)
  channels.py            채널 루브릭, 결정적 형식·브랜드 검사
  prompts/               에이전트 시스템 프롬프트와 채널 가이드
  backends/              Claude API 백엔드와 오프라인 모의 백엔드
  agents/ pipeline.py    에이전트 계층과 계획→리서치→작성→검수→수정 루프 (이어서 실행, 예산 상한)
  db.py                  워크스페이스 (SQLite: 프로필, 자료, 실행, 보관함, 캘린더, 사용량)
  actions.py planner.py  재검수·수정 요청·직접 수정·슬롯 초안, 주간 콘텐츠 계획
  exporters/             붙여넣기·업로드용 파일 (docx, 네이버 html, txt, 카드뉴스 zip)
  costs.py               토큰 → 비용 (가격표는 prices.json·환경 변수로 변경)
  server.py cli.py       대시보드 서버(REST + SSE, 접근 토큰)와 명령줄
web/                     대시보드 (스튜디오 · 보관함 · 캘린더 · 브랜드·자료 · 사용량)
examples/sample-run/     샘플 브리프로 실제 리서치·작성·검수를 돌린 결과
docs/                    운영 안내, API, 구조, 이벤트 스키마, 3D 에셋 제작 기록
Dockerfile docker-compose.yml   서버 배포 (접근 토큰 필수, 기본 127.0.0.1)
tests/                   오프라인 테스트 (pytest)
```

문서: [운영 안내](docs/operations.md) · [HTTP API](docs/api.md) · [아키텍처](docs/architecture.md) · [이벤트 스키마](docs/event-schema.md) · [3D 에셋](docs/assets.md)

## 알아 둘 점

- **자동 게시는 하지 않습니다.** 결과물은 초안이고, 사람이 사실과 표현을 확인한 뒤 각 플랫폼에 직접 올립니다. "게시 완료" 표시는 기록일 뿐입니다.
- **근거 없는 수치는 쓰지 않습니다.** 모든 수치는 리서치 팩의 출처와 기준 시점을 따라가며, 검수 에이전트가 출처 없는 수치를 치명적 이슈로 처리합니다. 사용자 자료와 프로필은 "사용자 제공 사실"로 표시됩니다. 그래도 제출·게시 전에 원문 링크를 한 번 더 열어 보세요.
- **사업계획서의 팀 정보는 비워 둡니다.** `[대표자 성명]` 같은 자리표시를 신청자가 채웁니다. 정부지원사업은 대필과 허위 기재를 금지하므로 최종 문장은 신청자가 다듬어야 합니다.
- **내 컴퓨터가 기본입니다.** 서버는 `127.0.0.1`에만 열리고, 바깥에 열려면 접근 토큰이 필요합니다.
- 캐릭터·아이콘·모션·3D 모델은 Higgsfield로 만들었습니다. 프롬프트와 제작 순서는 [docs/assets.md](docs/assets.md)에 있습니다.

## 개발

```bash
pip install -e ".[export,docs]" pytest
pytest                                 # 오프라인 테스트 (임시 워크스페이스를 써요)
python scripts/build_artifact.py       # 대시보드를 단일 페이지(dist/artifact)로 빌드
```

GitHub Actions(`.github/workflows/ci.yml`)가 push와 pull request마다 Python 3.11에서 테스트와 단일 페이지 빌드를 돌립니다. 모델은 기본 `claude-opus-5`이고 `--model` 또는 `INSIA_MODEL`로 바꿉니다. 안전 분류기가 요청을 거절하면 서버 측 폴백(`fallbacks: "default"`)이 다른 모델로 이어서 처리하며, `--no-fallbacks` 또는 `INSIA_FALLBACKS=0`으로 끌 수 있습니다.
