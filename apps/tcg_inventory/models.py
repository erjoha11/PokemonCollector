"""SQLAlchemy models for the TCG inventory app.

Computed fields (`duplicates`, `total_value`, `unique_value`) are Python
properties, never persisted columns -- a stored/derived-value mismatch was a
real bug in the Excel version this app replaces.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import Date, Float, ForeignKey, Integer, String, Table, Column
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

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)

    name: Mapped[str] = mapped_column(String, nullable=False)
    number: Mapped[str | None] = mapped_column(String, nullable=True)
    series: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    set: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    variant: Mapped[str | None] = mapped_column(String, nullable=True)
    rarity: Mapped[str | None] = mapped_column(String, nullable=True)
    illustrator: Mapped[str | None] = mapped_column(String, nullable=True)

    reference_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    binder_id: Mapped[int | None] = mapped_column(ForeignKey("binders.id"), nullable=True)
    classification: Mapped[str | None] = mapped_column(String, nullable=True)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    flagged_missing_since: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

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


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)  # "kjøp" | "salg"
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    platform: Mapped[str | None] = mapped_column(String, nullable=True)
    fees: Mapped[float | None] = mapped_column(Float, nullable=True)

    card: Mapped[Card] = relationship(back_populates="transactions")


class SetReleaseOrder(Base):
    """Lookup table for chronological (release-date) sorting of sets.

    Empty by default -- the ~100-row table from the Excel work needs to be
    supplied separately (see README) to enable release-order sorting.
    Without rows here, the app falls back to alphabetical (series, set).
    """

    __tablename__ = "set_release_order"

    id: Mapped[int] = mapped_column(primary_key=True)
    series: Mapped[str] = mapped_column(String, nullable=False)
    set: Mapped[str] = mapped_column(String, nullable=False)
    release_rank: Mapped[int] = mapped_column(Integer, nullable=False)
