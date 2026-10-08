"""Want and sale lists (issue #370, epic #366).

A list (`card_lists`) is a user-curated snapshot of prints
(`card_list_items`, one per master card). It's either a **want** list
(cards to buy) or a **sale** list (spares to sell); the kind is fixed when
the list is created.

- Items point at master cards, not Cards, so a want list can hold prints
  the user doesn't own (checklist master cards from #368).
- Nothing is added or removed automatically. Bulk adds from the set page
  are explicit and idempotent (`add_items`); "got it" items leave only
  through `remove_got_it`.
- Every status is computed live from the Cards linked to the item's master
  card, never stored, so it follows the next Dex sync with no list edits:
  - want: Got it / On the way / Got k of n / Possibly owned (unmatched) /
    Missing;
  - sale: Sold out / Listed / On the way / Not enough spares / Available.
  Spares are the set page's per-print, in-hand figure (`MasterSetSlot.spares`,
  models.print_in_hand_spares), so the two never disagree.
- Copies on the way (Dex's Incoming tag, issue #382) count as owned (a want
  item says "On the way", so it isn't bought twice) but are never
  available: sale spares, the /sales link and "Copy as text" count only
  copies in hand.
- Lists never touch qty, collections, binders or transactions (same
  invariant as `listings`).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

import constants
import masterdata
import queries
from form_validation import FormError
from models import CardList, CardListItem

LIST_KINDS = {"want": "Want list", "sale": "Sale list"}
ITEM_SOURCES = ("manual", "missing", "spares")

# Status keys per kind, in summary order.
WANT_STATUSES = ("missing", "unmatched", "partial", "on_the_way", "got")
SALE_STATUSES = ("available", "listed", "on_the_way", "short", "sold_out")
STATUS_LABELS = {
    "missing": "Missing",
    "unmatched": "Possibly owned (unmatched)",
    "partial": "Got some",  # the row shows "Got k of n"
    "got": "Got it",
    "on_the_way": "On the way",
    "available": "Available",
    "listed": "Listed",
    "short": "Not enough spares",
    "sold_out": "Sold out",
}
# Lower-case forms for a list's one-line summary ("3 missing · 1 got it").
SUMMARY_LABELS = {
    "missing": "missing",
    "unmatched": "possibly owned",
    "partial": "partly got",
    "got": "got it",
    "on_the_way": "on the way",
    "available": "available",
    "listed": "listed",
    "short": "not enough spares",
    "sold_out": "sold out",
}
# masterdata language -> the code shown for a set with no checklist.
_MASTER_LANGUAGE_CODES = {"int": "", "ja": "JP", "ko": "KR", "zh-hans": "ZH", "zh-hant": "ZH"}


def _kr(value: float) -> str:
    return f"{value:,.0f} kr".replace(",", " ")


def _number_key(number: str | None) -> str:
    text = (number or "").strip()
    return (text.lstrip("0") or "0") if text.isdigit() else text


# --------------------------------------------------------------------------
# Reading: live status
# --------------------------------------------------------------------------
@dataclass
class _SetInfo:
    """One (language, set_code)'s master-set detail, shared by every item
    of that set (one `master_set_detail` call per set per request)."""

    detail: object | None  # queries.MasterSetDetail
    slots: dict = field(default_factory=dict)  # master_card_id -> MasterSetSlot
    unmatched_numbers: set = field(default_factory=set)


def _set_info(db: Session, language: str, set_code: str) -> _SetInfo:
    detail = queries.master_set_detail(db, language, set_code)
    if detail is None:
        return _SetInfo(detail=None)
    numbers = set()
    for card in detail.unmatched:
        if card.master_card is not None:
            numbers.add(_number_key(card.master_card.number))
        else:
            parsed = masterdata.parse_dex_card_id(card.card_id)
            if parsed:
                numbers.add(_number_key(parsed[2]))
    return _SetInfo(detail=detail, slots={s.master.id: s for s in detail.slots}, unmatched_numbers=numbers)


@dataclass
class ListItemView:
    item: CardListItem
    kind: str
    slot: object  # queries.MasterSetSlot: the print's owned cards, spares, price
    set_label: str
    has_checklist: bool
    status: str
    listing_ids: list[int]
    price: float | None  # market price of the print; None = no price data

    @property
    def master(self):
        return self.item.master_card

    @property
    def card(self):
        """The owned Card the row links to (most copies), or None."""
        return self.slot.card

    @property
    def owned_qty(self) -> int:
        return self.slot.owned_qty

    @property
    def in_hand_qty(self) -> int:
        return self.slot.in_hand_qty

    @property
    def in_transit(self) -> int:
        return self.slot.in_transit

    @property
    def spares(self) -> int:
        """Spares in hand (the sellable ones, #382)."""
        return self.slot.spares

    @property
    def spares_on_the_way(self) -> int:
        return self.slot.spares_on_the_way

    @property
    def name(self) -> str:
        return self.slot.name or self.master.name or ""

    @property
    def image_url(self) -> str | None:
        return self.slot.image_url

    @property
    def variant_label(self) -> str:
        variant = self.master.variant or ""
        return self.slot.track_label or self.master.variant_label or masterdata.VARIANT_LABELS.get(variant, variant)

    @property
    def print_label(self) -> str:
        """"#023 · Poké Ball"; the base track's prints just "#023 · Main"."""
        return " · ".join(p for p in (self.slot.number_label, self.variant_label) if p)

    @property
    def status_label(self) -> str:
        if self.status == "partial":
            return f"Got {self.owned_qty} of {self.item.qty}"
        return STATUS_LABELS[self.status]

    @property
    def counted_qty(self) -> int:
        """The copies the list's total counts: still missing (want; copies
        on the way count as owned) or sellable, capped at spares in hand
        (sale)."""
        if self.kind == "want":
            return max(self.item.qty - self.owned_qty, 0)
        return min(self.item.qty, self.spares)

    @property
    def est_value(self) -> float | None:
        if self.price is None:
            return None
        return self.price * self.counted_qty

    @property
    def text_line(self) -> str:
        """One "Copy as text" line, e.g.
        `Pokémon Card 151 (Korean) #023 Ekans · Poké Ball ×1 — 45 kr`
        (target price if set, else market price, else no price)."""
        qty = self.counted_qty if self.kind == "want" else self.item.qty
        parts = [self.set_label, self.slot.number_label, self.name]
        line = " ".join(p for p in parts if p)
        if self.slot.track not in ("main", "secret") and self.variant_label:
            line += f" · {self.variant_label}"
        line += f" ×{qty}"
        price = self.item.target_price if self.item.target_price is not None else self.price
        if price is not None:
            line += f" — {_kr(price)}"
        return line


@dataclass
class CardListDetail:
    card_list: CardList
    items: list[ListItemView]

    @property
    def kind(self) -> str:
        return self.card_list.kind

    @property
    def kind_label(self) -> str:
        return LIST_KINDS.get(self.kind, self.kind)

    @property
    def status_counts(self) -> dict[str, int]:
        order = WANT_STATUSES if self.kind == "want" else SALE_STATUSES
        counts = {key: 0 for key in order}
        for view in self.items:
            counts[view.status] = counts.get(view.status, 0) + 1
        return counts

    @property
    def summary(self) -> str:
        """"3 missing · 1 got it", statuses with no items left out."""
        return " · ".join(f"{n} {SUMMARY_LABELS[key]}" for key, n in self.status_counts.items() if n)

    @property
    def got_items(self) -> list[ListItemView]:
        return [v for v in self.items if v.status == "got"] if self.kind == "want" else []

    @property
    def est_total(self) -> float:
        """Want: "Est. cost to complete" = market price × copies still
        missing. Sale: "Est. value of spares" = market price × min(qty,
        spares). Items with no price are left out (see `unpriced`)."""
        return sum(v.est_value for v in self.items if v.est_value is not None)

    @property
    def unpriced(self) -> int:
        """Items that would count toward the total but have no price."""
        return sum(1 for v in self.items if v.price is None and v.counted_qty)

    @property
    def sale_card_ids(self) -> list[int]:
        """One Card in hand per item for `/sales?card_ids=...` (the card
        with most copies in hand of the print, #382); sold-out items and
        items whose every copy is on the way have none."""
        cards = (v.slot.in_hand_card for v in self.items)
        return [c.id for c in cards if c is not None]

    @property
    def text_items(self) -> list[ListItemView]:
        """What "Copy as text" lists: want items still wanted (not got it,
        not on the way), sale items still owned (not sold out)."""
        skip = ("got", "on_the_way") if self.kind == "want" else ("sold_out",)
        return [v for v in self.items if v.status not in skip]

    @property
    def text(self) -> str:
        lines = [f"{self.card_list.name} ({self.kind_label.lower()})"]
        lines += [v.text_line for v in self.text_items]
        return "\n".join(lines)


def _set_label(master, info: _SetInfo) -> str:
    """The checklist's display name (it already says e.g. "(Korean)"), else
    the set name plus a language code: the corrected card language of a
    linked card (#367, e.g. KR), else the master card's language."""
    if info.detail is not None:
        return info.detail.checklist.display_name
    code = next((constants.language_code(c.language) for c in master.cards if c.language), None)
    if code is None:
        code = _MASTER_LANGUAGE_CODES.get(master.language, (master.language or "").upper())
    name = master.set_name or master.set_code
    return f"{name} ({code})" if code and code != "EN" else name


def _want_status(view_qty: int, owned: int, in_hand: int, number: str | None, info: _SetInfo) -> str:
    if owned >= view_qty:
        # Owned copies on the way count (so it isn't bought twice), but it's
        # only "Got it" (and removable) once enough are in hand (#382).
        return "got" if in_hand >= view_qty else "on_the_way"
    if owned > 0:
        return "partial"
    if _number_key(number) in info.unmatched_numbers:
        # An owned card of this number lands on no checklist print, so it
        # may well be this one: don't claim it's missing (ux, #366).
        return "unmatched"
    return "missing"


def _sale_status(qty: int, owned: int, spares: int, spares_on_the_way: int, listed: bool) -> str:
    """`spares` are in hand; `spares_on_the_way` would be spares once they
    arrive (#382): "On the way" when those make up the shortfall, else
    "Not enough spares"."""
    if owned == 0:
        return "sold_out"
    if listed:
        return "listed"
    if qty > spares:
        return "on_the_way" if qty <= spares + spares_on_the_way else "short"
    return "available"


def list_detail(db: Session, card_list: CardList, set_cache: dict | None = None) -> CardListDetail:
    """A list's items with live status, sorted by set, then number (base
    print before ball prints). `set_cache` shares the per-set lookups
    across several lists (the /collections Lists summaries)."""
    set_cache = {} if set_cache is None else set_cache
    rows = []
    for item in card_list.items:
        master = item.master_card
        key = (master.language, master.set_code)
        if key not in set_cache:
            set_cache[key] = _set_info(db, *key)
        info = set_cache[key]
        slot = info.slots.get(master.id) or queries.MasterSetSlot(
            master=master,
            track="",
            counts=False,
            cards=queries.owned_cards_of(master),
            number_label="#" + (master.number or ""),
        )
        rows.append((item, master, info, slot))

    listed = {}
    if card_list.kind == "sale":
        listed = queries.active_listings_by_card(db, [c.id for _i, _m, _s, slot in rows for c in slot.cards])

    views = []
    for item, master, info, slot in rows:
        listing_ids = sorted({lid for c in slot.cards for lid in listed.get(c.id, [])}, reverse=True)
        if card_list.kind == "want":
            status = _want_status(item.qty, slot.owned_qty, slot.in_hand_qty, master.number, info)
        else:
            status = _sale_status(item.qty, slot.owned_qty, slot.spares, slot.spares_on_the_way, bool(listing_ids))
        price = slot.price
        if price is None:
            # An unowned print: any linked card's resolved price (e.g. a
            # sold copy at qty 0); else no price data.
            price = next((c.display_price for c in master.cards if c.display_price is not None), None)
        views.append(
            ListItemView(
                item=item,
                kind=card_list.kind,
                slot=slot,
                set_label=_set_label(master, info),
                has_checklist=info.detail is not None,
                status=status,
                listing_ids=listing_ids,
                price=price,
            )
        )
    views.sort(
        key=lambda v: (
            v.set_label.lower(),
            queries._number_sort_key(v.master.number),
            queries._TRACK_RANK.get(v.slot.track, len(queries._TRACK_RANK)),
            v.master.variant or "",
        )
    )
    return CardListDetail(card_list=card_list, items=views)


def all_lists(db: Session) -> list[CardListDetail]:
    """Every list with its live status, by kind (want first) then name."""
    lists = db.query(CardList).order_by(CardList.kind.desc(), CardList.name, CardList.id).all()
    cache: dict = {}
    return [list_detail(db, cl, cache) for cl in lists]


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------
def clean_name(raw: str | None) -> str:
    name = (raw or "").strip()
    if not name:
        raise FormError("Give the list a name.")
    return name


def create_list(db: Session, name: str | None, kind: str | None, note: str | None = None) -> CardList:
    """A new, empty list (flushed, not committed). The kind can't change later."""
    if kind not in LIST_KINDS:
        raise FormError("Pick a kind: want list or sale list.")
    card_list = CardList(
        name=clean_name(name), kind=kind, note=(note or "").strip() or None, created_at=dt.datetime.now()
    )
    db.add(card_list)
    db.flush()
    return card_list


def add_items(db: Session, card_list: CardList, entries, source: str = "manual") -> tuple[int, int]:
    """Add `(master_card_id, qty)` entries to a list. Idempotent: a print
    already on the list is left exactly as it is (qty, price and note
    untouched). Returns (added, already on list). Flushes, doesn't commit."""
    if source not in ITEM_SOURCES:
        raise ValueError(f"unknown list item source {source!r}")
    existing = {
        mid for (mid,) in db.query(CardListItem.master_card_id).filter(CardListItem.list_id == card_list.id)
    }
    added = already = 0
    now = dt.datetime.now()
    for master_card_id, qty in entries:
        if master_card_id in existing:
            already += 1
            continue
        existing.add(master_card_id)
        db.add(
            CardListItem(
                list_id=card_list.id,
                master_card_id=master_card_id,
                qty=max(int(qty or 1), 1),
                source=source,
                added_at=now,
            )
        )
        added += 1
    db.flush()
    return added, already


def remove_got_it(db: Session, card_list: CardList) -> int:
    """Delete a want list's "Got it" items (the explicit "Remove got-it
    items (N)" action); "On the way" items stay until they arrive (#382).
    Returns how many went. Flushes, doesn't commit."""
    detail = list_detail(db, card_list)
    got = [v.item for v in detail.got_items]
    for item in got:
        db.delete(item)
    db.flush()
    return len(got)
