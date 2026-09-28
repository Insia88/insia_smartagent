"""Shared context for the agent layer.

Agent steps are generators: they emit events on the bus, call the backend,
and ``yield`` the simulated duration (seconds) of the work they just did. The
pipeline's runner turns those yields into virtual time (mock) or ignores them
(live, where real time passes during the API call). One implementation, two
drivers, identical event streams.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Generator, TypeVar

from ..backends.base import Backend
from ..channels import CHANNELS
from ..config import Settings
from ..events import EventBus
from ..models import Brief

T = TypeVar("T")
Step = Generator[float, None, T]

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
