"""Canonical card identity, matching tcg_inventory's masterdata.

tcg_inventory keys every printed card on (language, set_code, number, variant)
and derives it from Dex's card_id (see apps/tcg_inventory/masterdata.py).
Apps never import from each other, so the few rules needed to produce the
same key from what's printed on a card are copied here; keep them in sync.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

# Language code -> Dex card_id prefix. Same codes as
# tcg_inventory/masterdata.py's DEX_LANGUAGE_PREFIXES (inverted).
DEX_PREFIX_BY_LANGUAGE = {
    "int": "",
    "ja": "jpn_",
    "zh-hans": "scn_",
}
LANGUAGES = ("int", "ja", "ko", "zh-hans", "zh-hant")

# Canonical variant codes, same as masterdata.VARIANT_LABELS.
VARIANTS = (
    "unspecified",
    "normal",
    "holo",
    "reverse_holo",
    "cosmos_holo",
    "cracked_ice_holo",
    "expansion_stamp",
    "first_edition",
    "first_edition_holo",
    "shadowless",
    "poke_ball_holo",
    "master_ball_holo",
    "friend_ball_holo",
)

_VARIANT_ALIASES = {
    "1st_edition": "first_edition",
    "1st_edition_holo": "first_edition_holo",
    "pokeball_holo": "poke_ball_holo",
    "masterball_holo": "master_ball_holo",
    "reverse": "reverse_holo",
    "reverseholo": "reverse_holo",
    "non_holo": "normal",
}


def normalize_variant(raw: Optional[str]) -> str:
    """Free-text variant -> canonical code (unknown ones keep their slug)."""
    if not raw or not raw.strip():
        return "unspecified"
    ascii_text = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_")
    return _VARIANT_ALIASES.get(slug, slug)


def normalize_number(raw: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """A printed collector number -> (number, printed_number).

    "4/102" -> ("4", "4/102"); "004/165" -> ("4", "004/165");
    "TG05/TG30" -> ("TG05", ...); "SWSH050" -> ("SWSH050", "SWSH050").
    Leading zeros are dropped from purely numeric numbers, since the
    catalogs' IDs (and so Dex's card_id) don't carry them.
    """
    if not raw or not raw.strip():
        return None, None
    printed = raw.strip()
    number = printed.split("/")[0].strip().replace(" ", "")
    if not number:
        return None, printed
    if number.isdigit():
        number = str(int(number))
    return number, printed


def dex_id(language: Optional[str], set_code: Optional[str], number: Optional[str]) -> Optional[str]:
    """The Dex card_id this card would have ("sv2-109", "jpn_sv2a-168"), or
    None when a part is missing or the language has no known Dex prefix."""
    if not (language and set_code and number):
        return None
    prefix = DEX_PREFIX_BY_LANGUAGE.get(language)
    if prefix is None:
        return None
    return f"{prefix}{set_code.lower()}-{number}"
