"""Business-rule constants for mapping Dex export categories onto the data
model. These come from the Excel system this app replaces -- see the task
description / app README before changing any of them.
"""
import re

# The Dex category that represents the actual physical inventory ("My
# Collection" in Dex). Every other category is a tag on a subset of the same
# physical cards, never a separate set of cards.
MY_COLLECTION_CATEGORY = "My Collection"

# Categories that must always be fully ignored on import -- never turned into
# a collection, a binder, or anything else. "Incoming" is the user's Dex
# folder of won-but-not-yet-arrived cards (added at qty 0, removed on
# arrival); Dex doesn't export qty-0 rows, so its only exported rows are cards
# already in My Collection, and "what's on the way" lives in Dex and the
# Facebook wins inbox instead (#309, #311).
EXCLUDED_CATEGORIES_EXACT = {"Wishlist", "Incoming"}
EXCLUDED_CATEGORIES_PREFIX = ("151 Fullarts",)

# Dex folder names that route to `binder_id` instead of becoming a collection.
BINDER_CATEGORIES = {
    "Illustrator Binder",
    "Vintage Binder",
    "151 Binder",
    "Tradebinder",
}

# Illustrator-specific collections -- deliberate, curated tagging. Highest
# primary-collection priority (rank 1).
ILLUSTRATOR_COLLECTIONS = {
    "Tomokazu Komiya Collection",
    "Shinji Kanda Illustrator Collection",
    "Yuka Morii Collection",
    "Saya Tsuruta Collection",
}

VINTAGE_COLLECTION_NAME = "Vintage Collection"
GENERIC_COLLECTION_NAME = "Collection"
SV151_COLLECTION_NAME = "Scarlet & Violet: 151 JP/KR"

# Collections whose cards always live in one specific physical binder.
# Applied at the end of every import (see importer._apply_auto_binder_rules)
# to fill in `binder_id` for a card that doesn't already have one -- an
# explicit Dex Binder-category export (e.g. Tradebinder) always wins over
# this fallback, since that reflects where the card is actually placed.
# No entry for Vintage Collection -- it doesn't have its own physical binder.
AUTO_BINDER_RULES = [
    (frozenset(ILLUSTRATOR_COLLECTIONS), "Illustrator Binder"),
    (frozenset({SV151_COLLECTION_NAME}), "151 Binder"),
]

# Primary-collection priority ranks -- lower number wins when a card belongs
# to more than one collection at once. Only affects which collection gets
# "credit" in summaries; card_collections keeps every actual tag regardless.
PRIORITY_RANK_ILLUSTRATOR = 1
PRIORITY_RANK_VINTAGE = 2
PRIORITY_RANK_GENERIC_COLLECTION = 3
PRIORITY_RANK_SV151 = 4
# Fallback for any collection name not covered by the rules above (e.g. a
# newly created Dex folder). Kept lowest priority so known rules always win.
PRIORITY_RANK_DEFAULT = 99


def priority_rank_for(collection_name: str) -> int:
    if collection_name in ILLUSTRATOR_COLLECTIONS:
        return PRIORITY_RANK_ILLUSTRATOR
    if collection_name == VINTAGE_COLLECTION_NAME:
        return PRIORITY_RANK_VINTAGE
    if collection_name == GENERIC_COLLECTION_NAME:
        return PRIORITY_RANK_GENERIC_COLLECTION
    if collection_name == SV151_COLLECTION_NAME:
        return PRIORITY_RANK_SV151
    return PRIORITY_RANK_DEFAULT


def is_excluded_category(category: str) -> bool:
    if category in EXCLUDED_CATEGORIES_EXACT:
        return True
    return any(category.startswith(prefix) for prefix in EXCLUDED_CATEGORIES_PREFIX)


def is_binder_category(category: str) -> bool:
    return category in BINDER_CATEGORIES


