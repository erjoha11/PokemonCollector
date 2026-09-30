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

1. "live"       -- fetched from Norges Bank within the last `_CACHE_TTL`, by
                   this process or (when a DB `bind` is passed) by any
                   invocation that stored it in the `fx_rates` table.
2. "last-known" -- the fetch failed, but an earlier one in this process
                   succeeded; its rates are reused.
3. "stored"     -- the fetch failed and this process has nothing, but the
                   `fx_rates` table does: the latest stored rate per
                   currency (issue #210). Only when a `bind` is passed.
4. "fallback"   -- nothing has ever succeeded anywhere: the old fixed
                   approximations below, last resort only.

Caching is per process (module-level): the first price lookup of a run pays
for one HTTP request, every later card in that run reuses it. A failed fetch
is cached too (for `_FAILURE_TTL`), so an unreachable API costs one timeout
per run, not one per card. On Vercel "per process" means per warm function
instance -- which is why rates are also persisted to the `fx_rates` table
(models.FxRate, one row per observation date + currency): a cold start
reuses a rate another invocation fetched, and a Norges Bank outage lands on
the last stored rate instead of the constant. `price_refresh` reports which
source a run used so that shows up in the cron response/logs.

DB access here always uses its own short connection on `bind` (never the
caller's session/transaction), so storing a rate commits on its own and a DB
error can't poison the caller's transaction. Any DB error is swallowed:
rates degrade to the in-process/constant behaviour, never block pricing.
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


def _load_stored(bind, fresh_since: dt.datetime | None = None) -> FxRates | None:
    """Latest stored rate per currency from the fx_rates table (only rows
    fetched at/after `fresh_since`, when given). None when there's nothing
    usable or the table can't be read."""
    from sqlalchemy import select

    from models import FxRate

    try:
        rates: dict[str, float] = {}
        as_of: dt.date | None = None
        with bind.connect() as conn:
            for currency in FALLBACK_RATES:
                query = select(FxRate.rate_nok, FxRate.date).where(FxRate.currency == currency)
                if fresh_since is not None:
                    query = query.where(FxRate.fetched_at >= fresh_since)
                row = conn.execute(query.order_by(FxRate.date.desc(), FxRate.id.desc()).limit(1)).first()
                if row is not None:
                    rates[currency] = row.rate_nok
                    as_of = row.date if as_of is None else max(as_of, row.date)
    except Exception:  # noqa: BLE001 -- never block pricing on the DB
        return None
    if not rates:
        return None
    return FxRates(rates={**FALLBACK_RATES, **rates}, as_of=as_of, source="stored")


def _store(bind, rates: FxRates) -> None:
    """Persist a live fetch (one row per currency for its observation date;
    an existing row for that date is left as-is)."""
    if rates.as_of is None:
        return
    from sqlalchemy import update
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from models import FxRate

    try:
        insert_fn = pg_insert if bind.dialect.name == "postgresql" else sqlite_insert
        now = dt.datetime.utcnow()
        rows = [
            {"date": rates.as_of, "currency": currency, "rate_nok": rate, "fetched_at": now}
            for currency, rate in rates.rates.items()
        ]
        with bind.begin() as conn:
            conn.execute(insert_fn(FxRate).values(rows).on_conflict_do_nothing(index_elements=["date", "currency"]))
            # Mark the observation as re-confirmed now, so other invocations
            # treat it as fresh (see get_rates' stored-reuse step).
            conn.execute(
                update(FxRate).where(FxRate.date == rates.as_of, FxRate.currency.in_(list(rates.rates))).values(fetched_at=now)
            )
    except Exception:  # noqa: BLE001
        pass


def get_rates(bind=None, force_refresh: bool = False) -> FxRates:
    """Current NOK rates, cached per process -- see module docstring for the
    live -> last-known -> stored -> fallback order. `bind` (a SQLAlchemy
    engine, e.g. `session.get_bind()`) enables the fx_rates table: reuse of
    a rate stored within the cache TTL, persisting a live fetch, and the
    "stored" fallback. Never raises."""
    global _cache, _cache_expires_at, _last_known
    now = time.monotonic()
    if not force_refresh and _cache is not None and now < _cache_expires_at:
        return _cache

    if bind is not None and not force_refresh:
        recent = _load_stored(bind, fresh_since=dt.datetime.utcnow() - dt.timedelta(seconds=_CACHE_TTL))
        if recent is not None:
            rates = FxRates(rates=recent.rates, as_of=recent.as_of, source="live")
            _last_known = rates
            _cache, _cache_expires_at = rates, now + _CACHE_TTL
            return rates

    live = _fetch_live()
    if live is not None:
        # Fill in any currency the response lacked from what we had before.
        merged = {**FALLBACK_RATES, **(_last_known.rates if _last_known else {}), **live.rates}
        rates = FxRates(rates=merged, as_of=live.as_of, source="live")
        _last_known = rates
        _cache, _cache_expires_at = rates, now + _CACHE_TTL
        if bind is not None:
            _store(bind, FxRates(rates=live.rates, as_of=live.as_of, source="live"))
        return rates

    stored = _load_stored(bind) if bind is not None and _last_known is None else None
    if _last_known is not None:
        rates = FxRates(rates=_last_known.rates, as_of=_last_known.as_of, source="last-known")
    elif stored is not None:
        rates = stored
    else:
        rates = FxRates(rates=dict(FALLBACK_RATES), as_of=None, source="fallback")
    _cache, _cache_expires_at = rates, now + _FAILURE_TTL
    return rates


def usd_to_nok() -> float:
    return get_rates().to_nok("USD")


def eur_to_nok() -> float:
    return get_rates().to_nok("EUR")
