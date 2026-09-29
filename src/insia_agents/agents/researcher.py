"""리서치 에이전트 (researcher): initial research and reviewer-requested follow-ups."""

from __future__ import annotations

import re
import threading
from collections import defaultdict
from typing import Any

from ..backends.base import followup_questions, merge_research
from ..models import ChannelId, ResearchPack, ResearchQuestion
from .common import AgentContext, Step, channel_label

AGENT = "researcher"
BACKEND_EVENT_TYPES = frozenset({"research.query", "agent.status", "log"})


class ResearchStore:
    """The run's research pack. Follow-ups from concurrent channels merge here
    under a lock so ids stay continuous (``s1..``, ``f1..``)."""

    def __init__(self) -> None:
        self.pack = ResearchPack(findings=[], sources=[], gaps=[])
        self.questions: list[ResearchQuestion] = []
        self.lock = threading.Lock()

    def snapshot(self) -> ResearchPack:
        with self.lock:
            return self.pack.model_copy(deep=True)


def _drop_unknown_source_ids(raw: ResearchPack, existing: ResearchPack | None) -> ResearchPack:
    """Keep only source ids the backend could have meant: its own new sources or
    the snapshot it was given. Another channel may merge sources while this call
    runs; without this, a dangling id in ``raw`` could bind to one of those."""
    valid = {s.id for s in raw.sources} | ({s.id for s in existing.sources} if existing else set())
    if all(sid in valid for f in raw.findings for sid in f.source_ids):
        return raw
    findings = [f.model_copy(update={"source_ids": [sid for sid in f.source_ids if sid in valid]}) for f in raw.findings]
    return raw.model_copy(update={"findings": findings})


def run(ctx: AgentContext, store: ResearchStore, questions: list[ResearchQuestion], *,
        followup: bool = False, channel: ChannelId | None = None) -> Step[ResearchPack]:
    """Research ``questions`` and merge into ``store``; returns the new items only."""
    if followup:
        ctx.status(AGENT, "searching", f"{channel_label(channel or '')} 검수에서 요청한 추가 조사를 시작해요")
    else:
        ctx.status(AGENT, "searching", f"질문 {len(questions)}개로 웹 검색을 시작해요")
    yield ctx.pace("research_start")

    buffered: list[tuple[str, dict[str, Any]]] = []

    def emit(event_type: str, data: dict[str, Any]) -> None:
        if event_type not in BACKEND_EVENT_TYPES:
            return
        if ctx.simulated:
            buffered.append((event_type, data))
        else:  # live: show searches as they happen
            ctx.bus.emit(event_type, AGENT, data)

    # The backend call runs without the lock: a live follow-up search can take minutes and the
    # other channels need store.snapshot() meanwhile. merge_research renumbers against whatever
    # the pack holds by the time the call returns, so ids stay continuous.
    existing = store.snapshot() if followup else None
    what = f"{channel_label(channel or '')} 추가 조사" if followup else "웹 리서치"
    raw = ctx.call(AGENT, what, ctx.backend.research, ctx.brief, questions, emit, existing=existing)
    raw = _drop_unknown_source_ids(raw, existing)
    with store.lock:  # no yields while holding the lock (the mock runner is single-threaded)
        merged, added = merge_research(store.pack, raw)
        store.pack = merged
        if not followup:  # follow-up questions were recorded by followup() when their ids were allocated
            store.questions.extend(questions)

    queries: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event_type, data in buffered:
        if event_type == "research.query":
            queries[str(data.get("question_id", ""))].append(data)
        else:
            ctx.bus.emit(event_type, AGENT, data)

    sources = {s.id: s for s in added.sources}
    emitted: set[str] = set()

    def emit_finding(finding) -> Step[None]:
        for sid in finding.source_ids:
            if sid in sources and sid not in emitted:
                emitted.add(sid)
                ctx.bus.emit("research.source", AGENT, {"source": sources[sid]})
                yield ctx.pace("research_source")
        ctx.bus.emit("research.finding", AGENT, {"finding": finding})
        yield ctx.pace("research_finding")

    reading_announced = False
    asked = {q.id for q in questions}
    for question in questions:
        for data in queries.pop(question.id, []):
            ctx.bus.emit("research.query", AGENT, data)
            yield ctx.pace("research_query")
        for finding in (f for f in added.findings if f.question_id == question.id):
            if not reading_announced:
                ctx.status(AGENT, "reading", "원문을 확인하며 근거를 정리하는 중이에요")
                reading_announced = True
            yield from emit_finding(finding)
    for rest in queries.values():  # queries the backend could not map to a question
        for data in rest:
            ctx.bus.emit("research.query", AGENT, data)
            yield ctx.pace("research_query")
    for finding in (f for f in added.findings if f.question_id not in asked):
        yield from emit_finding(finding)
    for sid, source in sources.items():  # cited by nothing new, still part of the pack
        if sid not in emitted:
            emitted.add(sid)
            ctx.bus.emit("research.source", AGENT, {"source": source})
            yield ctx.pace("research_source")

    ctx.status(AGENT, "writing", "리서치 팩을 정리하는 중이에요")
    yield ctx.pace("research_structure")
    with store.lock:
        total = store.pack
        counts = {"findings": len(total.findings), "sources": len(total.sources), "gaps": list(total.gaps)}
    ctx.bus.emit("research.completed", AGENT, {**counts, "followup": followup})
    if followup:
        ctx.handoff(AGENT, "orchestrator", "result", f"추가 근거 {len(added.findings)}건 전달", channel)
    else:
        ctx.handoff(AGENT, "orchestrator", "result", f"근거 {counts['findings']}건 · 출처 {counts['sources']}곳 전달")
    ctx.status(AGENT, "idle", "추가 조사 요청이 오면 다시 찾아볼게요")
    yield ctx.pace("handoff")
    return added


def _next_question_number(existing: list[ResearchQuestion]) -> int:
    """Number after the highest ``qN`` id (or the count, if larger), so plan ids like q1, q3, q4 give q5."""
    numbers = [int(m.group(1)) for q in existing if (m := re.fullmatch(r"q(\d+)", q.id.strip().lower()))]
    return max(max(numbers, default=0), len(existing)) + 1


def followup(ctx: AgentContext, store: ResearchStore, needs: list[str], channel: ChannelId) -> Step[ResearchPack]:
    # Allocate and record the ids in one locked step: two channels asking for follow-ups at the
    # same time must never get the same question id.
    with store.lock:
        start = _next_question_number(store.questions)
        questions = [q.model_copy(update={"id": f"q{start + i}"})
                     for i, q in enumerate(followup_questions(needs, channel))]
        store.questions.extend(questions)
    if not questions:
        return ResearchPack(findings=[], sources=[], gaps=[])
    ctx.handoff("orchestrator", AGENT, "task", f"{channel_label(channel)} 추가 조사 {len(questions)}건 요청", channel)
    added = yield from run(ctx, store, questions, followup=True, channel=channel)
    return added
