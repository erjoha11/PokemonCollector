"""Data types shared by every step of the pipeline (fetch -> parse -> identify)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Physical-condition vocabulary. Deliberately the same values as
# apps/tcg_inventory/constants.py's CARD_CONDITIONS -- apps don't import from
# each other, so this is a literal copy kept in sync by convention. Keep both
# lists identical if either changes.
CONDITIONS = (
    "Mint",
    "Near Mint",
    "Lightly Played",
    "Moderately Played",
    "Heavily Played",
    "Damaged",
    "Unknown",
)

CONFIDENCE_LEVELS = ("high", "medium", "low")


@dataclass
class FinnAd:
    """What a finn.no ad says about itself: text, asking price and photos."""

    url: str
    title: str
    description: str = ""
    price: Optional[float] = None
    currency: Optional[str] = None
    images: list[str] = field(default_factory=list)
    location: Optional[str] = None


@dataclass
class IdentifiedCard:
    """One distinct card seen in the ad's photos.

    `language`, `set_code`, `number` and `variant` use the same vocabulary as
    tcg_inventory's masterdata key (see apps/tcg_inventory/masterdata.py), so
    `master_key` / `dex_id` can be looked up there directly.
    """

    name: str
    set_name: Optional[str] = None
    set_code: Optional[str] = None
    number: Optional[str] = None
    printed_number: Optional[str] = None
    language: Optional[str] = None
    variant: str = "unspecified"
    condition: str = "Unknown"
    graded: Optional[str] = None
    quantity: int = 1
    confidence: str = "low"
    photos: list[int] = field(default_factory=list)
    notes: str = ""
    dex_id: Optional[str] = None

    @property
    def master_key(self) -> Optional[tuple[str, str, str, str]]:
        """(language, set_code, number, variant) -- tcg_inventory's masterdata
        key -- or None when the photo didn't show enough to build it."""
        if not (self.language and self.set_code and self.number):
            return None
        return (self.language, self.set_code, self.number, self.variant)


@dataclass
class AdAnalysis:
    """The full result for one ad: the ad itself plus the cards in its photos."""

    ad: FinnAd
    cards: list[IdentifiedCard] = field(default_factory=list)
    model: Optional[str] = None

    @property
    def card_count(self) -> int:
        return sum(card.quantity for card in self.cards)
