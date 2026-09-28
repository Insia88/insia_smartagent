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
    origin: Literal["web", "user"] = Field(
        default="web",
        description="web=웹에서 찾은 출처, user=사용자가 올린 자료(회사 소개서·IR 자료 등, url은 user://<문서 id>)",
    )


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


# ---------------------------------------------------------------------------
# Workspace (production use): company profile, user materials, content items
# ---------------------------------------------------------------------------
# These are stored by ``insia_agents.db.Workspace`` (SQLite). ``ContentPlan``
# and ``PlannedSlot`` are LLM-produced and must stay structured-output
# compatible; the others are not sent as output schemas.

ContentStatus = Literal["draft", "needs_changes", "approved", "scheduled", "published", "archived"]
DraftSource = Literal["agent", "human"]
SlotStatus = Literal["planned", "generating", "drafted", "skipped"]


class TeamMember(BaseModel):
    role: str = Field(description="역할 (예: 대표, CTO, 마케팅 담당)")
    name: str = Field(default="", description="실명. 사업계획서에는 블라인드 규정 때문에 절대 쓰지 않는다")
    background: str = Field(default="", description="학위·전공, 경력, 보유 역량 (사용자 제공 사실)")
    hiring: bool = Field(default=False, description="채용 예정 인력이면 true")


class Profile(BaseModel):
    """회사·브랜드 프로필. 모든 에이전트가 참고하는 사용자 제공 사실과 브랜드 규칙."""

    company_name: str = ""
    service_name: str = ""
    one_liner: str = Field(default="", description="한 줄 소개")
    description: str = Field(default="", description="서비스 설명")
    industry: str = ""
    stage: str = Field(default="", description="예: 예비창업, 초기(3년 이내), 도약")
    target_customers: str = ""
    problem: str = ""
    solution: str = ""
    differentiators: list[str] = Field(default_factory=list)
    business_model: str = ""
    pricing: str = Field(default="", description="확정 가격이 아니면 '가정'이라고 적는다")
    traction: list[str] = Field(default_factory=list, description="사용자 제공 실적·지표 (예: 베타 사용자 120명, 2026-08 기준)")
    team: list[TeamMember] = Field(default_factory=list)
    tone: str = Field(default="", description="브랜드 톤앤매너")
    banned_words: list[str] = Field(default_factory=list, description="쓰면 안 되는 표현")
    required_phrases: list[str] = Field(default_factory=list, description="반드시 넣을 문구 (예: 광고 표시, 면책 문구)")
    default_hashtags: list[str] = Field(default_factory=list)
    cta: str = Field(default="", description="기본 행동 유도 문구 (예: 무료 체험 신청은 프로필 링크에서)")
    contact: str = Field(default="", description="문의처 (이메일·네이버 톡톡 등)")
    naver_blog_url: str = ""
    linkedin_url: str = ""
    instagram_handle: str = ""
    brand_colors: list[str] = Field(default_factory=list, description="카드뉴스용 브랜드 색 (#RRGGBB), 첫 번째가 주 색")
    notes: str = ""
    updated_at: str = ""


class UserDocument(BaseModel):
    """사용자가 올린 참고 자료. 리서치 팩에 origin='user' 출처로 들어간다."""

    id: str = Field(description='"u1", "u2", ...')
    title: str
    kind: Literal["text", "markdown", "pdf", "docx"] = "text"
    filename: str = ""
    text: str
    chars: int = 0
    created_at: str = ""


class ContentItem(BaseModel):
    """채널 산출물 하나 (예: 이번 주 링크드인 게시물). 버전·검수·승인·게시 상태를 가진다."""

    id: str
    run_id: str = ""
    channel: ChannelId
    title: str
    status: ContentStatus = "draft"
    version: int = Field(default=1, description="현재 버전 번호 (1부터)")
    score: int | None = None
    passed: bool | None = None
    scheduled_at: str = Field(default="", description="게시 예정일 YYYY-MM-DD 또는 ISO 시각")
    published_at: str = ""
    published_url: str = ""
    note: str = ""
    created_at: str = ""
    updated_at: str = ""
    # The last approval (kept after publishing as an audit record; 0/None/"" = never approved).
    approved_version: int = Field(default=0, description="승인한 버전 번호 (0 = 승인한 적 없음)")
    approval_forced: bool = Field(default=False, description="검수를 통과하지 못한 버전을 사람이 '그래도 승인'했으면 true")
    approved_score: int | None = Field(default=None, description="승인한 버전의 검수 점수 (검수 전이면 None)")
    approved_at: str = Field(default="", description="승인한 시각 (UTC ISO)")


class DraftVersion(BaseModel):
    id: str
    item_id: str
    version: int
    source: DraftSource
    draft: Draft
    review: Review | None = None
    instructions: str = Field(default="", description="사람이 준 수정 지시 (있을 때)")
    created_at: str = ""


class ContentItemDetail(BaseModel):
    item: ContentItem
    versions: list[DraftVersion]
    brief: Brief | None = None


class UsageRecord(BaseModel):
    run_id: str = ""
    agent: str = ""
    task: str = Field(default="", description="plan, research, draft, review, revise, plan_calendar ...")
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    web_search_requests: int = 0
    cost_usd: float = 0.0
    created_at: str = ""


class PlannedSlot(BaseModel):
    date: str = Field(description="게시 예정일 YYYY-MM-DD")
    channel: ChannelId
    topic: str = Field(description="이 게시물의 주제 한 줄")
    angle: str = Field(description="관점·형식 (예: 체크리스트, 사례, 데이터 해설)")
    keywords: list[str] = Field(description="핵심 키워드 1~5개, 첫 번째가 메인")
    goal: str = Field(description="이 게시물로 얻으려는 것 (인지, 문의, 저장 등)")


class ContentPlan(BaseModel):
    summary: str = Field(description="이번 기간 콘텐츠 전략 2~3문장")
    slots: list[PlannedSlot]


class CalendarSlot(BaseModel):
    id: str
    date: str
    channel: ChannelId
    topic: str
    angle: str = ""
    keywords: list[str] = Field(default_factory=list)
    goal: str = ""
    status: SlotStatus = "planned"
    item_id: str = ""
    run_id: str = ""
    created_at: str = ""
