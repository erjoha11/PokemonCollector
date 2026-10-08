"""The Facebook wins inbox (issue #309): what `fb_auction_watcher` sends to
`POST /inbox/fb-wins`, checked and staged in `won_items`.

The contract is the extension's (the producer's), documented in
`apps/fb_auction_watcher/docs/spec.md` "Sending wins to tcg_inventory":

    {"format": "fbaw-won", "version": 1, "sent_at": "<ISO>",
     "items": [{"external_ref", "seller", "sale_type", "ended_on", "post_url",
                "lot_url", "label", "price", "shipping_text", "payment_text",
                "paid_at", "received_at"}, ...]}

Apps never import each other, so this side re-checks every field rather than
trusting the sender. `tests/test_cross_app_won_inbox.py` (repo root) runs
`parse_payload` over the extension's committed fixture, so a contract change
on either side fails there.

Three pieces, kept apart so each is testable on its own:
- `parse_payload`: pure, raises `PayloadError` (a readable message) on
  anything it can't accept, so the route writes nothing.
- `upsert_items`: writes `won_items` only -- never transactions or cards.
  Pending rows are refreshed; registered and ignored ones are never touched.
- `pending_sales`: the Purchased tab's list, one entry per won sale.

And the link flow (slice 2), where a sale opens in the New Order cart:
- `open_sale`: one sale's pending items plus what the cart is prefilled with.
- `candidates` / `annotate_cards`: the cards each item may be, by a fuzzy
  name match, never auto-linked.
- `linkable_items` / `mark_registered`: what Register does to the items
  linked in the cart.

And keeping items pointed at their order (#317):
- `follow_order_edit`: Edit order's move/merge/split carries
  `won_items.purchase_id` along, so a registered item keeps pointing at the
  order its cards ended up in.
- An item is *open* (listed, can be opened in the cart, linked, ignored)
  when it's pending, or registered on an order that no longer has any
  transaction ("order missing").
"""
from __future__ import annotations

import datetime as dt
import difflib
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sqlalchemy import and_, exists, or_
from sqlalchemy.orm import Session

from models import Card, Transaction, WonItem

FORMAT = "fbaw-won"
SUPPORTED_VERSION = 1
SOURCE = "fbaw"

# The body cap: a few hundred wins is far beyond a real send, so anything
# bigger is a bug or abuse, refused before parsing.
MAX_BODY_BYTES = 512 * 1024
MAX_ITEMS = 1000
MAX_TEXT = 2000  # shipping/payment terms
MAX_SHORT = 500  # refs, labels, names, URLs
MAX_PRICE = 10_000_000

SALE_TYPES = {"auction", "claim", "fixed"}
STATUS_PENDING = "pending"
STATUS_REGISTERED = "registered"
STATUS_IGNORED = "ignored"

# Links are rendered as <a href>, so only Facebook https URLs get in.
_FACEBOOK_HOSTS = {"www.facebook.com", "facebook.com", "m.facebook.com", "web.facebook.com"}


class PayloadError(ValueError):
    """The payload can't be accepted; the message says why, for the sender."""


@dataclass(frozen=True)
class WonItemIn:
    external_ref: str
    seller: str | None
    sale_type: str
    ended_on: dt.date | None
    post_url: str
    lot_url: str
    label: str
    price: float | None
    shipping_text: str | None
    payment_text: str | None
    paid_at: dt.datetime | None
    received_at: dt.datetime | None


def _text(item: dict, key: str, where: str, *, required: bool = False, limit: int = MAX_SHORT) -> str | None:
    value = item.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise PayloadError(f"{where}: {key} is missing")
        return None
    if not isinstance(value, str):
        raise PayloadError(f"{where}: {key} must be text")
    value = value.strip()
    if len(value) > limit:
        raise PayloadError(f"{where}: {key} is longer than {limit} characters")
    return value


