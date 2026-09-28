from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import anthropic
import pytest

from fakes import RefusedMessage, message, text
from insia_agents import costs
from insia_agents.costs import (
    PRICES,
    WEB_SEARCH_USD_PER_1K,
    cost_of,
    env_key,
    estimate_tokens,
    load_prices,
    mock_usage,
    price_for,
    usage_from_response,
)
from insia_agents.models import UsageRecord


@pytest.fixture(autouse=True)
def no_price_overrides(monkeypatch, tmp_path):
    """No INSIA_PRICE_* from the environment, no prices.json from the CWD."""
    import os

    for name in list(os.environ):
        if name.startswith("INSIA_PRICE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("INSIA_HOME", str(tmp_path / "workspace"))
    costs._warned.clear()


def _record(model="claude-opus-5", **tokens) -> UsageRecord:
    return UsageRecord(model=model, **tokens)


def test_default_price_table_matches_the_documented_list_prices():
    assert PRICES["claude-opus-5"] == {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25}
    assert PRICES["claude-sonnet-5"] == {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5}
    assert PRICES["claude-haiku-4-5"] == {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25}
    assert PRICES["claude-fable-5-1"]["input"] == 10.0 and PRICES["claude-fable-5-1"]["output"] == 50.0
    assert WEB_SEARCH_USD_PER_1K == 10.0


def test_cost_math_all_token_kinds_and_searches():
    record = _record(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=2_000_000,
                     cache_write_tokens=400_000, web_search_requests=7)
    # 5.00 + 2.50 + 1.00 + 2.50 + 0.07
    assert cost_of(record) == pytest.approx(11.07)
    small = _record(model="claude-sonnet-5", input_tokens=1234, output_tokens=567)
    assert cost_of(small) == pytest.approx((1234 * 2.0 + 567 * 10.0) / 1e6)


def test_dated_snapshot_ids_use_the_base_price_but_other_suffixes_do_not():
    assert price_for("claude-opus-5-20260115") == PRICES["claude-opus-5"]
    assert price_for("claude-opus-5-5") == PRICES["claude-opus-5-5"]  # a different model, not a snapshot
    assert price_for("claude-opus-5-7") is None


def test_unknown_model_costs_zero_and_warns_once(caplog):
    caplog.set_level(logging.WARNING, logger="insia_agents.costs")
    record = _record(model="claude-unknown-9", input_tokens=10_000, output_tokens=10_000)
    assert price_for("claude-unknown-9") is None
    assert cost_of(record) == 0.0
    # the tokens stay unpriced, but the web-search fee does not depend on the model: 3 searches = $0.03
    assert cost_of(record.model_copy(update={"web_search_requests": 3})) == pytest.approx(0.03)
    warnings = [r for r in caplog.records if "claude-unknown-9" in r.getMessage()]
    assert len(warnings) == 1 and "INSIA_PRICE_CLAUDE_UNKNOWN_9" in warnings[0].getMessage()


def test_mock_records_are_free_without_warning(caplog):
    caplog.set_level(logging.WARNING, logger="insia_agents.costs")
    assert cost_of(_record(model="mock", input_tokens=99_999, output_tokens=99_999, web_search_requests=5)) == 0.0
    assert price_for("mock") == {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}
    assert not caplog.records


def test_env_overrides_existing_and_new_models():
    env = {
        "INSIA_PRICE_CLAUDE_OPUS_5_INPUT": "4",
        "INSIA_PRICE_CLAUDE_OPUS_5_CACHE_READ": "0.4",
        "INSIA_PRICE_CLAUDE_NEW_1_INPUT": "3",
        "INSIA_PRICE_CLAUDE_NEW_1_OUTPUT": "15",
        "INSIA_PRICE_CLAUDE_HALF_1_INPUT": "3",  # no OUTPUT: incomplete new model, ignored
        "INSIA_PRICE_CLAUDE_SONNET_5_OUTPUT": "-1",  # invalid: ignored
        "INSIA_PRICE_WEB_SEARCH_PER_1K": "12.5",
    }
    table, web = load_prices(env=env, home="/nonexistent")
    assert table["claude-opus-5"] == {"input": 4.0, "output": 25.0, "cache_read": 0.4, "cache_write": 6.25}
    assert table["claude-new-1"] == {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}
    assert "claude-half-1" not in table
    assert table["claude-sonnet-5"]["output"] == 10.0
    assert web == 12.5
    assert env_key("claude-opus-5") == "CLAUDE_OPUS_5"
    record = _record(model="claude-new-1", input_tokens=1_000_000, web_search_requests=2)
    assert cost_of(record, env=env, home="/nonexistent") == pytest.approx(3.0 + 0.025)


def test_prices_json_in_the_workspace(tmp_path):
    home = tmp_path / "ws"
    home.mkdir()
    (home / "prices.json").write_text(json.dumps({
        "models": {"claude-opus-5": {"output": 20}, "claude-custom": {"input": 1, "output": 2}},
        "web_search_usd_per_1k": 8,
    }), encoding="utf-8")
    table, web = load_prices(env={}, home=home)
    assert table["claude-opus-5"]["output"] == 20.0 and table["claude-opus-5"]["input"] == 5.0
    assert table["claude-custom"]["cache_write"] == 1.25
    assert web == 8.0
    # a flat file (models at the top level) works too, and env beats the file
    (home / "prices.json").write_text(json.dumps({"claude-opus-5": {"input": 6}}), encoding="utf-8")
    table, _ = load_prices(env={"INSIA_PRICE_CLAUDE_OPUS_5_INPUT": "7"}, home=home)
    assert table["claude-opus-5"]["input"] == 7.0


def test_broken_prices_json_falls_back_to_defaults(tmp_path, caplog):
    home = tmp_path / "ws"
    home.mkdir()
    (home / "prices.json").write_text("{not json", encoding="utf-8")
    caplog.set_level(logging.WARNING, logger="insia_agents.costs")
    table, web = load_prices(env={}, home=home)
    assert table["claude-opus-5"] == PRICES["claude-opus-5"] and web == WEB_SEARCH_USD_PER_1K
    assert any("prices.json" in r.getMessage() for r in caplog.records)


def test_usage_from_sdk_message_reads_every_field():
    msg = anthropic.types.Message.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5", "content": [],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1200, "output_tokens": 3400, "cache_read_input_tokens": 50_000,
                  "cache_creation_input_tokens": 6000,
                  "server_tool_use": {"web_search_requests": 4, "web_fetch_requests": 2}},
    })
    record = usage_from_response(msg, agent="researcher", task="research", model="claude-opus-5", run_id="r1")
    assert (record.input_tokens, record.output_tokens, record.cache_read_tokens, record.cache_write_tokens,
            record.web_search_requests) == (1200, 3400, 50_000, 6000, 4)
    assert (record.agent, record.task, record.run_id, record.model) == ("researcher", "research", "r1", "claude-opus-5")
    expected = (1200 * 5 + 3400 * 25 + 50_000 * 0.5 + 6000 * 6.25) / 1e6 + 4 * 0.01
    assert record.cost_usd == pytest.approx(expected)
    assert record.created_at.endswith("Z")


