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

from sqlalchemy import func
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

    # A series' own rank is its earliest set's rank.
    release_ranks: dict[str, int] = {}
    for (series_key, _set_key), rank in set_release_ranks.items():
        release_ranks[series_key] = min(release_ranks.get(series_key, rank), rank)

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
    display_price = func.coalesce(Card.tcgplayer_price, Card.reference_price)
    return (
        db.query(Card)
        .filter(display_price.isnot(None))
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


def real_value_history(db: Session, metric: str = "unique") -> list[dict]:
    """The real, non-approximated counterpart to collection_value_growth:
    the collection's total value on each date a snapshot was actually taken
    (see snapshots.record_daily_snapshot), summed across every card's
    CardSnapshot row for that (date, source) -- what the collection was
    actually worth on date X, not today's price applied retroactively.
    Empty until at least one snapshot has accumulated (there is no way to
    backfill snapshots for dates before this table existed).

    Up to two points per date: one from the scheduled cron sync, one from
    the latest manual sync that day (see CardSnapshot.source). Points are
    ordered cron-then-manual within a date; the label only gets a
    "(cron)"/"(manual)" suffix when both exist for the same date, so the
    common one-point-a-day case keeps its plain date label.

    `metric` reuses the same VALUE_GROWTH_METRICS lambdas as
    collection_value_growth -- CardSnapshot exposes the same
    duplicates/unique_value/total_value properties as Card, computed from
    that snapshot's own qty/reference_price instead of today's.
    """
    if metric not in VALUE_GROWTH_METRICS:
        raise ValueError(f"unknown metric: {metric!r} (expected one of {sorted(VALUE_GROWTH_METRICS)})")
    _, value_of = VALUE_GROWTH_METRICS[metric]

    by_date_source: dict[tuple[dt.date, str], float] = {}
    snapshots_by_date_source: dict[tuple[dt.date, str], list[CardSnapshot]] = {}
    for snap in db.query(CardSnapshot).all():
        key = (snap.date, snap.source)
        by_date_source[key] = by_date_source.get(key, 0.0) + value_of(snap)
        snapshots_by_date_source.setdefault(key, []).append(snap)

    sources_by_date: dict[dt.date, set[str]] = {}
    for date, source in by_date_source:
        sources_by_date.setdefault(date, set()).add(source)

    source_order = {"cron": 0, "manual": 1}
    result = []
    for (date, source), total in sorted(
        by_date_source.items(), key=lambda item: (item[0][0], source_order.get(item[0][1], 2))
    ):
        label = date.strftime("%Y-%m-%d")
        if len(sources_by_date[date]) > 1:
            label = f"{label} ({source})"
        snapshots = snapshots_by_date_source[(date, source)]
        if metric == "unique":
            card_count = sum(snap.qty > 0 for snap in snapshots)
        elif metric == "duplicates":
            card_count = sum(snap.duplicates for snap in snapshots)
        else:
            card_count = sum(snap.qty for snap in snapshots)
        result.append({"label": label, "cumulative_value": total, "card_count": card_count})
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


def economic_summary(db: Session) -> dict:
    """Total real money in/out across every registered transaction, plus the
    "paper" gain/loss against today's collection value (unique_value, so
    duplicates don't inflate it) -- unrealized, since it compares a real
    amount paid against today's reference price, not a sale.

    Purchase-side spend includes `purchase_shipping` (see
    `_purchase_shipping_total`) alongside price + fees -- previously omitted
    here even though `_group_transactions_by_purchase`'s per-order diff line
    already accounted for it, understating the "Net invested"/"Paper
    gain/loss" KPIs whenever any order had shipping set.
    """
    txs = db.query(Transaction).all()
    total_bought = sum(t.price + (t.fees or 0.0) for t in txs if t.type == "purchase") + _purchase_shipping_total(txs)
    total_sold = sum(t.price for t in txs if t.type == "sale")
    net_invested = total_bought - total_sold
    return {
        "total_bought": total_bought,
        "total_sold": total_sold,
        "net_invested": net_invested,
    }


def net_invested_by_card(db: Session) -> dict[int, float]:
    """Return actual net investment per card using economic-summary rules.

    Like `economic_summary`, this now includes `purchase_shipping`. Since
    shipping is a per-order cost, not per-card, it's attributed in full to a
    single card per order -- the lowest transaction id in that `purchase_id`
    group (the `order_by` below makes this deterministic) -- rather than
    split across every card in the lot. This keeps
    `sum(net_invested_by_card(db).values())` consistent with
    `economic_summary`'s `net_invested`; the tradeoff is that one card's own
    figure can look inflated relative to its lot-mates when a multi-card
    order has shipping set. A row with no `purchase_id` counts its own
    shipping on its own card, same as `_purchase_shipping_total`.
    """
    invested: dict[int, float] = {}
    seen_purchase_ids: set[int] = set()
    for tx in db.query(Transaction).order_by(Transaction.purchase_id, Transaction.id).all():
        if tx.type == "purchase":
            amount = tx.price + (tx.fees or 0.0)
            if tx.purchase_shipping:
                if tx.purchase_id is None:
                    amount += tx.purchase_shipping
                elif tx.purchase_id not in seen_purchase_ids:
                    seen_purchase_ids.add(tx.purchase_id)
                    amount += tx.purchase_shipping
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


@dataclass
class ListingCardPricing:
    """One card within a `Listing`, annotated with the three prices the
    /listings page compares side by side -- see the "Sales listings
    (finn.no)" business rule in README.md. `listed_price` is the listing's
    own `suggested_price` (one price per listing, not per card, since a
    listing covers a lot rather than pricing each card in it separately).
    """

    card: Card
    cost: float
    market_price: float | None
    listed_price: float | None


@dataclass
class ListingOverview:
    listing: Listing
    card_rows: list[ListingCardPricing]


def _build_listing_overview(listing: Listing, invested_by_card: dict[int, float]) -> ListingOverview:
    card_rows = [
        ListingCardPricing(
            card=card,
            cost=invested_by_card.get(card.id, 0.0),
            market_price=card.display_price,
            listed_price=listing.suggested_price,
        )
        for card in listing.cards
    ]
    return ListingOverview(listing=listing, card_rows=card_rows)


def listing_overview(db: Session, include_delisted: bool = False) -> list[ListingOverview]:
    """Every `Listing`, newest first, with each of its cards annotated with
    cost (actual money spent, from `net_invested_by_card` -- the same
    Transaction-derived figure used everywhere else, not a second "cost"
    concept), market price (`Card.display_price`), and listed price
    (`Listing.suggested_price`). Never touches `qty`, `card_collections`, or
    `binder_id` -- see README's "Sales listings (finn.no)" business rule.

    Excludes `status == "delisted"` listings by default -- `/listings`' "Show
    delisted" toggle passes `include_delisted=True` to include them.
    """
    invested_by_card = net_invested_by_card(db)
    query = db.query(Listing).options(selectinload(Listing.cards)).order_by(Listing.created_at.desc())
    if not include_delisted:
        query = query.filter(Listing.status != "delisted")
    return [_build_listing_overview(listing, invested_by_card) for listing in query.all()]


def listing_entry(db: Session, listing_id: int) -> ListingOverview | None:
    """Single-listing counterpart to `listing_overview`, used to re-render
    one row after an htmx action (e.g. delisting) without recomputing
    pricing for every listing. Returns None if the listing no longer exists.
    """
    listing = (
        db.query(Listing).options(selectinload(Listing.cards)).filter(Listing.id == listing_id).first()
    )
    if listing is None:
        return None
    invested_by_card = net_invested_by_card(db)
    return _build_listing_overview(listing, invested_by_card)


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
