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

from sqlalchemy.orm import Session, selectinload

import card_images
import constants
import masterdata
import pricing
from db import get_or_create_set
from models import Binder, Card, Collection, ImportLog, Set

MY_COLLECTION_CATEGORY = constants.MY_COLLECTION_CATEGORY

# One network round-trip per card without a cached image is too slow for a
# large first-time import (and risks exceeding Vercel's serverless function
# timeout on the cron/manual sync route) -- capped per import call instead.
# Any card left without an image this run picks up again on the next sync,
# since the condition below is "still missing one", not "just created".
_MAX_IMAGE_LOOKUPS_PER_IMPORT = 25

# The sync used to look up up to 25 pokemontcg.io prices per run as well.
# Since issue #349 the daily price cron (price_refresh.py) fetches every
# international card's price by ID in a few batch requests, so the sync
# only writes the Dex price and fills images.

# Sync circuit breaker (issue #225). A sync that would newly flag more than
# this share of the existing cards as "missing from My Collection" is almost
# certainly a truncated/broken export rather than real sales, so it aborts
# with no changes. MISSING_ABORT_MIN_CARDS keeps the check from tripping on
# tiny collections (flagging 1 of 3 cards is 33% but perfectly normal).
# Flagging is non-destructive, so a manual sync can override it after a
# genuine large clear-out (allow_mass_missing=True); the cron never does.
MISSING_ABORT_FRACTION = 0.05
MISSING_ABORT_MIN_CARDS = 10


# How many card names an ImportAborted carries as a sample (issue #274).
ABORT_SAMPLE_NAMES = 10


class ImportAborted(Exception):
    """Raised before anything is written when a sync fails a safety check
    (issue #225). The message is user-facing. `overridable` is True only for
    the mass-missing check -- an empty My Collection can't be overridden.

    The mass-missing check also fills in structured fields (issue #274), so
    callers never parse the message: `newly_missing` (cards this sync would
    newly flag), `total_cards` (cards in the database), `limit_fraction`
    (MISSING_ABORT_FRACTION) and `sample_names` (the first
    ABORT_SAMPLE_NAMES of those cards' names, sorted). All None/empty for
    the empty-My-Collection abort."""

    def __init__(
        self,
        message: str,
        overridable: bool = False,
        *,
        newly_missing: int | None = None,
        total_cards: int | None = None,
        limit_fraction: float | None = None,
        sample_names: list[str] | None = None,
    ):
        super().__init__(message)
        self.overridable = overridable
        self.newly_missing = newly_missing
        self.total_cards = total_cards
        self.limit_fraction = limit_fraction
        self.sample_names = list(sample_names or [])