def _url(item: dict, key: str, where: str) -> str:
    value = _text(item, key, where, required=True)
    parts = urlsplit(value)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in _FACEBOOK_HOSTS:
        raise PayloadError(f"{where}: {key} must be an https://www.facebook.com/ link")
    return value


def _date(item: dict, key: str, where: str) -> dt.date | None:
    value = _text(item, key, where)
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise PayloadError(f"{where}: {key} must be a date like 2026-10-04, got {value!r}") from None


def _datetime(item: dict, key: str, where: str) -> dt.datetime | None:
    value = _text(item, key, where)
    if value is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PayloadError(f"{where}: {key} must be an ISO date-time, got {value!r}") from None
    # Stored naive UTC, like the app's other timestamps.
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def _price(item: dict, where: str) -> float | None:
    value = item.get("price")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PayloadError(f"{where}: price must be a number of kr or null")
    if value < 0 or value > MAX_PRICE:
        raise PayloadError(f"{where}: price {value} is out of range")
    return float(value)


def _item(raw: object, index: int) -> WonItemIn:
    where = f"items[{index}]"
    if not isinstance(raw, dict):
        raise PayloadError(f"{where} must be an object")
    ref = _text(raw, "external_ref", where, required=True)
    if not ref.startswith(f"{SOURCE}:"):
        raise PayloadError(f"{where}: external_ref must start with '{SOURCE}:', got {ref!r}")
    where = f"{where} ({ref})"
    sale_type = _text(raw, "sale_type", where, required=True)
    if sale_type not in SALE_TYPES:
        raise PayloadError(f"{where}: sale_type must be one of {', '.join(sorted(SALE_TYPES))}, got {sale_type!r}")
    return WonItemIn(
        external_ref=ref,
        seller=_text(raw, "seller", where),
        sale_type=sale_type,
        ended_on=_date(raw, "ended_on", where),
        post_url=_url(raw, "post_url", where),
        lot_url=_url(raw, "lot_url", where),
        label=_text(raw, "label", where, required=True),
        price=_price(raw, where),
        shipping_text=_text(raw, "shipping_text", where, limit=MAX_TEXT),
        payment_text=_text(raw, "payment_text", where, limit=MAX_TEXT),
        paid_at=_datetime(raw, "paid_at", where),
        received_at=_datetime(raw, "received_at", where),
    )


def parse_payload(data: object) -> list[WonItemIn]:
    """Checks a decoded JSON body against the v1 contract and returns its
    items, or raises PayloadError. Unknown fields are ignored (a newer
    sender may add some within the same major version); an unknown major
    version is refused outright, since its fields may mean something else."""
    if not isinstance(data, dict):
        raise PayloadError("The body must be a JSON object")
    if data.get("format") != FORMAT:
        raise PayloadError(f"Unknown format {data.get('format')!r}: expected {FORMAT!r}")
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise PayloadError(f"version must be a whole number, got {version!r}")
    if version != SUPPORTED_VERSION:
        raise PayloadError(
            f"Unsupported {FORMAT} version {version}: this tcg_inventory reads version {SUPPORTED_VERSION}. "
            "Update whichever side is older so they match."
        )
    items = data.get("items")
    if not isinstance(items, list):
        raise PayloadError("items must be a list")
    if len(items) > MAX_ITEMS:
        raise PayloadError(f"Too many items ({len(items)}); at most {MAX_ITEMS} per send")
    parsed = [_item(raw, i) for i, raw in enumerate(items)]
    seen: set[str] = set()
    for it in parsed:
        if it.external_ref in seen:
            raise PayloadError(f"external_ref {it.external_ref!r} appears more than once")
        seen.add(it.external_ref)
    return parsed


_FIELDS = (
    "seller", "sale_type", "ended_on", "post_url", "lot_url", "label", "price",
    "shipping_text", "payment_text", "paid_at", "received_at",
)


