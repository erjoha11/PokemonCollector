"""Identify the Pokemon cards in a finn.no ad's photos with Claude (vision).

One request per ad (photos are sent by URL, labelled "Photo 1..N"), answered
with structured outputs so the reply is always schema-valid JSON -- no
free-text parsing. Each card comes back in tcg_inventory's masterdata
vocabulary (language / set code / number / variant, see card_ids.py) so it
can be matched against the collection.
"""
from __future__ import annotations

import base64
import json
from typing import Iterable, Optional

import anthropic
import requests

from .card_ids import LANGUAGES, VARIANTS, dex_id, normalize_number, normalize_variant
from .models import CONDITIONS, CONFIDENCE_LEVELS, IdentifiedCard

DEFAULT_MODEL = "claude-opus-5"
MAX_TOKENS = 16000
# Photos per request. finn.no allows up to ~20 photos per ad, so one request
# per ad is the normal case; larger sets are split and merged.
MAX_PHOTOS_PER_REQUEST = 20
# Server-side refusal fallback: if the model declines, the API re-runs the
# request on Anthropic's recommended fallback model inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """You identify Pokemon Trading Card Game cards in photos from a Norwegian classifieds ad (finn.no). The seller's title and description are included; they are often in Norwegian and may name the cards, the set, or the condition, but trust what the photos show over what the text claims.

Report every distinct card you can see, once, with how many copies are visible. Read the details from the card itself:
- name: as printed (e.g. "Charizard ex", "Mew", "Professor's Research").
- set_name and set_code: the expansion, from the set symbol, the set code printed at the bottom (e.g. "PAL", "SV2a"), the card design, or the number format. set_code uses the lowercase ID style of pokemontcg.io for international prints (base1, sv2, sv3pt5, swsh12pt5) and TCGdex for Asian prints (sv2a, s12a, m2a). Leave it null rather than guess.
- printed_number: the collector number exactly as printed ("4/102", "199/165", "TG05/TG30", "SWSH050"); null if it can't be read.
- language: "int" for English and other international prints, "ja" Japanese, "ko" Korean, "zh-hans" / "zh-hant" Chinese; "unknown" if you can't tell.
- variant: the finish. "normal" (no foil), "holo" (foil artwork), "reverse_holo" (foil everywhere except the artwork), the Poke Ball / Master Ball / Friend Ball pattern holos of the 151 era, "cosmos_holo", "cracked_ice_holo", "first_edition" / "first_edition_holo", "shadowless", "expansion_stamp"; "unspecified" when the photo doesn't show it.
- condition: your best read of the physical condition from what is visible (edges, corners, surface, creases). "Unknown" when the photo is too small or blurry to judge.
- graded: the grading company and grade for a slabbed card ("PSA 10", "CGC 9.5"), else null.
- photos: the numbers of the photos the card appears in.
- confidence: "high" when name, set and number are all readable, "medium" when one is inferred, "low" when mostly guessed.
- notes: anything a buyer should know (damage, stamps, alterations, cards only partly visible) - one short sentence or empty.

Bulk piles or stacks where individual cards can't be told apart: report only the cards you can identify and mention the rest in the notes of the closest card. If no card can be identified, return an empty list."""

CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "set_name": {"type": ["string", "null"]},
                    "set_code": {"type": ["string", "null"]},
                    "printed_number": {"type": ["string", "null"]},
                    "language": {"type": "string", "enum": [*LANGUAGES, "unknown"]},
                    "variant": {"type": "string", "enum": list(VARIANTS)},
                    "condition": {"type": "string", "enum": list(CONDITIONS)},
                    "graded": {"type": ["string", "null"]},
                    "quantity": {"type": "integer"},
                    "photos": {"type": "array", "items": {"type": "integer"}},
                    "confidence": {"type": "string", "enum": list(CONFIDENCE_LEVELS)},
                    "notes": {"type": "string"},
                },
                "required": [
                    "name", "set_name", "set_code", "printed_number", "language", "variant",
                    "condition", "graded", "quantity", "photos", "confidence", "notes",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["cards"],
    "additionalProperties": False,
}


class CardIdentificationError(RuntimeError):
    """Claude couldn't return a card list for these photos."""


