"""Standalone TCGplayer price refresh, decoupled from Dex CSV sync.

Since issue #210 this writes each card's `pokemontcg` row in `card_prices`
(models.CardPrice; `cards.tcgplayer_price` is kept as a mirror) and then
re-resolves the card's market price (pricing.resolve_cards). The due/backoff
state below is read from that row, per source, not from `cards`.

`importer.py` already refreshes a card's pokemontcg price as a side effect
of a Dex sync (see its own `_MAX_PRICE_LOOKUPS_PER_IMPORT`/
`_PRICE_STALE_AFTER_DAYS`) -- but that means pricing only ever gets fresher
when a Dex sync happens to run. Per issue #93, Dex sync is meant to become
optional/droppable over time, and pricing is the one thing it quietly still
provides for free -- so it needs its own refresh schedule, independent of
whether/when a sync runs. `app.py`'s `/cron/price-refresh` route (its own
Vercel Cron entry in `vercel.json`) calls `refresh_stale_prices` below on its
own daily schedule; `importer.py`'s own per-sync refresh stays too
(harmless overlap -- whichever runs first just makes the other's job a
no-op for that card until the price goes stale again). Both share the
"which cards are due" rule and the failed-lookup backoff defined here
(price_lookup_due / apply_price_lookup, issue #216).

Also a one-off CLI for forcing a full re-price regardless of staleness
(issue #209: every price stored before the live Norges Bank rate replaced a
fixed, ~10% too high USD/NOK constant):

    python price_refresh.py --reprice-all [--limit N] [--dry-run]

Uses the same DATABASE_URL as the app (see db.py), like
seed_set_release_order.py / backfill_images.py.
"""
from __future__ import annotations

import argparse
import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import exists
from sqlalchemy.orm import Session, selectinload

import card_images
import fx_rates
import pricing
from models import Card, CardPrice

SOURCE = pricing.SOURCE_POKEMONTCG

# Independent of importer.py's own per-sync budget -- this cron isn't
# sharing a serverless function's time budget with CSV parsing, so it can
# afford to walk more cards per run.
MAX_PRICE_LOOKUPS_PER_RUN = 100

# Matches importer.py's own staleness window -- both paths refresh the same
# column on the same schedule, they just don't have to run together anymore.
PRICE_STALE_AFTER_DAYS = 7

# A card whose lookup came back with no usable price (stamped on its
# pokemontcg card_prices row's lookup_failed_at) is skipped for this many days before being
# tried again (issue #216) -- same idea as backfill_images'
# IMAGE_RETRY_AFTER_DAYS, but shorter: pokemontcg.io's free tier is flaky
# and a transient error looks the same as "no match", so an already-priced
# card that hits one shouldn't be frozen for a month. Hundreds of cards
# can never be priced (mostly Japanese prints), so this keeps them from
# being re-looked-up every day.
PRICE_RETRY_AFTER_DAYS = 14


def _pokemontcg_row_exists(*conditions):
    return exists().where(CardPrice.card_id == Card.id, CardPrice.source == SOURCE, *conditions)


def due_for_price_lookup_filter(today: dt.date):
    """SQL filter for cards whose pokemontcg price is missing or stale and
    that aren't inside a failed-lookup backoff window. Shared by the cron,
    the CLI's dry-run count, and (via price_lookup_due) importer.py's
    per-sync lookups.
    """
    stale_cutoff = today - dt.timedelta(days=PRICE_STALE_AFTER_DAYS)
    retry_cutoff = today - dt.timedelta(days=PRICE_RETRY_AFTER_DAYS)
    fresh = _pokemontcg_row_exists(CardPrice.price_nok.isnot(None), CardPrice.fetched_at >= stale_cutoff)
    backed_off = _pokemontcg_row_exists(CardPrice.lookup_failed_at >= retry_cutoff)
    return ~fresh & ~backed_off


def price_lookup_due(card: Card, today: dt.date) -> bool:
    """Python-side twin of due_for_price_lookup_filter, for a single card
    already in hand (importer.py walks CSV rows, not a query)."""
    stale_cutoff = today - dt.timedelta(days=PRICE_STALE_AFTER_DAYS)
    retry_cutoff = today - dt.timedelta(days=PRICE_RETRY_AFTER_DAYS)
    row = pricing.get_row(card, SOURCE)
    stale = row is None or row.price_nok is None or row.fetched_at is None or row.fetched_at < stale_cutoff
    backed_off = row is not None and row.lookup_failed_at is not None and row.lookup_failed_at >= retry_cutoff
    return stale and not backed_off


