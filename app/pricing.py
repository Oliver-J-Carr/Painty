"""Estimated cost of a painting, from its token counts and per-model prices.

Prices are set in .env, one variable per model, in US dollars per million tokens:

    PAINTY_PRICE_GPT_6_1_SOL=2/0.10/10/2.50   input / cached input / output / cache write
    PAINTY_PRICE_GPT_5_4=2.50/0.25/15         input / cached input / output (writes cost nothing extra)
    PAINTY_PRICE_GPT_5_4_MINI=0.25/2          input / output (cached costs the same as input)

The variable name is the model name in capitals, with every run of other
characters turned into "_". Costs are always worked out from the current
prices, so older paintings get one too, and changing a price changes them all.

OpenAI's input tokens include the cached reads, the cache writes and images, and
output tokens include reasoning. Each input token is billed at exactly one rate:
cached, cache write or plain input. Models from GPT-5.6 on bill cache writes
(1.25 times input for the default 30-minute cache); older ones charge them as
plain input, so their write price is the input price. Pricing is linear, so the
cost of a whole drawing is exact from its totals, however they were spread over calls.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping

log = logging.getLogger("painty")

PREFIX = "PAINTY_PRICE_"
# Variable name -> (input, cached input, output, cache write), US dollars per million tokens.
Price = tuple[float, float, float, float]
Prices = dict[str, Price]


def price_var(model: str) -> str:
    """The .env variable that holds a model's price: gpt-6.1-sol -> PAINTY_PRICE_GPT_6_1_SOL."""
    return PREFIX + re.sub(r"[^A-Z0-9]+", "_", (model or "").upper()).strip("_")


def parse_price(raw: str) -> Price | None:
    """'input/cached/output/write', '.../output' or 'input/output' as (input, cached,
    output, write). A missing cached or write price is the input price. None if it isn't one."""
    try:
        parts = [float(p) for p in raw.split("/")]
    except ValueError:
        return None
    if len(parts) == 2:
        parts = [parts[0], parts[0], parts[1]]
    if len(parts) == 3:
        parts.append(parts[0])
    if len(parts) != 4 or any(p < 0 or p != p or p == float("inf") for p in parts):
        return None
    return (parts[0], parts[1], parts[2], parts[3])


def read_prices(environ: Mapping[str, str]) -> Prices:
    """Every PAINTY_PRICE_* variable that holds a price. Blank ones are skipped quietly
    (no price yet), unreadable ones with a warning."""
    prices: Prices = {}
    for name, raw in environ.items():
        if not name.startswith(PREFIX) or not (raw or "").strip():
            continue
        price = parse_price(raw.strip())
        if price is None:
            log.warning("ignoring %s=%r: write it as input/cached/output[/cache write] in "
                        "dollars per million tokens, like 1.25/0.125/10", name, raw)
            continue
        prices[name] = price
    return prices


def cost(model: str, input_tokens: int, cached_tokens: int, output_tokens: int,
         prices: Prices, cache_write_tokens: int = 0, billed: float | None = None) -> dict:
    """The estimated cost in US dollars, with the rates used and the variable they came from.

    usd is None when the model has no price; var then says which variable to set.
    Paintings saved before cache writes were counted have none, so theirs are
    charged as plain input: a slight underestimate on models that bill writes.
    billed is what OpenRouter charged, passed along as it is when there is one. It
    can differ from the estimate, because a model's hosts charge different prices.
    """
    var = price_var(model)
    extra = {} if billed is None else {"billed": billed}
    rates = prices.get(var)
    if rates is None:
        return {"usd": None, "var": var, **extra}
    total = input_tokens or 0
    cached = max(0, min(cached_tokens or 0, total))
    written = max(0, min(cache_write_tokens or 0, total - cached))
    uncached = total - cached - written
    p_in, p_cached, p_out, p_write = rates
    usd = (uncached * p_in + cached * p_cached + written * p_write
           + (output_tokens or 0) * p_out) / 1_000_000
    return {"usd": round(usd, 6), "rates": list(rates), "var": var, **extra}
