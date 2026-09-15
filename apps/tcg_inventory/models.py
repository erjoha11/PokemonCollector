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

    reference_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    binder_id: Mapped[int | None] = mapped_column(ForeignKey("binders.id"), nullable=True)
    classification: Mapped[str | None] = mapped_column(String, nullable=True)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    flagged_missing_since: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
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

    @property
    def duplicates(self) -> int:
        return max(self.qty - 1, 0)

    @property
    def unique_value(self) -> float:
        return self.reference_price or 0.0

    @property
    def total_value(self) -> float:
        return self.qty * (self.reference_price or 0.0)

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
    """One row per (card, date): that card's qty and reference_price as of
    that date. Written once a day by `snapshots.record_daily_snapshot`,
    called from the `/cron/dropbox-sync` cron job right after a successful
    sync (see app.py). Stores the same raw inputs Card's computed properties
    use (never a derived total, same reasoning as Card above) -- so
    duplicates/unique_value/total_value can be computed the same way, but
    as of a past date instead of today. Without this table there is no way
    to answer "what was the collection worth on date X" -- see
    queries.real_value_history and HANDOFF.md.
    """

    __tablename__ = "card_snapshots"
    __table_args__ = (
        UniqueConstraint("card_id", "date", name="uq_card_snapshots_card_id_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(
        ForeignKey("cards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
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
    type: Mapped[str] = mapped_column(String, nullable=False)  # "kjøp" | "salg"
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
    # pattern as date/platform above) -- lets Historikk show a "registrert
    # vs. avtalt" diff per purchase, so missing normal-print cards (priced
    # individually later, never bulk-estimated) show up as an unaccounted
    # remainder instead of silently vanishing.
    purchase_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Shipping cost for the whole purchase -- same redundant-per-row pattern
    # as purchase_total, and subtracted alongside it when computing the diff
    # above, so shipping doesn't masquerade as an unpriced card.
    purchase_shipping: Mapped[float | None] = mapped_column(Float, nullable=True)

    card: Mapped[Card] = relationship(back_populates="transactions")


class SetReleaseOrder(Base):
    """Lookup table for chronological (release-date) sorting of sets.

    Empty by default -- the ~100-row table from the Excel work needs to be
    supplied separately (see README) to enable release-order sorting.
    Without rows here, the app falls back to alphabetical (series, set).
    Grows over time as new sets get released: a (series, set) missing here
    sorts after every known set rather than guessing (see app.py's
    UNKNOWN_RELEASE_RANK), and should get a real row added once its actual
    release date is known -- never guessed.
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
