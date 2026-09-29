"""Regression tests for the eval set's final-review findings (grounding, spend ledger, resume, reps, prompts).

Offline only: mock content with priced fake usage, a temporary workspace, no network, no API key (a dummy key only
where the live confirmation prompt is answered "n" before anything runs).
"""

from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import insia_agents.evals.runner as runner_mod
from insia_agents.backends.base import BackendError
from insia_agents.backends.mock_backend import MockBackend
from insia_agents.cli import main
from insia_agents.costs import load_prices, usage_from_response
from insia_agents.evals.cases import load_case
from insia_agents.evals.compare import compare_summaries
from insia_agents.evals.graders import apply_assertions, grade_channel
from insia_agents.evals.grounding import Evidence, analyze_numbers
from insia_agents.evals.runner import SPEND_FILE, EvalOptions, UsageCollector, _merge_outcomes, run_eval
from insia_agents.models import ChannelResult, Draft, Finding, ResearchPack, Review, Source, UsageRecord

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals" / "cases"


@pytest.fixture(autouse=True)
def user_home(tmp_path, monkeypatch):
    home = tmp_path / "user-workspace"
    home.mkdir()
    monkeypatch.setenv("INSIA_HOME", str(home))
    monkeypatch.delenv("INSIA_MAX_COST_USD", raising=False)
    return home


def _grade_case(case_id: str, channel: str, content: str, *, research: ResearchPack | None = None,
                hashtags: list[str] | None = None) -> dict:
    """The same path as run_case: Evidence.build (research, profile, documents) → grade_channel → apply_assertions."""
    case = load_case(CASES / f"{case_id}.json")
    research = research or ResearchPack(findings=[], sources=[])
    docs = [SimpleNamespace(id=f"doc{i}", text=d.text) for i, d in enumerate(case.documents, 1)]
    draft = Draft(channel=channel, round=0, title="제목", content=content, hashtags=hashtags or [])
    review = Review(channel=channel, round=0, score=85, passed=True, rubric=[], issues=[], summary="-")
    result = ChannelResult(channel=channel, final=draft, drafts=[draft], reviews=[review], passed=True, rounds=0)
    grade = grade_channel(case, result, research, Evidence.build(research, profile=case.profile, documents=docs))
    apply_assertions(case, grade, "live")
    return grade


def _must(grade: dict, prefix: str) -> dict:
    return next(o for o in grade["must"] if o["key"].startswith(prefix))


def _numbers(grade: dict) -> dict[str, dict]:
    return {n["text"]: n for n in grade["grounding"]["numbers"]}


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------


USER_SOURCE = Source(id="s5", title="플로노트 회사 소개서 (2026년 9월)", url="user://doc1", publisher="사용자 제공 자료",
                     tier=1, accessed="2026-09-28", origin="user")
WEB_SOURCE = Source(id="s1", title="2025 소상공인 실태조사", url="https://kosis.kr/x", publisher="통계청", tier=1)


def test_user_sourced_finding_does_not_settle_the_documents_tentative_price():
    """user-docs-traction: the researcher restating the document ('검토 중·확정 전', or with the hedge dropped, or
    only in the note) must not turn the undecided 29,000원 into settled evidence."""
    settled = "## 3. 성장전략\n\n- 수익 모델: 꽃집 단위 월 구독, 요금은 월 29,000원 단일 요금제예요 (자사 자료)."
    restatements = [
        Finding(id="f5", question_id="q3", claim="플로노트는 월 29,000원 단일 요금제를 검토 중이며 확정 전이다.",
                source_ids=["s5"], confidence="medium", note="사용자 제공 자료(외부 검증 전)"),
        Finding(id="f5", question_id="q3", claim="플로노트 요금: 월 29,000원 단일 요금제.", source_ids=["s5"],
                confidence="medium", note="사용자 제공 자료"),  # the hedge was dropped
        Finding(id="f5", question_id="q3", claim="플로노트 요금안: 월 29,000원 단일 요금제.", source_ids=["s5"],
                confidence="low", note="가정 — 확정 전인 요금안"),
    ]
    for finding in restatements:
        pack = ResearchPack(findings=[finding], sources=[WEB_SOURCE, USER_SOURCE])
        grade = _grade_case("user-docs-traction", "bizplan", settled, research=pack)
        outcome = _must(grade, "assumption_marked")
        assert not outcome["passed"] and "29,000원" in outcome["detail"], finding.claim
        price = _numbers(grade)["29,000원"]
        assert price["status"] == "unsupported" and price["tentative"] and "research:f5" not in price["origins"]
        marked = _grade_case("user-docs-traction", "bizplan", settled.replace("단일 요금제예요", "단일 요금제(가정, 확정 전)예요"),
                             research=pack)
        assert _must(marked, "assumption_marked")["passed"]
    # a finding backed by a web source is still research evidence (a competitor's published price, say)
    web = ResearchPack(findings=[Finding(id="f2", question_id="q2", claim="경쟁 서비스 월 요금은 29,000원이다.",
                                         source_ids=["s1"], confidence="high")], sources=[WEB_SOURCE, USER_SOURCE])
    grade = _grade_case("user-docs-traction", "bizplan", settled, research=web)
    assert _numbers(grade)["29,000원"]["status"] == "supported" and _must(grade, "assumption_marked")["passed"]


