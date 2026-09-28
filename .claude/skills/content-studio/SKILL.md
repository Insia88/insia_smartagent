---
name: content-studio
description: 브리프 하나로 사업계획서·네이버 블로그·링크드인·인스타그램 콘텐츠를 같은 핵심 메시지로 한 번에 만든다(원 소스 멀티 유즈). 리서치 위임 → 채널별 작성 → 검수 위임 → 수정(최대 2회)을 돌리고 점수 요약표와 사람 최종 승인 체크리스트를 낸다. 여러 채널 콘텐츠를 동시에 만들거나 채널을 고르지 않았을 때 사용.
argument-hint: "[주제 또는 brief.json 경로] [채널: bizplan,naver_blog,linkedin,instagram]"
---

# 콘텐츠 스튜디오: 원 소스 멀티 유즈

요청: $ARGUMENTS

이 스킬은 메인 스레드에서 총괄 에이전트 역할로 실행한다. 리서치는 `researcher`, 검수는 `reviewer` 서브에이전트에게 Agent 도구로 맡긴다. 결과물은 초안이고, 게시는 사람이 승인한 뒤 직접 한다.

## 시작 전에 읽을 것

- `src/insia_agents/prompts/agents/orchestrator.md`: 작성 원칙, 회사 프로필·사용자 자료 사용 규칙, plan·draft·revise 필드 규칙
- 요청된 채널의 가이드 `src/insia_agents/prompts/channels/<channel>.md` (형식의 단일 기준)
- `src/insia_agents/models.py`: Brief, Profile, UserDocument, ResearchPack, Draft, Review JSON 구조

아래 명령에서 패키지가 설치되지 않았다면 앞에 `PYTHONPATH=src `를 붙인다.

## 절대 규칙

- 사실·수치는 `research.json`에 있는 것만, 출처와 기준시점을 붙여 쓴다. 회사 자신에 대한 사실은 `profile.json`에 적힌 만큼만 쓴다(부풀리지 않음, 어긋나는 내용 금지).
- 프로필의 금지 표현은 쓰지 않고, 필수 문구는 블로그·링크드인·인스타그램에 그대로 넣는다. 사업계획서에는 팀원 실명·학교명·직장명을 쓰지 않는다.
- 통계, 사람, 고객사, 후기, 실적을 지어내지 않는다. 모르는 자리는 `[대표자 성명]`, `[확인 필요: 무엇]`, `○○`.
- 가정은 "가정"이라고 밝힌다. 과장·확정 표현(혁신적, 세계 최초, 보장, 100%)을 쓰지 않는다.
- 어떤 채널에도 자동 게시하지 않는다.

## 1. 브리프 만들기

- `$ARGUMENTS`가 `.json` 경로면 그 파일을 Brief로 읽는다.
- 글이면 `topic`, `goal`, `audience`, `channels`, `tone`, `keywords`(첫 번째가 메인 키워드), `notes`, `language: "ko"`를 채운다. 채널을 말하지 않았으면 네 채널 모두.
- `$ARGUMENTS`가 비어 있거나 주제를 알 수 없을 때만 질문한다. 최대 3개(주제, 목적·독자, 메인 키워드). 그 밖의 빈칸은 합리적으로 채우고 `plan.md`에 "가정한 것"으로 적는다.
- 실행 폴더 `outputs/<YYYY-MM-DD>-<slug>/`를 만들고 `brief.json`을 저장한다. slug는 주제를 영문 소문자·하이픈으로 줄인 것.

## 1-1. 회사 프로필과 사용자 자료

워크스페이스(대시보드 "브랜드·자료")에 저장된 프로필과 자료를 가져온다.

```bash
python -m insia_agents profile show --json > <run>/profile.json
python -m insia_agents docs list --json > <run>/documents.json
```