@dataclass
class UpsertResult:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    # Registered or ignored: left exactly as they were.
    kept: int = 0

    def as_dict(self) -> dict:
        return {"added": self.added, "updated": self.updated, "unchanged": self.unchanged, "kept": self.kept}


def upsert_items(db: Session, items: list[WonItemIn], now: dt.datetime | None = None) -> UpsertResult:
    """Stages the items by external_ref, without committing. A new ref
    becomes a pending row; a pending row gets the sent fields (and
    last_seen_at); a registered or ignored row is never changed. So
    re-sending is always safe."""
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    result = UpsertResult()
    refs = [it.external_ref for it in items]
    existing = {row.external_ref: row for row in db.query(WonItem).filter(WonItem.external_ref.in_(refs))} if refs else {}
    for it in items:
        row = existing.get(it.external_ref)
        if row is None:
            db.add(
                WonItem(
                    external_ref=it.external_ref,
                    source=SOURCE,
                    status=STATUS_PENDING,
                    first_seen_at=now,
                    last_seen_at=now,
                    **{f: getattr(it, f) for f in _FIELDS},
                )
            )
            result.added += 1
            continue
        if row.status != STATUS_PENDING:
            result.kept += 1
            continue
        changed = False
        for f in _FIELDS:
            if getattr(row, f) != getattr(it, f):
                setattr(row, f, getattr(it, f))
                changed = True
        row.last_seen_at = now
        if changed:
            result.updated += 1
        else:
            result.unchanged += 1
    return result


def ignore_item(db: Session, item_id: int) -> bool:
    """Marks an open item ignored (a cancelled or duplicate win, or one
    whose order is missing and isn't to be registered again), without
    committing. Later sends leave it alone. False if there's no such open
    item (already ignored, registered on an order that exists, or gone)."""
    row = db.query(WonItem).filter(WonItem.id == item_id, _open_filter()).one_or_none()
    if row is None:
        return False
    row.status = STATUS_IGNORED
    return True


@dataclass
class PendingSale:
    """One won sale (a Facebook post) with its pending items: the Purchased
    tab's "Facebook wins to register" list is one of these per sale, not per
    seller -- one sale becomes one order (#309)."""

    post_url: str
    seller: str | None
    sale_type: str
    ended_on: dt.date | None
    shipping_text: str | None
    payment_text: str | None
    items: list[WonItem] = field(default_factory=list)
    # Order IDs some of these items point at (a lot kept pending as "not
    # complete", or a registered item, #317) that no longer have any
    # transaction: shown as "order missing" rather than hidden.
    missing_orders: set[int] = field(default_factory=set)

    @property
    def known_total(self) -> float:
        return round(sum(i.price for i in self.items if i.price is not None), 2)

    @property
    def unknown_count(self) -> int:
        return sum(1 for i in self.items if i.price is None)


def _open_filter():
    """Open items: pending, or registered on an order that no longer has
    any transaction (deleted, or emptied by Edit order) -- that registration
    no longer stands, so the item comes back as "order missing" (#317).
    Edit order carries `purchase_id` along (`follow_order_edit`), so a
    merged or moved order doesn't count as missing."""
    has_order = exists().where(Transaction.purchase_id == WonItem.purchase_id)
    return or_(
        WonItem.status == STATUS_PENDING,
        and_(WonItem.status == STATUS_REGISTERED, WonItem.purchase_id.isnot(None), ~has_order),
    )


def pending_sales(db: Session) -> list[PendingSale]:
    """Open items (pending, or registered on a missing order) grouped by
    sale, newest sale first (no end date last)."""
    rows = (
        db.query(WonItem)
        .filter(_open_filter())
        .order_by(WonItem.id)
        .all()
    )
    sales: dict[str, PendingSale] = {}
    for row in rows:
        sale = sales.get(row.post_url)
        if sale is None:
            sale = sales[row.post_url] = PendingSale(
                post_url=row.post_url,
                seller=row.seller,
                sale_type=row.sale_type,
                ended_on=row.ended_on,
                shipping_text=row.shipping_text,
                payment_text=row.payment_text,
            )
        sale.items.append(row)
    _mark_missing_orders(db, list(sales.values()))
    return sorted(sales.values(), key=lambda s: (s.ended_on is None, -(s.ended_on or dt.date.min).toordinal()))