def test_measurement_duration_and_rating_units_are_claims():
    health = ("보장균수 100억 CFU 제품을 먹으면 14일 만에 배변 횟수가 달라져요.\n\n구매 후기 평점은 4.9점이에요.\n\n"
              "건강기능식품은 질병의 예방 및 치료를 위한 의약품이 아닙니다")
    grade = _grade_case("health-food-disclaimer", "naver_blog", health, hashtags=["#광고", "#유산균"])
    outcome = _must(grade, "max_unsupported_numbers")
    assert not outcome["passed"] and outcome["value"] == 3
    assert all(t in outcome["detail"] for t in ("100억 CFU", "14일", "4.9점"))

    carousel = "강아지 간식 성분표 읽는 법\n\n- 문구: 100g당 단백질 32g, 지방 4.5g이에요.\n- 문구: 한 조각 12kcal예요."
    grade = _grade_case("instagram-carousel-limits", "instagram", carousel)
    assert {t for t, n in _numbers(grade).items() if n["status"] == "unsupported"} == {"32g", "4.5g", "12kcal"}
    assert not _must(grade, "max_unsupported_numbers")["passed"]  # "100g당" is the label's serving basis

    effect = ("코어핏 가을 체험 수업을 열어요.\n\n12주 다니면 허리둘레가 평균 5.5cm 줄고 체지방이 3kg 빠져요.\n\n"
              "퇴근길 50분이면 충분해요.\n\n#광고")
    grade = _grade_case("sns-banned-required", "linkedin", effect, hashtags=["#광고", "#필라테스체험", "#자세교정"])
    numbers = _numbers(grade)
    assert {t for t, n in numbers.items() if n["status"] == "unsupported"} == {"12주", "5.5cm", "3kg"}
    assert numbers["50분"]["status"] == "supported" and "profile" in numbers["50분"]["origins"]  # the profile's own fact

    # dates, clock times, scales, rates, fractions and compound words are wording, not claims
    wording = ("2026년 9월 28일 확인. 오후 3시 30분 시작. 80점 이상이면 통과, 5점 만점. 1일 1포 드세요. 화면 3분의 1. "
               "5포인트 적립. 5G 요금제. 2번째 슬라이드, 제3회 대회, 4주 차 점검. 주 2회 수업, 3종 세트.")
    draft = Draft(channel="naver_blog", round=0, title="-", content=wording)
    assert [m.text for m in analyze_numbers(draft, Evidence.build(None)) if m.kind == "claim"] == []
    # measurements are compared in one base unit
    pack = ResearchPack(findings=[Finding(id="f1", question_id="q1", claim="참가자 평균 감량은 3,200g이었다.",
                                          source_ids=["s1"], confidence="high")], sources=[WEB_SOURCE])
    draft = Draft(channel="naver_blog", round=0, title="-", content="평균 3.2kg 줄었어요. 어떤 분은 5kg 줄었어요.")
    assert {m.text: m.status for m in analyze_numbers(draft, Evidence.build(pack))} == {"3.2kg": "supported",
                                                                                         "5kg": "unsupported"}


GUIDE_BLOCK = """□ (수익 모델) 세무 사무소 단위 월 구독
- 베이직 월 19,000원: 고객 50명까지 자료 요청
- 프로 월 39,000원: 고객 무제한, 홈택스 연동
※ 가정: 월 구독료 베이직·프로, 시장 검증 전 제안 가격"""


def test_note_after_a_list_covers_every_item():
    """The bizplan guide's own layout (□ line, '- ' bullets, '※ 가정:' paragraph) marks every price in the list."""
    variants = {
        "guide": GUIDE_BLOCK,
        "blank line before the note": GUIDE_BLOCK.replace("\n※", "\n\n※"),
        "note as a list item": GUIDE_BLOCK.replace("\n※", "\n- ※"),
        "swapped bullets": GUIDE_BLOCK.replace("- 베이직 월 19,000원: 고객 50명까지 자료 요청\n- 프로 월 39,000원: 고객 무제한, 홈택스 연동",
                                               "- 프로 월 39,000원: 고객 무제한, 홈택스 연동\n- 베이직 월 19,000원: 고객 50명까지 자료 요청"),
        "loose list, sub-items": ("□ (수익 모델) 월 구독\n- 베이직\n  - 월 19,000원\n\n- 프로\n  - 월 39,000원\n\n"
                                  "※ 가정: 시장 검증 전 제안 가격"),
    }
    for name, block in variants.items():
        grade = _grade_case("pricing-assumption", "bizplan", block)
        assert _must(grade, "assumption_marked")["passed"], name
        assert {_numbers(grade)[t]["status"] for t in ("19,000원", "39,000원")} == {"supported"}, name
    blog = _grade_case("pricing-assumption", "naver_blog",
                       "## 요금\n\n- 베이직: 월 19,000원\n- 프로: 월 39,000원\n\n※ 위 요금은 모두 가정이에요.")
    assert _must(blog, "assumption_marked")["passed"]
    # without the note the prices are still unmarked; a new □ headline after the note starts its own block
    bare = _grade_case("pricing-assumption", "bizplan", GUIDE_BLOCK.rsplit("\n", 1)[0])
    assert not _must(bare, "assumption_marked")["passed"]
    later = _grade_case("pricing-assumption", "bizplan", GUIDE_BLOCK + "\n□ (자금 조달) 정부지원 이후\n- 추가 요금 월 9,000원")
    assert "9,000원" in _must(later, "assumption_marked")["detail"] and "19,000원" not in _must(later, "assumption_marked")["detail"]


# ---------------------------------------------------------------------------
# Runner: spend ledger, Ctrl+C, timeout, resume
# ---------------------------------------------------------------------------


class PricedMock(MockBackend):
    """Mock content; every call is billed $0.40 at the served model (``settings.model``)."""

    def _usage(self, agent, task, prompt, output):  # noqa: ANN001
        if self.on_usage is not None:
            self.on_usage(UsageRecord(agent=agent, task=task, model=self.settings.model, input_tokens=1000,
                                      output_tokens=100, cost_usd=0.4))


def _ledger_total(folder: Path) -> float:
    lines = (folder / SPEND_FILE).read_text(encoding="utf-8").splitlines()
    return round(sum(json.loads(line)["cost_usd"] for line in lines), 6)


