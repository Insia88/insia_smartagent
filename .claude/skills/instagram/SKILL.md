---
name: instagram
description: 인스타그램 캐러셀 원고(7~10장, 장별 문구·비주얼 지시·대체텍스트)와 캡션(첫 줄 125자 이하, 2,200자 이하, 저장·공유 유도, 해시태그 3~5개)을 만든다. 리서치·검수 루프로 사실과 형식을 확인한다. 인스타그램 카드뉴스·캐러셀 게시물을 만들 때 사용.
argument-hint: "[주제 또는 brief.json 경로] [슬라이드 수 7~10(선택)]"
---

# 인스타그램 캐러셀

요청: $ARGUMENTS

메인 스레드에서 총괄 에이전트 역할로 실행하고, 리서치는 `researcher`, 검수는 `reviewer` 서브에이전트에게 Agent 도구로 맡긴다. 게시는 사람이 승인한 뒤 직접 한다.

## 먼저 읽을 것

- `src/insia_agents/prompts/channels/instagram.md`: 플랫폼 기준, 출력 형식, 루브릭, 체크리스트 (단일 기준)
- `src/insia_agents/prompts/agents/orchestrator.md`: 작성 원칙, Draft·수정 규칙
- 공통 절차의 명령어 전문은 `.claude/skills/content-studio/SKILL.md`와 같다.

## 1. 브리프

- `.json` 경로면 그대로 읽고 `channels`를 `["instagram"]`로 둔다.
- 글이면 Brief를 채운다. 슬라이드 수를 말하지 않았으면 8장으로 가정한다.
- 주제를 알 수 없을 때만 질문한다(최대 3개): ① 주제 ② 읽을 사람 ③ 저장해 둘 만한 형태(체크리스트, 단계별 방법, 비교 등).
- `outputs/<YYYY-MM-DD>-<slug>/brief.json` 저장.
- 이어서 `.claude/skills/content-studio/SKILL.md` 1-1단계대로 워크스페이스의 회사 프로필(`profile.json`)과 사용자 자료(`documents.json`)를 가져온다. 명령이 실패하면 핵심 사실 3가지만 묻고 진행한다. 프로필이 있으면 브랜드 색(비주얼 지시), 계정명, 기본 CTA, 기본 해시태그(5개 안), 필수 문구를 쓰고 금지 표현은 쓰지 않는다.

## 2. 계획과 리서치

`plan.md`에 한 줄 약속(1장 훅), 장별 한 메시지 목록, 리서치 질문 3~4개(Plan 규칙 3~6개 안에서)를 쓴다. 이미지에 올릴 수치는 1~2개로 좁히고 공식 출처를 찾게 한다. `researcher`에게 `<run>/research.json` 저장을 맡기고 검증한다.

## 3. 초안 (round 0) 작성 포인트

- `content`의 최상위 섹션은 `## 캐러셀`과 `## 캡션` 두 개뿐. `## 캡션` 줄에는 다른 글자를 붙이지 않는다.
- 슬라이드 7~10장, 각 장은 줄 맨 앞 `### 슬라이드 N — 제목` + `- 문구:`(25어절 이하) + `- 비주얼:` + `- 대체텍스트:`. 수치가 있는 장은 `- 출처:`.
- 흐름: 1장 훅 → 2장 문제 → 단계·팁(장당 하나) → 마지막 장 요약 + 저장 유도. 레이아웃 체계를 모든 장에 통일해 지시한다.
- 캡션: 첫 줄 125자 이하 훅, 전체 2,200자 이하, 끝에서 두 번째 줄에 저장·공유·댓글 부탁, 마지막 줄 = `hashtags`와 같은 3~5개 태그.
- 해시태그는 게시물당 최대 5개(인스타그램 2025년 12월 발표 기준). 캡션에 링크를 넣지 않는다("프로필 링크"로 안내).
- INSIA 서비스 자체 홍보라면 3D 카피바라 마스코트(유자 디렉터·돋보기 탐험가·꼼꼼 검수관)를 비주얼에 써도 좋다.
- `title`은 내부 관리용(1장 문구 요약).

`drafts/instagram.r0.json`과 `.md` 저장 후 형식 검사:

```bash
python -m insia_agents check <run>/drafts/instagram.r0.json
# 설치 전이면: PYTHONPATH=src python -m insia_agents check <run>/drafts/instagram.r0.json
```

`check`는 가까운 `brief.json`을 자동으로 찾는다(또는 `--brief <run>/brief.json`). `profile.json`이 있으면 금지 표현·필수 문구·블라인드 검사까지 도는 content-studio 4단계의 프로필 포함 명령을 쓴다.

## 4. 검수와 수정 (최대 2회)

1. `reviewer`에게 채널 `instagram`, round, 초안·`research.json`·`brief.json`·(있으면) `profile.json`·가이드 경로를 넘긴다. Review JSON을 `reviews/instagram.r<N>.json`에 저장하고 `finalize_review`로 확정한다(content-studio 5단계 명령).
2. 통과 = 80점 이상 + critical 0개. 미통과면 critical·major를 모두 고치고 `round + 1`로 저장, `change_log`에 이슈별 한 줄.
3. 2회 뒤에도 미통과면 최고 점수 round를 최종본으로, 미통과로 표시한다.

## 5. 최종 패키지와 보고

- `final/instagram.md`(캐러셀 원고 + 캡션 + 태그), `final/summary.md`(요약 표, 모든 출처, 확인 사항, 승인 체크리스트).
- `python -m insia_agents import-run <run>`으로 워크스페이스 보관함에 가져온다(로컬 저장, 게시 아님). 실패하면 오류를 한 줄로 전하고 나중에 같은 명령으로 가져올 수 있다고 안내한다.
- 사용자에게: `| 채널 | 점수 | 라운드 | 통과 여부 |` 표, 실행 폴더 경로, 보관함 가져오기 결과, 채울 것 3가지 이내, 승인 체크리스트.

사람 최종 승인 체크리스트 (인스타그램)
- [ ] 장별 이미지를 비주얼 지시대로 만들었고 글자가 잘리지 않는다
- [ ] 대체텍스트를 업로드 화면의 고급 설정에 넣었다
- [ ] 이미지 속 수치와 출처 표기를 원문에서 다시 확인했다
- [ ] 해시태그가 5개 이하다
- [ ] 게시는 사람이 직접 한다(자동 게시 없음)