- 프로필 JSON이 `{"profile": {...}}`로 감싸여 있으면 안쪽만 저장한다. 모든 필드가 비어 있으면 `profile.json`을 지운다(프로필 없음). 자료가 없으면 `documents.json`을 지운다. `text`가 없는 자료는 사용자에게 필요한 부분을 붙여 달라고 하거나 건너뛰고 `plan.md`에 적는다.
- **명령이 실패하면**(미설치, 워크스페이스 없음, 이전 버전) 멈추지 말고 핵심 사실을 최대 3개만 묻는다: ① 회사·서비스 이름과 한 줄 소개 ② 목표 고객 ③ 쓰면 안 되는 표현과 꼭 넣을 문구(없으면 "없음"). 답한 내용만 Profile 필드(`company_name`, `service_name`, `one_liner`, `target_customers`, `banned_words`, `required_phrases`)로 `profile.json`에 저장한다. 사용자가 원하지 않으면 프로필 없이 진행한다.

## 2. 계획 (`plan.md`, `plan.json`)

요약 2~4문장, 모든 채널이 공유할 핵심 메시지 3~5개, 리서치 질문 3~6개(`q1`…: 질문, 쓰임, 채널, 우선순위), 채널별 섹션 순서. 프로필이나 자료로 답이 있는 회사 내부 사실은 질문으로 만들지 않는다. 끝에 Plan JSON 코드 블록을 붙이고 같은 JSON을 `plan.json`에 저장한다.

## 3. 리서치 위임

Agent 도구로 `researcher` 호출. 프롬프트: 실행 폴더 경로, 브리프 요약, 질문 전체(id 포함), "`<run>/research.json`에 ResearchPack JSON으로 저장", 있으면 `profile.json`·`documents.json` 경로와 "사용자 자료는 origin user 출처(`user://<자료 id>`)로 맨 앞에 넣을 것". 돌아오면 검증한다.

```bash
python -c "import sys; from insia_agents.models import ResearchPack as R; p=R.model_validate_json(open(sys.argv[1]).read()); print(len(p.findings),'findings',len(p.sources),'sources (user:',sum(s.origin=='user' for s in p.sources),')',len(p.gaps),'gaps')" <run>/research.json
```

## 4. 채널별 초안 (round 0)

채널마다 가이드를 다시 열고, 같은 핵심 메시지를 채널 독자에 맞게 옮긴다. 사업계획서의 근거·수치가 기준이고, 블로그·링크드인·인스타그램은 그중 채널에 맞는 것만 골라 쓴다. 프로필이 있으면 SNS 마무리에 기본 CTA, 해시태그에 기본 해시태그(개수 한도 안), 본문에 필수 문구를 넣는다. 사용자 자료에서 온 사실은 사업계획서 `[s#]`, 다른 채널 `(자사 자료: 자료 제목)`으로 밝힌다.

- `drafts/<channel>.r0.json`: Draft JSON (`channel`, `round`, `title`, `content`, `hashtags`, `used_finding_ids`, `change_log`)
- `drafts/<channel>.r0.md`: 제목 + 본문 + 해시태그
- 저장 뒤 형식 검사(프로필이 있으면 금지 표현·필수 문구·블라인드 검사 포함). 실패 항목은 검수 전에 고친다.

```bash
python -c "import sys,json,os; from insia_agents.models import Brief,Draft,Profile; from insia_agents.channels import check_format; d=Draft.model_validate_json(open(sys.argv[1]).read()); run=sys.argv[2]; b=Brief.model_validate_json(open(os.path.join(run,'brief.json')).read()); f=os.path.join(run,'profile.json'); pd=json.load(open(f)) if os.path.isfile(f) else None; pd=pd.get('profile',pd) if isinstance(pd,dict) else None; p=Profile.model_validate(pd) if pd else None; [print(('통과' if c.passed else '미충족'), c.label, c.value, '(기준', c.expected+')') for c in check_format(d,b,p)]" <run>/drafts/<channel>.r0.json <run>
# 프로필 없이 빠르게: python -m insia_agents check <run>/drafts/<channel>.r0.json  (가까운 brief.json을 자동으로 찾음)
```

## 5. 검수 위임과 점수 확정

채널마다 Agent 도구로 `reviewer` 호출(채널이 여럿이면 한 번에 병렬로). 프롬프트: 채널 id, round, 초안 JSON 경로, `research.json`·`brief.json` 경로, 있으면 `profile.json` 경로, 채널 가이드 경로. 돌려받은 Review JSON을 `reviews/<channel>.r<N>.json`에 저장하고 코드로 확정한다(프로필 검사 포함).

