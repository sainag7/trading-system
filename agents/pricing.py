"""Model pricing — turns token counts into dollars.

Used only for the Anthropic Messages API path. The Claude Agent SDK reports
``total_cost_usd`` on its ``ResultMessage``, so MCP-mediated calls never need
this table; see :mod:`agents.llm`.

Rules of this module:
  * Pure arithmetic and table lookup — no network, no I/O, no LLM.
  * An unknown model returns ``None``, never ``0.0``. A model we cannot price is
    a gap in the table that must be visible in the UI; silently billing zero
    would under-report spend and look like the tracker is working.

Prices are US dollars per 1,000,000 tokens.
"""
from __future__ import annotations

# $/1M tokens, keyed by model-ID prefix. Model IDs are complete without a date
# suffix, but config.yaml pins `claude-haiku-4-5-20251001`, so lookup is by
# LONGEST PREFIX rather than exact match — a dated snapshot prices as its base
# model instead of falling through to "unpriced".
PRICES: dict[str, tuple[float, float]] = {
    #  model prefix          (input, output)
    "claude-fable-5":        (10.00, 50.00),
    "claude-mythos-5":       (10.00, 50.00),
    "claude-opus-5":         (5.00, 25.00),
    "claude-opus-4-8":       (5.00, 25.00),
    "claude-opus-4-7":       (5.00, 25.00),
    "claude-opus-4-6":       (5.00, 25.00),
    "claude-sonnet-5":       (2.00, 10.00),
    "claude-sonnet-4-6":     (3.00, 15.00),
    "claude-haiku-4-5":      (1.00, 5.00),
}

# Cache-tier multipliers applied to the INPUT rate.
CACHE_WRITE_MULTIPLIER = 1.25   # writing a prefix into the cache
CACHE_READ_MULTIPLIER = 0.10    # reading a cached prefix (the ~90% saving)

_PER_TOKEN = 1_000_000.0

# Cost provenance, recorded alongside every figure so the UI never mixes a
# broker-reported number with one we derived.
COMPUTED = "computed"           # priced here from token counts
SDK_REPORTED = "sdk_reported"   # taken from the Agent SDK's own total_cost_usd
UNPRICED = "unpriced"           # model not in PRICES — cost is unknown, not zero


def rates_for(model: str | None) -> tuple[float, float] | None:
    """(input, output) $/1M for ``model``, or ``None`` if it is not priced.

    Matches the longest prefix so ``claude-haiku-4-5-20251001`` resolves to the
    ``claude-haiku-4-5`` row.
    """
    if not model:
        return None
    m = str(model).strip()
    best: str | None = None
    for prefix in PRICES:
        if m.startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    return PRICES[best] if best else None


def cost_usd(
    model: str | None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> tuple[float | None, str]:
    """Price one call. Returns ``(cost_or_None, source)``.

    ``source`` is :data:`COMPUTED` when the model is priced and :data:`UNPRICED`
    when it is not — in which case the cost is ``None`` so the caller can show
    "unknown" rather than a misleading $0.00.
    """
    rates = rates_for(model)
    if rates is None:
        return None, UNPRICED
    in_rate, out_rate = rates
    total = (
        (input_tokens or 0) * in_rate
        + (output_tokens or 0) * out_rate
        + (cache_write_tokens or 0) * in_rate * CACHE_WRITE_MULTIPLIER
        + (cache_read_tokens or 0) * in_rate * CACHE_READ_MULTIPLIER
    ) / _PER_TOKEN
    return total, COMPUTED
