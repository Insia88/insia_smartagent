"""총괄 에이전트 (orchestrator): plan, draft, revise and channel wrap-up."""

from __future__ import annotations

import inspect
from typing import Any

from ..channels import chars_no_space, chars_with_space
from ..models import ChannelId, ChannelResult, Draft, Plan, ResearchPack, Review
from .common import AgentContext, Step, channel_label, excerpt

AGENT = "orchestrator"


def plan(ctx: AgentContext) -> Step[Plan]:
    ctx.status(AGENT, "planning", "브리프를 읽고 작업 계획을 세우는 중이에요")
    result = ctx.call(AGENT, "작업 계획", ctx.backend.plan, ctx.brief)
    yield ctx.pace("plan")
    emit_plan(ctx, result)
    return result


def emit_plan(ctx: AgentContext, result: Plan) -> None:
    ctx.bus.emit("plan.created", AGENT, {
        "summary": result.summary,
        "key_messages": result.key_messages,
        "questions": result.questions,
        "outlines": result.outlines,
    })


def delegate_research(ctx: AgentContext, result: Plan) -> Step[None]:
    ctx.handoff(AGENT, "researcher", "task", f"리서치 질문 {len(result.questions)}개 전달")
    ctx.status(AGENT, "waiting", "리서치 결과를 기다리는 중이에요")
    yield ctx.pace("handoff")


def emit_draft(ctx: AgentContext, draft: Draft, agent: str = AGENT, **extra: Any) -> None:
    """``draft.created`` for a new draft (``agent="system"`` for a human edit)."""
    ctx.bus.emit("draft.created", agent, {
        "channel": draft.channel,
        "round": draft.round,
        "title": draft.title,
        "chars": chars_with_space(draft.content),
        "chars_no_space": chars_no_space(draft.content),
        "excerpt": excerpt(draft.content),
        "hashtags": draft.hashtags,
        "change_log": draft.change_log,
        **extra,
    })


_emit_draft = emit_draft  # backwards-compatible alias


def draft(ctx: AgentContext, result: Plan, research: ResearchPack, channel: ChannelId) -> Step[Draft]:
    label = channel_label(channel)
    ctx.status(AGENT, "writing", f"{label} 초안을 쓰는 중이에요")
    produced = ctx.call(AGENT, f"{label} 초안 작성", ctx.backend.draft, ctx.brief, result, research, channel)
    produced = produced.model_copy(update={"channel": channel, "round": 0})
    yield ctx.pace("draft", channel)
    emit_draft(ctx, produced)
    return produced


def _accepts_instructions(method: Any) -> bool:
    try:
        params = inspect.signature(method).parameters.values()
    except (TypeError, ValueError):  # builtins / C callables: just try it
        return True
    return any(p.name == "instructions" or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


def call_revise(ctx: AgentContext, result: Plan, research: ResearchPack, current: Draft, review: Review,
                instructions: str = "") -> Draft:
    """``backend.revise`` with human ``instructions`` when there are any.

    Older backends without the ``instructions`` keyword still work: they get
    the call without it (the instructions also sit in ``backend.context``).
    """
    revise = ctx.backend.revise
    if instructions.strip():
        if _accepts_instructions(revise):
            try:
                return revise(ctx.brief, result, research, current, review, instructions=instructions)
            except TypeError as exc:
                if "instructions" not in str(exc):
                    raise
        ctx.log("이 백엔드는 수정 지시를 따로 받지 않아서, 검수 의견 중심으로 고쳐요", "warn", AGENT)
    return revise(ctx.brief, result, research, current, review)


def revise(ctx: AgentContext, result: Plan, research: ResearchPack, current: Draft, review: Review,
           instructions: str = "") -> Step[Draft]:
    next_round = current.round + 1
    label = channel_label(current.channel)
    if instructions.strip():
        ctx.status(AGENT, "revising", f"수정 지시와 검수 의견을 반영해 {label} 수정본(R{next_round})을 쓰는 중이에요")
    else:
        ctx.status(AGENT, "revising", f"검수 의견을 반영해 {label} 수정본(R{next_round})을 쓰는 중이에요")
    produced = ctx.call(AGENT, f"{label} 수정", call_revise, ctx, result, research, current, review, instructions)
    produced = produced.model_copy(update={"channel": current.channel, "round": next_round})
    yield ctx.pace("revise", current.channel, next_round)
    emit_draft(ctx, produced)
    return produced


def complete_channel(ctx: AgentContext, outcome: ChannelResult, score: int) -> None:
    final = outcome.final
    ctx.bus.emit("channel.completed", AGENT, {
        "channel": outcome.channel,
        "passed": outcome.passed,
        "score": score,
        "rounds": outcome.rounds,
        "final_round": final.round,
        "title": final.title,
        "content": final.content,
        "hashtags": final.hashtags,
    })
