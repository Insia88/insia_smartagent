"""Offline backend: replays the recorded sample run or builds ``[데모]`` templates.

- If the brief's topic matches ``examples/sample-run/brief.json``, the recorded
  run is replayed: ``plan.json``, ``research.json``,
  ``drafts/<channel>.r<N>.json``, ``reviews/<channel>.r<N>.json`` and optional
  ``followups/<channel>.r<N>.json`` (a ResearchPack with follow-up findings) and
  ``meta.json`` (``{"model": ...}``). A missing round reuses the last available.
- Otherwise it generates clearly labelled ``[데모]`` content from the brief. It
  never invents concrete statistics (numbers are ``○○`` placeholders) and it
  reproduces a realistic review loop: some first drafts fail the channel's
  format or rubric, the revision fixes them.

Durations are simulated (``sim_seconds``); the pipeline turns them into
virtual time, so mock traces have realistic ``t`` values even at speed 0.

Run context (``backend.context``) in template mode: profile values appear in
the drafts (service name, target customers, facts marked "(자사 자료)", team
roles without names in the business plan, CTA / required phrases / default
hashtags in SNS posts; banned words are removed in revisions), and user
documents become ``origin="user"`` sources with ``[데모]`` findings (first
sentence of each document). The recorded replay ignores the context and says
so. Every call reports synthetic usage (model ``mock``, cost 0) to
``on_usage``. ``plan_calendar`` is deterministic.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..channels import CHANNELS, FORMAT_ITEM_ID, chars_no_space, chars_with_space, first_lines
from ..config import Settings
from ..costs import mock_usage
from ..models import (
    ALL_CHANNELS,
    Brief,
    ChannelId,
    ChannelOutline,
    ContentItem,
    ContentPlan,
    Draft,
    FactCheck,
    Finding,
    FormatCheck,
    Plan,
    PlannedSlot,
    Profile,
    ResearchPack,
    ResearchQuestion,
    Review,
    ReviewIssue,
    RubricScore,
    Source,
)
from ..planner import (DEFAULT_GOALS, cap_counts, history_keys, normalize_counts, normalize_plan, repeats_history, slot_days,
                       spread, topic_key)
from ..prompt_loader import DocumentExcerpt, budget_documents, profile_is_empty, user_sources
from .base import BackendError, EmitFn, NoticeFn, RunContext, UsageFn

TEMPLATE_MODEL = "mock-template"

SIM_SECONDS: dict[str, float] = {
    "plan": 9.0,
    "handoff": 0.5,
    "research_start": 1.2,
    "research_query": 2.6,
    "research_source": 0.9,
    "research_finding": 1.3,
    "research_structure": 4.0,
    "review_start": 1.0,
}
DRAFT_SECONDS: dict[str, float] = {"bizplan": 42.0, "naver_blog": 28.0, "linkedin": 19.0, "instagram": 24.0}
REVIEW_SECONDS: dict[str, float] = {"bizplan": 18.0, "naver_blog": 14.0, "linkedin": 11.0, "instagram": 13.0}


def sim_seconds(kind: str, channel: str | None = None, round: int = 0) -> float:
    """Deterministic simulated duration for one step of the pipeline."""
    if kind == "draft":
        return DRAFT_SECONDS.get(channel or "", 20.0)
    if kind == "revise":
        return round_1(DRAFT_SECONDS.get(channel or "", 20.0) * 0.7 + 1.5 * round)
    if kind == "review":
        return REVIEW_SECONDS.get(channel or "", 12.0) + 1.5 * round
    return SIM_SECONDS.get(kind, 1.0)


def round_1(value: float) -> float:
    return round(value, 1)


# ---------------------------------------------------------------------------
# Korean text helpers
# ---------------------------------------------------------------------------


def _has_batchim(word: str) -> bool:
    word = word.strip()
    if not word:
        return False
    ch = word[-1]
    if "가" <= ch <= "힣":
        return (ord(ch) - 0xAC00) % 28 != 0
    if ch.isdigit():
        return ch in "013678"
    if ch.isalpha():
        return ch.lower() in "lmnr"
    return False


def josa(word: str, pair: str) -> str:
    """``josa("서비스", "은/는")`` → ``"서비스는"``."""
    with_batchim, without = pair.split("/")
    return word + (with_batchim if _has_batchim(word) else without)


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def short_name(topic: str) -> str:
    """A product/topic name short enough to repeat in templates."""
    quoted = re.findall(r"[\'\"‘’“”「『]([^\'\"‘’“”」』]{2,30})[\'\"‘’“”」』]", topic)
    if quoted:
        return quoted[-1].strip()
    return _clip(topic, 24)


def hashtag(text: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z가-힣_]", "", text)
    return f"#{cleaned}" if cleaned else ""


def make_tags(brief: Brief, count: int, extra: list[str], first: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Up to ``count`` hashtags: ``first`` (e.g. the profile's default
    hashtags), then the brief keywords, then ``extra``."""
    tags: list[str] = []
    for word in [*first, *brief.keywords, *extra]:
        tag = hashtag(word)
        if tag and tag not in tags and len(tag) <= 20:
            tags.append(tag)
        if len(tags) == count:
            break
    return tags


def _fit(core: list[str], optional: list[str], measure, lo: int, hi: int, sep: str = "\n\n") -> str:
    """Join paragraphs, adding optional ones until ``lo`` is reached without passing ``hi``."""
    parts = list(core)
    text = sep.join(parts)
    for para in optional:
        if measure(text) >= lo:
            break
        candidate = sep.join([*parts, para])
        if measure(candidate) > hi:
            continue
        parts.append(para)
        text = candidate
    return text


OPTIONAL_MARK = "<<OPTIONAL>>"


def _fill(template: str, optional: list[str], measure, lo: int, hi: int) -> str:
    """Replace ``OPTIONAL_MARK`` with as many optional paragraphs as needed to reach ``lo``."""
    chosen: list[str] = []

    def render(parts: list[str]) -> str:
        return template.replace(OPTIONAL_MARK, "".join(p + "\n\n" for p in parts))

    for para in optional:
        if measure(render(chosen)) >= lo:
            break
        if measure(render([*chosen, para])) <= hi:
            chosen.append(para)
    return render(chosen)