def test_usage_tolerates_missing_fields_dicts_and_refusals():
    minimal = usage_from_response(message([text("x")]), agent="orchestrator", task="plan", model="claude-opus-5")
    assert (minimal.input_tokens, minimal.output_tokens, minimal.cache_read_tokens, minimal.web_search_requests) == (10, 10, 0, 0)
    no_usage = usage_from_response(SimpleNamespace(), agent="a", task="t", model="claude-sonnet-5")
    assert no_usage.model == "claude-sonnet-5" and no_usage.cost_usd == 0.0 and no_usage.input_tokens == 0
    as_dict = usage_from_response({"model": "claude-haiku-4-5", "usage": {"input_tokens": 1_000_000, "server_tool_use": None}},
                                  agent="a", task="t", model="claude-opus-5")
    assert as_dict.model == "claude-haiku-4-5" and as_dict.cost_usd == pytest.approx(1.0)
    refused = usage_from_response(RefusedMessage(), agent="orchestrator", task="plan", model="claude-opus-5")  # content never read
    assert refused.input_tokens == 0 and refused.model == "claude-opus-5"
    weird = usage_from_response({"usage": {"input_tokens": "12", "output_tokens": None, "cache_read_input_tokens": -5}},
                                agent="a", task="t", model="claude-opus-5")
    assert (weird.input_tokens, weird.output_tokens, weird.cache_read_tokens) == (12, 0, 0)


def test_served_model_is_priced_after_a_fallback():
    served = SimpleNamespace(model="claude-sonnet-5", usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=0))
    record = usage_from_response(served, agent="a", task="t", model="claude-opus-5")
    assert record.model == "claude-sonnet-5" and record.cost_usd == pytest.approx(2.0)


def test_mock_usage_is_free_and_estimated():
    record = mock_usage(agent="orchestrator", task="draft", prompt="가" * 1000, output="나" * 300)
    assert record.model == "mock" and record.cost_usd == 0.0
    assert record.input_tokens == 500 and record.output_tokens == 150
    assert estimate_tokens("") == 0 and estimate_tokens("a") == 1
    assert cost_of(record) == 0.0


def test_usage_meter_prices_with_its_workspace_prices_json(tmp_path):
    """Unpriced usage is priced with the run's workspace prices.json, not INSIA_HOME."""
    from insia_agents.db import Workspace
    from insia_agents.pipeline import UsageMeter, record_cost

    home = tmp_path / "custom-home"  # INSIA_HOME (autouse fixture) points elsewhere
    (home).mkdir()
    (home / "prices.json").write_text(json.dumps({"models": {"claude-custom": {"input": 2, "output": 4}}}),
                                      encoding="utf-8")
    record = _record("claude-custom", input_tokens=1_000_000, output_tokens=500_000)
    assert record_cost(record) == 0.0  # unknown model without the workspace file
    assert record_cost(record, home=home) == pytest.approx(4.0)
    ws = Workspace(home)
    try:
        meter = UsageMeter("r1", workspace=ws)
        meter(record)
        assert meter.spent == pytest.approx(4.0)
    finally:
        ws.close()


