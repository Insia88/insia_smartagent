from __future__ import annotations

import json
from dataclasses import replace

import pytest

from insia_agents.backends.base import BackendError
from insia_agents.backends.mock_backend import MockBackend
from insia_agents.channels import check_format
from insia_agents.events import AGENTS, EVENT_TYPES, EventBus, RealClock, SimClock
from insia_agents.models import ALL_CHANNELS
from insia_agents.pipeline import SimRunner, ThreadRunner, execute_run, run_pipeline


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
