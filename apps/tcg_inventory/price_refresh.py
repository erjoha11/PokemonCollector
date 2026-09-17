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
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

import card_images
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

    for card in candidates[:budget]:
        result.cards_checked += 1
        api_data = card_images.fetch_card_data(card.name, card.set, card.number)
        if api_data.tcgplayer_price is not None:
            card.tcgplayer_price = api_data.tcgplayer_price
            card.tcgplayer_price_updated_at = today
            result.cards_updated += 1
        elif api_data.low_confidence_match:
            result.cards_low_confidence.append(
                f"{card.name} ({card.set or '?'} {card.number or '?'})"
            )

    db.commit()
    return result
