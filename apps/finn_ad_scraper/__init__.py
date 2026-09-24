"""Open a finn.no ad and identify the Pokemon cards in its photos.

    from finn_ad_scraper import analyze_ad
    result = analyze_ad("https://www.finn.no/recommerce/forsale/item/123456789")
"""
from .card_identifier import CardIdentificationError, identify_cards
from .finn_ad import FinnAdFetchError, fetch_finn_ad, parse_ad
from .models import AdAnalysis, FinnAd, IdentifiedCard
from .pipeline import analyze_ad

__all__ = [
    "AdAnalysis",
    "CardIdentificationError",
    "FinnAd",
    "FinnAdFetchError",
    "IdentifiedCard",
    "analyze_ad",
    "fetch_finn_ad",
    "identify_cards",
    "parse_ad",
]
