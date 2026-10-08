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

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import case, func, literal, null, select
from sqlalchemy.orm import Session, selectinload

import constants
import pricing
from models import Card, CardPrice, CardSnapshot, FavoritePokemon, Listing, PokemonAlias, Set, Transaction
from models import listing_cards

# Series with no research done in set_release_order yet sort after every
# known series, not before -- mirrors app.py's UNKNOWN_RELEASE_RANK.
_UNKNOWN_RELEASE_RANK = 999999


def all_cards_with_collections(db: Session) -> list[Card]:
    """The one full-table load every breakdown below is built from. Each
    breakdown also accepts an already-loaded `cards` list (see their
    `cards=None` params) so a caller building several breakdowns in one
    request -- the dashboard route does all five -- only pays for this once.

    `Card.linked_set` is eager-loaded alongside `collections` because
    `by_series_breakdown` reads `card.linked_set.release_rank` for every
    card -- without this, that would be an N+1 lazy-load per card.
    """
    return db.query(Card).options(selectinload(Card.collections), selectinload(Card.linked_set)).all()


@dataclass
class Bucket:
    name: str
    qty: int = 0
    duplicates: int = 0
    unique_value: float = 0.0
    total_value: float = 0.0
    net_invested: float = 0.0
    # True once assign_bucket_investment() finds at least one card in this
    # bucket with a registered transaction. Without one there is no known
    # cost, so the dashboard shows "-" for Net invested/Gain/loss rather than
    # a "gain" equal to the bucket's whole value.
    has_investment: bool = False
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
    # Only populated for set-level buckets within `by_series_breakdown`'s
    # `child_sets` (see #142) -- the set's known card count from `Set.total_cards`
    # (via `set_sync.py`'s api.pokemontcg.io backfill), used to compute
    # `completion_pct`. None for every other kind of bucket (collection,
    # series, rarity, Pokemon), and also None for a set-level bucket whose
    # linked `Set` row has no `total_cards` yet (JP/KR sets the API doesn't
    # cover, or one `set_sync.py` just hasn't run against since import) --
    # the template must render an explicit "unknown" state for that case,
    # never a bare 0%/100%.
    total_cards: int | None = None
    # Set by assign_bucket_investment(): owned cards in the bucket with no
    # registered transaction, and their total value (all of it counts as
    # gain_loss, since their cost is unknown).
    no_cost_count: int = 0
    no_cost_value: float = 0.0

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
    def owned_numbers(self) -> int:
        """Distinct card numbers owned (qty > 0) in this bucket -- what
        completion counts. Not `unique_count`: that counts every Card row,
        so a reverse holo, a second language or a second variant of the same
        number would each count as one more card of the set and push
        completion past what's actually filled (a JP+KR 151 set had 274
        rows over 175 numbers).
        """
        return len({card.number for card in self.cards if card.qty > 0 and card.number})

    @property
    def completion_pct(self) -> float | None:
        """Percent of the set actually owned (`owned_numbers / total_cards`),
        or None when `total_cards` isn't known yet -- see `total_cards`'
        docstring above for why that happens and why the template must not
        collapse it to 0%/100%.
        """
        if not self.total_cards:
            return None
        return self.owned_numbers / self.total_cards * 100

    @property
    def series_completion(self) -> dict | None:
        """A series row's completion, aggregated over its child sets whose
        size is known: owned numbers / total cards across just those sets,
        plus how many of the series' sets that covers -- so a series with
        one synced set of five says so instead of passing that one set off
        as the whole series. None when no child set has a known size.
        """
        known = [b for b in self.child_sets if b.total_cards]
        if not known:
            return None
        owned = sum(b.owned_numbers for b in known)
        total = sum(b.total_cards for b in known)
        return {
            "pct": owned / total * 100,
            "owned": owned,
            "total": total,
            "sets_known": len(known),
            "sets": len(self.child_sets),
        }

    @property
    def gain_loss(self) -> float:
        """Total value (duplicates included) minus net invested -- the one
        gain definition used everywhere in the app, the same as the Market
        Value hero's (`gain_summary`). Net invested pays for every copy
        bought, so it's measured against every copy owned."""
        return self.total_value - self.net_invested

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


def collection_membership_breakdown(db: Session, cards: list[Card] | None = None) -> dict:
    """Dashboard Inventory table: one row per collection with *every* card
    carrying that collection's tag (real membership, the same set Inventory's
    collection filter shows), Bulk (no tag at all), and two deduplicated
    rows that count each card exactly once:

    - "collections": every card with at least one tag
    - "total": every card, Bulk included -- the whole collection

    A card with several tags is in several rows, so the per-collection rows
    can add up to more than "collections"/"total" -- by design. The
    primary-collection credit (`Card.primary_collection`, lowest
    `priority_rank`) is no longer used to pick a single row for a card here:
    it made a multi-tagged card vanish from every collection but one.
    """
    cards = all_cards_with_collections(db) if cards is None else cards

    children: dict[str, Bucket] = {}
    bulk = Bucket(name="Bulk", filterable=False)
    # Each card is visited once, so these two count it once whatever its tags.
    collections = Bucket(name="Collections")
    total = Bucket(name="Total", filterable=False)

    for card in cards:
        total.add(card)
        if not card.collections:
            bulk.add(card)
            continue
        collections.add(card)
        for collection in card.collections:
            children.setdefault(collection.name, Bucket(name=collection.name)).add(card)

    for bucket in list(children.values()) + [bulk]:
        bucket.cards.sort(key=_card_sort_key)

    return {
        "collections": collections,
        "children": _ordered_children(children.values()),
        "bulk": bulk,
        "total": total,
    }


def _ordered_children(buckets) -> list[Bucket]:
    """Dashboard display order for the collection breakdown -- distinct
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
    matches Inventory's default "release" sort. A series with no linked
    `Set` rows carrying a `release_rank` sorts after every known series.

    Each series bucket also carries `.child_sets` -- the sets within that
    series, for the dashboard's expandable per-series drill-down row --
    sorted the same way (release order, unresearched sets last).
    """
    cards = all_cards_with_collections(db) if cards is None else cards
    buckets: dict[str, Bucket] = {}
    set_buckets: dict[tuple[str, str], Bucket] = {}
    # Per-set rank read straight off each card's own `Set.id -> release_rank`
    # FK (same join app.py's Inventory "release" sort uses), keyed by the
    # same (series, set) pair the buckets above are built from -- an
    # in-Python dict over cards already loaded, rather than a second query
    # against the superseded `set_release_order` table (see issue #138).
    set_release_ranks: dict[tuple[str, str], int] = {}
    # Per-set known card count, read the same way as set_release_ranks above
    # (straight off each card's own `Set.total_cards` FK) -- unlike release
    # rank, ties aren't resolved with `min()`: a display-string collision
    # spanning more than one actual `Set` row is rare enough that "first
    # non-null value wins" is simpler and good enough (see #142).
    set_total_cards: dict[tuple[str, str], int] = {}
    for card in cards:
        series_key = card.series or "(uten serie)"
        bucket = buckets.setdefault(series_key, Bucket(name=series_key))
        bucket.add(card)

        set_key = card.set or "(uten sett)"
        set_bucket = set_buckets.setdefault((series_key, set_key), Bucket(name=set_key))
        set_bucket.add(card)

        linked_set = card.linked_set
        if linked_set is not None and linked_set.release_rank is not None:
            key = (series_key, set_key)
            set_release_ranks[key] = min(
                set_release_ranks.get(key, linked_set.release_rank), linked_set.release_rank
            )
        if linked_set is not None and linked_set.total_cards is not None:
            key = (series_key, set_key)
            set_total_cards.setdefault(key, linked_set.total_cards)

    # A series' own rank is its earliest set's rank.
    release_ranks: dict[str, int] = {}
    for (series_key, _set_key), rank in set_release_ranks.items():
        release_ranks[series_key] = min(release_ranks.get(series_key, rank), rank)

    for (series_key, set_key_), set_bucket in set_buckets.items():
        set_bucket.total_cards = set_total_cards.get((series_key, set_key_))
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


