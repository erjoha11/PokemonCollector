"""Backfill for cards whose `image_url` never got a successful match.

Context: `importer.py`'s regular sync path tries each card's image once
(capped at `_MAX_IMAGE_LOOKUPS_PER_IMPORT` per sync) by name/set/number
search, which misses most cards -- and a card that missed was never retried.
This is that retry, and since 2026-09-23 it looks up by Dex's own `card_id`
first (`card_images.fetch_image_by_card_id`: the Pokemon TCG API by id for
international prints, TCGdex's Japanese catalog for "jpn_" ones), falling
back to the old name search. It still never guesses: every image comes from
an API response for a card whose number (and name, where comparable)
matches -- a wrong image is worse than no image (see HANDOFF.md's
2026-09-16 tcgdex-guess incident).

Runs from `/cron/price-refresh` after prices (daily, small budget) and from
`/cron/image-backfill` for a manual catch-up pass (see app.py), or by hand:

    python backfill_images.py [--limit N]

Most valuable cards first, so the cards the Dashboard actually shows get
their pictures first. A card with no match is stamped
`image_lookup_failed_at` and skipped for IMAGE_RETRY_AFTER_DAYS, so it
can't block the queue for the rest.

Uses the same DATABASE_URL as the app (see db.py) -- run it locally
against SQLite, or with DATABASE_URL set to the Supabase connection
string to update prod.
"""
from __future__ import annotations

import argparse
import datetime as dt
import time
from dataclasses import dataclass

from sqlalchemy import func, or_

import card_images
from db import SessionLocal, init_db
from models import Card

# Same spirit as importer.py's _MAX_IMAGE_LOOKUPS_PER_IMPORT: the image
# APIs' free tiers are flaky, so a conservative default budget keeps one run
# from hammering them. Overridable via --limit / the route's `limit` param.
_DEFAULT_LOOKUP_BUDGET = 200
# A card with no match is retried after this long (an API may add the set).
IMAGE_RETRY_AFTER_DAYS = 30


@dataclass
class BackfillResult:
    attempted: int = 0
    filled: int = 0
    out_of_time: bool = False


def backfill_missing_images(
    db,
    limit: int = _DEFAULT_LOOKUP_BUDGET,
    time_budget_s: float | None = None,
    today: dt.date | None = None,
) -> tuple[int, int]:
    """Look up an image for up to `limit` cards with `image_url IS NULL`
    (owned, most valuable first; skipping ones that failed within
    IMAGE_RETRY_AFTER_DAYS), stopping early once `time_budget_s` seconds
    have passed -- a serverless function has a hard time limit. Returns
    (attempted, filled); the rest stay NULL. Commits as it goes, so a run
    cut short still keeps what it found.
    """
    result = run_backfill(db, limit=limit, time_budget_s=time_budget_s, today=today)
    return result.attempted, result.filled


def run_backfill(db, limit=_DEFAULT_LOOKUP_BUDGET, time_budget_s=None, today=None) -> BackfillResult:
    today = today or dt.date.today()
    retry_cutoff = today - dt.timedelta(days=IMAGE_RETRY_AFTER_DAYS)
    price = func.coalesce(Card.tcgplayer_price, Card.reference_price)
    cards = (
        db.query(Card)
        .filter(Card.image_url.is_(None))
        .filter(or_(Card.image_lookup_failed_at.is_(None), Card.image_lookup_failed_at < retry_cutoff))
        .order_by((Card.qty > 0).desc(), price.desc().nulls_last(), Card.id)
        .limit(limit)
        .all()
    )
    started = time.monotonic()
    result = BackfillResult()
    for card in cards:
        if time_budget_s is not None and time.monotonic() - started > time_budget_s:
            result.out_of_time = True
            break
        result.attempted += 1
        image_url = card_images.fetch_image_by_card_id(card.card_id, card.name, card.number)
        if not image_url and not (card.card_id or "").startswith("jpn_"):
            # Japanese prints aren't in the Pokemon TCG API, so its name
            # search can only ever return a wrong (English) card for them.
            image_url = card_images.fetch_card_data(card.name, card.set, card.number, card.variant).image_url
        if image_url:
            card.image_url = image_url
            card.image_lookup_failed_at = None
            result.filled += 1
        else:
            card.image_lookup_failed_at = today
        if result.attempted % 10 == 0:
            db.commit()
    db.commit()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_LOOKUP_BUDGET,
        help=f"max number of API lookups to attempt this run (default {_DEFAULT_LOOKUP_BUDGET})",
    )
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        attempted, filled = backfill_missing_images(db, limit=args.limit)
        still_missing = db.query(Card).filter(Card.image_url.is_(None)).count()
        print(
            f"backfill_images: attempted {attempted} lookup(s), filled {filled}, "
            f"{still_missing} card(s) still without an image_url"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