def _existing_orders(db: Session, purchase_ids: set[int]) -> set[int]:
    if not purchase_ids:
        return set()
    rows = db.query(Transaction.purchase_id).filter(Transaction.purchase_id.in_(purchase_ids)).distinct()
    return {pid for (pid,) in rows}


def _mark_missing_orders(db: Session, sales: list[PendingSale]) -> None:
    wanted = {i.purchase_id for s in sales for i in s.items if i.purchase_id is not None}
    existing = _existing_orders(db, wanted)
    for sale in sales:
        sale.missing_orders = {
            i.purchase_id for i in sale.items if i.purchase_id is not None and i.purchase_id not in existing
        }


# ── The link flow (slice 2): a sale in the New Order cart ─────────────────


def note_for(item: WonItem) -> str:
    """The note a linked cart row (and so its transaction) carries."""
    return f"{item.label} · {item.seller}" if item.seller else item.label


@dataclass
class OpenSale:
    """One sale opened in the New Order cart: its open items and the
    values the cart is prefilled with."""

    sale: PendingSale
    # Existing orders other items of this same sale are already on: items
    # registered there, and lots kept pending as "not complete" on them.
    registered_orders: list[int]
    # What those earlier orders already count of this sale's open items
    # (their Remaining, see `open_sale`), left out of the prefilled Total.
    accounted: float = 0.0
    # Known prices of open items already linked to cards on an existing
    # order (lots kept as "not complete"): on that order's rows already.
    on_order_known: float = 0.0

    @property
    def excluded(self) -> float:
        """Everything left out of the prefilled Total (#317)."""
        return round(self.accounted + self.on_order_known, 2)

    @property
    def total(self) -> float | None:
        """The prefilled Total: the open items' known prices minus what the
        sale's earlier orders already count (`excluded`), so the same money
        is never in two orders' Totals (#317); None when no price is known.
        Shipping starts blank, and the cart adds whatever is typed there to
        this Total until the Total is edited by hand, so Remaining (Total -
        prices - shipping, as Order history computes it) is the known price
        of what isn't linked, less what's excluded."""
        if all(i.price is None for i in self.sale.items):
            return None
        return round(max(0.0, self.sale.known_total - self.excluded), 2)


def _orders_remaining(db: Session, purchase_ids: set[int]) -> float:
    """The sum of each order's positive Remaining, exactly as Order history
    computes it: Total - card prices - shipping, trade/ripped prices not
    being cash. An order with no Total has no Remaining and adds nothing."""
    if not purchase_ids:
        return 0.0
    by_order: dict[int, list[Transaction]] = {}
    for tx in db.query(Transaction).filter(Transaction.purchase_id.in_(purchase_ids)):
        by_order.setdefault(tx.purchase_id, []).append(tx)
    out = 0.0
    for txs in by_order.values():
        total = next((t.purchase_total for t in txs if t.purchase_total is not None), None)
        if total is None:
            continue
        shipping = next((t.purchase_shipping for t in txs if t.purchase_shipping is not None), None) or 0.0
        prices = sum(t.price or 0.0 for t in txs if t.type not in ("trade", "ripped"))
        out += max(0.0, total - prices - shipping)
    return round(out, 2)


