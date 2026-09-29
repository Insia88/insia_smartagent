"""Event stream: clocks and a thread-safe, replayable event bus.

Every event has the shape documented in ``docs/event-schema.md``::

    {"seq": 1, "t": 0.0, "ts": "2026-09-28T00:15:00Z", "run_id": "...",
     "type": "run.started", "agent": "system", "data": {...}}

``t`` is seconds since the run started: wall-clock in live mode, simulated
(virtual) time in mock mode, so a recorded mock trace replays with realistic
pacing even when it was produced with ``speed=0``.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from pydantic import BaseModel

TERMINAL_TYPES = frozenset({"run.completed", "run.failed"})
AGENTS = ("orchestrator", "researcher", "reviewer", "system")

EVENT_TYPES = frozenset({
    "run.started", "agent.status", "handoff", "plan.created", "research.query", "research.source",
    "research.finding", "research.completed", "draft.created", "review.started", "review.completed",
    "revision.requested", "channel.completed", "channel.store_skipped", "run.completed", "run.failed", "log",
})


# ---------------------------------------------------------------------------
# Clocks
# ---------------------------------------------------------------------------


class RealClock:
    """Wall-clock seconds since construction (live mode)."""

    simulated = False

    def __init__(self) -> None:
        self._start = time.monotonic()

    def now(self) -> float:
        return time.monotonic() - self._start

    def advance(self, seconds: float) -> None:  # real time passes by itself
        return None

    def advance_to(self, t: float) -> None:
        return None


class SimClock:
    """Virtual time for mock runs.

    ``speed`` is a playback multiplier: ``advance(d)`` moves virtual time
    forward by ``d`` seconds and sleeps ``d / speed`` real seconds (``1`` = real
    time, ``2`` = twice as fast, ``0`` = no sleeping at all). The recorded
    virtual ``t`` never depends on ``speed``.
    """

    simulated = True

    def __init__(self, speed: float = 1.0, sleep: Callable[[float], None] = time.sleep) -> None:
        self.speed = max(0.0, float(speed))
        self._now = 0.0
        self._sleep = sleep
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        self.advance_to(self.now() + max(0.0, float(seconds or 0.0)))

    def advance_to(self, t: float) -> None:
        with self._lock:
            delta = t - self._now
            if delta <= 0:
                return
            self._now = t
        if self.speed > 0:
            self._sleep(delta / self.speed)


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------


def to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _iso(dt: datetime) -> str:
    text = dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    return text.replace("+00:00", "Z")


class EventBus:
    """Thread-safe event log for one run.

    - ``emit`` assigns a 1-based monotonic ``seq`` and a ``t`` from the clock.
    - ``subscribe`` yields every past event first, then live ones, and stops
      after a terminal event (``run.completed`` / ``run.failed``) or ``close()``.
    - An optional JSONL sink receives every event as it is emitted.
    - ``start_seq`` / ``start_t`` (or ``continue_from``) continue the numbering
      of an earlier stream of the same run (resume): the first event gets
      ``seq = start_seq + 1`` and ``t >= start_t``; ``ts`` stays wall-clock
      based (``started_at`` + time since this bus started).
    """

    def __init__(self, run_id: str, clock: RealClock | SimClock | None = None, started_at: datetime | None = None, *,
                 start_seq: int = 0, start_t: float = 0.0) -> None:
        self.run_id = run_id
        self.clock = clock or RealClock()
        self.started_at = started_at or datetime.now(timezone.utc)
        self._events: list[dict[str, Any]] = []
        self._cond = threading.Condition()
        self._closed = False
        self._sink = None
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._seq_offset = max(0, int(start_seq))
        self._t_offset = max(0.0, float(start_t))

    # -- emitting ----------------------------------------------------------
    def emit(self, type: str, agent: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        if agent not in AGENTS:
            raise ValueError(f"unknown agent {agent!r}")
        payload = to_jsonable(data or {})
        with self._cond:
            if self._closed:
                raise RuntimeError("event bus is closed")
            t = round(self._t_offset + max(0.0, self.clock.now()), 3)
            if self._events and t < self._events[-1]["t"]:
                t = self._events[-1]["t"]  # keep t non-decreasing across threads
            event = {
                "seq": self._seq_offset + len(self._events) + 1,
                "t": t,
                "ts": _iso(self.started_at + timedelta(seconds=max(0.0, t - self._t_offset))),
                "run_id": self.run_id,
                "type": type,
                "agent": agent,
                "data": payload,
            }
            self._events.append(event)
            if self._sink is not None:
                self._sink.write(json.dumps(event, ensure_ascii=False) + "\n")
                self._sink.flush()
            for listener in list(self._listeners):
                try:
                    listener(event)
                except Exception:  # a broken printer must not break the run
                    pass
            if type in TERMINAL_TYPES:
                self._closed = True
            self._cond.notify_all()
        return event

    def log(self, message: str, level: str = "info", agent: str = "system") -> dict[str, Any]:
        return self.emit("log", agent, {"level": level, "message": message})

    # -- reading -----------------------------------------------------------
    @property
    def closed(self) -> bool:
        with self._cond:
            return self._closed

    @property
    def events(self) -> list[dict[str, Any]]:
        with self._cond:
            return list(self._events)

    def __len__(self) -> int:
        with self._cond:
            return len(self._events)

    def last_seq(self) -> int:
        with self._cond:
            return self._seq_offset + len(self._events)

    @property
    def first_seq(self) -> int:
        """``seq`` of the first event this bus emits (1 unless it continues an earlier stream)."""
        return self._seq_offset + 1

    def continue_from(self, seq: int, t: float = 0.0) -> None:
        """Continue an earlier stream of this run: next ``seq`` is ``seq + 1``, ``t`` starts at ``t``.

        Only allowed before the first event is emitted.
        """
        with self._cond:
            if self._events:
                raise RuntimeError("continue_from() must be called before the first event")
            self._seq_offset = max(0, int(seq))
            self._t_offset = max(0.0, float(t))

    def subscribe(self, after_seq: int = 0, heartbeat: float | None = None) -> Iterator[dict[str, Any] | None]:
        """Replay events with ``seq > after_seq``, then follow live events.

        With ``heartbeat`` set, yields ``None`` whenever no event arrived for
        that many seconds (the SSE handler turns it into a comment line).
        """
        index = max(0, int(after_seq) - self._seq_offset)
        while True:
            with self._cond:
                if index >= len(self._events) and not self._closed:
                    self._cond.wait(timeout=heartbeat)
                if index < len(self._events):
                    batch = self._events[index:]
                    index = len(self._events)
                elif self._closed:
                    return
                else:
                    batch = None
            if batch is None:
                yield None
                continue
            for event in batch:
                yield event
                if event["type"] in TERMINAL_TYPES:
                    return

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        self.detach_sink()

    # -- sinks & listeners -------------------------------------------------
    def add_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """Call ``fn(event)`` for every new event (under the bus lock; exceptions are ignored)."""
        with self._cond:
            self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        with self._cond:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def attach_sink(self, path: str | Path, *, append: bool = False) -> Path:
        """Write past events to ``path`` (JSONL) and append every new one.

        ``append=True`` keeps what the file already holds (a resumed run adds
        to the events of its first attempt).
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._cond:
            if self._sink is not None:
                self._sink.close()
            self._sink = path.open("a" if append else "w", encoding="utf-8")
            for event in self._events:
                self._sink.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._sink.flush()
        return path

    def detach_sink(self) -> None:
        with self._cond:
            if self._sink is not None:
                self._sink.close()
                self._sink = None

    def to_trace(self, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """Demo-trace document (``web/demo/demo-run.json`` format)."""
        return {"version": 1, "meta": to_jsonable(meta or {}), "events": self.events}


def load_trace(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("events"), list):
        raise ValueError(f"{path}: version 1 트레이스 파일이 아니에요")
    return data
