"""The whole job in one call: fetch the ad, then identify its cards."""
from __future__ import annotations

from typing import Optional

from .card_identifier import DEFAULT_MODEL, identify_cards
from .finn_ad import fetch_finn_ad
from .models import AdAnalysis


def analyze_ad(
    url: str,
    *,
    client=None,
    model: str = DEFAULT_MODEL,
    use_browser: bool = True,
    inline_images: bool = False,
    html: Optional[str] = None,
) -> AdAnalysis:
    ad = fetch_finn_ad(url, html=html, use_browser=use_browser)
    context = f"{ad.title}\n\n{ad.description}".strip()
    cards = identify_cards(ad.images, ad_context=context, client=client, model=model, inline_images=inline_images)
    return AdAnalysis(ad=ad, cards=cards, model=model)
