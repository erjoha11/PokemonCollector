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
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from models import WonItem

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
    """Marks a pending item ignored (a cancelled or duplicate win), without
    committing. Later sends leave it alone. False if there's no such pending
    item (already ignored or registered, or gone)."""
    row = db.get(WonItem, item_id)
    if row is None or row.status != STATUS_PENDING:
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

    @property
    def known_total(self) -> float:
        return round(sum(i.price for i in self.items if i.price is not None), 2)

    @property
    def unknown_count(self) -> int:
        return sum(1 for i in self.items if i.price is None)


def pending_sales(db: Session) -> list[PendingSale]:
    """Pending items grouped by sale, newest sale first (no end date last)."""
    rows = (
        db.query(WonItem)
        .filter(WonItem.status == STATUS_PENDING)
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
    return sorted(sales.values(), key=lambda s: (s.ended_on is None, -(s.ended_on or dt.date.min).toordinal()))
