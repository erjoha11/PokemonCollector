"""Card masterdata: one canonical identity per printed card + variant, with
every external catalog's ID for it mapped onto that one identity.

There is no official, industry-wide ID for an individual Pokemon card (no
ISBN/GTIN equivalent -- barcodes only exist on sealed product), so every
tool (Dex, pokemontcg.io, TCGdex, TCGplayer, Cardmarket, Collectr, ...)
has its own. What they all ultimately describe is what's printed on the
card itself, so the canonical key here is built from that:

    (language, set_code, number, variant)

- `language`/`set_code`/`number` are parsed out of Dex's own `card_id`
  (see `parse_dex_card_id`) -- Dex follows pokemontcg.io's IDs for
  international prints ("sv2-109") and prefixes other catalogs
  ("jpn_sv2a-168", "scn_csv9-79").
- `variant` is Dex's free-text Variant normalized to a controlled code
  (see `normalize_variant` / `VARIANT_LABELS`). Variant naming is where
  catalogs disagree the most, so this vocabulary is the part worth
  extending deliberately as new variants show up.

`master_cards` holds the identity; `master_card_ids` maps any number of
external IDs onto it, one per `source`. `Card.master_card_id` links a
physical card (the row with `qty`, binder, collections) to its identity.
Masterdata never touches `qty`/collections/binders -- it's identity only.
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy.orm import Session

from models import Card, MasterCard, MasterCardId

# Dex card_id prefix -> language code. No prefix means Dex's "International"
# catalog (pokemontcg.io's IDs). A prefix not listed here is kept as-is as
# the language code rather than guessed -- add it here once confirmed.
DEX_LANGUAGE_PREFIXES = {
    "": "int",
    "jpn": "ja",
    "scn": "zh-hans",
}

# Canonical variant code -> display label. Codes are the stable part of the
# master key; labels are just for display.
VARIANT_LABELS = {
    "unspecified": "Unspecified",
    "normal": "Normal",
    "holo": "Holo",
    "reverse_holo": "Reverse Holo",
    "cosmos_holo": "Cosmos Holo",
    "cracked_ice_holo": "Cracked Ice Holo",
    "expansion_stamp": "Expansion Stamp",
    "first_edition": "1st Edition",
    "first_edition_holo": "1st Edition Holo",
    "shadowless": "Shadowless",
    "poke_ball_holo": "Poké Ball Holo",
    "master_ball_holo": "Master Ball Holo",
    "friend_ball_holo": "Friend Ball Holo",
}

# Dex spellings that don't slugify straight to their code above.
_DEX_VARIANT_ALIASES = {
    "1st_edition": "first_edition",
    "1st_edition_holo": "first_edition_holo",
    "pokeball_holo": "poke_ball_holo",
    "masterball_holo": "master_ball_holo",
}

# Sources written automatically when a card is linked. Anything else
# (tcgplayer, cardmarket, collectr, a verified tcgdex id, ...) is added via
# set_external_id() as those integrations get built.
SOURCE_DEX = "dex"
SOURCE_POKEMONTCG = "pokemontcg"

# How an external ID was established. "manual" is never overwritten by an
# automatic match -- it's how a wrong automatic match gets corrected.
MATCHED_EXACT = "exact_id"  # the source's own ID, copied verbatim
MATCHED_DERIVED = "derived"  # computed from another ID by a known rule
MATCHED_HEURISTIC = "heuristic"  # name/set/number search, could be wrong
MATCHED_MANUAL = "manual"

_DEX_CARD_ID = re.compile(r"^(?:([a-z]+)_)?([a-z0-9.]+)-([A-Za-z0-9]+)$")


@dataclass(frozen=True)
class MasterKey:
    language: str
    set_code: str
    number: str
    variant: str


def parse_dex_card_id(card_id: str | None) -> tuple[str, str, str] | None:
    """(language, set_code, number) from a Dex card_id, or None if it
    doesn't follow the `[prefix_]set-number` shape.
    """
    if not card_id:
        return None
    match = _DEX_CARD_ID.match(card_id.strip())
    if not match:
        return None
    prefix, set_code, number = match.groups()
    language = DEX_LANGUAGE_PREFIXES.get(prefix or "", prefix)
    return language, set_code, number


def normalize_variant(raw: str | None) -> str:
    """Dex's Variant text -> canonical variant code. An unknown variant
    still gets a stable code (its slug) rather than being dropped, so it
    can be added to VARIANT_LABELS later without re-keying anything.
    """
    if not raw or not raw.strip():
        return "unspecified"
    ascii_text = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_")
    return _DEX_VARIANT_ALIASES.get(slug, slug)


def master_key_for(card: Card) -> MasterKey | None:
    parsed = parse_dex_card_id(card.card_id)
    if parsed is None:
        return None
    language, set_code, number = parsed
    return MasterKey(language, set_code, number, normalize_variant(card.variant))


def set_external_id(
    session: Session, master: MasterCard, source: str, external_id: str, matched_by: str
) -> MasterCardId:
    """Record `master`'s ID in `source`. One ID per source per master card.
    A "manual" mapping is never overwritten by an automatic one.
    """
    existing = next((row for row in master.external_ids if row.source == source), None)
    if existing is None:
        existing = MasterCardId(source=source)
        master.external_ids.append(existing)
    elif existing.matched_by == MATCHED_MANUAL and matched_by != MATCHED_MANUAL:
        return existing
    if existing.external_id != external_id or existing.matched_by != matched_by:
        existing.external_id = external_id
        existing.matched_by = matched_by
        existing.matched_at = dt.date.today()
    return existing


def _get_or_create_master(session: Session, key: MasterKey, card: Card, cache: dict | None) -> MasterCard:
    if cache is not None and key in cache:
        return cache[key]
    master = (
        session.query(MasterCard)
        .filter_by(language=key.language, set_code=key.set_code, number=key.number, variant=key.variant)
        .one_or_none()
    )
    if master is None:
        master = MasterCard(
            language=key.language,
            set_code=key.set_code,
            number=key.number,
            variant=key.variant,
            variant_label=VARIANT_LABELS.get(key.variant, card.variant),
            name=card.name,
            series=card.series,
            set_name=card.set,
            printed_number=card.number,
            created_at=dt.datetime.utcnow(),
        )
        session.add(master)
        session.flush()
    if cache is not None:
        cache[key] = master
    return master


def link_card(session: Session, card: Card, cache: dict | None = None) -> MasterCard | None:
    """Link `card` to its master identity (created if new) and seed the
    external IDs known from Dex's card_id alone. Returns None, leaving the
    card unlinked, when its card_id can't be parsed.
    """
    key = master_key_for(card)
    if key is None:
        return None
    master = _get_or_create_master(session, key, card, cache)
    card.master_card = master
    set_external_id(session, master, SOURCE_DEX, card.card_id, MATCHED_EXACT)
    if key.language == "int":
        # Dex's international IDs are pokemontcg.io's IDs (card_images.py
        # looks cards up there by this exact ID).
        set_external_id(session, master, SOURCE_POKEMONTCG, card.card_id, MATCHED_DERIVED)
    return master


def backfill_master_cards(session: Session) -> int:
    """Link every card that has no master identity yet. Idempotent; returns
    how many cards were linked.
    """
    cache: dict[MasterKey, MasterCard] = {}
    linked = 0
    for card in session.query(Card).filter(Card.master_card_id.is_(None)).all():
        if link_card(session, card, cache) is not None:
            linked += 1
    session.commit()
    return linked
