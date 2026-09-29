"""Grade one channel result of one case: format/brand checks, grounding, placeholders, process, assertions.

Everything here is deterministic and offline: the same draft, research pack
and case always get the same grade. The optional LLM judge lives in
``judge.py`` and is never called from here.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..channels import check_format
from ..models import ChannelResult, Draft, Review, ResearchPack
from .cases import Assertion, EvalCase, format_number
from .grounding import Evidence, Mention, analyze_numbers, contains, grounding_summary, hedged, placeholders

MONEY_UNITS = ("원",)
DEFAULT_ASSUMPTION_MARKERS = ("가정",)
MAX_NUMBERS_IN_RESULT = 400


def final_review(result: ChannelResult) -> Review | None:
    """The review of the draft the pipeline chose as final (same rule as ``storage.save_run``)."""
    return next((r for r in result.reviews if r.round == result.final.round), result.reviews[-1] if result.reviews else None)


def grade_channel(case: EvalCase, result: ChannelResult, research: ResearchPack | None, evidence: Evidence) -> dict[str, Any]:
    """Every deterministic measurement of one channel's final draft (no pass/fail yet)."""
    draft = result.final
    review = final_review(result)
    checks = check_format(draft, case.brief, case.profile)
    mentions = analyze_numbers(draft, evidence)
    issues = review.issues if review is not None else []
    return {
        "channel": result.channel,
        "status": "ok",
        "title": draft.title,
        "chars": len(draft.content.strip()),
        "score": review.score if review is not None else None,
        "passed": bool(result.passed),
        "rounds": result.rounds,
        "critical": sum(1 for i in issues if i.severity == "critical"),
        "major": sum(1 for i in issues if i.severity == "major"),
        "issues": [{"severity": i.severity, "location": i.location, "problem": i.problem} for i in issues][:12],
        "review_summary": review.summary if review is not None else "",
        "checks": [c.model_dump(mode="json") for c in checks],
        "grounding": {**grounding_summary(mentions),
                      "numbers": [m.as_dict() for m in mentions if m.kind == "claim"][:MAX_NUMBERS_IN_RESULT]},
        "placeholders": placeholders(draft),
        "draft": draft.model_dump(mode="json"),
        "_mentions": mentions,  # dropped before writing (Mention objects)
    }


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


def _outcome(assertion: Assertion, passed: bool, detail: str, *, value: Any = None) -> dict[str, Any]:
    out = {"key": assertion.key, "type": assertion.type, "label": assertion.label(), "passed": bool(passed),
           "detail": detail}
    if value is not None:
        out["value"] = value
    if assertion.note:
        out["note"] = assertion.note
    return out


def _money_violations(mentions: Iterable[Mention], units: tuple[str, ...], markers: tuple[str, ...]) -> list[Mention]:
    """Figures in ``units`` that research/documents do not firmly support and that ``markers`` do not mark.

    A value the user's document itself leaves open ("월 29,000원 … 검토 중이며 확정 전") is not settled evidence: the
    draft still has to call it an assumption. Markers are matched as words (``가정용`` is not ``가정``) and plan words
    only when attached to the figure (see ``grounding.hedged``)."""
    bad = []
    for m in mentions:
        if m.kind != "claim" or m.unit not in units:
            continue
        if any(o.startswith(("research:", "document:")) for o in m.firm_origins):
            continue
        if not hedged(m, markers):
            bad.append(m)
    return bad


