"""SQLAlchemy models for the TCG inventory app.

Computed fields (`duplicates`, `total_value`, `unique_value`) are Python
properties, never persisted columns -- a stored/derived-value mismatch was a
real bug in the Excel version this app replaces. The one deliberate
exception is the resolved market price (`Card.market_price` and friends,
issue #210) -- see the comment next to `Card.display_price` for why.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, Date, Float, ForeignKey, Integer, String, Table, Text, Column, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import Base

card_collections = Table(
    "card_collections",
    Base.metadata,
    Column("card_id", Integer, ForeignKey("cards.id", ondelete="CASCADE"), primary_key=True),
    Column("collection_id", Integer, ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True),
)


class Binder(Base):
    __tablename__ = "binders"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)

    cards: Mapped[list["Card"]] = relationship(back_populates="binder")


# "On the way" badge turns into a warning after this many days (issue #382).
IN_TRANSIT_WARN_DAYS = 21


def in_hand_spares(in_hand_qty: int) -> int:
    """Spares available to sell: every copy *in hand* beyond the first,
    `max(in_hand - 1, 0)` (issue #382). The one definition every sale-facing
    figure uses -- business rule #2's `duplicates = max(qty - 1, 0)` stays
    the ownership figure."""
    return max((in_hand_qty or 0) - 1, 0)


def print_in_hand_spares(cards) -> int:
    """`in_hand_spares` for one print from every Card linked to its master
    card (summed in-hand qty), the in-hand counterpart of
    queries.print_spares for the master set and sale lists."""
    return in_hand_spares(sum(c.in_hand_qty for c in cards))


class Collection(Base):
    __tablename__ = "collections"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    priority_rank: Mapped[int] = mapped_column(Integer, nullable=False)

    cards: Mapped[list["Card"]] = relationship(
        secondary=card_collections, back_populates="collections"
    )


class Card(Base):
    __tablename__ = "cards"
    __table_args__ = (
        # Dex's "Id" alone is not a unique physical card -- the same Id
        # appears once per Variant the user owns (e.g. "Normal" and "Poké
        # Ball Holo" of the same card are two separate rows/cards with the
        # same Id). (Id, Variant) is the real natural key -- see importer.py.
        UniqueConstraint("card_id", "variant", name="uq_cards_card_id_variant"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[str] = mapped_column(String, nullable=False, index=True)

    name: Mapped[str] = mapped_column(String, nullable=False)
    number: Mapped[str | None] = mapped_column(String, nullable=True)
    # Derived from `number` at import time (e.g. "109/189" -> 109) purely so
    # cards within a set sort in printed order (1, 2, ..., 10) instead of
    # alphabetically ("1", "10", "2", ...). Never set directly -- see
    # importer._parse_number_int.
    number_int: Mapped[int | None] = mapped_column(Integer, nullable=True)
    series: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    set: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    variant: Mapped[str | None] = mapped_column(String, nullable=True)
    # From Dex's "Locale" column -- which language/region print this physical
    # card is (e.g. "ENG", "JPN", "KOR"), not a UI display language.
    language: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    rarity: Mapped[str | None] = mapped_column(String, nullable=True)
    illustrator: Mapped[str | None] = mapped_column(String, nullable=True)
    # Real card photo, looked up once at import time from the Pokemon TCG API
    # (api.pokemontcg.io -- Dex itself doesn't expose card images) via
    # card_images.fetch_image_url, keyed by name/set/number since Dex's own
    # `card_id` doesn't correspond to that API's card IDs. Null when no
    # confident match was found, or the lookup failed/was skipped (e.g.
    # offline) -- never retried automatically, since a card's image never
    # changes once printed.
    # Since 2026-09-23 also looked up by Dex's own card_id (Pokemon TCG API
    # by id for international prints, TCGdex's Japanese catalog for "jpn_"
    # ones) -- see card_images.fetch_image_by_card_id and backfill_images.py.
    image_url: Mapped[str | None] = mapped_column(String, nullable=True)
    # When backfill_images last tried and found no image, so a card with no
    # match waits IMAGE_RETRY_AFTER_DAYS before being retried instead of
    # being re-looked-up (and blocking the queue) on every run.
    image_lookup_failed_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    # --- Legacy per-source price columns (deprecated mirrors, issue #210) ---
    # Per-source prices live in `card_prices` (CardPrice below) since #210;
    # these columns are kept (init_db() never drops/renames a column) and
    # still written as mirrors so nothing that reads them breaks, but no
    # consumer should read them for a displayed price -- read
    # `market_price`/`display_price` instead.
    # Mirror of the `dex` card_prices row: Dex's exported Price cell, in NOK.
    # An empty Price cell no longer wipes it (the last known value is kept).
    reference_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Mirror of the `pokemontcg` card_prices row: pokemontcg.io's TCGplayer
    # market price, converted to NOK (card_images.fetch_card_data), and the
    # date it was fetched.
    tcgplayer_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    tcgplayer_price_updated_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    # DEPRECATED, no longer written or read (issue #210). Was the pokemontcg
    # failed-lookup stamp from #216; that state now lives per source in
    # CardPrice.lookup_failed_at (seeded from this column once, by
    # pricing.backfill_from_legacy). Kept only because init_db() can't drop
    # a column.
    price_lookup_failed_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    # --- Resolved market price (materialized, issue #210) ---
    # Written only by pricing.resolve_cards(), from this card's card_prices
    # rows -- never set directly. See display_price below for why this is
    # stored rather than computed.
    market_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Which card_prices source won ("dex", "pokemontcg", ...), and the date
    # that source's price was fetched. Null when the card has no price.
    market_price_source: Mapped[str | None] = mapped_column(String, nullable=True)
    market_price_as_of: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    # Comma-separated flags (pricing.FLAG_*): "stale", "no_price",
    # "variant_price_uncertain". Null when resolved with nothing to flag --
    # and also on a card the resolver has never seen (see display_price).
    price_flags: Mapped[str | None] = mapped_column(String, nullable=True)
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Real Set entity, replacing the string-matched `set_release_order` join
    # below -- see Set's docstring. Nullable (and populated automatically by
    # db.py's `_backfill_sets()`, not written here) so this stays additive:
    # every existing card keeps working via `series`/`set` even before it's
    # linked. `series`/`set` themselves are kept as-is alongside this FK
    # (denormalized) -- importer.py's Dex CSV sync still needs a plain
    # display/fallback string, and removing them is a separate, riskier
    # migration (see issue #133).
    set_id: Mapped[int | None] = mapped_column(ForeignKey("sets.id"), nullable=True)
    # Canonical print identity (language, set, number, variant) this physical
    # card is an instance of -- see MasterCard and masterdata.py. Nullable
    # and additive like set_id: linked at import time and by db.py's
    # _backfill_master_cards(); null only for a card_id masterdata.py can't
    # parse.
    master_card_id: Mapped[int | None] = mapped_column(ForeignKey("master_cards.id"), nullable=True, index=True)

    binder_id: Mapped[int | None] = mapped_column(ForeignKey("binders.id"), nullable=True)
    classification: Mapped[str | None] = mapped_column(String, nullable=True)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    flagged_missing_since: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    # "On the way" (issue #382): copies paid for but not yet received, from
    # the Dex "Incoming" folder (constants.STATUS_CATEGORIES). Written only by
    # importer._apply_in_transit: `in_transit_qty` = min(Incoming row qty,
    # qty); `in_transit_since` = the first sync that saw the tag, kept while
    # the tag stays (also across qty changes). Both null when not in transit.
    # Real per-card data from Dex, not derived from other columns -- read it
    # through the computed `in_transit` / `in_hand_qty` below, never raw.
    in_transit_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    in_transit_since: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    # Physical grade, e.g. "Near Mint" -- see constants.CARD_CONDITIONS. Real
    # per-card data with no other source (nobody's grading these on import),
    # unlike duplicates/total_value above -- not derived, so it's a real
    # column, not a computed property. Null ("Unknown" in the UI) until a
    # user sets it, most commonly when building a finn.no listing (ads.py).
    condition: Mapped[str | None] = mapped_column(String, nullable=True)
    # When this physical card (card_id, variant) was first imported -- set once,
    # on creation, never touched on update. Null for cards that already existed
    # before this column was added; there's no way to recover their real
    # creation date after the fact.
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    binder: Mapped[Binder | None] = relationship(back_populates="cards")
    collections: Mapped[list[Collection]] = relationship(
        secondary=card_collections, back_populates="cards"
    )
    # Deliberately no "delete"/"delete-orphan" cascade (issue #225): a Card
    # can be re-synced from Dex, its purchase/sale/trade history can't. With
    # the default cascade, deleting a Card that still has transactions makes
    # the ORM try to null Transaction.card_id (NOT NULL), so the flush fails
    # loudly instead of silently taking the money history with it. (The DB
    # FK is still ondelete=CASCADE; changing it to RESTRICT would need a
    # deliberate migration -- init_db() is additive-only.)
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="card")
    # Named `linked_set`, not `set` -- `Card.set` is already the plain
    # string column above (Dex's "Set" export column).
    linked_set: Mapped["Set | None"] = relationship(back_populates="cards")
    master_card: Mapped["MasterCard | None"] = relationship(back_populates="cards")
    # Latest price per source (see CardPrice). Lazy by default; bulk paths
    # (importer.py, price_refresh.py) selectinload it.
    prices: Mapped[list["CardPrice"]] = relationship(
        back_populates="card", cascade="all, delete-orphan"
    )

    @property
    def duplicates(self) -> int:
        return max(self.qty - 1, 0)

    # --- "On the way" (issue #382) -----------------------------------------
    # An in-transit copy counts as owned (value, dashboard, snapshots,
    # completion, `duplicates` above) but is never available to sell: every
    # sale-facing figure goes through `in_hand_qty` / `in_hand_spares`.
    @property
    def in_transit(self) -> int:
        """Copies on the way, clamped to 0..qty (a stored value can't make
        `in_hand_qty` negative)."""
        return min(max(self.in_transit_qty or 0, 0), max(self.qty or 0, 0))

    @property
    def in_hand_qty(self) -> int:
        """Copies actually in hand: qty minus the ones on the way."""
        return max((self.qty or 0) - self.in_transit, 0)

    @property
    def fully_in_transit(self) -> bool:
        """Owned, but no copy in hand yet: can't be picked for a sale or ad."""
        return (self.qty or 0) > 0 and self.in_hand_qty == 0

    @property
    def in_hand_spares(self) -> int:
        return in_hand_spares(self.in_hand_qty)

    @property
    def in_transit_days(self) -> int | None:
        """Days since the first sync that saw the Incoming tag; None when
        not in transit."""
        if not self.in_transit or self.in_transit_since is None:
            return None
        return max((dt.date.today() - self.in_transit_since).days, 0)

    @property
    def in_transit_overdue(self) -> bool:
        """On the way for IN_TRANSIT_WARN_DAYS or more: the badge shows its
        age in warning style (a forgotten tag, or a lost parcel)."""
        days = self.in_transit_days
        return days is not None and days >= IN_TRANSIT_WARN_DAYS

    # Why market_price is STORED, not computed (issue #210). The "computed,
    # never stored" rule at the top of this module is about values derivable
    # purely from other columns of the same row (`duplicates` from `qty`),
    # which drift the moment they're stored separately. The resolved market
    # price is different: it's a time-dependent *decision* -- which source
    # was fresh and reachable on the day it was resolved (pricing.py's chain
    # and 14-day freshness window) -- more like a snapshot than a derived
    # column. It also has to be a real column so Inventory can sort and page
    # by it in SQL. It stays honest because it's never edited by hand: only
    # pricing.resolve_cards() writes it, on every card_prices write and in a
    # full DB-only pass at the end of each cron, and re-running that
    # re-derives it from card_prices at any time.
    @property
    def display_price(self) -> float | None:
        """The price every Python-side consumer (value calculations,
        templates) should read: the resolved `market_price`. SQL consumers
        read the `market_price` column directly.

        Falls back to the pre-#210 rule (TCGplayer, else Dex) only for a
        card the resolver has never seen -- `price_flags` is null *and* there
        is no market_price. Every resolved card has either a market_price or
        the "no_price" flag, and init_db()'s backfill resolves every
        existing card, so in practice this is only a safety net.
        """
        if self.market_price is not None or self.price_flags is not None:
            return self.market_price
        return self.tcgplayer_price if self.tcgplayer_price is not None else self.reference_price

    @property
    def price_flag_list(self) -> list[str]:
        return [f for f in (self.price_flags or "").split(",") if f]

    @property
    def unique_value(self) -> float:
        """A qty=0 card (traded/sold away, but still present in the export --
        see `qty`'s own docstring context in importer.py) contributes nothing
        here, same as it already contributes nothing to `duplicates`/
        `total_value` above -- see issue #132. Gated the same way
        `total_value` naturally is via the `self.qty *` multiplication, just
        made explicit since `unique_value` doesn't otherwise multiply by qty.
        """
        return (self.display_price or 0.0) if self.qty > 0 else 0.0

    @property
    def total_value(self) -> float:
        return self.qty * (self.display_price or 0.0)

    @property
    def primary_collection(self) -> Collection | None:
        """The collection that gets "credit" for this card in summaries:
        the one with the lowest priority_rank among all collections the
        card actually belongs to. Does not affect card_collections itself.
        """
        if not self.collections:
            return None
        return min(self.collections, key=lambda c: c.priority_rank)


class CardSnapshot(Base):
    """One row per (card, date, source): that card's qty and reference_price
    as of that date. Written by `snapshots.record_daily_snapshot`, called
    from the `/cron/dropbox-sync` cron job right after a successful sync
    (source="cron") and from the manual CSV-upload/Dropbox-sync routes
    (source="manual") -- see app.py. Two sources per date, not per-call
    timestamps, is deliberate: it caps each day at exactly the scheduled
    cron point plus one "latest manual sync of the day" point, instead of
    growing unbounded every time someone re-triggers a sync (see
    HANDOFF.md). Stores the same raw inputs Card's computed properties use
    (never a derived total, same reasoning as Card above) -- so
    duplicates/unique_value/total_value can be computed the same way, but
    as of a past date instead of today. Without this table there is no way
    to answer "what was the collection worth on date X" -- see
    queries.real_value_history and HANDOFF.md.
    """

    __tablename__ = "card_snapshots"
    __table_args__ = (
        UniqueConstraint("card_id", "date", "source", name="uq_card_snapshots_card_id_date_source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(
        ForeignKey("cards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String, nullable=False, default="cron", server_default="cron")
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    # Misnamed for history's sake (init_db() can't rename a column): holds
    # the card's *resolved market price* that day (Card.display_price), not
    # Dex's reference price.
    reference_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Which card_prices source that price came from (Card.market_price_source)
    # -- lets queries.price_movers tell a real market move from a source
    # switch (issue #210). Null on every row written before #210, and on a
    # card with no price.
    price_source: Mapped[str | None] = mapped_column(String, nullable=True)

    @property
    def duplicates(self) -> int:
        return max(self.qty - 1, 0)

    @property
    def unique_value(self) -> float:
        return self.reference_price or 0.0

    @property
    def total_value(self) -> float:
        return self.qty * (self.reference_price or 0.0)


class CardPrice(Base):
    """The latest price for one card from one source -- one row per
    (card_id, source), overwritten in place (issue #210). Deliberately no
    per-source history: that would be ~4 rows x every card x every day, and
    `card_snapshots` alone already grows too fast (#169). History of the
    *chosen* price lives in card_snapshots (reference_price + price_source).

    Sources are pricing.CHAIN's names: "dex" (Dex CSV Price cell, NOK,
    written by importer.py), "pokemontcg" (pokemontcg.io TCGplayer market
    price, USD, written by price_refresh.py / importer.py), and
    "tcgdex_tcgplayer" (USD) / "tcgdex_cardmarket" (EUR), written by
    tcgdex_prices.py (#211).

    A row can exist with no price at all -- only `lookup_failed_at` -- for a
    source that has been tried and never priced this card; that's the
    per-source failed-lookup backoff (issue #216's former
    `cards.price_lookup_failed_at`). Written only through pricing.py, which
    re-resolves `Card.market_price` after every write.
    """

    __tablename__ = "card_prices"
    __table_args__ = (UniqueConstraint("card_id", "source", name="uq_card_prices_card_id_source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(
        ForeignKey("cards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String, nullable=False)
    # In the source's own currency. Null when the native value isn't known
    # (e.g. rows seeded from the NOK-only legacy tcgplayer_price column).
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String, nullable=True)
    # NOK per 1 unit of `currency` used to produce price_nok (1.0 for NOK;
    # null when unknown, e.g. seeded legacy rows).
    fx_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_nok: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Which print's price was used when the source has several (e.g.
    # pokemontcg.io's "holofoil" / "reverseHolofoil").
    variant_key: Mapped[str | None] = mapped_column(String, nullable=True)
    # When we last got a price from this source -- what pricing.py's
    # freshness window is measured against.
    fetched_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    # The source's own "as of" date, when it reports one.
    source_updated_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    # Comma-separated per-source flags, e.g. "variant_price_uncertain".
    flags: Mapped[str | None] = mapped_column(String, nullable=True)
    # Last lookup that came back with no usable price; cleared on success.
    lookup_failed_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    card: Mapped[Card] = relationship(back_populates="prices")

    @property
    def flag_list(self) -> list[str]:
        return [f for f in (self.flags or "").split(",") if f]


class FxRate(Base):
    """One day's NOK exchange rate for one currency, from Norges Bank
    (fx_rates.py, issue #210). Lets a serverless invocation reuse a rate
    another one already fetched, and a failed Norges Bank call fall back to
    the last stored rate instead of a hard-coded constant.
    """

    __tablename__ = "fx_rates"
    __table_args__ = (UniqueConstraint("date", "currency", name="uq_fx_rates_date_currency"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # Norges Bank's observation date (a business day), not the fetch date.
    date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String, nullable=False)
    rate_nok: Mapped[float] = mapped_column(Float, nullable=False)
    fetched_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


# Every value `Transaction.type` may hold -- the routes that write a type
# validate against this (form_validation.parse_tx_type, issue #228), since
# the money queries only count "purchase"/"sale" and a typo would silently
# drop a row out of every figure.
TRANSACTION_TYPES = ("purchase", "sale", "trade", "ripped")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id", ondelete="CASCADE"), nullable=False)
    # "purchase" | "sale" | "trade" | "ripped". "ripped" = pulled from a pack
    # yourself: always price 0 and, like "trade", never counted toward Net
    # invested or any other money figure -- it only records how you got the
    # card (see RIPPED in app.py).
    type: Mapped[str] = mapped_column(String, nullable=False)
    # Only meaningful when type == "trade": "in" (card received) or "out"
    # (card given away). NULL on every non-trade row, and on trade rows
    # recorded before this column existed. On a trade row, `price` is any
    # cash that moved alongside the card -- paid on an "in" row, received
    # on an "out" row -- see queries.trade_summary.
    direction: Mapped[str | None] = mapped_column(String, nullable=True)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    platform: Mapped[str | None] = mapped_column(String, nullable=True)
    fees: Mapped[float | None] = mapped_column(Float, nullable=True)
    # A user-assigned tag grouping transactions bought together in the same
    # order/session -- distinct from `id` (this row's own identity). Purely
    # informational: never set automatically, only ever what the user assigns.
    purchase_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # The agreed/full price for the whole purchase this row belongs to (same
    # value redundantly stored on every row sharing a purchase_id, same
    # pattern as date/platform above) -- lets History show a "registered
    # vs. agreed" diff per purchase, so missing normal-print cards (priced
    # individually later, never bulk-estimated) show up as an unaccounted
    # remainder instead of silently vanishing.
    purchase_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Shipping cost for the whole purchase -- same redundant-per-row pattern
    # as purchase_total, and subtracted alongside it when computing the diff
    # above, so shipping doesn't masquerade as an unpriced card. Despite
    # the name, it carries a *sale* order's shipping too (what the seller
    # paid -- Mark sold and the cart with type Sale write it), which comes
    # off that sale's net proceeds; see queries.shipping_shares (issue
    # #254). Not renamed: init_db() is additive-only.
    purchase_shipping: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Free-form note on this row, e.g. "Kjopt pa Collect63 Card Show" -- see
    # issue #109 / HANDOFF.md's 2026-09-14 entry, which set a note like this
    # directly in prod before this column existed.
    note: Mapped[str | None] = mapped_column(String, nullable=True)
    # Set only for a "sale" row created by `POST /listings/{id}/mark-sold`
    # (issue #127) -- links this real, completed sale back to the `Listing`
    # lot it was sold out of. Nullable/additive: every other Transaction
    # (plain purchases, sales registered directly via the purchase-cart form,
    # and every row that predates this column) has no `Listing` to point at.
    # Deliberately on `Transaction`, not a `sold_transaction_id` FK on
    # `Listing` the other way around -- a lot of N cards sold together needs
    # N Transaction rows (one per card, each with its own realized price and
    # cost basis), not one row a single FK on Listing could name, so
    # many-Transactions-to-one-Listing is the only relationship that fits.
    # See models.Listing's docstring for the full flow.
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("listings.id"), nullable=True, index=True)

    card: Mapped[Card] = relationship(back_populates="transactions")
    listing: Mapped["Listing | None"] = relationship(back_populates="sale_transactions")


listing_cards = Table(
    "listing_cards",
    Base.metadata,
    Column("listing_id", Integer, ForeignKey("listings.id", ondelete="CASCADE"), primary_key=True),
    Column("card_id", Integer, ForeignKey("cards.id", ondelete="CASCADE"), primary_key=True),
)


class Listing(Base):
    """A finn.no ad generated from a selection of cards (ads.py), recorded
    once the user clicks "Mark as listed" after copying the generated text
    out. Deliberately its own table rather than a boolean/date pair on
    `Card`: the primary use case is a lot (several cards, one ad, one
    price), so a per-card flag would either duplicate the same date across
    every card in the lot or lose the "these cards were one ad" grouping.
    It's also deliberately not a `Transaction` -- Transaction rows are real,
    completed cash flow that every economic query (economic_summary,
    cash_flow_by_month, net_invested_by_card) sums directly, and a listing
    has no price actually received yet. Marking a card listed never touches
    `qty`/`card_collections`/`binder_id` -- listed != sold; a real sale is
    still only ever recorded as a `Transaction` once it actually happens.

    `POST /listings/{id}/delist` (the "Remove listing" control on
    `/listings`) sets `status = "delisted"` and, like every other listing
    action, never touches `qty`/`card_collections`/`binder_id` -- delisting
    an ad is not the same as the cards being gone. `/listings` excludes
    delisted listings by default; its "Show delisted" toggle reveals them.

    `GET`/`POST /listings/{id}/edit` (issue #126) lets `title`,
    `description`, `suggested_price`, and the attached card set
    (`listing_cards`) all be changed after creation -- a listed price gets
    renegotiated, a card gets pulled from the lot, ad copy needs a tweak.
    "Regenerate ad text" there reruns `ads.build_listing` off the
    *currently selected* cards so the text doesn't go stale relative to an
    edited card set. `POST /listings/{id}/delete` hard-removes the row
    itself (distinct from delist -- for a mistaken/duplicate/test entry
    that shouldn't remain in history even delisted); the client requires a
    confirmation step first since, unlike delist, it's irreversible. Both
    actions keep the same invariant as delist: never `qty`,
    `card_collections`, `binder_id`, or `Transaction` rows.

    `GET`/`POST /listings/{id}/mark-sold` (issue #127) is the one listing
    action that *does* touch `Transaction`: it reuses the purchase-cart
    UI/route pattern (`/transactions/purchase/start` + `.../add-row`),
    pre-filled with this listing's cards and each defaulted to
    `suggested_price / card_count` as an editable starting guess -- never
    auto-submitted, since these numbers become real, permanent cost-basis
    history the moment they're saved. Submitting creates one
    `Transaction(type="sale", listing_id=<this listing>.id, ...)` per card,
    all sharing one fresh `purchase_id` (same grouping convention the
    purchase-cart form already uses), then flips `status` to `"sold"` only
    after every row commits, in a single DB transaction -- a failure
    partway must not leave orphaned Transactions or a `status` stuck
    between the two. Re-running mark-sold against an already-`"sold"`
    listing is a no-op (no duplicate Transactions). Like every other
    listing action, this still never touches `qty`, `card_collections`, or
    `binder_id` -- that invariant belongs to the Dex CSV sync alone (see
    `importer.py`), not to any Transaction, sale-linked or otherwise.
    """

    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    suggested_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Free-text, not an enum, same precedent as Transaction.platform -- only
    # "finn.no" is generated today but nothing here assumes that.
    platform: Mapped[str] = mapped_column(String, nullable=False, default="finn.no")
    # "active" | "delisted" | "sold" -- "sold" is set by
    # `POST /listings/{id}/mark-sold` (issue #127) once every card's sale
    # Transaction has committed; see that route and `sale_transactions`
    # below.
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")

    cards: Mapped[list["Card"]] = relationship(secondary=listing_cards)
    # Real per-card sale rows created by mark-sold (issue #127) -- see
    # Transaction.listing_id. Not `cards` above (that's the lot's current
    # membership, editable via `/listings/{id}/edit`); this is the actual
    # cash-flow history, which stays fixed to whichever cards were in the
    # lot at the moment it was marked sold even if `cards` changes later
    # (editing a sold listing's card set is not a flow this app exposes).
    sale_transactions: Mapped[list["Transaction"]] = relationship(back_populates="listing")


class Set(Base):
    """Real Set entity -- (series, name) with a proper primary key, replacing
    `SetReleaseOrder`'s string-matched join to `Card` below (see its
    docstring). `Card.set_id` is a real FK to this table, so a future Dex
    rename of a set name can't silently break the join for a card that's
    already linked, the way a string match could -- see issue #133.

    Rows are get-or-created automatically for every distinct (series, set)
    pair seen on `cards`, by db.py's `_backfill_sets()` -- nothing needs to
    seed this table by hand the way `set_release_order` did/does.
    `release_rank` is nullable because not every set is known to
    `set_sync.py`'s api.pokemontcg.io backfill (see issue #136) -- app.py's
    Inventory "release order" sort falls back to `UNKNOWN_RELEASE_RANK` for
    a card with no linked Set row, or a linked one with a null rank -- same
    fallback semantics as before, just sourced from this FK now.
    `release_rank` used to be "hand-entered once researched, never
    guessed"; issue #136 deliberately changed that -- `set_sync.py` now
    populates it in bulk from api.pokemontcg.io's real published
    `releaseDate` per set, which satisfies "never guessed" a different way
    (real published data, not a manual estimate) rather than abandoning the
    principle. A set that script can't confidently match (see its
    module docstring -- notably JP/KR sets, which that API doesn't cover
    yet) is left null rather than assigned a wrong rank; `release_rank` can
    still be hand-edited directly for a case the sync can't cover.
    `total_cards` is likewise populated by `set_sync.py` (from the API's
    per-set card count) for every matched set -- reserved for a future "set
    completion %" feature (tracked separately, not built here).
    """

    __tablename__ = "sets"
    __table_args__ = (UniqueConstraint("series", "name", name="uq_sets_series_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    series: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    release_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_cards: Mapped[int | None] = mapped_column(Integer, nullable=True)

    cards: Mapped[list["Card"]] = relationship(back_populates="linked_set")


class MasterCard(Base):
    """Masterdata: one canonical identity per printed card + variant,
    independent of which catalog (Dex, pokemontcg.io, TCGdex, TCGplayer,
    ...) it came from. Keyed on (language, set_code, number, variant) --
    what's printed on the card, since no official per-card ID exists. See
    masterdata.py for how the key is derived and the variant vocabulary.

    Identity only: qty/binder/collections stay on `Card` (the physical
    card you own). A MasterCard can exist with no `Card` pointing at it,
    which is what a future wishlist or set-completion view would build on.
    `name`/`series`/`set_name`/`printed_number` are descriptive copies taken
    from the first card linked, not part of the key.
    """

    __tablename__ = "master_cards"
    __table_args__ = (
        UniqueConstraint("language", "set_code", "number", "variant", name="uq_master_cards_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # "int" (Dex's International catalog), "ja", "zh-hans", ... -- see
    # masterdata.DEX_LANGUAGE_PREFIXES.
    language: Mapped[str] = mapped_column(String, nullable=False)
    set_code: Mapped[str] = mapped_column(String, nullable=False, index=True)
    number: Mapped[str] = mapped_column(String, nullable=False)
    # Canonical code, e.g. "reverse_holo" -- see masterdata.VARIANT_LABELS.
    variant: Mapped[str] = mapped_column(String, nullable=False)
    variant_label: Mapped[str | None] = mapped_column(String, nullable=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    series: Mapped[str | None] = mapped_column(String, nullable=True)
    set_name: Mapped[str | None] = mapped_column(String, nullable=True)
    printed_number: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # Filled in by set_checklist_seed.py (issue #368) from TCGdex, only where
    # still empty: a print nobody owns has no Card to show a rarity or photo
    # from. Never overwrites a value that's already there.
    rarity: Mapped[str | None] = mapped_column(String, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String, nullable=True)

    cards: Mapped[list["Card"]] = relationship(back_populates="master_card")
    external_ids: Mapped[list["MasterCardId"]] = relationship(
        back_populates="master_card", cascade="all, delete-orphan"
    )


class MasterCardId(Base):
    """One external catalog's ID for a MasterCard -- at most one per
    `source` ("dex", "pokemontcg", later "tcgplayer", "collectr", ...).
    Deliberately *not* unique on (source, external_id): catalogs like
    pokemontcg.io have one ID per print with variants inside it, so the
    same ID legitimately maps onto several MasterCards (Normal and Reverse
    Holo of the same print). `matched_by` records how trustworthy the
    mapping is; a "manual" row is never overwritten automatically -- see
    masterdata.set_external_id.
    """

    __tablename__ = "master_card_ids"
    __table_args__ = (
        UniqueConstraint("master_card_id", "source", name="uq_master_card_ids_master_source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    master_card_id: Mapped[int] = mapped_column(
        ForeignKey("master_cards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String, nullable=False)
    external_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # "exact_id" | "derived" | "heuristic" | "manual" -- see masterdata.py.
    matched_by: Mapped[str] = mapped_column(String, nullable=False)
    matched_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    master_card: Mapped[MasterCard] = relationship(back_populates="external_ids")


class SetChecklist(Base):
    """A set's reference list of prints (issue #368): what the set *should*
    contain, owned or not, so a set page can show what's missing. One per
    masterdata (language, set_code). Filled by set_checklist_seed.py from
    TCGdex; see README "Master sets / checklists".

    Membership (`SetChecklistCard`) is explicit: a print is in the set only
    if the seed put it there, so an odd Dex variant never silently becomes
    part of it. How complete the set is gets computed from the members and
    the cards linked to them, never stored.
    """

    __tablename__ = "set_checklists"
    __table_args__ = (UniqueConstraint("language", "set_code", name="uq_set_checklists_language_set_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # masterdata's key parts, e.g. "ja" / "sv2a".
    language: Mapped[str] = mapped_column(String, nullable=False)
    set_code: Mapped[str] = mapped_column(String, nullable=False)
    # What the user calls the set, e.g. "Pokémon Card 151 (Korean)".
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    # Where the list came from ("tcgdex") and that source's own set ID ("SV2a").
    source: Mapped[str] = mapped_column(String, nullable=False)
    source_set_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # When the seed last changed this checklist (a re-run that finds nothing
    # new leaves it alone).
    fetched_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    cards: Mapped[list["SetChecklistCard"]] = relationship(
        back_populates="checklist", cascade="all, delete-orphan"
    )


class SetChecklistCard(Base):
    """One print (a MasterCard) in a set checklist, on one track:

    - `main` / `secret`: the base print of each number (main = up to the
      set's official count, e.g. 1-165; secret = above it, 166-210).
    - `poke_ball`: the Poké Ball reverse print of a number.
    - `master_ball`: the Master Ball reverse print. Listed, but
      `counts_toward_completion` is false: the user doesn't collect it.
    """

    __tablename__ = "set_checklist_cards"
    __table_args__ = (
        UniqueConstraint("checklist_id", "master_card_id", name="uq_set_checklist_cards_checklist_master"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    checklist_id: Mapped[int] = mapped_column(
        ForeignKey("set_checklists.id", ondelete="CASCADE"), nullable=False, index=True
    )
    master_card_id: Mapped[int] = mapped_column(ForeignKey("master_cards.id"), nullable=False, index=True)
    # "main" | "secret" | "poke_ball" | "master_ball" -- see set_checklist_seed.TRACKS.
    track: Mapped[str] = mapped_column(String, nullable=False)
    counts_toward_completion: Mapped[bool] = mapped_column(Boolean, nullable=False)

    checklist: Mapped[SetChecklist] = relationship(back_populates="cards")
    master_card: Mapped[MasterCard] = relationship()


class CardList(Base):
    """A user-curated list of prints (issue #370, epic #366): a want list
    (cards to buy) or a sale list (spares to sell). `kind` is fixed when
    the list is created. See card_lists.py and README "Want and sale lists".

    A list is a curated snapshot: nothing is ever added or removed
    automatically, and an item's status (Missing / Got it / Listed / ...)
    is computed live from the cards linked to its master card, never
    stored. Like `listings`, a list never touches qty, collections,
    binders or transactions.
    """

    __tablename__ = "card_lists"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # "want" | "sale" -- see card_lists.LIST_KINDS. Never changed after creation.
    kind: Mapped[str] = mapped_column(String, nullable=False)
    note: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    items: Mapped[list["CardListItem"]] = relationship(
        back_populates="card_list", cascade="all, delete-orphan"
    )


class CardListItem(Base):
    """One print on a list. Points at the masterdata identity, not a
    `Card`: that's how a want list holds prints the user doesn't own (a
    checklist master card with no Card). The owned cards, if any, are found
    through `MasterCard.cards`."""

    __tablename__ = "card_list_items"
    __table_args__ = (UniqueConstraint("list_id", "master_card_id", name="uq_card_list_items_list_master"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    list_id: Mapped[int] = mapped_column(ForeignKey("card_lists.id", ondelete="CASCADE"), nullable=False, index=True)
    master_card_id: Mapped[int] = mapped_column(ForeignKey("master_cards.id"), nullable=False, index=True)
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # What the user wants to pay (want) or ask (sale), in kr.
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str | None] = mapped_column(String, nullable=True)
    # "manual" | "missing" | "spares": how the item got onto the list.
    source: Mapped[str] = mapped_column(String, nullable=False, default="manual")
    added_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    card_list: Mapped[CardList] = relationship(back_populates="items")
    master_card: Mapped[MasterCard] = relationship()


class SetReleaseOrder(Base):
    """Lookup table for chronological (release-date) sorting of sets.

    Superseded by `Set` above (see issue #133) -- app.py's Inventory sort no
    longer reads this table, and `db.py`'s `_backfill_sets()` only reads it
    once, to carry any existing `release_rank` row over onto the matching
    new `Set` row during backfill. Kept in place (not dropped/renamed) as a
    safety net per CLAUDE.md's additive-only `init_db()` rule -- nothing new
    should write to this table going forward; edit `Set.release_rank`
    instead.

    Was empty by default -- the ~100-row table from the Excel work needed to
    be supplied separately (see README) to enable release-order sorting.
    Without rows here, the app fell back to alphabetical (series, set). A
    (series, set) missing here sorted after every known set rather than
    guessing (see app.py's former UNKNOWN_RELEASE_RANK usage against this
    table), and should have gotten a real row added once its actual release
    date was known -- never guessed. Same rules now apply to `Set` instead.
    """

    __tablename__ = "set_release_order"
    __table_args__ = (UniqueConstraint("series", "set", name="uq_set_release_order_series_set"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    series: Mapped[str] = mapped_column(String, nullable=False)
    set: Mapped[str] = mapped_column(String, nullable=False)
    release_rank: Mapped[int] = mapped_column(Integer, nullable=False)


class ImportLog(Base):
    """One row per background-job run shown on the Sync status page
    (`/sync-status`, issue #264). Persisted (rather than only printed to
    Vercel's runtime logs) so the app itself can show what happened on every
    run, including the unattended cron runs nobody watched live.

    Originally Dex syncs only. Since #264 it also records the outcomes that
    used to leave no trace -- an empty Dropbox folder, a circuit-breaker
    abort (#225), a Dropbox error -- and the other cron jobs (price refresh,
    set sync, image backfill). The table name and Dex-specific counters stay
    as they were (`init_db()` is additive-only); `job`/`status`/`message`/
    `warnings_text` were added as nullable columns, and
    `db._backfill_import_log_defaults` fills `job="dex-sync"`/`status="ok"`
    into rows written before them. Code reading these should still treat a
    NULL as those defaults (see `sync_status`).

    The card counters (`cards_created` etc.) only mean something on
    `job == "dex-sync"` rows; other jobs put their summary in `message`.
    `cards_deleted` is dead (no sync deletes since #225) and no longer shown,
    but stays for the historical rows.
    """

    __tablename__ = "import_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ran_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    # How the run was triggered: "cron" (the real scheduled Vercel call) or
    # "manual" (a ?secret= call, a local run, or a test seed). Older rows
    # may also say "dropbox" (the manual Dropbox picker, removed in #264).
    source: Mapped[str] = mapped_column(String, nullable=False)
    files: Mapped[str | None] = mapped_column(String, nullable=True)  # comma-joined filenames
    cards_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cards_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cards_flagged_missing: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cards_deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    collections_touched: Mapped[str | None] = mapped_column(String, nullable=True)
    binders_touched: Mapped[str | None] = mapped_column(String, nullable=True)
    warnings_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # --- added in #264, all nullable (additive ALTER, no default on old rows) ---
    # "dex-sync" | "price-refresh" | "set-sync" | "image-backfill"
    job: Mapped[str | None] = mapped_column(String, nullable=True, default="dex-sync")
    # "ok" | "empty" (no CSV files) | "aborted" (circuit breaker) | "failed"
    status: Mapped[str | None] = mapped_column(String, nullable=True, default="ok")
    # One-line summary (other jobs) or the error / abort reason.
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The import's warnings, one per line (only the count was kept before).
    warnings_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # NULL job/status = a row from before #264, always a successful Dex sync.
    @property
    def job_name(self) -> str:
        return self.job or "dex-sync"

    @property
    def status_name(self) -> str:
        return self.status or "ok"

    @property
    def warnings_list(self) -> list[str]:
        return (self.warnings_text or "").splitlines()


class FavoritePokemon(Base):
    """A Pokemon (by name, e.g. "Sableye") the user flagged as a favorite on
    the Dashboard's Pokemon breakdown -- not tied to any one physical card,
    since the breakdown itself groups every print of that name together.
    """

    __tablename__ = "favorite_pokemon"

    name: Mapped[str] = mapped_column(String, primary_key=True)


class Release(Base):
    """**Unused since issue #264.** The in-app Release Notes page and its
    routes were removed; `notes/CHANGELOG.md` is the record of changes now.
    The model and the `releases` table are left in place on purpose
    (`init_db()` never drops a table) so existing rows aren't lost. Nothing
    reads or writes it any more. Original description below.

    One entry in the in-app "Release Notes" page (`/releases`, issue
    #144) -- a small, hand-authored log of user-facing changes, written
    directly to the database (not a `CHANGELOG.md` file, and not generated
    from git/PR history) so authoring works identically on local SQLite and
    the read-only-filesystem Vercel deploy alike. See db.py's module
    docstring/`DB_PATH.touch()` probe for why a file can't be authored to
    in place on Vercel, and CLAUDE.md's computed-vs-stored precedent for why
    "one small additive table, same code path everywhere" beats an
    environment-specific trick here too.

    `body` is rendered as plain, Jinja-autoescaped text with
    `white-space: pre-wrap` -- no Markdown parser, deliberately: this is one
    owner writing a few sentences per entry, not a dependency worth adding.
    No edit-in-place for v1 (delete and re-add instead) and no versioning/
    tags/categories -- see issue #144's "Sequencing / fast-follows" section.
    """

    __tablename__ = "releases"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)


class PokemonAlias(Base):
    """Maps one printed card name (e.g. "Dark Celebi") onto the folder name
    it should be grouped/favorited under (e.g. "Celebi") -- lets the user
    put cards into the same "Pokemon folder" whether they're name variants
    of the same species (Celebi / Dark Celebi) or a whole evolution family
    (Slowpoke / Slowbro / Slowking). Purely a display grouping -- it never
    touches the underlying Card rows.
    """

    __tablename__ = "pokemon_alias"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String, nullable=False, index=True)


class WonItem(Base):
    """One lot won on Facebook, sent by `fb_auction_watcher` to
    `POST /inbox/fb-wins` (issue #309): a staging row waiting to be
    registered as a purchase, never a transaction or a card itself.

    Keyed by `external_ref` (`fbaw:<postId>:<commentId>`, or
    `fbaw:<postId>:pos<n>` for a lot without a comment ID). A re-send
    refreshes a `pending` row (e.g. a price that was unknown becomes known)
    and never touches a `registered` or `ignored` one -- see won_inbox.py.
    The contract is defined on the producer side, in
    `apps/fb_auction_watcher/docs/spec.md` "Sending wins to tcg_inventory".

    Totals per sale are computed from the items, never stored.
    `purchase_id` is the Order ID the item was registered under by the
    New Order cart's link flow (also set on a lot kept pending as "not
    complete"); a plain label like `Transaction.purchase_id`, so it can
    point at an order that was later deleted, merged or split.
    """

    __tablename__ = "won_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_ref: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    # Which producer sent it: "fbaw" (fb_auction_watcher) for now.
    source: Mapped[str] = mapped_column(String, nullable=False)
    seller: Mapped[str | None] = mapped_column(String, nullable=True)
    # "auction" | "claim" | "fixed"
    sale_type: Mapped[str] = mapped_column(String, nullable=False)
    # The sale's end date in Europe/Oslo; NULL when it has none (fixed price).
    ended_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    post_url: Mapped[str] = mapped_column(String, nullable=False)
    lot_url: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    # What the lot costs in kr, or NULL when it isn't known yet.
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The seller's own shipping / payment terms, as written in the post.
    shipping_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The user's own Paid / Received marks in the extension.
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    received_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    # "pending" | "registered" | "ignored"
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending", index=True)
    purchase_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class JobLock(Base):
    """Single-flight guard for background jobs (issue #340, designed in
    #274 point 2). A row exists while a run of `job` is in progress: the
    run inserts it before doing anything and deletes it when it finishes,
    whatever the outcome. A second run that finds a live row refuses with
    `already_running`. A row older than `job_locks.STALE_AFTER` belongs to a
    run that was killed (e.g. Vercel's 300 s timeout) before it could clean
    up: it's taken over, and that run is recorded on /sync-status as
    `failed` ("interrupted"), so a killed run no longer leaves no trace.

    Stored in the database because an in-process lock doesn't reach other
    serverless instances, and session-level `pg_advisory_lock` isn't held
    reliably with NullPool + Supabase's transaction-mode pooler (db.py).
    Only `dex-sync` uses it for now; #274 generalizes it to every job.
    """

    __tablename__ = "job_locks"

    # A sync_status job name, e.g. "dex-sync".
    job: Mapped[str] = mapped_column(String, primary_key=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)  # naive UTC
    # Who started the run: "cron" / "manual" (later "connector", #274).
    trigger: Mapped[str | None] = mapped_column(String, nullable=True)
    # Random per-run token, so a run only ever releases (or a takeover only
    # replaces) the exact lock it saw -- never a newer run's.
    token: Mapped[str] = mapped_column(String, nullable=False)
