"""Korean ``report.md`` for one eval result: failures first, then the per-case/channel table."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from ..agents.common import channel_label
from ..config import KST
from .compare import render_compare

STATUS_LABELS = {"ok": "완료", "partial": "일부 완료", "error": "실패", "budget_stopped": "예산 상한으로 멈춤",
                 "timeout": "시간 초과", "skipped": "건너뜀", "cancelled": "중단"}
FAILURE_LABELS = {"budget": "예산", "api": "API 오류", "refusal": "모델 거절", "timeout": "시간 초과",
                  "cancelled": "중단", "harness": "평가 도구 오류"}


def _kst(ts: str | None) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(KST).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts


def _usd(value: float | None) -> str:
    value = float(value or 0.0)
    if not value:
        return "$0"
    return f"${value:.4f}" if value < 0.01 else f"${value:,.2f}"


def _cell(text: Any, limit: int = 90) -> str:
    flat = " ".join(str(text if text is not None else "").split()).replace("|", "\\|")
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _num(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f"{value:g}" if isinstance(value, float) else str(value)


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _failures(summary: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for case in summary["cases"]:
        for failure in case.get("failures", []):
            lines.append(f"- **{case['id']}** 실행 문제({FAILURE_LABELS.get(failure['class'], failure['class'])}): "
                         f"{_cell(failure['message'], 200)}")
        for outcome in case.get("must", []):
            if not outcome["passed"]:
                lines.append(f"- **{case['id']}** — {outcome['label']} `{outcome['key']}`: {_cell(outcome['detail'], 200)}")
        for channel in case["channels"]:
            for outcome in channel.get("must", []):
                if outcome["passed"] or outcome.get("error"):
                    continue
                why = f" — 왜: {outcome['note']}" if outcome.get("note") else ""
                lines.append(f"- **{case['id']} · {channel_label(channel['channel'])}** — {outcome['label']} "
                             f"`{outcome['key']}`: {_cell(outcome['detail'], 200)}{why}")
            if channel["status"] not in ("ok", "partial"):
                lines.append(f"- **{case['id']} · {channel_label(channel['channel'])}** 평가하지 못했어요 "
                             f"({STATUS_LABELS.get(channel['status'], channel['status'])}): {_cell(channel.get('error'), 160)}")
            elif channel.get("reps_ok", 0) < channel.get("reps", 0):
                missed = channel["reps"] - channel["reps_ok"]
                lines.append(f"- **{case['id']} · {channel_label(channel['channel'])}** 반복 {channel['reps']}번 중 {missed}번은 "
                             f"평가하지 못했어요(실행 문제라 품질 실패로 세지 않아요): {_cell(channel.get('error'), 160)}")
        if case.get("model_mismatch"):
            lines.append(f"- **{case['id']}** 요청한 모델과 다른 모델이 응답했어요: {', '.join(case['model_mismatch'])} "
                         "(서버 측 거절 대체일 수 있어요. 이 케이스 점수는 조심해서 보세요)")
    return lines


def _ratio(passed: int, total: int) -> str:
    return f"{passed}/{total}" if total else "-"


def render_report(summary: dict[str, Any]) -> str:
    t = summary["totals"]
    mode = summary.get("mode", "")
    out: list[str] = ["# INSIA 품질 평가 결과", ""]
    out.append(f"- 실행: {_kst(summary.get('created_at'))} (KST) · **{mode} 모드** · 모델 {summary.get('model')} · "
               f"케이스 {t['cases']}개 · 채널 {t['channels']}개" + (f" · 반복 {summary.get('reps')}회" if summary.get("reps", 1) > 1 else ""))
    mean = f"{t['mean_score']:g}점" if t.get("mean_score") is not None else "-"
    out.append(f"- 결과: 필수 조건 **{t['must_passed']}/{t['must_total']}** 통과 · 권장 목표 {t['should_passed']}/{t['should_total']} · "
               f"평가 못 한 채널 {t['error_channels']}개 · 평균 검수 점수 {mean} · 검수 통과 채널 {t['reviewer_passed']}/{t['ok_channels']}")
    cost = f"- 비용: {_usd(t['cost_usd'])}"
    if t.get("judge_cost_usd"):
        cost += f" + LLM 비교 채점 {_usd(t['judge_cost_usd'])}"
    if t.get("discarded_cost_usd"):
        cost += f" + 다시 돌리느라 버린 이전 시도 {_usd(t['discarded_cost_usd'])}"
    if t.get("unlisted_cost_usd", 0) >= 0.005:
        cost += (f" + 요약 밖 {_usd(t['unlisted_cost_usd'])}(중단·시간 초과 뒤 끝난 호출, 요약에 넣지 않은 이전 결과) = "
                 f"**이 폴더에서 쓴 비용 {_usd(t.get('folder_cost_usd'))}**")
    if t.get("cost_per_case") and t["cost_usd"]:
        c = t["cost_per_case"]
        cost += f" (케이스당 평균 {_usd(c['mean'])}, 최소 {_usd(c['min'])} · 최대 {_usd(c['max'])})"
    plan = summary.get("plan") or {}
    if plan.get("estimate_usd") is not None:
        cost += f" · 실행 전 추정 {_usd(plan['estimate_usd'])}"
    out.append(cost + f" · 걸린 시간 {t['wall_s']:,.1f}초")
    if summary.get("stopped"):
        out.append(f"- 멈춤: {summary['stopped']}")
    if summary.get("interrupted"):
        out.append("- 중단: 사용자가 도중에 멈춰서 끝난 실행과 중단한 실행까지만 담았어요. 쓴 비용은 `spend.jsonl`에 모두 있고 "
                   "`--resume`이 예산에 넣어요.")
    if t.get("cost_incomplete_runs"):
        out.append(f"- 비용 일부 빠짐: 실행 {t['cost_incomplete_runs']}번은 멈춘 뒤에도 진행 중이던 호출이 있어서 그 비용이 케이스 "
                   "비용에 빠져 있어요(평가 중에 끝나면 `spend.jsonl`과 예산에는 들어가요).")
    carried = (summary.get("carried") or {})
    if carried.get("cases"):
        out.append(f"- 이전 결과 그대로: 이번에 돌리지 않은 케이스 {len(carried['cases'])}개({', '.join(carried['cases'])})는 "
                   "이 폴더의 이전 결과를 같은 설정 그대로 담았어요.")
    if carried.get("left_out"):
        out.append(f"- 요약에서 뺀 이전 결과: {', '.join(carried['left_out'])} (모드나 설정이 다르거나 저장된 실행을 다시 채점할 수 없어서요. 비용은 폴더 비용에 들어가요)")
    if carried.get("dropped_channels"):
        shown = ", ".join(f"{case_id} {channel_label(channel)}" for case_id, channel in carried["dropped_channels"])
        out.append(f"- 요약에서 빠진 이전 채널: {shown} (모드·설정·케이스가 달라 이번에 다시 돌리지 않았어요. 비용은 폴더 비용에 들어가요)")
    resumed = summary.get("resumed") or {}
    if resumed.get("reused_runs") or resumed.get("prior_cost_usd"):
        regraded = (f" (그중 {resumed['regraded_runs']}번은 채점 코드가 바뀌어 저장된 결과로 다시 채점했어요)"
                    if resumed.get("regraded_runs") else "")
        out.append(f"- 이어서 실행: 이전에 끝난 실행 {resumed.get('reused_runs', 0)}번을 다시 쓰고{regraded}, 이 폴더에서 이미 쓴 비용 "
                   f"{_usd(resumed.get('prior_cost_usd'))}을 먼저 예산에 넣었어요.")
    out.append("")
    if mode == "mock":
        out.append("> **mock 모드 결과예요.** API를 부르지 않아서, 샘플 브리프는 녹화된 실행을, 나머지는 `[데모]` 템플릿을 채점해요. "
                   "검수 점수는 녹화·템플릿 값이라 품질 판단에 쓰지 않아요. mock에서 볼 것은 평가 도구가 제대로 도는지와, "
                   "형식·블라인드·금지 표현·근거 확인 같은 **결정적 검사**가 템플릿 출력에서 어떻게 나오는지예요.")
    else:
        n = max(1, t["channels"]) * max(1, int(summary.get("reps") or 1))
        noise = 1 / math.sqrt(n)
        out.append(f"> live 결과예요. 채널 {t['channels']}개 × 반복 {summary.get('reps', 1)}회라 통과율 차이 ±{noise:.0%} 안쪽은 "
                   "우연일 수 있어요. 검수 점수는 같은 입력에서도 몇 점씩 흔들리니 한 번의 작은 차이로 결론 내리지 마세요.")
    out.append("")

    out.append("## 먼저 볼 것")
    out.append("")
    failures = _failures(summary)
    baseline = summary.get("baseline")
    if baseline and baseline.get("exit_code"):  # regressions, planned channels not graded, or a score drop
        reasons = baseline.get("reasons") or [baseline.get("headline", "")]
        failures.insert(0, f"- **기준 대비 실패** — {' · '.join(reasons)}. 아래 '기준 대비 변화'를 보세요.")
    if summary.get("stopped") and not any("실행 문제" in line for line in failures):
        failures.append(f"- 평가가 끝까지 돌지 않았어요: {summary['stopped']}")
    if failures:
        out.extend(failures)
    elif baseline:
        out.append("- 필수 조건을 모두 통과했고 실행 문제도 없고, 기준 대비 회귀도 없어요.")
    else:
        out.append("- 필수 조건을 모두 통과했고 실행 문제도 없어요.")
    out.append("")

    if baseline:
        out.append("## 기준 대비 변화")
        out.append("")
        out.append(render_compare(baseline).rstrip())
        out.append("")

    out.append("## 케이스별 결과")
    out.append("")
    out.append("실패가 있는 케이스를 위에 두었어요. 필수·권장은 통과/전체, 수치는 근거 없는 수/주장 수예요.")
    out.append("")
    out.append("| 케이스 | 채널 | 필수 | 권장 | 검수 점수 | 검수 | 수정 | 형식 검사 | 근거 없는 수치 | 인용률 | 자리표시 |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|")
    ordered = sorted(summary["cases"], key=lambda c: (c["must_failed"] == 0 and c["must_errors"] == 0 and c["status"] == "ok", c["id"]))
    for case in ordered:
        for channel in case["channels"]:
            musts = channel.get("must", [])
            shoulds = channel.get("should", [])
            must_cell = _ratio(sum(1 for o in musts if o["passed"]), len(musts))
            if any(not o["passed"] for o in musts):
                must_cell = f"**{must_cell}** ✗"
            if channel["status"] not in ("ok", "partial"):
                out.append(f"| {case['id']} | {channel_label(channel['channel'])} | 평가 못 함 | - | - | "
                           f"{STATUS_LABELS.get(channel['status'], channel['status'])} | - | - | - | - | - |")
                continue
            checks = channel.get("checks", [])
            failed_checks = [c["id"] for c in checks if not c["passed"]]
            check_cell = _ratio(sum(1 for c in checks if c["passed"]), len(checks))
            if failed_checks:
                check_cell += " (" + ", ".join(failed_checks) + ")"
            g = channel.get("grounding", {})
            out.append(
                f"| {case['id']} | {channel_label(channel['channel'])} | {must_cell} | "
                f"{_ratio(sum(1 for o in shoulds if o['passed']), len(shoulds))} | {_num(channel.get('score'))} | "
                f"{'통과' if channel.get('passed') else '미통과'} | {_num(channel.get('rounds'))} | {check_cell} | "
                f"{g.get('unsupported', 0)}/{g.get('claims', 0)} | {_pct(g.get('citation_coverage'))} | {_num(channel.get('placeholders'))} |")
        if case.get("must"):
            passed = sum(1 for o in case["must"] if o["passed"])
            out.append(f"| {case['id']} | (케이스 전체) | {_ratio(passed, len(case['must']))} | | | | | | | | |")
    out.append("")

    unmet = [(case, channel, o) for case in summary["cases"] for channel in case["channels"]
             for o in channel.get("should", []) if not o["passed"] and not o.get("error")]
    if unmet:
        out.append("## 권장 목표 미달")
        out.append("")
        out.append("실패로 치지는 않지만 품질이 흔들리는 신호예요.")
        out.append("")
        for case, channel, o in unmet[:40]:
            out.append(f"- {case['id']} · {channel_label(channel['channel'])} — {o['label']}: {_cell(o['detail'], 160)}")
        out.append("")

    out.append("## 형식·브랜드 검사 통과율")
    out.append("")
    out.append("| 채널 | 검사 | 통과 |")
    out.append("|---|---|---|")
    for check in summary.get("checks", []):
        mark = "" if check["passed"] == check["total"] else " ✗"
        out.append(f"| {channel_label(check['channel'])} | {check['label']} (`{check['id']}`) | {check['passed']}/{check['total']}{mark} |")
    out.append("")

    judged = [(case, channel) for case in summary["cases"] for channel in case["channels"] if channel.get("judge")]
    if judged:
        wins = [channel["judge_win"] for _, channel in judged]
        out.append("## LLM 비교 채점 (유료, 참고용)")
        out.append("")
        out.append(f"- 판정 모델 {judged[0][1]['judge'].get('judge_model')} · 이번 결과 승률 {sum(wins) / len(wins):.0%} "
                   f"(이김 1, 무승부 0.5, 짐·둘 다 부족 0, 채널 {len(wins)}개). 사람이 판정한 몇 쌍과 맞춰 본 뒤 믿으세요.")
        out.append("")
        out.append("| 케이스 | 채널 | 판정 | 요약 |")
        out.append("|---|---|---|---|")
        labels = {"current": "이번 결과", "baseline": "기준 결과", "tie": "무승부", "both_bad": "둘 다 부족"}
        for case, channel in judged:
            j = channel["judge"]
            out.append(f"| {case['id']} | {channel_label(channel['channel'])} | {labels.get(j['winner'], j['winner'])} | "
                       f"{_cell(j.get('summary'), 120)} |")
        out.append("")

    g = summary.get("grounding", {})
    out.append("## 수치 근거 확인")
    out.append("")
    out.append(f"- 수치 주장 {g.get('claims', 0)}개: 근거 있음 {g.get('supported', 0)} · 가정 표시 {g.get('assumed', 0)} · "
               f"확인 필요 표시 {g.get('flagged', 0)} · **근거 없음 {g.get('unsupported', 0)}**")
    out.append(f"- 출처가 필요한 수치 {g.get('factual', 0)}개 중 같은 문단에 출처(기관명·[s#]·출처 표기)가 있는 비율: "
               f"{_pct(g.get('citation_coverage'))}")
    out.append("- 근거 있음 = 같은 값이 리서치 팩(근거·출처 제목, gaps 제외)·회사 프로필·사용자 자료에 있음(브리프는 요청이라 근거로 치지 않아요). "
               "자료 스스로 '검토 중·확정 전·가정'이라고 한 값은 초안도 그렇게 밝혀야 근거 있음이고, 확정처럼 쓰면 근거 없음이에요.")
    out.append("- 가정 표시 = 그 문장(표는 그 행, 바로 뒤 `※` 줄 포함)에 가정·예시·가상·시나리오가 있거나, 수치에 바로 붙은 "
               "목표·예정·계획이 있음(`목표 매출 3억 원`, `2,000개 확보 목표`). '목표 시장', '출시 예정인 …'처럼 수치와 떨어진 말이나 "
               "'가정용'·'가정에서' 같은 다른 낱말은 가정 표시가 아니에요.")
    rows = [(case, channel, n) for case in summary["cases"] for channel in case["channels"]
            for n in channel.get("unsupported", [])]
    if rows:
        out.append("")
        out.append("근거 없는 수치 (지어낸 값일 수 있어요. 리서치 팩에 넣거나, 가정이면 '가정'이라고 쓰거나, ○○로 바꾸세요):")
        out.append("")
        out.append("| 케이스 | 채널 | 수치 | 문장 |")
        out.append("|---|---|---|---|")
        for case, channel, n in rows[:30]:
            text = n["text"] + (" (자료에선 확정 전)" if n.get("tentative") else "")
            out.append(f"| {case['id']} | {channel_label(channel['channel'])} | {text} | {_cell(n['sentence'], 110)} |")
        if len(rows) > 30:
            out.append(f"| … | | 외 {len(rows) - 30}개 | cases/<id>.json에 전부 있어요 |")
    out.append("")

    out.append("## 케이스 설명")
    out.append("")
    out.append("| 케이스 | 제목 | 무엇을 보나 | 비용 | 걸린 시간 |")
    out.append("|---|---|---|---|---|")
    for case in sorted(summary["cases"], key=lambda c: c["id"]):
        duration = f"{case['duration_s']:,.0f}초" + (" (가상)" if mode == "mock" else "")
        out.append(f"| {case['id']} | {_cell(case['title'], 50)} | {_cell(case.get('description'), 120)} | "
                   f"{_usd(case['cost_usd'])} | {duration} |")
    out.append("")
    out.append("## 읽는 법")
    out.append("")
    out.append("- **필수(must)** 실패는 그대로 두면 안 되는 문제예요(블라인드 위반, 금지 표현, 지어낸 수치 등). 하나라도 있으면 "
               "`insia eval run`이 종료 코드 1로 끝나요.")
    out.append("- **권장(should)** 은 목표치예요(검수 점수, 수정 횟수, 인용률). 떨어지면 원인을 보지만 실패로 치지는 않아요.")
    out.append("- 케이스마다 최종본 전문, 수치별 판정, 검수 의견은 `cases/<케이스>.json`에, 파이프라인 기록(계획·리서치·초안·검수·이벤트)은 "
               "`runs/<케이스>/`에 있어요.")
    out.append("- 프롬프트·채널 가이드·모델을 바꾸기 전 결과를 기준으로 두고 `insia eval compare <기준> <이번>`으로 비교하세요.")
    return "\n".join(out) + "\n"