@dataclass
class ImportResult:
    cards_created: int = 0
    cards_updated: int = 0
    cards_flagged_missing: int = 0
    collections_touched: set[str] = field(default_factory=set)
    binders_touched: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    # Quantity-0 rows for cards that aren't in the database (Dex's "all
    # variants" export lists every unowned variant, issue #340). Skipped, not
    # warned about: My Collection rows and other categories' rows alike.
    unowned_rows_skipped: int = 0


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
    today: dt.date | None = None,
    source: str = "manual",
    allow_mass_missing: bool = False,
    file_dates: dict[str, dt.date] | None = None,
) -> ImportResult:
    """Import one or more Dex CSV exports as a single sync.

    `files` is a list of (filename, raw_bytes) tuples. Every category found
    across all files is processed together, so passing the main export and
    the Vintage export in one call (as every sync should) merges correctly
    without either one clobbering the other's untouched data.

    `file_dates` (filename -> export date; the Dropbox sync passes each
    file's `client_modified`) does two things (issue #351):

    - The `dex` prices are dated at the My Collection file's export date,
      not at `today`. Re-reading the same export every day is not a new
      price, so an unchanged export goes stale after pricing.FRESH_DAYS and
      the chain falls through to the live sources. A file with no date (a
      direct call, no Dropbox) keeps `today`; a date in the future is
      capped at `today`.
    - A category found in more than one file is read from the newest file
      only, with a warning. Without dates the rows are merged as before.

    `source` ("manual" | "cron") is only used to label the
    ImportLog row this call writes -- see _log_import below.

    Never deletes a card: one missing from My Collection is only flagged
    (the old `full_load` hard-delete path was removed in issue #225, since
    deleting a card took its transactions and value history with it).
    Raises ImportAborted, before writing anything, if the export looks
    broken -- see _check_circuit_breaker.
    """
    today = today or dt.date.today()
    result = ImportResult()

    file_dates = file_dates or {}
    # category -> filename -> rows, in file order.
    rows_by_category_file: dict[str, dict[str, list[dict]]] = {}
    empty_files: list[str] = []
    for filename, content in files:
        try:
            rows = _parse_csv(content)
        except UnicodeDecodeError as exc:
            result.warnings.append(
                f"{filename}: kunne ikke lese filen som CSV (feil tegnsett) -- {exc}. "
                f"{len(content)} bytes, first bytes: {content[:20]!r}."
            )
            continue
        if not rows:
            empty_files.append(filename)
        for row in rows:
            category = (row.get("Category") or "").strip()
            if not category:
                result.warnings.append(f"{filename}: rad uten Category-verdi hoppet over.")
                continue
            rows_by_category_file.setdefault(category, {}).setdefault(filename, []).append(row)

    rows_by_category, category_dates = _pick_category_files(rows_by_category_file, file_dates, result)
    # The date the dex prices are stamped with (issue #351): My Collection's
    # export date, never later than today.
    dex_price_date = min(category_dates.get(MY_COLLECTION_CATEGORY) or today, today)

    # --- 1. My Collection defines the physical inventory ground truth. ---
    # Dex's "Id" alone is not a unique physical card: the same Id appears
    # once per Variant the user owns (e.g. a card's "Normal" and "Poké Ball
    # Holo" prints are two separate rows with the same Id, each a distinct
    # physical card). (Id, Variant) is the real natural key throughout.
    my_collection_rows = rows_by_category.pop(MY_COLLECTION_CATEGORY, [])
    seen_keys: set[tuple[str, str | None]] = set()

    _check_circuit_breaker(db, my_collection_rows, empty_files, allow_mass_missing)

    if my_collection_rows:
        row_ids = {
            (row.get("Id") or "").strip() for row in my_collection_rows if (row.get("Id") or "").strip()
        }
        existing = (
            db.query(Card).options(selectinload(Card.prices)).filter(Card.card_id.in_(row_ids)).all()
            if row_ids
            else []
        )
        cards_by_key: dict[tuple[str, str | None], Card] = {(c.card_id, c.variant): c for c in existing}
        image_lookup_budget = _MAX_IMAGE_LOOKUPS_PER_IMPORT
        # Reused across every row in this import call so get_or_create_set()
        # only queries/creates once per distinct (series, set) pair seen in
        # this sync, not once per card -- see get_or_create_set()'s docstring
        # (db.py) and issue #134.
        sets_cache: dict[tuple[str, str], Set] = {}
        masters_cache: dict = {}
        # Dex's Price cell per card, written to card_prices in bulk after the
        # loop (pricing.bulk_record_prices) -- one statement per chunk, not
        # one UPDATE per card per sync (issue #210, and #193's timeout).
        dex_prices: dict[Card, float] = {}
        imported_cards: list[Card] = []

        for row in my_collection_rows:
            card_id = (row.get("Id") or "").strip()
            if not card_id:
                result.warnings.append("My Collection: rad uten Id hoppet over.")
                continue
            variant = (row.get("Variant") or "").strip() or None
            key = (card_id, variant)
            qty = _parse_qty(row.get("Quantity"))

            card = cards_by_key.get(key)
            if card is None and qty == 0:
                # An unowned variant from Dex's "all variants" export (issue
                # #340): never becomes a card. A card already in the database
                # whose row drops to 0 is still updated below, as before.
                result.unowned_rows_skipped += 1
                continue
            seen_keys.add(key)
            is_new = card is None
            if is_new:
                card = Card(card_id=card_id, variant=variant, created_at=dt.datetime.utcnow())
                db.add(card)
                cards_by_key[key] = card
            imported_cards.append(card)

            card.name = (row.get("Name") or "").strip()
            card.number = (row.get("Number") or "").strip() or None
            card.number_int = _parse_number_int(card.number)
            card.series = (row.get("Series") or "").strip() or None
            card.set = (row.get("Set") or "").strip() or None
            if card.series and card.set:
                # Link Card.set_id inline at import time rather than relying
                # solely on db.py's init_db()-time _backfill_sets() to catch
                # up on the next restart (issue #134) -- a set seen for the
                # first time gets a real, unranked Set row here (never
                # silently skipped); release_rank/total_cards fill in later
                # via set_sync.py or a manual edit.
                card.set_id = get_or_create_set(db, card.series, card.set, cache=sets_cache).id
            else:
                card.set_id = None
            if card.master_card_id is None and card.master_card is None:
                # (card_id, variant) never changes for a Card row, so its
                # master identity only needs linking once -- see
                # masterdata.py.
                masterdata.link_card(db, card, cache=masters_cache)
            # Notes follow Dex: a note removed there is cleared here too (it
            # used to be kept forever). Nothing in the app edits Card.notes.
            card.notes = _notes_from_row(row)
            # A "KR" note makes the card Korean; otherwise Dex's Locale
            # (issue #367, constants.py).
            card.language = constants.physical_language(row.get("Locale"), card.notes)
            card.rarity = (row.get("Rarity") or "").strip() or None
            card.illustrator = (row.get("Illustrator") or "").strip() or None
            # The `dex` source price (issue #210). An empty/unparseable Price
            # cell keeps the last known one -- it used to overwrite
            # reference_price with None. reference_price is now a mirror of
            # the dex card_prices row.
            dex_price = _parse_price(row.get("Price"))
            if dex_price is not None:
                card.reference_price = dex_price
                dex_prices[card] = dex_price
            card.qty = qty
            card.flagged_missing_since = None  # it's back, un-flag it

            if card.image_url is None and image_lookup_budget > 0:
                # By Dex's own card_id first (see card_images.
                # fetch_image_by_card_id); the name search only for
                # non-Japanese prints -- Japanese ones aren't in that API, so
                # its hit could only be the wrong card. No price lookups here
                # (issue #349): the daily price cron fetches pokemontcg.io
                # prices by ID, see price_refresh.py.
                card.image_url = card_images.fetch_image_by_card_id(card.card_id, card.name, card.number) or (
                    None
                    if (card.card_id or "").startswith("jpn_")
                    else card_images.search_image_url(card.name, card.set, card.number)
                )
                image_lookup_budget -= 1

            if is_new:
                result.cards_created += 1
            else:
                result.cards_updated += 1

        # Cards previously known but absent from this My Collection export.
        missing = [c for c in db.query(Card).all() if (c.card_id, c.variant) not in seen_keys]
        for card in missing:
            if card.flagged_missing_since is None:
                card.flagged_missing_since = today
                result.cards_flagged_missing += 1

        db.flush()
        pricing.bulk_record_prices(
            db, pricing.SOURCE_DEX, {card.id: price for card, price in dex_prices.items()}, dex_price_date
        )
        pricing.resolve_cards(db, [card.id for card in imported_cards], today=today)

    # --- 2. Everything else: binders and collections, keyed by category. ---
    def _cards_by_key(keys: set[tuple[str, str | None]]) -> dict[tuple[str, str | None], Card]:
        if not keys:
            return {}
        ids = {k[0] for k in keys}
        found = db.query(Card).filter(Card.card_id.in_(ids)).all()
        return {(c.card_id, c.variant): c for c in found if (c.card_id, c.variant) in keys}

    # Status categories (Incoming, issue #382) never become a collection or
    # binder. Authoritative only in a sync with My Collection rows; without
    # them the in-transit status is left untouched.
    incoming_rows = rows_by_category.pop(constants.INCOMING_CATEGORY, None)
    for category in [c for c in rows_by_category if constants.is_status_category(c)]:
        rows_by_category.pop(category)  # no other status category yet
    if my_collection_rows:
        _apply_in_transit(db, incoming_rows, category_dates, today, result)

    for category, rows in rows_by_category.items():
        if constants.is_excluded_category(category):
            continue  # Wishlist / 151 Fullarts * -- never touched.

        row_keys = {
            ((r.get("Id") or "").strip(), (r.get("Variant") or "").strip() or None)
            for r in rows
            if (r.get("Id") or "").strip()
        }
        # Keys with at least one row of quantity > 0 in this category.
        owned_keys = {
            ((r.get("Id") or "").strip(), (r.get("Variant") or "").strip() or None)
            for r in rows
            if (r.get("Id") or "").strip() and _parse_qty(r.get("Quantity")) > 0
        }
        cards_by_key = _cards_by_key(row_keys)
        for card_id, variant in row_keys:
            if (card_id, variant) not in cards_by_key:
                if (card_id, variant) not in owned_keys:
                    # Unowned variant ("all variants" export, issue #340):
                    # counted, not one warning per row.
                    result.unowned_rows_skipped += 1
                    continue
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


