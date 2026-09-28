"""Pipeline + workspace: persistence as it happens, profile plumbing, budget cap, crash → resume."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace

import pytest

from insia_agents.backends.base import APICallError, BackendError, RunContext
from insia_agents.backends.mock_backend import MockBackend
from insia_agents.db import NotFoundError, Workspace, WorkspaceError, pipeline_item_id
from insia_agents.events import EventBus, SimClock
from insia_agents.models import ALL_CHANNELS, Profile, TeamMember, UsageRecord
from insia_agents.pipeline import (BudgetExceeded, PipelineError, build_context, execute_run, resume_run, run_pipeline)


@pytest.fixture
def ws(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    yield workspace
    workspace.close()


# Fixed virtual durations so the interleaving of channels (and therefore what
# has finished when a run stops) never depends on the mock's own timing table.
DRAFT_SECONDS = {"linkedin": 10.0, "instagram": 12.0, "naver_blog": 14.0, "bizplan": 40.0}


class TimedBackend(MockBackend):
    """Mock backend with fixed step durations that logs every backend call."""

    def __init__(self, settings):
        super().__init__(settings)
        self.calls: list[tuple[str, str]] = []

    def sim_seconds(self, kind, channel=None, round=0):
        if kind == "draft":
            return DRAFT_SECONDS[channel]
        if kind == "review":
            return 5.0
        if kind == "revise":
            return 8.0
        return 0.5 if kind == "handoff" else 1.0

    def plan(self, brief):
        self.calls.append(("plan", ""))
        return super().plan(brief)

    def research(self, brief, questions, emit, existing=None):
        self.calls.append(("followup" if existing is not None else "research", ""))
        return super().research(brief, questions, emit, existing)

    def draft(self, brief, plan, research, channel):
        self.calls.append(("draft", channel))
        return super().draft(brief, plan, research, channel)

    def review(self, brief, research, draft, format_checks):
        self.calls.append(("review", draft.channel))
        return super().review(brief, research, draft, format_checks)

    def revise(self, brief, plan, research, draft, review, *args, **kwargs):
        self.calls.append(("revise", draft.channel))
        return super().revise(brief, plan, research, draft, review, *args, **kwargs)

    def channel_calls(self, channel):
        return [kind for kind, ch in self.calls if ch == channel]


# ---------------------------------------------------------------------------
# Persistence of a full run
# ---------------------------------------------------------------------------


def test_full_mock_run_persists_everything(settings, brief, ws):
    result, bus = execute_run(brief, settings, workspace=ws)
    run = ws.get_run(result.run_id)
    json.dumps(run, ensure_ascii=False)
    assert run["status"] == "completed" and run["kind"] == "pipeline" and run["error"] is None
    assert run["mode"] == "mock" and run["finished_at"]
    assert run["plan"]["summary"] == result.plan.summary
    assert len(run["research"]["findings"]) == len(result.research.findings)  # includes follow-up findings
    assert run["progress"]["channels"] == {ch: "completed" for ch in ALL_CHANNELS}
    assert run["options"]["max_rounds"] == 2 and run["options"]["pass_score"] == 80

    # every event is in the DB, identical to the live stream
    stored = ws.list_events(result.run_id)
    assert stored == bus.events
    assert bus.events[-1]["data"]["items"] == {ch: pipeline_item_id(result.run_id, ch) for ch in ALL_CHANNELS}

    # every round of every channel is a version with its review
    for channel_result in result.results:
        detail = ws.get_item(pipeline_item_id(result.run_id, channel_result.channel))
        assert detail is not None and detail.item.run_id == result.run_id
        rounds = [v for v in detail.versions if v.draft.round in {d.round for d in channel_result.drafts}]
        assert [v.draft.round for v in detail.versions[: len(channel_result.drafts)]] == [d.round for d in channel_result.drafts]
        assert [v.review.score for v in detail.versions[: len(channel_result.drafts)]] == [r.score for r in channel_result.reviews]
        assert all(v.source == "agent" for v in rounds)
        latest = detail.versions[-1]
        assert latest.draft.content == channel_result.final.content
        assert detail.item.passed == channel_result.passed and detail.item.score == latest.review.score
        assert detail.item.status == ("draft" if channel_result.passed else "needs_changes")
        assert detail.brief.topic == brief.topic
    revised = [r for r in result.results if len(r.drafts) > 1]
    assert revised, "the mock run must include a revision loop"
    summary = ws.list_runs()[0]
    assert summary["run_id"] == result.run_id and set(summary["scores"]) == set(ALL_CHANNELS)


def test_run_without_workspace_still_works(settings, brief):
    result, bus = execute_run(brief, settings)
    assert bus.events[-1]["type"] == "run.completed" and "items" not in bus.events[-1]["data"]
    assert len(result.results) == 4


def test_server_created_run_row_is_reused(settings, brief, ws):
    backend = MockBackend(settings)
    bus = EventBus("server-made-run", clock=SimClock(0))
    ws.create_run("server-made-run", brief, options={"requested_by": "dashboard"})
    run_pipeline(brief, backend, bus, settings, workspace=ws)
    run = ws.get_run("server-made-run")
    assert run["status"] == "completed" and run["options"]["requested_by"] == "dashboard"
    assert run["options"]["max_rounds"] == 2


# ---------------------------------------------------------------------------
# Run context: profile and documents
# ---------------------------------------------------------------------------


class ContextRecordingBackend(MockBackend):
    def __init__(self, settings):
        super().__init__(settings)
        self.seen_contexts: list = []

    def draft(self, brief, plan, research, channel):
        self.seen_contexts.append(getattr(self, "context", None))
        return super().draft(brief, plan, research, channel)


def test_profile_reaches_backend_and_brand_checks(settings, brief, ws):
    profile = ws.save_profile(Profile(company_name="인시아", banned_words=["데모"], required_phrases=["광고 아님"],
                                      team=[TeamMember(role="대표", name="김인시")]))
    backend = ContextRecordingBackend(settings)
    result, bus = execute_run(brief, settings, workspace=ws, backend=backend)
    assert backend.seen_contexts and all(c.profile == profile for c in backend.seen_contexts)
    assert all(c.today == "2026-09-28" for c in backend.seen_contexts)
    reviews = [e["data"] for e in bus.events if e["type"] == "review.completed"]
    ids = {r["channel"]: {c["id"] for c in r["format_checks"]} for r in reviews}
    assert "banned_words" in ids["linkedin"] and "required_phrases" in ids["linkedin"]
    assert {"banned_words", "blind_names"} <= ids["bizplan"] and "required_phrases" not in ids["bizplan"]
    banned = next(c for c in reviews[0]["format_checks"] if c["id"] == "banned_words")
    assert banned["passed"] is False  # the mock templates say "[데모]"
    assert ws.get_run(result.run_id)["profile"]["company_name"] == "인시아"
    assert ws.get_run(result.run_id)["options"]["use_profile"] is True


def test_use_profile_off_and_empty_profile_mean_no_brand_checks(settings, brief, ws):
    ws.save_profile(Profile(banned_words=["데모"]))
    result, bus = execute_run(brief, replace(settings, use_profile=False), workspace=ws)
    checks = {c["id"] for e in bus.events if e["type"] == "review.completed" for c in e["data"]["format_checks"]}
    assert "banned_words" not in checks
    assert ws.get_run(result.run_id)["profile"] is None
    ws.save_profile(Profile())
    assert build_context(ws, settings).profile is None


def test_build_context_documents(settings, ws):
    first = ws.add_document("회사 소개", "소개 본문")
    second = ws.add_document("IR", "IR 본문")
    assert build_context(ws, settings).documents == []
    assert [d.id for d in build_context(ws, settings, docs="all").documents] == [first.id, second.id]
    assert [d.id for d in build_context(ws, settings, docs="u2").documents] == ["u2"]
    assert [d.id for d in build_context(ws, settings, docs=["u1"]).documents] == ["u1"]
    assert build_context(ws, settings, docs="none").documents == []
    with pytest.raises(WorkspaceError, match="u9"):
        build_context(ws, settings, docs="u1,u9")
    assert build_context(None, settings, docs="all") == RunContext(None, [], "2026-09-28")


def test_run_options_record_documents(settings, brief, ws):
    ws.add_document("회사 소개", "소개 본문")
    context = build_context(ws, settings, docs="all")
    result, bus = execute_run(brief, settings, workspace=ws, context=context)
    assert ws.get_run(result.run_id)["options"]["doc_ids"] == ["u1"]
    assert any("사용자 자료 1개" in e["data"].get("message", "") for e in bus.events if e["type"] == "log")


# ---------------------------------------------------------------------------
# Budget cap
# ---------------------------------------------------------------------------


class PricedBackend(TimedBackend):
    """Every review costs $0.60 (reported through on_usage like the live backend)."""

    review_cost = 0.6

    def review(self, brief, research, draft, format_checks):
        out = super().review(brief, research, draft, format_checks)
        if getattr(self, "on_usage", None):
            self.on_usage(UsageRecord(agent="reviewer", task="review", model="claude-opus-5", input_tokens=1000,
                                      output_tokens=200, cost_usd=self.review_cost))
        return out


def test_budget_cap_stops_run_and_keeps_finished_channels(settings, brief, ws):
    capped = replace(settings, max_cost_usd=1.0, max_rounds=0)
    backend = PricedBackend(capped)
    bus = EventBus("budget-run", clock=SimClock(0))
    with pytest.raises(BudgetExceeded) as stop:
        run_pipeline(brief, backend, bus, capped, workspace=ws, out_dir=capped.out_dir)
    exc = stop.value
    assert "예산 상한 $1.00를 넘어 실행을 멈췄어요" in str(exc)
    assert "끝난 채널(링크드인, 인스타그램)은 저장해 뒀어요" in str(exc)
    # linkedin and instagram finished their review before the cap was hit; the others were stopped
    assert exc.completed == ["linkedin", "instagram"] and exc.stopped == ["bizplan", "naver_blog"]
    assert exc.spent == pytest.approx(1.2) and exc.result is not None
    assert {r.channel for r in exc.result.results} == {"linkedin", "instagram"}
    assert backend.calls.count(("review", "naver_blog")) == 0  # no new paid call after the cap

    failed = bus.events[-1]
    assert failed["type"] == "run.failed" and failed["data"]["budget_exceeded"] is True
    assert failed["data"]["completed_channels"] == ["linkedin", "instagram"]
    run = ws.get_run("budget-run")
    assert run["status"] == "failed" and "예산 상한" in run["error"]
    assert run["cost_usd"] == pytest.approx(1.2) and ws.run_cost("budget-run") == pytest.approx(1.2)
    assert run["progress"]["channels"] == {"linkedin": "completed", "instagram": "completed",
                                           "bizplan": "stopped", "naver_blog": "stopped"}
    for ch in ("linkedin", "instagram"):
        detail = ws.get_item(pipeline_item_id("budget-run", ch))
        assert len(detail.versions) == 1 and detail.versions[0].review is not None
    blog = ws.get_item(pipeline_item_id("budget-run", "naver_blog"))
    assert len(blog.versions) == 1 and blog.versions[0].review is None  # drafted, stopped before its review
    out = capped.out_dir / "budget-run"
    assert (out / "linkedin.md").is_file() and not (out / "bizplan.md").exists()

    # a higher budget lets the same run finish; finished channels are not redone
    resumed_backend = PricedBackend(settings)
    result = resume_run("budget-run", replace(capped, max_cost_usd=10.0), ws, backend=resumed_backend)
    assert {r.channel for r in result.results} == set(ALL_CHANNELS)
    assert resumed_backend.channel_calls("linkedin") == [] and resumed_backend.channel_calls("instagram") == []
    assert resumed_backend.channel_calls("naver_blog") == ["review"]  # its R0 draft was kept
    skipped = [e["data"]["message"] for e in ws.list_events("budget-run") if e["type"] == "log" and "건너뛰어요" in e["data"]["message"]]
    assert skipped == ["링크드인은 이미 끝나서 건너뛰어요", "인스타그램은 이미 끝나서 건너뛰어요"]
    assert resumed_backend.channel_calls("bizplan") == ["review"]
    run = ws.get_run("budget-run")
    assert run["status"] == "completed" and run["cost_usd"] == pytest.approx(2.4)


def test_budget_already_spent_stops_before_any_call(settings, brief, ws):
    capped = replace(settings, max_cost_usd=0.5)
    ws.create_run("spent-run", brief)
    ws.record_usage(UsageRecord(run_id="spent-run", task="plan", cost_usd=0.75))
    ws.update_run("spent-run", status="failed")
    backend = TimedBackend(capped)
    with pytest.raises(BudgetExceeded):
        resume_run("spent-run", capped, ws, backend=backend)
    assert backend.calls == []


def test_backends_that_never_report_usage_are_fine(settings, brief, ws):
    result, _ = execute_run(brief, replace(settings, max_cost_usd=0.01), workspace=ws)
    assert ws.get_run(result.run_id)["status"] == "completed"


# ---------------------------------------------------------------------------
# Crash → resume
# ---------------------------------------------------------------------------


class SimulatedCrash(BaseException):
    """Stands in for the process dying (not an Exception, so nothing catches it)."""


class CrashingBackend(TimedBackend):
    def revise(self, brief, plan, research, draft, review, *args, **kwargs):
        if draft.channel == "naver_blog":
            raise SimulatedCrash("kill -9")
        return super().revise(brief, plan, research, draft, review, *args, **kwargs)


def _crash(settings, brief, ws, run_id="crash-run"):
    bus = EventBus(run_id, clock=SimClock(0))
    with pytest.raises(SimulatedCrash):
        run_pipeline(brief, CrashingBackend(settings), bus, settings, workspace=ws, out_dir=settings.out_dir)
    return bus


def test_resume_after_crash_finishes_only_the_remaining_work(settings, brief, ws):
    first = _crash(settings, brief, ws)
    run = ws.get_run("crash-run")
    assert run["status"] == "failed" and "SimulatedCrash" in run["error"]
    assert run["progress"]["channels"] == {"instagram": "completed"}
    stored = {ch: ws.list_run_versions("crash-run", ch) for ch in ALL_CHANNELS}
    assert [v.review is not None for v in stored["naver_blog"]] == [True]
    assert [v.review is not None for v in stored["linkedin"]] == [True]  # its R1 was in flight
    assert stored["bizplan"] == []  # still drafting when the process died
    last_seq = first.events[-1]["seq"]

    backend = TimedBackend(settings)
    result = resume_run("crash-run", settings, ws, backend=backend)

    # plan and research were reused; instagram was not touched
    assert ("plan", "") not in backend.calls and ("research", "") not in backend.calls
    assert backend.channel_calls("instagram") == []
    assert backend.channel_calls("naver_blog")[0] == "revise"  # continued from its R0 review
    assert backend.channel_calls("linkedin")[0] == "revise"
    assert backend.channel_calls("bizplan")[0] == "draft"
    assert [c for c in backend.calls if c[0] == "draft"] == [("draft", "bizplan")]

    assert {r.channel for r in result.results} == set(ALL_CHANNELS) and all(r.passed for r in result.results)
    run = ws.get_run("crash-run")
    assert run["status"] == "completed" and run["error"] is None
    assert run["progress"]["channels"] == {ch: "completed" for ch in ALL_CHANNELS}
    for ch in ALL_CHANNELS:
        rounds = [v.draft.round for v in ws.list_run_versions("crash-run", ch)]
        assert rounds == sorted(set(rounds)), f"{ch}: no duplicated rounds"
        assert all(v.review is not None for v in ws.list_run_versions("crash-run", ch))
    assert len(ws.get_item(pipeline_item_id("crash-run", "instagram")).versions) == 1

    # one continuous event stream: seq continues, the resumed part starts with run.started(resumed)
    events = ws.list_events("crash-run")
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    resumed = [e for e in events if e["seq"] > last_seq]
    assert resumed[0]["type"] == "run.started" and resumed[0]["data"]["resumed"] is True
    assert resumed[-1]["type"] == "run.completed"
    assert all(a["t"] <= b["t"] for a, b in zip(events, events[1:]))
    completed_again = [e["data"]["channel"] for e in resumed if e["type"] == "channel.completed"]
    assert sorted(completed_again) == sorted(ALL_CHANNELS)  # instagram re-announced for the dashboard
    assert any(e["type"] == "plan.created" for e in resumed) and any(e["type"] == "research.source" for e in resumed)
    lines = (settings.out_dir / "crash-run" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(events)  # the resumed events were appended, not overwritten


def test_interrupted_run_is_marked_on_startup_and_resumable(settings, brief, ws):
    _crash(settings, brief, ws, run_id="killed-run")
    # what a real kill leaves behind: status still running, no terminal event
    ws.update_run("killed-run", status="running")
    with ws._tx() as conn:
        conn.execute("DELETE FROM events WHERE run_id = 'killed-run' AND type = 'run.failed'")
    last_seq = ws.last_event("killed-run")["seq"]
    with pytest.raises(PipelineError, match="실행 중"):
        resume_run("killed-run", settings, ws, backend=TimedBackend(settings))
    assert ws.mark_interrupted() == 1
    assert ws.get_run("killed-run")["status"] == "interrupted"
    closing = ws.last_event("killed-run")
    assert closing["type"] == "run.failed" and closing["seq"] == last_seq + 1 and closing["data"]["interrupted"] is True
    result = resume_run("killed-run", settings, ws, backend=TimedBackend(settings))
    assert len(result.results) == 4 and ws.get_run("killed-run")["status"] == "completed"
    events = ws.list_events("killed-run")
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert events[last_seq + 1]["type"] == "run.started" and events[last_seq + 1]["data"]["resumed"] is True


def test_double_resume_cannot_both_run(settings, brief, ws, monkeypatch):
    _crash(settings, brief, ws, run_id="twice-run")
    real_claim = ws.claim_run

    def someone_else_first(run_id, expected):
        assert real_claim(run_id, expected)  # the other request wins the race ...
        return real_claim(run_id, expected)  # ... so this one loses

    monkeypatch.setattr(ws, "claim_run", someone_else_first)
    backend = TimedBackend(settings)
    with pytest.raises(PipelineError, match="이미 다른 곳에서"):
        resume_run("twice-run", settings, ws, backend=backend)
    assert backend.calls == []


def test_resume_refusals(settings, brief, ws):
    result, _ = execute_run(brief, settings, workspace=ws)
    with pytest.raises(PipelineError, match="이미 모든 채널"):
        resume_run(result.run_id, settings, ws)
    with pytest.raises(NotFoundError):
        resume_run("no-such-run", settings, ws)
    ws.create_run("job-run", brief, kind="review")
    ws.update_run("job-run", status="failed")
    with pytest.raises(PipelineError, match="이어서 실행할 수 없어요"):
        resume_run("job-run", settings, ws)


class FailingInstagramBackend(MockBackend):
    def draft(self, brief, plan, research, channel):
        if channel == "instagram":
            raise BackendError("인스타그램 테스트 실패")
        return super().draft(brief, plan, research, channel)


def test_resume_retries_a_failed_channel_of_a_completed_run(settings, brief, ws):
    result, bus = execute_run(brief, settings, workspace=ws, backend=FailingInstagramBackend(settings))
    run = ws.get_run(result.run_id)
    assert run["status"] == "completed" and "인스타그램 테스트 실패" in run["error"]
    assert run["progress"]["channels"]["instagram"] == "failed"
    assert "instagram" not in bus.events[-1]["data"]["items"]
    backend = TimedBackend(settings)
    resumed = resume_run(result.run_id, settings, ws, backend=backend)
    assert {c for _, c in backend.calls if c} == {"instagram"}
    assert len(resumed.results) == 4 and ws.get_run(result.run_id)["error"] is None


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


class FlakyPlanBackend(MockBackend):
    def __init__(self, settings, failures, retryable=True):
        super().__init__(settings)
        self.failures = failures
        self.retryable = retryable
        self.plan_calls = 0

    def plan(self, brief):
        self.plan_calls += 1
        if self.plan_calls <= self.failures:
            raise APICallError("Anthropic 서버가 지금 혼잡해요 (529).", kind="server", status_code=529,
                               retryable=self.retryable)
        return super().plan(brief)


def test_retryable_error_is_retried_with_backoff(settings, brief, ws):
    backend = FlakyPlanBackend(settings, failures=2)
    bus = EventBus("retry-run", clock=SimClock(0))
    run_pipeline(brief, backend, bus, settings, workspace=ws)
    assert backend.plan_calls == 3
    warnings = [e for e in bus.events if e["type"] == "log" and "다시 시도" in e["data"]["message"]]
    assert [w["data"]["level"] for w in warnings] == ["warn", "warn"]
    assert "(1/2)" in warnings[0]["data"]["message"] and "(2/2)" in warnings[1]["data"]["message"]
    assert warnings[1]["t"] - warnings[0]["t"] >= 2.0  # the 2 s backoff passed in virtual time
    assert ws.get_run("retry-run")["status"] == "completed"


class ThreadedMock(MockBackend):
    name = "live"  # exercises the ThreadRunner (channels in worker threads) without network


def test_threaded_run_persists_from_worker_threads(settings, brief, ws):
    from insia_agents.events import RealClock
    from insia_agents.pipeline import ThreadRunner

    bus = EventBus("threaded-ws", clock=RealClock())
    result = run_pipeline(brief, ThreadedMock(settings), bus, settings, runner=ThreadRunner(4), workspace=ws)
    assert ws.list_events("threaded-ws") == bus.events
    assert ws.get_run("threaded-ws")["progress"]["channels"] == {ch: "completed" for ch in ALL_CHANNELS}
    for channel_result in result.results:
        versions = ws.list_run_versions("threaded-ws", channel_result.channel)
        assert [v.draft.round for v in versions] == [d.round for d in channel_result.drafts]
        assert all(v.review is not None for v in versions)
    # follow-up research from a channel thread was saved with continuous ids
    stored = ws.get_run("threaded-ws")["research"]
    assert [s["id"] for s in stored["sources"]] == [f"s{i}" for i in range(1, len(stored["sources"]) + 1)]
    assert len(stored["findings"]) == len(result.research.findings)