@pytest.mark.usefixtures("no_network")
def test_ctrl_c_mid_case_keeps_its_spend_and_resume_counts_it(tmp_path, monkeypatch, caplog):
    billed = {"usd": 0.0, "calls": 0}
    interrupt_now, pressed = threading.Event(), threading.Event()
    instances = {"n": 0}

    class Interrupted(PricedMock):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances["n"] += 1
            self.case_no = instances["n"]
            self.calls = 0

        def _usage(self, agent, task, prompt, output):  # noqa: ANN001
            super()._usage(agent, task, prompt, output)
            billed["usd"] += 0.4
            billed["calls"] += 1
            self.calls += 1
            if self.case_no == 2 and self.calls == 3:
                interrupt_now.set()  # the person presses Ctrl+C while this call is still in flight
                pressed.wait(5)

    def wait(self, seconds=None):  # noqa: ANN001 - _Worker.wait with Ctrl+C arriving while the eval waits
        deadline = None if seconds is None else time.monotonic() + seconds
        while self.alive and (deadline is None or time.monotonic() < deadline):
            if interrupt_now.is_set() and not pressed.is_set():
                pressed.set()
                raise KeyboardInterrupt
            self._done.wait(0.02)
        return not self.alive

    monkeypatch.setattr(runner_mod._Worker, "wait", wait)
    out = tmp_path / "live"
    options = dict(mode="live", cases_dir=CASES, out_dir=out, case_ids=["very-short-brief", "naver-long-keyword"])
    with pytest.raises(KeyboardInterrupt):
        run_eval(EvalOptions(max_cost_usd=10.0, **options), printer=lambda _line: None, backend_factory=Interrupted)
    spent = round(billed["usd"], 6)
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["interrupted"] and [c["id"] for c in summary["cases"]] == ["very-short-brief", "naver-long-keyword"]
    assert summary["cases"][1]["status"] == "cancelled" and summary["cases"][1]["failures"][0]["class"] == "cancelled"
    assert summary["totals"]["cost_usd"] == pytest.approx(spent) and summary["totals"]["folder_cost_usd"] == pytest.approx(spent)
    assert _ledger_total(out) == pytest.approx(spent)
    cancelled = json.loads((out / "cases" / "naver-long-keyword.json").read_text(encoding="utf-8"))["reps"][0]
    assert cancelled["status"] == "cancelled" and cancelled["cost_usd"] == pytest.approx(1.2)
    assert "중단" in (out / "report.md").read_text(encoding="utf-8")
    # the cancelled worker finished before its workspace was closed: no "워크스페이스가 이미 닫혔어요"
    assert not [r for r in caplog.records if "닫혔" in (r.getMessage() + str(r.exc_info and r.exc_info[1]))]
    # --resume with the cap at what was really spent: nothing new runs, the finished case is reused
    lines: list[str] = []
    resumed = run_eval(EvalOptions(max_cost_usd=spent, resume=True, **options), printer=lines.append,
                       backend_factory=PricedMock)
    assert resumed["resumed"] == {"reused_runs": 1, "prior_cost_usd": pytest.approx(spent)}
    assert [c["status"] for c in resumed["cases"]] == ["ok", "skipped"]
    assert _ledger_total(out) == pytest.approx(spent)  # not a cent more


@pytest.mark.usefixtures("no_network")
def test_a_call_that_finishes_after_a_timeout_is_billed_to_the_ledger_record_and_next_cap(tmp_path, monkeypatch):
    billed = {"usd": 0.0}
    release, late_billed = threading.Event(), threading.Event()
    instances = {"n": 0}

    class SlowSecondCall(MockBackend):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances["n"] += 1
            self.case_no, self.calls = instances["n"], 0

        def _usage(self, agent, task, prompt, output):  # noqa: ANN001
            self.calls += 1
            cost = 0.4
            if self.case_no == 1 and self.calls == 2:
                release.wait(10)  # a long research call still streaming when the timeout fires
                cost = 2.0
            billed["usd"] += cost
            if self.on_usage is not None:
                self.on_usage(UsageRecord(agent=agent, task=task, model="claude-opus-5", input_tokens=1000,
                                          output_tokens=100, cost_usd=cost))
            if cost == 2.0:
                late_billed.set()

    caps: list[tuple[str, float]] = []
    real_run_case = runner_mod.run_case

    def spy(planned, rep, *args, **kwargs):  # noqa: ANN001
        caps.append((planned.case.id, kwargs["remaining_cap"]))
        return real_run_case(planned, rep, *args, **kwargs)

    monkeypatch.setattr(runner_mod, "run_case", spy)

    def printer(line: str) -> None:
        if "시간 초과" in line and not release.is_set():  # case 1 was recorded; its call now finishes and is billed
            release.set()
            assert late_billed.wait(5)

    out = tmp_path / "t"
    summary = run_eval(EvalOptions(mode="live", cases_dir=CASES, out_dir=out, max_cost_usd=10.0, timeout_s=0.3,
                                   case_ids=["very-short-brief", "naver-long-keyword"]),
                       printer=printer, backend_factory=SlowSecondCall)
    first = summary["cases"][0]
    assert first["status"] == "timeout"
    assert caps[1][0] == "naver-long-keyword" and caps[1][1] == pytest.approx(10.0 - 2.4)  # the late $2 counted
    assert first["cost_usd"] == pytest.approx(2.4) and not first["cost_incomplete"]  # refreshed before the summary
    assert "진행 중이던 호출 비용까지 넣었어요" in first["failures"][0]["message"]
    assert summary["totals"]["folder_cost_usd"] == pytest.approx(billed["usd"]) == pytest.approx(_ledger_total(out))

    # the call is still running when the eval ends: the record says so, and the ledger gets it once it finishes
    release.clear()
    late_billed.clear()
    instances["n"] = 0
    out2 = tmp_path / "t2"
    try:
        summary = run_eval(EvalOptions(mode="live", cases_dir=CASES, out_dir=out2, max_cost_usd=10.0, timeout_s=0.2,
                                       case_ids=["very-short-brief"]), printer=lambda _line: None,
                           backend_factory=SlowSecondCall)
        rep = json.loads((out2 / "cases" / "very-short-brief.json").read_text(encoding="utf-8"))["reps"][0]
        assert rep["cost_incomplete"] and rep["cost_usd"] == pytest.approx(0.4)
        assert "빠져 있어요" in rep["failure"]["message"] and summary["totals"]["cost_incomplete_runs"] == 1
        assert "비용 일부 빠짐" in (out2 / "report.md").read_text(encoding="utf-8")
    finally:
        release.set()
    assert late_billed.wait(5)
    assert _ledger_total(out2) == pytest.approx(2.4)  # what --resume counts toward the cap