INCOMING_CLEARED_WARNING = (
    "Incoming export missing or older than My Collection; in-transit status cleared"
)


def _apply_in_transit(
    db: Session,
    incoming_rows: list[dict] | None,
    category_dates: dict[str, dt.date | None],
    today: dt.date,
    result: ImportResult,
) -> None:
    """Set every card's "On the way" status from the Incoming export (issue
    #382). Called only in a sync with My Collection rows, after they're
    imported, so `card.qty` is this export's.

    The emptied-folder rule: Dex exports no Incoming rows once the folder is
    empty, and the cron reads every CSV in the Dropbox folder, so an old
    Incoming export could otherwise be re-applied forever. So Incoming counts
    only when present and dated the same day as My Collection or later (an
    undated file -- a manual upload -- counts when present). Otherwise it's
    treated as empty: every in-transit field is cleared, with a warning when
    that actually clears a card. A stale status would hide cards from
    selling indefinitely; a cleared one only makes them look in hand, as
    before #382.

    A qty > 0 row sets `in_transit_qty = min(row qty, card.qty)` and keeps
    `in_transit_since` (else today). Qty-0 rows (won, not paid) are ignored
    and never create cards. Every other card is cleared.
    """
    mc_date = category_dates.get(MY_COLLECTION_CATEGORY)
    in_date = category_dates.get(constants.INCOMING_CATEGORY)
    stale = incoming_rows is not None and mc_date is not None and in_date is not None and in_date < mc_date
    usable = incoming_rows is not None and not stale

    in_transit: dict[tuple[str, str | None], int] = {}
    for row in incoming_rows if usable else []:
        card_id = (row.get("Id") or "").strip()
        qty = _parse_qty(row.get("Quantity"))
        if not card_id or qty <= 0:
            continue
        key = (card_id, (row.get("Variant") or "").strip() or None)
        in_transit[key] = max(in_transit.get(key, 0), qty)

    tagged: set[int] = set()
    if in_transit:
        ids = {k[0] for k in in_transit}
        found = {(c.card_id, c.variant): c for c in db.query(Card).filter(Card.card_id.in_(ids))}
        for key, row_qty in in_transit.items():
            card = found.get(key)
            if card is None:
                variant_label = f" ({key[1]})" if key[1] else ""
                result.warnings.append(
                    f"{constants.INCOMING_CATEGORY}: kort med Id '{key[0]}'{variant_label} finnes ikke i "
                    "databasen (mangler i My Collection-eksporten) -- hoppet over."
                )
                continue
            qty = min(row_qty, card.qty or 0)
            if qty <= 0:
                continue
            card.in_transit_qty = qty
            if card.in_transit_since is None:
                card.in_transit_since = today
            tagged.add(card.id)

    cleared = 0
    for card in db.query(Card).filter(
        (Card.in_transit_qty.is_not(None)) | (Card.in_transit_since.is_not(None))
    ):
        if card.id in tagged:
            continue
        if card.in_transit:
            cleared += 1
        card.in_transit_qty = None
        card.in_transit_since = None
    if not usable and cleared:
        result.warnings.append(INCOMING_CLEARED_WARNING)


