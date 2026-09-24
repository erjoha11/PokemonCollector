"""Builds finn.no-ready ad text (title + description) from a selection of
cards -- the inverse direction of apps/finn_ad_scraper, which reads an
existing finn.no ad. Unlike that app's photo-identification step, this is a
deterministic template fill over data already in the DB (name, set, number,
variant, condition, price) -- no LLM call, no network, no DB access. Kept as
a pure function so it's trivially unit-testable, same shape as
importer.py's routing functions.

finn.no ad text itself is written in Norwegian (that's the audience reading
it on the marketplace) even though the rest of this app's UI is English --
the two are unrelated: one is buyer-facing copy for a Norwegian site, the
other is this app's own interface.
"""
from __future__ import annotations

import constants
from dataclasses import dataclass, field

MAX_TITLE_LENGTH = 70


@dataclass
class SaleItem:
    """One card selected for a listing, with the per-sale details that
    don't live on `Card` itself (how many of it to sell, its condition and
    asking price at listing time -- condition is persisted on Card, qty/price
    are decided fresh per listing so they never imply "sell everything I
    own" or silently trust a stale reference price).
    """

    card_id: int
    name: str
    set: str | None
    number: str | None
    variant: str | None
    language: str | None
    condition: str | None
    qty: int
    price: float | None


@dataclass
class ListingDraft:
    title: str
    description: str
    suggested_price: float | None
    card_count: int
    truncated_title: bool = field(default=False)


def _card_label(item: SaleItem) -> str:
    parts = [item.name]
    if item.set:
        parts.append(item.set)
    if item.number:
        parts.append(f"#{item.number}")
    label = " ".join(parts)
    extras = []
    if item.variant:
        extras.append(item.variant)
    # English is the default a buyer assumes; only other print languages
    # are worth calling out (as JP, KR, ... -- see constants.language_code).
    code = constants.language_code(item.language)
    if code and code != "EN":
        extras.append(code)
    if extras:
        label += f" ({', '.join(extras)})"
    return label


def _format_kr(amount: float) -> str:
    return f"{amount:,.0f} kr".replace(",", " ")


def _price_line(item: SaleItem) -> str:
    if item.price is None:
        return "pris: ikke satt"
    return _format_kr(item.price)


def _build_title(items: list[SaleItem]) -> tuple[str, bool]:
    if len(items) == 1:
        title = f"Pokemon kort – {_card_label(items[0])}"
    else:
        sets = {i.set for i in items if i.set}
        set_part = next(iter(sets)) if len(sets) == 1 else "diverse sett"
        title = f"Pokemon kort, {len(items)} stk – {set_part}"
    truncated = len(title) > MAX_TITLE_LENGTH
    if truncated:
        title = title[: MAX_TITLE_LENGTH - 1].rstrip() + "…"
    return title, truncated


def _group_by_set(items: list[SaleItem]) -> list[tuple[str, list[SaleItem]]]:
    groups: dict[str, list[SaleItem]] = {}
    order: list[str] = []
    for item in items:
        key = item.set or "Ukjent sett"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)
    return [(key, groups[key]) for key in order]


def build_listing(items: list[SaleItem], shipping_note: str | None = None) -> ListingDraft:
    """Build a finn.no title + description for the given selected cards.

    `items` is expected non-empty -- callers (app.py routes) are
    responsible for rejecting an empty selection before this is called,
    same as any other case that can't produce meaningful output.
    """
    if not items:
        raise ValueError("build_listing requires at least one item")

    title, truncated = _build_title(items)

    lines: list[str] = []
    if len(items) == 1:
        lines.append(f"Selger {_card_label(items[0])}.")
    else:
        lines.append(f"Selger {len(items)} Pokemon kort fra samlingen.")
    lines.append("")

    groups = _group_by_set(items)
    for set_name, set_items in groups:
        if len(groups) > 1:
            lines.append(f"{set_name}:")
        for item in set_items:
            qty_part = f"{item.qty} stk, " if item.qty != 1 else ""
            condition = item.condition or "Unknown"
            lines.append(f"- {_card_label(item)} — {qty_part}tilstand: {condition}, {_price_line(item)}")
        lines.append("")

    priced = [item for item in items if item.price is not None]
    suggested_price = sum(item.price * item.qty for item in priced) if priced else None
    if suggested_price is not None:
        unpriced = len(items) - len(priced)
        note = " (noen kort mangler pris — se over)" if unpriced else ""
        lines.append(f"Foreslått pris til sammen: {_format_kr(suggested_price)}{note}")
        lines.append("")

    lines.append(
        "Tilstand er vurdert etter beste evne — se bilder for detaljer. "
        "Ta gjerne kontakt ved spørsmål."
    )
    lines.append(shipping_note or "Sendes med sporbar frakt. Betaling via Vipps eller bankoverføring.")

    description = "\n".join(lines).strip() + "\n"

    return ListingDraft(
        title=title,
        description=description,
        suggested_price=suggested_price,
        card_count=len(items),
        truncated_title=truncated,
    )