def apply_price_lookup(card: Card, api_data: card_images.CardApiData, today: dt.date, record_failure: bool = True) -> bool:
    """Store a lookup's price as `card`'s pokemontcg card_prices row (and the
    `tcgplayer_price` mirror) and return True, or -- when there's no usable
    price -- stamp that row's lookup_failed_at and return False. A success
    clears any earlier failure stamp; the old price is never touched on a
    failure. The caller must pricing.resolve_cards() the card before
    committing.

    A lookup that only failed because no real exchange rate was available
    (`api_data.fx_unavailable`, issue #229) writes nothing at all -- no price,
    no failure stamp -- so the card stays due and is retried next run."""
    if api_data.fx_unavailable:
        return False
    if api_data.tcgplayer_price is not None:
        pricing.record_price(
            card,
            SOURCE,
            price_nok=api_data.tcgplayer_price,
            price=api_data.tcgplayer_price_usd,
            currency="USD",
            fx_rate=api_data.usd_to_nok,
            variant_key=api_data.tcgplayer_variant_key,
            fetched_at=today,
            flags=[pricing.FLAG_VARIANT_UNCERTAIN] if api_data.variant_price_uncertain else [],
        )
        card.tcgplayer_price = api_data.tcgplayer_price
        card.tcgplayer_price_updated_at = today
        return True
    if record_failure:
        pricing.record_failure(card, SOURCE, today)
    return False


def _refresh_priority(card: Card) -> tuple:
    """Fair ordering for a limited budget (issue #216): already-priced stale
    cards first (oldest price first), then never-priced cards that have
    never failed, then cards retrying after a failed lookup (oldest failure
    first). Without this, cards that can never be priced sorted first and
    took the whole budget every day."""
    row = pricing.get_row(card, SOURCE)
    if row is not None and row.price_nok is not None and row.fetched_at is not None:
        return (0, row.fetched_at, card.id)
    if row is None or row.lookup_failed_at is None:
        return (1, dt.date.min, card.id)
    return (2, row.lookup_failed_at, card.id)


@dataclass
class PriceRefreshResult:
    cards_checked: int = 0
    cards_updated: int = 0
    cards_low_confidence: list[str] = field(default_factory=list)
    cards_variant_uncertain: list[str] = field(default_factory=list)
    # The USD/NOK rate this run converted at, and where it came from
    # ("live" / "last-known" / "stored" / "fallback", see fx_rates).
    usd_to_nok: float | None = None
    fx_source: str | None = None
    fx_as_of: dt.date | None = None
    # "ok", or "degraded" when the only rate available was fx_rates' fallback
    # constant (issue #229): then no card is looked up or written at all --
    # every due card stays due (cards_skipped) and the next run retries.
    status: str = "ok"
    degraded_reason: str | None = None
    cards_skipped: int = 0


def refresh_stale_prices(
    db: Session,
    today: dt.date | None = None,
    budget: int = MAX_PRICE_LOOKUPS_PER_RUN,
) -> PriceRefreshResult:
    """Refresh the pokemontcg price for up to `budget` due cards (see
    due_for_price_lookup_filter), in _refresh_priority order: stale priced
    cards, then never-tried cards, then failed cards past their retry
    window. A lookup with no usable price stamps the row's lookup_failed_at.

    Low-confidence API matches (see card_images._is_confident_match) are
    skipped rather than trusted -- flagged in the result for visibility
    instead of silently feeding a wrong price into the Market Value KPI.
    """
    today = today or dt.date.today()
    result = PriceRefreshResult()

    candidates = db.query(Card).options(selectinload(Card.prices)).filter(due_for_price_lookup_filter(today)).all()
    # Tiered ordering in Python rather than a dialect-specific ORDER BY
    # (NULL ordering differs between SQLite and Postgres).
    candidates.sort(key=_refresh_priority)

    _refresh_cards(db, candidates[:budget], today, result)
    return result


