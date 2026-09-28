---
name: bizplan
description: 예비창업패키지 등 정부 창업지원사업용 사업계획서 초안을 PSST 양식(문제인식·실현가능성·성장전략·팀 구성)과 개조식 문체로 만든다. 공식 통계 리서치, TAM·SAM·SOM, 비즈니스 모델, 추진 일정·사업비 표, 팀 자리표시까지 넣고 검수·수정 루프를 돌린다. 사업계획서·지원사업 신청서를 쓸 때 사용.
argument-hint: "[사업 아이템/주제 또는 brief.json 경로] [지원사업명(선택)]"
---

# 사업계획서 (PSST)

요청: $ARGUMENTS

메인 스레드에서 총괄 에이전트 역할로 실행하고, 리서치는 `researcher`, 검수는 `reviewer` 서브에이전트에게 Agent 도구로 맡긴다. 결과물은 신청자가 사실을 채우고 자기 문장으로 다듬어 제출할 초안이다.

## 먼저 읽을 것

- `src/insia_agents/prompts/channels/bizplan.md`: 양식 구조, 출력 형식, 루브릭, 체크리스트 (단일 기준)
- `src/insia_agents/prompts/agents/orchestrator.md`: 작성 원칙, Draft·수정 규칙
- 공통 절차의 명령어 전문은 `.claude/skills/content-studio/SKILL.md`와 같다.

## 1. 브리프

- `$ARGUMENTS`가 `.json` 경로면 그대로 읽고 `channels`를 `["bizplan"]`로 둔다.
- 글이면 Brief를 채운다. `goal`에 지원사업명(없으면 "예비창업패키지 신청"으로 가정), `notes`에 팀·보유 자료·가격 가정.
- 주제를 알 수 없을 때만 질문한다(최대 3개): ① 아이템 한 줄과 목표 고객 ② 지원사업명·연도 ③ 현재 단계(아이디어·시제품·매출).
- `outputs/<YYYY-MM-DD>-<slug>/brief.json` 저장.
- 이어서 `.claude/skills/content-studio/SKILL.md` 1-1단계대로 워크스페이스의 회사 프로필(`profile.json`)과 사용자 자료(`documents.json`)를 가져온다. 명령이 실패하면 핵심 사실 3가지만 묻고 진행한다. 프로필의 회사 사실·실적·가격·팀 역량은 `(자사 자료)`로 쓰고, 사용자 자료는 `[s#]`로 인용한다.

## 2. 계획과 리서치

`plan.md`에 핵심 메시지와 리서치 질문 4~6개를 쓴다. 사업계획서에 꼭 필요한 질문:
- 시장 규모와 추이(TAM 산정용 공식 통계, 최신 기준연도)
- 목표 고객 수(SAM 산정용: 사업체 수, 종사자 규모 등)
- 고객 문제의 근거(실태조사·설문의 애로 항목)
- 경쟁·대체 서비스와 가격(요금 페이지, 공시)
- 해당 지원사업의 최신 공고 기준(K-Startup 공고문의 평가지표, 사업비 비목)

`researcher`에게 질문 전체와 저장 경로 `<run>/research.json`을 넘기고, Tier 1(KOSIS, 중소벤처기업부, 소상공인시장진흥공단, K-Startup, DART, KIPRIS 등)을 우선하라고 지시한다. 돌아오면 ResearchPack으로 검증한다.

## 3. 초안 (round 0) 작성 포인트

- 첫 줄 `# <아이템명> 사업계획서`, 이어서 일반현황 → 창업 아이템 개요(요약) → `## 1. 문제 인식 (Problem)…` → `## 2. 실현 가능성 (Solution)…` → `## 3. 성장전략 (Scale-up)…` → `## 4. 팀 구성 (Team)…` → `## 참고자료` → `## 제출 전 확인 필요 사항 (제출본에서 삭제)`.
- 개조식: `□ (라벨) 명사형 한 줄`, 근거는 `- `, 세부는 들여쓴 `  - `, 가정·참고는 `※`.
- 수치마다 기준시점 + `[s#]`, 참고자료 목록과 번호 일치. 계산값은 `※ 산식:`.
- TAM → SAM → SOM 산식과 가정, 비즈니스 모델(가격은 가정 표시), 추진 일정 표, 1·2단계 사업비 표(공고의 9개 비목만, 산출 근거 = 품목 수량×단가, 대표자 인건비 제외).
- 팀은 프로필에 있는 역할·학위·전공·경력 연수·보유 역량만 `(자사 자료)`로 쓰고, 없는 정보는 `[대표자 성명]`, `[학위·전공]`, `[경력: ○○ 분야 ○년]` 자리표시로 둔다. 성명·학교명·직장명은 프로필에 있어도 절대 쓰지 않는다(블라인드, 프로필이 있으면 코드가 실명 노출을 검사한다).
- 분량: 공백 제외 3,000~15,000자. `hashtags`는 `[]`.

`drafts/bizplan.r0.json`(Draft JSON)과 `.md` 저장 후 형식 검사:

```bash
python -m insia_agents check <run>/drafts/bizplan.r0.json
# 설치 전이면: PYTHONPATH=src python -m insia_agents check <run>/drafts/bizplan.r0.json
```

`check`는 가까운 `brief.json`을 자동으로 찾는다(또는 `--brief <run>/brief.json`). `profile.json`이 있으면 금지 표현·필수 문구·블라인드 검사까지 도는 content-studio 4단계의 프로필 포함 명령을 쓴다.

## 4. 검수와 수정 (최대 2회)

1. `reviewer`에게 채널 `bizplan`, round, 초안·`research.json`·`brief.json`·(있으면) `profile.json`·가이드 경로를 넘긴다. 돌려받은 Review JSON을 `reviews/bizplan.r<N>.json`에 저장하고 `finalize_review`로 확정한다(명령은 content-studio 5단계와 같음).
2. 통과 = 80점 이상 + critical 0개. 미통과면 critical·major를 모두 고치고(`needs_research`가 있으면 추가 조사 먼저) `round + 1`로 저장, `change_log`에 이슈별 한 줄.
3. 2회 뒤에도 미통과면 최고 점수 round를 최종본으로, 미통과로 표시한다.

## 5. 최종 패키지와 보고

- `final/bizplan.md`, `final/summary.md`(요약 표, 모든 출처, 확인 필요 목록, 승인 체크리스트).
- `python -m insia_agents import-run <run>`으로 워크스페이스 보관함에 가져온다(로컬 저장, 게시 아님). 실패하면 오류를 한 줄로 전하고 나중에 같은 명령으로 가져올 수 있다고 안내한다.
- 사용자에게: `| 채널 | 점수 | 라운드 | 통과 여부 |` 표, 실행 폴더 경로, 보관함 가져오기 결과, 신청자가 채울 것 3가지 이내, 승인 체크리스트.

사람 최종 승인 체크리스트 (사업계획서)
- [ ] `[확인 필요]`·`[대표자 성명]`·`○○` 자리를 실제 사실로 채웠다
- [ ] 핵심 수치를 참고자료 원문에서 다시 확인했다
- [ ] 가격·일정·사업비가 실제 계획과 맞는다
- [ ] 최신 공고 첨부 양식에 옮겼고 목차를 바꾸지 않았다
- [ ] 블라인드 규정(성명·학교·직장명 가림)을 지켰다
- [ ] 본인 문장으로 다듬었다(대필·유사도 검토 규정)
