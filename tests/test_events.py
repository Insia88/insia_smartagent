from __future__ import annotations

import json
import threading

import pytest

from insia_agents.events import EventBus, RealClock, SimClock, load_trace
from insia_agents.models import Source


def test_emit_shape_and_serialization():
    bus = EventBus("run-1", clock=SimClock(0))
    source = Source(id="s1", title="t", url="https://kosis.kr", tier=1)
    event = bus.emit("research.source", "researcher", {"source": source})
    assert event["seq"] == 1 and event["t"] == 0.0 and event["run_id"] == "run-1"
    assert event["ts"].endswith("Z")
    assert event["data"]["source"]["url"] == "https://kosis.kr"
    json.dumps(event)
    with pytest.raises(ValueError):
        bus.emit("log", "nobody", {})


def test_sim_clock_virtual_time_and_sleep():
    slept = []
    clock = SimClock(speed=0.5, sleep=slept.append)
    bus = EventBus("r", clock=clock)
    clock.advance(10)
    clock.advance_to(4)  # never goes backwards
    event = bus.emit("log", "system", {"level": "info", "message": "x"})
    assert event["t"] == 10.0
    assert slept == [5.0]
    fast = SimClock(speed=0, sleep=lambda s: pytest.fail("speed 0 must not sleep"))
    fast.advance(100)
    assert fast.now() == 100


def test_concurrent_emits_are_ordered():
    bus = EventBus("r", clock=RealClock())

    def worker(n: int) -> None:
        for i in range(100):
            bus.emit("log", "system", {"level": "info", "message": f"{n}-{i}"})

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    events = bus.events
    assert [e["seq"] for e in events] == list(range(1, 401))
    assert all(a["t"] <= b["t"] for a, b in zip(events, events[1:]))


def test_subscribe_replays_then_follows_until_terminal():
    bus = EventBus("r", clock=SimClock(0))
    for i in range(3):
        bus.emit("log", "system", {"level": "info", "message": str(i)})
    received: list[dict] = []
    started = threading.Event()

    def reader() -> None:
        started.set()
        for event in bus.subscribe():
            if event is not None:
                received.append(event)

    thread = threading.Thread(target=reader)
    thread.start()
    started.wait()
    bus.emit("log", "system", {"level": "info", "message": "live"})
    bus.emit("run.completed", "system", {"duration_s": 1, "scores": {}, "passed": {}, "output_dir": None})
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert [e["seq"] for e in received] == [1, 2, 3, 4, 5]
    assert bus.closed
    with pytest.raises(RuntimeError):
        bus.emit("log", "system", {})
    # a late subscriber still gets the whole history, then stops
    assert [e["seq"] for e in bus.subscribe(after_seq=3)] == [4, 5]


def test_subscribe_heartbeat_yields_none():
    bus = EventBus("r", clock=SimClock(0))
    stream = bus.subscribe(heartbeat=0.01)
    assert next(stream) is None
    bus.close()
    assert list(stream) == []


def test_jsonl_sink_and_trace(tmp_path):
    bus = EventBus("r", clock=SimClock(0))
    bus.emit("log", "system", {"level": "info", "message": "before sink"})
    path = bus.attach_sink(tmp_path / "run" / "events.jsonl")
    bus.emit("run.failed", "system", {"error": "x"})
    bus.detach_sink()
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [e["seq"] for e in lines] == [1, 2]
    trace = bus.to_trace({"title": "t", "mode": "mock"})
    assert trace["version"] == 1 and trace["meta"]["title"] == "t" and len(trace["events"]) == 2
    (tmp_path / "trace.json").write_text(json.dumps(trace), encoding="utf-8")
    assert load_trace(tmp_path / "trace.json")["events"][1]["type"] == "run.failed"
