---
name: linkedin
description: 링크드인 게시물을 첫 두 줄 훅(210자 이하), 한두 문장 문단, 데이터·경험 기반 관점, 댓글을 부르는 질문, 해시태그 3~5개 구조로 만든다. 본문 링크 없이 1,300~2,000자로 쓰고 리서치·검수 루프로 완성한다. 링크드인 포스트를 쓸 때 사용.
argument-hint: "[주제 또는 brief.json 경로] [전하고 싶은 관점(선택)]"
---

# 링크드인

요청: $ARGUMENTS

메인 스레드에서 총괄 에이전트 역할로 실행하고, 리서치는 `researcher`, 검수는 `reviewer` 서브에이전트에게 Agent 도구로 맡긴다. 게시는 사람이 승인한 뒤 직접 한다.

## 먼저 읽을 것

- `src/insia_agents/prompts/channels/linkedin.md`: 플랫폼 기준, 출력 형식, 루브릭, 체크리스트 (단일 기준)
- `src/insia_agents/prompts/agents/orchestrator.md`: 작성 원칙, Draft·수정 규칙
- 공통 절차의 명령어 전문은 `.claude/skills/content-studio/SKILL.md`와 같다.

## 1. 브리프

- `.json` 경로면 그대로 읽고 `channels`를 `["linkedin"]`로 둔다.
- 글이면 Brief를 채운다. `notes`에 사용자가 말한 경험·관점을 그대로 옮긴다(글의 1인칭 경험은 여기 있는 것만 쓴다).
- 주제를 알 수 없을 때만 질문한다(최대 3개): ① 주제 ② 전하고 싶은 관점이나 직접 겪은 일 ③ 읽을 사람(창업자, 채용 후보, 고객사 등).
- `outputs/<YYYY-MM-DD>-<slug>/brief.json` 저장.

## 2. 계획과 리서치

`plan.md`에 한 문장 관점, 훅 후보 2~3개, 관점을 받칠 근거 질문 3~4개(Plan 규칙 3~6개 안에서)를 쓴다. 링크드인은 수치 1~2개면 충분하니 가장 설득력 있는 공식 통계를 찾게 한다. `researcher`에게 `<run>/research.json` 저장을 맡기고 검증한다.

## 3. 초안 (round 0) 작성 포인트

- 일반 텍스트만. 마크다운 제목·굵게·표 없이, 목록은 `•` 또는 `- `.
- 첫 두 줄(빈 줄 제외) 합계 210자 이하, 첫 줄은 140자 이하 권장(모바일).
- 전체 공백 포함 1,300~2,000자(줄바꿈, 해시태그 줄 포함).
- 합니다체, 문단 1~2문장. 흐름: 훅 → 맥락 → 관점 3개 안팎 → 근거 수치(기관·기준시점) → 질문.
- 본문에 URL 금지. 링크가 필요하면 `change_log`에 `"첫 댓글 링크: <URL>"`.
- 마지막 줄 = `hashtags`와 같은 3~5개 태그를 같은 순서로.
- `title`은 게시되지 않는 내부 관리용 한 줄.

`drafts/linkedin.r0.json`과 `.md` 저장 후 형식 검사:

```bash
python -m insia_agents check <run>/drafts/linkedin.r0.json
# 설치 전이면: PYTHONPATH=src python -m insia_agents check <run>/drafts/linkedin.r0.json
```

## 4. 검수와 수정 (최대 2회)

1. `reviewer`에게 채널 `linkedin`, round, 초안·`research.json`·`brief.json`·가이드 경로를 넘긴다. Review JSON을 `reviews/linkedin.r<N>.json`에 저장하고 `finalize_review`로 확정한다(content-studio 5단계 명령).
2. 통과 = 80점 이상 + critical 0개. 미통과면 critical·major를 모두 고치고 `round + 1`로 저장, `change_log`에 이슈별 한 줄.
3. 2회 뒤에도 미통과면 최고 점수 round를 최종본으로, 미통과로 표시한다.

## 5. 최종 패키지와 보고

- `final/linkedin.md`(본문 + 해시태그, 첫 댓글 링크 메모), `final/summary.md`(요약 표, 모든 출처, 확인 사항, 승인 체크리스트).
- 사용자에게: `| 채널 | 점수 | 라운드 | 통과 여부 |` 표, 실행 폴더 경로, 채울 것 3가지 이내, 승인 체크리스트.

사람 최종 승인 체크리스트 (링크드인)
- [ ] 1인칭 경험이 실제 있었던 일이다
- [ ] 수치와 기관·기준시점을 원문에서 다시 확인했다
- [ ] 링크는 게시 직후 첫 댓글에 단다
- [ ] 다른 사람·회사를 언급했다면 동의·사실관계를 확인했다
- [ ] 게시는 사람이 직접 한다(자동 게시 없음)