# Condition vocabulary for `Card.condition` (see models.py) and generated
# finn.no ad text (ads.py). Deliberately the same values as
# apps/finn_ad_scraper/models.py's CONDITIONS tuple -- apps don't
# import from each other, so this is a literal copy kept in sync by
# convention, not by shared code. Keep both lists identical if either
# changes.
CARD_CONDITIONS = (
    "Mint",
    "Near Mint",
    "Lightly Played",
    "Moderately Played",
    "Heavily Played",
    "Damaged",
    "Unknown",
)


# Short display codes for a card's print language (Card.language, from Dex's
# "Locale" column). Dex writes full names ("International", "Japanese",
# "Simplified Chinese") or three-letter codes ("ENG", "JPN"); everywhere the
# app shows a language it uses these short region-style codes instead.
# "International" is Dex's English-language catalog, hence EN. A value not
# listed here is shown as-is.
LANGUAGE_CODES = {
    "international": "EN",
    "english": "EN",
    "eng": "EN",
    "en": "EN",
    "japanese": "JP",
    "jpn": "JP",
    "ja": "JP",
    "korean": "KR",
    "kor": "KR",
    "ko": "KR",
    "simplified chinese": "CN",
    "chinese (simplified)": "CN",
    "chs": "CN",
    "traditional chinese": "TW",
    "chinese (traditional)": "TW",
    "cht": "TW",
    "german": "DE",
    "french": "FR",
    "italian": "IT",
    "spanish": "ES",
    "portuguese": "PT",
    "dutch": "NL",
    "polish": "PL",
    "russian": "RU",
    "thai": "TH",
    "indonesian": "ID",
}


def language_code(language: str | None) -> str:
    """"International" -> "EN", "Japanese" -> "JP", ...; "" for no language."""
    if not language or not language.strip():
        return ""
    return LANGUAGE_CODES.get(language.strip().lower(), language.strip())


# Physical print language that overrides Dex's "Locale" (issue #367). Dex
# has no Korean catalog for some sets (e.g. 151, VSTAR Universe), so the
# user logs those Korean cards as the Japanese print and marks them with a
# "KR" token in the card's Dex notes (Note 1..Note 5). The importer then
# stores Card.language = "Korean". Case-insensitive, whole word: "KR", "kr",
# "KR; mint", "bought KR lot" match; "KRAKEN", "okr" don't. Only
# Card.language changes: card_id, masterdata, prices and images all key off
# card_id, so a Korean-marked card keeps its Japanese identity and prices.
KOREAN = "Korean"
KOREAN_NOTE_TOKEN = "KR"
KOREAN_NOTE_RE = re.compile(rf"\b{KOREAN_NOTE_TOKEN}\b", re.IGNORECASE)

# TEMPORARY set-level fallback, keyed on the Dex card_id prefix before "-"
# ("jpn_sv2a-12" -> "jpn_sv2a"): every card of the set gets this language,
# note or not, until the user has tagged all of them with "KR" in Dex.
# Remove it by deleting the entry. While it's here a genuine Japanese sv2a
# card would also show as Korean (the user owns none).
PHYSICAL_LOCALE_OVERRIDES: dict[str, str] = {
    "jpn_sv2a": KOREAN,
}


def has_korean_note(notes: str | None) -> bool:
    """True when the notes contain the KR token as a whole word."""
    return bool(notes) and KOREAN_NOTE_RE.search(notes) is not None


def physical_language(card_id: str | None, locale: str | None, notes: str | None) -> str | None:
    """The language stored on Card.language at import: Korean for a "KR"
    note or a PHYSICAL_LOCALE_OVERRIDES set, else Dex's Locale (None when
    blank)."""
    if has_korean_note(notes):
        return KOREAN
    prefix = (card_id or "").split("-", 1)[0]
    if prefix in PHYSICAL_LOCALE_OVERRIDES:
        return PHYSICAL_LOCALE_OVERRIDES[prefix]
    return (locale or "").strip() or None
