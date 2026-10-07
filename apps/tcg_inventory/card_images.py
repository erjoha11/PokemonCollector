"""Real card photos and live TCGPlayer prices, both looked up from the same
Pokemon TCG API (api.pokemontcg.io) call -- Dex itself has no card images and
importer.py's own Dex-CSV "Price" column is the only price signal otherwise.
That API's card IDs don't correspond to Dex's own `card_id`, so lookup is by
name + set + printed number instead, best-effort: any failure (network, no
match, ambiguous set name) just leaves the card without an image/price rather
than blocking an import. Images are looked up once and cached forever (a
card's image never changes), by the Dex sync and backfill_images.py.

Prices (issue #349) are no longer searched card by card: price_refresh.py
fetches them daily by stored pokemontcg.io ID in batches
(pokemontcg_client.py) and only falls back to this module's search for a
card whose ID isn't on pokemontcg.io. The price rules (_choose_tcgplayer_
price, _is_confident_match, _same_number, _names_overlap) live here and are
shared by both paths.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

import fx_rates
import masterdata

_API_URL = "https://api.pokemontcg.io/v2/cards"
_TIMEOUT = 5.0

# The Pokemon TCG API's tcgplayer prices are always USD; every other price in
# this app (Dex's own exported "Price" column, and every `| kr` template
# display) is NOK. Converted at Norges Bank's daily USD/NOK rate via
# fx_rates.py (fetched once per run and cached, with a last-known-rate and
# then a fixed-constant fallback so a failed lookup never blocks pricing).
# Until issue #209 this was a fixed `_USD_TO_NOK = 10.5`, ~10% above the real
# rate -- prices stored before that fix stay inflated until re-priced (see
# price_refresh.py's --reprice-all and HANDOFF.md).


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
    # The native value behind tcgplayer_price (issue #210): the USD market
    # price, which `tcgplayer.prices` key it came from, and the USD/NOK rate
    # it was converted at. Recorded on the card's `pokemontcg` card_prices
    # row. None whenever tcgplayer_price is None.
    tcgplayer_price_usd: float | None = None
    tcgplayer_variant_key: str | None = None
    usd_to_nok: float | None = None
    # True when the API had a confident match but the only USD/NOK rate
    # available was fx_rates' fixed fallback constant (issue #229): no price
    # is returned (tcgplayer_price is None), and callers must treat this as
    # "try again later", not as a failed lookup. (The daily price pass,
    # price_refresh.py, checks the rate itself before it starts, issue #349.)
    fx_unavailable: bool = False


def _printed_number(number: str | None) -> str | None:
    """Dex's `number` is e.g. "23/107" (this card / set size); the API wants
    just the printed number, "23".
    """
    if not number:
        return None
    match = re.match(r"\d+", number.strip())
    return match.group(0) if match else None


# Which `tcgplayer.prices` key is which Dex print (issue #350). Keyed by
# masterdata's variant code (masterdata.normalize_variant of Dex's Variant),
# never by substrings of the raw text. Each code lists the keys that are
# exactly that print, in order of preference. Keys are compared lowercased
# with hyphens removed, because pokemontcg.io spells "reverseHolofoil" and
# TCGdex spells "reverse-holofoil". Deliberately narrow:
#
# - "holo" is only ever `holofoil`. WotC sets have `unlimitedHolofoil` /
#   `1stEditionHolofoil` instead, and those stay unmatched.
# - "normal" is `normal`, or WotC's `unlimited` (the non-holo print that
#   isn't 1st Edition; Dex has its own "1st Edition" variant for that one).
#   The old substring rule matched `unlimited` too, so this keeps WotC
#   "Normal" cards on the right print.
# - "first_edition" is WotC's non-holo `1stEdition` key. On a holo-only card
#   (`1stEditionHolofoil` + `unlimitedHolofoil`) the one 1st Edition key is
#   that print, see _match_variant_key.
#
# A code missing from this table, other than "unspecified", is a print
# TCGplayer has no key for: Poké Ball / Master Ball / any "<x> Ball Holo"
# pattern, cosmos and cracked-ice holos, stamped and shadowless prints, and
# any variant code Dex adds later. It never gets a TCGplayer price (see
# has_tcgplayer_print).
_TCGPLAYER_KEYS: dict[str, tuple[str, ...]] = {
    "normal": ("normal", "unlimited"),
    "holo": ("holofoil",),
    "reverse_holo": ("reverseholofoil",),
    "first_edition": ("1stedition", "1steditionnormal"),
    "first_edition_holo": ("1steditionholofoil",),
}
_UNSPECIFIED = "unspecified"


def _key_form(key: str) -> str:
    return key.replace("-", "").lower()


def has_tcgplayer_print(variant: str | None) -> bool:
    """False when Dex's `Variant` is a print TCGplayer has no price key for
    (a Poké Ball / Master Ball pattern, a cosmos holo, ...). Such a card
    gets no price from pokemontcg.io or TCGdex's TCGplayer block, because
    the only prices there are for other prints of it (issue #350). A blank
    variant ("unspecified") is still priceable."""
    code = masterdata.normalize_variant(variant)
    return code == _UNSPECIFIED or code in _TCGPLAYER_KEYS


def _match_variant_key(variant: str | None, price_keys: list[str]) -> str | None:
    """The `tcgplayer.prices` key (as given in `price_keys`) that is exactly
    Dex's print, by masterdata variant code (_TCGPLAYER_KEYS), or None when
    no key is that print. Never guesses; whether to fall back to another
    print is the caller's decision (see _choose_tcgplayer_price)."""
    code = masterdata.normalize_variant(variant)
    by_form = {_key_form(key): key for key in price_keys}
    for candidate in _TCGPLAYER_KEYS.get(code, ()):
        if candidate in by_form:
            return by_form[candidate]
    if code == "first_edition":
        first_edition = [key for form, key in by_form.items() if form.startswith("1stedition")]
        if len(first_edition) == 1:
            return first_edition[0]
    return None


@dataclass(frozen=True)
class _PriceChoice:
    nok: float
    usd: float
    key: str
    usd_to_nok: float
    uncertain: bool


def _best_tcgplayer_price(tcgplayer: dict | None, variant: str | None = None) -> tuple[float | None, bool]:
    """(price_in_nok, uncertain) -- see _choose_tcgplayer_price."""
    choice = _choose_tcgplayer_price(tcgplayer, variant)
    return (choice.nok, choice.uncertain) if choice else (None, False)


def _choose_tcgplayer_price(tcgplayer: dict | None, variant: str | None = None) -> _PriceChoice | None:
    """`tcgplayer.prices` has one entry per print variant (normal, holofoil,
    reverseHolofoil, 1stEditionHolofoil, ...), each with market/low/mid/high,
    in USD. Returns the chosen print's price (NOK and native USD), its key,
    the rate used and whether the choice is uncertain:

    - Dex's `Variant` is a print TCGplayer has no key for (a Poké Ball /
      Master Ball pattern, ..., see has_tcgplayer_print) -> None, even when
      the card has just one priced print: that price is another print's
      (issue #350). Nothing is written, so the price chain falls through
      to Cardmarket, which prices ball patterns as their own product.
    - No priced variant at all -> None.
    - Exactly one priced variant -> that one, not uncertain (nothing to
      disambiguate regardless of what Dex's `Variant` says).
    - Multiple priced variants -> the key that is exactly Dex's print
      (_match_variant_key), not uncertain. If there's none (a blank
      variant, or e.g. a WotC "Holo" between `1stEditionHolofoil` and
      `unlimitedHolofoil`), fall back to the first priced variant present
      (better than no price at all) but flag it `uncertain=True` so callers
      can surface it rather than trust a guess silently -- different prints
      of the same card can have very different market prices.

    Converted to NOK here at the current Norges Bank USD/NOK rate (see
    fx_rates.get_rates) since every other price in this app -- Dex's own
    column included -- is NOK; returning raw USD would silently understate
    these cards' value by ~10x wherever it's displayed.
    """
    if not tcgplayer or not has_tcgplayer_print(variant):
        return None
    prices = tcgplayer.get("prices") or {}
    price_keys = [key for key, variant_prices in prices.items() if (variant_prices or {}).get("market") is not None]
    if not price_keys:
        return None

    usd_to_nok = fx_rates.usd_to_nok()

    def _choice(key: str, uncertain: bool) -> _PriceChoice:
        usd = prices[key]["market"]
        return _PriceChoice(round(usd * usd_to_nok, 2), usd, key, usd_to_nok, uncertain)

    if len(price_keys) == 1:
        return _choice(price_keys[0], False)

    matched_key = _match_variant_key(variant, price_keys)
    if matched_key:
        return _choice(matched_key, False)

    return _choice(price_keys[0], True)


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


def search_query(name: str, set_name: str | None, number: str | None) -> str:
    """The name + set name + printed number query for the card search
    (also pokemontcg_client.Client.search's)."""
    query_parts = [f'name:"{name}"']
    if set_name:
        query_parts.append(f'set.name:"{set_name}"')
    printed_number = _printed_number(number)
    if printed_number:
        query_parts.append(f"number:{printed_number}")
    return " ".join(query_parts)


def _search_card(name: str, set_name: str | None, number: str | None) -> dict | None:
    """The search's top hit, or None (no name, no match, or the API failed
    twice). Never raises."""
    if not name:
        return None
    # The free tier of this API is noticeably flaky in practice -- repeated,
    # identical queries routinely 500/502 for no apparent reason -- so one
    # retry roughly doubles the real-world match rate instead of leaving a
    # card without an image/price over one bad response.
    data = None
    for _attempt in range(2):
        try:
            response = httpx.get(
                _API_URL,
                params={"q": search_query(name, set_name, number), "pageSize": 1},
                timeout=_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json().get("data") or []
            break
        except (httpx.HTTPError, ValueError):
            continue
    return data[0] if data else None


def search_image_url(name: str, set_name: str | None, number: str | None) -> str | None:
    """The image of the search's top hit, best-effort, with no price and no
    exchange rate involved. The Dex sync's fallback when the by-ID image
    lookup found nothing (it no longer looks up prices, issue #349)."""
    card = _search_card(name, set_name, number)
    return ((card or {}).get("images") or {}).get("small")


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
    card = _search_card(name, set_name, number)
    if card is None:
        return CardApiData(image_url=None, tcgplayer_price=None)

    confident = _is_confident_match(name, number, card)
    # Never convert at the fallback constant (issue #229, fx_rates docstring):
    # no price this time, flagged so the caller leaves the card due.
    fx_unavailable = confident and not fx_rates.get_rates().usable("USD")
    choice = _choose_tcgplayer_price(card.get("tcgplayer"), variant) if confident and not fx_unavailable else None
    return CardApiData(
        image_url=card.get("images", {}).get("small"),
        tcgplayer_price=choice.nok if choice else None,
        low_confidence_match=not confident,
        variant_price_uncertain=choice.uncertain if choice else False,
        tcgplayer_price_usd=choice.usd if choice else None,
        tcgplayer_variant_key=choice.key if choice else None,
        usd_to_nok=choice.usd_to_nok if choice else None,
        fx_unavailable=fx_unavailable,
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
