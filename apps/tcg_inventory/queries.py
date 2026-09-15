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

from sqlalchemy.orm import Session, selectinload

import constants
from models import Card, FavoritePokemon, PokemonAlias, SetReleaseOrder, Transaction

# Series with no research done in set_release_order yet sort after every
# known series, not before -- mirrors app.py's UNKNOWN_RELEASE_RANK.
_UNKNOWN_RELEASE_RANK = 999999


def all_cards_with_collections(db: Session) -> list[Card]:
    """The one full-table load every breakdown below is built from. Each
    breakdown also accepts an already-loaded `cards` list (see their
    `cards=None` params) so a caller building several breakdowns in one
    request -- the dashboard route does all five -- only pays for this once.
    """
    return db.query(Card).options(selectinload(Card.collections)).all()


@dataclass
class Bucket:
    name: str
    qty: int = 0
    duplicates: int = 0
    unique_value: float = 0.0
    total_value: float = 0.0
    # False only for the synthetic "Bulk" bucket (cards with no collection at
    # all) -- there's no real collection to filter Inventory by, so its qty
    # cell is plain text instead of a link. Every other bucket is filterable.
    filterable: bool = True
    # Only populated for series buckets -- the sets within that series, for
    # the dashboard's expandable drill-down row. Empty for every other kind
    # of bucket (collection, rarity, Pokemon).
    child_sets: list["Bucket"] = field(default_factory=list)
    # Every bucket accumulates the actual cards behind it via .add() below --
    # the dashboard's final drill-down level, uniformly available on every
    # kind of bucket (collection, set, rarity) since it's populated here
    # rather than per-breakdown-function.
    cards: list[Card] = field(default_factory=list)

    def add(self, card: Card) -> None:
        self.qty += card.qty
        self.duplicates += card.duplicates
        self.unique_value += card.unique_value
        self.total_value += card.total_value
        self.cards.append(card)

    @property
    def unique_count(self) -> int:
        """Number of distinct cards (owned at least once), duplicates excluded.

        Always `qty - duplicates`: each card contributes exactly 1 here
        regardless of how many copies it has (min(card.qty, 1)).
        """
        return self.qty - self.duplicates

    @property
    def distinct_sets(self) -> list[str]:
        """Every set among this bucket's cards, e.g. a Pokemon folder that
        spans several prints/species (Sableye across two sets, or a merged
        Slowpoke/Slowbro/Slowking folder) has more than one -- the template
        decides how to show that ambiguity, this just reports the raw set.
        """
        return sorted({card.set for card in self.cards if card.set})

    @property
    def distinct_series(self) -> list[str]:
        return sorted({card.series for card in self.cards if card.series})


def _card_sort_key(card: Card):
    # Default order for cards nested inside a dashboard bucket: most
    # valuable first. Column-header clicks (see app.py's CARD_LEAF_SORT_KEYS)
    # override this per-table; this is only what a freshly expanded bucket
    # shows before any column is clicked.
    return (-card.unique_value, card.number_int if card.number_int is not None else _UNKNOWN_RELEASE_RANK, card.name)


def headline_summary(db: Session, cards: list[Card] | None = None) -> dict:
    cards = all_cards_with_collections(db) if cards is None else cards
    qty_physical = sum(c.qty for c in cards)
    qty_unique = sum(1 for c in cards if c.qty > 0)
    duplicates = sum(c.duplicates for c in cards)
    unique_value = sum(c.unique_value for c in cards)
    total_value = sum(c.total_value for c in cards)
    duplicate_value = total_value - unique_value  # value tied up in extra copies specifically
    return {
        "qty_physical": qty_physical,
        "qty_unique": qty_unique,
        "duplicates": duplicates,
        "unique_value": unique_value,
        "total_value": total_value,
        "duplicate_value": duplicate_value,
        "avg_unique_value": (unique_value / qty_unique) if qty_unique else 0.0,
        "avg_physical_value": (total_value / qty_physical) if qty_physical else 0.0,
    }


