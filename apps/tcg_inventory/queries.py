"""Read-side aggregation queries backing the dashboard.

Everything here is computed live from `cards` (+ its relationships) on every
call -- nothing is cached or stored, per the "duplicates/total_value must
never go out of sync" rule this app exists to fix.

Cards are loaded once with collections eager-loaded and grouped in Python:
the whole collection is a few thousand rows at most (a physical card
binder), so this is simpler and easier to verify than reproducing the
primary-collection tie-break logic in SQL.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, selectinload

from models import Binder, Card


def _all_cards_with_collections(db: Session) -> list[Card]:
    return db.query(Card).options(selectinload(Card.collections)).all()


@dataclass
class Bucket:
    name: str
    qty: int = 0
    duplicates: int = 0
    unique_value: float = 0.0
    total_value: float = 0.0

    def add(self, card: Card) -> None:
        self.qty += card.qty
        self.duplicates += card.duplicates
        self.unique_value += card.unique_value
        self.total_value += card.total_value


def headline_summary(db: Session) -> dict:
    cards = _all_cards_with_collections(db)
    qty_physical = sum(c.qty for c in cards)
    qty_unique = sum(1 for c in cards if c.qty > 0)
    unique_value = sum(c.unique_value for c in cards)
    total_value = sum(c.total_value for c in cards)
    return {
        "qty_physical": qty_physical,
        "qty_unique": qty_unique,
        "unique_value": unique_value,
        "total_value": total_value,
    }


def collection_bulk_breakdown(db: Session) -> dict:
    """Nested Collection (parent) / named collections (children) / Bulk.

    Children are keyed by each card's primary_collection, so the parent
    (sum of all children) always matches exactly by construction -- this
    was the hard-won bug fix from the Excel version.
    """
    cards = _all_cards_with_collections(db)

    children: dict[str, Bucket] = {}
    bulk = Bucket(name="Bulk")
    parent = Bucket(name="Collection")

    for card in cards:
        primary = card.primary_collection
        if primary is None:
            bulk.add(card)
            continue
        bucket = children.setdefault(primary.name, Bucket(name=primary.name))
        bucket.add(card)
        parent.add(card)

    return {
        "parent": parent,
        "children": sorted(children.values(), key=lambda b: b.name),
        "bulk": bulk,
    }


def by_series_breakdown(db: Session) -> list[Bucket]:
    cards = _all_cards_with_collections(db)
    buckets: dict[str, Bucket] = {}
    for card in cards:
        key = card.series or "(uten serie)"
        bucket = buckets.setdefault(key, Bucket(name=key))
        bucket.add(card)
    return sorted(buckets.values(), key=lambda b: b.name)


def by_rarity_breakdown(db: Session) -> list[Bucket]:
    cards = _all_cards_with_collections(db)
    buckets: dict[str, Bucket] = {}
    for card in cards:
        key = card.rarity or "(uten rarity)"
        bucket = buckets.setdefault(key, Bucket(name=key))
        bucket.add(card)
    return sorted(buckets.values(), key=lambda b: b.name)


@dataclass
class BinderBucket:
    name: str
    qty: int = 0
    unique_value: float = 0.0  # sum of reference_price, NOT qty * price

    def add(self, card: Card) -> None:
        self.qty += card.qty
        self.unique_value += card.unique_value


def by_binder_breakdown(db: Session) -> list[BinderBucket]:
    binders = db.query(Binder).options(selectinload(Binder.cards)).all()
    buckets = []
    for binder in binders:
        bucket = BinderBucket(name=binder.name)
        for card in binder.cards:
            bucket.add(card)
        buckets.append(bucket)
    return sorted(buckets, key=lambda b: b.name)


def top_valuable_cards(db: Session, limit: int = 10) -> list[Card]:
    return (
        db.query(Card)
        .filter(Card.reference_price.isnot(None))
        .order_by(Card.reference_price.desc())
        .limit(limit)
        .all()
    )