# Rarity order, low to high -- the one ranking every rarity sort/filter in
# the app uses (Dashboard's Rarity breakdown, Inventory's Rarity column,
# Transactions' Rarity column; see rarity_rank / rarity_sort_expr). Modern
# tiers first, then the older eras' "Holo Rare"/"Holo Rare V" and Triple
# Rare, then the Illustration Rare tiers and the gold/secret tiers above
# them. A name not in this list (another era's own rarity name) sorts
# alphabetically after all of these but before the "no real rarity"
# groups in _RARITY_TAIL -- so a new name never lands at the very bottom.
RARITY_ORDER = [
    "Common",
    "Uncommon",
    "Rare",
    "Double Rare",  # includes ACE SPEC
    "Ultra Rare",  # full-art ex cards
    "Amazing Rare",
    "Holo Rare",
    "Holo Rare V",
    "Triple Rare",
    "Illustration Rare",
    "Special Illustration Rare",
    "Hyper Rare",  # gold cards
    "Rainbow Rare",  # phased out, used in earlier sets
    "Black White Rare",  # set-specific gold-symbol variants, e.g. Trainer Gallery
    "Secret Rare",  # numbered beyond the set's main size
]
NO_RARITY_LABEL = "(uten rarity)"  # Dashboard bucket for cards with no rarity at all
# Always last, in this order, after any unrecognized name.
_RARITY_TAIL = ["No Rarity", NO_RARITY_LABEL, "Promo"]
_RARITY_RANKS = {name: i for i, name in enumerate(RARITY_ORDER)}
_UNKNOWN_RARITY_RANK = len(RARITY_ORDER)
_RARITY_RANKS.update({name: _UNKNOWN_RARITY_RANK + 1 + i for i, name in enumerate(_RARITY_TAIL)})


def rarity_rank(name: str | None) -> tuple[int, str]:
    """Sort key for a rarity name (None = no rarity): tier rank, then name
    (only breaks ties between unrecognized names)."""
    name = name or NO_RARITY_LABEL
    rank = _RARITY_RANKS.get(name, _UNKNOWN_RARITY_RANK)
    return (rank, name.lower() if rank == _UNKNOWN_RARITY_RANK else "")


def rarity_sort_expr(column):
    """SQL equivalent of rarity_rank's first element, for ORDER BY on a
    rarity column (pair it with the column itself for the name tie-break)."""
    return case(
        {name: rank for name, rank in _RARITY_RANKS.items() if name != NO_RARITY_LABEL},
        value=column,
        else_=case((column.is_(None), _RARITY_RANKS[NO_RARITY_LABEL]), else_=_UNKNOWN_RARITY_RANK),
    )