def collection_bulk_breakdown(db: Session, cards: list[Card] | None = None) -> dict:
    """Nested Collection (parent) / named collections (children) / Bulk.

    Children are keyed by each card's primary_collection, so the parent
    (sum of all children) always matches exactly by construction -- this
    was the hard-won bug fix from the Excel version.
    """
    cards = all_cards_with_collections(db) if cards is None else cards

    children: dict[str, Bucket] = {}
    bulk = Bucket(name="Bulk", filterable=False)
    parent = Bucket(name="Collection")

    for card in cards:
        primary = card.primary_collection
        if primary is None:
            bulk.add(card)
            continue
        bucket = children.setdefault(primary.name, Bucket(name=primary.name))
        bucket.add(card)
        parent.add(card)

    for bucket in list(children.values()) + [bulk]:
        bucket.cards.sort(key=_card_sort_key)

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


def by_series_breakdown(db: Session, cards: list[Card] | None = None) -> list[Bucket]:
    """Series sort by release order (oldest first), not alphabetically --
    matches Inventory's default "release" sort. A series with no
    set_release_order rows at all sorts after every known series.

    Each series bucket also carries `.child_sets` -- the sets within that
    series, for the dashboard's expandable per-series drill-down row --
    sorted the same way (release order, unresearched sets last).
    """
    cards = all_cards_with_collections(db) if cards is None else cards
    buckets: dict[str, Bucket] = {}
    set_buckets: dict[tuple[str, str], Bucket] = {}
    for card in cards:
        series_key = card.series or "(uten serie)"
        bucket = buckets.setdefault(series_key, Bucket(name=series_key))
        bucket.add(card)

        set_key = card.set or "(uten sett)"
        set_bucket = set_buckets.setdefault((series_key, set_key), Bucket(name=set_key))
        set_bucket.add(card)

    # One pass over set_release_order covers both the per-series rank (its
    # earliest set's rank) and the per-set rank -- a second, near-identical
    # query for just the per-series min would just re-scan the same rows.
    set_release_rows = db.query(SetReleaseOrder).all()
    set_release_ranks = {(r.series, r.set): r.release_rank for r in set_release_rows}
    release_ranks: dict[str, int] = {}
    for r in set_release_rows:
        release_ranks[r.series] = min(release_ranks.get(r.series, r.release_rank), r.release_rank)

    for (series_key, _set_key), set_bucket in set_buckets.items():
        buckets[series_key].child_sets.append(set_bucket)
        set_bucket.cards.sort(key=_card_sort_key)
    for series_key, bucket in buckets.items():
        bucket.child_sets.sort(
            key=lambda b, series_key=series_key: (
                set_release_ranks.get((series_key, b.name), _UNKNOWN_RELEASE_RANK),
                b.name,
            )
        )

    return sorted(
        buckets.values(),
        key=lambda b: (release_ranks.get(b.name, _UNKNOWN_RELEASE_RANK), b.name),
    )


# Modern (Scarlet & Violet-era) rarity tier order, low to high, per request.
# Anything not in this list (older eras' own rarity names -- "Holo Rare",
# "Promo", etc -- that don't share one single ranking across eras) sorts
# alphabetically after these.
_RARITY_TIER_ORDER = [
    "Common",
    "Uncommon",
    "Rare",
    "Double Rare",  # includes ACE SPEC
    "Ultra Rare",  # full-art ex cards
    "Illustration Rare",
    "Special Illustration Rare",
    "Hyper Rare",  # gold cards
    "Rainbow Rare",  # phased out, used in earlier sets
    "Black White Rare",  # set-specific gold-symbol variants, e.g. Trainer Gallery
    "Secret Rare",  # numbered beyond the set's main size
]


