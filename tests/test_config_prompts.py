from __future__ import annotations

import pytest

from insia_agents.config import Settings, has_credentials, load_sample_brief, resolve_mode
from insia_agents.prompt_loader import PromptNotFoundError, agent_prompt, channel_guide, check_prompts, clear_cache


def test_resolve_mode(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    assert resolve_mode("auto", env={}, home=home)[0] == "mock"
    assert resolve_mode("auto", env={"ANTHROPIC_API_KEY": "x"}, home=home)[0] == "live"
    assert resolve_mode("auto", env={"ANTHROPIC_AUTH_TOKEN": "x"}, home=home)[0] == "live"
    (home / ".config" / "anthropic").mkdir(parents=True)
    assert has_credentials(env={}, home=home)
    assert resolve_mode("auto", env={}, home=home)[0] == "live"
    assert resolve_mode("mock", env={"ANTHROPIC_API_KEY": "x"}, home=home)[0] == "mock"
    assert resolve_mode("live", env={}, home=home)[0] == "live"


def test_settings_from_env():
    settings = Settings.from_env(env={"INSIA_MODEL": "claude-sonnet-5", "INSIA_EFFORT_RESEARCHER": "low",
                                      "INSIA_FALLBACKS": "0", "INSIA_MODE": "mock"})
    assert settings.model == "claude-sonnet-5"
    assert settings.effort == {"orchestrator": "high", "researcher": "low", "reviewer": "high"}
    assert settings.fallbacks is False and settings.mode == "mock"
    defaults = Settings.from_env(env={})
    assert defaults.model == "claude-opus-5" and defaults.fallbacks is True
    assert defaults.max_rounds == 2 and defaults.pass_score == 80
    with pytest.raises(ValueError):
        Settings.from_env(env={"INSIA_EFFORT_REVIEWER": "extreme"})
    with pytest.raises(ValueError):
        Settings.from_env(env={}, max_rounds=9)


def test_prompts_load_from_override(prompts_dir):
    assert "orchestrator" in agent_prompt("orchestrator")
    assert "linkedin" in channel_guide("linkedin")
    assert check_prompts() == []


def test_missing_prompt_has_clear_error(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    (empty / "agents").mkdir(parents=True)
    monkeypatch.setenv("INSIA_PROMPTS_DIR", str(empty))
    clear_cache()
    with pytest.raises(PromptNotFoundError) as info:
        agent_prompt("reviewer")
    assert "reviewer.md" in str(info.value) and "INSIA_PROMPTS_DIR" in str(info.value)
    assert len(check_prompts()) == 7


def test_sample_brief_fallback(settings):
    brief = load_sample_brief(settings)  # sample_dir=None → built-in copy
    assert "INSIA" in brief.topic and brief.channels == ["bizplan", "naver_blog", "linkedin", "instagram"]