def _refresh_cards(
    db: Session, cards: list[Card], today: dt.date, result: PriceRefreshResult, record_failures: bool = True
) -> None:
    # Resolve the exchange rate once, up front, for the whole run -- every
    # card lookup below then reuses fx_rates' cached value (one Norges Bank
    # request per run, not per card), and the result records which rate
    # this run's prices were converted at.
    rates = fx_rates.get_rates(db.get_bind())
    result.usd_to_nok = rates.to_nok("USD")
    result.fx_source = rates.source
    result.fx_as_of = rates.as_of
    if not rates.usable("USD"):
        # Never store a price at the fallback constant (issue #229). Nothing
        # is looked up or stamped, so freshness doesn't advance and every
        # card is still due next run.
        result.status = "degraded"
        result.degraded_reason = fx_rates.FALLBACK_REASON
        result.cards_skipped = len(cards)
        return

    pending: list[int] = []
    for card in cards:
        result.cards_checked += 1
        pending.append(card.id)
        api_data = card_images.fetch_card_data(card.name, card.set, card.number, card.variant)
        if apply_price_lookup(card, api_data, today, record_failure=record_failures):
            result.cards_updated += 1
            if api_data.variant_price_uncertain:
                result.cards_variant_uncertain.append(
                    f"{card.name} ({card.set or '?'} {card.number or '?'})"
                )
        elif api_data.low_confidence_match:
            result.cards_low_confidence.append(
                f"{card.name} ({card.set or '?'} {card.number or '?'})"
            )
        if result.cards_checked % 25 == 0:
            # Long CLI runs keep progress if interrupted; each committed
            # batch is resolved first so cards.market_price never lags.
            pricing.resolve_cards(db, pending, today=today)
            pending.clear()
            db.commit()

    pricing.resolve_cards(db, pending, today=today)
    db.commit()


def reprice_all(
    db: Session,
    today: dt.date | None = None,
    limit: int | None = None,
) -> PriceRefreshResult:
    """Re-fetch the pokemontcg price for every card that already has one,
    ignoring staleness and the cron's per-run budget -- a forced full
    re-price (issue #209). Only cards with an existing price are touched:
    it corrects stored values, it doesn't try to price cards that have
    never had one (the daily cron keeps doing that).

    A card whose lookup fails keeps its old price *and* its old fetch date,
    and isn't stamped lookup_failed_at, so the cron still treats it as due. Stored values are only ever replaced
    by a fresh lookup, never rescaled.
    """
    today = today or dt.date.today()
    cards = (
        db.query(Card)
        .options(selectinload(Card.prices))
        .filter(_has_price_filter())
        .order_by(Card.id)
        .all()
    )
    # Oldest-priced first, so a --limit'ed run makes progress the same way
    # the cron does.
    cards.sort(key=lambda c: pricing.get_row(c, SOURCE).fetched_at or dt.date.min)
    if limit is not None:
        cards = cards[:limit]
    result = PriceRefreshResult()
    _refresh_cards(db, cards, today, result, record_failures=False)
    return result


def _has_price_filter():
    return _pokemontcg_row_exists(CardPrice.price_nok.isnot(None))


def main() -> None:
    parser = argparse.ArgumentParser(description="TCGPlayer price refresh (see module docstring).")
    parser.add_argument(
        "--reprice-all",
        action="store_true",
        help="re-price every card that already has a pokemontcg price, ignoring staleness/budget",
    )
    parser.add_argument("--limit", type=int, default=None, help="max cards to look up this run")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="only print how many cards would be looked up and the FX rate that would be used",
    )
    args = parser.parse_args()

    from db import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    try:
        if args.dry_run:
            query = db.query(Card)
            if args.reprice_all:
                query = query.filter(_has_price_filter())
                count = query.count()
            else:
                count = query.filter(due_for_price_lookup_filter(dt.date.today())).count()
            if args.limit is not None:
                count = min(count, args.limit)
            rates = fx_rates.get_rates(db.get_bind())
            print(
                f"price_refresh (dry run): would look up {count} card(s) at "
                f"USD/NOK {rates.to_nok('USD')} ({rates.source}, as of {rates.as_of})"
            )
            return
        if args.reprice_all:
            result = reprice_all(db, limit=args.limit)
        else:
            budget = args.limit if args.limit is not None else MAX_PRICE_LOOKUPS_PER_RUN
            result = refresh_stale_prices(db, budget=budget)
        if result.status != "ok":
            print(f"price_refresh: DEGRADED -- {result.degraded_reason} skipped={result.cards_skipped}")
        print(
            f"price_refresh: status={result.status} checked={result.cards_checked} updated={result.cards_updated} "
            f"low_confidence={len(result.cards_low_confidence)} "
            f"variant_uncertain={len(result.cards_variant_uncertain)} "
            f"usd_to_nok={result.usd_to_nok} ({result.fx_source}, as of {result.fx_as_of})"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
