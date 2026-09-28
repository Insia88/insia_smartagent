"""Backend protocol, typed errors and research-pack helpers shared by backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from ..models import (Brief, ChannelId, ContentItem, ContentPlan, Draft, Finding, FormatCheck, Plan, Profile,
                      ResearchPack, ResearchQuestion, Review, Source, UsageRecord, UserDocument)

EmitFn = Callable[[str, dict[str, Any]], None]
NoticeFn = Callable[[str, str, str], None]  # (agent, level, message)
UsageFn = Callable[[UsageRecord], None]


@dataclass
class RunContext:
    """Per-run inputs beyond the brief: the company profile, user materials and
    the reference date. Backends read ``backend.context`` (set before a run)."""

    profile: Profile | None = None
    documents: list[UserDocument] = field(default_factory=list)
    today: str = ""
    instructions: str = ""  # human revision instructions for item jobs


@runtime_checkable
class Backend(Protocol):
    """What the agent layer calls. Backends never emit lifecycle events
    themselves (the agent layer does, so live and mock streams match); the
    researcher may call ``emit`` for ``research.query`` / ``agent.status`` /
    ``log`` while it works.

    Per-run inputs: set ``backend.context = RunContext(profile, documents,
    today, instructions)`` before a run (both backends start with
    ``RunContext(None, [], settings.today)``). The profile is rendered into the
    plan/draft/revise/review/plan_calendar prompts; user documents become
    ``origin="user"`` sources of the first research pack (capped at
    ``settings.max_document_chars`` with a notice when cut).

    Usage: when ``on_usage`` is set it receives a priced ``UsageRecord`` after
    every API response (every ``pause_turn`` continuation too; mock mode sends
    synthetic, free records). Records carry ``run_id=""`` — the callback owner
    knows the run and fills it. An exception raised by the callback that is a
    ``BackendError`` propagates (it stops the step); any other exception is
    reported through ``on_notice`` and ignored, so a broken recorder never
    fails a run.
    """

    name: str  # "live" | "mock"
    model: str
    context: RunContext
    on_usage: UsageFn | None

    def plan(self, brief: Brief) -> Plan: ...

    def research(self, brief: Brief, questions: list[ResearchQuestion], emit: EmitFn,
                 existing: ResearchPack | None = None) -> ResearchPack: ...

    def draft(self, brief: Brief, plan: Plan, research: ResearchPack, channel: ChannelId) -> Draft: ...

    def review(self, brief: Brief, research: ResearchPack, draft: Draft, format_checks: list[FormatCheck]) -> Review: ...

    def revise(self, brief: Brief, plan: Plan, research: ResearchPack, draft: Draft, review: Review,
               instructions: str = "") -> Draft:
        """``instructions``: human revision instructions ("사람의 수정 지시");
        empty → ``context.instructions``."""
        ...

    def plan_calendar(self, profile: Profile, theme: str, start: str, end: str, counts: dict[str, int],
                      history: list[ContentItem]) -> ContentPlan:
        """Content calendar for ``start``..``end`` (YYYY-MM-DD, inclusive) with
        ``counts[channel]`` posts per channel on weekdays, avoiding the topics
        in ``history``. The result is already normalized (see
        ``insia_agents.planner.normalize_plan``)."""
        ...


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BackendError(RuntimeError):
    """Base class. ``str(exc)`` is a Korean message fit for the dashboard."""


class ConfigError(BackendError):
    pass


class RefusalError(BackendError):
    def __init__(self, message: str, *, category: str | None = None, explanation: str | None = None,
                 request_id: str | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.explanation = explanation
        self.request_id = request_id


class OutputTruncatedError(BackendError):
    pass


class InvalidOutputError(BackendError):
    def __init__(self, message: str, *, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw


class APICallError(BackendError):
    """Wraps an ``anthropic`` SDK error (the original is ``__cause__``)."""

    def __init__(self, message: str, *, kind: str, status_code: int | None = None, request_id: str | None = None,
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.request_id = request_id
        self.retryable = retryable


class AuthError(APICallError):
    pass


class RateLimitedError(APICallError):
    pass


class APIConnectionFailed(APICallError):
    pass


class ServerSideError(APICallError):
    pass


class RequestRejectedError(APICallError):
    pass


# ---------------------------------------------------------------------------
# Research helpers
# ---------------------------------------------------------------------------


def _num(prefix: str, value: str) -> int:
    value = value.strip().lower()
    if value.startswith(prefix) and value[len(prefix):].isdigit():
        return int(value[len(prefix):])
    return 0


def next_ids(pack: ResearchPack | None) -> tuple[int, int]:
    """(next source number, next finding number) after ``pack``."""
    if pack is None:
        return 1, 1
    s = max((_num("s", src.id) for src in pack.sources), default=0)
    f = max((_num("f", fin.id) for fin in pack.findings), default=0)
    return max(s, len(pack.sources)) + 1, max(f, len(pack.findings)) + 1


def _norm_url(url: str) -> str:
    return url.strip().rstrip("/").lower()


def merge_research(base: ResearchPack | None, addition: ResearchPack) -> tuple[ResearchPack, ResearchPack]:
    """Append ``addition`` to ``base`` with continuous ids and URL de-duplication.

    Returns ``(merged, new_items)`` where ``new_items`` holds only the sources
    and findings that were actually added (with their final ids) — the agent
    layer emits events for those. Findings whose sources are unknown keep only
    the known ids; findings left without any source are dropped (and noted in
    ``gaps``) because the orchestrator may only use sourced facts.
    """
    base = base or ResearchPack(findings=[], sources=[], gaps=[])
    next_s, next_f = next_ids(base)
    by_url = {_norm_url(s.url): s.id for s in base.sources}
    known_ids = {s.id for s in base.sources}
    id_map: dict[str, str] = {}
    new_sources: list[Source] = []
    for src in addition.sources:
        key = _norm_url(src.url)
        if key in by_url:
            id_map[src.id] = by_url[key]
            continue
        new_id = f"s{next_s}"
        next_s += 1
        by_url[key] = new_id
        id_map[src.id] = new_id
        known_ids.add(new_id)
        new_sources.append(src.model_copy(update={"id": new_id}))

    new_findings: list[Finding] = []
    gaps = list(addition.gaps)
    for fin in addition.findings:
        ids: list[str] = []
        for sid in fin.source_ids:
            mapped = id_map.get(sid, sid if sid in known_ids else None)
            if mapped and mapped not in ids:
                ids.append(mapped)
        if not ids:
            gaps.append(f"출처 없이 제외된 주장: {fin.claim}")
            continue
        new_findings.append(fin.model_copy(update={"id": f"f{next_f}", "source_ids": ids}))
        next_f += 1

    merged_gaps = list(base.gaps)
    for gap in gaps:
        if gap not in merged_gaps:
            merged_gaps.append(gap)
    merged = ResearchPack(
        findings=[*base.findings, *new_findings],
        sources=[*base.sources, *new_sources],
        gaps=merged_gaps,
    )
    return merged, ResearchPack(findings=new_findings, sources=new_sources, gaps=gaps)


def followup_questions(needs: list[str], channel: ChannelId, existing: list[ResearchQuestion] | None = None) -> list[ResearchQuestion]:
    start = len(existing or []) + 1
    return [
        ResearchQuestion(
            id=f"q{start + i}",
            question=text.strip(),
            why=f"검수 에이전트가 {channel} 초안 검수 중 추가 근거를 요청함",
            channels=[channel],
            priority="high",
        )
        for i, text in enumerate(n for n in needs if n.strip())
    ]
