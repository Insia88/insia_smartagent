"""Load the Korean prompt files shipped in ``insia_agents/prompts``.

``prompts/agents/<role>.md`` are the system prompts for the API backend and
``prompts/channels/<channel>.md`` are the channel guides (shared with the
Claude Code skills). Set ``INSIA_PROMPTS_DIR`` to use another directory with
the same layout (handy for experiments and tests).
"""

from __future__ import annotations

import os
from functools import lru_cache
from importlib import resources
from pathlib import Path

AGENT_PROMPTS = ("orchestrator", "researcher", "reviewer")
CHANNEL_GUIDES = ("bizplan", "naver_blog", "linkedin", "instagram")


class PromptNotFoundError(FileNotFoundError):
    pass


def _override_dir() -> Path | None:
    value = os.environ.get("INSIA_PROMPTS_DIR", "").strip()
    return Path(value).expanduser() if value else None


@lru_cache(maxsize=64)
def _read(kind: str, name: str, override: str | None) -> str:
    if kind not in ("agents", "channels"):
        raise ValueError(f"unknown prompt kind {kind!r}")
    rel = f"prompts/{kind}/{name}.md"
    if override:
        path = Path(override) / kind / f"{name}.md"
        where = str(path)
        text = path.read_text(encoding="utf-8") if path.is_file() else None
    else:
        node = resources.files("insia_agents").joinpath("prompts", kind, f"{name}.md")
        where = f"insia_agents/{rel}"
        text = node.read_text(encoding="utf-8") if node.is_file() else None
    if text is None or not text.strip():
        raise PromptNotFoundError(
            f"프롬프트 파일을 찾을 수 없어요: {where}. "
            f"패키지를 다시 설치하거나(pip install -e .) INSIA_PROMPTS_DIR 경로를 확인해 주세요. "
            f"(missing or empty prompt file: {where})"
        )
    return text.strip()


def load_prompt(kind: str, name: str) -> str:
    override = _override_dir()
    return _read(kind, name, str(override) if override else None)


def agent_prompt(role: str) -> str:
    return load_prompt("agents", role)


def channel_guide(channel: str) -> str:
    return load_prompt("channels", channel)


def check_prompts() -> list[str]:
    """Return a list of Korean problems (empty when every prompt file loads)."""
    problems: list[str] = []
    for role in AGENT_PROMPTS:
        try:
            agent_prompt(role)
        except PromptNotFoundError as exc:
            problems.append(str(exc))
    for channel in CHANNEL_GUIDES:
        try:
            channel_guide(channel)
        except PromptNotFoundError as exc:
            problems.append(str(exc))
    return problems


def clear_cache() -> None:
    _read.cache_clear()
