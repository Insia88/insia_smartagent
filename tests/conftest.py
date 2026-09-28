from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:  # allow running without `pip install -e .`
    sys.path.insert(0, str(ROOT / "src"))

from insia_agents.config import Settings  # noqa: E402
from insia_agents.models import ALL_CHANNELS, Brief  # noqa: E402
from insia_agents.prompt_loader import clear_cache  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """No real credentials, no ~/.config/anthropic, no INSIA_* leaking in."""
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "INSIA_MODE", "INSIA_MODEL", "INSIA_FALLBACKS",
                 "INSIA_OUT_DIR", "INSIA_PROMPTS_DIR", "INSIA_SAMPLE_DIR", "INSIA_WEB_DIR", "INSIA_TODAY",
                 "INSIA_EFFORT_ORCHESTRATOR", "INSIA_EFFORT_RESEARCHER", "INSIA_EFFORT_REVIEWER"):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def settings(tmp_path) -> Settings:
    base = Settings.from_env(env={}, mode="mock", speed=0.0, today="2026-09-28")
    return replace(base, out_dir=tmp_path / "outputs", sample_dir=None, web_dir=None)


@pytest.fixture
def brief() -> Brief:
    return Brief(
        topic="테스트 주제",
        goal="서비스 소개",
        audience="1인 창업자",
        channels=list(ALL_CHANNELS),
        keywords=["AI 마케팅 자동화", "1인 창업"],
        tone="친근한 전문가 톤",
    )


@pytest.fixture
def prompts_dir(tmp_path, monkeypatch) -> Path:
    """Dummy prompt files so tests never depend on the real prompt contents."""
    root = tmp_path / "prompts"
    for role in ("orchestrator", "researcher", "reviewer"):
        (root / "agents").mkdir(parents=True, exist_ok=True)
        (root / "agents" / f"{role}.md").write_text(f"# {role} 테스트 프롬프트\n역할: {role}", encoding="utf-8")
    for channel in ALL_CHANNELS:
        (root / "channels").mkdir(parents=True, exist_ok=True)
        (root / "channels" / f"{channel}.md").write_text(f"# {channel} 테스트 가이드", encoding="utf-8")
    monkeypatch.setenv("INSIA_PROMPTS_DIR", str(root))
    clear_cache()
    return root
