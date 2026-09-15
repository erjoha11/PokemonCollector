"""Real card photos, looked up from the Pokemon TCG API (api.pokemontcg.io)
-- Dex itself has no card images. That API's card IDs don't correspond to
Dex's own `card_id`, so lookup is by name + set + printed number instead,
best-effort: any failure (network, no match, ambiguous set name) just leaves
the card without an image rather than blocking an import. See importer.py's
`_MAX_IMAGE_LOOKUPS_PER_IMPORT` for how this is kept from slowing a large
sync down -- looked up once per card and cached in `Card.image_url`
(never re-fetched once set, since a card's image never changes).
"""
from __future__ import annotations

import re

import httpx

_API_URL = "https://api.pokemontcg.io/v2/cards"
_TIMEOUT = 5.0


def _printed_number(number: str | None) -> str | None:
    """Dex's `number` is e.g. "23/107" (this card / set size); the API wants
    just the printed number, "23".
    """
    if not number:
        return None
    match = re.match(r"\d+", number.strip())
    return match.group(0) if match else None


def fetch_image_url(name: str, set_name: str | None, number: str | None) -> str | None:
    """Best-effort small-image URL for one card, or None if no confident
    match was found or the lookup couldn't be completed. Never raises --
    a failed lookup is not a reason to fail an import.
    """
    if not name:
        return None

    query_parts = [f'name:"{name}"']
    if set_name:
        query_parts.append(f'set.name:"{set_name}"')
    printed_number = _printed_number(number)
    if printed_number:
        query_parts.append(f"number:{printed_number}")

    # The free tier of this API is noticeably flaky in practice -- repeated,
    # identical queries routinely 500/502 for no apparent reason -- so one
    # retry roughly doubles the real-world match rate instead of leaving a
    # card imageless over one bad response.
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
        return None
    return data[0].get("images", {}).get("small")
