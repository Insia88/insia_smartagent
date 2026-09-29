"""Token usage → USD cost.

Every Claude API response carries a ``usage`` object; the live backend turns
each one into a ``UsageRecord`` (``usage_from_response``) and hands it to
``backend.on_usage``. ``cost_of`` prices a record with the table below.

Prices change. The defaults are list prices in USD per million tokens
(checked 2026-09-28 against the Claude API model docs) and can be overridden
without a code change, later sources winning:

1. ``PRICES`` / ``WEB_SEARCH_USD_PER_1K`` in this module,
2. ``<workspace>/prices.json`` (workspace = ``INSIA_HOME``, default
   ``./workspace``)::

       {"claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
        "web_search_usd_per_1k": 10.0}

   (models may also be nested under a ``"models"`` key),
3. environment variables ``INSIA_PRICE_<MODEL>_<FIELD>`` where ``<MODEL>`` is
   the model id upper-cased with every non-alphanumeric character replaced by
   ``_`` and ``<FIELD>`` is ``INPUT``, ``OUTPUT``, ``CACHE_READ`` or
   ``CACHE_WRITE`` (e.g. ``INSIA_PRICE_CLAUDE_OPUS_5_INPUT=5``), plus
   ``INSIA_PRICE_WEB_SEARCH_PER_1K``. A model that is not in the table needs at
   least ``INPUT`` and ``OUTPUT``; missing cache prices are derived from the
   input price (read 0.1×, 5-minute write 1.25×).

Tokens of a model without a price cost 0 and the model is logged once (a
warning, never an error; the live backend also shows a notice): the run keeps
going, but the budget cap cannot see that spend, so set a price for it. Web
searches are still counted (their fee does not depend on the model).
``cache_write`` is the 5-minute-TTL rate (the only TTL this package uses).
Mock-mode records (model ``mock``) are always free.

Server-side refusal fallback: a response can be produced by several attempts
(the requested model declines, a fallback model answers). ``usage.iterations``
is then the per-attempt source of truth and top-level ``usage`` covers only
the attempt that produced the message, so ``usage_from_response`` bills every
iteration at its own model's rates (a declined attempt included).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .models import UsageRecord

log = logging.getLogger(__name__)

PRICE_FIELDS: tuple[str, ...] = ("input", "output", "cache_read", "cache_write")

# USD per million tokens (MTok).
PRICES: dict[str, dict[str, float]] = {
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    # Default server-side fallback target (cyber-category refusals of Opus 5 /
    # Fable 5.1 go to Opus 4.8); Opus 5 is priced the same as Opus 4.8.
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 5.0},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25},
    # Fable 5.1 bills cache reads at 0.025x input ($0.25/MTok); Fable 5 at 0.1x.
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": 12.5},
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_read": 1.0, "cache_write": 12.5},
}
WEB_SEARCH_USD_PER_1K = 10.0  # server-side web search, per 1,000 searches (web fetch is not metered)

MOCK_MODEL = "mock"
PRICES_FILE = "prices.json"
ENV_PREFIX = "INSIA_PRICE_"
WEB_SEARCH_ENV = "INSIA_PRICE_WEB_SEARCH_PER_1K"

_DATED_SUFFIX = re.compile(r"^-\d{8}$")
_warned: set[str] = set()


# ---------------------------------------------------------------------------
# Price table
# ---------------------------------------------------------------------------


def env_key(model: str) -> str:
    """``claude-opus-5`` → ``CLAUDE_OPUS_5`` (the ``<MODEL>`` part of the env name)."""
    return re.sub(r"[^A-Za-z0-9]", "_", model.strip()).upper()


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _complete(entry: Mapping[str, float]) -> dict[str, float] | None:
    """Fill derivable cache prices; ``None`` when input/output are missing."""
    if "input" not in entry or "output" not in entry:
        return None
    full = dict(entry)
    full.setdefault("cache_read", round(full["input"] * 0.1, 6))
    full.setdefault("cache_write", round(full["input"] * 1.25, 6))
    return {field: full[field] for field in PRICE_FIELDS}


def _merge(table: dict[str, dict[str, float]], model: str, fields: Mapping[str, float], origin: str) -> None:
    if model in table:
        table[model] = {**table[model], **fields}
        return
    completed = _complete(fields)
    if completed is None:
        log.warning("%s: 새 모델 %s에는 input과 output 가격이 모두 필요해요. 무시할게요.", origin, model)
        return
    table[model] = completed


def _read_prices_file(path: Path) -> tuple[dict[str, dict[str, float]], float | None]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as exc:
        log.warning("가격 파일 %s을(를) 읽지 못해 기본 가격을 써요: %s", path, exc)
        return {}, None
    if not isinstance(raw, dict):
        log.warning("가격 파일 %s은(는) JSON 객체여야 해요. 기본 가격을 써요.", path)
        return {}, None
    web = _number(raw.get("web_search_usd_per_1k"))
    models = raw.get("models") if isinstance(raw.get("models"), dict) else raw
    table: dict[str, dict[str, float]] = {}
    for model, entry in models.items():
        if not isinstance(entry, dict):
            continue
        fields = {f: n for f in PRICE_FIELDS if (n := _number(entry.get(f))) is not None}
        if fields:
            table[str(model)] = fields
    return table, web


def load_prices(env: Mapping[str, str] | None = None, home: str | Path | None = None) -> tuple[dict[str, dict[str, float]], float]:
    """``(model → prices, web search USD per 1K)`` after file and env overrides."""
    env = os.environ if env is None else env
    table = {model: dict(prices) for model, prices in PRICES.items()}
    web = WEB_SEARCH_USD_PER_1K

    if home is None:
        home = (env.get("INSIA_HOME") or "").strip() or "workspace"
    path = Path(home).expanduser() / PRICES_FILE
    file_models, file_web = _read_prices_file(path)
    for model, fields in file_models.items():
        _merge(table, model, fields, str(path))
    if file_web is not None:
        web = file_web

    by_key = {env_key(model): model for model in table}
    pending: dict[str, dict[str, float]] = {}
    for name, value in env.items():
        if not name.startswith(ENV_PREFIX):
            continue
        if name == WEB_SEARCH_ENV:
            number = _number(value)
            if number is None:
                log.warning("%s=%r: 0 이상의 숫자여야 해요. 무시할게요.", name, value)
            else:
                web = number
            continue
        rest = name[len(ENV_PREFIX):]
        field = next((f for f in ("CACHE_WRITE", "CACHE_READ", "OUTPUT", "INPUT") if rest.endswith("_" + f)), None)
        if field is None:
            continue
        number = _number(value)
        if number is None:
            log.warning("%s=%r: 0 이상의 숫자여야 해요. 무시할게요.", name, value)
            continue
        key = rest[: -len(field) - 1]
        model = by_key.get(key) or key.lower().replace("_", "-")
        pending.setdefault(model, {})[field.lower()] = number
    for model, fields in pending.items():
        _merge(table, model, fields, "환경 변수")
    return table, web


def _lookup(table: Mapping[str, Mapping[str, float]], model: str) -> Mapping[str, float] | None:
    model = model.strip()
    if model in table:
        return table[model]
    # Dated snapshot ids (``claude-opus-5-20260115``) share the base model's price.
    for known in sorted(table, key=len, reverse=True):
        if model.startswith(known) and _DATED_SUFFIX.match(model[len(known):]):
            return table[known]
    return None


def price_for(model: str, *, prices: Mapping[str, Mapping[str, float]] | None = None,
              env: Mapping[str, str] | None = None, home: str | Path | None = None) -> dict[str, float] | None:
    """Per-MTok prices for ``model`` or ``None`` when unknown (mock: all zero)."""
    if is_mock_model(model):
        return {field: 0.0 for field in PRICE_FIELDS}
    table = prices if prices is not None else load_prices(env, home)[0]
    found = _lookup(table, model)
    return dict(found) if found is not None else None


def is_mock_model(model: str) -> bool:
    return model.strip().lower().startswith(MOCK_MODEL)


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def _warn_unpriced(model: str) -> None:
    if model not in _warned:
        _warned.add(model)
        log.warning("모델 %r의 가격을 몰라 이 모델의 토큰 비용을 0으로 기록해요. prices.json이나 %s%s_INPUT/_OUTPUT으로 가격을 넣어 주세요.",
                    model, ENV_PREFIX, env_key(model or "MODEL"))


def _token_cost(price: Mapping[str, float], input_tokens: int, output_tokens: int, cache_read: int,
                cache_write: int) -> float:
    return (input_tokens * price.get("input", 0.0)
            + output_tokens * price.get("output", 0.0)
            + cache_read * price.get("cache_read", 0.0)
            + cache_write * price.get("cache_write", 0.0)) / 1_000_000


def cost_of(record: UsageRecord, *, prices: Mapping[str, Mapping[str, float]] | None = None,
            web_search_per_1k: float | None = None, env: Mapping[str, str] | None = None,
            home: str | Path | None = None) -> float:
    """USD cost of one usage record (6 decimals). Unknown model → its tokens
    cost 0 (+ one warning); web searches are still counted."""
    if is_mock_model(record.model):
        return 0.0
    if prices is None or web_search_per_1k is None:
        table, web = load_prices(env, home)
        prices = table if prices is None else prices
        web_search_per_1k = web if web_search_per_1k is None else web_search_per_1k
    searches = record.web_search_requests * web_search_per_1k / 1000
    price = _lookup(prices, record.model)
    if price is None:
        _warn_unpriced(record.model)
        return round(searches, 6)
    tokens = _token_cost(price, record.input_tokens, record.output_tokens, record.cache_read_tokens,
                         record.cache_write_tokens)
    return round(tokens + searches, 6)


# ---------------------------------------------------------------------------
# Response → record
# ---------------------------------------------------------------------------


def _field(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _count(obj: Any, name: str) -> int:
    value = _field(obj, name)
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _tokens_of(obj: Any) -> tuple[int, int, int, int]:
    """``(input, output, cache_read, cache_write)`` of a usage object or an iteration entry."""
    return tuple(_count(obj, name) for name in TOKEN_FIELDS)  # type: ignore[return-value]


def _model_name(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def served_model(response: Any, model: str) -> str:
    """The model that produced the returned message (``response.model``), else ``model``."""
    return _model_name(_field(response, "model")) or model


def usage_parts(response: Any, model: str) -> list[tuple[str, tuple[int, int, int, int]]]:
    """The billed attempts of one response: ``[(model, (input, output, cache_read, cache_write))]``.

    ``usage.iterations`` (server-side refusal fallback, server-tool loops,
    compaction) lists every sampling attempt with its own model and tokens and
    is the billing source of truth: top-level ``usage`` covers only the attempt
    that produced the message (a declined attempt bills at its model's rates,
    the fallback attempt at the fallback model's). An entry without a model is
    billed at the requested ``model``. Without iterations there is one part:
    top-level ``usage`` at the served model.
    """
    usage = _field(response, "usage")
    iterations = _field(usage, "iterations")
    parts: list[tuple[str, tuple[int, int, int, int]]] = []
    if isinstance(iterations, (list, tuple)):
        for entry in iterations:
            tokens = _tokens_of(entry)
            if any(tokens):
                parts.append((_model_name(_field(entry, "model")) or model, tokens))
    return parts or [(served_model(response, model), _tokens_of(usage))]


def response_models(response: Any, model: str) -> list[str]:
    """Every model billed for this response (served model first), deduplicated."""
    names = [served_model(response, model)] + [name for name, _ in usage_parts(response, model)]
    return list(dict.fromkeys(n for n in names if n))


def usage_from_response(response: Any, *, agent: str, task: str, model: str, run_id: str = "",
                        prices: Mapping[str, Mapping[str, float]] | None = None,
                        web_search_per_1k: float | None = None) -> UsageRecord:
    """Build a priced ``UsageRecord`` from an SDK ``Message`` (or a dict/fake).

    Missing fields count as 0. ``record.model`` is the model that served the
    response (``response.model`` — differs from ``model`` after a server-side
    refusal fallback; ``model`` when the response names none). Tokens and cost
    cover every attempt in ``usage.iterations`` (see ``usage_parts``), each at
    its own model's price, plus the web-search fee once; a part whose model has
    no price adds 0 and is logged (``response_models`` lists the models so the
    caller can warn). Only reads ``model`` and ``usage`` — never ``content``
    (a refusal is recorded too).
    """
    usage = _field(response, "usage")
    served = served_model(response, model)
    top = _tokens_of(usage)
    parts = usage_parts(response, model)
    summed = tuple(sum(tokens[i] for _, tokens in parts) for i in range(4))
    # The iteration breakdown includes the top-level attempt; never bill less than top-level reports.
    tokens = tuple(max(a, b) for a, b in zip(summed, top))
    record = UsageRecord(
        run_id=run_id,
        agent=agent,
        task=task,
        model=served,
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        cache_read_tokens=tokens[2],
        cache_write_tokens=tokens[3],
        web_search_requests=_count(_field(usage, "server_tool_use"), "web_search_requests"),
        created_at=now_iso(),
    )
    if is_mock_model(served):
        return record
    if prices is None or web_search_per_1k is None:
        table, web = load_prices()
        prices = table if prices is None else prices
        web_search_per_1k = web if web_search_per_1k is None else web_search_per_1k

    def priced(parts_: list[tuple[str, tuple[int, int, int, int]]]) -> float:
        total = 0.0
        for name, counts in parts_:
            price = {f: 0.0 for f in PRICE_FIELDS} if is_mock_model(name) else _lookup(prices, name)
            if price is None:
                _warn_unpriced(name)
                continue
            total += _token_cost(price, *counts)
        return total

    token_cost = max(priced(parts), priced([(served, top)]))
    searches = record.web_search_requests * web_search_per_1k / 1000
    return record.model_copy(update={"cost_usd": round(token_cost + searches, 6)})


def estimate_tokens(text: str) -> int:
    """Rough token estimate for synthetic (mock) usage: ~2 characters per token
    for Korean-heavy text. Never used for billing."""
    return max(1, math.ceil(len(text) / 2)) if text else 0


def mock_usage(*, agent: str, task: str, prompt: str, output: str, run_id: str = "") -> UsageRecord:
    """Synthetic usage for mock mode: estimated tokens, model ``mock``, cost 0."""
    return UsageRecord(run_id=run_id, agent=agent, task=task, model=MOCK_MODEL,
                       input_tokens=estimate_tokens(prompt), output_tokens=estimate_tokens(output),
                       cost_usd=0.0, created_at=now_iso())
