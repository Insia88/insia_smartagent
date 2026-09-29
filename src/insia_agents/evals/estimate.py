"""Live-mode cost estimate printed before any paid call.

Two methods, best first:

1. ``measured``: a previous live eval (``--estimate-from <result folder>``)
   gives each case's measured cost; a case it did not run gets that run's
   average cost per channel × its channel count. This is the pilot-based
   estimate the eval guidance recommends: run one or two cases live first.
2. ``model``: a static token model. Every call is sized in characters from
   the real prompt files (agent prompt + channel guide, read now) plus
   typical artifact sizes measured on the recorded sample run
   (``examples/sample-run``, 2026-09-28), turned into tokens at
   ``CHARS_PER_TOKEN`` and priced with ``costs.py`` (prices.json and
   ``INSIA_PRICE_*`` overrides included). Thinking tokens are billed as output
   and assumed to be ``THINKING_FACTOR`` × the visible output. Prompt caching
   is ignored (every input token at the full rate), so the model errs high on
   input; web-search result tokens are the largest unknown. Treat it as
   ±2~3×, never as a quote.

Per case: plan + research (search call with web tools, structuring call) +
per channel draft + review + ``r`` × (revise + review), with ``r`` = 1 for
the typical figure and ``max_rounds`` for the maximum.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..costs import load_prices, price_for
from ..prompt_loader import agent_prompt, channel_guide

CHARS_PER_TOKEN = 1.5  # Korean-heavy JSON/markdown (costs.estimate_tokens uses 2.0 for synthetic mock usage)
THINKING_FACTOR = 1.0  # adaptive thinking tokens ≈ visible output tokens (billed as output)

# Typical sizes in characters, measured on the recorded sample run (examples/sample-run, 2026-09-28).
PLAN_CHARS = 6_000
RESEARCH_PACK_CHARS = 36_000
SEARCH_MEMO_CHARS = 15_000
SEARCH_INPUT_TOKENS = 120_000  # web search/fetch results re-sent across the research call (assumption)
DRAFT_CHARS = {"bizplan": 22_000, "naver_blog": 6_000, "linkedin": 2_500, "instagram": 7_000}
REVIEW_CHARS = {"bizplan": 19_000, "naver_blog": 12_000, "linkedin": 8_500, "instagram": 8_500}


@dataclass
class CaseEstimate:
    case_id: str
    channels: int
    typical_usd: float
    max_usd: float
    method: str  # model | measured | measured-average

    def as_dict(self) -> dict[str, Any]:
        return {"case": self.case_id, "channels": self.channels, "typical_usd": round(self.typical_usd, 4),
                "max_usd": round(self.max_usd, 4), "method": self.method}


def _tokens(chars: float) -> float:
    return math.ceil(chars / CHARS_PER_TOKEN)


def _safe_len(loader, name: str) -> int:
    try:
        return len(loader(name))
    except Exception:  # noqa: BLE001 - a missing prompt file must not break the estimate
        return 5_000


def _call_usd(price: Mapping[str, float], input_chars: float, output_chars: float, *, extra_input_tokens: float = 0.0) -> float:
    tokens_in = _tokens(input_chars) + extra_input_tokens
    tokens_out = _tokens(output_chars) * (1 + THINKING_FACTOR)
    return (tokens_in * price.get("input", 0.0) + tokens_out * price.get("output", 0.0)) / 1_000_000


def model_estimate(case_id: str, channels: Sequence[str], *, model: str, max_rounds: int, context_chars: int,
                   web_search_max_uses: int = 12, home: str | Path | None = None) -> CaseEstimate:
    """Static token-model estimate for one case (see the module docstring)."""
    prices, web_per_1k = load_prices(home=home)
    price = price_for(model, prices=prices)
    if price is None:  # unknown model: the caller warns; estimate with the default model's price shape at 0
        price = {"input": 0.0, "output": 0.0}
    orchestrator = _safe_len(agent_prompt, "orchestrator")
    researcher = _safe_len(agent_prompt, "researcher")
    reviewer = _safe_len(agent_prompt, "reviewer")
    fixed = (_call_usd(price, orchestrator + context_chars, PLAN_CHARS)
             + _call_usd(price, researcher + PLAN_CHARS + context_chars, SEARCH_MEMO_CHARS, extra_input_tokens=SEARCH_INPUT_TOKENS)
             + web_search_max_uses * web_per_1k / 1000
             + _call_usd(price, researcher + SEARCH_MEMO_CHARS * 2 + context_chars, RESEARCH_PACK_CHARS))
    typical = fixed
    maximum = fixed
    for channel in channels:
        guide = _safe_len(channel_guide, channel)
        draft = DRAFT_CHARS.get(channel, 6_000)
        review = REVIEW_CHARS.get(channel, 10_000)
        draft_in = orchestrator + guide + PLAN_CHARS + RESEARCH_PACK_CHARS + context_chars
        review_in = reviewer + guide + RESEARCH_PACK_CHARS + draft + context_chars
        first = _call_usd(price, draft_in, draft) + _call_usd(price, review_in, review)
        again = _call_usd(price, draft_in + draft + review, draft) + _call_usd(price, review_in, review)
        typical += first + again * min(1, max_rounds)
        maximum += first + again * max_rounds
    return CaseEstimate(case_id, len(channels), typical, maximum, "model")


def load_measured(path: str | Path) -> dict[str, Any]:
    """Measured costs from a previous live eval result folder (its summary.json)."""
    folder = Path(path)
    summary_path = folder / "summary.json" if folder.is_dir() else folder
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    if data.get("mode") != "live":
        raise ValueError("live 모드로 돌린 평가 결과만 비용 추정에 쓸 수 있어요 (mock 결과는 비용이 0이에요)")
    per_case: dict[str, tuple[float, int]] = {}
    channels = 0
    total = 0.0
    for case in data.get("cases", []):
        if case.get("status") not in ("ok", "partial"):
            continue
        n = len([c for c in case.get("channels", []) if c.get("status") == "ok"]) or 1
        cost = float(case.get("cost_usd") or 0.0) / max(1, int(case.get("reps") or 1))
        per_case[case["id"]] = (cost, n)
        channels += n
        total += cost
    return {"per_case": per_case, "per_channel": (total / channels) if channels else 0.0, "source": str(summary_path)}


def measured_estimate(case_id: str, channels: Sequence[str], measured: Mapping[str, Any], *, max_rounds: int) -> CaseEstimate:
    known = measured["per_case"].get(case_id)
    if known is not None and known[1] == len(channels):
        cost = known[0]
        return CaseEstimate(case_id, len(channels), cost, cost * 1.5, "measured")
    cost = measured["per_channel"] * len(channels)
    return CaseEstimate(case_id, len(channels), cost, cost * 1.5, "measured-average")
