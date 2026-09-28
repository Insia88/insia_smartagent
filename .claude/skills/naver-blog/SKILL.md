---
name: naver-blog
description: 네이버 블로그 글을 검색 의도에 바로 답하는 구조로 만든다. 메인 키워드가 들어간 40자 이하 제목, 해요체 도입부, 소제목 3개 이상, 이미지 자리 3개 이상, 출처 줄, 태그 5~10개까지 갖춘 초안을 리서치·검수 루프로 완성한다. 네이버 블로그 포스팅을 쓸 때 사용.
argument-hint: "[주제 또는 brief.json 경로] [메인 키워드(선택)]"
---

# 네이버 블로그

요청: $ARGUMENTS

메인 스레드에서 총괄 에이전트 역할로 실행하고, 리서치는 `researcher`, 검수는 `reviewer` 서브에이전트에게 Agent 도구로 맡긴다. 게시는 사람이 승인한 뒤 직접 한다.

## 먼저 읽을 것

- `src/insia_agents/prompts/channels/naver_blog.md`: 출력 형식, 루브릭, 체크리스트 (단일 기준)
- `src/insia_agents/prompts/agents/orchestrator.md`: 작성 원칙, Draft·수정 규칙
- 공통 절차의 명령어 전문은 `.claude/skills/content-studio/SKILL.md`와 같다.

## 1. 브리프

- `.json` 경로면 그대로 읽고 `channels`를 `["naver_blog"]`로 둔다.
- 글이면 Brief를 채운다. `keywords[0]`이 메인 키워드다. 사용자가 키워드를 주지 않았으면 주제에서 검색량이 있을 법한 2~4어절 표현을 고르고 `plan.md`에 가정으로 적는다.
- 주제를 알 수 없을 때만 질문한다(최대 3개): ① 주제 ② 메인 키워드 ③ 읽을 사람과 글을 읽고 할 행동(문의, 이웃추가 등).
- `outputs/<YYYY-MM-DD>-<slug>/brief.json` 저장.

## 2. 계획과 리서치

`plan.md`에 검색자가 궁금해할 질문 흐름(도입 답 → 소제목 3~5개 → 정리)과 리서치 질문 3~5개를 쓴다. 블로그에 필요한 근거: 독자가 믿을 만한 수치 1~3개, 방법·절차의 공식 안내, 비교 기준. `researcher`에게 `<run>/research.json` 저장을 맡기고 검증한다.

## 3. 초안 (round 0) 작성 포인트

- `title`: 공백 포함 40자 이하, 메인 키워드 포함(앞쪽 권장). 본문에 제목을 다시 쓰지 않는다.
- 도입부 2~4문장, 첫 문장에서 검색 의도에 답한다. 해요체, 문단 1~3문장.
- `## ` 소제목 3개 이상(`###`는 세지 않음), `[이미지: 구체 설명]` 3개 이상.
- 공백 제외 1,500~3,000자. 마지막 `## ` 소제목에 요약 + 행동 유도. 사실을 썼다면 끝에 `출처:` 줄.
- 경험담은 브리프에 있는 것만. 없으면 `[대표 경험 추가: …]` 자리표시.
- `hashtags` 5~10개(`#` 포함, 띄어쓰기 없이), 본문에는 태그를 쓰지 않는다.
- 네이버 검색 로직(C-Rank, D.I.A.)은 계산 방식이 공개되지 않았으니 가이드의 권장사항 수준으로만 반영한다. 키워드 반복으로 채우지 않는다.

`drafts/naver_blog.r0.json`과 `.md` 저장 후 형식 검사:

```bash
python -m insia_agents check <run>/drafts/naver_blog.r0.json
# 설치 전이면: PYTHONPATH=src python -m insia_agents check <run>/drafts/naver_blog.r0.json
```

제목 키워드 검사는 브리프가 있어야 돌므로, 점수 확정 단계(`finalize_review`, content-studio 5단계 명령)에서 최종 확인한다.

## 4. 검수와 수정 (최대 2회)

1. `reviewer`에게 채널 `naver_blog`, round, 초안·`research.json`·`brief.json`·가이드 경로를 넘긴다. Review JSON을 `reviews/naver_blog.r<N>.json`에 저장하고 `finalize_review`로 확정한다.
2. 통과 = 80점 이상 + critical 0개. 미통과면 critical·major를 모두 고치고 `round + 1`로 저장, `change_log`에 이슈별 한 줄.
3. 2회 뒤에도 미통과면 최고 점수 round를 최종본으로, 미통과로 표시한다.

## 5. 최종 패키지와 보고

- `final/naver_blog.md`(제목, 본문, 태그), `final/summary.md`(요약 표, 모든 출처, 확인 사항, 승인 체크리스트).
- 사용자에게: `| 채널 | 점수 | 라운드 | 통과 여부 |` 표, 실행 폴더 경로, 채울 것 3가지 이내, 승인 체크리스트.

사람 최종 승인 체크리스트 (블로그)
- [ ] `[대표 경험 추가]` 같은 자리표시를 실제 경험으로 채우거나 지웠다
- [ ] `[이미지: …]` 자리에 넣을 사진·캡처를 준비했고 저작권을 확인했다
- [ ] 수치와 출처를 원문에서 다시 확인했다
- [ ] 광고·협찬이면 그 사실을 밝혔다
- [ ] 게시는 사람이 직접 한다(자동 게시 없음)