@pytest.mark.usefixtures("no_network")
def test_resume_reruns_runs_made_with_other_settings(tmp_path):
    out = tmp_path / "live"
    common = dict(mode="live", cases_dir=CASES, out_dir=out, max_cost_usd=50.0, case_ids=["very-short-brief"])
    first = run_eval(EvalOptions(model="claude-opus-5", **common), printer=lambda _line: None, backend_factory=PricedMock)
    lines: list[str] = []
    again = run_eval(EvalOptions(model="claude-sonnet-5", pass_score=90, resume=True, **common), printer=lines.append,
                     backend_factory=PricedMock)
    assert again["resumed"]["reused_runs"] == 0 and again["cases"][0]["served_models"] == ["claude-sonnet-5"]
    assert any("달라진 설정: 모델, 통과 점수" in line for line in lines)
    data = json.loads((out / "cases" / "very-short-brief.json").read_text(encoding="utf-8"))
    assert data["settings"]["model"] == "claude-sonnet-5" and data["settings"]["pass_score"] == 90
    assert data["result"]["discarded_cost_usd"] == pytest.approx(first["totals"]["cost_usd"])
    warnings = " ".join(compare_summaries(first, again)["warnings"])
    assert "모델이 달라요" in warnings and "통과 점수 80 → 90" in warnings
    # same settings again: reused as is
    same = run_eval(EvalOptions(model="claude-sonnet-5", pass_score=90, resume=True, **common),
                    printer=lambda _line: None, backend_factory=PricedMock)
    assert same["resumed"]["reused_runs"] == 1


@pytest.mark.usefixtures("no_network")
def test_resume_with_fewer_cases_keeps_the_folder_in_the_summary_and_the_cap(tmp_path):
    out = tmp_path / "live"
    common = dict(mode="live", cases_dir=CASES, out_dir=out)
    first = run_eval(EvalOptions(max_cost_usd=50.0, case_ids=["very-short-brief"], **common), printer=lambda _line: None,
                     backend_factory=PricedMock)
    spent = first["totals"]["cost_usd"]
    lines: list[str] = []
    again = run_eval(EvalOptions(max_cost_usd=spent, resume=True, case_ids=["naver-long-keyword"], **common),
                     printer=lines.append, backend_factory=PricedMock)
    # the other case's spend is in the cap: the new case never starts
    assert again["resumed"]["prior_cost_usd"] == pytest.approx(spent)
    by_id = {c["id"]: c for c in again["cases"]}
    assert by_id["naver-long-keyword"]["status"] == "skipped" and by_id["very-short-brief"]["status"] == "ok"
    assert by_id["very-short-brief"]["carried"] and again["carried"]["cases"] == ["very-short-brief"]
    assert ["very-short-brief", "linkedin"] in again["plan"]["scope"]
    assert any("이전 결과를 요약에 그대로" in line for line in lines)
    assert _ledger_total(out) == pytest.approx(spent)
    # a comparison against this folder still covers the carried case
    broken = copy.deepcopy(again)
    next(c for c in broken["cases"] if c["id"] == "very-short-brief")["channels"][0].update({"status": "error"})
    assert compare_summaries(again, broken)["missing"]
    # another pass score: the earlier result is left out of the summary, its spend still counts
    other = run_eval(EvalOptions(max_cost_usd=100.0, resume=True, pass_score=90, case_ids=["naver-long-keyword"],
                                 **common), printer=lambda _line: None, backend_factory=PricedMock)
    assert [c["id"] for c in other["cases"]] == ["naver-long-keyword"] and other["carried"]["left_out"] == ["very-short-brief"]
    assert other["resumed"]["prior_cost_usd"] == pytest.approx(spent)


def test_legacy_folder_without_a_ledger_seeds_it_from_the_case_files(tmp_path):
    out = tmp_path / "old"
    (out / "cases").mkdir(parents=True)
    (out / "cases" / "x.json").write_text(json.dumps({"discarded_cost_usd": 0.5, "reps": [
        {"rep": 1, "cost_usd": 1.25, "judge_cost_usd": 0.25}]}), encoding="utf-8")
    ledger = runner_mod.SpendLedger(out / SPEND_FILE)
    assert ledger.seed_from_case_files(out) == pytest.approx(2.0) and ledger.total == pytest.approx(2.0)
    assert runner_mod.SpendLedger(out / SPEND_FILE).total == pytest.approx(2.0)  # persisted once
    assert runner_mod.SpendLedger(out / SPEND_FILE).seed_from_case_files(out) == 0.0


# ---------------------------------------------------------------------------
# Unpriced fallback, reps, confirmation prompt
# ---------------------------------------------------------------------------


def _fallback_response(requested: str, served: str) -> dict:
    return {"model": served, "usage": {"input_tokens": 200_000, "output_tokens": 20_000, "iterations": [
        {"model": requested, "input_tokens": 2_000, "output_tokens": 50},  # the declined attempt, priced
        {"model": served, "input_tokens": 200_000, "output_tokens": 20_000}]}}


@pytest.mark.usefixtures("no_network")
def test_unpriced_fallback_after_a_priced_declined_attempt_stops_the_eval(tmp_path, user_home):
    prices, web = load_prices(home=user_home)

    def record(agent: str = "orchestrator", task: str = "draft") -> UsageRecord:
        return usage_from_response(_fallback_response("claude-opus-5", "claude-mystery-9"), agent=agent, task=task,
                                   model="claude-opus-5", prices=prices, web_search_per_1k=web)

    assert record().cost_usd > 0  # the declined attempt is billed, so the cost alone cannot reveal the fallback
    collector = UsageCollector(prices)
    collector(record())
    assert collector.unpriced == {"claude-mystery-9"}

    class FallbackMixed(MockBackend):
        def _usage(self, agent, task, prompt, output):  # noqa: ANN001
            if self.on_usage is not None:
                self.on_usage(record(agent, task))

    lines: list[str] = []
    summary = run_eval(EvalOptions(mode="live", cases_dir=CASES, out_dir=tmp_path / "f", max_cost_usd=5.0,
                                   case_ids=["very-short-brief", "naver-long-keyword"]),
                       printer=lines.append, backend_factory=FallbackMixed)
    assert summary["cases"][0]["unpriced_models"] == ["claude-mystery-9"]
    assert summary["cases"][1]["status"] == "skipped" and "가격을 모르는 모델" in summary["stopped"]
    assert summary["exit_code"] == 1