class _Ctx(dict):
    """format_map helper: missing keys stay visible instead of raising."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def brief_context(brief: Brief, profile: Profile | None = None) -> dict[str, str]:
    """Template variables. A company profile (when given) supplies the service
    name and the target customers."""
    service = _clip(profile.service_name, 24) if profile is not None and profile.service_name.strip() else ""
    customers = profile.target_customers.strip() if profile is not None else ""
    name = service or short_name(brief.topic)
    audience = _clip(customers or brief.audience or "1인 창업자와 소상공인", 30)
    kw0 = brief.keywords[0].strip() if brief.keywords else name
    kw1 = brief.keywords[1].strip() if len(brief.keywords) > 1 else "업무 자동화"
    goal = _clip(brief.goal or f"{name} 소개와 사업화 준비", 60)
    return {
        "name": name,
        "name_eun": josa(name, "은/는"),
        "name_i": josa(name, "이/가"),
        "name_eul": josa(name, "을/를"),
        "name_wa": josa(name, "과/와"),
        "audience": audience,
        "audience_eun": josa(audience, "은/는"),
        "audience_i": josa(audience, "이/가"),
        "kw0": kw0,
        "kw0_eul": josa(kw0, "을/를"),
        "kw1": kw1,
        "goal": goal,
        "topic": _clip(brief.topic, 80),
    }


# ---------------------------------------------------------------------------
# Template research
# ---------------------------------------------------------------------------

_TEMPLATE_SOURCES = [
    ("[데모] KOSIS 국가통계포털 — 관련 통계표 (원문 확인 필요)", "https://kosis.kr", "통계청", 1),
    ("[데모] 중소벤처기업부 — 정책·실태조사 자료 (원문 확인 필요)", "https://www.mss.go.kr", "중소벤처기업부", 1),
    ("[데모] 소상공인시장진흥공단 — 소상공인 지원 자료 (원문 확인 필요)", "https://www.semas.or.kr", "소상공인시장진흥공단", 1),
    ("[데모] 정보통신정책연구원 — 디지털 활용 연구 (원문 확인 필요)", "https://www.kisdi.re.kr", "정보통신정책연구원", 2),
]
_FOLLOWUP_SOURCE = ("[데모] K-Startup 창업지원포털 — 지원사업 공고 (원문 확인 필요)", "https://www.k-startup.go.kr", "창업진흥원", 1)


def _question_specs(ctx: dict[str, str]) -> list[tuple[str, str, list[ChannelId], str]]:
    return [
        (f"{ctx['audience']} 규모와 최근 추이는 어느 정도인가(최신 기준연도)", "사업계획서 문제인식·시장 규모, 블로그 도입부 근거",
         ["bizplan", "naver_blog", "linkedin"], "high"),
        (f"{ctx['audience_i']} {ctx['kw0']} 관련 업무에서 겪는 어려움을 보여 주는 공식 조사 결과", "모든 채널의 문제 제기 근거",
         ["bizplan", "naver_blog", "linkedin", "instagram"], "high"),
        (f"{ctx['kw0']} 관련 경쟁·대체 서비스와 가격대", "사업계획서 차별성·비즈니스 모델, 링크드인 관점",
         ["bizplan", "linkedin"], "medium"),
        (f"{ctx['audience']} 대상 정부 지원사업·정책 현황", "사업계획서 성장전략, 인스타그램 정보 슬라이드",
         ["bizplan", "instagram", "naver_blog"], "medium"),
    ]


_FINDING_CLAIMS = [
    "[데모] {audience} 수는 ○○만 곳으로 집계됨 (기준: ○○년)",
    "[데모] {audience} 중 ○○%가 {kw0} 관련 업무를 가장 큰 어려움으로 꼽음 (기준: ○○년 조사)",
    "[데모] {kw0} 관련 유료 서비스의 월 이용료는 ○○원~○○원 수준으로 확인됨 (기준: ○○년)",
    "[데모] {audience} 대상 디지털 전환 지원사업 예산은 ○○억 원 규모임 (기준: ○○년)",
]


def template_plan(brief: Brief, profile: Profile | None = None) -> Plan:
    ctx = brief_context(brief, profile)
    wanted = list(dict.fromkeys(brief.channels))
    questions = []
    for i, (question, why, channels, priority) in enumerate(_question_specs(ctx), start=1):
        chans = [c for c in channels if c in wanted] or list(wanted)
        questions.append(ResearchQuestion(id=f"q{i}", question=question, why=why, channels=chans, priority=priority))  # type: ignore[arg-type]
    sections = {
        "bizplan": ["일반현황", "창업 아이템 개요(요약)", "1. 문제 인식 (Problem)", "2. 실현 가능성 (Solution)",
                    "3. 성장전략 (Scale-up)", "4. 팀 구성 (Team)", "참고자료"],
        "naver_blog": ["도입부", f"{ctx['kw0']}, 왜 지금 필요할까요", "혼자서도 가능한 3단계", "직접 써 보며 확인한 점", "정리하면"],
        "linkedin": ["훅 2줄", "맥락", "관점 3가지", "근거", "질문", "해시태그"],
        "instagram": ["표지", "문제 공감", "원인", "해결 1", "해결 2", "해결 3", "체크리스트", "저장 유도", "캡션"],
    }
    return Plan(
        summary=(f"[데모] {ctx['name_i']} 해결하려는 문제를 공식 통계로 먼저 확인하고, 같은 핵심 메시지를 채널 형식에 맞게 풀어 씁니다. "
                 "mock 모드라 수치는 모두 ○○ 자리표시이며, live 모드에서는 웹 리서치로 실제 값을 채웁니다."),
        key_messages=[
            f"{ctx['audience_eun']} {ctx['kw0']} 업무를 혼자 감당하느라 시간이 부족하다",
            f"{ctx['name_eun']} 반복 작업을 줄이고 사람이 판단할 일에 집중하게 돕는다",
            "모든 수치는 출처와 기준 시점을 밝히고, 최종 게시는 사람이 승인한다",
            "가격·목표 수치처럼 근거 없이 정한 값은 가정으로 표시한다",
        ],
        questions=questions,
        outlines=[ChannelOutline(channel=c, sections=sections[c]) for c in wanted],
    )


def template_research(brief: Brief, today: str, profile: Profile | None = None) -> ResearchPack:
    ctx = _Ctx(brief_context(brief, profile))
    sources = [Source(id=f"s{i}", title=t, url=u, publisher=p, published="", tier=tier, accessed=today)  # type: ignore[arg-type]
               for i, (t, u, p, tier) in enumerate(_TEMPLATE_SOURCES, start=1)]
    source_for = [["s1"], ["s2", "s4"], ["s4"], ["s2", "s3"]]
    findings = [
        Finding(id=f"f{i}", question_id=f"q{i}", claim=claim.format_map(ctx), source_ids=source_for[i - 1], confidence="low",
                note="[데모] 자리표시 값 — live 모드에서 원문 수치와 기준 시점으로 바뀜")
        for i, claim in enumerate(_FINDING_CLAIMS, start=1)
    ]
    return ResearchPack(
        findings=findings,
        sources=sources,
        gaps=["[데모] 모든 수치는 ○○ 자리표시예요. live 모드에서 웹 검색으로 실제 값과 기준 시점을 채워요."],
    )


def first_sentence(text: str, limit: int = 90) -> str:
    """First sentence (or line) of a document, whitespace collapsed."""
    flat = re.sub(r"\s+", " ", re.sub(r"^[#>*\-\s]+", "", text.strip(), flags=re.MULTILINE)).strip()
    match = re.search(r"(.+?[.!?。]|.+?(?:다|요|임|함)\.?)(\s|$)", flat)
    return _clip(match.group(1) if match else flat, limit)


def _best_question(text: str, questions: list[ResearchQuestion]) -> str:
    if not questions:
        return "q1"
    from .anthropic_backend import match_question  # pure helper; importing it does not load the SDK

    return match_question(text, questions) or questions[0].id


def template_user_research(excerpts: list[DocumentExcerpt], questions: list[ResearchQuestion],
                           start_source: int, start_finding: int, today: str) -> ResearchPack:
    """User documents as ``origin="user"`` sources plus one ``[데모]`` finding
    each (the document's first sentence — mock mode reads no further)."""
    sources = user_sources(excerpts, start_source, today)
    findings = []
    for i, (excerpt, source) in enumerate(zip(excerpts, sources)):
        sentence = first_sentence(excerpt.text)
        findings.append(Finding(
            id=f"f{start_finding + i}", question_id=_best_question(f"{source.title} {sentence}", questions),
            claim=f"[데모] 「{source.title}」(사용자 제공 자료)에 적힌 내용: {sentence}", source_ids=[source.id],
            confidence="medium", note="사용자 제공 자료(외부 검증 전) — [데모] mock 모드는 자료의 첫 문장만 옮겨요",
        ))
    return ResearchPack(findings=findings, sources=sources, gaps=[])


def template_followup(questions: list[ResearchQuestion], existing: ResearchPack | None, today: str) -> ResearchPack:
    title, url, publisher, tier = _FOLLOWUP_SOURCE
    source = Source(id="s1", title=title, url=url, publisher=publisher, published="", tier=tier, accessed=today)  # type: ignore[arg-type]
    findings = [
        Finding(id=f"f{i}", question_id=q.id, claim=f"[데모] {_clip(q.question, 60)} — 확인된 값은 ○○ (기준: ○○년)",
                source_ids=["s1"], confidence="low", note="[데모] 추가 조사 자리표시")
        for i, q in enumerate(questions, start=1)
    ]
    return ResearchPack(findings=findings, sources=[source], gaps=[])


# ---------------------------------------------------------------------------
# Template drafts
# ---------------------------------------------------------------------------

_BIZ_HEAD = """# {name} 사업계획서 (초안)

> [데모] mock 모드 템플릿으로 만든 예시 초안임. ○○ 자리표시 수치와 [s#] 출처는 live 모드의 웹 리서치로 채워야 하며, 제출 전 사람이 모든 내용을 확인해야 함.

## 일반현황

| 항목 | 내용 |
|---|---|
| 창업 아이템명 | {name} |
{company_row}| 대표자 | [대표자 성명] |
| 목표 고객 | {audience} |
| 신청 목적 | {goal} |
| 사업 형태 | {biz_model} |

## 창업 아이템 개요(요약)

- {name_eun} {audience_i} {kw0} 업무에 쓰는 시간을 줄이도록 돕는 서비스임
- 자료 조사, 초안 작성, 검수를 단계별로 나누고 각 단계 결과를 사람이 확인한 뒤 다음 단계로 넘기는 구조임
- 핵심 가치는 반복 작업 시간 절감, 근거 있는 콘텐츠, 사람의 최종 승인 세 가지임
- 1차 목표는 초기 고객 ○○곳과 함께 실제 업무 흐름에서 효과를 검증하는 것임 (목표값은 가정)

| 구분 | 내용 |
|---|---|
| 해결할 문제 | {kw0} 관련 반복 작업에 드는 시간과 근거 확인 부담 |
| 해결 방법 | 조사·작성·검수를 나눠 맡는 단계형 작업 흐름과 사람의 최종 승인 |
| 주요 고객 | {audience} |
| 기대 효과 | 작업 시간 단축, 근거 있는 결과물, 일정한 품질 유지 (효과 수치는 시범 운영으로 확인 예정) |

### 주요 기능

- 브리프 한 장으로 작업 계획과 조사 질문을 자동으로 정리하는 기능
- 공개 자료를 조사해 출처 등급과 기준 시점을 붙인 근거 목록을 만드는 기능
- 채널별 형식 규칙에 맞춘 초안 작성과 규칙 위반 항목 자동 표시 기능
- 검수 점수와 수정 요청, 수정 이력을 한 화면에서 확인하는 기능
- 최종본을 복사하거나 파일로 저장해 바로 활용하는 기능

{profile_facts}## 1. 문제 인식 (Problem)

### 1-1. 창업 배경 및 필요성

- 국내 {audience} 수는 ○○만 곳 규모로 집계됨 (기준: ○○년) [s1]
- 이들 가운데 상당수가 {kw0}을(를) 포함한 운영 업무를 대표 한 명이 직접 맡고 있음 [s2]
- 조사 결과 {kw0} 관련 업무를 가장 큰 어려움으로 꼽은 비율은 ○○%임 (기준: ○○년 조사) [s2]
- 전담 인력을 두기 어려운 구조라 업무가 밀리면 고객 응대와 판매 준비에 쓸 시간이 줄어듦
- 디지털 도구는 늘었지만 도구마다 쓰는 법이 달라 오히려 관리할 일이 늘었다는 의견이 많음 [s4]
- 업무가 몰리는 시기에는 홍보·기록·문서 작업이 가장 먼저 미뤄지고, 이는 신규 고객 유입 감소로 이어질 수 있음

### 1-2. 목표 고객이 겪는 문제

- 매번 자료를 새로 찾고 정리하는 데 시간이 많이 듦
- 채널마다 형식과 분량 규칙이 달라 같은 내용을 여러 번 다시 써야 함
- 수치나 사실을 확인할 방법이 마땅치 않아 근거 없는 표현이 섞이기 쉬움
- 결과물의 품질을 점검해 줄 사람이 없어 실수를 게시 후에야 발견함
- 지원사업 신청서처럼 정해진 양식이 있는 문서는 작성 요령을 몰라 마감 직전에 몰아서 쓰게 됨
- 도구를 새로 익힐 시간이 부족해 결국 익숙한 방식으로 되돌아가는 경우가 많음

### 1-3. 기존 대안의 한계

- 범용 생성형 AI 도구는 초안은 빠르지만 출처 확인과 형식 점검을 사용자가 직접 해야 함
- 외주 대행은 품질은 안정적이나 월 비용 부담이 커 초기 사업자에게 맞지 않음 [s3]
- 무료 템플릿은 업종과 목적에 맞게 고치는 데 다시 시간이 듦
- 교육 프로그램은 도움이 되지만 교육 이후 실제 업무에 적용하는 단계는 여전히 혼자 해야 함

### 1-4. 해결하려는 핵심 과제

- 자료 조사부터 초안, 검수까지 이어지는 반복 작업 시간을 줄이는 것
- 근거가 있는 내용만 결과물에 들어가도록 출처 확인 과정을 기본 흐름에 넣는 것
- 사용자가 최종 판단에만 집중할 수 있도록 확인할 항목을 한눈에 보여 주는 것

## 2. 실현 가능성 (Solution)

### 2-1. 서비스 구조

- 리서치 단계: 공식 통계와 공공기관 자료를 우선 찾아 출처 등급을 붙여 정리함
  - 출처는 공공·공식 자료, 언론·연구기관 자료, 기타 자료의 세 등급으로 구분함
  - 찾지 못한 내용은 빈칸으로 남기고 추가 확인 목록에 올림
- 작성 단계: 정리된 근거만 사용해 채널별 형식에 맞춘 초안을 만듦
  - 사업계획서, 블로그, SNS 게시물마다 분량·구성 규칙을 따로 적용함
- 검수 단계: 루브릭 채점, 사실 확인, 형식 검사를 거쳐 수정 요청을 돌려보냄
  - 기준 점수에 못 미치면 최대 두 번까지 수정한 뒤 가장 좋은 버전을 제시함
- 승인 단계: 사람이 최종본을 확인하고 게시 여부를 결정함

### 2-2. 개발·사업화 방안

- 1단계(○개월): 핵심 흐름(조사→작성→검수) 시제품 개발, 내부 테스트 진행 예정
- 2단계(○개월): 초기 고객 ○○곳 대상 시범 운영, 사용 로그로 개선점 도출 예정
- 3단계(○개월): 결제·계정 기능을 붙여 유료 전환 시작 예정
- 단계마다 사용자 인터뷰를 진행해 다음 단계의 우선순위를 정할 예정임

### 2-3. 차별성

- 모든 수치에 출처와 기준 시점을 붙이고, 근거가 없으면 자리표시로 남겨 허위 정보를 줄임
- 채널별 형식 규칙을 코드로 검사해 같은 초안은 항상 같은 형식 점수를 받음
- 검수 결과를 근거로 수정 이력을 남겨 사람이 변경 내용을 한눈에 확인할 수 있음
- 작업 과정을 화면에 실시간으로 보여 주어 사용자가 어느 단계에서 무엇이 바뀌었는지 이해할 수 있음

### 2-4. 품질 관리와 개인정보 보호

- 실존 인물의 이름·연락처, 확인되지 않은 후기와 실적은 결과물에 넣지 않도록 검수 기준에 포함함
- 과장·확정 표현(최초, 유일, 보장 등)은 검수 단계에서 자동으로 지적하도록 설계함
- 사용자가 입력한 사업 정보는 해당 작업에만 사용하고, 보관 기간과 삭제 방법을 이용약관에 명시할 예정임

### 2-5. 시제품 검증 계획

- 검증 지표: 결과물 한 건을 완성하는 데 걸린 시간, 수정 횟수, 사용자 만족도
- 검증 방법: 시범 운영 참여자의 작업 전후 시간을 기록해 비교할 예정임
- 판단 기준: 목표 지표는 시범 운영 전에 가정값으로 정하고, 결과에 따라 조정할 예정임

### 2-6. 운영 방식

- 사용자는 주제·목적·대상 독자·채널만 입력하면 되고, 나머지 단계는 서비스가 순서대로 진행함
- 각 단계의 결과와 판단 근거를 화면에 남겨 사용자가 언제든 중간 결과를 확인하고 수정할 수 있음
- 사용자가 승인하지 않은 결과물은 외부 채널에 게시하지 않는 것을 기본 원칙으로 함
- 이용 중 접수된 문의와 개선 요청은 주 단위로 모아 다음 업데이트 우선순위에 반영할 예정임
"""

_BIZ_SCALE_R0 = """
## 3. 성장전략 (Scale-up)

### 3-1. 사업화 추진 전략

- 초기에는 {audience} 커뮤니티와 지원기관 교육 프로그램을 통해 체험 사용자를 모을 예정임
- 체험 사용자의 작업 시간 변화와 만족도를 기록해 유료 전환의 근거로 삼을 예정임
- 이후 업종별 템플릿을 늘려 적용 범위를 넓힐 계획임

### 3-2. 추진 일정

- 시제품 개발, 시범 운영, 정식 출시 순으로 진행할 예정이며 구체 일정은 확정 후 표로 제시할 예정임

### 3-3. 자금 조달

- 정부 창업지원사업과 초기 매출로 개발 자금을 마련할 계획임
"""

_BIZ_SCALE_R1 = """
## 3. 성장전략 (Scale-up)

### 3-1. 시장 규모 (TAM·SAM·SOM)

| 구분 | 정의 | 규모 | 근거 |
|---|---|---|---|
| TAM | 국내 {audience} 전체의 {kw0} 관련 지출 | ○○억 원 (가정) | [s1] 사업체 수 × 월 이용료 가정 |
| SAM | 이 중 디지털 도구를 이미 쓰는 사업자 | ○○억 원 (가정) | [s2] 활용 비율 적용 |
| SOM | 3년 안에 확보할 목표 고객 ○○곳 | ○○억 원 (가정) | 월 구독료 가정 × 목표 고객 수 |

- 모든 규모 값은 원문 수치 확인 후 다시 계산할 예정임. 계산식은 참고자료의 [s#] 값을 기준으로 함

### 3-2. 비즈니스 모델

- 월 구독형 요금제(베이직/프로)를 가정함. 가격은 경쟁·대체 서비스의 월 이용료 범위를 참고해 정할 예정임 [s3]
- 베이직은 채널 1~2개, 프로는 채널 4개와 검수 이력 보관을 제공하는 구성을 가정함
- 지원기관·교육기관과의 단체 이용 계약을 두 번째 수익원으로 검토 중임

### 3-3. 사업화 추진 전략

- 초기에는 {audience} 커뮤니티와 지원기관 교육 프로그램을 통해 체험 사용자를 모을 예정임
- 체험 사용자의 작업 시간 변화와 만족도를 기록해 유료 전환의 근거로 삼을 예정임
- 관련 정부 지원사업과 연계해 초기 고객 접점을 넓힐 예정임 [s5]

### 3-4. 추진 일정

| 단계 | 기간 | 주요 내용 | 산출물 |
|---|---|---|---|
| 1단계 | ○개월 | 핵심 흐름 시제품 개발 | 시제품, 내부 테스트 결과 |
| 2단계 | ○개월 | 초기 고객 ○○곳 시범 운영 | 사용 로그, 개선 목록 |
| 3단계 | ○개월 | 결제 기능 추가, 정식 출시 | 유료 전환 지표 |

### 3-5. 사업비 집행 계획

| 비목 | 산출 근거 | 금액 |
|---|---|---|
| 인건비 | 개발 인력 ○명 × ○개월 (가정) | ○○○만 원 |
| 외주용역비 | 디자인·보안 점검 (가정) | ○○○만 원 |
| 지급수수료 | API 이용료·클라우드 비용 (가정) | ○○○만 원 |
| 마케팅비 | 체험 사용자 모집 (가정) | ○○○만 원 |
| 합계 | | ○○○○만 원 |
"""

_BIZ_TEAM = """
## 4. 팀 구성 (Team)

| 구분 | 성명 | 담당 업무 | 보유 역량 |
|---|---|---|---|
{team_rows}

- 보유 인프라: [보유 장비·공간 기재]
- 협력 기관: [협력 예정 기관명 — 확인 필요]

### 팀 역량 보완 계획

- 대표자는 고객 인터뷰와 시범 운영을 직접 맡아 현장의 요구를 제품에 반영할 예정임
- 개발 인력은 핵심 흐름 구현과 데이터 보안을 담당하고, 부족한 디자인 역량은 외부 협력으로 보완할 예정임
- 콘텐츠 품질 관리 인력은 시범 운영 결과를 보고 채용 시점을 정할 예정임

## 참고자료

{references}
"""

_BLOG_INTRO = """{kw0}, 막상 시작하려면 어디서부터 손대야 할지 막막하죠. 이 글은 {audience_i} 혼자서도 {kw0_eul} 부담 없이 시작하는 방법을 순서대로 정리한 [데모] 예시 글이에요.

결론부터 말하면 처음부터 모든 걸 자동화하려고 하기보다, 반복되는 일 하나를 골라 줄이는 것부터 시작하는 게 좋아요. 아래에서 왜 그런지, 어떻게 하면 되는지 차근차근 볼게요.

이 글에서 다루는 내용은 세 가지예요. 지금 왜 필요한지, 혼자서 어떤 순서로 시작하면 되는지, 그리고 직접 해 보며 조심해야 할 점이에요. 끝까지 읽고 나면 이번 주에 바로 해 볼 일 하나가 정해질 거예요."""

_BLOG_R0_BODY = """[이미지: 노트북 앞에서 할 일 목록을 정리하는 1인 창업자]

## {kw0}, 왜 지금 필요할까요

혼자 사업을 하면 제품 준비, 고객 응대, 홍보까지 모두 직접 챙겨야 해요. 관련 조사에서도 {audience}의 상당수가 이런 업무 부담을 큰 어려움으로 꼽았어요(출처: 중소벤처기업부, ○○년 기준).

시간이 부족하면 가장 먼저 밀리는 게 꾸준한 홍보예요. 오늘 할 일은 많은데 글 한 편 쓰는 데 반나절이 걸리니까요. 게다가 채널마다 글 형식이 달라서 같은 내용을 여러 번 다시 써야 하죠.

그래서 {name} 같은 도구를 쓸 때도 처음 목표는 "전부 맡기기"가 아니라 "반복되는 일 줄이기"로 잡는 게 현실적이에요. 자료를 찾고 정리하는 일, 초안의 뼈대를 잡는 일, 맞춤법과 형식을 점검하는 일처럼 매번 비슷하게 반복되는 일부터 맡겨 보세요.

[이미지: 반복 업무와 판단 업무를 나눈 표]

## 혼자서도 가능한 3단계

첫째, 일주일 동안 {kw0}에 쓴 시간을 기록해 보세요. 어디에 시간이 새는지 보이면 줄일 곳도 보여요. 기록은 거창할 필요 없이 메모장에 "무슨 일, 몇 분"만 적어도 충분해요.

둘째, 가장 오래 걸린 일 하나만 도구에 맡겨 보세요. 자료 조사나 초안 작성처럼 반복되는 일이 좋아요. 한 번에 여러 개를 바꾸면 무엇이 효과가 있었는지 알기 어려워요.

셋째, 결과물은 반드시 직접 확인하세요. 특히 수치와 사실은 출처를 열어 보고 기준 시점이 맞는지 봐야 해요. 도구가 만든 초안은 출발점일 뿐이고, 최종 판단은 사람의 몫이에요.

처음 한 달은 효과를 숫자로 확인해 보는 걸 추천해요. 글 한 편에 걸린 시간을 적어 두면 줄어든 시간이 눈에 보여서 계속할 힘이 생겨요. 그리고 채널을 한꺼번에 늘리기보다 한 채널에서 꾸준히 올리는 습관을 먼저 만들면 좋아요.

도구가 만든 문장을 그대로 쓰기보다 소리 내어 읽어 보는 것도 방법이에요. 어색한 부분이 바로 들려서 내 말투로 고치기가 쉬워요. 우리 가게만의 이야기와 단골손님이 자주 묻는 질문을 한두 줄 더하면 글이 훨씬 살아나요.

{profile_para}<<OPTIONAL>>**정리하면**

- 처음에는 반복되는 일 하나만 줄여 보세요
- 도구가 만든 결과물의 수치는 출처와 기준 시점을 꼭 확인하세요
- 줄어든 시간은 고객과 제품에 쓰세요

도움이 되셨다면 이웃추가하고 다음 글도 받아 보세요. 궁금한 점은 댓글로 남겨 주시면 답해 드릴게요.{closing_extra}

출처: 중소벤처기업부 정책·실태조사 자료(○○년 기준, 원문 확인 필요){source_extra}"""

_BLOG_R1_BODY = """[이미지: 노트북 앞에서 할 일 목록을 정리하는 1인 창업자]

## {kw0}, 왜 지금 필요할까요

혼자 사업을 하면 제품 준비, 고객 응대, 홍보까지 모두 직접 챙겨야 해요. 관련 조사에서도 {audience}의 상당수가 이런 업무 부담을 큰 어려움으로 꼽았어요(출처: 중소벤처기업부, ○○년 기준).

시간이 부족하면 가장 먼저 밀리는 게 꾸준한 홍보예요. 오늘 할 일은 많은데 글 한 편 쓰는 데 반나절이 걸리니까요.

[이미지: 반복 업무와 판단 업무를 나눈 표]

## 먼저 반복되는 일과 판단할 일을 나눠요

도구에 맡기기 좋은 일은 매번 비슷하게 반복되는 일이에요. 자료를 찾고 정리하기, 초안의 뼈대 잡기, 맞춤법과 형식 점검하기가 대표적이에요.

반대로 어떤 메시지를 낼지, 고객에게 무엇을 약속할지는 사람이 정해야 해요. 이 구분만 해 둬도 도구를 쓸 때 기대치가 분명해져요.

## 혼자서도 가능한 3단계

첫째, 일주일 동안 {kw0}에 쓴 시간을 기록해 보세요. 메모장에 "무슨 일, 몇 분"만 적어도 어디에 시간이 새는지 보여요.

둘째, 가장 오래 걸린 일 하나만 {name} 같은 도구에 맡겨 보세요. 한 번에 여러 개를 바꾸면 무엇이 효과가 있었는지 알기 어려워요.

셋째, 결과물은 반드시 직접 확인하세요. 수치와 사실은 출처를 열어 보고 기준 시점이 맞는지 봐야 해요.

[이미지: 3단계 체크리스트 카드]

## 직접 써 보며 확인한 점

[경험 사례: 실제 사용 경험이 있으면 이 자리에 적어 주세요]

써 보면 초안은 빨리 나오지만, 우리 가게만의 이야기와 말투는 사람이 더해야 글이 살아나요. 도구가 만든 초안은 출발점이고, 최종 판단은 사람의 몫이라는 점을 기억해 주세요.

처음 한 달은 효과를 숫자로 확인해 보는 걸 추천해요. 글 한 편에 걸린 시간을 적어 두면 줄어든 시간이 눈에 보여서 계속할 힘이 생겨요. 채널을 한꺼번에 늘리기보다 한 채널에서 꾸준히 올리는 습관을 먼저 만드는 것도 좋아요.

도구가 만든 문장은 소리 내어 읽어 보세요. 어색한 부분이 바로 들려서 내 말투로 고치기가 쉬워요. 단골손님이 자주 묻는 질문을 한두 줄 더하면 글이 훨씬 살아나요.

[이미지: 초안과 최종본을 나란히 비교한 화면]

{profile_para}<<OPTIONAL>>## 정리하면

- 반복되는 일과 판단할 일을 먼저 나눠요
- 가장 오래 걸리는 일 하나부터 줄여요
- 수치는 출처와 기준 시점을 꼭 확인해요

도움이 되셨다면 이웃추가하고 다음 글도 받아 보세요. 궁금한 점은 댓글로 남겨 주시면 답해 드릴게요.{closing_extra}

출처: 중소벤처기업부 정책·실태조사 자료(○○년 기준, 원문 확인 필요){source_extra}"""

_BLOG_OPTIONAL = [
    "덧붙여, 기록한 시간은 한 달 뒤에 다시 비교해 보세요. 어떤 일을 맡겼을 때 가장 많이 줄었는지 보이면 다음에 맡길 일도 자연스럽게 정해져요.",
    "익숙해진 뒤에 다른 채널로 넓혀도 늦지 않아요. 한 채널에서 반응이 좋았던 글을 다른 채널 형식으로 바꿔 쓰는 것부터 해 보면 부담이 적어요.",
    "마지막으로, 글을 올린 뒤 달린 댓글과 질문을 모아 두세요. 다음 글의 주제가 되고, 고객이 실제로 궁금해하는 게 무엇인지 알 수 있어요.",
    "혹시 어떤 일부터 맡길지 고민된다면, 매주 같은 순서로 반복하는 일을 떠올려 보세요. 순서가 정해진 일일수록 도구가 잘 도와줄 수 있어요.",
    "도구를 고를 때는 기능 개수보다 결과물을 확인하기 쉬운지를 먼저 보세요. 어디서 가져온 정보인지, 무엇이 바뀌었는지 바로 보이는 도구가 오래 쓰기 좋아요.",
    "가격도 꼭 따져 보세요. 월 이용료가 부담된다면 무료 체험 기간 동안 가장 시간이 많이 드는 일 하나에만 써 보고 판단해도 충분해요.",
    "처음부터 완벽한 글을 목표로 하지 않아도 괜찮아요. 일주일에 한 편이라도 꾸준히 올리는 게, 가끔 올리는 완벽한 글보다 검색에서도 고객 기억에서도 오래 남아요.",
    "글을 쓰다 막히면 고객이 처음 가게를 찾았을 때 무엇을 궁금해했는지 떠올려 보세요. 그 질문 하나에 답하는 것만으로도 좋은 글 한 편이 돼요.",
]

_LI_R0 = [
    "{name_eun} 자료 조사와 초안 작성, 검수까지 이어지는 반복 작업을 리서치·작성·검수 에이전트가 나눠 맡는 구조로 만든 서비스이고, 오늘은 이 구조를 처음 설계하면서 시행착오를 겪고 배운 점을 조금 길게 정리해 보려고 합니다.",
    "혼자 사업을 하면 제품 준비도, 고객 응대도, 홍보와 문서 작업도 모두 대표의 몫이라 콘텐츠는 늘 뒤로 밀리기 마련인데, 이 문제를 어떻게 풀 수 있을지 몇 달 동안 고민한 과정과 지금까지의 결론을 공유합니다.",
]
_LI_R1_HOOK = [
    "혼자 사업하면 홍보는 늘 '이번 주만 넘기고'가 됩니다.",
    "그런데 고객은 이번 주에도 검색하고 있습니다.",
]
_LI_BODY = [
    "[데모] 이 글은 mock 모드 템플릿으로 만든 예시입니다. 수치는 ○○ 자리표시로 남겨 두었습니다.",
    "{audience_eun} 제품 준비와 고객 응대만으로도 하루가 빠듯합니다. 중소벤처기업부 조사(○○년 기준)에서도 운영 업무 부담이 주요 어려움으로 꼽혔습니다.",
    "그러다 보니 콘텐츠는 늘 '시간 날 때 하는 일'이 됩니다. 문제는 그 시간이 좀처럼 나지 않는다는 것입니다. 한 달에 한두 편 올리다 멈추는 일이 반복됩니다.",
    "그래서 {name_eul} 만들며 한 가지 원칙을 세웠습니다. 사람이 할 판단은 남기고, 반복되는 일만 덜어 내자는 것입니다.",
    "그 과정에서 배운 것은 세 가지입니다.",
    "• 첫째, 자료 조사와 초안 작성은 나눠야 합니다. 근거를 먼저 모아 두면 글이 흔들리지 않습니다.\n• 둘째, 검수는 쓴 사람이 아닌 다른 눈이 해야 합니다. 형식과 사실을 따로 점검하면 실수가 줄어듭니다.\n• 셋째, 최종 게시는 사람이 승인해야 합니다. 도구는 초안을 빠르게 만들 뿐, 책임은 사람에게 있습니다.",
    "특히 수치는 출처와 기준 시점을 반드시 함께 적습니다. 근거가 없으면 문장을 지우거나 표현을 낮춥니다.",
    "이렇게 역할을 나누고 나니 가장 크게 달라진 건 글의 속도보다 확인의 부담이었습니다. 무엇을 확인해야 하는지 목록으로 보이니, 마지막 점검에 쓰는 시간이 짧아졌습니다.",
    "물론 도구가 모든 걸 대신하지는 못합니다. 우리 고객이 어떤 말에 반응하는지, 어떤 약속을 해도 되는지는 여전히 대표가 가장 잘 압니다.",
    "[대표 경험: 실제로 겪은 장면이 있으면 이 자리에 한두 문장으로 적어 주세요]",
]
_LI_OPTIONAL = [
    "처음에는 모든 걸 자동화하고 싶었습니다. 하지만 막상 해 보니 가장 효과가 큰 건 반복되는 일 하나를 확실히 줄이는 것이었습니다.",
    "한 주에 글 한 편이라도 꾸준히 올리는 흐름이 생기면, 그다음부터는 채널을 넓히는 일이 훨씬 쉬워집니다.",
    "무엇을 맡기고 무엇을 남길지 정하는 것, 결국 그게 작은 팀이 콘텐츠를 지속하는 방법이라고 생각합니다.",
    "앞으로도 시행착오를 솔직하게 나누겠습니다. 비슷한 고민을 하는 분들께 작은 참고가 되길 바랍니다.",
    "돌아보면 가장 오래 걸린 건 글쓰기 자체가 아니라, 무엇을 근거로 쓸지 정하는 일이었습니다. 근거를 먼저 모으는 순서로 바꾸자 글이 훨씬 빨리 정리됐습니다.",
    "검수 기준을 글로 적어 둔 것도 도움이 됐습니다. 기준이 보이면 고칠 곳도 분명해지고, 같은 실수를 반복하지 않게 됩니다.",
    "작은 팀일수록 '누가 확인하는가'를 먼저 정해 두는 게 좋습니다. 확인하는 사람이 정해지면 도구를 쓰는 범위도 자연스럽게 정해집니다.",
    "그리고 한 번에 완벽한 시스템을 만들려 하기보다, 한 채널에서 한 달 동안 써 보고 조정하는 편이 훨씬 빨랐습니다.",
]
_LI_CTA = "여러분은 콘텐츠 한 편에 몇 시간을 쓰고 계신가요? 댓글로 나눠 주세요."

_IG_SLIDES = [
    ("{kw0}, 혼자서도 됩니다", "{kw0}, 혼자서도 시작할 수 있어요", "{cover_bg}에 큰 제목, 오른쪽 아래에 노트북 일러스트", "제목 '{kw0}, 혼자서도 시작할 수 있어요'가 적힌 표지"),
    ("이런 고민 있으신가요", "글 한 편에 반나절, 그래서 홍보가 밀려요", "시계와 쌓인 할 일 메모를 나란히 배치", "시계와 할 일 메모가 놓인 책상 그림"),
    ("왜 이렇게 오래 걸릴까", "채널마다 형식이 달라 같은 글을 여러 번 써요", "블로그·SNS 화면 세 개를 겹쳐 보여 주는 구성", "형식이 다른 세 개의 게시물 화면"),
    ("해결 1 — 시간 기록", "일주일만 시간을 기록해 보세요", "체크 표시가 있는 주간 표 한 장", "요일별 작업 시간을 적은 주간 표"),
    ("해결 2 — 하나만 맡기기", "가장 오래 걸린 일 하나만 맡겨요", "큰 화살표로 한 가지 업무를 도구 아이콘에 넘기는 장면", "한 가지 업무 카드를 도구 아이콘으로 옮기는 그림"),
    ("해결 3 — 직접 확인", "수치는 출처와 기준 시점을 확인해요", "돋보기와 출처 카드, 체크 도장", "돋보기로 출처 카드를 확인하는 모습"),
    ("오늘의 체크리스트", "기록하기 · 하나만 맡기기 · 직접 확인하기", "세 줄 체크리스트, 항목마다 아이콘", "세 가지 항목이 적힌 체크리스트"),
    ("저장해 두세요", "저장해 두고 이번 주에 하나만 해 보세요", "저장 아이콘을 강조한 마무리 장면, 계정명 {ig_handle}", "저장 아이콘이 강조된 마무리 화면"),
]


def _references(research: ResearchPack) -> str:
    lines = []
    for src in research.sources:
        if src.origin == "user":
            lines.append(f"- [{src.id}] 자사 자료, 「{src.title}」(사용자 제공, 외부 검증 전)")
            continue
        when = src.published or "기준 시점 확인 필요"
        lines.append(f"- [{src.id}] {src.title}, {src.publisher} ({when}) {src.url}")
    return "\n".join(lines) or "- [s1] [참고자료 확인 필요]"


def _items(values: list[str], count: int, limit: int) -> list[str]:
    return [_clip(v, limit) for v in values if v.strip()][:count]


def _banned_pattern(word: str) -> re.Pattern[str] | None:
    chars = [c for c in word if not c.isspace()]
    return re.compile(r"\s*".join(re.escape(c) for c in chars), re.IGNORECASE) if chars else None


def scrub_banned(text: str, banned: list[str]) -> tuple[str, list[str]]:
    """Replace each banned expression (spacing/case-insensitive, like the
    code check) with ``○○``; returns the new text and the words removed."""
    hits: list[str] = []
    for word in banned:
        pattern = _banned_pattern(word)
        if pattern is not None and pattern.search(text):
            text = pattern.sub("○○", text)
            hits.append(word.strip())
    return text, hits


def _profile_context(profile: Profile | None, research: ResearchPack) -> dict[str, str]:
    """Extra template variables from the company profile and user sources
    (all empty strings without a profile, so the template reads as before)."""
    p = profile if profile is not None and not profile_is_empty(profile) else None
    user_srcs = [s for s in research.sources if s.origin == "user"]
    extra = {"company_row": "", "biz_model": "월 구독형 서비스 (가정)", "profile_facts": "", "team_rows": _DEFAULT_TEAM_ROWS,
             "profile_para": "", "closing_extra": "", "source_extra": "", "cover_bg": "짙은 남색 배경", "ig_handle": "자리표시"}
    if user_srcs:
        extra["source_extra"] = "".join(f"\n출처: 자사 자료 「{s.title}」(사용자 제공)" for s in user_srcs)
    facts: list[str] = []
    if p is not None:
        if p.company_name.strip():
            extra["company_row"] = f"| 기업명 | {_clip(p.company_name, 40)} (자사 자료) |\n"
        if p.business_model.strip():
            extra["biz_model"] = f"{_clip(p.business_model, 60)} (자사 자료)"
        for label, value in (("한 줄 소개", p.one_liner), ("해결하려는 문제", p.problem), ("해결 방법", p.solution),
                             ("가격", p.pricing)):
            if value.strip():
                facts.append(f"- {label}: {_clip(value, 120)} (자사 자료)")
        facts += [f"- 차별점: {d} (자사 자료)" for d in _items(p.differentiators, 3, 80)]
        facts += [f"- 실적·지표: {t} (자사 자료)" for t in _items(p.traction, 3, 80)]
        members = [m for m in p.team if m.role.strip() or m.background.strip()][:6]
        if members:
            rows = []
            for m in members:
                role = _clip(m.role, 20) or "팀원"
                label = "채용 예정" if m.hiring else role
                if m.background.strip():
                    ability = f"{_clip(m.background, 80)} (자사 자료)"
                else:
                    ability = "[요구 역량: ○○]" if m.hiring else "[경력: ○○ 분야 ○년]"
                rows.append(f"| {label} | ○○○ | {role} | {ability} |")
            extra["team_rows"] = "\n".join(rows)
        name = _clip(p.service_name, 24) or "저희 서비스"
        if p.one_liner.strip():
            extra["profile_para"] = f"참고로 {josa(name, '을/를')} 한 줄로 소개하면 '{_clip(p.one_liner, 90)}'예요(자사 자료).\n\n"
        closing = [_clip(p.cta, 100)] if p.cta.strip() else []
        if p.contact.strip():
            closing.append(f"문의: {_clip(p.contact, 60)}")
        closing += _items(p.required_phrases, 3, 100)
        if closing:
            extra["closing_extra"] = "\n\n" + "\n".join(closing)
        if p.brand_colors:
            extra["cover_bg"] = f"브랜드 주 색({_clip(p.brand_colors[0], 12)}) 배경"
        if p.instagram_handle.strip():
            extra["ig_handle"] = _clip(p.instagram_handle, 30)
    user_ids = {s.id for s in user_srcs}
    for finding in [f for f in research.findings if f.source_ids and f.source_ids[0] in user_ids][:3]:
        claim = _DEMO_PREFIX.sub("", finding.claim)
        facts.append(f"- {claim} [{finding.source_ids[0]}]")
    if facts:
        extra["profile_facts"] = "### 자사 자료 기반 사실 (사용자 제공, 외부 검증 전)\n\n" + "\n".join(facts) + "\n\n"
    return extra


_DEMO_PREFIX = re.compile(r"^\[데모\]\s*")
_DEFAULT_TEAM_ROWS = """| 대표 | [대표자 성명] | 사업 총괄, 고객 인터뷰 | [경력: ○○ 분야 ○년] |
| 팀원 | [팀원 성명] | 서비스 개발 | [경력: ○○ 개발 ○년] |
| 채용 예정 | [채용 예정] | 콘텐츠 품질 관리 | [요구 역량: ○○] |"""


def template_draft(brief: Brief, research: ResearchPack, channel: ChannelId, round: int,
                   profile: Profile | None = None) -> Draft:
    ctx = _Ctx(brief_context(brief, profile), **_profile_context(profile, research))
    has_profile = profile is not None and not profile_is_empty(profile)
    default_tags = list(profile.default_hashtags) if has_profile and profile is not None else []
    required = _items(profile.required_phrases, 3, 100) if has_profile and profile is not None else []
    cta = _clip(profile.cta, 100) if has_profile and profile is not None and profile.cta.strip() else ""
    used = [f.id for f in research.findings]
    fixed = round >= 1
    change_log: list[str] = []

    if channel == "bizplan":
        scale = _BIZ_SCALE_R1 if fixed else _BIZ_SCALE_R0
        content = (_BIZ_HEAD + scale + _BIZ_TEAM).format_map(_Ctx(ctx, references=_references(research)))
        title = f"{ctx['name']} 사업계획서 (초안)"
        hashtags: list[str] = []
        if fixed:
            change_log = [
                "[major] 성장전략: TAM·SAM·SOM 표를 추가하고 각 값에 [s#] 근거와 가정 표시를 붙임",
                "[major] 성장전략: 사업비 집행 계획 표와 추진 일정 표를 추가함",
                "[minor] 비즈니스 모델: 월 구독형 가격이 가정임을 명시함",
            ]

    elif channel == "naver_blog":
        body = _BLOG_R1_BODY if fixed else _BLOG_R0_BODY
        template = _BLOG_INTRO.format_map(ctx) + "\n\n" + body.format_map(ctx)
        content = _fill(template, _BLOG_OPTIONAL, chars_no_space, 1700, 2900)
        kw = ctx["kw0"]
        candidates = [f"{kw} 혼자 시작하는 3단계 방법", f"{kw} 시작 가이드", f"{kw} 3단계", kw]
        title = next((c for c in candidates if len(c) <= 40), kw[:40])
        hashtags = make_tags(brief, 8, ["1인창업", "소상공인", "콘텐츠마케팅", "블로그운영", "마케팅팁", "창업준비", "업무자동화", "데모"],
                             first=default_tags)
        if fixed:
            change_log = [
                "[major] 형식: ## 소제목을 2개에서 5개로 늘림",
                "[major] 형식: [이미지: …] 자리를 2개에서 5개로 늘림",
                "[minor] 경험·독창성: 직접 써 본 경험을 적을 자리표시 문단을 추가함",
            ]

    elif channel == "linkedin":
        count = 4 if fixed else 7
        hashtags = make_tags(brief, count, ["1인창업", "콘텐츠마케팅", "AI에이전트", "스타트업", "소상공인", "마케팅자동화", "창업", "데모"],
                             first=default_tags)
        hook = _LI_R1_HOOK if fixed else _LI_R0
        tag_line = " ".join(hashtags)
        head = "\n".join(line.format_map(ctx) for line in hook)
        body = [p.format_map(ctx) for p in _LI_BODY]
        if has_profile and profile is not None:
            about: list[str] = []
            if profile.one_liner.strip():
                about.append(f"참고로 {ctx['name_eul']} 한 줄로 소개하면 '{_clip(profile.one_liner, 90)}'입니다.")
            traction = _items(profile.traction, 2, 60)
            if traction:
                about.append(f"지금까지의 진행 상황은 {', '.join(traction)}입니다(자사 집계).")
            if about:
                body.insert(4, " ".join(about))
        tail = [*([cta] if cta else []), *required, _LI_CTA, tag_line]

        def measure(text: str) -> int:
            return chars_with_space(text + "\n\n" + "\n\n".join(tail))

        middle = _fit([head, *body], _LI_OPTIONAL, measure, 1450, 1950)
        content = middle + "\n\n" + "\n\n".join(tail)
        title = f"[데모] {ctx['name']} — 반복 업무를 덜어 낸 방법"
        if fixed:
            hook_len = chars_with_space(first_lines(content, 2))
            change_log = [
                f"[major] 훅: 첫 두 줄을 서비스 소개에서 독자의 상황으로 바꾸고 {hook_len}자로 줄임",
                "[major] 형식: 해시태그를 7개에서 4개로 줄이고 마지막 줄과 맞춤",
            ]

    elif channel == "instagram":
        hashtags = make_tags(brief, 5, ["1인창업", "소상공인", "콘텐츠마케팅", "마케팅팁", "업무자동화", "데모"], first=default_tags)
        slides = []
        for i, (heading, line, visual, alt) in enumerate(_IG_SLIDES, start=1):
            slides.append(
                f"### 슬라이드 {i} — {heading.format_map(ctx)}\n- 문구: {line.format_map(ctx)}\n"
                f"- 비주얼: {visual.format_map(ctx)}\n- 대체텍스트: {alt.format_map(ctx)}"
            )
        contact = profile.contact.strip() if has_profile and profile is not None else ""
        brand_lines = [*([cta] if cta else []), *([f"문의: {_clip(contact, 60)}"] if contact and "://" not in contact else []),
                       *required]
        caption_lines = [
            _clip(f"{ctx['kw0']}, 혼자서도 시작할 수 있어요. 오늘은 딱 3단계만 정리했어요.", 120),
            "",
            "[데모] mock 모드 템플릿으로 만든 예시 캡션이에요.",
            "",
            "1. 일주일 동안 작업 시간을 기록해요",
            "2. 가장 오래 걸린 일 하나만 도구에 맡겨요",
            "3. 수치는 출처와 기준 시점을 직접 확인해요",
            "",
            *([*brand_lines, ""] if brand_lines else []),
            "저장해 두고 이번 주에 하나만 해 보세요. 친구에게 공유하면 함께 시작하기 좋아요.",
            "",
            " ".join(hashtags),
        ]
        content = "## 캐러셀\n\n" + "\n\n".join(slides) + "\n\n## 캡션\n\n" + "\n".join(caption_lines)
        title = f"[데모] {ctx['kw0']} 3단계 캐러셀"
        if fixed:
            change_log = ["[minor] 슬라이드 5: 문구를 짧게 다듬음"]
    else:  # pragma: no cover - ChannelId is a closed set
        raise ValueError(channel)

    if fixed and has_profile and profile is not None and profile.banned_words:
        banned = [w for w in profile.banned_words if w.strip()]
        kept_tags = [t for t in hashtags if not scrub_banned(t, banned)[1]]
        if kept_tags != hashtags and channel in ("linkedin", "instagram"):
            content = content.replace(" ".join(hashtags), " ".join(kept_tags))
        hashtags = kept_tags
        title, hit_title = scrub_banned(title, banned)
        content, hit_body = scrub_banned(content, banned)
        hits = list(dict.fromkeys(hit_title + hit_body))
        if hits:
            change_log.append(f"[major] 브랜드: 금지 표현({', '.join(hits)})을 지움")

    return Draft(channel=channel, round=round, title=title, content=content, hashtags=hashtags,
                 used_finding_ids=used, change_log=change_log)


# ---------------------------------------------------------------------------
# Template reviews
# ---------------------------------------------------------------------------

# (score, comment) per rubric id for the first draft (0) and revisions (1).
_SCORES: dict[str, dict[int, dict[str, tuple[int, str]]]] = {
    "bizplan": {
        0: {"problem": (15, "문제 근거가 있으나 고객 사례가 추상적임"),
            "solution": (14, "단계별 구조는 명확하나 개발 일정이 구체적이지 않음"),
            "scaleup": (8, "TAM·SAM·SOM과 사업비 집행 계획이 빠져 성장 논리가 약함"),
            "team": (7, "자리표시로 남긴 점은 적절하나 역할 연결 설명이 짧음"),
            "evidence": (12, "수치마다 [s#]가 붙어 있으나 일부 주장은 출처가 없음")},
        1: {"problem": (17, "문제 근거와 기존 대안의 한계가 잘 정리됨"),
            "solution": (17, "단계별 개발 계획과 차별성이 구체적임"),
            "scaleup": (17, "시장 규모 표, 비즈니스 모델, 일정·사업비 표가 갖춰짐"),
            "team": (8, "역할과 필요 역량이 표로 정리됨"),
            "evidence": (16, "수치마다 출처와 기준 시점, 가정 표시가 있음")},
    },
    "naver_blog": {
        0: {"search_intent": (16, "제목과 도입부에 메인 키워드가 자연스럽게 들어감"),
            "originality": (13, "직접 경험이 드러나지 않아 정보글 느낌이 강함"),
            "readability": (13, "소제목과 이미지 자리가 적어 모바일에서 길게 느껴짐"),
            "accuracy": (16, "수치에 기관명과 기준 시점을 밝힘"),
            "cta": (8, "이웃추가·댓글 유도가 있음")},
        1: {"search_intent": (17, "검색 의도에 바로 답하는 도입부와 키워드 배치가 좋음"),
            "originality": (15, "경험을 적을 자리를 마련했지만 실제 사례는 채워야 함"),
            "readability": (18, "짧은 문단, 소제목, 이미지 자리로 읽기 편함"),
            "accuracy": (16, "수치에 기관명과 기준 시점을 밝힘"),
            "cta": (9, "요약과 행동 유도가 명확함")},
    },
    "linkedin": {
        0: {"hook": (13, "첫 두 줄이 서비스 소개로 시작하고 길어서 '더 보기' 전에 잘림"),
            "insight": (19, "세 가지 교훈이 실무에 쓸모 있음"),
            "structure": (11, "목록은 좋으나 첫 문단이 길어 훑어 읽기 어려움"),
            "accuracy": (12, "수치에 기관과 기준 시점을 밝힘"),
            "cta": (8, "답하기 쉬운 질문으로 끝남")},
        1: {"hook": (21, "독자의 상황으로 시작하는 짧은 두 줄이라 멈춰 읽게 됨"),
            "insight": (21, "세 가지 교훈이 구체적이고 쓸모 있음"),
            "structure": (13, "한두 문장 문단과 목록으로 잘 읽힘"),
            "accuracy": (12, "수치에 기관과 기준 시점을 밝힘"),
            "cta": (9, "답하기 쉬운 질문으로 끝남")},
    },
    "instagram": {
        0: {"hook": (21, "표지 문구와 캡션 첫 줄이 짧고 분명함"),
            "slide_flow": (21, "문제→원인→해결→체크리스트 흐름이 자연스러움"),
            "visual_direction": (13, "장별 비주얼 지시와 대체텍스트가 구체적임"),
            "accuracy": (12, "수치 주장을 하지 않아 사실 오류 위험이 낮음"),
            "cta": (9, "저장·공유 유도가 분명함")},
        1: {"hook": (22, "표지 문구와 캡션 첫 줄이 짧고 분명함"),
            "slide_flow": (22, "장당 한 메시지로 끝까지 넘기게 됨"),
            "visual_direction": (13, "장별 비주얼 지시와 대체텍스트가 구체적임"),
            "accuracy": (12, "수치 주장을 하지 않아 사실 오류 위험이 낮음"),
            "cta": (9, "저장·공유 유도가 분명함")},
    },
}

_CONTENT_ISSUES: dict[str, list[ReviewIssue]] = {
    "bizplan": [
        ReviewIssue(severity="major", location="3. 성장전략", problem="TAM·SAM·SOM 시장 규모 산정이 없음",
                    fix="TAM→SAM→SOM 표를 넣고 각 값에 [s#]와 기준 시점, 가정 여부를 표기"),
        ReviewIssue(severity="major", location="3. 성장전략", problem="사업비 집행 계획과 추진 일정이 표로 정리되지 않음",
                    fix="비목·산출 근거·금액 표와 단계·기간·산출물 표를 추가"),
        ReviewIssue(severity="minor", location="1-2. 목표 고객이 겪는 문제", problem="고객 문제 사례가 추상적임",
                    fix="브리프의 대상 고객이 겪는 장면 한 가지를 구체적으로 서술"),
    ],
    "naver_blog": [
        ReviewIssue(severity="minor", location="도입부 두 번째 문단", problem="문단이 길어 모바일에서 한 화면을 넘김",
                    fix="두 문장씩 끊어 문단을 나누기"),
    ],
    "linkedin": [
        ReviewIssue(severity="major", location="첫 두 줄", problem="서비스 소개로 시작해 독자가 멈출 이유가 약함",
                    fix="독자의 상황을 짚는 짧은 한 줄로 시작 (예: '혼자 사업하면 홍보는 늘 뒤로 밀립니다.')"),
    ],
    "instagram": [
        ReviewIssue(severity="minor", location="슬라이드 5", problem="문구가 길어 한눈에 읽히지 않음",
                    fix="12단어 이내로 줄이기"),
    ],
}

_NEEDS_RESEARCH = {"bizplan": ["경쟁·대체 서비스의 월 구독 가격대 (공개 자료, 기준 시점 포함)"]}


def template_review(brief: Brief, research: ResearchPack, draft: Draft, format_checks: list[FormatCheck]) -> Review:
    spec = CHANNELS[draft.channel]
    level = 0 if draft.round == 0 else 1
    table = _SCORES[draft.channel][level]
    rubric: list[RubricScore] = []
    for item in spec.rubric:
        if item.id == FORMAT_ITEM_ID:
            passed = sum(1 for c in format_checks if c.passed)
            score = round(item.max * passed / len(format_checks)) if format_checks else item.max
            rubric.append(RubricScore(id=item.id, label=item.label, score=score, max=item.max, comment="코드가 다시 계산함"))
        else:
            score, comment = table[item.id]
            rubric.append(RubricScore(id=item.id, label=item.label, score=score, max=item.max, comment=comment))

    issues: list[ReviewIssue] = []
    for check in format_checks:
        if not check.passed:
            issues.append(ReviewIssue(severity="major", location=f"형식 — {check.label}",
                                      problem=f"{check.label} {check.value}, 기준({check.expected}) 미충족",
                                      fix=f"{check.label}: 현재 {check.value} → {check.expected}로 맞추기"))
    if level == 0:
        issues.extend(_CONTENT_ISSUES.get(draft.channel, []))
    else:
        issues.append(ReviewIssue(severity="minor", location="전체", problem="자리표시(○○, [ ])가 남아 있음",
                                  fix="게시 전 실제 값과 경험으로 채우기"))
    issues.sort(key=lambda i: {"critical": 0, "major": 1, "minor": 2}[i.severity])

    by_id = {f.id: f for f in research.findings}
    fact_checks = [
        FactCheck(claim=_clip(by_id[fid].claim, 80), verdict="supported", source_ids=list(by_id[fid].source_ids),
                  note="[데모] 자리표시 값 — live 모드에서 원문 수치로 대조")
        for fid in draft.used_finding_ids[:4] if fid in by_id
    ]
    total = sum(r.score for r in rubric)
    score = round(100 * total / spec.rubric_total)
    failing = [i for i in issues if i.severity in ("critical", "major")]
    if failing:
        summary = f"[데모] {len(failing)}건의 주요 이슈가 있어 수정이 필요해요. 가장 먼저 '{failing[0].problem}'부터 고쳐 주세요."
    else:
        summary = "[데모] 형식과 내용 기준을 충족해요. 게시 전 자리표시만 실제 값으로 채우면 돼요."
    return Review(
        channel=draft.channel, round=draft.round, score=score, passed=score >= 80 and not any(i.severity == "critical" for i in issues),
        rubric=rubric, issues=issues, fact_checks=fact_checks, format_checks=list(format_checks),
        needs_research=list(_NEEDS_RESEARCH.get(draft.channel, [])) if level == 0 else [],
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Template content calendar
# ---------------------------------------------------------------------------

_CALENDAR_ANGLES: list[tuple[str, str]] = [
    ("체크리스트", "{subject}, 시작 전에 확인할 5가지"),
    ("단계별 방법", "{subject} 3단계로 시작하기"),
    ("자주 묻는 질문", "{audience_i} {subject}에 대해 자주 묻는 질문"),
    ("흔한 실수", "{subject}에서 흔히 하는 실수 3가지"),
    ("데이터 해설", "{subject} 관련 공식 통계 읽는 법"),
    ("비교", "{subject}: 직접 하기와 도구 쓰기 비교"),
    ("비하인드", "{service_eul} 만들며 배운 점"),
    ("사례", "{subject} 적용 전후로 달라진 점 [사례 확인 필요]"),
    ("용어 정리", "{subject} 핵심 용어 한 번에 정리"),
]


def _calendar_topics(profile: Profile | None, theme: str) -> list[tuple[str, str]]:
    """Deterministic (topic, angle) bank from the theme and the profile."""
    p = profile if profile is not None and not profile_is_empty(profile) else Profile()
    service = _clip(p.service_name, 24) or _clip(p.company_name, 24) or "우리 서비스"
    subject = _clip(theme, 40) or _clip(p.one_liner, 40) or service
    audience = _clip(p.target_customers, 24) or "고객"
    ctx = _Ctx(subject=subject, audience_i=josa(audience, "이/가"), service_eul=josa(service, "을/를"))
    bank = [(template.format_map(ctx), angle) for angle, template in _CALENDAR_ANGLES]
    for diff in _items(p.differentiators, 3, 40):
        bank.append((f"{josa(diff, '이/가')} 필요한 이유", "차별점 소개"))
    if p.problem.strip():
        bank.append((f"{_clip(p.problem, 50)} — 왜 생기고 어떻게 줄일까", "고객 문제"))
    return bank


def template_calendar(profile: Profile | None, theme: str, start: str, end: str, counts: dict[str, int],
                      history: list[ContentItem]) -> ContentPlan:
    """Deterministic plan: each channel's posts spread over the weekdays in
    range (channels offset so they rarely share a day), topics drawn in turn
    from a bank built from the theme and profile, skipping any topic that
    repeats ``history`` or an earlier slot."""
    days = slot_days(start, end)
    capped, _ = cap_counts(normalize_counts(counts), len(days))
    p = profile if profile is not None and not profile_is_empty(profile) else Profile()
    bank = _calendar_topics(p, theme)
    seen = history_keys(history)
    main = _clip(theme, 20) or _clip(p.service_name, 20) or "콘텐츠 마케팅"
    order = {c: i for i, c in enumerate(ALL_CHANNELS)}
    wanted: list[tuple[str, ChannelId]] = []
    for channel, count in capped.items():
        wanted += [(day, channel) for day in spread(count, days, offset=order[channel])]
    wanted.sort(key=lambda pair: (pair[0], order[pair[1]]))

    slots: list[PlannedSlot] = []
    cursor = 0
    extra = 0
    for day, channel in wanted:
        while True:
            if cursor < len(bank):
                topic, angle = bank[cursor]
            else:
                extra += 1
                topic, angle = f"{_clip(theme, 40) or main} 인사이트 {extra}", "인사이트"
            cursor += 1
            if not repeats_history(topic, seen):
                break
        seen.add(topic_key(topic))
        keywords = list(dict.fromkeys(k for k in (main, _clip(p.industry, 20), angle) if k))[:5]
        slots.append(PlannedSlot(date=day, channel=channel, topic=topic, angle=angle, keywords=keywords,
                                 goal=DEFAULT_GOALS.get(channel, "")))
    total = len(slots)
    mix = ", ".join(f"{CHANNELS[c].label} {n}편" for c, n in capped.items())
    summary = (f"[데모] {start}~{end} 평일에 {mix}, 모두 {total}편을 배치했어요. 지난 게시물과 겹치는 주제는 뺐어요. "
               "live 모드에서는 총괄 에이전트가 프로필과 주제를 읽고 계획해요.")
    return normalize_plan(ContentPlan(summary=summary, slots=slots), start, end, capped)


# ---------------------------------------------------------------------------
# Recorded sample run
# ---------------------------------------------------------------------------


def _topic_key(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class SampleRun:
    """Files of a recorded run (``examples/sample-run``)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.brief = Brief.model_validate_json((self.root / "brief.json").read_text(encoding="utf-8"))

    @classmethod
    def load(cls, root: Path | None) -> "SampleRun | None":
        if root is None or not (Path(root) / "brief.json").is_file():
            return None
        try:
            return cls(Path(root))
        except (OSError, ValueError):
            return None

    def matches(self, brief: Brief) -> bool:
        return _topic_key(brief.topic) == _topic_key(self.brief.topic)

    def complete(self) -> bool:
        return (self.root / "plan.json").is_file() and (self.root / "research.json").is_file()

    def meta(self) -> dict[str, Any]:
        path = self.root / "meta.json"
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                return {}
        return {}

    def plan(self) -> Plan:
        return Plan.model_validate_json((self.root / "plan.json").read_text(encoding="utf-8"))

    def research(self) -> ResearchPack:
        return ResearchPack.model_validate_json((self.root / "research.json").read_text(encoding="utf-8"))

    def _latest(self, folder: str, channel: str, round: int) -> Path | None:
        for n in range(round, -1, -1):
            path = self.root / folder / f"{channel}.r{n}.json"
            if path.is_file():
                return path
        return None

    def draft(self, channel: str, round: int) -> Draft | None:
        path = self._latest("drafts", channel, round)
        return Draft.model_validate_json(path.read_text(encoding="utf-8")) if path else None

    def review(self, channel: str, round: int) -> Review | None:
        path = self._latest("reviews", channel, round)
        return Review.model_validate_json(path.read_text(encoding="utf-8")) if path else None

    def followup(self, channel: str, round: int) -> ResearchPack | None:
        path = self.root / "followups" / f"{channel}.r{round}.json"
        return ResearchPack.model_validate_json(path.read_text(encoding="utf-8")) if path.is_file() else None


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


def _query_from_question(question: str) -> str:
    text = re.sub(r"[()\[\]?？,.·]", " ", question)
    return _clip(re.sub(r"\s+", " ", text), 40)


class MockBackend:
    name = "mock"

    def __init__(self, settings: Settings, sample_dir: Path | None = None) -> None:
        self.settings = settings
        self.sample = SampleRun.load(sample_dir if sample_dir is not None else settings.sample_dir)
        self.model = TEMPLATE_MODEL
        self.on_notice: NoticeFn | None = None
        self.on_usage: UsageFn | None = None
        self.context = RunContext(today=settings.today)
        self._last_review_round: dict[str, int] = {}

    @property
    def today(self) -> str:
        return (self.context.today if self.context is not None else "") or self.settings.today

    @property
    def profile(self) -> Profile | None:
        profile = self.context.profile if self.context is not None else None
        return None if profile_is_empty(profile) else profile

    # -- source selection ----------------------------------------------------
    def replaying(self, brief: Brief) -> bool:
        return self.sample is not None and self.sample.matches(brief) and self.sample.complete()

    def prepare(self, brief: Brief) -> str:
        """Pick replay vs template for this brief; returns a Korean log line."""
        if self.replaying(brief):
            assert self.sample is not None
            self.model = str(self.sample.meta().get("model") or self.settings.model)
            note = f"샘플 브리프와 같아서 기록된 실행({self.sample.root.name})을 재생해요"
            if self.profile is not None or (self.context is not None and self.context.documents):
                note += ". 기록을 그대로 재생하므로 회사 프로필과 사용자 자료는 반영되지 않아요"
            return note
        self.model = TEMPLATE_MODEL
        if self.sample is not None and self.sample.matches(brief):
            return "샘플 기록이 아직 완성되지 않아 [데모] 템플릿으로 만들어요"
        return "[데모] 템플릿으로 예시 콘텐츠를 만들어요. 수치는 ○○ 자리표시이고, live 모드에서 실제 리서치로 채워져요"

    def sim_seconds(self, kind: str, channel: str | None = None, round: int = 0) -> float:
        return sim_seconds(kind, channel, round)

    # -- usage / notices ---------------------------------------------------------
    def _notice(self, agent: str, level: str, message: str) -> None:
        if self.on_notice is not None:
            try:
                self.on_notice(agent, level, message)
            except Exception:
                pass

    def _usage(self, agent: str, task: str, prompt: Any, output: Any) -> None:
        """Synthetic, free usage (tokens estimated from the text length)."""
        if self.on_usage is None:
            return
        record = mock_usage(agent=agent, task=task, prompt=_as_text(prompt), output=_as_text(output))
        try:
            self.on_usage(record)
        except BackendError:
            raise
        except Exception as exc:  # a broken recorder must not fail the run
            self._notice("system", "warn", f"사용량 기록 중 오류가 나서 이번 호출 사용량을 저장하지 못했어요: {exc}")

    # -- Backend protocol ------------------------------------------------------
    def plan(self, brief: Brief) -> Plan:
        if self.replaying(brief):
            assert self.sample is not None
            plan = self.sample.plan()
            wanted = set(brief.channels)
            missing = [c for c in brief.channels if c not in {o.channel for o in plan.outlines}]
            extra = template_plan(brief.model_copy(update={"channels": missing})).outlines if missing else []
            plan = plan.model_copy(update={"outlines": [o for o in plan.outlines if o.channel in wanted] + extra})
        else:
            plan = template_plan(brief, self.profile)
        self._usage("orchestrator", "plan", [brief, self.profile], plan)
        return plan

    def research(self, brief: Brief, questions: list[ResearchQuestion], emit: EmitFn,
                 existing: ResearchPack | None = None) -> ResearchPack:
        for question in questions:
            emit("research.query", {"question_id": question.id, "query": _query_from_question(question.question)})
        pack, sent = self._research(brief, questions, existing)
        self._usage("researcher", "research", [brief, questions, sent], pack)
        return pack

    def _research(self, brief: Brief, questions: list[ResearchQuestion],
                  existing: ResearchPack | None) -> tuple[ResearchPack, list[str]]:
        if existing is None:
            if self.replaying(brief):
                assert self.sample is not None
                return self.sample.research(), []
            pack = template_research(brief, self.today, self.profile)
            excerpts, notice = budget_documents(list(self.context.documents) if self.context else [],
                                                self.settings.max_document_chars)
            if notice:
                self._notice("researcher", "warn", notice)
            if excerpts:
                s, f = len(pack.sources) + 1, len(pack.findings) + 1
                mine = template_user_research(excerpts, questions or template_plan(brief, self.profile).questions, s, f, self.today)
                pack = ResearchPack(findings=[*pack.findings, *mine.findings], sources=[*pack.sources, *mine.sources],
                                    gaps=pack.gaps)
            return pack, [e.text for e in excerpts]
        # follow-up: recorded file when present, else a labelled placeholder
        if self.replaying(brief):
            assert self.sample is not None
            channel = questions[0].channels[0] if questions and questions[0].channels else ""
            recorded = self.sample.followup(channel, self._last_review_round.get(channel, 0))
            if recorded is not None:
                return recorded, []
            return ResearchPack(findings=[], sources=[], gaps=[f"(기록된 추가 조사 없음) {q.question}" for q in questions]), []
        return template_followup(questions, existing, self.today), []

    def draft(self, brief: Brief, plan: Plan, research: ResearchPack, channel: ChannelId) -> Draft:
        produced = None
        if self.replaying(brief):
            assert self.sample is not None
            recorded = self.sample.draft(channel, 0)
            if recorded is not None:
                produced = recorded.model_copy(update={"channel": channel, "round": 0})
        if produced is None:
            produced = template_draft(brief, research, channel, 0, self.profile)
        self._usage("orchestrator", "draft", [brief, plan, research, self.profile], produced)
        return produced

    def review(self, brief: Brief, research: ResearchPack, draft: Draft, format_checks: list[FormatCheck]) -> Review:
        self._last_review_round[draft.channel] = draft.round  # a follow-up after this review reads followups/<ch>.r<N>
        result = None
        if self.replaying(brief):
            assert self.sample is not None
            recorded = self.sample.review(draft.channel, draft.round)
            if recorded is not None:
                result = recorded.model_copy(update={"channel": draft.channel, "round": draft.round,
                                                     "format_checks": list(format_checks)})
        if result is None:
            result = template_review(brief, research, draft, format_checks)
        self._usage("reviewer", "review", [brief, research, draft, format_checks, self.profile], result)
        return result

    def revise(self, brief: Brief, plan: Plan, research: ResearchPack, draft: Draft, review: Review,
               instructions: str = "") -> Draft:
        next_round = draft.round + 1
        human = (instructions or (self.context.instructions if self.context is not None else "") or "").strip()
        produced = None
        if self.replaying(brief):
            assert self.sample is not None
            recorded = self.sample.draft(draft.channel, next_round)
            if recorded is not None:
                produced = recorded.model_copy(update={"channel": draft.channel, "round": next_round})
        if produced is None:
            produced = template_draft(brief, research, draft.channel, next_round, self.profile)
        if human:
            note = f"[사람 지시] {_clip(human, 100)} — [데모] mock 모드라 지시는 기록만 했어요(live 모드에서 실제로 반영)"
            produced = produced.model_copy(update={"change_log": [note, *produced.change_log]})
        self._usage("orchestrator", "revise", [brief, plan, research, draft, review, human, self.profile], produced)
        return produced

    def plan_calendar(self, profile: Profile, theme: str, start: str, end: str, counts: dict[str, int],
                      history: list[ContentItem]) -> ContentPlan:
        plan = template_calendar(profile, theme, start, end, counts, history)
        self._usage("orchestrator", "plan_calendar", [profile, theme, start, end, counts, history], plan)
        return plan


def _as_text(value: Any) -> str:
    """Text used to estimate synthetic token counts."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(_as_text(v) for v in value)
    if hasattr(value, "model_dump_json"):
        return value.model_dump_json()
    return json.dumps(value, ensure_ascii=False, default=str)