def open_sale(db: Session, item_id: int) -> OpenSale | None:
    """The sale `item_id` belongs to, with that sale's open items, or None
    if the item is gone or nothing of its sale is open.

    Leftovers of a partly registered sale (#317, decided 2026-10-06): the
    first order's Total was prefilled with the whole sale, so its Remaining
    already counts the items left unlinked. The new order's prefilled Total
    leaves out what the sale's earlier orders still show as Remaining
    (capped at the leftovers' known prices), plus the known price of any
    lot already linked to cards on an existing order. If the earlier
    order's Total was set to cover only its own cards (Remaining 0), nothing
    is left out. Approximate when an earlier order also holds other sales
    (merged): its Remaining may include theirs; the cap keeps it bounded."""
    anchor = db.get(WonItem, item_id)
    if anchor is None:
        return None
    rows = (
        db.query(WonItem)
        .filter(WonItem.post_url == anchor.post_url, _open_filter())
        .order_by(WonItem.id)
        .all()
    )
    if not rows:
        return None
    first = rows[0]
    sale = PendingSale(
        post_url=first.post_url,
        seller=first.seller,
        sale_type=first.sale_type,
        ended_on=first.ended_on,
        shipping_text=first.shipping_text,
        payment_text=first.payment_text,
        items=rows,
    )
    _mark_missing_orders(db, [sale])
    registered = {
        pid
        for (pid,) in db.query(WonItem.purchase_id).filter(
            WonItem.post_url == anchor.post_url,
            WonItem.status == STATUS_REGISTERED,
            WonItem.purchase_id.isnot(None),
        )
    }
    on_order = [i for i in rows if i.purchase_id is not None and i.purchase_id not in sale.missing_orders]
    earlier = _existing_orders(db, registered) | {i.purchase_id for i in on_order}
    on_order_known = round(sum(i.price for i in on_order if i.price is not None), 2)
    leftover_known = max(0.0, sale.known_total - on_order_known)
    accounted = min(leftover_known, _orders_remaining(db, earlier))
    return OpenSale(
        sale=sale,
        registered_orders=sorted(earlier),
        accounted=round(accounted, 2),
        on_order_known=on_order_known,
    )


# Fuzzy name matching. Labels are free text from a Facebook comment
# ("3. Charizard ex 199/165 NM"), card names are Dex's ("Charizard ex"). A
# card is a candidate when at least one distinctive word of its name is in
# the label (a small typo allowed) and at least half of its words are; its
# printed number in the label adds to the score.
_WORD = re.compile(r"[a-z0-9]+")
# Words too common in card names to identify one on their own.
_GENERIC = {
    "ex", "gx", "v", "vmax", "vstar", "mega", "m", "tag", "team", "and", "the", "of",
    "holo", "reverse", "promo", "lv", "x", "break", "prime", "star", "card", "full", "art",
}
MAX_CANDIDATES = 6
_MIN_SCORE = 0.5


def _words(text: str | None) -> list[str]:
    if not text:
        return []
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return _WORD.findall(text)


def _distinctive(word: str) -> bool:
    return len(word) >= 3 and word not in _GENERIC and not word.isdigit()


def _printed_number(card: Card) -> str | None:
    """"199/165" -> "199", "007" -> "7"; None when there's no number."""
    if not card.number:
        return None
    head = card.number.split("/")[0].strip().lstrip("0")
    return head if head.isdigit() else None


@dataclass
class Candidate:
    card: Card
    score: float
    # Orders this card already has an acquired transaction on (purchase,
    # ripped, trade), for the "already on order #N" badge.
    orders: list[int]
    # It has an acquired transaction with no Order ID (registered alone).
    unordered_acquired: bool
    # First seen on or after the sale ended: likely this very copy.
    recent: bool
    # Tagged Incoming in Dex ("On the way", #382).
    in_transit: bool = False
    # ... and first seen on the way on or after the sale ended: very likely
    # this copy, even a 2nd copy of a card already owned (which `recent`,
    # keyed on created_at, misses). Ranked with or before `recent`.
    transit_since_sale: bool = False

    @property
    def acquired(self) -> bool:
        return bool(self.orders) or self.unordered_acquired


