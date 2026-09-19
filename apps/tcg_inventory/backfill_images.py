"""One-off backfill for cards whose `image_url` never got a successful match
at import time.

Context: `importer.py`'s regular sync path already tries to fetch each
card's image (and TCGPlayer price) via `card_images.fetch_card_data`, but
that's capped at `_MAX_IMAGE_LOOKUPS_PER_IMPORT` lookups per sync and gives
up silently on no-match/ambiguous-set (see card_images.py's module
docstring). A card that missed its budget slot, or that simply had no
confident match on a given day, never gets retried by the normal sync --
it stays `image_url IS NULL` forever unless something explicitly retries
it. This script is that explicit retry, run by hand, not on a schedule.

It deliberately does NOT touch `importer.py`'s per-sync budget/staleness
logic (issue #128's acceptance criteria) -- this is a separate, one-off
pass, not a change to the daily cron. It also never guesses: a card with
no confident match after this runs is left `image_url IS NULL`, same as
today -- a wrong image is worse than no image (see HANDOFF.md's 2026-09-16
tcgdex-guess incident this replaced).

Usage:
    python backfill_images.py [--limit N]

Uses the same DATABASE_URL as the app (see db.py) -- run it locally
against SQLite, or with DATABASE_URL set to the Supabase connection
string to update prod.
"""
from __future__ import annotations

import argparse

import card_images
from db import SessionLocal, init_db
from models import Card

# Same spirit as importer.py's _MAX_IMAGE_LOOKUPS_PER_IMPORT: the Pokemon
# TCG API's free tier is flaky and this is a manually-triggered pass, not a
# background job, so a conservative default budget keeps one run from
# hammering the API even against a DB with many NULL rows. Overridable via
# --limit for a deliberately larger/smaller pass.
_DEFAULT_LOOKUP_BUDGET = 200


def backfill_missing_images(db, limit: int = _DEFAULT_LOOKUP_BUDGET) -> tuple[int, int]:
    """Attempts an image lookup for every `Card` with `image_url IS NULL`,
    up to `limit` API calls. Returns (attempted, filled) -- `filled` is how
    many actually got a real URL written back; the rest stay NULL, same as
    a normal sync's best-effort behavior.
    """
    cards = (
        db.query(Card)
        .filter(Card.image_url.is_(None))
        .order_by(Card.id)
        .limit(limit)
        .all()
    )
    attempted = 0
    filled = 0
    for card in cards:
        attempted += 1
        api_data = card_images.fetch_card_data(card.name, card.set, card.number, card.variant)
        if api_data.image_url:
            card.image_url = api_data.image_url
            filled += 1
    db.commit()
    return attempted, filled


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
