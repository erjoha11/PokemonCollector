"""Command-line entry point: open a finn.no ad and identify its cards.

Usage:
    python -m finn_ad_scraper.cli https://www.finn.no/recommerce/forsale/item/123456789
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from .card_identifier import identify_cards
from .finn_ad import fetch_finn_ad


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Identify Pokemon cards in a finn.no ad.")
    parser.add_argument("url", help="finn.no ad URL")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON instead of a summary")
    args = parser.parse_args(argv)

    try:
        ad = fetch_finn_ad(args.url)
        cards = identify_cards(ad.images, ad_context=f"{ad.title}\n\n{ad.description}")
    except Exception as exc:  # surface a clean error instead of a traceback
        print(f"Error processing {args.url}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {"ad": dataclasses.asdict(ad), "cards": [dataclasses.asdict(c) for c in cards]}
        payload["ad"].pop("raw_html", None)
        print(json.dumps(payload, indent=2, default=str))
        return 0

    print(f"{ad.title}")
    print(f"  Ad URL: {ad.url}")
    if ad.price is not None:
        print(f"  Asking price: {ad.price:.0f} {ad.currency or ''}".rstrip())
    print(f"  Photos: {len(ad.images)}")
    print(f"  Cards identified: {len(cards)}")
    for card in cards:
        holo = " (holo)" if card.is_holo else ""
        number = f" #{card.card_number}" if card.card_number else ""
        print(
            f"    - {card.quantity}x {card.name}{number}{holo} "
            f"[{card.set_name or 'unknown set'}, {card.condition}] confidence={card.confidence}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
