"""Command line: identify the Pokemon cards in a finn.no ad.

    python -m finn_ad_scraper.cli "https://www.finn.no/recommerce/forsale/item/123456789"
    python -m finn_ad_scraper.cli URL --json
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from .card_identifier import DEFAULT_MODEL
from .models import AdAnalysis
from .pipeline import analyze_ad


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Identify the Pokemon cards in a finn.no ad.")
    parser.add_argument("url", nargs="?", help="finn.no ad URL (asked for when omitted)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a summary")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude model (default {DEFAULT_MODEL})")
    parser.add_argument("--no-browser", action="store_true", help="never fall back to headless Chromium")
    parser.add_argument(
        "--inline-images", action="store_true",
        help="download the photos and send them base64-encoded instead of by URL",
    )
    args = parser.parse_args(argv)

    url = args.url or input("finn.no ad URL: ").strip()
    if not url:
        print("A finn.no ad URL is required.", file=sys.stderr)
        return 1

    try:
        result = analyze_ad(
            url, model=args.model, use_browser=not args.no_browser, inline_images=args.inline_images
        )
    except Exception as exc:  # one clean line instead of a traceback
        print(f"Could not analyze {url}: {exc}", file=sys.stderr)
        return 1

    print(to_json(result) if args.json else summary(result))
    return 0


def to_json(result: AdAnalysis) -> str:
    payload = {
        "ad": dataclasses.asdict(result.ad),
        "model": result.model,
        "cards": [dict(dataclasses.asdict(card), master_key=card.master_key) for card in result.cards],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def summary(result: AdAnalysis) -> str:
    ad = result.ad
    lines = [ad.title, f"  {ad.url}"]
    if ad.price is not None:
        lines.append(f"  Asking price: {ad.price:,.0f} {ad.currency or ''}".replace(",", " ").rstrip())
    lines.append(f"  Photos: {len(ad.images)}   Cards: {result.card_count} ({len(result.cards)} distinct)")
    if ad.price and result.card_count:
        lines.append(f"  Price per card: {ad.price / result.card_count:,.0f} {ad.currency or ''}".replace(",", " ").rstrip())
    for card in result.cards:
        where = ", ".join(filter(None, [card.set_name, card.printed_number and f"#{card.printed_number}"])) or "set unknown"
        extras = [card.variant if card.variant != "unspecified" else None, card.language, card.graded or card.condition]
        ident = f"  [{card.dex_id}]" if card.dex_id else ""
        lines.append(
            f"    {card.quantity}x {card.name} ({where}) - {', '.join(x for x in extras if x)}"
            f" - {card.confidence} confidence{ident}"
        )
        if card.notes:
            lines.append(f"       {card.notes}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
