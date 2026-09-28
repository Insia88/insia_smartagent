"""총괄 에이전트 (orchestrator): plan, draft, revise and channel wrap-up."""

from __future__ import annotations

from ..channels import chars_no_space, chars_with_space
from ..models import ChannelId, ChannelResult, Draft, Plan, ResearchPack, Review
from .common import AgentContext, Step, channel_label, excerpt

AGENT = "orchestrator"


def plan(ctx: AgentContext) -> Step[Plan]:
    ctx.status(AGENT, "planning", "브리프를 읽고 작업 계획을 세우는 중이에요")
    result = ctx.backend.plan(ctx.brief)
    yield ctx.pace("plan")
    ctx.bus.emit("plan.created", AGENT, {
        "summary": result.summary,
        "key_messages": result.key_messages,
        "questions": result.questions,
        "outlines": result.outlines,
    })
    return result


def delegate_research(ctx: AgentContext, result: Plan) -> Step[None]:
    ctx.handoff(AGENT, "researcher", "task", f"리서치 질문 {len(result.questions)}개 전달")
    ctx.status(AGENT, "waiting", "리서치 결과를 기다리는 중이에요")
    yield ctx.pace("handoff")


def _emit_draft(ctx: AgentContext, draft: Draft) -> None:
    ctx.bus.emit("draft.created", AGENT, {
        "channel": draft.channel,
        "round": draft.round,
        "title": draft.title,
        "chars": chars_with_space(draft.content),
        "chars_no_space": chars_no_space(draft.content),
        "excerpt": excerpt(draft.content),
        "hashtags": draft.hashtags,
        "change_log": draft.change_log,
    })


def draft(ctx: AgentContext, result: Plan, research: ResearchPack, channel: ChannelId) -> Step[Draft]:
    ctx.status(AGENT, "writing", f"{channel_label(channel)} 초안을 쓰는 중이에요")
    produced = ctx.backend.draft(ctx.brief, result, research, channel)
    produced = produced.model_copy(update={"channel": channel, "round": 0})
    yield ctx.pace("draft", channel)
    _emit_draft(ctx, produced)
    return produced


def revise(ctx: AgentContext, result: Plan, research: ResearchPack, current: Draft, review: Review) -> Step[Draft]:
    next_round = current.round + 1
    ctx.status(AGENT, "revising", f"검수 의견을 반영해 {channel_label(current.channel)} 수정본(R{next_round})을 쓰는 중이에요")
    produced = ctx.backend.revise(ctx.brief, result, research, current, review)
    produced = produced.model_copy(update={"channel": current.channel, "round": next_round})
    yield ctx.pace("revise", current.channel, next_round)
    _emit_draft(ctx, produced)
    return produced


def complete_channel(ctx: AgentContext, outcome: ChannelResult, score: int) -> None:
    final = outcome.final
    ctx.bus.emit("channel.completed", AGENT, {
        "channel": outcome.channel,
        "passed": outcome.passed,
        "score": score,
        "rounds": outcome.rounds,
        "title": final.title,
        "content": final.content,
        "hashtags": final.hashtags,
    })