# ---------------------------------------------------------------------------
# Server-side refusal fallback: usage.iterations is the billing source of truth
# ---------------------------------------------------------------------------


def _iteration(kind: str, model: str | None, input_tokens: int, output_tokens: int, **extra) -> SimpleNamespace:
    return SimpleNamespace(type=kind, model=model, input_tokens=input_tokens, output_tokens=output_tokens,
                           cache_read_input_tokens=extra.get("cache_read", 0),
                           cache_creation_input_tokens=extra.get("cache_write", 0))


def _fallback_response(fallback_model: str = "claude-opus-4-8", searches: int = 0) -> SimpleNamespace:
    """Opus 5 declined mid-stream (20k in / 3k out), the fallback model answered (20k in / 5k out).
    Top-level usage covers only the serving attempt, as the API reports it."""
    return SimpleNamespace(model=fallback_model, usage=SimpleNamespace(
        input_tokens=20_000, output_tokens=5_000, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        server_tool_use=SimpleNamespace(web_search_requests=searches),
        iterations=[_iteration("message", "claude-opus-5", 20_000, 3_000),
                    _iteration("fallback_message", fallback_model, 20_000, 5_000)]))


def test_default_fallback_target_opus_4_8_has_its_documented_price():
    # Opus 5 is "a drop-in upgrade at Opus 4.8's pricing ($5/$25 per MTok)"; cache 0.1x read, 1.25x 5-minute write
    assert PRICES["claude-opus-4-8"] == {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25}


def test_refusal_fallback_bills_every_attempt_at_its_own_model_price():
    record = usage_from_response(_fallback_response(searches=2), agent="orchestrator", task="plan", model="claude-opus-5")
    declined = (20_000 * 5 + 3_000 * 25) / 1e6   # the declined attempt bills at normal rates
    served = (20_000 * 5 + 5_000 * 25) / 1e6     # the fallback attempt at the fallback model's rates
    assert record.cost_usd == pytest.approx(declined + served + 2 * 0.01)  # the search fee once
    assert record.model == "claude-opus-4-8"  # the model that produced the message
    assert (record.input_tokens, record.output_tokens) == (40_000, 8_000)
    assert costs.response_models(_fallback_response(), "claude-opus-5") == ["claude-opus-4-8", "claude-opus-5"]


def test_attempts_are_priced_per_model_and_entries_without_a_model_use_the_request_model():
    msg = SimpleNamespace(model="claude-sonnet-5", usage=SimpleNamespace(
        input_tokens=1_000_000, output_tokens=0, iterations=[
            _iteration("message", None, 1_000_000, 0),                        # no model: the requested one (opus 5)
            _iteration("compaction", None, 0, 0),                             # nothing billed: skipped
            _iteration("fallback_message", "claude-sonnet-5", 1_000_000, 0, cache_read=1_000_000)]))
    record = usage_from_response(msg, agent="a", task="t", model="claude-opus-5")
    assert record.cost_usd == pytest.approx(5.0 + 2.0 + 0.2)
    assert record.cache_read_tokens == 1_000_000


def test_an_incomplete_iteration_breakdown_never_bills_less_than_top_level():
    msg = SimpleNamespace(model="claude-opus-5", usage=SimpleNamespace(
        input_tokens=100_000, output_tokens=10_000, iterations=[_iteration("message", "claude-opus-5", 1_000, 100)]))
    record = usage_from_response(msg, agent="a", task="t", model="claude-opus-5")
    assert record.cost_usd == pytest.approx((100_000 * 5 + 10_000 * 25) / 1e6)
    assert (record.input_tokens, record.output_tokens) == (100_000, 10_000)


def test_an_unpriced_fallback_model_is_flagged_and_the_known_attempt_still_counts(caplog):
    caplog.set_level(logging.WARNING, logger="insia_agents.costs")
    record = usage_from_response(_fallback_response("claude-future-9", searches=1), agent="a", task="t",
                                 model="claude-opus-5")
    assert record.cost_usd == pytest.approx((20_000 * 5 + 3_000 * 25) / 1e6 + 0.01)  # not silently 0
    assert price_for("claude-future-9") is None
    assert any("claude-future-9" in r.getMessage() and "INSIA_PRICE_CLAUDE_FUTURE_9" in r.getMessage()
               for r in caplog.records)
    assert "claude-future-9" in costs.response_models(_fallback_response("claude-future-9"), "claude-opus-5")


def test_fallback_billing_feeds_the_budget_meter():
    """UsageMeter keeps the backend's positive cost, so the cap sees the declined attempt too."""
    from insia_agents.pipeline import UsageMeter

    meter = UsageMeter("r1", cap=0.3)
    meter(usage_from_response(_fallback_response(), agent="a", task="t", model="claude-opus-5"))
    assert meter.spent == pytest.approx(0.4) and meter.exceeded()