def by_rarity_breakdown(db: Session, cards: list[Card] | None = None) -> list[Bucket]:
    cards = all_cards_with_collections(db) if cards is None else cards
    buckets: dict[str, Bucket] = {}
    for card in cards:
        key = card.rarity or "(uten rarity)"
        bucket = buckets.setdefault(key, Bucket(name=key))
        bucket.add(card)

    for bucket in buckets.values():
        bucket.cards.sort(key=_card_sort_key)

    def _rank(b: Bucket) -> tuple[int, str]:
        try:
            return (_RARITY_TIER_ORDER.index(b.name), "")
        except ValueError:
            return (len(_RARITY_TIER_ORDER), b.name)

    return sorted(buckets.values(), key=_rank)


def pokemon_alias_map(db: Session) -> dict[str, str]:
    return {row.name: row.canonical_name for row in db.query(PokemonAlias).all()}


def resolve_pokemon_name(name: str, alias_map: dict[str, str]) -> str:
    """Follow the alias chain to its root. Merging is supposed to keep this
    a single hop (see /pokemon/merge, which re-points anything aliased to
    the old name), but this walks defensively in case that invariant is
    ever broken by hand (e.g. direct DB edits).
    """
    seen: set[str] = set()
    while name in alias_map and name not in seen:
        seen.add(name)
        name = alias_map[name]
    return name


def by_pokemon_breakdown(
    db: Session, cards: list[Card] | None = None, alias_map: dict[str, str] | None = None
) -> list[Bucket]:
    """Every card sharing the same name (e.g. all Sableye you own, across
    every set/variant) grouped into one bucket -- "how much Sableye do I
    have" rather than "how much of this exact print". Names merged via
    PokemonAlias (e.g. "Dark Celebi" -> "Celebi") land in the same bucket
    as their canonical name. Alphabetical, since there's no natural
    priority order for a Pokemon the way there is for rarity tiers or set
    release dates.
    """
    alias_map = pokemon_alias_map(db) if alias_map is None else alias_map
    cards = all_cards_with_collections(db) if cards is None else cards
    buckets: dict[str, Bucket] = {}
    for card in cards:
        canonical = resolve_pokemon_name(card.name, alias_map)
        bucket = buckets.setdefault(canonical, Bucket(name=canonical))
        bucket.add(card)

    for bucket in buckets.values():
        bucket.cards.sort(key=_card_sort_key)

    return sorted(buckets.values(), key=lambda b: b.name.lower())


def favorite_pokemon_names(db: Session) -> set[str]:
    return {row.name for row in db.query(FavoritePokemon).all()}


def merge_pokemon(db: Session, name: str, canonical: str) -> None:
    """Put `name` into the same Pokemon folder as `canonical` -- e.g. "Dark
    Celebi" into "Celebi" (a rename), or "Slowpoke" into "Slowbro" (an
    evolution family) -- so the Dashboard's Pokemon breakdown groups every
    card under either name into one bucket. This only affects that display
    grouping; it never changes the underlying Card rows, prices, or export.
    `canonical` is resolved to its own true root first (in case it's itself
    already folded into something else), and anything currently folded into
    `name` is cascaded onto that same root, so no alias chain ever needs
    more than one hop to resolve. Does not commit -- the caller decides that.
    """
    name = name.strip()
    canonical = canonical.strip()
    if not name or not canonical:
        return

    alias_map = pokemon_alias_map(db)
    root = resolve_pokemon_name(canonical, alias_map)
    if name == root:
        return

    db.query(PokemonAlias).filter(PokemonAlias.canonical_name == name).update({"canonical_name": root})

    existing = db.query(PokemonAlias).filter(PokemonAlias.name == name).one_or_none()
    if existing is not None:
        existing.canonical_name = root
    else:
        db.add(PokemonAlias(name=name, canonical_name=root))

    old_favorite = db.query(FavoritePokemon).filter(FavoritePokemon.name == name).one_or_none()
    if old_favorite is not None:
        db.delete(old_favorite)
        db.flush()
        if db.query(FavoritePokemon).filter(FavoritePokemon.name == root).one_or_none() is None:
            db.add(FavoritePokemon(name=root))


def top_valuable_cards(db: Session, limit: int = 10) -> list[Card]:
    return (
        db.query(Card)
        .filter(Card.reference_price.isnot(None))
        .order_by(Card.reference_price.desc())
        .limit(limit)
        .all()
    )