def evaluate_channel(assertion: Assertion, grade: dict[str, Any]) -> dict[str, Any]:
    """Pass/fail of one channel-level assertion against a ``grade_channel`` result."""
    kind = assertion.type
    checks = {c["id"]: c for c in grade["checks"]}
    draft = Draft.model_validate(grade["draft"])
    g = grade["grounding"]
    value = assertion.value
    if kind == "checks_pass":
        missing = [c for c in assertion.checks if c not in checks]
        failed = [f"{checks[c]['label']}({checks[c]['value']})" for c in assertion.checks if c in checks and not checks[c]["passed"]]
        if missing:
            return _outcome(assertion, False, "검사가 실행되지 않았어요: " + ", ".join(missing))
        return _outcome(assertion, not failed, "실패: " + ", ".join(failed) if failed else "모두 통과")
    if kind == "all_checks_pass":
        failed = [f"{c['label']}({c['value']})" for c in grade["checks"] if not c["passed"]]
        return _outcome(assertion, not failed, "실패: " + ", ".join(failed) if failed else f"{len(checks)}개 모두 통과")
    if kind == "not_contains":
        found = [v for v in assertion.values if contains(draft, v)]
        return _outcome(assertion, not found, "들어감: " + ", ".join(found) if found else "없음")
    if kind == "contains":
        missing = [v for v in assertion.values if not contains(draft, v)]
        return _outcome(assertion, not missing, "빠짐: " + ", ".join(missing) if missing else "모두 있음")
    if kind == "max_unsupported_numbers":
        bad = [n for n in g["numbers"] if n["status"] == "unsupported"]
        shown = ", ".join(n["text"] + (" (자료에선 확정 전인 값)" if n.get("tentative") else "") for n in bad[:6]) + \
            (" …" if len(bad) > 6 else "")
        return _outcome(assertion, len(bad) <= value, f"{len(bad)}개 (기준 {format_number(value)}개 이하)" +
                        (f": {shown}" if bad else ""), value=len(bad))
    if kind == "min_placeholders":
        count = grade["placeholders"]["count"]
        return _outcome(assertion, count >= value, f"{count}개 (기준 {format_number(value)}개 이상)", value=count)
    if kind == "assumption_marked":
        units = tuple(assertion.units) or MONEY_UNITS
        markers = tuple(assertion.markers) or DEFAULT_ASSUMPTION_MARKERS
        bad = _money_violations(grade["_mentions"], units, markers)
        shown = ", ".join(m.text + (" (자료에선 확정 전인 값)" if m.tentative else "") for m in bad[:6])
        return _outcome(assertion, not bad, f"'{'/'.join(markers)}' 표시 없는 수치 {len(bad)}개: {shown}" if bad else
                        f"리서치·자료로 확정되지 않은 {'/'.join(units)} 수치는 모두 '{'/'.join(markers)}' 표시가 있어요")
    if kind == "min_citation_coverage":
        coverage = g["citation_coverage"]
        if coverage is None:
            return _outcome(assertion, True, "출처가 필요한 수치가 없어요")
        return _outcome(assertion, coverage >= value, f"{coverage:.0%} (기준 {value:.0%} 이상, 수치 {g['factual']}개 중 "
                        f"{g['cited']}개)", value=coverage)
    if kind == "min_score":
        score = grade["score"]
        return _outcome(assertion, score is not None and score >= value, f"{score}점 (기준 {format_number(value)}점 이상)",
                        value=score)
    if kind == "reviewer_passed":
        return _outcome(assertion, bool(grade["passed"]), "통과" if grade["passed"] else f"미통과 ({grade['score']}점)")
    if kind == "max_rounds":
        return _outcome(assertion, grade["rounds"] <= value, f"{grade['rounds']}회 (기준 {format_number(value)}회 이하)",
                        value=grade["rounds"])
    if kind == "max_critical_issues":
        return _outcome(assertion, grade["critical"] <= value, f"{grade['critical']}개 (기준 {format_number(value)}개 이하)",
                        value=grade["critical"])
    raise ValueError(f"channel assertion {kind!r} cannot be evaluated here")  # pragma: no cover - validated earlier


def evaluate_case_level(assertion: Assertion, *, cost_usd: float, duration_s: float) -> dict[str, Any]:
    value = assertion.value or 0.0
    if assertion.type == "max_cost_usd":
        return _outcome(assertion, cost_usd <= value, f"${cost_usd:,.4f} (기준 ${value:,.2f} 이하)", value=round(cost_usd, 6))
    if assertion.type == "max_duration_s":
        return _outcome(assertion, duration_s <= value, f"{duration_s:,.1f}초 (기준 {format_number(value)}초 이하)",
                        value=round(duration_s, 1))
    raise ValueError(f"case assertion {assertion.type!r} cannot be evaluated here")  # pragma: no cover


def not_evaluated(assertion: Assertion, reason: str) -> dict[str, Any]:
    """A channel that produced no result (run error, budget stop): the assertion cannot be judged."""
    out = _outcome(assertion, False, reason)
    out["error"] = True
    return out


def apply_assertions(case: EvalCase, grade: dict[str, Any], mode: str) -> None:
    """Fill ``grade['must']`` / ``grade['should']`` for this channel."""
    for kind in ("must", "should"):
        grade[kind] = [evaluate_channel(a, grade) for a in case.assertions(kind) if a.applies(grade["channel"], mode)]


def public(grade: dict[str, Any]) -> dict[str, Any]:
    """The JSON-serializable part of a grade."""
    return {k: v for k, v in grade.items() if not k.startswith("_")}