def _pick_category_files(
    rows_by_category_file: dict[str, dict[str, list[dict]]],
    file_dates: dict[str, dt.date],
    result: ImportResult,
) -> tuple[dict[str, list[dict]], dict[str, dt.date | None]]:
    """Each category's rows, and the export date they're from.

    A category in one file: that file's rows and date (None if undated).
    A category in several files that all have a date: only the newest file
    (the first listed on a tie), with a warning naming the ones skipped --
    reading them all let an older export's rows overwrite a newer one's
    (notes/REVIEW.md). If any of them is undated, the rows are merged as
    before and the category has no date.
    """
    rows_by_category: dict[str, list[dict]] = {}
    category_dates: dict[str, dt.date | None] = {}
    for category, by_file in rows_by_category_file.items():
        names = list(by_file)
        if len(names) == 1:
            rows_by_category[category] = by_file[names[0]]
            category_dates[category] = file_dates.get(names[0])
        elif all(name in file_dates for name in names):
            newest = max(names, key=lambda name: (file_dates[name], -names.index(name)))
            rows_by_category[category] = by_file[newest]
            category_dates[category] = file_dates[newest]
            skipped = ", ".join(f"{name} ({file_dates[name].isoformat()})" for name in names if name != newest)
            result.warnings.append(
                f"{category}: found in more than one file; read only the newest, "
                f"{newest} ({file_dates[newest].isoformat()}), skipped {skipped}."
            )
        else:
            rows_by_category[category] = [row for name in names for row in by_file[name]]
            category_dates[category] = None
    return rows_by_category, category_dates