def test_merge_outcomes_separates_unevaluated_reps_from_failures():
    ok = {"key": "k", "type": "all_checks_pass", "label": "L", "passed": True, "detail": "모두 통과"}
    bad = {**ok, "passed": False, "detail": "실패: 해시태그"}
    missing = {**ok, "passed": False, "detail": "평가하지 못했어요: API 오류", "error": True}
    merged = _merge_outcomes([[ok], [missing]])[0]
    assert merged["error"] and not merged["passed"] and merged["reps_unevaluated"] == 1
    assert merged["detail"].startswith("반복 2번 중 1번은 평가하지 못했어요")
    failed = _merge_outcomes([[missing], [bad]])[0]
    assert "error" not in failed and not failed["passed"] and failed["detail"] == "실패: 해시태그"
    assert _merge_outcomes([[missing], [missing]])[0]["error"]
    assert _merge_outcomes([[ok], [ok]])[0]["passed"]


def test_an_unevaluated_rep_is_not_a_quality_regression(tmp_path):
    instances = {"n": 0}

    class SecondRepFails(MockBackend):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances["n"] += 1
            self.rep = instances["n"]

        def _usage(self, agent, task, prompt, output):  # noqa: ANN001
            if self.rep == 2:
                raise BackendError("API 서버가 잠시 응답하지 않아요 (529 overloaded)")
            super()._usage(agent, task, prompt, output)

    base = run_eval(EvalOptions(mode="mock", cases_dir=CASES, out_dir=tmp_path / "base", case_ids=["very-short-brief"]),
                    printer=lambda _line: None)
    assert base["exit_code"] == 0
    cur = run_eval(EvalOptions(mode="mock", cases_dir=CASES, out_dir=tmp_path / "cur", case_ids=["very-short-brief"],
                               reps=2, baseline=tmp_path / "base"), printer=lambda _line: None,
                   backend_factory=SecondRepFails)
    totals = cur["totals"]
    assert totals["must_failed"] == 0 and totals["must_errors"] == 2 and cur["exit_code"] == 1
    cmp = cur["baseline"]
    assert cmp["regressions"] == [] and cmp["partial"] and not cmp["missing"]
    report = (tmp_path / "cur" / "report.md").read_text(encoding="utf-8")
    top = report.split("## 먼저 볼 것", 1)[1].split("## ", 1)[0]
    assert "일부 반복에서 평가하지 못했어요" in top and "반복 2번 중 1번은 평가하지 못했어요" in top
    assert "통과하던 필수 조건" not in report  # never reported as a regression of the drafts


@pytest.mark.usefixtures("no_network")
def test_live_confirmation_prompt_goes_to_stderr_and_json_stays_clean(tmp_path, monkeypatch, capsys):
    asked: list[tuple] = []
    monkeypatch.setattr(runner_mod.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda *args: asked.append(args) or "n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-dummy-never-used")
    code = main(["eval", "run", "--mode", "live", "--case", "very-short-brief", "--max-cost-usd", "5", "--json",
                 "--out", str(tmp_path / "x")])
    captured = capsys.readouterr()
    assert code == 1 and asked == [()]  # input() gets no prompt: it would write it to stdout
    assert json.loads(captured.out)["cancelled"] is True
    assert "live 평가를 시작할까요?" in captured.err and "시작하지 않았어요" in captured.err
    assert not (tmp_path / "x").exists()


# ---------------------------------------------------------------------------
# Round 2: resume keeps unreached reps, Ctrl+C during the late wait, narrower --channels, late call before the next run,
# settings and grader fingerprints
# ---------------------------------------------------------------------------


def _interrupting_wait(interrupt_now: threading.Event, pressed: threading.Event):
    """``_Worker.wait`` where Ctrl+C arrives while the eval waits for the run (no real signal: deterministic)."""

    def wait(self, seconds=None):  # noqa: ANN001
        deadline = None if seconds is None else time.monotonic() + seconds
        while self.alive and (deadline is None or time.monotonic() < deadline):
            if interrupt_now.is_set() and not pressed.is_set():
                pressed.set()
                raise KeyboardInterrupt
            self._done.wait(0.02)
        return not self.alive

    return wait


def _case_file(folder: Path, case_id: str) -> dict:
    return json.loads((folder / "cases" / f"{case_id}.json").read_text(encoding="utf-8"))


@pytest.mark.usefixtures("no_network")
def test_interrupted_resume_keeps_an_earlier_finished_rep_it_had_not_reached(tmp_path, monkeypatch):
    """--reps 2: rep 1 failed, rep 2 finished. A --resume re-runs rep 1 and is stopped with Ctrl+C: rep 2 must stay in
    the case file (and be reused next time), never be paid for again."""
    state = {"attempt": 1, "instance": 0}
    billed = {"usd": 0.0}
    interrupt_now, pressed = threading.Event(), threading.Event()

    class Flaky(PricedMock):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            state["instance"] += 1
            self.no, self.calls = state["instance"], 0

        def _usage(self, agent, task, prompt, output):  # noqa: ANN001
            super()._usage(agent, task, prompt, output)
            billed["usd"] += 0.4
            self.calls += 1
            if state["attempt"] == 1 and self.no == 1 and self.calls == 2:
                raise BackendError("API 서버가 잠시 응답하지 않아요 (529 overloaded)")
            if state["attempt"] == 2 and self.calls == 2:
                interrupt_now.set()
                pressed.wait(5)

    monkeypatch.setattr(runner_mod._Worker, "wait", _interrupting_wait(interrupt_now, pressed))
    out = tmp_path / "live"
    options = dict(mode="live", cases_dir=CASES, out_dir=out, case_ids=["very-short-brief"], reps=2, max_cost_usd=50.0)
    run_eval(EvalOptions(**options), printer=lambda _line: None, backend_factory=Flaky)
    first = {r["rep"]: r for r in _case_file(out, "very-short-brief")["reps"]}
    assert first[1]["status"] == "error" and first[2]["status"] == "ok"

    state.update(attempt=2, instance=0)
    with pytest.raises(KeyboardInterrupt):
        run_eval(EvalOptions(resume=True, **options), printer=lambda _line: None, backend_factory=Flaky)
    data = _case_file(out, "very-short-brief")
    reps = {r["rep"]: r for r in data["reps"]}
    assert reps[1]["status"] == "cancelled" and reps[2] == first[2]  # the finished rep is still there, untouched
    # every cent is in the file exactly once: the replaced rep-1 attempt moved to the discarded cost
    assert data["discarded_cost_usd"] == pytest.approx(first[1]["cost_usd"])
    assert data["discarded_cost_usd"] + sum(r["cost_usd"] for r in reps.values()) == pytest.approx(_ledger_total(out))
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["cases"][0]["reps"] == 2  # the summary shows the finished rep too

    state.update(attempt=3, instance=0)
    before = _ledger_total(out)
    lines: list[str] = []
    again = run_eval(EvalOptions(resume=True, **options), printer=lines.append, backend_factory=Flaky)
    assert again["resumed"]["reused_runs"] == 1 and again["cases"][0]["status"] == "ok"
    assert _ledger_total(out) - before == pytest.approx(again["cases"][0]["cost_usd"] - first[2]["cost_usd"])
    assert _ledger_total(out) == pytest.approx(billed["usd"])