def _acquired_by_card(db: Session, acquired_types: tuple[str, ...], card_ids=None) -> dict[int, list[int | None]]:
    query = db.query(Transaction.card_id, Transaction.purchase_id).filter(Transaction.type.in_(acquired_types))
    if card_ids is not None:
        query = query.filter(Transaction.card_id.in_(card_ids))
    out: dict[int, list[int | None]] = {}
    for card_id, purchase_id in query:
        ids = out.setdefault(card_id, [])
        if purchase_id not in ids:
            ids.append(purchase_id)
    return out


def _candidate(card: Card, score: float, ended_on: dt.date | None, acquired: dict) -> Candidate:
    pids = acquired.get(card.id, [])
    return Candidate(
        card=card,
        score=score,
        orders=sorted(p for p in pids if p is not None),
        unordered_acquired=None in pids,
        recent=bool(ended_on and card.created_at and card.created_at.date() >= ended_on),
        in_transit=bool(card.in_transit),
        transit_since_sale=bool(
            ended_on and card.in_transit and card.in_transit_since and card.in_transit_since >= ended_on
        ),
    )


def _rank(c: Candidate):
    # 1. no acquired transaction yet, 2. on the way since the sale's end or
    # first seen on/after it (on the way first within that group), then the match.
    return (
        c.acquired,
        not (c.transit_since_sale or c.recent),
        not c.transit_since_sale,
        -c.score,
        c.card.name,
        c.card.id,
    )


def annotate_cards(db: Session, cards: list[Card], ended_on: dt.date | None, acquired_types: tuple[str, ...]) -> list[Candidate]:
    """Badges and ranking for an item's own "Not listed? Search…" results."""
    acquired = _acquired_by_card(db, acquired_types, [c.id for c in cards]) if cards else {}
    return sorted((_candidate(c, 0.0, ended_on, acquired) for c in cards), key=_rank)


def candidates(db: Session, items: list[WonItem], acquired_types: tuple[str, ...]) -> dict[int, list[Candidate]]:
    """Up to MAX_CANDIDATES cards per item, best first. Suggestions only: the
    cart shows them as unticked checkboxes and never links one by itself.
    A card reaches tcg_inventory once its qty is raised in Dex (tagged
    Incoming until it arrives, #382) and the daily sync runs, so an empty
    list is normal for a while."""
    cards = db.query(Card).all()
    acquired = _acquired_by_card(db, acquired_types)
    names = {c.id: _words(c.name) for c in cards}
    vocab_by_initial: dict[str, set[str]] = {}
    for words in names.values():
        for w in words:
            vocab_by_initial.setdefault(w[0], set()).add(w)

    out: dict[int, list[Candidate]] = {}
    for item in items:
        label_words = set(_words(item.label))
        numbers = {w.lstrip("0") for w in label_words if w.isdigit() and w.lstrip("0")}
        matched: set[str] = set()
        for w in label_words:
            if w in vocab_by_initial.get(w[0], ()):
                matched.add(w)
            elif len(w) >= 4 and not w.isdigit():
                matched.update(difflib.get_close_matches(w, vocab_by_initial.get(w[0], ()), n=3, cutoff=0.85))
        found = []
        for card in cards:
            words = names[card.id]
            if not words or not any(_distinctive(w) and w in matched for w in words):
                continue
            score = sum(1 for w in words if w in matched) / len(words)
            if score < _MIN_SCORE:
                continue
            if _printed_number(card) in numbers:
                score += 0.5
            found.append(_candidate(card, score, item.ended_on, acquired))
        found.sort(key=_rank)
        out[item.id] = found[:MAX_CANDIDATES]
    return out


class LinkError(ValueError):
    """A linked item can't be registered; the message says why."""