def _check_circuit_breaker(
    db: Session,
    my_collection_rows: list[dict],
    empty_files: list[str],
    allow_mass_missing: bool,
) -> None:
    """Refuse an import that looks like a broken export, before any write
    (issue #225).

    1. No My Collection rows while some selected file had no data rows at
       all: that empty/header-only file may well be the My Collection
       export, so the sync can't be trusted. (A sync with no My Collection
       file, where every file has data, is a legitimate category-only sync
       and still runs -- it never flags anything.)
    2. The sync would newly flag more than MISSING_ABORT_FRACTION of the
       existing cards (and more than MISSING_ABORT_MIN_CARDS) as missing --
       looks like a truncated export. Overridable, see allow_mass_missing.
    """
    if not my_collection_rows and empty_files:
        raise ImportAborted(
            f"Sync aborted, nothing was changed: {', '.join(empty_files)} "
            f"has no data rows (empty or header-only), and no \"{MY_COLLECTION_CATEGORY}\" "
            "rows were found. Re-export My Collection from Dex and sync again."
        )
    if not my_collection_rows or allow_mass_missing:
        return

    seen_keys = {
        ((row.get("Id") or "").strip(), (row.get("Variant") or "").strip() or None)
        for row in my_collection_rows
        if (row.get("Id") or "").strip()
    }
    existing = db.query(Card.card_id, Card.variant, Card.flagged_missing_since, Card.name).all()
    missing_names = [
        name or card_id
        for card_id, variant, flagged, name in existing
        if flagged is None and (card_id, variant) not in seen_keys
    ]
    newly_missing = len(missing_names)
    limit = max(len(existing) * MISSING_ABORT_FRACTION, MISSING_ABORT_MIN_CARDS)
    if newly_missing > limit:
        raise ImportAborted(
            f"Sync aborted, nothing was changed: this {MY_COLLECTION_CATEGORY} export would flag "
            f"{newly_missing} of {len(existing)} cards as missing (limit: "
            f"{MISSING_ABORT_FRACTION:.0%}). That usually means a truncated or incomplete "
            "export -- re-export from Dex and sync again. If you really did remove that "
            "many cards, run the sync again with the override.",
            overridable=True,
            newly_missing=newly_missing,
            total_cards=len(existing),
            limit_fraction=MISSING_ABORT_FRACTION,
            sample_names=sorted(missing_names, key=lambda n: (n or "").casefold())[:ABORT_SAMPLE_NAMES],
        )


def _log_import(db: Session, result: ImportResult, source: str, filenames: list[str]) -> None:
    db.add(
        ImportLog(
            ran_at=dt.datetime.utcnow(),
            source=source,
            files=", ".join(filenames) or None,
            cards_created=result.cards_created,
            cards_updated=result.cards_updated,
            cards_flagged_missing=result.cards_flagged_missing,
            # No sync deletes cards any more (issue #225); the column stays
            # (init_db() is additive-only) for the historical rows.
            cards_deleted=0,
            collections_touched=", ".join(sorted(result.collections_touched)) or None,
            binders_touched=", ".join(sorted(result.binders_touched)) or None,
            warnings_count=len(result.warnings),
            job="dex-sync",
            status="ok",
            message=(
                f"Skipped {result.unowned_rows_skipped} rows with quantity 0 for cards not in the "
                "database (unowned variants)"
                if result.unowned_rows_skipped
                else None
            ),
            # One warning per line; a warning's own line breaks are flattened
            # so the split back into a list on /sync-status stays 1:1.
            warnings_text="\n".join(" ".join(w.splitlines()) for w in result.warnings) or None,
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
