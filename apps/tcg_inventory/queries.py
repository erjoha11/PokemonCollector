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
from dataclasses import dataclass, field

from sqlalchemy import case, func
from sqlalchemy.orm import Session, selectinload

import constants
from models import Card, CardSnapshot, FavoritePokemon, Listing, PokemonAlias, Set, Transaction

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
    def completion_pct(self) -> float | None:
        """Percent of the set actually owned (`unique_count / total_cards`),
        or None when `total_cards` isn't known yet -- see `total_cards`'
        docstring above for why that happens and why the template must not
        collapse it to 0%/100%. `unique_count` is already qty>0-gated, so
        this is unaffected by #132 (a separate bug about `Card.unique_value`
        not being qty-gated).
        """
        if not self.total_cards:
            return None
        return self.unique_count / self.total_cards * 100

    @property
    def gain_loss(self) -> float:
        return self.unique_value - self.net_invested

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
    display_price = func.coalesce(Card.tcgplayer_price, Card.reference_price)
    return (
        db.query(Card)
        .filter(display_price.isnot(None), Card.qty > 0)
        .order_by(display_price.desc())
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
    """Cumulative Net invested (economic_summary's rules, shipping included)
    as of each of `dates` -- the "what had I paid by then" line drawn under
    the Market Value chart."""
    shares = shipping_shares(txs)
    amounts = []
    for tx in txs:
        if tx.type == "purchase":
            amounts.append((tx.date, tx.price + (tx.fees or 0.0) + shares.get(tx.id, 0.0)))
        elif tx.type == "sale":
            amounts.append((tx.date, -tx.price))
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


def _purchase_shipping_total(txs: list[Transaction]) -> float:
    """Sum of `purchase_shipping` across `txs`, counted once per order.

    Every purchase-type row sharing a `purchase_id` carries an identical copy
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
        if t.type != "purchase" or not t.purchase_shipping:
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

    `txs`, when given, is a caller-supplied `Transaction.query.all()` result
    (e.g. `dashboard()` loading it once and threading it through both this
    and `net_invested_by_card` instead of each independently re-scanning the
    whole table -- same pattern as `all_cards_with_collections` for `cards`).
    """
    if txs is None:
        txs = db.query(Transaction).all()
    total_bought = sum(t.price + (t.fees or 0.0) for t in txs if t.type == "purchase") + _purchase_shipping_total(txs)
    total_sold = sum(t.price for t in txs if t.type == "sale")
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
    every copy owned, not just one per card.

    Per-card figures only cover owned cards (qty > 0) that have at least one
    registered transaction (i.e. appear in `invested_by_card`); a card never
    registered has no known cost, so it can't be called up or down. Each is
    that card's total_value (all copies) minus what it cost. A ripped card
    (cost 0) counts as up by its full value.
    """
    unique_value = sum(c.unique_value for c in cards)
    total_value = sum(c.total_value for c in cards)
    gain = total_value - net_invested
    per_card = [
        (c, c.total_value - invested_by_card[c.id]) for c in cards if c.qty > 0 and c.id in invested_by_card
    ]
    per_card.sort(key=lambda pair: pair[1], reverse=True)
    return {
        "gain": gain,
        "pct": (gain / net_invested * 100) if net_invested > 0 else None,
        "unique_value": unique_value,
        "total_value": total_value,
        "net_invested": net_invested,
        "n_up": sum(1 for _, g in per_card if g > 0),
        "n_down": sum(1 for _, g in per_card if g < 0),
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


def shipping_shares(txs: list[Transaction]) -> dict[int, float]:
    """Each purchase row's share of its order's shipping, keyed by
    transaction id -- shipping is part of what a card actually cost.

    An order's `purchase_shipping` (stored redundantly on every row, see
    `_purchase_shipping_total`) is split across the order's purchase-type
    rows in proportion to their `price`, so a 100 kr card carries more of it
    than a 5 kr card. When none of them has a price yet (price 0 = not
    priced, e.g. before "Distribute remaining" has run) it's split evenly
    instead. A row with no `purchase_id` carries its own shipping in full.
    The shares of an order always add up to its shipping, so totals built
    from them match `economic_summary`.
    """
    shares: dict[int, float] = {}
    groups: dict[int, list[Transaction]] = {}
    for t in txs:
        if t.type != "purchase":
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


def net_invested_by_card(db: Session, txs: list[Transaction] | None = None) -> dict[int, float]:
    """Return actual net investment per card using economic-summary rules.

    Like `economic_summary`, this includes `purchase_shipping`: each
    purchase row carries its share of its order's shipping (see
    `shipping_shares` -- split by price across the order's cards), so a
    card's figure is what it really cost to get it home, and
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
        if tx.type == "purchase":
            amount = tx.price + (tx.fees or 0.0) + shares.get(tx.id, 0.0)
        elif tx.type == "sale":
            amount = -tx.price
        else:
            amount = 0.0
        invested[tx.card_id] = invested.get(tx.card_id, 0.0) + amount
    return invested


def assign_bucket_investment(buckets, invested_by_card: dict[int, float]) -> None:
    """Attach transaction totals to buckets without changing value rules."""
    for bucket in buckets:
        bucket.net_invested = sum(invested_by_card.get(card.id, 0.0) for card in bucket.cards)
        bucket.has_investment = any(card.id in invested_by_card for card in bucket.cards)


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
