"""Shared context for the agent layer.

Agent steps are generators: they emit events on the bus, call the backend,
and ``yield`` the simulated duration (seconds) of the work they just did. The
pipeline's runner turns those yields into virtual time (mock) or ignores them
(live, where real time passes during the API call). One implementation, two
drivers, identical event streams.

Every backend call goes through ``AgentContext.call``: it first runs the
pipeline's ``checkpoint`` (cancel flag, budget cap — no new paid call once the
cap is exceeded) and retries a ``BackendError`` marked ``retryable`` (429, 5xx,
timeouts) after ``RETRY_DELAYS`` seconds before giving up on the step.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Generator, TypeVar

from ..backends.base import Backend, BackendError, RunContext
from ..channels import CHANNELS
from ..config import Settings
from ..events import EventBus
from ..models import Brief, Profile

T = TypeVar("T")
Step = Generator[float, None, T]

# Waits before each pipeline-level retry of a retryable backend error (the SDK
# has already retried the HTTP request itself by then).
RETRY_DELAYS: tuple[float, ...] = (2.0, 6.0)

STATUS_LABELS = {
    "idle": "대기", "planning": "기획 중", "searching": "검색 중", "reading": "자료 읽는 중", "writing": "작성 중",
    "reviewing": "검수 중", "revising": "수정 중", "waiting": "대기", "done": "완료", "error": "오류",
}


def channel_label(channel: str) -> str:
    spec = CHANNELS.get(channel)  # type: ignore[call-overload]
    return spec.label if spec else channel


def excerpt(text: str, limit: int = 160) -> str:
    """Plain-text preview: drop markdown markers and image slots, collapse space."""
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s*#{1,6}\s*", "", line)
        line = re.sub(r"\[이미지[^\]]*\]", "", line)
        line = re.sub(r"^\s*[-*•]\s+", "", line)
        line = line.replace("**", "").replace("|", " ").strip()
        if line and not re.fullmatch(r"[-: ]+", line):
            lines.append(line)
    flat = re.sub(r"\s+", " ", " ".join(lines)).strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


@dataclass
class AgentContext:
    bus: EventBus
    backend: Backend
    settings: Settings
    brief: Brief
    simulated: bool
    context: RunContext = field(default_factory=RunContext)
    # Called before every backend call; raises to stop (RunCancelled, BudgetExceeded).
    checkpoint: Callable[[], None] | None = None
    # Waits between retries; the pipeline passes an interruptible wait (live) or the virtual clock (mock).
    wait: Callable[[float], None] | None = None

    @property
    def profile(self) -> Profile | None:
        """The company profile for this run (``None`` when not used)."""
        return self.context.profile if self.context is not None else None

    def call(self, agent: str, what: str, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Call the backend with the checkpoint and pipeline-level retries."""
        attempt = 0
        while True:
            if self.checkpoint is not None:
                self.checkpoint()
            try:
                return fn(*args, **kwargs)
            except BackendError as exc:
                delays = RETRY_DELAYS
                if not getattr(exc, "retryable", False) or attempt >= len(delays):
                    raise
                delay = float(delays[attempt])
                attempt += 1
                self.log(f"{what}: {exc} — {delay:g}초 뒤 자동으로 다시 시도해요 ({attempt}/{len(delays)})", "warn", agent)
                self._pause(delay)

    def _pause(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self.wait is not None:
            self.wait(seconds)
        elif self.simulated:
            self.bus.clock.advance(seconds)
        else:
            time.sleep(seconds)

    def pace(self, kind: str, channel: str | None = None, round: int = 0) -> float:
        if not self.simulated:
            return 0.0
        fn = getattr(self.backend, "sim_seconds", None)
        if fn is None:
            from ..backends.mock_backend import sim_seconds as fn  # default table
        return float(fn(kind, channel, round))

    def status(self, agent: str, status: str, message: str) -> None:
        self.bus.emit("agent.status", agent, {"status": status, "message": message})

    def handoff(self, sender: str, to: str, kind: str, label: str, channel: str | None = None) -> None:
        data: dict[str, Any] = {"from": sender, "to": to, "kind": kind, "label": label}
        if channel:
            data["channel"] = channel
        self.bus.emit("handoff", sender, data)

    def log(self, message: str, level: str = "info", agent: str = "system") -> None:
        self.bus.emit("log", agent, {"level": level, "message": message})