def linkable_items(db: Session, item_ids: set[int]) -> dict[int, WonItem]:
    """The open items the cart linked (pending, or registered on a missing
    order), or LinkError naming the first one that's gone or no longer open
    (registered or ignored in another tab), so Register writes nothing
    rather than registering an item twice."""
    rows = {r.id: r for r in db.query(WonItem).filter(WonItem.id.in_(item_ids))} if item_ids else {}
    open_ids = {i for (i,) in db.query(WonItem.id).filter(WonItem.id.in_(item_ids), _open_filter())} if item_ids else set()
    for item_id in sorted(item_ids):
        row = rows.get(item_id)
        if row is None:
            raise LinkError("An imported Facebook item in this cart no longer exists -- nothing was saved. Reload the page.")
        if item_id not in open_ids:
            where = f" on order #{row.purchase_id}" if row.purchase_id is not None else ""
            raise LinkError(
                f"“{row.label}” is already {row.status}{where} -- nothing was saved. "
                "Remove its rows (or reload the page) and register again."
            )
    return rows


def mark_registered(items: dict[int, WonItem], purchase_id: int, keep_pending: set[int]) -> None:
    """Points every linked item at the new order, without committing. Each
    becomes `registered`, except a lot marked "not complete", which is (or
    goes back to) pending, pointing at the order, until its other cards are
    added."""
    for item_id, row in items.items():
        row.purchase_id = purchase_id
        row.status = STATUS_PENDING if item_id in keep_pending else STATUS_REGISTERED


def complete_item(db: Session, item_id: int) -> bool:
    """Marks a lot kept pending as "not complete" registered, once the rest
    is on its order, without committing. False unless it's a pending item
    pointing at an order that still exists."""
    row = db.get(WonItem, item_id)
    if row is None or row.status != STATUS_PENDING or row.purchase_id is None:
        return False
    if not _existing_orders(db, {row.purchase_id}):
        return False
    row.status = STATUS_REGISTERED
    return True


# ── Edit order carries the items along (#317) ─────────────────────────────


def _pick_order(dests: Counter, source: int) -> int | None:
    """Where an item's rows ended up: the one order if they're all in one;
    the source order if some are still there; otherwise the order holding
    most of them (lowest ID on a tie). None if there are none."""
    if not dests:
        return None
    if len(dests) == 1:
        return next(iter(dests))
    if source in dests:
        return source
    return min(dests, key=lambda pid: (-dests[pid], pid))


def follow_order_edit(db: Session, source: int, notes_before: dict[int, str | None]) -> None:
    """After an Edit order save on order `source` (rows moved, merged into
    another order, split off, or deleted; flushed, not committed), points
    that order's registered and pending items at the order their cards are
    on now. `notes_before` is every row the order had before the save:
    tx id -> its note then.

    `won_items` has no per-transaction link, so an item's own rows are
    found by the note Register gave them ("<label> · <seller>", `note_for`)
    as it was before this save. The item follows where its own rows went
    (`_pick_order`): all in one order -> that order; split across orders ->
    stays on `source` if some are still there, otherwise the order with
    most of them. An item with no own rows left (its note was edited, its
    rows deleted, or a lot's other cards were added later with no note)
    goes by all of the order's rows the same way: it stays put while the
    order still has rows, and follows a merge or a whole move. If every row
    was deleted it keeps pointing at `source`, which then shows as "order
    missing". Ignored items are left alone."""
    if not notes_before:
        return
    items = (
        db.query(WonItem)
        .filter(WonItem.purchase_id == source, WonItem.status != STATUS_IGNORED)
        .all()
    )
    if not items:
        return
    now = dict(db.query(Transaction.id, Transaction.purchase_id).filter(Transaction.id.in_(list(notes_before))).all())
    all_dests = Counter(pid for pid in now.values() if pid is not None)
    for item in items:
        mine = note_for(item)
        own = Counter(
            now[txid]
            for txid, note in notes_before.items()
            if (note or "").strip() == mine and now.get(txid) is not None
        )
        target = _pick_order(own or all_dests, source)
        if target is not None:
            item.purchase_id = target
