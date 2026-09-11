"""Import logic for Dex CSV exports.

A Dex export is one CSV per Dex folder/category (semicolon separated):

    Type;Category;Locale;Series;Set;Id;Number;Name;Variant;Rarity;
    Illustrator;Quantity;Price;Note 1;Note 2;Note 3;Note 4;Note 5

Every sync should include both the main export ("My Collection") and the
Vintage Collection export together, plus optionally any other category
exports (illustrator collections, binders, "Scarlet & Violet: 151 JP/KR",
etc). See constants.py for the routing rules; see the app README for the
full rationale (it's hard-won from the Excel version this replaces).
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

import constants
from models import Binder, Card, Collection

MY_COLLECTION_CATEGORY = constants.MY_COLLECTION_CATEGORY


@dataclass
class ImportResult:
    cards_created: int = 0
    cards_updated: int = 0
    cards_flagged_missing: int = 0
    cards_deleted: int = 0
    collections_touched: set[str] = field(default_factory=set)
    binders_touched: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


def _parse_price(raw: str | None) -> float | None:
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    raw = raw.replace(" ", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_qty(raw: str | None) -> int:
    if raw is None:
        return 0
    raw = raw.strip()
    if not raw:
        return 0
    try:
        return int(float(raw))
    except ValueError:
        return 0


def _notes_from_row(row: dict) -> str | None:
    parts = [row.get(f"Note {i}", "").strip() for i in range(1, 6)]
    parts = [p for p in parts if p]
    return "; ".join(parts) if parts else None


def _parse_csv(content: bytes) -> list[dict]:
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    rows = []
    for row in reader:
        # Skip fully blank lines.
        if not any((v or "").strip() for v in row.values()):
            continue
        rows.append(row)
    return rows


def import_dex_csv_files(
    db: Session,
    files: list[tuple[str, bytes]],
    full_load: bool = False,
    today: dt.date | None = None,
) -> ImportResult:
    """Import one or more Dex CSV exports as a single sync.

    `files` is a list of (filename, raw_bytes) tuples. Every category found
    across all files is processed together, so passing the main export and
    the Vintage export in one call (as every sync should) merges correctly
    without either one clobbering the other's untouched data.
    """
    today = today or dt.date.today()
    result = ImportResult()

    rows_by_category: dict[str, list[dict]] = {}
    for filename, content in files:
        try:
            rows = _parse_csv(content)
        except UnicodeDecodeError:
            result.warnings.append(f"{filename}: kunne ikke lese filen som CSV (feil tegnsett).")
            continue
        for row in rows:
            category = (row.get("Category") or "").strip()
            if not category:
                result.warnings.append(f"{filename}: rad uten Category-verdi hoppet over.")
                continue
            rows_by_category.setdefault(category, []).append(row)

    # --- 1. My Collection defines the physical inventory ground truth. ---
    my_collection_rows = rows_by_category.pop(MY_COLLECTION_CATEGORY, [])
    seen_card_ids: set[str] = set()

    if my_collection_rows:
        for row in my_collection_rows:
            card_id = (row.get("Id") or "").strip()
            if not card_id:
                result.warnings.append("My Collection: rad uten Id hoppet over.")
                continue
            seen_card_ids.add(card_id)

            card = db.query(Card).filter(Card.card_id == card_id).one_or_none()
            is_new = card is None
            if is_new:
                card = Card(card_id=card_id)
                db.add(card)

            card.name = (row.get("Name") or "").strip()
            card.number = (row.get("Number") or "").strip() or None
            card.series = (row.get("Series") or "").strip() or None
            card.set = (row.get("Set") or "").strip() or None
            card.variant = (row.get("Variant") or "").strip() or None
            card.rarity = (row.get("Rarity") or "").strip() or None
            card.illustrator = (row.get("Illustrator") or "").strip() or None
            card.reference_price = _parse_price(row.get("Price"))
            card.qty = _parse_qty(row.get("Quantity"))
            notes = _notes_from_row(row)
            if notes:
                card.notes = notes
            card.flagged_missing_since = None  # it's back, un-flag it

            if is_new:
                result.cards_created += 1
            else:
                result.cards_updated += 1

        # Cards previously known but absent from this My Collection export.
        missing = db.query(Card).filter(~Card.card_id.in_(seen_card_ids)).all()
        for card in missing:
            if full_load:
                db.delete(card)
                result.cards_deleted += 1
            elif card.flagged_missing_since is None:
                card.flagged_missing_since = today
                result.cards_flagged_missing += 1

        db.flush()

    # --- 2. Everything else: binders and collections, keyed by category. ---
    def _cards_by_id(card_ids: set[str]) -> dict[str, Card]:
        if not card_ids:
            return {}
        found = db.query(Card).filter(Card.card_id.in_(card_ids)).all()
        return {c.card_id: c for c in found}

    for category, rows in rows_by_category.items():
        if constants.is_excluded_category(category):
            continue  # Wishlist / 151 Fullarts * -- never touched.

        row_card_ids = {(r.get("Id") or "").strip() for r in rows if (r.get("Id") or "").strip()}
        cards_by_id = _cards_by_id(row_card_ids)
        for card_id in row_card_ids:
            if card_id not in cards_by_id:
                result.warnings.append(
                    f"{category}: kort med Id '{card_id}' finnes ikke i databasen "
                    "(mangler i My Collection-eksporten) -- hoppet over."
                )

        matched_cards = list(cards_by_id.values())

        if constants.is_binder_category(category):
            binder = db.query(Binder).filter(Binder.name == category).one_or_none()
            if binder is None:
                binder = Binder(name=category)
                db.add(binder)
                db.flush()
            # This category is present in the sync: fully replace membership.
            for card in db.query(Card).filter(Card.binder_id == binder.id).all():
                if card.card_id not in cards_by_id:
                    card.binder_id = None
            for card in matched_cards:
                card.binder_id = binder.id
            result.binders_touched.add(category)
        else:
            collection = db.query(Collection).filter(Collection.name == category).one_or_none()
            if collection is None:
                collection = Collection(name=category, priority_rank=constants.priority_rank_for(category))
                db.add(collection)
                db.flush()
            # This category is present in the sync: fully replace membership
            # (this is what protects e.g. Vintage Collection tags from being
            # wiped when a sync doesn't include a fresh Vintage export --
            # a category absent from rows_by_category is never touched).
            current_members = set(collection.cards)
            new_members = set(matched_cards)
            for card in current_members - new_members:
                card.collections.remove(collection)
            for card in new_members - current_members:
                card.collections.append(collection)
            result.collections_touched.add(category)

    db.commit()
    return result
