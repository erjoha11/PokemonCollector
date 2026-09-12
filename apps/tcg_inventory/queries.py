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

from dataclasses import dataclass, field

from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

import constants
from models import Binder, Card, SetReleaseOrder

# Series with no research done in set_release_order yet sort after every
# known series, not before -- mirrors app.py's UNKNOWN_RELEASE_RANK.
_UNKNOWN_RELEASE_RANK = 999999


def _all_cards_with_collections(db: Session) -> list[Card]:
    return db.query(Card).options(selectinload(Card.collections)).all()


@dataclass
class Bucket:
    name: str
    qty: int = 0
    duplicates: int = 0
    unique_value: float = 0.0
    total_value: float = 0.0
    # Only populated for series buckets -- the sets within that series, for
    # the dashboard's expandable drill-down row. Empty for every other kind
    # of bucket (collection, rarity).
    sets: list["Bucket"] = field(default_factory=list)

    def add(self, card: Card) -> None:
        self.qty += card.qty
        self.duplicates += card.duplicates
        self.unique_value += card.unique_value
        self.total_value += card.total_value

    @property
    def unique_count(self) -> int:
        """Number of distinct cards (owned at least once), duplicates excluded.

        Always `qty - duplicates`: each card contributes exactly 1 here
        regardless of how many copies it has (min(card.qty, 1)).
        """
        return self.qty - self.duplicates


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
        "children": _ordered_children(children.values()),
        "bulk": bulk,
    }


def _ordered_children(buckets) -> list[Bucket]:
    """Dashboard display order for the Collection/Bulk breakdown -- distinct
    from `constants.priority_rank_for` (which only decides primary_collection
    tie-breaks and must stay untouched by display preferences).

    Ordinary collections sort alphabetically first, Vintage Collection is
    pinned right after the first one (always the #2 row), and the curated
    illustrator collections always sort last, alphabetically among
    themselves.
    """
    illustrators = sorted(
        (b for b in buckets if b.name in constants.ILLUSTRATOR_COLLECTIONS), key=lambda b: b.name
    )
    vintage = [b for b in buckets if b.name == constants.VINTAGE_COLLECTION_NAME]
    others = sorted(
        (
            b
            for b in buckets
            if b.name not in constants.ILLUSTRATOR_COLLECTIONS and b.name != constants.VINTAGE_COLLECTION_NAME
        ),
        key=lambda b: b.name,
    )
    return others[:1] + vintage + others[1:] + illustrators


def by_series_breakdown(db: Session) -> list[Bucket]:
    """Series sort by release order (oldest first), not alphabetically --
    matches Inventory's default "release" sort. A series with no
    set_release_order rows at all sorts after every known series.

    Each series bucket also carries `.sets` -- the sets within that series,
    for the dashboard's expandable per-series drill-down row -- sorted the
    same way (release order, unresearched sets last).
    """
    cards = _all_cards_with_collections(db)
    buckets: dict[str, Bucket] = {}
    set_buckets: dict[tuple[str, str], Bucket] = {}
    for card in cards:
        series_key = card.series or "(uten serie)"
        bucket = buckets.setdefault(series_key, Bucket(name=series_key))
        bucket.add(card)

        set_key = card.set or "(uten sett)"
        set_bucket = set_buckets.setdefault((series_key, set_key), Bucket(name=set_key))
        set_bucket.add(card)

    release_ranks = dict(
        db.query(SetReleaseOrder.series, func.min(SetReleaseOrder.release_rank))
        .group_by(SetReleaseOrder.series)
        .all()
    )
    set_release_ranks = {(r.series, r.set): r.release_rank for r in db.query(SetReleaseOrder).all()}

    for (series_key, _set_key), set_bucket in set_buckets.items():
        buckets[series_key].sets.append(set_bucket)
    for series_key, bucket in buckets.items():
        bucket.sets.sort(
            key=lambda b, series_key=series_key: (
                set_release_ranks.get((series_key, b.name), _UNKNOWN_RELEASE_RANK),
                b.name,
            )
        )

    return sorted(
        buckets.values(),
        key=lambda b: (release_ranks.get(b.name, _UNKNOWN_RELEASE_RANK), b.name),
    )


# Rarity tiers with a well-known, unambiguous order -- everything else (the
# many special/holo/ultra variants that don't share one universal ranking
# across eras) sorts alphabetically after these, per request ("Common først").
_RARITY_TIER_ORDER = ["Common", "Uncommon", "Rare"]


def by_rarity_breakdown(db: Session) -> list[Bucket]:
    cards = _all_cards_with_collections(db)
    buckets: dict[str, Bucket] = {}
    for card in cards:
        key = card.rarity or "(uten rarity)"
        bucket = buckets.setdefault(key, Bucket(name=key))
        bucket.add(card)

    def _rank(b: Bucket) -> tuple[int, str]:
        try:
            return (_RARITY_TIER_ORDER.index(b.name), "")
        except ValueError:
            return (len(_RARITY_TIER_ORDER), b.name)

    return sorted(buckets.values(), key=_rank)


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
