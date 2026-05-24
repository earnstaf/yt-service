"""LLM cost computation.

Static per-million-token price table. Prices are sourced from vendor public
pricing pages as of 2026-05; review periodically and update with citations.
``compute_cost`` returns a ``Decimal`` so the daily-cost-cap arithmetic in
:func:`app.llm.fallback.execute` is exact.

If a model isn't in the table, we fall back to ``Decimal('0')`` and log a
warning. That's safer than refusing to serve — the worst case is under-
counting against the cost cap, which is itself approximate.
"""

from __future__ import annotations

from decimal import Decimal

from app.logging import get_logger

_logger = get_logger("llm.cost")


# (input_per_million, output_per_million) in USD.
# Sources fetched 2026-05; update with pricing changes.
PRICES_USD_PER_MILLION: dict[str, tuple[Decimal, Decimal]] = {
    # Anthropic — https://www.anthropic.com/pricing#anthropic-api
    "claude-opus-4-7": (Decimal("15.00"), Decimal("75.00")),
    "claude-sonnet-4-6": (Decimal("3.00"), Decimal("15.00")),
    # OpenAI — https://openai.com/api/pricing/
    "gpt-4o": (Decimal("2.50"), Decimal("10.00")),
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "whisper-1": (Decimal("0"), Decimal("0")),  # billed per minute, not tokens
    # Google — https://ai.google.dev/pricing
    "gemini-2.5-flash": (Decimal("0.30"), Decimal("2.50")),
    "gemini-2.5-pro": (Decimal("1.25"), Decimal("10.00")),
}


def compute_cost(model: str, tokens_in: int, tokens_out: int) -> Decimal:
    """Return USD cost for the given token usage at the model's rates.

    Returns ``Decimal('0')`` and logs a warning if the model isn't priced.
    """
    rates = PRICES_USD_PER_MILLION.get(model)
    if rates is None:
        _logger.warning("llm_cost_unknown_model", model=model)
        return Decimal("0")
    in_rate, out_rate = rates
    return (Decimal(tokens_in) * in_rate + Decimal(tokens_out) * out_rate) / Decimal("1000000")
