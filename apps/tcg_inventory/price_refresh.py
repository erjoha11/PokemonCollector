"""Standalone TCGPlayer price refresh, decoupled from Dex CSV sync.

`importer.py` already refreshes a card's `tcgplayer_price` as a side effect
of a Dex sync (see its own `_MAX_PRICE_LOOKUPS_PER_IMPORT`/
`_PRICE_STALE_AFTER_DAYS`) -- but that means pricing only ever gets fresher
when a Dex sync happens to run. Per issue #93, Dex sync is meant to become
optional/droppable over time, and pricing is the one thing it quietly still
provides for free -- so it needs its own refresh schedule, independent of
whether/when a sync runs. `app.py`'s `/cron/price-refresh` route (its own
Vercel Cron entry in `vercel.json`) calls `refresh_stale_prices` below on its
own daily schedule; `importer.py`'s own per-sync refresh is left as-is
(harmless overlap -- whichever runs first just makes the other's job a
no-op for that card until the price goes stale again).

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

from sqlalchemy.orm import Session

import card_images
import fx_rates
from models import Card

# Independent of importer.py's own per-sync budget -- this cron isn't
# sharing a serverless function's time budget with CSV parsing, so it can
# afford to walk more cards per run.
MAX_PRICE_LOOKUPS_PER_RUN = 100

# Matches importer.py's own staleness window -- both paths refresh the same
# column on the same schedule, they just don't have to run together anymore.
PRICE_STALE_AFTER_DAYS = 7


@dataclass
class PriceRefreshResult:
    cards_checked: int = 0
    cards_updated: int = 0
    cards_low_confidence: list[str] = field(default_factory=list)
    cards_variant_uncertain: list[str] = field(default_factory=list)
    # The USD/NOK rate this run converted at, and where it came from
    # ("live" / "last-known" / "fallback", see fx_rates) -- surfaced in the
    # cron response so a run priced at the fallback constant is visible.
    usd_to_nok: float | None = None
    fx_source: str | None = None
    fx_as_of: dt.date | None = None


def refresh_stale_prices(
    db: Session,
    today: dt.date | None = None,
    budget: int = MAX_PRICE_LOOKUPS_PER_RUN,
) -> PriceRefreshResult:
    """Refresh `tcgplayer_price` for up to `budget` cards, oldest-priced (and
    never-priced) first, so a limited per-run budget always makes progress
    on whichever cards are most overdue rather than re-checking the same
    handful every time.

    Low-confidence API matches (see card_images._is_confident_match) are
    skipped rather than trusted -- flagged in the result for visibility
    instead of silently feeding a wrong price into the Market Value KPI.
    """
    today = today or dt.date.today()
    stale_cutoff = today - dt.timedelta(days=PRICE_STALE_AFTER_DAYS)
    result = PriceRefreshResult()

    candidates = (
        db.query(Card)
        .filter(
            (Card.tcgplayer_price_updated_at.is_(None))
            | (Card.tcgplayer_price_updated_at < stale_cutoff)
        )
        .all()
    )
    # NULL-sorts-first isn't portable between SQLite and Postgres, so the
    # "never priced first" ordering is done here in Python instead of
    # relying on a dialect-specific ORDER BY.
    candidates.sort(key=lambda c: c.tcgplayer_price_updated_at or dt.date.min)

    _refresh_cards(db, candidates[:budget], today, result)
    return result


def _refresh_cards(db: Session, cards: list[Card], today: dt.date, result: PriceRefreshResult) -> None:
    # Resolve the exchange rate once, up front, for the whole run -- every
    # card lookup below then reuses fx_rates' cached value (one Norges Bank
    # request per run, not per card), and the result records which rate
    # this run's prices were converted at.
    rates = fx_rates.get_rates()
    result.usd_to_nok = rates.to_nok("USD")
    result.fx_source = rates.source
    result.fx_as_of = rates.as_of

    for card in cards:
        result.cards_checked += 1
        api_data = card_images.fetch_card_data(card.name, card.set, card.number, card.variant)
        if api_data.tcgplayer_price is not None:
            card.tcgplayer_price = api_data.tcgplayer_price
            card.tcgplayer_price_updated_at = today
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
            db.commit()  # long CLI runs keep progress if interrupted

    db.commit()


def reprice_all(
    db: Session,
    today: dt.date | None = None,
    limit: int | None = None,
) -> PriceRefreshResult:
    """Re-fetch `tcgplayer_price` for every card that already has one,
    ignoring staleness and the cron's per-run budget -- a forced full
    re-price (issue #209). Only cards with an existing price are touched:
    it corrects stored values, it doesn't try to price cards that have
    never had one (the daily cron keeps doing that).

    A card whose lookup fails keeps its old price *and* its old
    `tcgplayer_price_updated_at`, so the cron still treats it as due. Stored
    values are only ever replaced by a fresh lookup, never rescaled.
    """
    today = today or dt.date.today()
    cards = (
        db.query(Card)
        .filter(Card.tcgplayer_price.isnot(None))
        .order_by(Card.id)
        .all()
    )
    # Oldest-priced first, so a --limit'ed run makes progress the same way
    # the cron does.
    cards.sort(key=lambda c: c.tcgplayer_price_updated_at or dt.date.min)
    if limit is not None:
        cards = cards[:limit]
    result = PriceRefreshResult()
    _refresh_cards(db, cards, today, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="TCGPlayer price refresh (see module docstring).")
    parser.add_argument(
        "--reprice-all",
        action="store_true",
        help="re-price every card that already has a tcgplayer_price, ignoring staleness/budget",
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
                query = query.filter(Card.tcgplayer_price.isnot(None))
                count = query.count()
            else:
                cutoff = dt.date.today() - dt.timedelta(days=PRICE_STALE_AFTER_DAYS)
                count = query.filter(
                    (Card.tcgplayer_price_updated_at.is_(None))
                    | (Card.tcgplayer_price_updated_at < cutoff)
                ).count()
            if args.limit is not None:
                count = min(count, args.limit)
            rates = fx_rates.get_rates()
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
        print(
            f"price_refresh: checked={result.cards_checked} updated={result.cards_updated} "
            f"low_confidence={len(result.cards_low_confidence)} "
            f"variant_uncertain={len(result.cards_variant_uncertain)} "
            f"usd_to_nok={result.usd_to_nok} ({result.fx_source}, as of {result.fx_as_of})"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
