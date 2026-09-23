"""Real card photos and live TCGPlayer prices, both looked up from the same
Pokemon TCG API (api.pokemontcg.io) call -- Dex itself has no card images and
importer.py's own Dex-CSV "Price" column is the only price signal otherwise.
That API's card IDs don't correspond to Dex's own `card_id`, so lookup is by
name + set + printed number instead, best-effort: any failure (network, no
match, ambiguous set name) just leaves the card without an image/price rather
than blocking an import. See importer.py for how often each is looked up --
image_url once and cached forever (a card's image never changes), tcgplayer
price on a staleness schedule (prices move).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

_API_URL = "https://api.pokemontcg.io/v2/cards"
_TIMEOUT = 5.0

# The Pokemon TCG API's tcgplayer prices are always USD; every other price in
# this app (Dex's own exported "Price" column, and every `| kr` template
# display) is NOK. A fixed approximate rate, not a live lookup -- one more
# external, flaky dependency isn't worth it for a number that's already a
# best-effort estimate. Revisit if USD/NOK drifts far from this over time.
_USD_TO_NOK = 10.5


@dataclass
class CardApiData:
    image_url: str | None
    tcgplayer_price: float | None
    # True when the API returned a card, but its own name/number didn't
    # exactly match what was searched for -- see _is_confident_match. A
    # low-confidence match still yields an image (best-effort, low stakes --
    # see module docstring), but never a price: a wrong image is a cosmetic
    # annoyance, a wrong price silently corrupts the Market Value KPI and
    # value-growth charts. Callers (importer.py, price_refresh.py) can use
    # this to flag/log a card worth a manual look rather than trusting a
    # guess.
    low_confidence_match: bool = False
    # True when the card had more than one priced print (normal/holofoil/
    # reverseHolofoil/...) and this module couldn't confidently tell which
    # one matches Dex's own `Variant` field -- see _match_variant_key. A
    # price is still returned (best-effort, same spirit as low_confidence_
    # match on the image side), but callers should surface it as worth a
    # manual look rather than trusting it silently, since different prints
    # of the same card can have very different market prices.
    variant_price_uncertain: bool = False


def _printed_number(number: str | None) -> str | None:
    """Dex's `number` is e.g. "23/107" (this card / set size); the API wants
    just the printed number, "23".
    """
    if not number:
        return None
    match = re.match(r"\d+", number.strip())
    return match.group(0) if match else None


# Dex's free-text `Variant` field and the API's `tcgplayer.prices` keys
# don't share a vocabulary, so this is a deliberately conservative,
# ordered (substring-in-Dex-variant -> candidate API key substrings) rule
# set -- first match wins. Only covers cases that are genuinely
# unambiguous; e.g. a plain "Holo" is left unmapped on purpose, since it
# could mean holofoil, reverseHolofoil, or unlimitedHolofoil and guessing
# wrong here would silently misprice a card exactly like the case this is
# meant to prevent. See _match_variant_key.
_VARIANT_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("1st edition", ("1stedition",)),
    ("reverse holo", ("reverseholofoil",)),
    ("normal", ("normal", "unlimited")),
]


def _match_variant_key(variant: str | None, price_keys: list[str]) -> str | None:
    """Best-effort match from Dex's own `Variant` value to one of the
    `tcgplayer.prices` keys actually present on this card. Returns None
    (never guesses) when the variant is missing or doesn't hit one of the
    unambiguous hints in _VARIANT_HINTS.
    """
    if not variant:
        return None
    lowered = variant.strip().lower()
    for hint, candidates in _VARIANT_HINTS:
        if hint not in lowered:
            continue
        for candidate in candidates:
            for key in price_keys:
                if candidate in key.lower():
                    return key
    return None


def _best_tcgplayer_price(tcgplayer: dict | None, variant: str | None = None) -> tuple[float | None, bool]:
    """`tcgplayer.prices` has one entry per print variant (normal, holofoil,
    reverseHolofoil, 1stEditionHolofoil, ...), each with market/low/mid/high,
    in USD. Returns (price_in_nok, uncertain):

    - No priced variant at all -> (None, False).
    - Exactly one priced variant -> that one, not uncertain (nothing to
      disambiguate regardless of what Dex's `Variant` says).
    - Multiple priced variants -> try to match Dex's own `Variant` field via
      _match_variant_key; if that succeeds, use it, not uncertain. If it
      can't be matched, fall back to the first priced variant present
      (better than no price at all) but flag it `uncertain=True` so callers
      can surface it rather than trust a guess silently -- different prints
      of the same card can have very different market prices.

    Converted to NOK here (see _USD_TO_NOK) since every other price in this
    app -- Dex's own column included -- is NOK; returning raw USD would
    silently understate these cards' value by ~10x wherever it's displayed.
    """
    if not tcgplayer:
        return None, False
    prices = tcgplayer.get("prices") or {}
    price_keys = [key for key, variant_prices in prices.items() if (variant_prices or {}).get("market") is not None]
    if not price_keys:
        return None, False

    def _price_in_nok(key: str) -> float:
        return round(prices[key]["market"] * _USD_TO_NOK, 2)

    if len(price_keys) == 1:
        return _price_in_nok(price_keys[0]), False

    matched_key = _match_variant_key(variant, price_keys)
    if matched_key:
        return _price_in_nok(matched_key), False

    return _price_in_nok(price_keys[0]), True


def _is_confident_match(name: str, number: str | None, card: dict) -> bool:
    """Post-fetch sanity check on the single candidate `fetch_card_data`
    picks (`data[0]`, see below). The search query already scopes by
    name/set/number, but the API's query parser does fuzzy/tokenized name
    matching, so the top result isn't guaranteed to actually be the exact
    card asked for -- fine when the payoff is just a card image, not fine
    when it silently feeds a wrong card's price into the Market Value KPI
    (see importer.py / models.Card.display_price). Requires the returned
    card's own name and printed number to match exactly (case-insensitive)
    before its price is trusted; set is intentionally not re-checked here
    since the query already filters on it and set-name formatting varies
    enough between Dex and this API to cause false negatives.
    """
    api_name = (card.get("name") or "").strip().lower()
    if api_name != (name or "").strip().lower():
        return False
    printed_number = _printed_number(number)
    if printed_number:
        api_number = (card.get("number") or "").strip()
        if api_number != printed_number:
            return False
    return True


def fetch_card_data(
    name: str, set_name: str | None, number: str | None, variant: str | None = None
) -> CardApiData:
    """Best-effort image URL and TCGPlayer market price for one card, from a
    single API call. Never raises -- a failed lookup just leaves both fields
    None rather than being a reason to fail an import. `variant` is Dex's
    own `Variant` field (e.g. "Normal", "Holo", "1st Edition"), used only to
    disambiguate which print's price to trust when a card has more than
    one -- see _best_tcgplayer_price.
    """
    if not name:
        return CardApiData(image_url=None, tcgplayer_price=None)

    query_parts = [f'name:"{name}"']
    if set_name:
        query_parts.append(f'set.name:"{set_name}"')
    printed_number = _printed_number(number)
    if printed_number:
        query_parts.append(f"number:{printed_number}")

    # The free tier of this API is noticeably flaky in practice -- repeated,
    # identical queries routinely 500/502 for no apparent reason -- so one
    # retry roughly doubles the real-world match rate instead of leaving a
    # card without an image/price over one bad response.
    data = None
    for _attempt in range(2):
        try:
            response = httpx.get(
                _API_URL,
                params={"q": " ".join(query_parts), "pageSize": 1},
                timeout=_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json().get("data") or []
            break
        except (httpx.HTTPError, ValueError):
            continue
    if not data:
        return CardApiData(image_url=None, tcgplayer_price=None)

    card = data[0]
    confident = _is_confident_match(name, number, card)
    price, variant_uncertain = (
        _best_tcgplayer_price(card.get("tcgplayer"), variant) if confident else (None, False)
    )
    return CardApiData(
        image_url=card.get("images", {}).get("small"),
        tcgplayer_price=price,
        low_confidence_match=not confident,
        variant_price_uncertain=variant_uncertain,
    )


def fetch_image_url(name: str, set_name: str | None, number: str | None) -> str | None:
    """Back-compat wrapper around fetch_card_data for callers that only
    want the image (currently just tests) -- importer.py itself calls
    fetch_card_data directly so it doesn't pay for two API round-trips.
    """
    return fetch_card_data(name, set_name, number).image_url


# --------------------------------------------------------------------------
# Image lookup by Dex's own card_id
# --------------------------------------------------------------------------
# fetch_card_data above searches by name + set name + number, which misses
# most cards: Dex's set names often don't match the API's, and Japanese
# prints aren't in the Pokemon TCG API at all. Dex's `card_id` is more
# useful than that module docstring gives it credit for:
#
# - International prints use the Pokemon TCG API's own card id scheme
#   ("ex5-4", "dv1-5", "hgss4-17"), so the card can be fetched directly by
#   id -- no search.
# - Japanese prints are "jpn_<set code>-<number>" ("jpn_sv2a-168"), which
#   TCGdex's Japanese catalog (api.tcgdex.net/v2/ja) has under the same set
#   code, just capitalized ("SV2a-168").
#
# Neither URL is ever built by hand and trusted (the 2026-09-16 hand-rolled
# assets.tcgdex.net URLs all 404'd, see HANDOFF.md): each lookup fetches the
# card from the API and only uses the image the API itself returns, after
# checking the returned card's number (and, for international cards, name)
# matches Dex's -- a wrong image is worse than none.
_POKEMONTCG_CARD_URL = "https://api.pokemontcg.io/v2/cards/{card_id}"
_TCGDEX_JA_CARD_URL = "https://api.tcgdex.net/v2/ja/cards/{card_id}"
_INTERNATIONAL_ID = re.compile(r"^[a-z0-9.]+-[A-Za-z0-9]+$")
_JAPANESE_ID = re.compile(r"^jpn_([a-z0-9.]+)-([A-Za-z0-9]+)$")


def _get_json(url: str) -> dict | None:
    """GET with the same one-retry-on-flakiness policy as fetch_card_data.
    None on 404 (no such card), repeated errors, or a non-JSON body."""
    for _attempt in range(2):
        try:
            response = httpx.get(url, timeout=_TIMEOUT)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            continue
    return None


def _same_number(api_number: str | None, dex_number: str | None) -> bool:
    """"4" vs Dex's "4/101", "001" vs "1" -- compared as the printed number,
    ignoring leading zeros."""
    printed = _printed_number(dex_number)
    api_printed = _printed_number(api_number)
    if printed is None or api_printed is None:
        return False
    return int(printed) == int(api_printed)


def _names_overlap(dex_name: str, api_name: str) -> bool:
    """Loose name check for a by-id hit: Dex and the API word some names
    differently ("Dark Celebi" vs "Celebi", "Charizard ex" vs "Charizard-EX"),
    so any shared word of 3+ letters is enough -- the id + number already
    pin the card down; this only catches a scheme mismatch."""
    words = lambda s: {w for w in re.findall(r"[a-z]+", (s or "").lower()) if len(w) >= 3}
    return bool(words(dex_name) & words(api_name))


def _tcgdex_set_ids(set_code: str) -> list[str]:
    """TCGdex capitalizes the letter prefix of a Japanese set code and keeps
    any trailing letter lowercase ("sv2a" -> "SV2a", "s12a" -> "S12a",
    "sm12a" -> "SM12a"). A couple of fallbacks in case a code doesn't
    follow that, most likely first."""
    match = re.match(r"^([a-z]+)(.*)$", set_code)
    candidates = []
    if match:
        candidates.append(match.group(1).upper() + match.group(2))
    candidates += [set_code.upper(), set_code]
    return list(dict.fromkeys(candidates))


def _pokemontcg_image(card_id: str, name: str, number: str | None) -> str | None:
    payload = _get_json(_POKEMONTCG_CARD_URL.format(card_id=card_id))
    card = (payload or {}).get("data") or {}
    if not card or card.get("id") != card_id:
        return None
    if not _same_number(card.get("number"), number) or not _names_overlap(name, card.get("name")):
        return None
    images = card.get("images") or {}
    return images.get("small") or images.get("large")


def _tcgdex_ja_image(set_code: str, local_id: str, number: str | None) -> str | None:
    printed = _printed_number(number) or _printed_number(local_id)
    if printed is None:
        return None
    ids = [f"{set_id}-{n}" for set_id in _tcgdex_set_ids(set_code) for n in dict.fromkeys([local_id, printed, printed.zfill(3)])]
    for tcgdex_id in ids:
        card = _get_json(_TCGDEX_JA_CARD_URL.format(card_id=tcgdex_id))
        if not card:
            continue
        api_set = ((card.get("set") or {}).get("id") or "").lower()
        if api_set != set_code.lower() or not _same_number(card.get("localId"), printed):
            return None  # found *a* card, but not this one -- don't keep guessing
        image = card.get("image")
        # TCGdex returns the image as a base URL; quality + format are
        # appended per its asset docs.
        return f"{image}/low.webp" if image else None
    return None


def fetch_image_by_card_id(card_id: str | None, name: str, number: str | None) -> str | None:
    """Image URL for a card, looked up by Dex's own `card_id` (see the
    comment block above). Never raises; None when the id isn't one of the
    two known schemes, the API has no such card, or the returned card
    doesn't match."""
    if not card_id:
        return None
    japanese = _JAPANESE_ID.match(card_id)
    if japanese:
        return _tcgdex_ja_image(japanese.group(1), japanese.group(2), number)
    if _INTERNATIONAL_ID.match(card_id):
        return _pokemontcg_image(card_id, name, number)
    return None
