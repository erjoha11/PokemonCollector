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
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

import card_images
import constants
from models import Binder, Card, Collection, ImportLog

MY_COLLECTION_CATEGORY = constants.MY_COLLECTION_CATEGORY

# One network round-trip per card without a cached image is too slow for a
# large first-time import (and risks exceeding Vercel's serverless function
# timeout on the cron/manual sync route) -- capped per import call instead.
# Any card left without an image this run picks up again on the next sync,
# since the condition below is "still missing one", not "just created".
_MAX_IMAGE_LOOKUPS_PER_IMPORT = 25


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
    """Parse a Dex "Price" cell into a float.

    Dex exports the price with a currency prefix and locale-dependent
    formatting, e.g. "kr 0,48" (Norwegian: comma decimal, space thousands)
    or plain "150.5". Strip everything but the digits/separators, then
    figure out which of "," or "." is the decimal point from whichever
    appears last (European "1.234,56" vs US "1,234.56"); a lone "," is
    treated as a decimal point (matches the Norwegian kr format above).
    """
    if raw is None:
        return None
    raw = raw.strip().replace(" ", "").replace("\xa0", "")
    if not raw:
        return None
    match = re.search(r"-?[\d.,]+", raw)
    if not match:
        return None
    number = match.group(0)
    if "," in number and "." in number:
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        number = number.replace(",", ".")
    try:
        return float(number)
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


def _parse_number_int(number: str | None) -> int | None:
    """Extract a sortable integer from Dex's "Number" cell (e.g. "109/189"
    -> 109, "SWSH175/307" -> 175) so cards within a set sort in printed
    order (1, 2, ..., 10) instead of alphabetically ("1", "10", "2", ...).
    """
    if not number:
        return None
    left = number.split("/")[0]
    digits = re.sub(r"\D", "", left)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _notes_from_row(row: dict) -> str | None:
    parts = [row.get(f"Note {i}", "").strip() for i in range(1, 6)]
    parts = [p for p in parts if p]
    return "; ".join(parts) if parts else None


def _decode_csv_bytes(content: bytes) -> str:
    """Decode a Dex CSV export, whose encoding varies by export path.

    Dex's in-app CSV export writes UTF-16LE with a BOM; files that reach us
    some other way (manual save-as, re-export) may be plain UTF-8. Detect
    from the BOM when present rather than assuming either way; fall back
    from UTF-8 to UTF-16 if the former fails to decode.
    """
    if content.startswith(b"\xff\xfe") or content.startswith(b"\xfe\xff"):
        return content.decode("utf-16")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("utf-16")