# --------------------------------------------------------------------------
# Economic analysis -- development over time
# --------------------------------------------------------------------------
_UNTRACKED_MONTH = "Before tracking"  # cards imported before created_at existed


# Same three numbers as the Dashboard KPI's Value / Duplicate value / Total
# value -- "unique" never double-counts a duplicate, "duplicates" is just the
# extra value tied up in the copies beyond the first, "total" is both together.
VALUE_GROWTH_METRICS: dict[str, tuple[str, callable]] = {
    "unique": ("Unique collection", lambda card: card.unique_value),
    "duplicates": ("Duplicates", lambda card: card.total_value - card.unique_value),
    "total": ("Total", lambda card: card.total_value),
}


def collection_value_growth(
    db: Session, cards: list[Card] | None = None, metric: str = "unique"
) -> list[dict]:
    """Cumulative value of the collection, month by month, using each card's
    `created_at` as its "added" date. This is an approximation, not a real
    historical price series: it applies TODAY's reference_price to the month
    a card was added, since Dex gives no historical price snapshots. It
    answers "how has my collection's assessed value grown as I added cards",
    not "what was it actually worth back then". Cards with no created_at
    (imported before that column existed) are bucketed into one "Before
    tracking" starting point rather than guessing a date, so the running
    total still ends at today's real value for that metric.

    `metric` picks which of the three value shown -- see VALUE_GROWTH_METRICS.
    """
    if metric not in VALUE_GROWTH_METRICS:
        raise ValueError(f"unknown metric: {metric!r} (expected one of {sorted(VALUE_GROWTH_METRICS)})")
    _, value_of = VALUE_GROWTH_METRICS[metric]
    cards = all_cards_with_collections(db) if cards is None else cards

    by_month: dict[str, float] = {}
    for card in cards:
        label = card.created_at.strftime("%Y-%m") if card.created_at else _UNTRACKED_MONTH
        by_month[label] = by_month.get(label, 0.0) + value_of(card)

    ordered_labels = sorted(by_month, key=lambda label: "" if label == _UNTRACKED_MONTH else label)

    running = 0.0
    result = []
    for label in ordered_labels:
        running += by_month[label]
        result.append({"label": label, "added_value": by_month[label], "cumulative_value": running})
    return result


def cash_flow_by_month(db: Session) -> list[dict]:
    """Actual money in (purchase, price + fees) and money out (sale, price)
    per calendar month, straight from the Transaction log -- real history,
    not an estimate (unlike collection_value_growth above).
    """
    txs = db.query(Transaction).all()
    by_month: dict[str, dict[str, float]] = {}
    for tx in txs:
        label = tx.date.strftime("%Y-%m")
        bucket = by_month.setdefault(label, {"bought": 0.0, "sold": 0.0})
        if tx.type == "purchase":
            bucket["bought"] += tx.price + (tx.fees or 0.0)
        elif tx.type == "sale":
            bucket["sold"] += tx.price

    result = []
    cumulative_invested = 0.0
    for label in sorted(by_month):
        bucket = by_month[label]
        cumulative_invested += bucket["bought"] - bucket["sold"]
        result.append(
            {
                "label": label,
                "bought": bucket["bought"],
                "sold": bucket["sold"],
                "cumulative_invested": cumulative_invested,
            }
        )
    return result


def economic_summary(db: Session) -> dict:
    """Total real money in/out across every registered transaction, plus the
    "paper" gain/loss against today's collection value (unique_value, so
    duplicates don't inflate it) -- unrealized, since it compares a real
    amount paid against today's reference price, not a sale.
    """
    txs = db.query(Transaction).all()
    total_bought = sum(t.price + (t.fees or 0.0) for t in txs if t.type == "purchase")
    total_sold = sum(t.price for t in txs if t.type == "sale")
    net_invested = total_bought - total_sold
    return {
        "total_bought": total_bought,
        "total_sold": total_sold,
        "net_invested": net_invested,
    }