@pytest.mark.usefixtures("no_network")
def test_ctrl_c_during_the_final_late_call_wait_still_writes_summary_and_report(tmp_path):
    release = threading.Event()

    class HangingSecondCall(PricedMock):
        calls = 0

        def _usage(self, agent, task, prompt, output):  # noqa: ANN001
            HangingSecondCall.calls += 1
            if HangingSecondCall.calls == 2:
                release.wait(10)
            super()._usage(agent, task, prompt, output)

    def printer(line: str) -> None:
        if "그 비용까지 기록하려고요" in line:  # the person presses Ctrl+C during "최대 N초 기다려요"
            raise KeyboardInterrupt

    out = tmp_path / "live"
    try:
        with pytest.raises(KeyboardInterrupt):
            run_eval(EvalOptions(mode="live", cases_dir=CASES, out_dir=out, max_cost_usd=10.0, timeout_s=0.3,
                                 case_ids=["very-short-brief"]), printer=printer, backend_factory=HangingSecondCall)
        summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        assert summary["interrupted"] and summary["exit_code"] == 1
        assert summary["cases"][0]["status"] == "timeout" and summary["totals"]["cost_incomplete_runs"] == 1
        assert "비용 일부 빠짐" in (out / "report.md").read_text(encoding="utf-8")
    finally:
        release.set()


def test_resume_with_narrower_channels_is_refused_before_anything_runs(tmp_path):
    out = tmp_path / "mock"
    common = dict(mode="mock", cases_dir=CASES, out_dir=out, case_ids=["user-docs-traction"])
    first = run_eval(EvalOptions(**common), printer=lambda _line: None)
    assert first["plan"]["scope"] == [["user-docs-traction", "bizplan"], ["user-docs-traction", "linkedin"]]
    before = (out / "summary.json").read_text(encoding="utf-8")
    lines: list[str] = []
    with pytest.raises(runner_mod.UsageError) as caught:
        run_eval(EvalOptions(resume=True, channels=["linkedin"], **common), printer=lines.append)
    message = str(caught.value)
    assert "빠지는 채널 bizplan" in message and "같은 --channels" in message and lines == []
    assert (out / "summary.json").read_text(encoding="utf-8") == before  # nothing was touched
    assert main(["eval", "run", "--mode", "mock", "--case", "user-docs-traction", "--channels", "linkedin",
                 "--resume", "--out", str(out)]) == 2
    # the same channels are fine (finished runs reused)
    same = run_eval(EvalOptions(resume=True, **common), printer=lambda _line: None)
    assert same["resumed"]["reused_runs"] == 1
    # with other settings the earlier results could not be kept anyway: allowed, and said out loud
    lines.clear()
    other = run_eval(EvalOptions(resume=True, channels=["linkedin"], pass_score=90, **common), printer=lines.append)
    assert other["carried"]["dropped_channels"] == [["user-docs-traction", "bizplan"]]
    assert any("요약에서 빠지는 이전 채널: user-docs-traction의 bizplan" in line for line in lines)
    assert "요약에서 빠진 이전 채널" in (out / "report.md").read_text(encoding="utf-8")


