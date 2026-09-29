from __future__ import annotations

import _thread
import json
import threading
import time
from dataclasses import replace

import pytest

from insia_agents.backends.base import BackendError
from insia_agents.backends.mock_backend import MockBackend
from insia_agents.channels import check_format
from insia_agents.events import AGENTS, EVENT_TYPES, EventBus, RealClock, SimClock
from insia_agents.models import ALL_CHANNELS
from insia_agents.pipeline import RunCancelled, SimRunner, ThreadRunner, execute_run, run_pipeline


def _types(bus):
    return [e["type"] for e in bus.events]


def test_mock_pipeline_end_to_end(settings, brief):
    result, bus = execute_run(brief, settings)
    events = bus.events
    types = _types(bus)

    # stream contract
    assert types[0] == "run.started" and types[-1] == "run.completed"
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert all(a["t"] <= b["t"] for a, b in zip(events, events[1:]))
    assert all(e["type"] in EVENT_TYPES and e["agent"] in AGENTS for e in events)
    assert all(e["run_id"] == result.run_id for e in events)
    json.dumps(events, ensure_ascii=False)
    started = events[0]["data"]
    assert started["mode"] == "mock" and started["max_rounds"] == 2 and started["pass_score"] == 80
    assert started["channels"] == list(ALL_CHANNELS)
    assert events[-1]["t"] > 60  # simulated time even at speed 0

    # the review loop actually happened
    assert types.count("channel.completed") == 4
    assert "revision.requested" in types
    assert any(len(r.drafts) >= 2 for r in result.results)
    followups = [e for e in events if e["type"] == "research.completed" and e["data"]["followup"]]
    assert followups, "bizplan review should trigger a follow-up research"
    for event in events:
        data = event["data"]
        if event["type"] == "draft.created":
            assert len(data["excerpt"]) <= 160 and data["chars"] >= data["chars_no_space"] > 0
        if event["type"] == "review.completed":
            assert set(data["fact_checks"]) == {"supported", "unsupported", "needs_source"}
            assert 0 <= data["score"] <= 100
        if event["type"] == "handoff":
            assert {"from", "to", "kind", "label"} <= set(data) and data["kind"] in ("task", "result", "feedback")
        if event["type"] == "agent.status":
            assert data["status"] in ("idle", "planning", "searching", "reading", "writing", "reviewing", "revising",
                                      "waiting", "done", "error")

    # research ids are continuous after the follow-up merge
    assert [s.id for s in result.research.sources] == [f"s{i}" for i in range(1, len(result.research.sources) + 1)]
    assert [f.id for f in result.research.findings] == [f"f{i}" for i in range(1, len(result.research.findings) + 1)]

    # final drafts satisfy the channel formats and pass
    for channel_result in result.results:
        assert channel_result.passed
        assert all(c.passed for c in check_format(channel_result.final, brief)), channel_result.channel
        assert channel_result.rounds == len(channel_result.drafts) - 1

    # outputs on disk
    out = settings.out_dir / result.run_id
    expected = {"brief.json", "plan.json", "research.json", "result.json", "events.jsonl"}
    expected |= {f"{c}.md" for c in ALL_CHANNELS} | {f"{c}.review.json" for c in ALL_CHANNELS}
    assert expected <= {p.name for p in out.iterdir()}
    lines = (out / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(events) and json.loads(lines[-1])["type"] == "run.completed"
    assert events[-1]["data"]["output_dir"] == str(out)
    linkedin_md = (out / "linkedin.md").read_text(encoding="utf-8")
    assert linkedin_md.startswith("# ") and "#" in linkedin_md.splitlines()[-1]
    blog_md = (out / "naver_blog.md").read_text(encoding="utf-8")
    assert "태그:" in blog_md


def _comparable(bus):
    out = []
    for e in bus.events:
        data = dict(e["data"])
        data.pop("output_dir", None)
        out.append((e["type"], e["agent"], e["t"], json.dumps(data, ensure_ascii=False, sort_keys=True)))
    return out


def test_mock_runs_are_deterministic(settings, brief):
    _, first = execute_run(brief, settings)
    _, second = execute_run(brief, settings)
    assert _comparable(first) == _comparable(second)


def test_no_revision_when_max_rounds_zero(settings, brief):
    result, bus = execute_run(brief, replace(settings, max_rounds=0))
    assert "revision.requested" not in _types(bus)
    linkedin = next(r for r in result.results if r.channel == "linkedin")
    assert not linkedin.passed and linkedin.rounds == 0
    assert bus.events[-1]["data"]["passed"]["linkedin"] is False


def test_rounds_are_bounded_and_best_draft_wins(settings, brief):
    result, bus = execute_run(brief, replace(settings, pass_score=99, max_rounds=2))
    for channel_result in result.results:
        assert len(channel_result.drafts) == 3 and not channel_result.passed
        best = max(channel_result.reviews, key=lambda r: (r.passed, r.score, r.round))
        assert channel_result.final.round == best.round
    assert _types(bus).count("revision.requested") == 8


class FailingChannelBackend(MockBackend):
    def draft(self, brief, plan, research, channel):
        if channel == "instagram":
            raise BackendError("인스타그램 테스트 실패")
        return super().draft(brief, plan, research, channel)


def test_one_channel_failure_does_not_stop_others(settings, brief):
    result, bus = execute_run(brief, settings, backend=FailingChannelBackend(settings))
    assert {r.channel for r in result.results} == {"bizplan", "naver_blog", "linkedin"}
    completed = bus.events[-1]
    assert completed["type"] == "run.completed" and "instagram" in completed["data"]["errors"]
    assert any(e["type"] == "log" and e["data"]["level"] == "error" for e in bus.events)


class BrokenPlanBackend(MockBackend):
    def plan(self, brief):
        raise BackendError("계획 단계 테스트 실패")


def test_failure_emits_run_failed(settings, brief):
    bus = EventBus("fail-run", clock=SimClock(0))
    with pytest.raises(BackendError):
        run_pipeline(brief, BrokenPlanBackend(settings), bus, settings, out_dir=settings.out_dir)
    assert bus.events[-1]["type"] == "run.failed"
    assert bus.events[-1]["data"]["error"] == "계획 단계 테스트 실패"
    assert bus.closed


class ThreadedBackend(MockBackend):
    name = "live"  # exercises the live ThreadRunner without network


def test_live_runner_runs_channels_in_threads(settings, brief):
    bus = EventBus("threaded", clock=RealClock())
    result = run_pipeline(brief, ThreadedBackend(settings), bus, settings, runner=ThreadRunner(4), out_dir=None)
    assert {r.channel for r in result.results} == set(ALL_CHANNELS)
    events = bus.events
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert all(a["t"] <= b["t"] for a, b in zip(events, events[1:]))
    assert events[-1]["type"] == "run.completed" and events[-1]["data"]["output_dir"] is None


def test_sim_runner_interleaves_by_virtual_time():
    clock = SimClock(0)
    runner = SimRunner(clock)
    order = []

    def lane(name, delays):
        for d in delays:
            order.append((name, clock.now()))
            yield d
        return name

    results = runner.run_parallel({"a": lane("a", [5, 5]), "b": lane("b", [3, 3, 3])})
    assert results == {"a": "a", "b": "b"}
    assert order == [("a", 0.0), ("b", 0.0), ("b", 3.0), ("a", 5.0), ("b", 6.0)]
    assert clock.now() == 10.0


# ---------------------------------------------------------------------------
# Follow-up research: lock scope, question ids, failure handling
# ---------------------------------------------------------------------------


def _research_ctx(settings, brief, backend):
    from insia_agents.agents.common import AgentContext

    return AgentContext(bus=EventBus("research", clock=RealClock()), backend=backend, settings=settings,
                        brief=brief, simulated=False)


class BlockingFollowupBackend(ThreadedBackend):
    """The first follow-up search blocks until released, like a slow live web search."""

    def __init__(self, settings):
        super().__init__(settings)
        self.in_call = threading.Event()
        self.release = threading.Event()

    def research(self, brief, questions, emit, existing=None):
        if existing is not None and not self.in_call.is_set():
            self.in_call.set()
            self.release.wait(5)
        return super().research(brief, questions, emit, existing)


def test_followup_search_does_not_hold_the_store_lock(settings, brief):
    from insia_agents.agents import researcher

    backend = BlockingFollowupBackend(settings)
    ctx = _research_ctx(settings, brief, backend)
    store = researcher.ResearchStore()
    runner = ThreadRunner()
    runner.run(researcher.run(ctx, store, list(backend.plan(brief).questions)))
    initial_ids = [q.id for q in store.questions]

    slow = threading.Thread(target=runner.run,
                            args=(researcher.followup(ctx, store, ["느린 추가 질문"], "bizplan"),), daemon=True)
    slow.start()
    try:
        assert backend.in_call.wait(5)
        # while that search is in flight, other channels can read the pack and run their own follow-up
        assert store.lock.acquire(timeout=1), "follow-up search must not hold store.lock"
        store.lock.release()
        runner.run(researcher.followup(ctx, store, ["빠른 추가 질문"], "linkedin"))
    finally:
        backend.release.set()
        slow.join(5)
    assert not slow.is_alive()

    pack = store.snapshot()
    ids = [q.id for q in store.questions]
    n = len(initial_ids)
    assert ids == initial_ids + [f"q{n + 1}", f"q{n + 2}"]  # distinct, allocated in request order
    assert [q.question for q in store.questions[n:]] == ["느린 추가 질문", "빠른 추가 질문"]
    assert [s.id for s in pack.sources] == [f"s{i}" for i in range(1, len(pack.sources) + 1)]
    assert [f.id for f in pack.findings] == [f"f{i}" for i in range(1, len(pack.findings) + 1)]
    assert {f.question_id for f in pack.findings} >= {f"q{n + 1}", f"q{n + 2}"}
    known = {s.id for s in pack.sources}
    assert all(set(f.source_ids) <= known for f in pack.findings)


def test_followup_question_ids_follow_the_highest_existing_id(settings, brief):
    from insia_agents.agents import researcher
    from insia_agents.models import ResearchQuestion

    store = researcher.ResearchStore()
    store.questions = [ResearchQuestion(id=qid, question=f"질문 {qid}", why="테스트", channels=["bizplan"],
                                        priority="high") for qid in ("q1", "q3", "q4")]
    ctx = _research_ctx(settings, brief, ThreadedBackend(settings))
    ThreadRunner().run(researcher.followup(ctx, store, ["추가 질문 1", "추가 질문 2"], "bizplan"))
    assert [q.id for q in store.questions] == ["q1", "q3", "q4", "q5", "q6"]


class FailingFollowupBackend(MockBackend):
    def research(self, brief, questions, emit, existing=None):
        if existing is not None:
            raise BackendError("추가 조사 테스트 실패")
        return super().research(brief, questions, emit, existing)


def test_failed_followup_still_revises_and_resets_researcher(settings, brief):
    result, bus = execute_run(brief, settings, backend=FailingFollowupBackend(settings))
    events = bus.events
    assert events[-1]["type"] == "run.completed"
    bizplan = next(r for r in result.results if r.channel == "bizplan")
    assert bizplan.reviews[0].needs_research
    assert len(bizplan.drafts) >= 2, "the revision must still run on the research we already have"

    warn = [e for e in events if e["type"] == "log" and e["agent"] == "researcher" and e["data"]["level"] == "warn"]
    assert warn and "추가 조사 테스트 실패" in warn[0]["data"]["message"]
    assert not any(e["type"] == "research.completed" and e["data"]["followup"] for e in events)
    statuses = [e["data"]["status"] for e in events if e["type"] == "agent.status" and e["agent"] == "researcher"]
    assert statuses[-1] == "done"
    assert "error" in statuses and statuses[-2] == "idle"  # not left 'searching' after the failure


# ---------------------------------------------------------------------------
# Ctrl+C in live mode
# ---------------------------------------------------------------------------


class HangingDraftBackend(ThreadedBackend):
    """Drafts block like a long live API call; counts every backend call made after that."""

    def __init__(self, settings):
        super().__init__(settings)
        self.drafting = threading.Event()
        self.release = threading.Event()
        self.later_calls = 0

    def draft(self, brief, plan, research, channel):
        self.drafting.set()
        self.release.wait(10)
        return super().draft(brief, plan, research, channel)

    def review(self, *args, **kwargs):
        self.later_calls += 1
        return super().review(*args, **kwargs)

    def revise(self, *args, **kwargs):
        self.later_calls += 1
        return super().revise(*args, **kwargs)


def test_ctrl_c_stops_live_channels_without_waiting(settings, brief):
    backend = HangingDraftBackend(settings)
    bus = EventBus("interrupt", clock=RealClock())

    def interrupt_once_drafting():
        if backend.drafting.wait(10):
            _thread.interrupt_main()

    threading.Thread(target=interrupt_once_drafting, daemon=True).start()
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        run_pipeline(brief, backend, bus, settings, runner=ThreadRunner(4), out_dir=None)
    assert time.monotonic() - started < 5, "Ctrl+C must not wait for the channel loops to finish"
    workers = [t for t in threading.enumerate() if t.name.startswith("insia-channel")]
    assert workers and all(t.daemon for t in workers)  # never hold the process open at exit

    backend.release.set()  # the in-flight calls return ...
    for worker in workers:
        worker.join(5)
    assert not any(t.is_alive() for t in workers)
    assert backend.later_calls == 0  # ... and no channel goes on to review or revise
    events = bus.events
    assert events[-1]["type"] == "run.failed" and events[-1]["data"]["error"] == "사용자가 실행을 중단했어요"
    assert "channel.completed" not in _types(bus)


def test_thread_runner_cancel_stops_at_next_step():
    runner = ThreadRunner(2)
    steps = []

    def lane(name):
        steps.append((name, 1))
        if name == "a":
            runner.cancel()
        yield 0.0
        steps.append((name, 2))
        return name

    with pytest.raises(RunCancelled):
        runner.run_parallel({"a": lane("a"), "b": lane("b")})
    assert ("a", 2) not in steps


# ---------------------------------------------------------------------------
# Retries, run context, channel.completed details
# ---------------------------------------------------------------------------


from insia_agents.backends.base import APICallError, RunContext  # noqa: E402
from insia_agents.models import Profile  # noqa: E402


class FlakyDraftBackend(MockBackend):
    """Fails each channel's first draft call ``failures`` times."""

    def __init__(self, settings, failures=1, retryable=True):
        super().__init__(settings)
        self.failures = failures
        self.retryable = retryable
        self.draft_calls: dict[str, int] = {}

    def draft(self, brief, plan, research, channel):
        self.draft_calls[channel] = self.draft_calls.get(channel, 0) + 1
        if self.draft_calls[channel] <= self.failures:
            raise APICallError("요청 한도를 넘었어요 (429).", kind="rate_limit", status_code=429, retryable=self.retryable)
        return super().draft(brief, plan, research, channel)


def test_non_retryable_error_is_not_retried(settings, brief):
    backend = FlakyDraftBackend(settings, failures=1, retryable=False)
    bus = EventBus("no-retry", clock=SimClock(0))
    with pytest.raises(APICallError):  # every channel's draft failed once, for good
        run_pipeline(brief, backend, bus, settings)
    assert set(backend.draft_calls.values()) == {1}
    assert not any(e["type"] == "log" and "다시 시도" in e["data"]["message"] for e in bus.events)


def test_retries_exhausted_fail_the_step(settings, brief):
    backend = FlakyDraftBackend(settings, failures=5)
    bus = EventBus("exhausted", clock=SimClock(0))
    with pytest.raises(APICallError):
        run_pipeline(brief, backend, bus, settings)
    assert set(backend.draft_calls.values()) == {3}  # first try + 2 retries
    assert bus.events[-1]["type"] == "run.failed"


def test_live_runner_retries_with_interruptible_wait(settings, brief, monkeypatch):
    from insia_agents.agents import common

    monkeypatch.setattr(common, "RETRY_DELAYS", (0.01, 0.02))

    class FlakyThreaded(FlakyDraftBackend):
        name = "live"

    backend = FlakyThreaded(settings, failures=1)
    bus = EventBus("threaded-retry", clock=RealClock())
    result = run_pipeline(brief, backend, bus, settings, runner=ThreadRunner(4))
    assert len(result.results) == 4 and set(backend.draft_calls.values()) == {2}
    retries = [e for e in bus.events if e["type"] == "log" and "다시 시도" in e["data"]["message"]]
    assert len(retries) == 4 and all(e["agent"] == "orchestrator" for e in retries)


def test_run_context_profile_without_workspace(settings, brief):
    profile = Profile(company_name="인시아", banned_words=["데모"])
    backend = MockBackend(settings)
    bus = EventBus("ctx", clock=SimClock(0))
    run_pipeline(brief, backend, bus, settings, context=RunContext(profile=profile, documents=[], today="2026-09-28"))
    assert backend.context.profile == profile
    assert backend.on_usage is None  # restored after the run
    checks = [c for e in bus.events if e["type"] == "review.completed" for c in e["data"]["format_checks"]]
    assert any(c["id"] == "banned_words" and not c["passed"] for c in checks)


def test_channel_completed_reports_the_final_round(settings, brief):
    result, bus = execute_run(brief, replace(settings, pass_score=99))
    for event in (e for e in bus.events if e["type"] == "channel.completed"):
        channel_result = next(r for r in result.results if r.channel == event["data"]["channel"])
        assert event["data"]["final_round"] == channel_result.final.round


def test_usage_reports_are_summed_without_workspace(settings, brief):
    from insia_agents.models import UsageRecord
    from insia_agents.pipeline import BudgetExceeded

    class Priced(MockBackend):
        def plan(self, brief):
            self.on_usage(UsageRecord(task="plan", model="claude-opus-5", input_tokens=10, cost_usd=2.0))
            return super().plan(brief)

    forwarded = []
    backend = Priced(settings)
    backend.on_usage = forwarded.append  # a caller's own callback keeps receiving records
    bus = EventBus("no-ws-budget", clock=SimClock(0))
    with pytest.raises(BudgetExceeded):
        run_pipeline(brief, backend, bus, replace(settings, max_cost_usd=1.0))
    # (the mock may add its own zero-cost synthetic records)
    assert [r.cost_usd for r in forwarded if r.cost_usd] == [2.0]
    assert forwarded and all(r.run_id == "no-ws-budget" for r in forwarded)
    assert backend.on_usage == forwarded.append
    assert bus.events[-1]["data"]["budget_exceeded"] is True and bus.events[-1]["data"]["completed_channels"] == []
