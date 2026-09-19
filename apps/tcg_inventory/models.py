"""SQLAlchemy models for the TCG inventory app.

Computed fields (`duplicates`, `total_value`, `unique_value`) are Python
properties, never persisted columns -- a stored/derived-value mismatch was a
real bug in the Excel version this app replaces.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime, Date, Float, ForeignKey, Integer, String, Table, Column, UniqueConstraint
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
    image_url: Mapped[str | None] = mapped_column(String, nullable=True)

    reference_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Live TCGPlayer market price, looked up from the same Pokemon TCG API
    # call as image_url (card_images.fetch_card_data). Unlike image_url this
    # is refetched periodically (prices move; images never do) -- see
    # importer.py's staleness check against tcgplayer_price_updated_at. Null
    # when no confident match was found yet, or the API had no tcgplayer
    # pricing data for this card. `display_price` below is what every
    # consumer should read, not this column directly.
    tcgplayer_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    tcgplayer_price_updated_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
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

    binder_id: Mapped[int | None] = mapped_column(ForeignKey("binders.id"), nullable=True)
    classification: Mapped[str | None] = mapped_column(String, nullable=True)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    flagged_missing_since: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
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
    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="card", cascade="all, delete-orphan"
    )
    # Named `linked_set`, not `set` -- `Card.set` is already the plain
    # string column above (Dex's "Set" export column).
    linked_set: Mapped["Set | None"] = relationship(back_populates="cards")

    @property
    def duplicates(self) -> int:
        return max(self.qty - 1, 0)

    @property
    def display_price(self) -> float | None:
        """The price every consumer (value calculations, sorting, templates)
        should read: the live TCGPlayer price when we have one, falling back
        to Dex's own exported Price otherwise. Two independent sources are
        kept in separate columns rather than one column overwritten in
        place, so it's always possible to tell which one produced a given
        value -- see tcgplayer_price's column comment.
        """
        return self.tcgplayer_price if self.tcgplayer_price is not None else self.reference_price

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
    reference_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    @property
    def duplicates(self) -> int:
        return max(self.qty - 1, 0)

    @property
    def unique_value(self) -> float:
        return self.reference_price or 0.0

    @property
    def total_value(self) -> float:
        return self.qty * (self.reference_price or 0.0)


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)  # "purchase" | "sale" | "trade"
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
    # above, so shipping doesn't masquerade as an unpriced card.
    purchase_shipping: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Free-form note on this row, e.g. "Kjopt pa Collect63 Card Show" -- see
    # issue #109 / HANDOFF.md's 2026-09-14 entry, which set a note like this
    # directly in prod before this column existed.
    note: Mapped[str | None] = mapped_column(String, nullable=True)

    card: Mapped[Card] = relationship(back_populates="transactions")


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
    # "active" | "delisted" | "sold" -- "sold" is reserved for a future link
    # to a real Transaction (e.g. a `sold_transaction_id` FK) once that flow
    # is built; nothing sets it yet.
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")

    cards: Mapped[list["Card"]] = relationship(secondary=listing_cards)


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
    """One row per completed import/sync -- manual upload, a manual Dropbox
    sync, or the scheduled cron job. Persisted (rather than only printed to
    Vercel's runtime logs) so the app itself can show a history of what
    happened on every sync, including the unattended cron runs nobody
    watched live.
    """

    __tablename__ = "import_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ran_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)  # "manual" | "dropbox" | "cron"
    files: Mapped[str | None] = mapped_column(String, nullable=True)  # comma-joined filenames
    cards_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cards_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cards_flagged_missing: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cards_deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    collections_touched: Mapped[str | None] = mapped_column(String, nullable=True)
    binders_touched: Mapped[str | None] = mapped_column(String, nullable=True)
    warnings_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class FavoritePokemon(Base):
    """A Pokemon (by name, e.g. "Sableye") the user flagged as a favorite on
    the Dashboard's Pokemon breakdown -- not tied to any one physical card,
    since the breakdown itself groups every print of that name together.
    """

    __tablename__ = "favorite_pokemon"

    name: Mapped[str] = mapped_column(String, primary_key=True)


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