class _LateSecondCall(MockBackend):
    """Case 1's second call hangs until ``release`` and is billed $2.00; every other call $0.40."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        type(self).instances += 1
        self.case_no, self.calls = type(self).instances, 0

    def _usage(self, agent, task, prompt, output):  # noqa: ANN001
        self.calls += 1
        cost = 0.4
        if self.case_no == 1 and self.calls == 2:
            type(self).release.wait(10)
            cost = 2.0
        type(self).billed += cost
        if self.on_usage is not None:
            self.on_usage(UsageRecord(agent=agent, task=task, model="claude-opus-5", input_tokens=1000, output_tokens=100,
                                      cost_usd=cost))
        if cost == 2.0:
            type(self).late_billed.set()


def _late_backend():
    return type("LateSecondCall", (_LateSecondCall,), {"instances": 0, "billed": 0.0, "release": threading.Event(),
                                                        "late_billed": threading.Event()})


@pytest.mark.usefixtures("no_network")
def test_next_case_waits_for_a_late_call_that_outlives_the_grace_wait(tmp_path, monkeypatch):
    """The timed-out call finishes only after the grace wait: the next case must not start with a cap that leaves it
    out (it waits for it), and when it never finishes the eval stops rather than risk the budget."""
    caps: list[tuple[str, float]] = []
    real_run_case = runner_mod.run_case

    def spy(planned, rep, *args, **kwargs):  # noqa: ANN001
        caps.append((planned.case.id, kwargs["remaining_cap"]))
        return real_run_case(planned, rep, *args, **kwargs)

    monkeypatch.setattr(runner_mod, "run_case", spy)
    two = dict(mode="live", cases_dir=CASES, max_cost_usd=10.0, timeout_s=0.3,
               case_ids=["very-short-brief", "naver-long-keyword"])

    # (a) it finishes while the eval waits before case 2
    backend = _late_backend()

    def printer(line: str) -> None:
        if "다음 실행을 시작하면" in line:
            assert not backend.late_billed.is_set()  # still running after the grace wait
            threading.Timer(0.05, backend.release.set).start()

    out = tmp_path / "a"
    summary = run_eval(EvalOptions(out_dir=out, **two), printer=printer, backend_factory=backend)
    assert caps[1] == ("naver-long-keyword", pytest.approx(10.0 - 2.4))  # the late $2 is in case 2's cap
    assert summary["cases"][0]["cost_usd"] == pytest.approx(2.4) and not summary["cases"][0]["cost_incomplete"]
    assert summary["cases"][1]["status"] == "ok" and not summary["stopped"]
    assert _ledger_total(out) == pytest.approx(backend.billed)

    # (b) it never finishes in time: case 2 is skipped, the budget is never at risk
    caps.clear()
    backend = _late_backend()
    out = tmp_path / "b"
    try:
        summary = run_eval(EvalOptions(out_dir=out, **two), printer=lambda _line: None, backend_factory=backend)
        assert [c for c, _cap in caps] == ["very-short-brief"]
        assert summary["cases"][1]["status"] == "skipped" and "끝나지 않아" in summary["stopped"]
        assert summary["exit_code"] == 1 and summary["cases"][0]["cost_incomplete"]
    finally:
        backend.release.set()
    assert backend.late_billed.wait(5)


def test_resume_reruns_when_effort_changes_and_the_settings_cover_the_run_code(tmp_path, monkeypatch):
    base = runner_mod._base_settings(EvalOptions())
    settings = runner_mod.run_settings(base, EvalOptions())
    assert {"effort", "fallbacks", "max_document_chars", "web_search_max_uses", "code", "prompts"} <= set(settings)
    out = tmp_path / "mock"
    common = dict(mode="mock", cases_dir=CASES, out_dir=out, case_ids=["very-short-brief"])
    run_eval(EvalOptions(**common), printer=lambda _line: None)
    monkeypatch.setenv("INSIA_EFFORT_ORCHESTRATOR", "low")
    monkeypatch.setenv("INSIA_FALLBACKS", "0")
    lines: list[str] = []
    again = run_eval(EvalOptions(resume=True, **common), printer=lines.append)
    assert again["resumed"]["reused_runs"] == 0
    assert any("달라진 설정: 추론 노력(INSIA_EFFORT_*), 거절 시 대체 모델(INSIA_FALLBACKS)" in line for line in lines)
    # another run-code fingerprint (a pipeline edit between attempts) is a changed setting too
    monkeypatch.setattr(runner_mod, "RUN_CODE", ("config.py",))
    lines.clear()
    third = run_eval(EvalOptions(resume=True, **common), printer=lines.append)
    assert third["resumed"]["reused_runs"] == 0 and any("파이프라인 코드" in line for line in lines)


def _no_backend(_settings):  # noqa: ANN001
    raise AssertionError("re-grading must not run the pipeline")


def test_resume_regrades_saved_runs_when_the_grader_code_changed(tmp_path, monkeypatch):
    out = tmp_path / "mock"
    common = dict(mode="mock", cases_dir=CASES, out_dir=out)
    first = run_eval(EvalOptions(case_ids=["very-short-brief", "user-docs-traction"], **common), printer=lambda _line: None)
    fresh = {c["id"]: c for c in first["cases"]}
    # the saved grades are from "older grader code": wipe them so a re-grade is visible
    for case_id in ("very-short-brief", "user-docs-traction"):
        path = out / "cases" / f"{case_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["grader"] = "old-grader"
        for rep in data["reps"]:
            for grade in rep["channels"]:
                grade.update(must=[], should=[], checks=[])
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    lines: list[str] = []
    again = run_eval(EvalOptions(resume=True, case_ids=["very-short-brief"], **common), printer=lines.append,
                     backend_factory=_no_backend)
    assert again["resumed"]["reused_runs"] == 1 and again["resumed"]["regraded_runs"] == 1
    assert any("다시 채점해요: very-short-brief" in line for line in lines)
    by_id = {c["id"]: c for c in again["cases"]}
    for case_id in ("very-short-brief", "user-docs-traction"):  # planned and carried case both graded again
        assert by_id[case_id]["channels"] == fresh[case_id]["channels"], case_id
        assert _case_file(out, case_id)["grader"] == runner_mod.grader_fingerprint()
    assert by_id["user-docs-traction"]["carried"]
    assert "저장된 결과로 다시 채점" in (out / "report.md").read_text(encoding="utf-8")
    # a saved run that cannot be read back is run again (never kept with stale grades)
    data = _case_file(out, "very-short-brief")
    data["grader"] = "old-grader"
    (out / "cases" / "very-short-brief.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (out / "runs" / "very-short-brief" / "result.json").unlink()
    lines.clear()
    third = run_eval(EvalOptions(resume=True, case_ids=["very-short-brief"], **common), printer=lines.append)
    assert third["resumed"]["reused_runs"] == 0 and any("저장된 실행 결과(runs/)를 읽지 못해" in line for line in lines)


def test_a_finding_citing_the_users_material_next_to_a_web_source_cannot_settle_its_value():
    """Round 2 of #16: user + web sources, no sources, an unknown source — the user's undecided price stays open; a
    finding whose own text says the value is undecided is open whatever it cites; web values in the same finding count."""
    settled = "## 3. 성장전략\n\n- 수익 모델: 꽃집 단위 월 구독, 요금은 월 29,000원 단일 요금제예요 (자사 자료)."
    restated = "플로노트는 월 29,000원 단일 요금제를 운영한다."  # the hedge dropped
    for ids in (["s5", "s1"], [], ["s9"]):
        pack = ResearchPack(findings=[Finding(id="f5", question_id="q3", claim=restated, source_ids=ids,
                                              confidence="medium")], sources=[WEB_SOURCE, USER_SOURCE])
        grade = _grade_case("user-docs-traction", "bizplan", settled, research=pack)
        price = _numbers(grade)["29,000원"]
        assert not _must(grade, "assumption_marked")["passed"], ids
        assert price["status"] == "unsupported" and price["tentative"] and "research:f5" not in price["origins"], ids
    # the web part of a mixed finding is still research evidence
    mixed = ResearchPack(findings=[Finding(id="f6", question_id="q2", claim="경쟁 앱 평균 월 요금은 3만 5,000원, 플로노트는 월 "
                                           "29,000원이다.", source_ids=["s1", "s5"], confidence="medium")],
                         sources=[WEB_SOURCE, USER_SOURCE])
    grade = _grade_case("user-docs-traction", "bizplan", settled + "\n- 경쟁 앱 평균은 월 3만 5,000원이에요 [s1].", research=mixed)
    assert _numbers(grade)["3만 5,000원"]["status"] == "supported" and _numbers(grade)["29,000원"]["tentative"]
    # a web-only finding that itself says "검토 중" is open evidence; "잠정결과" (a published statistic) is not
    for claim, note, tentative in (("정부는 바우처 한도 500만 원을 검토 중이다.", "", True),
                                   ("바우처 한도는 500만 원이다.", "지원 한도 미정, 공고 확인 필요", True),
                                   ("바우처 한도는 500만 원이다.", "2023년 잠정결과 보도자료", False)):
        pack = ResearchPack(findings=[Finding(id="f1", question_id="q1", claim=claim, source_ids=["s1"],
                                              confidence="high", note=note)], sources=[WEB_SOURCE])
        draft = Draft(channel="naver_blog", round=0, title="-", content="바우처 한도는 500만 원이에요 [s1].")
        (mention,) = [m for m in analyze_numbers(draft, Evidence.build(pack)) if m.kind == "claim"]
        assert mention.tentative is tentative and (mention.status == "supported") is (not tentative), (claim, note)


def test_rates_dates_labels_and_honorifics_are_not_invented_figures():
    """Round 2 of #17: routine wording in advice is not a claim; effect figures still are."""
    wording = ("주 5일 근무, 하루 8시간, 매일 30분, 주 40시간, 1주일에 한 번, 1시간 무료. 마감은 28일까지, 매달 25일 정산, "
               "10일(금) 오픈. 24시간 상담, 365일 연중무휴. 402번 버스, 100번 국도. 10분이 신청했어요. 3 L사이즈, 5g 요금제. "
               "리뷰는 24시간 안에 답하세요.")
    draft = Draft(channel="naver_blog", round=0, title="-", content=wording)
    assert [m.text for m in analyze_numbers(draft, Evidence.build(None)) if m.kind == "claim"] == []
    claims = ("하루 2시간 절약돼요. 주 3시간 단축. 하루 평균 11시간 일해요. 8주 만에 5cm 줄었어요. 14일 만에 효과. "
              "50분이 신청했어요. 선착순 30분. 3KG 감량, 500ML 한 병, 12Kcal. 30분이면 충분해요.")
    draft = Draft(channel="naver_blog", round=0, title="-", content=claims)
    found = {m.text: m.family for m in analyze_numbers(draft, Evidence.build(None)) if m.kind == "claim"}
    assert found == {"2시간": "hours", "3시간": "hours", "11시간": "hours", "8주": "weeks", "5cm": "length",
                     "14일": "days", "50분": "people", "30분": "minutes", "3KG": "mass", "500ML": "volume",
                     "12Kcal": "kcal"}
    # an honorific head count and upper-case units are compared like the evidence says them
    pack = ResearchPack(findings=[Finding(id="f1", question_id="q1", claim="설명회 신청자는 50명, 평균 감량 3,000g이었다.",
                                          source_ids=["s1"], confidence="high")], sources=[WEB_SOURCE])
    draft = Draft(channel="naver_blog", round=0, title="-", content="설명회에 50분이 신청했어요. 평균 3KG 줄었어요.")
    assert {m.text: m.status for m in analyze_numbers(draft, Evidence.build(pack))} == {"50분": "supported",
                                                                                         "3KG": "supported"}
    # the local bakery's advice post keeps its must (no invented figures in it)
    advice = ("## 리뷰 답글 요령\n\n리뷰는 24시간 안에 답하세요. 주 3회 게시하고 하루 30분만 투자해도 충분해요.\n\n"
              "마감은 매달 25일까지 정리해요.")
    grade = _grade_case("local-bakery-no-profile", "naver_blog", advice)
    assert _must(grade, "max_unsupported_numbers")["passed"]


