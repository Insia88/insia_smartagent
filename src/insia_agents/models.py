"""Shared data contracts for the INSIA Smart Agent pipeline.

Every agent, backend (live Anthropic / offline mock), the HTTP server, the
recorded demo trace and the dashboard exchange data in these shapes.

Models whose instances are produced by the LLM (``Plan``, ``ResearchPack``,
``Draft``, ``Review`` and their children) must stay compatible with the Claude
structured-outputs JSON-schema subset: no free-form dicts, no recursive
types, no numeric constraints. Use ``insia_agents.schema.output_schema`` (or an
equivalent helper) to turn them into a strict schema before sending.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ChannelId = Literal["bizplan", "naver_blog", "linkedin", "instagram"]
AgentId = Literal["orchestrator", "researcher", "reviewer"]
Priority = Literal["high", "medium", "low"]
Confidence = Literal["high", "medium", "low"]
Severity = Literal["critical", "major", "minor"]
Verdict = Literal["supported", "unsupported", "needs_source"]

ALL_CHANNELS: tuple[ChannelId, ...] = ("bizplan", "naver_blog", "linkedin", "instagram")


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


class Brief(BaseModel):
    """What the user asks the 총괄 에이전트 to produce."""

    topic: str = Field(description="주제 또는 사업 아이템")
    goal: str = Field(default="", description="목적 (예: 예비창업패키지 제출, 서비스 런칭 홍보)")
    audience: str = Field(default="", description="타깃 독자")
    channels: list[ChannelId] = Field(default_factory=lambda: list(ALL_CHANNELS))
    tone: str = Field(default="", description="톤앤매너")
    keywords: list[str] = Field(default_factory=list, description="핵심/SEO 키워드. 첫 번째가 메인 키워드")
    notes: str = Field(default="", description="추가 요구사항, 보유 자료, 팀 정보 등")
    language: str = "ko"


# ---------------------------------------------------------------------------
# Orchestrator: plan
# ---------------------------------------------------------------------------


class ResearchQuestion(BaseModel):
    id: str = Field(description='"q1", "q2", ...')
    question: str
    why: str = Field(description="이 질문이 어떤 산출물의 어느 부분에 필요한지")
    channels: list[ChannelId]
    priority: Priority = "medium"


class ChannelOutline(BaseModel):
    channel: ChannelId
    sections: list[str] = Field(description="산출물의 소제목/섹션 순서")


class Plan(BaseModel):
    summary: str = Field(description="작업 전략 요약 (2~4문장)")
    key_messages: list[str] = Field(description="모든 채널이 공유할 핵심 메시지 3~5개")
    questions: list[ResearchQuestion] = Field(description="리서치 질문 3~6개")
    outlines: list[ChannelOutline]


# ---------------------------------------------------------------------------
# Researcher: research pack
# ---------------------------------------------------------------------------


class Source(BaseModel):
    id: str = Field(description='"s1", "s2", ...')
    title: str
    url: str
    publisher: str = ""
    published: str = Field(default="", description="YYYY, YYYY-MM 또는 YYYY-MM-DD. 모르면 빈 문자열")
    tier: Literal[1, 2, 3] = Field(
        description="1=정부·공공기관·공식통계·법령·기업 공시 원문, 2=언론·리서치기관·업계 보고서, 3=블로그·커뮤니티·기타"
    )
    accessed: str = Field(default="", description="확인한 날짜 YYYY-MM-DD")


class Finding(BaseModel):
    id: str = Field(description='"f1", "f2", ...')
    question_id: str
    claim: str = Field(description="검증 가능한 한 문장. 수치는 단위와 기준 시점을 포함")
    source_ids: list[str]
    confidence: Confidence
    note: str = ""


class ResearchPack(BaseModel):
    findings: list[Finding]
    sources: list[Source]
    gaps: list[str] = Field(default_factory=list, description="찾지 못했거나 불확실한 부분")


# ---------------------------------------------------------------------------
# Orchestrator: drafts
# ---------------------------------------------------------------------------


class Draft(BaseModel):
    channel: ChannelId
    round: int = Field(description="0 = 첫 초안, 1부터 수정본")
    title: str
    content: str = Field(description="채널 가이드의 출력 형식을 따른 마크다운 본문")
    hashtags: list[str] = Field(default_factory=list, description="'#' 포함. 네이버 블로그는 태그 목록")
    used_finding_ids: list[str] = Field(default_factory=list)
    change_log: list[str] = Field(default_factory=list, description="수정본에서 무엇을 고쳤는지")


# ---------------------------------------------------------------------------
# Reviewer: review
# ---------------------------------------------------------------------------


class RubricScore(BaseModel):
    id: str
    label: str
    score: int
    max: int
    comment: str


class ReviewIssue(BaseModel):
    severity: Severity
    location: str = ""
    problem: str
    fix: str


class FactCheck(BaseModel):
    claim: str
    verdict: Verdict
    source_ids: list[str] = Field(default_factory=list)
    note: str = ""


class FormatCheck(BaseModel):
    """Deterministic, code-computed check (see ``channels.check_format``)."""

    id: str
    label: str
    passed: bool
    value: str
    expected: str


class Review(BaseModel):
    channel: ChannelId
    round: int
    score: int = Field(description="0~100. 코드가 루브릭 합계로 다시 계산한다")
    passed: bool = Field(description="코드가 score와 critical 이슈로 다시 판정한다")
    rubric: list[RubricScore]
    issues: list[ReviewIssue]
    fact_checks: list[FactCheck] = Field(default_factory=list)
    format_checks: list[FormatCheck] = Field(default_factory=list)
    needs_research: list[str] = Field(default_factory=list, description="리서치 에이전트에게 되돌려 보낼 추가 조사 질문")
    summary: str


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class ChannelResult(BaseModel):
    channel: ChannelId
    final: Draft
    drafts: list[Draft]
    reviews: list[Review]
    passed: bool
    rounds: int = Field(description="수정 횟수 (첫 초안만 통과하면 0)")


class RunResult(BaseModel):
    run_id: str
    mode: Literal["live", "mock"]
    model: str
    brief: Brief
    plan: Plan
    research: ResearchPack
    results: list[ChannelResult]
    started_at: str
    finished_at: str
