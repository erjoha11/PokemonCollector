"""Seed/update the set_release_order table from data/set_release_order.json.

This is the ~100-row chronological (Series, Set) -> release_rank lookup that
drives Inventory's default sort (see README's "Sett-kronologi" section and
models.SetReleaseOrder's docstring). Safe to re-run any time the JSON file
changes: it upserts by (series, set), so existing rows get their rank
updated in place rather than duplicated.

Usage:
    python seed_set_release_order.py

Uses the same DATABASE_URL as the app (see db.py) -- run it locally against
SQLite, or with DATABASE_URL set to the Supabase connection string to update
the live database.
"""
from __future__ import annotations

import json
from pathlib import Path

from db import SessionLocal, init_db
from models import SetReleaseOrder

DATA_PATH = Path(__file__).resolve().parent / "data" / "set_release_order.json"


def main() -> None:
    init_db()
    with open(DATA_PATH, encoding="utf-8") as f:
        rows = json.load(f)

    db = SessionLocal()
    try:
        existing = {(r.series, r.set): r for r in db.query(SetReleaseOrder).all()}
        created = 0
        updated = 0
        for row in rows:
            key = (row["series"], row["set"])
            record = existing.get(key)
            if record is None:
                db.add(SetReleaseOrder(series=row["series"], set=row["set"], release_rank=row["rank"]))
                created += 1
            elif record.release_rank != row["rank"]:
                record.release_rank = row["rank"]
                updated += 1
        db.commit()
        print(f"set_release_order: {created} created, {updated} updated, {len(rows)} total in file")
    finally:
        db.close()


if __name__ == "__main__":
    main()
