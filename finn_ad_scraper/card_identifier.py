"""Identify Pokemon cards shown in a finn.no ad's photos using a vision LLM."""
from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from typing import Optional

import requests

DEFAULT_MODEL = "claude-sonnet-5"
MAX_IMAGES_PER_REQUEST = 5

CONDITIONS = (
    "Mint",
    "Near Mint",
    "Lightly Played",
    "Moderately Played",
    "Heavily Played",
    "Damaged",
    "Unknown",
)

_SYSTEM_PROMPT = f"""You are a Pokemon Trading Card Game grading and identification expert.
You will be shown photo(s) from a single online classifieds ad selling Pokemon cards,
along with the ad's title and description text for context.

Identify every distinct Pokemon card you can see. For each one report:
- name: the Pokemon/card name as printed on the card
- set_name: the expansion/set name if identifiable from the card design, symbol, or text (else null)
- card_number: the "xxx/yyy" collector number printed on the card if visible (else null)
- is_holo: true if the card is holofoil/reverse-holo/foil, false if clearly not, null if unclear
- condition: your best estimate of physical condition, one of {list(CONDITIONS)}
- quantity: how many copies of this exact card are visible (usually 1)
- confidence: your confidence in this identification, one of "high", "medium", "low"
- notes: brief notes on anything relevant (damage, holo pattern, language, graded slab, etc.)

Respond with ONLY a JSON array of objects with exactly those fields. No prose, no markdown
fences. If no cards are identifiable, respond with an empty JSON array: []
"""


@dataclass
class IdentifiedCard:
    name: str
    set_name: Optional[str] = None
    card_number: Optional[str] = None
    is_holo: Optional[bool] = None
    condition: str = "Unknown"
    quantity: int = 1
    confidence: str = "low"
    notes: str = ""


class CardIdentificationError(RuntimeError):
    pass


def identify_cards(
    image_urls: list[str],
    *,
    ad_context: str = "",
    client=None,
    model: str = DEFAULT_MODEL,
) -> list[IdentifiedCard]:
    """Identify Pokemon cards visible in a set of ad photos.

    Parameters
    ----------
    image_urls:
        Photo URLs taken from the ad (see `FinnAd.images`).
    ad_context:
        Free text (e.g. the ad's title + description) given to the model as
        extra context to help disambiguate.
    client:
        An `anthropic.Anthropic`-compatible client. Pass a fake/mock in tests.
        When omitted, a real client is constructed from the ANTHROPIC_API_KEY
        environment variable.
    model:
        The vision-capable model to use.
    """
    if not image_urls:
        return []

    if client is None:
        client = _default_client()

    all_cards: list[IdentifiedCard] = []
    for batch in _chunk(image_urls, MAX_IMAGES_PER_REQUEST):
        content = _build_image_content(batch)
        content.append(
            {
                "type": "text",
                "text": f"Ad title/description for context:\n{ad_context or '(none provided)'}",
            }
        )

        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
        text = _extract_text(response)
        all_cards.extend(_parse_cards(text))

    return _dedupe(all_cards)


def _default_client():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise CardIdentificationError(
            "the `anthropic` package is required; install it with `pip install anthropic`"
        ) from exc

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise CardIdentificationError("ANTHROPIC_API_KEY environment variable is not set")
    return anthropic.Anthropic(api_key=api_key)


def _build_image_content(image_urls: list[str]) -> list[dict]:
    content: list[dict] = []
    for url in image_urls:
        media_type, data = _download_image_as_base64(url)
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": data,
                },
            }
        )
    return content


def _download_image_as_base64(url: str) -> tuple[str, str]:
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    media_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
    if not media_type.startswith("image/"):
        media_type = "image/jpeg"
    return media_type, base64.b64encode(resp.content).decode("ascii")


def _extract_text(response) -> str:
    parts = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None) if not isinstance(block, dict) else block.get("text")
        if text:
            parts.append(text)
    return "".join(parts)


def _parse_cards(text: str) -> list[IdentifiedCard]:
    text = text.strip()
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        text = match.group(0)
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return []

    cards = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        cards.append(
            IdentifiedCard(
                name=str(item["name"]).strip(),
                set_name=(item.get("set_name") or None),
                card_number=(item.get("card_number") or None),
                is_holo=item.get("is_holo"),
                condition=item.get("condition") or "Unknown",
                quantity=int(item.get("quantity") or 1),
                confidence=item.get("confidence") or "low",
                notes=item.get("notes") or "",
            )
        )
    return cards


def _dedupe(cards: list[IdentifiedCard]) -> list[IdentifiedCard]:
    merged: dict[tuple, IdentifiedCard] = {}
    for card in cards:
        key = (card.name.lower(), (card.set_name or "").lower(), card.card_number)
        if key in merged:
            merged[key].quantity += card.quantity
        else:
            merged[key] = card
    return list(merged.values())


def _chunk(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]
