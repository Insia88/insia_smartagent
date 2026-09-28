"""검수 에이전트 (reviewer): independent QA; code finalizes the verdict."""

from __future__ import annotations

from ..channels import check_format, finalize_review
from ..models import Draft, ResearchPack, Review
from .common import AgentContext, Step, channel_label

AGENT = "reviewer"


def review(ctx: AgentContext, research: ResearchPack, draft: Draft) -> Step[Review]:
    label = channel_label(draft.channel)
    ctx.handoff("orchestrator", AGENT, "task", f"{label} 초안 R{draft.round} 검수 요청", draft.channel)
    yield ctx.pace("handoff")
    ctx.status(AGENT, "reviewing", f"{label} R{draft.round} 초안을 검수하는 중이에요")
    ctx.bus.emit("review.started", AGENT, {"channel": draft.channel, "round": draft.round})
    checks = check_format(draft, ctx.brief)
    raw = ctx.backend.review(ctx.brief, research, draft, checks)
    yield ctx.pace("review", draft.channel, draft.round)
    final = finalize_review(raw, draft, ctx.brief, pass_score=ctx.settings.pass_score)
    verdicts = {"supported": 0, "unsupported": 0, "needs_source": 0}
    for fact in final.fact_checks:
        verdicts[fact.verdict] = verdicts.get(fact.verdict, 0) + 1
    ctx.bus.emit("review.completed", AGENT, {
        "channel": final.channel,
        "round": final.round,
        "score": final.score,
        "passed": final.passed,
        "rubric": final.rubric,
        "issues": final.issues,
        "format_checks": final.format_checks,
        "fact_checks": verdicts,
        "needs_research": final.needs_research,
        "summary": final.summary,
    })
    return final


def top_issue(result: Review) -> str:
    for severity in ("critical", "major", "minor"):
        for issue in result.issues:
            if issue.severity == severity:
                return issue.problem
    failed = [c for c in result.format_checks if not c.passed]
    if failed:
        return f"{failed[0].label}: {failed[0].value} (기준 {failed[0].expected})"
    return f"점수 {result.score}점으로 통과 기준 미달"


def request_revision(ctx: AgentContext, result: Review) -> Step[None]:
    ctx.bus.emit("revision.requested", AGENT, {
        "channel": result.channel,
        "round": result.round,
        "issues": len(result.issues),
        "top_issue": top_issue(result),
    })
    ctx.handoff(AGENT, "orchestrator", "feedback", f"{channel_label(result.channel)} 수정 요청 {len(result.issues)}건", result.channel)
    ctx.status(AGENT, "waiting", "수정본을 기다리는 중이에요")
    yield ctx.pace("handoff")


def conclude(ctx: AgentContext, result: Review) -> Step[None]:
    label = channel_label(result.channel)
    text = f"{label} 통과 ({result.score}점)" if result.passed else f"{label} 최대 수정 횟수 도달 ({result.score}점)"
    ctx.handoff(AGENT, "orchestrator", "result", text, result.channel)
    ctx.status(AGENT, "waiting", "다음 초안을 기다리는 중이에요")
    yield ctx.pace("handoff")