```bash
python -c "import sys,json,os; from insia_agents.models import Brief,Draft,Review,Profile; from insia_agents.channels import finalize_review; run,dp,rp=sys.argv[1:4]; b=Brief.model_validate_json(open(os.path.join(run,'brief.json')).read()); f=os.path.join(run,'profile.json'); pd=json.load(open(f)) if os.path.isfile(f) else None; pd=pd.get('profile',pd) if isinstance(pd,dict) else None; p=Profile.model_validate(pd) if pd else None; d=Draft.model_validate_json(open(dp).read()); r=finalize_review(Review.model_validate_json(open(rp).read()), d, b, profile=p); open(rp,'w').write(r.model_dump_json(indent=2)); print(r.score, 'PASS' if r.passed else 'FAIL')" <run> <run>/drafts/<channel>.r<N>.json <run>/reviews/<channel>.r<N>.json
```

통과 = 80점 이상이고 critical 이슈 0개.

## 6. 수정 루프 (채널별 최대 2회)

미통과이고 round < 2이면:
1. `needs_research`가 있으면 `researcher`에게 추가 조사(기존 `research.json`에 id를 이어 붙여 전체 저장).
2. critical·major 이슈를 모두 해결, 실패한 형식 검사 수정, `unsupported`는 삭제·수정, 근거 없는 `needs_source`는 삭제하거나 자리표시. 사용자가 대화 중에 수정 지시를 줬다면 가장 먼저 반영하고 `change_log` 첫 줄에 `"[사람 지시] …"`.
3. `round + 1`로 저장, `change_log`에 이슈마다 `"[심각도] 위치: 무엇을 어떻게 고쳤는지"`.
4. 5단계로 돌아간다.

2회 뒤에도 미통과면 멈추고 최고 점수 round를 최종본으로, 미통과로 표시한다.

## 7. 최종 패키지

- `final/<channel>.md`: 최종 round의 제목, 본문, 해시태그
- `final/summary.md`: 요약 표, 모든 출처(`[s#] 발행기관, 제목, 발행일, Tier, URL`, 사용자 자료는 `[s#] 자사 자료, 제목`), 남은 확인 사항(gaps, 자리표시, 미반영 이슈), 사람 최종 승인 체크리스트

## 8. 보관함으로 가져오기

완성된 실행 폴더를 워크스페이스 보관함에 넣는다(로컬 저장이지 게시가 아니다). 대시보드 "보관함"에서 검토·재검수·수정 요청·승인·내보내기를 이어서 할 수 있다.

```bash
python -m insia_agents import-run <run>
```

명령이 없거나 실패하면 오류를 한 줄로 전하고, 나중에 같은 명령으로 가져올 수 있다고 안내한다.

## 9. 사용자에게 보여 줄 것

요약 표와 실행 폴더 경로, 보관함 가져오기 결과, 사람이 채울 것 3가지 이내, 그리고 승인 체크리스트. 본문 전체를 채팅에 다시 붙이지 않는다.

| 채널 | 점수 | 라운드 | 통과 여부 |
|---|---|---|---|
| 사업계획서 | (0~100) | R0~R2 | 통과 / 미통과 |
| 네이버 블로그 | … | … | … |

사람 최종 승인 체크리스트
- [ ] 자리표시(`[대표자 성명]`, `[확인 필요: …]`, `○○`)를 모두 채우거나 지웠다
- [ ] 핵심 수치를 원문 링크에서 다시 확인했다(특히 Tier 2·3)
- [ ] 가격·일정·목표 같은 가정을 실제 계획과 맞췄다
- [ ] 과장·확정 표현, 금지 표현, 타사 비방, 개인정보가 없다
- [ ] 회사 프로필의 사실(가격·실적·팀)과 어긋나는 곳이 없다
- [ ] 사업계획서는 공고 첨부 양식에 옮기고 본인 문장으로 다듬었다
- [ ] 해시태그 한도·글자수 같은 플랫폼 정책을 게시 직전에 다시 확인했다
- [ ] 이미지 저작권과 대체텍스트를 확인했다
- [ ] 게시는 사람이 직접 한다(자동 게시 없음)
