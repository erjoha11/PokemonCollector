"""Cards the Dex sync has flagged missing: listing them on /sync-status and
deleting one that was registered in error.

A sync never deletes a card (issue #225): a card missing from the Dex
export only gets `Card.flagged_missing_since` set (importer.py), because
deleting a card cascades to its transactions and snapshots, and Dex can't
give those back. A flagged card still counts everywhere until it's removed
here.

`delete_missing_card` is the only way the app deletes a card, and it is
deliberately narrow: the card must be flagged missing, have no
transactions (order rows) and be in no listing (`listing_cards`). The ones
that do have history stay listed with no delete action -- deleting them
would destroy order history.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Card, CardSnapshot, Transaction, listing_cards


@dataclass
class MissingCard:
    card: Card
    tx_count: int
    listing_count: int

    @property
    def deletable(self) -> bool:
        return self.tx_count == 0 and self.listing_count == 0


def _tx_counts(db: Session, card_ids: list[int]) -> dict[int, int]:
    rows = (
        db.query(Transaction.card_id, func.count(Transaction.id))
        .filter(Transaction.card_id.in_(card_ids))
        .group_by(Transaction.card_id)
        .all()
    )
    return dict(rows)


def _listing_counts(db: Session, card_ids: list[int]) -> dict[int, int]:
    rows = (
        db.query(listing_cards.c.card_id, func.count(listing_cards.c.listing_id))
        .filter(listing_cards.c.card_id.in_(card_ids))
        .group_by(listing_cards.c.card_id)
        .all()
    )
    return dict(rows)


def flagged_cards(db: Session) -> list[MissingCard]:
    """Every card with `flagged_missing_since` set, oldest flag first."""
    cards = (
        db.query(Card)
        .filter(Card.flagged_missing_since.isnot(None))
        .order_by(Card.flagged_missing_since, Card.name, Card.id)
        .all()
    )
    ids = [c.id for c in cards]
    if not ids:
        return []
    txs = _tx_counts(db, ids)
    listings = _listing_counts(db, ids)
    return [MissingCard(c, txs.get(c.id, 0), listings.get(c.id, 0)) for c in cards]


@dataclass
class DeleteResult:
    ok: bool
    message: str


def _label(card: Card) -> str:
    parts = [card.name]
    if card.set:
        parts.append(card.set)
    if card.number:
        parts.append(f"#{card.number}")
    if card.variant:
        parts.append(card.variant)
    return " · ".join(parts)


def delete_missing_card(db: Session, card_pk: int, confirmed: bool) -> DeleteResult:
    """Delete one card registered in error, or refuse and say why.

    Deletes only when all of these hold (checked here, inside the same
    transaction as the delete): the user ticked the confirmation, the card
    exists, it is flagged missing from Dex, it has zero transactions and it
    is in zero listings. Its collection tags (card_collections), value
    snapshots (card_snapshots) and prices (card_prices) go with it --
    intended for a card that should never have been registered.

    Snapshots are deleted explicitly rather than left to the DB's
    ON DELETE CASCADE, which SQLite only honours with `PRAGMA foreign_keys`
    on (this app doesn't turn it on). The ORM removes the card_collections
    rows (secondary) and card_prices (delete-orphan cascade) itself.
    Commits on success; changes nothing on refusal.
    """
    card = db.get(Card, card_pk)
    if card is None:
        return DeleteResult(False, "That card no longer exists -- it may already have been deleted.")
    label = _label(card)
    if not confirmed:
        return DeleteResult(False, f"Not deleted: tick the confirmation box to delete {label}.")
    if card.flagged_missing_since is None:
        return DeleteResult(
            False, f"Not deleted: {label} is not flagged missing from Dex. Only flagged cards can be deleted here."
        )
    tx_count = _tx_counts(db, [card.id]).get(card.id, 0)
    listing_count = _listing_counts(db, [card.id]).get(card.id, 0)
    if tx_count or listing_count:
        reasons = []
        if tx_count:
            reasons.append(f"{tx_count} order row{'' if tx_count == 1 else 's'}")
        if listing_count:
            reasons.append(f"{listing_count} listing{'' if listing_count == 1 else 's'}")
        return DeleteResult(
            False,
            f"Not deleted: {label} has {' and '.join(reasons)}. Deleting it would destroy that history.",
        )
    db.query(CardSnapshot).filter(CardSnapshot.card_id == card.id).delete(synchronize_session=False)
    db.delete(card)
    db.commit()
    return DeleteResult(True, f"Deleted {label}.")
