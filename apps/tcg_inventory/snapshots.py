"""Daily per-card value snapshots -- see CardSnapshot in models.py.

Written by app.py's /cron/dropbox-sync route (source="cron") right after
the scheduled sync, and by the manual CSV-upload/Dropbox-sync routes
(source="manual") right after a user-triggered one -- see README's
"Automatic daily sync". This is what makes queries.real_value_history
possible: a real, non-approximated "what was the collection worth on date
X", with up to two points per day: the scheduled run and the latest manual
one.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from models import Card, CardSnapshot


def record_daily_snapshot(db: Session, as_of: dt.date | None = None, source: str = "cron") -> int:
    """Write one CardSnapshot row per card for `as_of` (default: today) and
    `source` ("cron" or "manual"), capturing its current qty and
    display_price (TCGPlayer price when we have one, else Dex's reference
    price).

    Idempotent per (day, source): re-running this for a date/source that
    already has snapshots updates them in place instead of creating
    duplicates, so a manual re-run or a retried cron invocation on the same
    day never double-counts -- and re-triggering a manual sync repeatedly in
    one day keeps overwriting that same "manual" row rather than piling up,
    which is what caps each day at exactly two points (cron + latest manual).

    Returns the number of cards snapshotted.
    """
    as_of = as_of or dt.date.today()

    existing = {
        snap.card_id: snap
        for snap in db.query(CardSnapshot)
        .filter(CardSnapshot.date == as_of, CardSnapshot.source == source)
        .all()
    }

    count = 0
    for card in db.query(Card).all():
        snap = existing.get(card.id)
        if snap is None:
            snap = CardSnapshot(card_id=card.id, date=as_of, source=source)
            db.add(snap)
        snap.qty = card.qty
        snap.reference_price = card.display_price
        count += 1

    db.commit()
    return count