def test_a_note_after_a_list_covers_only_the_figures_it_is_about():
    """Round 2 of #18: '※ 가정: 요금은 제안 가격' after a list marks its prices, not an invented statistic in another
    bullet; a note that names no kind covers every item; wrapped lines and double blank lines stay in the list."""
    def statuses(content: str) -> dict[str, str]:
        draft = Draft(channel="bizplan", round=0, title="-", content=content)
        return {m.text: m.status for m in analyze_numbers(draft, Evidence.build(None)) if m.kind == "claim"}

    note = "※ 가정: 요금은 시장 검증 전 제안 가격"
    assert statuses(f"- 국내 세무사 1만 3천 명\n- 베이직 월 19,000원\n{note}") == {"1만 3천 명": "unsupported",
                                                                               "19,000원": "assumed"}
    assert statuses(f"- 베이직 월 19,000원\n- 소상공인 70%가 어려움을 겪어요\n{note}") == {"19,000원": "assumed",
                                                                                "70%": "unsupported"}
    assert statuses("- 1차년도 고객 500명\n- 월 매출 950만 원\n※ 가정: 내부 추정치") == {"500명": "assumed", "950만 원": "assumed"}
    assert statuses("- 소상공인 70%\n- 요금 월 19,000원\n※ 위 수치는 모두 가정이에요") == {"70%": "assumed", "19,000원": "assumed"}
    assert statuses(f"- 베이직 월 19,000원\n  (연간 결제 시 월 15,000원)\n- 프로 월 39,000원\n{note}") == {
        "19,000원": "assumed", "15,000원": "assumed", "39,000원": "assumed"}
    assert statuses(f"- 베이직 월 19,000원\n\n\n- 프로 월 39,000원\n{note}") == {"19,000원": "assumed", "39,000원": "assumed"}
    # a lone paragraph and a table stay fully covered by the note right after them
    assert statuses(f"세무사 1만 3천 명, 요금 월 19,000원이에요.\n{note}") == {"1만 3천 명": "assumed", "19,000원": "assumed"}
    assert statuses(f"| 고객 | 1,000명 |\n| 단가 | 19,000원 |\n\n{note}") == {"1,000명": "assumed", "19,000원": "assumed"}
    # the pricing case: an invented market size in the price list is still an unsupported claim
    grade = _grade_case("pricing-assumption", "naver_blog",
                        "## 요금\n\n- 국내 세무사 1만 3천 명이 대상이에요\n- 베이직: 월 19,000원\n- 프로: 월 39,000원\n\n" + note)
    assert _must(grade, "assumption_marked")["passed"]
    assert _numbers(grade)["1만 3천 명"]["status"] == "unsupported"
