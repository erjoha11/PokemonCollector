"""A small client for pokemontcg.io's card search, for the daily pokemontcg
price pass (price_refresh.py, issue #349).

Two calls:

- `cards_by_ids(ids)`: one request for up to CHUNK_SIZE cards by their
  pokemontcg.io IDs (`q=id:"a" OR id:"b" ...`), only the fields the price
  pass needs. Every international card in the collection fits in 10 of
  these (checked 2026-10-07: 50 IDs in one request, ~6 s).
- `search(name, set_name, number)`: the old name + set name + number search
  (card_images.fetch_card_data's query), used only as a fallback for a card
  whose stored ID isn't on pokemontcg.io.

Same failure policy as tcgdex_prices.Client: a timeout, 429 or 5xx is
retried once after a back-off and then raised as TransientError (RateLimited
for a 429), which says nothing about the cards asked for. The keyless API
500/502s intermittently, several times in a row on a bad day. Any other 4xx
is raised as TransientError too (a rejected query isn't the card's fault),
without a retry.
"""
from __future__ import annotations

import time
from typing import Callable, Iterable

import httpx

import card_images

API_URL = "https://api.pokemontcg.io/v2/cards"
CHUNK_SIZE = 50
SELECT = "id,name,number,tcgplayer"
_TIMEOUT = 12.0
REQUEST_INTERVAL_S = 0.5  # pause between requests (keyless tier, limits unknown)
BACKOFF_S = 3.0  # wait before the one retry of a 429/5xx/timeout
MAX_RETRY_AFTER_S = 15.0

# Indirection so the test suite can make every pause instant (conftest.py).
_SLEEP = time.sleep


class TransientError(Exception):
    """A request that failed for reasons that say nothing about the cards
    (timeout, 5xx, rate limit, rejected query). Never recorded as a failed
    lookup."""


class RateLimited(TransientError):
    pass


def id_query(ids: Iterable[str]) -> str:
    """`id:"a" OR id:"b" ...` -- IDs quoted, as the issue's probe did."""
    return " OR ".join(f'id:"{card_id}"' for card_id in ids)


class Client:
    def __init__(self, sleep: Callable[[float], None] | None = None):
        self._sleep = sleep or (lambda seconds: _SLEEP(seconds))
        self._last_request: float | None = None
        self.calls = 0  # HTTP requests made, retries included
        self.batch_requests = 0
        self.searches = 0

    def _pause(self) -> None:
        if self._last_request is not None:
            wait = REQUEST_INTERVAL_S - (time.monotonic() - self._last_request)
            if wait > 0:
                self._sleep(wait)

    def _get(self, params: dict) -> list[dict]:
        for attempt in range(2):
            self._pause()
            self.calls += 1
            try:
                response = httpx.get(API_URL, params=params, timeout=_TIMEOUT)
            except httpx.HTTPError as exc:
                self._last_request = time.monotonic()
                if attempt == 0:
                    self._sleep(BACKOFF_S)
                    continue
                raise TransientError(f"pokemontcg.io: {exc.__class__.__name__}") from exc
            self._last_request = time.monotonic()
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 0:
                    self._sleep(_retry_after(response))
                    continue
                cls = RateLimited if response.status_code == 429 else TransientError
                raise cls(f"pokemontcg.io: HTTP {response.status_code}")
            if response.status_code != 200:
                raise TransientError(f"pokemontcg.io: HTTP {response.status_code}")
            try:
                data = response.json().get("data")
            except (ValueError, AttributeError) as exc:
                raise TransientError("pokemontcg.io: invalid JSON") from exc
            if not isinstance(data, list):
                raise TransientError("pokemontcg.io: no data list in the response")
            return [card for card in data if isinstance(card, dict)]
        raise TransientError("pokemontcg.io")  # pragma: no cover (loop always returns/raises)

    def cards_by_ids(self, ids: list[str]) -> dict[str, dict]:
        """{id: card} for the IDs pokemontcg.io has. An ID missing from the
        result is not on pokemontcg.io (the response was a 200)."""
        if len(ids) > CHUNK_SIZE:
            raise ValueError(f"at most {CHUNK_SIZE} IDs per request")
        self.batch_requests += 1
        data = self._get({"q": id_query(ids), "select": SELECT, "pageSize": 250})
        return {card["id"]: card for card in data if card.get("id")}

    def search(self, name: str, set_name: str | None, number: str | None) -> dict | None:
        """The top hit of the name + set name + number search, or None."""
        self.searches += 1
        query = card_images.search_query(name, set_name, number)
        data = self._get({"q": query, "select": SELECT, "pageSize": 1})
        return data[0] if data else None


def _retry_after(response) -> float:
    try:
        value = float(response.headers.get("retry-after", ""))
    except ValueError:
        return BACKOFF_S
    return max(0.0, min(value, MAX_RETRY_AFTER_S))
