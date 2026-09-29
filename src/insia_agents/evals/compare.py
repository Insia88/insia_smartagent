"""Compare two eval results (baseline → current).

``insia eval compare A B`` (and ``insia eval run --baseline A``) exits 1 when:

- a ``must`` that passed in A fails in B (a regression),
- a case/channel that A graded is missing from B or produced no gradable output,
- the mean reviewer score over the channels both graded dropped by more than
  ``--max-score-drop`` points (default 5; reviewer scores move a few points
  between identical live runs, so a smaller threshold mostly measures noise).

Only what the current run *planned* is compared: a run limited with
``--case``/``--channels`` records its plan (``plan.scope`` in
``summary.json``), and baseline cases/channels outside it are listed as "not
in this run" instead of missing, so a cheap subset run against the last full
baseline passes when nothing it covered got worse. A planned channel that
produced no gradable output (error, budget stop, timeout) still counts as
missing.

``should`` changes, check pass rates, unsupported numbers and cost are
reported but never fail the comparison. Results from different modes (mock
vs live) are compared with a warning: their scores do not mean the same thing.
"""

from __future__ import annotations

import statistics
from typing import Any

from ..agents.common import channel_label


def _index(summary: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for case in summary.get("cases", []):
        rows[(case["id"], "*")] = {"status": case.get("status"), "must": case.get("must", []),
                                   "should": case.get("should", []), "case": case}
        for channel in case.get("channels", []):
            rows[(case["id"], channel["channel"])] = {**channel, "case": case}
    return rows


def _outcomes(row: dict[str, Any] | None, kind: str) -> dict[str, dict[str, Any]]:
    return {o["key"]: o for o in (row or {}).get(kind, [])}


def _ok(row: dict[str, Any] | None) -> bool:
    return bool(row) and row.get("status") in ("ok", "partial")


def _label(case_id: str, channel: str) -> str:
    return case_id if channel == "*" else f"{case_id} · {channel_label(channel)}"


def planned_scope(summary: dict[str, Any]) -> set[tuple[str, str]] | None:
    """(case, channel) pairs the run planned (``plan.scope``), or None for results without a recorded plan."""
    scope = (summary.get("plan") or {}).get("scope")
    if not isinstance(scope, list):
        return None
    return {(str(item[0]), str(item[1])) for item in scope if isinstance(item, (list, tuple)) and len(item) == 2}


def compare_summaries(baseline: dict[str, Any], current: dict[str, Any], *, max_score_drop: float = 5.0,
                      baseline_dir: str = "", current_dir: str = "",
                      scope: set[tuple[str, str]] | None = None) -> dict[str, Any]:
    """``scope``: the (case, channel) pairs to compare; defaults to what ``current`` planned (``plan.scope``)."""
    base_rows, cur_rows = _index(baseline), _index(current)
    scope = scope if scope is not None else planned_scope(current)
    out_of_scope: list[dict[str, Any]] = []
    if scope is not None:
        scoped_cases = {case_id for case_id, _ in scope}
        kept: dict[tuple[str, str], dict[str, Any]] = {}
        for key, row in base_rows.items():
            case_id, channel = key
            inside = case_id in scoped_cases if channel == "*" else key in scope
            if inside:
                kept[key] = row
            elif channel != "*":
                out_of_scope.append({"case": case_id, "channel": channel})
        base_rows = kept
    regressions: list[dict[str, Any]] = []
    fixed: list[dict[str, Any]] = []
    still_failing: list[dict[str, Any]] = []
    new_failures: list[dict[str, Any]] = []
    should_regressions: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    pairs: list[tuple[float, float]] = []

    for key, base in base_rows.items():
        case_id, channel = key
        cur = cur_rows.get(key)
        if channel != "*" and _ok(base) and not _ok(cur):
            reason = "이번 결과에 없어요" if cur is None else f"이번 실행에서 평가하지 못했어요 ({cur.get('error') or cur.get('status')})"
            missing.append({"case": case_id, "channel": channel, "reason": reason})
        if cur is None:
            continue
        base_must, cur_must = _outcomes(base, "must"), _outcomes(cur, "must")
        for mkey, outcome in cur_must.items():
            before = base_must.get(mkey)
            item = {"case": case_id, "channel": channel, "key": mkey, "label": outcome.get("label", mkey),
                    "detail": outcome.get("detail", "")}
            if outcome.get("error"):
                continue  # counted as missing above
            if before is None or before.get("error"):
                if not outcome["passed"]:
                    new_failures.append(item)
            elif before["passed"] and not outcome["passed"]:
                regressions.append({**item, "before": before.get("detail", "")})
            elif not before["passed"] and outcome["passed"]:
                fixed.append(item)
            elif not before["passed"] and not outcome["passed"]:
                still_failing.append(item)
        base_should, cur_should = _outcomes(base, "should"), _outcomes(cur, "should")
        for skey, outcome in cur_should.items():
            before = base_should.get(skey)
            if before and before["passed"] and not outcome["passed"] and not outcome.get("error"):
                should_regressions.append({"case": case_id, "channel": channel, "key": skey,
                                           "label": outcome.get("label", skey), "detail": outcome.get("detail", "")})
        if channel == "*":
            continue
        row: dict[str, Any] = {"case": case_id, "channel": channel, "status_before": base.get("status"),
                               "status_now": cur.get("status"), "score_before": base.get("score"),
                               "score_now": cur.get("score")}
        if _ok(base) and _ok(cur) and base.get("score") is not None and cur.get("score") is not None:
            row["score_delta"] = round(cur["score"] - base["score"], 1)
            pairs.append((float(base["score"]), float(cur["score"])))
        ub = (base.get("grounding") or {}).get("unsupported")
        uc = (cur.get("grounding") or {}).get("unsupported")
        if ub is not None and uc is not None:
            row["unsupported_before"], row["unsupported_now"] = ub, uc
        before_checks = {c["id"]: c["passed"] for c in base.get("checks", [])}
        row["checks_regressed"] = [c["id"] for c in cur.get("checks", []) if before_checks.get(c["id"]) and not c["passed"]]
        row["checks_fixed"] = [c["id"] for c in cur.get("checks", []) if before_checks.get(c["id"]) is False and c["passed"]]
        rows.append(row)

    added = [{"case": k[0], "channel": k[1]} for k in cur_rows if k not in base_rows and k[1] != "*"]
    mean_before = round(statistics.fmean(b for b, _ in pairs), 2) if pairs else None
    mean_now = round(statistics.fmean(c for _, c in pairs), 2) if pairs else None
    drop = round(mean_before - mean_now, 2) if pairs else 0.0
    exceeded = bool(pairs) and drop > max_score_drop
    cost_before = float((baseline.get("totals") or {}).get("cost_usd") or 0.0)
    cost_now = float((current.get("totals") or {}).get("cost_usd") or 0.0)

    reasons: list[str] = []
    if regressions:
        reasons.append(f"기준에서 통과하던 필수 조건 {len(regressions)}개가 이번에 실패했어요")
    if missing:
        reasons.append(f"기준에서 평가한 채널 {len(missing)}개를 이번에 평가하지 못했어요")
    if exceeded:
        reasons.append(f"평균 검수 점수가 {drop:g}점 떨어졌어요 (허용 {max_score_drop:g}점)")
    mode_before, mode_now = baseline.get("mode"), current.get("mode")
    warnings: list[str] = []
    if mode_before != mode_now:
        warnings.append(f"모드가 달라요 (기준 {mode_before}, 이번 {mode_now}). mock 점수는 녹화·템플릿 점수라 live와 비교할 수 없어요.")
    if new_failures:
        warnings.append(f"기준에는 없던(또는 평가 못 한) 필수 조건 {len(new_failures)}개가 이번에 실패했어요")
    if out_of_scope:
        cases_out = len({i["case"] for i in out_of_scope})
        warnings.append(f"이번 실행은 일부만 돌렸어요. 기준에만 있는 채널 {len(out_of_scope)}개(케이스 {cases_out}개)는 비교하지 않았어요.")
    exit_code = 1 if reasons else 0
    if exit_code:
        headline = "회귀가 있어요 — " + " · ".join(reasons)
    else:
        score = f"평균 검수 점수 {mean_before:g} → {mean_now:g}점" if pairs else "점수를 비교할 채널이 없어요"
        headline = f"회귀 없음 · {score} · 필수 조건 고침 {len(fixed)}개"
    return {
        "baseline_dir": baseline_dir, "current_dir": current_dir,
        "baseline_created_at": baseline.get("created_at"), "current_created_at": current.get("created_at"),
        "mode_before": mode_before, "mode_now": mode_now,
        "max_score_drop": max_score_drop,
        "mean_score": {"before": mean_before, "now": mean_now, "delta": (round(0.0 - drop, 2) + 0.0 if pairs else None),
                       "pairs": len(pairs)},
        "cost": {"before": round(cost_before, 6), "now": round(cost_now, 6), "delta": round(cost_now - cost_before, 6)},
        "regressions": regressions, "fixed": fixed, "still_failing": still_failing, "new_failures": new_failures,
        "should_regressions": should_regressions, "missing": missing, "added": added, "rows": rows,
        "out_of_scope": out_of_scope,
        "score_drop_exceeded": exceeded, "reasons": reasons, "warnings": warnings,
        "exit_code": exit_code, "headline": headline,
    }


def _money(value: float) -> str:
    if not value:
        return "$0"
    return f"${value:.4f}" if value < 0.01 else f"${value:,.2f}"


def _pct(before: float, now: float) -> str:
    if not before:
        return ""
    return f" ({(now - before) / before:+.0%})"


def render_compare(cmp: dict[str, Any], *, markdown: bool = True) -> str:
    """Korean text of a comparison (markdown tables; also readable in a terminal)."""
    lines: list[str] = []
    lines.append(f"- 기준: `{cmp['baseline_dir']}` ({cmp.get('mode_before')}) → 이번: `{cmp['current_dir']}` ({cmp.get('mode_now')})")
    lines.append(f"- 결과: **{cmp['headline']}**")
    ms = cmp["mean_score"]
    if ms["pairs"]:
        lines.append(f"- 평균 검수 점수: {ms['before']:g} → {ms['now']:g}점 ({ms['delta']:+g}, 같은 채널 {ms['pairs']}개 기준, "
                     f"허용 하락 {cmp['max_score_drop']:g}점)")
    cost = cmp["cost"]
    lines.append(f"- 비용: {_money(cost['before'])} → {_money(cost['now'])}{_pct(cost['before'], cost['now'])}")
    for warning in cmp["warnings"]:
        if cmp.get("out_of_scope") and warning.startswith("이번 실행은 일부만"):
            continue  # shown as the scope line below
        lines.append(f"- 주의: {warning}")

    def block(title: str, items: list[dict[str, Any]], fmt) -> None:
        if not items:
            return
        lines.append("")
        lines.append(f"**{title} ({len(items)})**")
        lines.append("")
        for item in items[:30]:
            lines.append("- " + fmt(item))
        if len(items) > 30:
            lines.append(f"- … 외 {len(items) - 30}개")

    if cmp.get("out_of_scope"):
        cases_out = sorted({i["case"] for i in cmp["out_of_scope"]})
        shown = ", ".join(cases_out[:8]) + (" …" if len(cases_out) > 8 else "")
        lines.append(f"- 비교 범위: 이번에 돌린 케이스·채널만 비교했어요 (기준에만 있는 채널 {len(cmp['out_of_scope'])}개 제외: {shown})")
    block("회귀: 기준에서 통과 → 이번에 실패한 필수 조건", cmp["regressions"],
          lambda i: f"{_label(i['case'], i['channel'])} — {i['label']} (`{i['key']}`): {i['detail']}")
    block("평가하지 못한 채널", cmp["missing"], lambda i: f"{_label(i['case'], i['channel'])} — {i['reason']}")
    block("새로 실패한 필수 조건 (기준에 없던 조건)", cmp["new_failures"],
          lambda i: f"{_label(i['case'], i['channel'])} — {i['label']}: {i['detail']}")
    block("고쳐진 필수 조건", cmp["fixed"], lambda i: f"{_label(i['case'], i['channel'])} — {i['label']}")
    block("나빠진 권장 목표", cmp["should_regressions"],
          lambda i: f"{_label(i['case'], i['channel'])} — {i['label']}: {i['detail']}")
    changed = [r for r in cmp["rows"] if r.get("score_delta") or r.get("checks_regressed") or r.get("checks_fixed")
               or r.get("unsupported_before") != r.get("unsupported_now")]
    if changed:
        lines.append("")
        lines.append("**채널별 변화**")
        lines.append("")
        lines.append("| 케이스 | 채널 | 점수 | 근거 없는 수치 | 형식 검사 |")
        lines.append("|---|---|---|---|---|")
        for r in changed:
            score = (f"{r['score_before']} → {r['score_now']} ({r['score_delta']:+g})" if r.get("score_delta") is not None
                     else f"{r.get('score_before')} → {r.get('score_now')}")
            unsup = f"{r.get('unsupported_before', '-')} → {r.get('unsupported_now', '-')}"
            checks = ", ".join([f"실패 {c}" for c in r["checks_regressed"]] + [f"고침 {c}" for c in r["checks_fixed"]]) or "-"
            lines.append(f"| {r['case']} | {channel_label(r['channel'])} | {score} | {unsup} | {checks} |")
    return "\n".join(lines) + "\n"
