"""
Fetch Pokémon trading card data (including prices) from the Pokémon TCG API.
API docs: https://docs.pokemontcg.io/
"""

import logging
import math
import time
from typing import Any

try:
    import requests
except ImportError as exc:
    raise ImportError("The 'requests' library is required. Install it with: pip install requests") from exc

BASE_URL = "https://api.pokemontcg.io/v2/cards"
PAGE_SIZE = 250  # maximum allowed by the API

logger = logging.getLogger(__name__)


def get_all_pokemon_cards(
    api_key: str | None = None,
    query: str | None = None,
    max_cards: int | None = None,
    delay_between_requests: float = 0.1,
) -> list[dict[str, Any]]:
    """Return a list of all Pokémon trading cards with pricing information.

    Each card dictionary includes (among other fields):
      - id, name, supertype, subtypes, hp, types
      - set  (name, series, releaseDate …)
      - rarity
      - images (small, large)
      - tcgplayer  → prices keyed by variant (normal, holofoil, reverseHolofoil …)
                    each variant has: low, mid, high, market, directLow
      - cardmarket → prices keyed by variant; each has averageSellPrice,
                    lowPrice, trendPrice, avg1, avg7, avg30 …

    Args:
        api_key:  Optional Pokémon TCG API key.  Without a key the API still
                  works but is rate-limited to ~1,000 requests/day.
        query:    Optional API query string to filter results, e.g.
                  ``"set.id:base1"`` or ``"name:Pikachu"``.
        max_cards: Stop after collecting this many cards (useful for testing).
        delay_between_requests: Seconds to sleep between paginated requests to
                                 stay within rate limits.

    Returns:
        A list of card dictionaries as returned by the Pokémon TCG API,
        augmented with nothing — the raw API payloads are returned as-is so
        callers can access any field they need.

    Raises:
        requests.HTTPError: If the API returns a non-2xx status code.
        requests.ConnectionError: If a network connectivity issue occurs.
        requests.Timeout: If a request exceeds the configured timeout.
    """
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key

    params: dict[str, Any] = {"pageSize": PAGE_SIZE, "page": 1}
    if query:
        params["q"] = query

    all_cards: list[dict[str, Any]] = []
    total_count: int | None = None

    while True:
        try:
            response = requests.get(BASE_URL, headers=headers, params=params, timeout=30)
            response.raise_for_status()
        except requests.ConnectionError as exc:
            raise requests.ConnectionError(
                "Unable to reach the Pokémon TCG API. Check your internet connection."
            ) from exc
        except requests.Timeout as exc:
            raise requests.Timeout(
                "Request to the Pokémon TCG API timed out. "
                "Consider increasing the timeout or retrying later."
            ) from exc

        payload = response.json()

        cards: list[dict[str, Any]] = payload.get("data", [])
        all_cards.extend(cards)

        if total_count is None:
            total_count = payload.get("totalCount", 0)
            total_pages = math.ceil(total_count / PAGE_SIZE)
            logger.info("Fetching %d cards across %d page(s)…", total_count, total_pages)

        current_page: int = payload.get("page", params["page"])

        logger.info(
            "Page %d: retrieved %d cards (total so far: %d)",
            current_page,
            len(cards),
            len(all_cards),
        )

        if max_cards and len(all_cards) >= max_cards:
            all_cards = all_cards[:max_cards]
            break

        # Stop when we have fetched every available card
        if len(all_cards) >= total_count:
            break

        params["page"] = current_page + 1
        time.sleep(delay_between_requests)

    return all_cards


def extract_prices(card: dict[str, Any]) -> dict[str, Any]:
    """Extract just the pricing section from a card dictionary.

    Returns a dict with keys ``"tcgplayer"`` and ``"cardmarket"`` (either may
    be ``None`` if the card has no pricing data for that platform).
    """
    return {
        "id": card.get("id"),
        "name": card.get("name"),
        "set": card.get("set", {}).get("name"),
        "tcgplayer": card.get("tcgplayer", {}).get("prices"),
        "cardmarket": card.get("cardmarket", {}).get("prices"),
    }