def identify_cards(
    image_urls: list[str],
    *,
    ad_context: str = "",
    client: Optional[anthropic.Anthropic] = None,
    model: str = DEFAULT_MODEL,
    inline_images: bool = False,
) -> list[IdentifiedCard]:
    """Identify the cards in an ad's photos.

    Photos are sent by URL, so Anthropic fetches them directly. Set
    `inline_images=True` to download them here and send them base64-encoded
    instead (for image hosts Anthropic can't reach). A request that fails
    because an image URL couldn't be fetched is retried once that way.
    """
    if not image_urls:
        return []
    client = client or anthropic.Anthropic()

    cards: list[IdentifiedCard] = []
    for offset in range(0, len(image_urls), MAX_PHOTOS_PER_REQUEST):
        batch = image_urls[offset : offset + MAX_PHOTOS_PER_REQUEST]
        try:
            raw = _request_cards(client, model, batch, offset, ad_context, inline_images)
        except anthropic.BadRequestError as exc:
            if inline_images or "image" not in str(exc).lower():
                raise
            raw = _request_cards(client, model, batch, offset, ad_context, inline_images=True)
        cards.extend(_to_card(item) for item in raw if item.get("name"))
    return merge_duplicates(cards)


def _request_cards(client, model, image_urls, offset, ad_context, inline_images) -> list[dict]:
    content: list[dict] = []
    for index, url in enumerate(image_urls, start=offset + 1):
        content.append({"type": "text", "text": f"Photo {index}:"})
        content.append({"type": "image", "source": _image_source(url, inline_images)})
    content.append(
        {
            "type": "text",
            "text": "Ad title and description (seller's own words):\n"
            + (ad_context.strip() or "(none)")
            + "\n\nIdentify the cards in the photos above.",
        }
    )

    response = client.beta.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": CARD_SCHEMA}},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    )

    if response.stop_reason == "refusal":
        category = getattr(response.stop_details, "category", None) if response.stop_details else None
        raise CardIdentificationError(f"The model declined to identify these photos (category: {category}).")
    if response.stop_reason == "max_tokens":
        raise CardIdentificationError("The card list was cut off (max_tokens); try fewer photos per request.")

    text = "".join(block.text for block in response.content if block.type == "text")
    try:
        return json.loads(text)["cards"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CardIdentificationError(f"Unexpected response format: {text[:200]!r}") from exc


def _image_source(url: str, inline: bool) -> dict:
    if not inline:
        return {"type": "url", "url": url}
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    media_type = response.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        media_type = "image/jpeg"
    return {"type": "base64", "media_type": media_type, "data": base64.standard_b64encode(response.content).decode()}


def _to_card(item: dict) -> IdentifiedCard:
    number, printed = normalize_number(item.get("printed_number"))
    language = item.get("language")
    language = None if language in (None, "", "unknown") else language
    set_code = (item.get("set_code") or "").strip().lower() or None
    return IdentifiedCard(
        name=item["name"].strip(),
        set_name=item.get("set_name") or None,
        set_code=set_code,
        number=number,
        printed_number=printed,
        language=language,
        variant=normalize_variant(item.get("variant")),
        condition=item.get("condition") if item.get("condition") in CONDITIONS else "Unknown",
        graded=item.get("graded") or None,
        quantity=max(int(item.get("quantity") or 1), 1),
        confidence=item.get("confidence") if item.get("confidence") in CONFIDENCE_LEVELS else "low",
        photos=sorted(set(item.get("photos") or [])),
        notes=(item.get("notes") or "").strip(),
        dex_id=dex_id(language, set_code, number),
    )


def merge_duplicates(cards: Iterable[IdentifiedCard]) -> list[IdentifiedCard]:
    """The same card reported twice (e.g. from two photo batches) becomes one
    entry with the copies added up and the photo lists combined."""
    merged: dict[tuple, IdentifiedCard] = {}
    for card in cards:
        key = (card.name.lower(), card.set_code, card.number, card.language, card.variant, card.graded)
        if key in merged:
            kept = merged[key]
            kept.quantity += card.quantity
            kept.photos = sorted(set(kept.photos) | set(card.photos))
        else:
            merged[key] = card
    return list(merged.values())
