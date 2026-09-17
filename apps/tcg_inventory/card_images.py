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


def _printed_number(number: str | None) -> str | None:
    """Dex's `number` is e.g. "23/107" (this card / set size); the API wants
    just the printed number, "23".
    """
    if not number:
        return None
    match = re.match(r"\d+", number.strip())
    return match.group(0) if match else None


def _best_tcgplayer_price(tcgplayer: dict | None) -> float | None:
    """`tcgplayer.prices` has one entry per print variant (normal, holofoil,
    reverseHolofoil, 1stEditionHolofoil, ...), each with market/low/mid/high,
    in USD. There's no reliable way to match a variant name to Dex's own
    `Variant` field, so just take the first variant's `market` price present
    -- better than no price at all, and this is already how Dex's own Price
    column is presumably sourced (a single number per card, not per variant).
    Converted to NOK here (see _USD_TO_NOK) since every other price in this
    app -- Dex's own column included -- is NOK; returning raw USD would
    silently understate these cards' value by ~10x wherever it's displayed.
    """
    if not tcgplayer:
        return None
    prices = tcgplayer.get("prices") or {}
    for variant_prices in prices.values():
        market = (variant_prices or {}).get("market")
        if market is not None:
            return round(market * _USD_TO_NOK, 2)
    return None


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


def fetch_card_data(name: str, set_name: str | None, number: str | None) -> CardApiData:
    """Best-effort image URL and TCGPlayer market price for one card, from a
    single API call. Never raises -- a failed lookup just leaves both fields
    None rather than being a reason to fail an import.
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
    return CardApiData(
        image_url=card.get("images", {}).get("small"),
        tcgplayer_price=_best_tcgplayer_price(card.get("tcgplayer")) if confident else None,
        low_confidence_match=not confident,
    )


def fetch_image_url(name: str, set_name: str | None, number: str | None) -> str | None:
    """Back-compat wrapper around fetch_card_data for callers that only
    want the image (currently just tests) -- importer.py itself calls
    fetch_card_data directly so it doesn't pay for two API round-trips.
    """
    return fetch_card_data(name, set_name, number).image_url
