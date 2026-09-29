"""Daily NOK exchange rates from Norges Bank, for converting foreign-currency
card prices into NOK -- the one currency every price in this app is stored
and shown in (see card_images._best_tcgplayer_price: the Pokemon TCG API's
TCGplayer prices are USD).

Replaces a hard-coded `_USD_TO_NOK = 10.5` that had drifted ~10% above the
real rate (issue #209). Norges Bank's open SDMX API needs no key:

    https://data.norges-bank.no/api/data/EXR/B.USD+EUR.NOK.SP?format=sdmx-json&lastNObservations=1

One request returns the latest business-day spot rate for every currency
asked for. EUR is fetched alongside USD (same request, no extra cost) for a
later EUR-priced source (pricing Phase 3); nothing uses it yet.

Never blocks pricing. `get_rates()` always returns a usable rate for every
currency in `FALLBACK_RATES`, falling back in this order:

1. "live"       -- fetched from Norges Bank within the last `_CACHE_TTL`.
2. "last-known" -- the fetch failed, but an earlier one in this process
                   succeeded; its rates are reused.
3. "fallback"   -- nothing has ever succeeded in this process: the old fixed
                   approximations below, last resort only.

Caching is per process (module-level): the first price lookup of a run pays
for one HTTP request, every later card in that run reuses it. A failed fetch
is cached too (for `_FAILURE_TTL`), so an unreachable API costs one timeout
per run, not one per card. On Vercel "per process" means per warm function
instance, so a cold start with Norges Bank down lands on "fallback";
`price_refresh` reports which source a run used so that shows up in the cron
response/logs.
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass

import httpx

NORGES_BANK_URL = (
    "https://data.norges-bank.no/api/data/EXR/B.USD+EUR.NOK.SP"
    "?format=sdmx-json&lastNObservations=1"
)
_TIMEOUT = 5.0

# Last resort only (see module docstring). USD is the pre-#209 constant,
# kept as-is on purpose; EUR is a similarly rough approximation.
FALLBACK_RATES: dict[str, float] = {"USD": 10.5, "EUR": 11.5}

_CACHE_TTL = 6 * 60 * 60  # seconds; Norges Bank publishes once per business day
_FAILURE_TTL = 10 * 60  # retry a failed fetch at most this often


@dataclass(frozen=True)
class FxRates:
    """NOK per 1 unit of each currency, e.g. rates["USD"] == 9.576."""

    rates: dict[str, float]
    as_of: dt.date | None  # Norges Bank's observation date; None for fallback
    source: str  # "live" | "last-known" | "fallback"

    def to_nok(self, currency: str) -> float:
        return self.rates[currency.upper()]


_cache: FxRates | None = None
_cache_expires_at: float = 0.0
_last_known: FxRates | None = None


def reset_cache() -> None:
    """Forget every cached/last-known rate (tests)."""
    global _cache, _cache_expires_at, _last_known
    _cache, _cache_expires_at, _last_known = None, 0.0, None


def set_rates(rates: FxRates, ttl: float = _CACHE_TTL) -> None:
    """Seed the cache directly -- used by tests to keep the suite offline and
    deterministic, and usable by a caller that already has a rate."""
    global _cache, _cache_expires_at, _last_known
    _cache, _cache_expires_at = rates, time.monotonic() + ttl
    if rates.source == "live":
        _last_known = rates


def parse_sdmx_rates(payload: dict) -> FxRates:
    """Parse Norges Bank's SDMX-JSON into an FxRates (source="live").

    Series keys look like "0:1:0:0" -- one index per series dimension
    (FREQ, BASE_CUR, QUOTE_CUR, TENOR), each pointing into that dimension's
    `values` list in `structure.dimensions.series`. BASE_CUR is decoded by
    dimension id rather than assuming a fixed position or order. A series'
    UNIT_MULT attribute (e.g. 2 for JPY, quoted per 100) is honoured if
    present. Only NOK-quoted series are kept. Raises ValueError/KeyError on
    a payload it can't make sense of, or one with no usable rate at all.
    """
    data = payload["data"]
    structure = data["structure"]
    dimensions = structure["dimensions"]["series"]
    dim_index = {dim["id"]: pos for pos, dim in enumerate(dimensions)}
    base_pos, quote_pos = dim_index["BASE_CUR"], dim_index.get("QUOTE_CUR")

    series_attrs = (structure.get("attributes") or {}).get("series") or []
    unit_mult_pos = next((pos for pos, a in enumerate(series_attrs) if a.get("id") == "UNIT_MULT"), None)

    obs_dims = structure["dimensions"].get("observation") or []
    obs_dates = obs_dims[0]["values"] if obs_dims else []

    rates: dict[str, float] = {}
    as_of: dt.date | None = None
    for key, series in data["dataSets"][0]["series"].items():
        indices = [int(part) for part in key.split(":")]
        base = dimensions[base_pos]["values"][indices[base_pos]]["id"]
        if quote_pos is not None and dimensions[quote_pos]["values"][indices[quote_pos]]["id"] != "NOK":
            continue

        observations = series.get("observations") or {}
        if not observations:
            continue
        obs_key = max(observations, key=int)  # latest, if more than one came back
        value = float(observations[obs_key][0])

        if unit_mult_pos is not None:
            attr_indices = series.get("attributes") or []
            if unit_mult_pos < len(attr_indices) and attr_indices[unit_mult_pos] is not None:
                mult = series_attrs[unit_mult_pos]["values"][attr_indices[unit_mult_pos]]["id"]
                value = value / (10 ** int(mult))

        if value <= 0:
            continue
        rates[base] = value
        if int(obs_key) < len(obs_dates):
            obs_date = dt.date.fromisoformat(obs_dates[int(obs_key)]["id"][:10])
            as_of = obs_date if as_of is None else max(as_of, obs_date)

    if not rates:
        raise ValueError("no NOK exchange rates in Norges Bank response")
    return FxRates(rates=rates, as_of=as_of, source="live")


def _fetch_live() -> FxRates | None:
    """One Norges Bank request (plus one retry). None on any failure."""
    for _attempt in range(2):
        try:
            response = httpx.get(NORGES_BANK_URL, timeout=_TIMEOUT)
            response.raise_for_status()
            return parse_sdmx_rates(response.json())
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            continue
    return None


def get_rates(force_refresh: bool = False) -> FxRates:
    """Current NOK rates, cached per process -- see module docstring for the
    live -> last-known -> fallback order. Never raises."""
    global _cache, _cache_expires_at, _last_known
    now = time.monotonic()
    if not force_refresh and _cache is not None and now < _cache_expires_at:
        return _cache

    live = _fetch_live()
    if live is not None:
        # Fill in any currency the response lacked from what we had before.
        merged = {**FALLBACK_RATES, **(_last_known.rates if _last_known else {}), **live.rates}
        rates = FxRates(rates=merged, as_of=live.as_of, source="live")
        _last_known = rates
        _cache, _cache_expires_at = rates, now + _CACHE_TTL
        return rates

    if _last_known is not None:
        rates = FxRates(rates=_last_known.rates, as_of=_last_known.as_of, source="last-known")
    else:
        rates = FxRates(rates=dict(FALLBACK_RATES), as_of=None, source="fallback")
    _cache, _cache_expires_at = rates, now + _FAILURE_TTL
    return rates


def usd_to_nok() -> float:
    return get_rates().to_nok("USD")


def eur_to_nok() -> float:
    return get_rates().to_nok("EUR")