def by_rarity_breakdown(db: Session, cards: list[Card] | None = None) -> list[Bucket]:
    cards = all_cards_with_collections(db) if cards is None else cards
    buckets: dict[str, Bucket] = {}
    for card in cards:
        key = card.rarity or NO_RARITY_LABEL
        bucket = buckets.setdefault(key, Bucket(name=key))
        bucket.add(card)

    for bucket in buckets.values():
        bucket.cards.sort(key=_card_sort_key)

    return sorted(buckets.values(), key=lambda b: rarity_rank(b.name))


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
    """The "Most valuable cards" dashboard tile -- excludes qty == 0 cards
    (traded/sold away, but still present in the export) so a card no longer
    owned can't appear here as if it still were -- see issue #132, same
    reasoning as `Card.unique_value`'s qty gating.
    """
    return (
        db.query(Card)
        .filter(Card.market_price.isnot(None), Card.qty > 0)
        .order_by(Card.market_price.desc())
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


# Period buttons on the Market Value chart: key -> (button label, days back
# from today, None = everything). Same shorthand as a stock-portfolio app.
VALUE_HISTORY_PERIODS: dict[str, tuple[str, int | None]] = {
    "1w": ("1U", 7),
    "1m": ("1M", 30),
    "3m": ("3M", 91),
    "6m": ("6M", 182),
    "1y": ("1Å", 365),
    "all": ("Alt", None),
}

# Within one date, which snapshot source counts as that day's closing value:
# the price refresh (06:00 UTC) runs after the Dropbox sync (05:00 UTC), and
# a manual sync is the latest user-triggered state of the day.
_SNAPSHOT_SOURCE_ORDER = {"cron": 0, "price-cron": 1, "manual": 2}


def real_value_history(
    db: Session,
    metric: str = "unique",
    period: str = "all",
    today: dt.date | None = None,
    live: tuple[float, int] | None = None,
) -> list[dict]:
    """The real, non-approximated counterpart to collection_value_growth:
    the collection's value per day a snapshot was actually taken (see
    snapshots.record_daily_snapshot) -- what it was actually worth on date X,
    not today's price applied retroactively. Empty until at least one
    snapshot has accumulated (there is no way to backfill snapshots for
    dates before this table existed).

    One point per date -- that day's last snapshot (see
    _SNAPSHOT_SOURCE_ORDER), like a portfolio's daily close -- so the chart
    can use a real time axis. `live` = (value, card_count) of the collection
    right now, for `metric`: when given, today's point is that live figure
    (added if no snapshot exists yet today), so the chart always ends on the
    same number the Market Value key figures show. `period` (see
    VALUE_HISTORY_PERIODS) keeps only the points from that many days back.

    `metric` mirrors the VALUE_GROWTH_METRICS lambdas, summed in SQL over
    CardSnapshot's own qty/reference_price (GROUP BY date + source rather
    than pulling every row into Python -- this table grows with calendar
    time regardless of collection size, see issue #162). "unique" only
    counts cards owned that day (qty > 0), same as Card.unique_value.
    """
    if metric not in VALUE_GROWTH_METRICS:
        raise ValueError(f"unknown metric: {metric!r} (expected one of {sorted(VALUE_GROWTH_METRICS)})")
    if period not in VALUE_HISTORY_PERIODS:
        raise ValueError(f"unknown period: {period!r} (expected one of {sorted(VALUE_HISTORY_PERIODS)})")
    today = today or dt.date.today()

    price = func.coalesce(CardSnapshot.reference_price, 0.0)
    if metric == "unique":
        value_expr = func.sum(case((CardSnapshot.qty > 0, price), else_=0.0))
        count_expr = func.sum(case((CardSnapshot.qty > 0, 1), else_=0))
    elif metric == "duplicates":
        value_expr = func.sum(case((CardSnapshot.qty > 1, (CardSnapshot.qty - 1) * price), else_=0.0))
        count_expr = func.sum(case((CardSnapshot.qty > 1, CardSnapshot.qty - 1), else_=0))
    else:  # total
        value_expr = func.sum(CardSnapshot.qty * price)
        count_expr = func.sum(CardSnapshot.qty)

    rows = (
        db.query(
            CardSnapshot.date,
            CardSnapshot.source,
            value_expr.label("value"),
            count_expr.label("card_count"),
        )
        .group_by(CardSnapshot.date, CardSnapshot.source)
        .all()
    )

    by_date: dict[dt.date, tuple[float, int]] = {}
    for date, _source, value, card_count in sorted(
        rows, key=lambda row: (row[0], _SNAPSHOT_SOURCE_ORDER.get(row[1], len(_SNAPSHOT_SOURCE_ORDER)))
    ):
        by_date[date] = (value or 0.0, int(card_count or 0))  # later source wins

    if live is not None and by_date:
        by_date[today] = (live[0], int(live[1]))

    days = VALUE_HISTORY_PERIODS[period][1]
    start = today - dt.timedelta(days=days) if days is not None else None
    return [
        {"date": date, "label": date.strftime("%Y-%m-%d"), "cumulative_value": value, "card_count": count}
        for date, (value, count) in sorted(by_date.items())
        if start is None or date >= start
    ]


def card_price_history(db: Session, card_id: int) -> list[dict]:
    """One card's price and qty per snapshot day, oldest first -- the card
    detail page's price history. Same one-point-per-day rule as
    `real_value_history`: when a day has several sources, the latest in
    `_SNAPSHOT_SOURCE_ORDER` wins (the day's close).
    """
    rows = db.query(CardSnapshot).filter(CardSnapshot.card_id == card_id).all()
    rows.sort(key=lambda r: (r.date, _SNAPSHOT_SOURCE_ORDER.get(r.source, len(_SNAPSHOT_SOURCE_ORDER))))
    by_date: dict[dt.date, CardSnapshot] = {}
    for r in rows:
        by_date[r.date] = r  # later source wins
    legacy = None
    points = []
    prev_source = None
    for d, r in sorted(by_date.items()):
        source = r.price_source
        if source is None and r.reference_price is not None:
            # Pre-#210 snapshot (no price_source recorded): infer its source
            # the same way price_movers does, for switch detection only.
            if legacy is None:
                legacy = _legacy_price_source(db, card_id)
            source = legacy[card_id]
        points.append(
            {
                "date": d,
                "label": d.strftime("%Y-%m-%d"),
                "price": r.reference_price,
                "qty": r.qty,
                "source": r.price_source,  # as recorded; null before issue #210
                # Tooltip line where this point's price came from a different
                # source than the previous one's (issue #210), else None.
                "source_note": pricing.source_switch_note(prev_source, source) if points else None,
            }
        )
        prev_source = source
    return points


def _effective_snapshot_source(ptcg_card_id):
    """SQL counterpart of card_price_history's per-point source: the
    snapshot's recorded price_source, or for a pre-#210 snapshot with a price
    the _legacy_price_source guess (`ptcg_card_id` is the outer-joined id of
    the card's priced pokemontcg row, NULL when it has none)."""
    return case(
        (CardSnapshot.price_source.isnot(None), CardSnapshot.price_source),
        (CardSnapshot.reference_price.is_(None), null()),
        (ptcg_card_id.isnot(None), literal(pricing.SOURCE_POKEMONTCG)),
        else_=literal(pricing.SOURCE_DEX),
    )


def _snapshot_source_switches(db: Session, since: dt.date | None = None) -> dict[dt.date, dict[tuple[str, str], int]]:
    """date -> {(old source, new source): number of cards} for every snapshot
    day on which a card's price source differs from its own previous
    snapshot day's (each day's closing snapshot, _SNAPSHOT_SOURCE_ORDER).
    A card gaining or losing a price altogether isn't a switch.

    Done in SQL with window functions (ROW_NUMBER for the day's closing
    snapshot, LAG for the card's previous day), so only the handful of
    switch rows come back, never card_snapshots itself (#162).
    """
    ptcg = (
        select(CardPrice.card_id)
        .where(CardPrice.source == pricing.SOURCE_POKEMONTCG, CardPrice.price_nok.isnot(None))
        .subquery()
    )
    source_rank = case(_SNAPSHOT_SOURCE_ORDER, value=CardSnapshot.source, else_=len(_SNAPSHOT_SOURCE_ORDER))
    closing = (
        select(
            CardSnapshot.card_id,
            CardSnapshot.date,
            _effective_snapshot_source(ptcg.c.card_id).label("src"),
            func.row_number()
            .over(partition_by=(CardSnapshot.card_id, CardSnapshot.date), order_by=source_rank.desc())
            .label("rn"),
        )
        .select_from(CardSnapshot)
        .outerjoin(ptcg, ptcg.c.card_id == CardSnapshot.card_id)
        .subquery()
    )
    lagged = (
        select(
            closing.c.date,
            closing.c.src,
            func.lag(closing.c.src).over(partition_by=closing.c.card_id, order_by=closing.c.date).label("prev"),
        )
        .where(closing.c.rn == 1)
        .subquery()
    )
    q = (
        select(lagged.c.date, lagged.c.prev, lagged.c.src, func.count().label("n"))
        .where(lagged.c.prev.isnot(None), lagged.c.src.isnot(None), lagged.c.prev != lagged.c.src)
        .group_by(lagged.c.date, lagged.c.prev, lagged.c.src)
    )
    if since is not None:
        q = q.where(lagged.c.date >= since)
    result: dict[dt.date, dict[tuple[str, str], int]] = {}
    for date, prev, src, n in db.execute(q):
        if isinstance(date, str):  # SQLite through a subquery, just in case
            date = dt.date.fromisoformat(date)
        result.setdefault(date, {})[(prev, src)] = int(n)
    return result


def history_source_notes(
    db: Session, history: list[dict], live_cards: list[Card] | None = None, today: dt.date | None = None
) -> list[list[str]]:
    """Per real_value_history point, the tooltip lines for cards whose price
    source switched that day ("Source: A → B (N cards)", most cards first;
    issue #210) -- empty for most points. Charts deliberately keep those
    points (unlike Price movers), so the tooltip is what explains a step.

    When the last point is today's live value with no snapshot taken yet
    today, it's compared against `live_cards`' current sources instead.
    """
    if not history:
        return []
    today = today or dt.date.today()
    switches = _snapshot_source_switches(db, since=history[0]["date"])

    last = history[-1]["date"]
    if (
        live_cards is not None
        and last == today
        and today not in switches
        and db.query(CardSnapshot.id).filter(CardSnapshot.date == today).first() is None
    ):
        prev_date = db.query(func.max(CardSnapshot.date)).filter(CardSnapshot.date < today).scalar()
        if prev_date is not None:
            before = _snapshot_price_sources(db, prev_date, with_price=True)
            legacy = None
            counts: dict[tuple[str, str], int] = {}
            for card in live_cards:
                if card.id not in before:
                    continue
                old, had_price = before[card.id]
                if old is None and had_price:
                    if legacy is None:
                        legacy = _legacy_price_source(db)
                    old = legacy[card.id]
                new = card.market_price_source
                if old and new and old != new:
                    counts[(old, new)] = counts.get((old, new), 0) + 1
            if counts:
                switches[today] = counts

    notes = []
    for row in history:
        day = switches.get(row["date"], {})
        notes.append(
            [pricing.source_switch_note(old, new, n) for (old, new), n in sorted(day.items(), key=lambda kv: -kv[1])]
        )
    return notes


def collection_detail(db: Session, collection_id: int) -> dict | None:
    """Everything `/collections/{id}` shows: the collection's own bucket
    (every card with its tag, owned or not -- the template dims qty 0),
    its cards grouped per set with each set's completion within the
    collection, and which of its cards are also in other collections.
    None when no such collection exists.

    A collection has no size of its own (it's a tag, not a checklist), so
    completion is per set: distinct numbers from that set carried by this
    collection / the set's `total_cards`. `completion` sums that over the
    sets with a known size, same shape as `Bucket.series_completion`.
    """
    from models import Collection

    collection = (
        db.query(Collection)
        .options(selectinload(Collection.cards).selectinload(Card.collections), selectinload(Collection.cards).selectinload(Card.linked_set))
        .filter(Collection.id == collection_id)
        .one_or_none()
    )
    if collection is None:
        return None

    bucket = Bucket(name=collection.name)
    sets: dict[str, Bucket] = {}
    ranks: dict[str, int] = {}
    for card in collection.cards:
        bucket.add(card)
        key = card.set or "(no set)"
        set_bucket = sets.setdefault(key, Bucket(name=key))
        set_bucket.add(card)
        if card.linked_set is not None:
            if card.linked_set.total_cards is not None and set_bucket.total_cards is None:
                set_bucket.total_cards = card.linked_set.total_cards
            if card.linked_set.release_rank is not None:
                ranks[key] = min(ranks.get(key, card.linked_set.release_rank), card.linked_set.release_rank)
    bucket.cards.sort(key=_card_sort_key)
    for set_bucket in sets.values():
        set_bucket.cards.sort(key=lambda c: (c.number_int if c.number_int is not None else _UNKNOWN_RELEASE_RANK, c.name))
    bucket.child_sets = sorted(sets.values(), key=lambda b: (ranks.get(b.name, _UNKNOWN_RELEASE_RANK), b.name))

    return {
        "collection": collection,
        "bucket": bucket,
        "completion": bucket.series_completion,
        "shared_count": sum(1 for c in bucket.cards if len(c.collections) > 1),
    }


# Per metric: how many copies of a card with `qty` count toward it -- the
# per-card counterpart of the SQL expressions in real_value_history.
_METRIC_COPIES = {
    "unique": lambda qty: 1 if qty > 0 else 0,
    "duplicates": lambda qty: max(qty - 1, 0),
    "total": lambda qty: max(qty, 0),
}


def _snapshot_state(db: Session, date: dt.date) -> dict[int, tuple[int, float]]:
    """card_id -> (qty, price) as of `date`'s last snapshot (same source
    precedence as real_value_history's daily point)."""
    rows = db.query(CardSnapshot).filter(CardSnapshot.date == date).all()
    rows.sort(key=lambda r: _SNAPSHOT_SOURCE_ORDER.get(r.source, len(_SNAPSHOT_SOURCE_ORDER)))
    return {r.card_id: (r.qty, r.reference_price or 0.0) for r in rows}  # later source wins


def _snapshot_price_sources(db: Session, date: dt.date, with_price: bool = False) -> dict:
    """card_id -> the price source recorded in `date`'s last snapshot (same
    precedence as _snapshot_state). None for rows written before issue #210
    added CardSnapshot.price_source. With `with_price`, the value is
    (source, whether that snapshot had a price) instead."""
    rows = (
        db.query(CardSnapshot.card_id, CardSnapshot.source, CardSnapshot.price_source, CardSnapshot.reference_price)
        .filter(CardSnapshot.date == date)
        .all()
    )
    rows.sort(key=lambda r: _SNAPSHOT_SOURCE_ORDER.get(r.source, len(_SNAPSHOT_SOURCE_ORDER)))
    if with_price:
        return {r.card_id: (r.price_source, r.reference_price is not None) for r in rows}
    return {r.card_id: r.price_source for r in rows}  # later source wins


def _legacy_price_source(db: Session, card_id: int | None = None) -> dict[int, str]:
    """What a pre-#210 snapshot's price (price_source NULL) most likely came
    from. The old rule was "TCGplayer (pokemontcg.io) if the card had one,
    else Dex", and a pokemontcg price, once found, was never cleared -- so a
    card that has a pokemontcg price today almost certainly showed it then
    too. Only wrong for a card first priced by pokemontcg.io after the
    snapshot date, which then drops out of Price movers for one period
    rather than a real source switch showing up as a price move."""
    q = db.query(CardPrice.card_id).filter(
        CardPrice.source == pricing.SOURCE_POKEMONTCG, CardPrice.price_nok.isnot(None)
    )
    if card_id is not None:  # just the one card (the card page)
        q = q.filter(CardPrice.card_id == card_id)
    with_tcgplayer = {cid for (cid,) in q}
    return defaultdict(lambda: pricing.SOURCE_DEX, {cid: pricing.SOURCE_POKEMONTCG for cid in with_tcgplayer})


# Below this absolute change (kr, per copy) a price move's % is hidden: a
# 3 kr card going to 6 kr is "+100 %" but not news.
PRICE_MOVE_PCT_MIN_KR = 10.0


@dataclass
class PriceMove:
    card: Card
    old_price: float
    new_price: float

    @property
    def change(self) -> float:
        return self.new_price - self.old_price

    @property
    def pct(self) -> float:
        return self.change / self.old_price * 100

    @property
    def show_pct(self) -> bool:
        return abs(self.change) >= PRICE_MOVE_PCT_MIN_KR


def price_movers(
    db: Session, cards: list[Card], days: int = 30, limit: int = 3, today: dt.date | None = None
) -> dict | None:
    """The owned cards whose price rose and fell the most (in kr, per copy)
    since `days` ago, comparing that day's snapshot price with today's live
    `display_price`. Uses the latest snapshot day on or before the start of
    the period, or the earliest one there is when history is shorter; None
    when there is no earlier snapshot day at all.

    Ranked by kr, not %, so a 5 kr card doubling doesn't crowd out a real
    move. Only cards owned now (qty > 0) with a price on both days count.

    A card whose price *source* differs between the snapshot and today
    (e.g. Dex -> pokemontcg.io, pricing.py) is left out and counted in
    `n_source_changes` instead -- that's a switch, not a market move (issue
    #210). Snapshots from before price_source existed get their source
    inferred (_legacy_price_source); a card with no source on either side
    is never counted as a switch.
    """
    today = today or dt.date.today()
    start = today - dt.timedelta(days=days)
    since = (
        db.query(func.max(CardSnapshot.date)).filter(CardSnapshot.date <= start).scalar()
        or db.query(func.min(CardSnapshot.date)).filter(CardSnapshot.date < today).scalar()
    )
    if since is None:
        return None
    before = _snapshot_state(db, since)
    before_sources = _snapshot_price_sources(db, since)
    legacy_sources = None
    moves = []
    n_source_changes = 0
    for card in cards:
        old = before.get(card.id, (0, 0.0))[1]
        new = card.display_price or 0.0
        if not (card.qty > 0 and old > 0 and new > 0 and new != old):
            continue
        old_source = before_sources.get(card.id)
        if old_source is None:
            if legacy_sources is None:
                legacy_sources = _legacy_price_source(db)
            old_source = legacy_sources[card.id]
        new_source = card.market_price_source
        if old_source and new_source and old_source != new_source:
            n_source_changes += 1
            continue
        moves.append(PriceMove(card, old, new))
    return {
        "since": since,
        "days": (today - since).days,
        "up": sorted((m for m in moves if m.change > 0), key=lambda m: -m.change)[:limit],
        "down": sorted((m for m in moves if m.change < 0), key=lambda m: m.change)[:limit],
        "n_up": sum(1 for m in moves if m.change > 0),
        "n_down": sum(1 for m in moves if m.change < 0),
        "n_source_changes": n_source_changes,
    }


RECENTLY_ADDED_LIMIT = 10


def recently_added(cards: list[Card], limit: int = RECENTLY_ADDED_LIMIT) -> list[Card]:
    """The owned cards (qty > 0, same rule as price_movers) added most
    recently, newest first -- the Dashboard "Recently added" slide (issue
    #279). "Added" is `Card.created_at`, the same known added date the
    Orders card picker's `?pick=recent` filter uses: set once when the
    card is first imported. Cards from before that column existed
    (`created_at` is None) have no real added date and are left out rather
    than sorted to either end. Ties (one import adds many cards at the same
    instant) break on id, highest first, like the picker.
    """
    dated = [c for c in cards if c.qty > 0 and c.created_at is not None]
    dated.sort(key=lambda c: (c.created_at, c.id), reverse=True)
    return dated[:limit]


def value_change_breakdown(
    db: Session, metric: str, history: list[dict], live_cards: list[Card] | None = None
) -> dict | None:
    """Split a real_value_history period's change (first to last point)
    into what the cards' prices did and what adding/removing cards did:

        cards = sum over cards of (copies_last - copies_first) * price_last
        price = sum over cards of  copies_first * (price_last - price_first)

    which add up exactly to the period's change. So "price" is how the
    cards already owned at the start moved, "cards" is the value of copies
    gained (or lost) since, at today's prices. `card_delta` is the net
    change in copies counted by `metric`.

    `live_cards` (today's Card rows) stand in for the last point when it's
    today's live value -- see real_value_history's `live`. Only two days of
    per-card rows are read, so this stays cheap whatever the period length.
    None with fewer than 2 points.
    """
    if len(history) < 2:
        return None
    copies = _METRIC_COPIES[metric]
    first = _snapshot_state(db, history[0]["date"])
    if live_cards is not None:
        last = {c.id: (c.qty, c.display_price or 0.0) for c in live_cards}
    else:
        last = _snapshot_state(db, history[-1]["date"])

    price_effect = cards_effect = 0.0
    card_delta = 0
    for card_id in first.keys() | last.keys():
        q0, p0 = first.get(card_id, (0, 0.0))
        q1, p1 = last.get(card_id, (0, 0.0))
        c0, c1 = copies(q0), copies(q1)
        price_effect += c0 * (p1 - p0)
        cards_effect += (c1 - c0) * p1
        card_delta += c1 - c0
    return {"price": price_effect, "cards": cards_effect, "card_delta": card_delta}


def net_invested_at_dates(txs: list[Transaction], dates: list[dt.date]) -> list[float]:
    """Cumulative Net invested (economic_summary's rules: shipping included,
    sales counted at net proceeds) as of each of `dates` -- the "what had I
    paid by then" line drawn under the Market Value chart."""
    shares = shipping_shares(txs)
    amounts = []
    for tx in txs:
        if tx.type in CASH_TYPES:
            amounts.append((tx.date, net_invested_amount(tx, shares)))
    amounts.sort(key=lambda pair: pair[0])
    result, running, i = [], 0.0, 0
    for date in sorted(dates):
        while i < len(amounts) and amounts[i][0] <= date:
            running += amounts[i][1]
            i += 1
        result.append(running)
    return result


def period_change(history: list[dict]) -> dict | None:
    """First-to-last change across a real_value_history result: kr and %
    (None % when the period starts at 0). None with fewer than 2 points."""
    if len(history) < 2:
        return None
    first, last = history[0]["cumulative_value"], history[-1]["cumulative_value"]
    change = last - first
    return {"change": change, "pct": (change / first * 100) if first > 0 else None}


def cash_flow_by_month(db: Session) -> list[dict]:
    """Actual money in (purchase: price + fees + its shipping share) and
    money out (sale: net proceeds, see `net_proceeds`) per calendar month,
    straight from the Transaction log -- real history, not an estimate
    (unlike collection_value_growth above). Same rules as
    `economic_summary`, so the last month's `cumulative_invested` equals
    its `net_invested`.
    """
    txs = db.query(Transaction).all()
    shares = shipping_shares(txs)
    by_month: dict[str, dict[str, float]] = {}
    for tx in txs:
        label = tx.date.strftime("%Y-%m")
        bucket = by_month.setdefault(label, {"bought": 0.0, "sold": 0.0})
        if tx.type == "purchase":
            bucket["bought"] += net_invested_amount(tx, shares)
        elif tx.type == "sale":
            bucket["sold"] += net_proceeds(tx, shares)

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


def _purchase_shipping_total(txs: list[Transaction], types: tuple[str, ...] = ("purchase",)) -> float:
    """Sum of `purchase_shipping` across `txs`, counted once per order, over
    rows whose type is in `types` -- purchase rows by default; pass
    `("sale",)` for seller-paid shipping on sale orders (despite its name,
    `purchase_shipping` also carries a sale order's shipping, see
    `shipping_shares`). `economic_summary` itself builds on
    `shipping_shares`, whose per-order shares add up to the same totals.

    Every row sharing a `purchase_id` carries an identical copy
    of that order's shipping cost (see `app.py::create_purchase` -- the same
    value is written onto every row when the order is registered, it's not
    divided across cards), so naively summing `t.purchase_shipping` per row
    would multiply a single shipping charge by however many cards were in
    the lot. A row with no `purchase_id` (registered individually) counts its
    own shipping value on its own. Mirrors
    `app.py::_group_transactions_by_purchase`'s diff line, which also only
    ever subtracts shipping once per group.
    """
    total = 0.0
    seen_purchase_ids: set[int] = set()
    for t in txs:
        if t.type not in types or not t.purchase_shipping:
            continue
        if t.purchase_id is None:
            total += t.purchase_shipping
        elif t.purchase_id not in seen_purchase_ids:
            seen_purchase_ids.add(t.purchase_id)
            total += t.purchase_shipping
    return total


def economic_summary(db: Session, txs: list[Transaction] | None = None) -> dict:
    """Total real money in/out across every registered transaction, plus the
    "paper" gain/loss against today's collection value (unique_value, so
    duplicates don't inflate it) -- unrealized, since it compares a real
    amount paid against today's reference price, not a sale.

    Purchase-side spend includes `purchase_shipping` (see
    `_purchase_shipping_total`) alongside price + fees -- previously omitted
    here even though `_group_transactions_by_purchase`'s per-order diff line
    already accounted for it, understating the "Net invested"/"Paper
    gain/loss" KPIs whenever any order had shipping set.

    Sale-side money is net proceeds (`net_proceeds`: price - fees - the
    row's share of seller-paid shipping), not the gross price -- issue
    #254. Both sides are built from the same `shipping_shares`, so
    `sum(net_invested_by_card(...).values()) == net_invested` holds (up to
    float rounding).

    `txs`, when given, is a caller-supplied `Transaction.query.all()` result
    (e.g. `dashboard()` loading it once and threading it through both this
    and `net_invested_by_card` instead of each independently re-scanning the
    whole table -- same pattern as `all_cards_with_collections` for `cards`).
    """
    if txs is None:
        txs = db.query(Transaction).all()
    shares = shipping_shares(txs)
    total_bought = sum(net_invested_amount(t, shares) for t in txs if t.type == "purchase")
    total_sold = sum(net_proceeds(t, shares) for t in txs if t.type == "sale")
    net_invested = total_bought - total_sold
    return {
        "total_bought": total_bought,
        "total_sold": total_sold,
        "net_invested": net_invested,
    }


def gain_summary(cards: list[Card], invested_by_card: dict[int, float], net_invested: float) -> dict:
    """The collector's headline number: how far today's value of everything
    owned (duplicates included -- total_value, the same figure the Market
    Value hero shows) is above (or below) what was paid, plus what's behind
    it. Net invested pays for every copy bought, so it's measured against
    every copy owned, not just one per card. Same definition as every
    table's Gain/loss (`Bucket.gain_loss`).

    Owned cards with no registered transaction have no known cost, so their
    whole value lands in `gain` -- reported separately as `no_cost_count`/
    `no_cost_value` so the headline can say how much of the gain that is,
    rather than let it pass as profit.

    Per-card figures only cover owned cards (qty > 0) that have at least one
    registered transaction (i.e. appear in `invested_by_card`). Each is
    that card's total_value (all copies) minus what it cost. A ripped card
    (cost 0) counts as up by its full value. `n_with_cost` is how many cards
    that comparison covers.
    """
    unique_value = sum(c.unique_value for c in cards)
    total_value = sum(c.total_value for c in cards)
    gain = total_value - net_invested
    owned = [c for c in cards if c.qty > 0]
    no_cost = [c for c in owned if c.id not in invested_by_card]
    per_card = [(c, c.total_value - invested_by_card[c.id]) for c in owned if c.id in invested_by_card]
    per_card.sort(key=lambda pair: pair[1], reverse=True)
    return {
        "gain": gain,
        "pct": (gain / net_invested * 100) if net_invested > 0 else None,
        "unique_value": unique_value,
        "total_value": total_value,
        "net_invested": net_invested,
        "n_up": sum(1 for _, g in per_card if g > 0),
        "n_down": sum(1 for _, g in per_card if g < 0),
        "n_with_cost": len(per_card),
        "no_cost_count": len(no_cost),
        "no_cost_value": sum(c.total_value for c in no_cost),
        "best": per_card[0] if per_card and per_card[0][1] > 0 else None,
    }


def trade_prices_at(db: Session, trade_txs: list[Transaction]) -> dict[int, float | None]:
    """Each trade row's card price as of the trade date, keyed by
    transaction id: the latest `card_snapshots` row on or before that date
    (any source). None when the card has no snapshot that old -- e.g. a
    trade from before snapshots existed -- rather than guessing with
    today's price, which `trade_summary` already reports separately.
    """
    if not trade_txs:
        return {}
    card_ids = {t.card_id for t in trade_txs}
    latest = max(t.date for t in trade_txs)
    snaps = (
        db.query(CardSnapshot)
        .filter(CardSnapshot.card_id.in_(card_ids), CardSnapshot.date <= latest)
        .order_by(CardSnapshot.date)
        .all()
    )
    by_card: dict[int, list[CardSnapshot]] = {}
    for s in snaps:
        by_card.setdefault(s.card_id, []).append(s)
    result: dict[int, float | None] = {}
    for t in trade_txs:
        price = None
        for s in by_card.get(t.card_id, []):
            if s.date > t.date:
                break
            price = s.reference_price
        result[t.id] = price
    return result


def trade_summary(txs: list[Transaction], prices_then: dict[int, float | None] | None = None) -> dict | None:
    """What a trade gave vs. got, and whether it came out ahead.

    Works on an order's rows (only its type == "trade" rows count; None if
    it has none). `direction` splits them into gave ("out") and got ("in");
    trade rows recorded before `direction` existed land in `unknown` and are
    left out of the totals rather than guessed. On a trade row `price` is
    cash that moved with the card (paid on "in", received on "out"), so

        gain = value of cards got - value of cards gave + cash received - cash paid

    `*_now` uses each card's current `display_price`. `*_then` uses
    `prices_then` (see `trade_prices_at`) and is None unless every card on
    both sides has a price for the trade date. Trade cash stays out of
    `economic_summary` / Net invested, same as before `direction` existed.
    """
    trades = [t for t in txs if t.type == "trade"]
    if not trades:
        return None
    prices_then = prices_then or {}

    def side(rows):
        now = sum(t.card.display_price or 0.0 for t in rows)
        then_vals = [prices_then.get(t.id) for t in rows]
        then = sum(then_vals) if all(v is not None for v in then_vals) else None
        return now, then

    gave = [t for t in trades if t.direction == "out"]
    got = [t for t in trades if t.direction == "in"]
    unknown = [t for t in trades if t.direction not in ("in", "out")]
    out_now, out_then = side(gave)
    in_now, in_then = side(got)
    cash_paid = sum(t.price or 0.0 for t in got)
    cash_received = sum(t.price or 0.0 for t in gave)
    cash_net = cash_received - cash_paid
    return {
        "gave": gave,
        "got": got,
        "unknown": unknown,
        "value_out_now": out_now,
        "value_in_now": in_now,
        "cash_paid": cash_paid,
        "cash_received": cash_received,
        "gain_now": in_now - out_now + cash_net,
        "value_out_then": out_then,
        "value_in_then": in_then,
        "gain_then": (in_then - out_then + cash_net) if out_then is not None and in_then is not None else None,
        "prices_then": {t.id: prices_then.get(t.id) for t in trades},
    }


# Row types that bring a copy of a card into the collection -- what an
# order's Gain values (issue #246). A trade only counts on its "in" side.
_ACQUIRING_TYPES = ("purchase", "ripped")


def _is_acquisition(t: Transaction) -> bool:
    return t.type in _ACQUIRING_TYPES or (t.type == "trade" and t.direction == "in")


def held_acquisition_ids(txs: list[Transaction]) -> set[int]:
    """Ids of the acquisition rows (purchase, ripped, trade "in") whose copy
    is still owned, judged against each card's current `qty`.

    One transaction row is one copy (there's no quantity column). A card
    owned `qty` times with N acquisition rows has min(qty, N) of them still
    held; the newest rows (by date, then id) are the ones treated as held --
    i.e. disposals are assumed oldest-first (FIFO). So a card bought twice and
    later sold once leaves only the later order credited, and a qty=0 card
    (sold/traded away) leaves none. `txs` must be every transaction, not one
    order's, since a card's copies can be spread across several orders.
    """
    by_card: dict[int, list[Transaction]] = defaultdict(list)
    for t in txs:
        if _is_acquisition(t):
            by_card[t.card_id].append(t)
    held: set[int] = set()
    for rows in by_card.values():
        qty = max(rows[0].card.qty or 0, 0)
        rows.sort(key=lambda t: (t.date, t.id), reverse=True)
        held.update(t.id for t in rows[:qty])
    return held


def order_gain(
    group_txs: list[Transaction], order_total: float, held_ids: set[int], trade: dict | None
) -> dict:
    """An order's paper gain/loss for Order history's Gain column (issue
    #246): today's market value of the copies from this order that are still
    owned, minus what the order cost (`order_total` -- its typed Total, else
    the automatic Value + Shipping, i.e. exactly what the Total column shows).

    Same idea as the headline "Paper gain/loss" (today's value of what you
    own minus what you paid), scoped to one order:

      - purchase / ripped rows: each still-held copy (see
        `held_acquisition_ids`) adds its card's `display_price`. A copy that
        has since been sold or traded away adds nothing, so its cost shows as
        a loss here and its proceeds wherever it was sold.
      - trade rows: the order's `trade_summary` gain_now (got - gave + cash),
        the same figure the expanded order's "Trade gain" shows. Its rows
        aren't valued again on top of that.
      - sale rows: a pure sale order has nothing still held to value -- gain
        is None ("—"). An order mixing sale rows with purchases is None too:
        its Value/Total add sale proceeds and purchase prices together, so
        there's no meaningful cost to subtract.

    `gain` is None when nothing in the order can be valued: no held copy has
    a market price and there's no trade figure, or the order has no
    purchase/ripped rows and no trade with In/Out set. When only some held copies
    lack a price they count as 0 (as in the headline) and `unpriced` says
    how many, so the template can flag the figure as partial.
    """
    types = {t.type for t in group_txs}
    if "sale" in types:
        return {"gain": None, "unpriced": 0, "reason": "sale"}
    held = [t for t in group_txs if t.type in _ACQUIRING_TYPES and t.id in held_ids]
    priced = [t for t in held if t.card.display_price is not None]
    unpriced = len(held) - len(priced)
    # A trade whose rows all lack In/Out has no gain figure (trade_summary
    # leaves them out rather than guessing) -- don't let its 0 pass as one.
    trade_known = trade is not None and bool(trade["gave"] or trade["got"])
    if not trade_known and held and not priced:
        return {"gain": None, "unpriced": unpriced, "reason": "no_price"}
    if not trade_known and not (types & set(_ACQUIRING_TYPES)):
        return {"gain": None, "unpriced": 0, "reason": "empty"}
    gain = sum(t.card.display_price for t in priced) - order_total
    if trade_known:
        gain += trade["gain_now"]
    return {"gain": gain, "unpriced": unpriced, "reason": None}


CASH_TYPES = ("purchase", "sale")


def shipping_shares(txs: list[Transaction]) -> dict[int, float]:
    """Each purchase or sale row's share of its order's shipping, keyed by
    transaction id. On a purchase, shipping is part of what a card actually
    cost; on a sale it's seller-paid shipping that comes off the proceeds
    (issue #254). Either way it pushes Net invested up.

    Despite the column name, `purchase_shipping` carries the shipping of
    any order, purchase or sale (Mark sold and the New Order cart with type
    Sale write it there too). No separate column and no rename on purpose:
    init_db() is additive-only.

    An order's `purchase_shipping` (stored redundantly on every row, see
    `_purchase_shipping_total`) is split across the order's purchase- and
    sale-type rows together (trade/ripped rows get none) in proportion to
    their `price`, so a 100 kr card carries more of it than a 5 kr card.
    When none of them has a price yet (price 0 = not priced, e.g. before
    "Distribute remaining" has run) it's split evenly instead; when some
    are priced, a 0-price row gets no share. A row with no `purchase_id`
    carries its own shipping in full. Shares are unrounded floats, so an
    order's shares always add up to its shipping (no cent remainder to
    assign) and totals built from them match `economic_summary`.
    """
    shares: dict[int, float] = {}
    groups: dict[int, list[Transaction]] = {}
    for t in txs:
        if t.type not in CASH_TYPES:
            continue
        if t.purchase_id is None:
            if t.purchase_shipping:
                shares[t.id] = t.purchase_shipping
        else:
            groups.setdefault(t.purchase_id, []).append(t)
    for rows in groups.values():
        shipping = next((t.purchase_shipping for t in rows if t.purchase_shipping), None)
        if not shipping:
            continue
        price_sum = sum(t.price or 0.0 for t in rows)
        for t in rows:
            weight = (t.price or 0.0) / price_sum if price_sum > 0 else 1 / len(rows)
            shares[t.id] = shipping * weight
    return shares


def net_proceeds(tx: Transaction, shares: dict[int, float]) -> float:
    """What a sale row actually brought in: price - fees - its share of the
    order's seller-paid shipping (`shares` from `shipping_shares`)."""
    return tx.price - (tx.fees or 0.0) - shares.get(tx.id, 0.0)


def net_invested_amount(tx: Transaction, shares: dict[int, float]) -> float:
    """A row's signed contribution to Net invested: a purchase adds price +
    fees + its shipping share, a sale subtracts its `net_proceeds`, any
    other type (trade, ripped) adds 0. The one rule every Net invested
    figure (`economic_summary`, `net_invested_by_card`,
    `net_invested_at_dates`, `cash_flow_by_month`) is built from."""
    if tx.type == "purchase":
        return tx.price + (tx.fees or 0.0) + shares.get(tx.id, 0.0)
    if tx.type == "sale":
        return -net_proceeds(tx, shares)
    return 0.0


@dataclass
class RealizedGain:
    """One sale row's realized gain (issue #256), from `realized_gains`.

    `proceeds` is the row's `net_proceeds`. `source` is the acquisition row
    whose copy this sale used up (None when there was none to match).
    `cost` is what that copy cost, None when unknown -- `reason` then says
    why: "no_acquisition" (no recorded copy bought on or before the sale
    date), "unpriced" (a purchase still at price 0, i.e. not priced yet) or
    "trade" (traded in: it cost the cards given away, not a cash price).
    `gain` is proceeds - cost, None whenever cost is.
    """

    proceeds: float
    source: Transaction | None
    cost: float | None
    reason: str | None

    @property
    def gain(self) -> float | None:
        return None if self.cost is None else self.proceeds - self.cost


def _acquisition_cost(t: Transaction, shares: dict[int, float]) -> tuple[float | None, str | None]:
    """What one acquired copy cost, as `(cost, reason-if-unknown)`. A
    purchase costs what it adds to Net invested (price + fees + its shipping
    share, `net_invested_amount`); a ripped card costs 0 (the pack isn't a
    card row); a traded-in card has no cash cost to use."""
    if t.type == "ripped":
        return 0.0, None
    if t.type == "purchase":
        if not t.price:
            return None, "unpriced"
        return net_invested_amount(t, shares), None
    return None, "trade"


def realized_gains(txs: list[Transaction], shares: dict[int, float]) -> dict[int, RealizedGain]:
    """Realized gain per sale row, keyed by sale transaction id (issue #256).
    Computed on every page load from the rows, never stored.

    Each disposal (a sale, or a trade "out") uses up the oldest acquired
    copy of that card (purchase, ripped, trade "in"; by date, then id) that
    no earlier disposal has used and that was acquired on or before the
    disposal's date -- FIFO, the same convention as `held_acquisition_ids`,
    which treats the newest copies as the ones still owned. So the copy a
    sale is costed against here is the one its purchase order no longer
    counts as held. Trade "out" rows consume a copy too (they dispose of
    one) but get no entry, since only sales realize cash.

    `txs` must be every transaction (a card's copies can sit in several
    orders); `shares` is `shipping_shares(txs)`.
    """
    by_card: dict[int, list[Transaction]] = defaultdict(list)
    for t in txs:
        if _is_acquisition(t) or t.type == "sale" or (t.type == "trade" and t.direction == "out"):
            by_card[t.card_id].append(t)
    result: dict[int, RealizedGain] = {}
    for rows in by_card.values():
        rows.sort(key=lambda t: (t.date, t.id))
        acquisitions = [t for t in rows if _is_acquisition(t)]
        next_free = 0
        for t in rows:
            if _is_acquisition(t):
                continue
            source = None
            if next_free < len(acquisitions) and acquisitions[next_free].date <= t.date:
                source = acquisitions[next_free]
                next_free += 1
            if t.type != "sale":
                continue
            cost, reason = _acquisition_cost(source, shares) if source else (None, "no_acquisition")
            result[t.id] = RealizedGain(net_proceeds(t, shares), source, cost, reason)
    return result


def sum_realized(gains: list[RealizedGain]) -> dict:
    """Totals over some sale rows' `RealizedGain`s: the known cost basis and
    gain, plus how many rows have an unknown cost (left out of both rather
    than counting their full proceeds as gain). `gain` is None when no row
    has a known cost."""
    known = [g for g in gains if g.cost is not None]
    return {
        "cost": sum(g.cost for g in known),
        "gain": sum(g.gain for g in known) if known else None,
        "unknown": len(gains) - len(known),
    }


def net_invested_by_card(db: Session, txs: list[Transaction] | None = None) -> dict[int, float]:
    """Return actual net investment per card using economic-summary rules.

    Like `economic_summary`, this includes `purchase_shipping`: each
    purchase row carries its share of its order's shipping (see
    `shipping_shares` -- split by price across the order's cards), so a
    card's figure is what it really cost to get it home. A sale counts at
    its net proceeds (`net_proceeds`), and
    `sum(net_invested_by_card(db).values())` still equals
    `economic_summary`'s `net_invested`.

    `txs`, when given, is a caller-supplied `Transaction.query.all()` result
    -- see `economic_summary`'s docstring for why (avoids a second full-table
    scan in the same request).
    """
    if txs is None:
        txs = db.query(Transaction).all()
    shares = shipping_shares(txs)
    invested: dict[int, float] = {}
    for tx in txs:
        invested[tx.card_id] = invested.get(tx.card_id, 0.0) + net_invested_amount(tx, shares)
    return invested


def assign_bucket_investment(buckets, invested_by_card: dict[int, float]) -> None:
    """Attach transaction totals to buckets without changing value rules.

    Also how much of each bucket's value is owned cards with no registered
    transaction (`no_cost_count`/`no_cost_value`) -- that value is all
    "gain" in `Bucket.gain_loss`, same as in `gain_summary`, so the UI can
    say so next to it.
    """
    for bucket in buckets:
        bucket.net_invested = sum(invested_by_card.get(card.id, 0.0) for card in bucket.cards)
        bucket.has_investment = any(card.id in invested_by_card for card in bucket.cards)
        no_cost = [c for c in bucket.cards if c.qty > 0 and c.id not in invested_by_card]
        bucket.no_cost_count = len(no_cost)
        bucket.no_cost_value = sum(c.total_value for c in no_cost)


@dataclass
class ListingCardPricing:
    """One card within a `Listing`, annotated with the prices the /listings
    page compares side by side -- see the "Sales listings (finn.no)"
    business rule in README.md. `listed_price` is the listing's own
    `suggested_price` (one price per listing, not per card, since a listing
    covers a lot rather than pricing each card in it separately).
    `sold_price` (issue #127) is per-card, unlike `listed_price` -- it's the
    real price this specific card's `Transaction(type="sale", listing_id=...)`
    row was recorded at via `POST /listings/{id}/mark-sold`; None until the
    listing is actually marked sold.
    """

    card: Card
    cost: float
    market_price: float | None
    listed_price: float | None
    sold_price: float | None = None


@dataclass
class ListingOverview:
    listing: Listing
    card_rows: list[ListingCardPricing]


def _sold_prices_by_listing_card(db: Session, listing_ids: list[int]) -> dict[tuple[int, int], float]:
    """`{(listing_id, card_id): price}` from every sale `Transaction` linked
    to one of `listing_ids` (issue #127's `Transaction.listing_id`). Summed
    per (listing, card) rather than assumed-unique -- mark-sold itself only
    ever writes one such row per card, but this stays correct even if that
    ever changes (e.g. a manually added second sale row for the same card).
    """
    if not listing_ids:
        return {}
    rows = (
        db.query(Transaction.listing_id, Transaction.card_id, Transaction.price)
        .filter(Transaction.listing_id.in_(listing_ids), Transaction.type == "sale")
        .all()
    )
    result: dict[tuple[int, int], float] = {}
    for listing_id, card_id, price in rows:
        key = (listing_id, card_id)
        result[key] = result.get(key, 0.0) + price
    return result


def _build_listing_overview(
    listing: Listing,
    invested_by_card: dict[int, float],
    sold_prices: dict[tuple[int, int], float] | None = None,
) -> ListingOverview:
    sold_prices = sold_prices or {}
    card_rows = [
        ListingCardPricing(
            card=card,
            cost=invested_by_card.get(card.id, 0.0),
            market_price=card.display_price,
            listed_price=listing.suggested_price,
            sold_price=sold_prices.get((listing.id, card.id)),
        )
        for card in listing.cards
    ]
    return ListingOverview(listing=listing, card_rows=card_rows)


def listing_overview(
    db: Session, include_delisted: bool = False, sold_only: bool = False
) -> list[ListingOverview]:
    """Every `Listing`, newest first, with each of its cards annotated with
    cost (actual money spent, from `net_invested_by_card` -- the same
    Transaction-derived figure used everywhere else, not a second "cost"
    concept), market price (`Card.display_price`), listed price
    (`Listing.suggested_price`), and -- once marked sold -- the real
    per-card sold price (`_sold_prices_by_listing_card`, issue #127). Never
    touches `qty`, `card_collections`, or `binder_id` -- see README's "Sales
    listings (finn.no)" business rule.

    Excludes `status == "delisted"` listings by default -- `/listings`' "Show
    delisted" toggle passes `include_delisted=True` to include them.
    `sold_only=True` (the "Sold" filter option, issue #127) further narrows
    to `status == "sold"` regardless of `include_delisted` -- a sold listing
    is never also delisted in practice, but this keeps the two filters
    independent rather than assuming that.
    """
    invested_by_card = net_invested_by_card(db)
    query = db.query(Listing).options(selectinload(Listing.cards)).order_by(Listing.created_at.desc())
    if not include_delisted:
        query = query.filter(Listing.status != "delisted")
    if sold_only:
        query = query.filter(Listing.status == "sold")
    listings = query.all()
    sold_prices = _sold_prices_by_listing_card(db, [listing.id for listing in listings])
    return [_build_listing_overview(listing, invested_by_card, sold_prices) for listing in listings]


def listing_entry(db: Session, listing_id: int) -> ListingOverview | None:
    """Single-listing counterpart to `listing_overview`, used to re-render
    one row after an htmx action (e.g. delisting, marking sold) without
    recomputing pricing for every listing. Returns None if the listing no
    longer exists.
    """
    listing = (
        db.query(Listing).options(selectinload(Listing.cards)).filter(Listing.id == listing_id).first()
    )
    if listing is None:
        return None
    invested_by_card = net_invested_by_card(db)
    sold_prices = _sold_prices_by_listing_card(db, [listing_id])
    return _build_listing_overview(listing, invested_by_card, sold_prices)


def active_listings_by_card(db: Session, card_ids: list[int] | None = None) -> dict[int, list[int]]:
    """card id -> ids of every `status == "active"` `Listing` it's in,
    newest first -- feeds the "Listed" badge on Inventory and `/sales`
    (issue #257). One query over `listing_cards` joined to `listings`, run
    once per request, never per row. Delisted and sold listings don't count.
    `card_ids` optionally narrows the lookup (e.g. `/sales`' selection);
    None means every card. Read-only -- nothing here touches `qty`,
    `card_collections`, or `binder_id`.
    """
    query = (
        select(listing_cards.c.card_id, Listing.id)
        .join(Listing, Listing.id == listing_cards.c.listing_id)
        .where(Listing.status == "active")
        .order_by(Listing.created_at.desc(), Listing.id.desc())
    )
    if card_ids is not None:
        if not card_ids:
            return {}
        query = query.where(listing_cards.c.card_id.in_(card_ids))
    result: dict[int, list[int]] = {}
    for card_id, listing_id in db.execute(query):
        result.setdefault(card_id, []).append(listing_id)
    return result


@dataclass
class UnlinkedSetCards:
    series: str | None
    set: str | None
    card_count: int


def unlinked_set_cards(db: Session) -> list[UnlinkedSetCards]:
    """Distinct (series, set) pairs among cards with no `Card.set_id`
    linked to a real `Set` row -- surfaces drift/gaps (e.g. a Dex rename
    db.py's `_backfill_sets()` hasn't caught up with yet, or a card
    imported before the FK existed and not yet re-synced) instead of
    letting it only silently fall back to `UNKNOWN_RELEASE_RANK` in the
    Inventory "release order" sort (see app.py, and issue #133). In
    practice `_backfill_sets()` runs on every app startup and links every
    card that has a non-null `series`/`set`, so a non-empty result here
    means either the app hasn't restarted since the last import, or a card
    has a null `series`/`set` to begin with (nothing to link it to).
    Grouped with a count rather than one row per card -- the useful signal
    is which sets are missing a link, not the individual cards.
    """
    rows = (
        db.query(Card.series, Card.set, func.count(Card.id))
        .filter(Card.set_id.is_(None))
        .group_by(Card.series, Card.set)
        .order_by(func.count(Card.id).desc())
        .all()
    )
    return [
        UnlinkedSetCards(series=series, set=set_name, card_count=count)
        for series, set_name, count in rows
    ]


@dataclass
class SetMissingReleaseRank:
    series: str
    name: str
    card_count: int


def sets_missing_release_rank(db: Session) -> list[SetMissingReleaseRank]:
    """`Set` rows with no `release_rank` yet -- either `set_sync.py`'s
    api.pokemontcg.io backfill never matched them (a JP/KR set the API
    doesn't cover yet, or a name/series mismatch that didn't clear
    `set_sync._match`'s confidence bar -- never guessed, see that module's
    docstring for why), or a set with no rank researched by hand either.
    Grouped with each set's own card count (via the `Card.set_id` FK, not a
    string join) so it's obvious which gaps are worth a manual look and
    which are empty/low-stakes -- same "make the gap visible instead of
    silently falling back" idea as `unlinked_set_cards()` above, just for
    `release_rank` instead of the FK link itself.
    """
    rows = (
        db.query(Set.series, Set.name, func.count(Card.id))
        .outerjoin(Card, Card.set_id == Set.id)
        .filter(Set.release_rank.is_(None))
        .group_by(Set.series, Set.name)
        .order_by(func.count(Card.id).desc())
        .all()
    )
    return [
        SetMissingReleaseRank(series=series, name=name, card_count=count)
        for series, name, count in rows
    ]


# --------------------------------------------------------------------------
# Master sets (issue #369): a set checklist (set_checklists.py, #368) against
# the cards linked to its prints. Everything here is computed on every call;
# owned / missing / spares are never stored.
# --------------------------------------------------------------------------
# Track display order and labels; set_checklists.TRACKS says which count.
MASTER_SET_TRACK_LABELS = {
    "main": "Main",
    "secret": "Secret",
    "poke_ball": "Poké Ball",
    "master_ball": "Master Ball",
}
_TRACK_RANK = {t: i for i, t in enumerate(MASTER_SET_TRACK_LABELS)}


@dataclass
class MasterSetSlot:
    """One checklist print and the owned cards linked to its master card."""

    master: object  # models.MasterCard
    track: str
    counts: bool
    cards: list  # owned (qty > 0) Card rows linked to the master, most copies first
    number_label: str  # "#023", padded to the checklist's widest number

    @property
    def owned_qty(self) -> int:
        return sum(c.qty for c in self.cards)

    @property
    def owned(self) -> bool:
        return self.owned_qty > 0

    @property
    def card(self):
        """The Card the slot's tile opens (the one with most copies), or None."""
        return self.cards[0] if self.cards else None

    @property
    def spares(self) -> int:
        """Every copy beyond the first of this print, counted per master
        card (epic #366, decision 4): two Dex rows on one print still keep
        only one copy for the master set."""
        return max(self.owned_qty - 1, 0)

    @property
    def price(self) -> float | None:
        return next((c.display_price for c in self.cards if c.display_price is not None), None)

    @property
    def spare_value(self) -> float:
        return self.spares * (self.price or 0.0)

    @property
    def track_label(self) -> str:
        return MASTER_SET_TRACK_LABELS.get(self.track, self.track)

    @property
    def name(self) -> str:
        card = self.card
        return (card.name if card is not None else None) or self.master.name or ""

    @property
    def image_url(self) -> str | None:
        card = self.card
        return (card.image_url if card is not None else None) or self.master.image_url


@dataclass
class MasterSetTrack:
    key: str
    label: str
    owned: int
    total: int
    counts: bool

    @property
    def pct(self) -> float:
        return self.owned / self.total * 100 if self.total else 0.0


@dataclass
class MasterSetDetail:
    checklist: object  # models.SetChecklist
    slots: list[MasterSetSlot]
    tracks: list[MasterSetTrack]  # only tracks with at least one print
    master_set: MasterSetTrack  # every print that counts toward completion
    unmatched: list  # owned Card rows of the set on no checklist print
    korean_proxy: bool  # owned cards are Korean, priced from the Japanese print

    @property
    def spare_slots(self) -> list[MasterSetSlot]:
        return [s for s in self.slots if s.spares]

    @property
    def spares(self) -> int:
        return sum(s.spares for s in self.slots)

    @property
    def spare_value(self) -> float:
        return sum(s.spare_value for s in self.slots)

    @property
    def missing(self) -> list[MasterSetSlot]:
        """Missing prints that count toward the master set, printed order."""
        return [s for s in self.slots if s.counts and not s.owned]


def _number_sort_key(number: str | None):
    text = (number or "").strip()
    return (0, int(text), "") if text.isdigit() else (1, 0, text)


def master_set_detail(db: Session, language: str, set_code: str) -> MasterSetDetail | None:
    """A set checklist with what's owned, missing and spare, or None when
    the masterdata (language, set_code) has no checklist.

    - A print is owned when the Cards linked to its master card add up to
      qty >= 1. Each track's X/Y is over checklist prints, so it can't pass
      100%; the master set is every print with `counts_toward_completion`
      (Master Ball prints are listed but never count).
    - Spares per print = max(sum(qty) - 1, 0), on any track.
    - Unmatched = owned cards of this (language, set_code) linked to no
      checklist print: linked to another master card of the set, or not
      linked at all (then matched on their Dex card_id).
    """
    import masterdata
    import set_checklists
    from models import MasterCard

    checklist = set_checklists.get_checklist(db, language, set_code)
    if checklist is None:
        return None

    width = max(
        (len(m.master_card.number) for m in checklist.cards if (m.master_card.number or "").isdigit()), default=0
    )

    def label(number: str | None) -> str:
        text = (number or "").strip()
        return "#" + (text.zfill(width) if text.isdigit() else text)

    slots = []
    member_ids = set()
    for member in checklist.cards:
        master = member.master_card
        member_ids.add(master.id)
        owned_cards = sorted((c for c in master.cards if c.qty > 0), key=lambda c: (-c.qty, c.id))
        slots.append(
            MasterSetSlot(
                master=master,
                track=member.track,
                counts=member.counts_toward_completion,
                cards=owned_cards,
                number_label=label(master.number),
            )
        )
    slots.sort(key=lambda s: (_number_sort_key(s.master.number), _TRACK_RANK.get(s.track, len(_TRACK_RANK))))

    tracks = []
    for key, track_label in MASTER_SET_TRACK_LABELS.items():
        in_track = [s for s in slots if s.track == key]
        if in_track:
            tracks.append(
                MasterSetTrack(
                    key=key,
                    label=track_label,
                    owned=sum(1 for s in in_track if s.owned),
                    total=len(in_track),
                    counts=set_checklists.TRACKS.get(key, False),
                )
            )
    counting = [s for s in slots if s.counts]
    master_set = MasterSetTrack(
        key="master_set",
        label="Master set",
        owned=sum(1 for s in counting if s.owned),
        total=len(counting),
        counts=True,
    )

    # Owned cards of this set that land on no checklist print.
    off_list = (
        db.query(Card)
        .join(MasterCard, Card.master_card_id == MasterCard.id)
        .filter(MasterCard.language == language, MasterCard.set_code == set_code, Card.qty > 0)
    )
    if member_ids:
        off_list = off_list.filter(~MasterCard.id.in_(member_ids))
    unmatched = off_list.all()
    unlinked = db.query(Card).filter(
        Card.master_card_id.is_(None), Card.qty > 0, Card.card_id.like(f"%{set_code}-%")
    )
    unmatched += [
        c for c in unlinked if (masterdata.parse_dex_card_id(c.card_id) or (None, None))[:2] == (language, set_code)
    ]
    unmatched.sort(key=lambda c: (c.number_int if c.number_int is not None else 10**9, c.card_id, c.variant or ""))

    owned_cards = [c for s in slots for c in s.cards]
    return MasterSetDetail(
        checklist=checklist,
        slots=slots,
        tracks=tracks,
        master_set=master_set,
        unmatched=unmatched,
        korean_proxy=any(pricing.is_jp_price_proxy(c) for c in owned_cards),
    )


def tracked_sets(db: Session) -> list[MasterSetDetail]:
    """Every set with a checklist, by display name (the Sets & lists
    landing page)."""
    from models import SetChecklist

    keys = db.query(SetChecklist.language, SetChecklist.set_code).order_by(SetChecklist.display_name).all()
    return [d for d in (master_set_detail(db, lang, code) for lang, code in keys) if d is not None]


def checklist_keys(db: Session) -> set[tuple[str, str]]:
    """(language, set_code) of every set with a checklist: decides where a
    "Master set" link shows."""
    from models import SetChecklist

    return {(lang, code) for lang, code in db.query(SetChecklist.language, SetChecklist.set_code)}


def card_set_key(card: Card) -> tuple[str, str] | None:
    """A card's masterdata (language, set_code), parsed from its Dex
    card_id the same way its master card's key is (masterdata.master_key_for),
    so no query per card."""
    import masterdata

    parsed = masterdata.parse_dex_card_id(card.card_id)
    return parsed[:2] if parsed else None