def _parse_csv(content: bytes) -> list[dict]:
    text = _decode_csv_bytes(content)
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
    source: str = "manual",
) -> ImportResult:
    """Import one or more Dex CSV exports as a single sync.

    `files` is a list of (filename, raw_bytes) tuples. Every category found
    across all files is processed together, so passing the main export and
    the Vintage export in one call (as every sync should) merges correctly
    without either one clobbering the other's untouched data.

    `source` ("manual" | "dropbox" | "cron") is only used to label the
    ImportLog row this call writes -- see _log_import below.
    """
    today = today or dt.date.today()
    result = ImportResult()

    rows_by_category: dict[str, list[dict]] = {}
    for filename, content in files:
        try:
            rows = _parse_csv(content)
        except UnicodeDecodeError as exc:
            result.warnings.append(
                f"{filename}: kunne ikke lese filen som CSV (feil tegnsett) -- {exc}. "
                f"{len(content)} bytes, first bytes: {content[:20]!r}."
            )
            continue
        for row in rows:
            category = (row.get("Category") or "").strip()
            if not category:
                result.warnings.append(f"{filename}: rad uten Category-verdi hoppet over.")
                continue
            rows_by_category.setdefault(category, []).append(row)

    # --- 1. My Collection defines the physical inventory ground truth. ---
    # Dex's "Id" alone is not a unique physical card: the same Id appears
    # once per Variant the user owns (e.g. a card's "Normal" and "Poké Ball
    # Holo" prints are two separate rows with the same Id, each a distinct
    # physical card). (Id, Variant) is the real natural key throughout.
    my_collection_rows = rows_by_category.pop(MY_COLLECTION_CATEGORY, [])
    seen_keys: set[tuple[str, str | None]] = set()

    if my_collection_rows:
        row_ids = {
            (row.get("Id") or "").strip() for row in my_collection_rows if (row.get("Id") or "").strip()
        }
        existing = db.query(Card).filter(Card.card_id.in_(row_ids)).all() if row_ids else []
        cards_by_key: dict[tuple[str, str | None], Card] = {(c.card_id, c.variant): c for c in existing}
        image_lookup_budget = _MAX_IMAGE_LOOKUPS_PER_IMPORT

        for row in my_collection_rows:
            card_id = (row.get("Id") or "").strip()
            if not card_id:
                result.warnings.append("My Collection: rad uten Id hoppet over.")
                continue
            variant = (row.get("Variant") or "").strip() or None
            key = (card_id, variant)
            seen_keys.add(key)

            card = cards_by_key.get(key)
            is_new = card is None
            if is_new:
                card = Card(card_id=card_id, variant=variant, created_at=dt.datetime.utcnow())
                db.add(card)
                cards_by_key[key] = card

            card.name = (row.get("Name") or "").strip()
            card.number = (row.get("Number") or "").strip() or None
            card.number_int = _parse_number_int(card.number)
            card.series = (row.get("Series") or "").strip() or None
            card.set = (row.get("Set") or "").strip() or None
            card.language = (row.get("Locale") or "").strip() or None
            card.rarity = (row.get("Rarity") or "").strip() or None
            card.illustrator = (row.get("Illustrator") or "").strip() or None
            card.reference_price = _parse_price(row.get("Price"))
            card.qty = _parse_qty(row.get("Quantity"))
            notes = _notes_from_row(row)
            if notes:
                card.notes = notes
            card.flagged_missing_since = None  # it's back, un-flag it

            if card.image_url is None and image_lookup_budget > 0:
                card.image_url = card_images.fetch_image_url(card.name, card.set, card.number)
                image_lookup_budget -= 1

            if is_new:
                result.cards_created += 1
            else:
                result.cards_updated += 1

        # Cards previously known but absent from this My Collection export.
        missing = [c for c in db.query(Card).all() if (c.card_id, c.variant) not in seen_keys]
        for card in missing:
            if full_load:
                db.delete(card)
                result.cards_deleted += 1
            elif card.flagged_missing_since is None:
                card.flagged_missing_since = today
                result.cards_flagged_missing += 1

        db.flush()

    # --- 2. Everything else: binders and collections, keyed by category. ---
    def _cards_by_key(keys: set[tuple[str, str | None]]) -> dict[tuple[str, str | None], Card]:
        if not keys:
            return {}
        ids = {k[0] for k in keys}
        found = db.query(Card).filter(Card.card_id.in_(ids)).all()
        return {(c.card_id, c.variant): c for c in found if (c.card_id, c.variant) in keys}

    for category, rows in rows_by_category.items():
        if constants.is_excluded_category(category):
            continue  # Wishlist / 151 Fullarts * -- never touched.

        row_keys = {
            ((r.get("Id") or "").strip(), (r.get("Variant") or "").strip() or None)
            for r in rows
            if (r.get("Id") or "").strip()
        }
        cards_by_key = _cards_by_key(row_keys)
        for card_id, variant in row_keys:
            if (card_id, variant) not in cards_by_key:
                variant_label = f" ({variant})" if variant else ""
                result.warnings.append(
                    f"{category}: kort med Id '{card_id}'{variant_label} finnes ikke i databasen "
                    "(mangler i My Collection-eksporten) -- hoppet over."
                )

        matched_cards = list(cards_by_key.values())

        if constants.is_binder_category(category):
            binder = db.query(Binder).filter(Binder.name == category).one_or_none()
            if binder is None:
                binder = Binder(name=category)
                db.add(binder)
                db.flush()
            # This category is present in the sync: fully replace membership.
            for card in db.query(Card).filter(Card.binder_id == binder.id).all():
                if (card.card_id, card.variant) not in cards_by_key:
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

    _apply_auto_binder_rules(db, result)
    _log_import(db, result, source, [name for name, _ in files])

    db.commit()
    return result


def _log_import(db: Session, result: ImportResult, source: str, filenames: list[str]) -> None:
    db.add(
        ImportLog(
            ran_at=dt.datetime.utcnow(),
            source=source,
            files=", ".join(filenames) or None,
            cards_created=result.cards_created,
            cards_updated=result.cards_updated,
            cards_flagged_missing=result.cards_flagged_missing,
            cards_deleted=result.cards_deleted,
            collections_touched=", ".join(sorted(result.collections_touched)) or None,
            binders_touched=", ".join(sorted(result.binders_touched)) or None,
            warnings_count=len(result.warnings),
        )
    )


def _apply_auto_binder_rules(db: Session, result: ImportResult) -> None:
    """Fill in `binder_id` for cards whose collection membership implies one
    specific physical binder (see constants.AUTO_BINDER_RULES: illustrator
    collections -> Illustrator Binder, 151 -> 151 Binder, Vintage -> Vintage
    Binder). Only fills cards that have no binder yet -- an explicit Dex
    Binder-category export (e.g. Tradebinder, handled above) always wins,
    since that reflects where the card is actually, physically placed.
    """
    for collection_names, binder_name in constants.AUTO_BINDER_RULES:
        cards = (
            db.query(Card)
            .join(Card.collections)
            .filter(Collection.name.in_(collection_names), Card.binder_id.is_(None))
            .distinct()
            .all()
        )
        if not cards:
            continue
        binder = db.query(Binder).filter(Binder.name == binder_name).one_or_none()
        if binder is None:
            binder = Binder(name=binder_name)
            db.add(binder)
            db.flush()
        for card in cards:
            card.binder_id = binder.id
        result.binders_touched.add(binder_name)
