# finn_ad_scraper

Opens a finn.no ad, reads its title, description, asking price and photos,
and identifies the Pokemon cards in the photos with Claude (vision). Each
card comes back with the same identity tcg_inventory uses for its
masterdata: language, set code, number and variant, plus the Dex card ID
built from them.

## Usage

```bash
pip install -r requirements.txt
playwright install chromium      # optional: only for the headless-browser fallback
export ANTHROPIC_API_KEY=sk-...  # or `ant auth login`

python -m finn_ad_scraper.cli "https://www.finn.no/recommerce/forsale/item/123456789"
python -m finn_ad_scraper.cli URL --json            # machine-readable
python -m finn_ad_scraper.cli URL --no-browser      # never start Chromium
python -m finn_ad_scraper.cli URL --inline-images   # send photos base64 instead of by URL
python -m finn_ad_scraper.cli URL --model claude-sonnet-5
```

Example summary:

```
Pokemon kort samling - Charizard m.fl.
  https://www.finn.no/recommerce/forsale/item/123456789
  Asking price: 1 500 NOK
  Photos: 4   Cards: 3 (2 distinct)
  Price per card: 500 NOK
    2x Charizard (Base Set, #4/102) - holo, int, Lightly Played - high confidence  [base1-4]
    1x Mew ex (Pokémon Card 151, #205/165) - holo, ja, PSA 10 - medium confidence  [jpn_sv2a-205]
```

From Python:

```python
from finn_ad_scraper import analyze_ad

result = analyze_ad("https://www.finn.no/recommerce/forsale/item/123456789")
for card in result.cards:
    print(card.name, card.dex_id, card.master_key)
```

## How it works

| Module | Job |
|---|---|
| `finn_ad.py` | `fetch_html` gets the page with a plain GET (finn.no embeds a JSON-LD `Product` block server-side) and renders it in headless Chromium only if that response has no structured data. `parse_ad` reads JSON-LD first (handles `@graph`, ImageObjects, `"1 250,50"` prices) and fills gaps from Open Graph/meta tags. |
| `card_identifier.py` | `identify_cards` sends all photos in one request (labelled "Photo 1..N", up to 20 per request, more are batched and merged) with the ad text as context. |
| `card_ids.py` | The identity rules shared with `tcg_inventory/masterdata.py`: variant codes, number normalization (`004/165` → `4`), Dex IDs (`jpn_sv2a-168`). Copied, not imported: apps don't import each other. Keep them in sync. |
| `models.py` | `FinnAd`, `IdentifiedCard`, `AdAnalysis`, and the `CONDITIONS` list (a literal copy of `tcg_inventory/constants.py`'s `CARD_CONDITIONS`). |
| `pipeline.py` | `analyze_ad(url)`: fetch, then identify. |
| `cli.py` | The command line. |

### The Claude call

- **Model:** `claude-opus-5` by default (`--model` to change).
- **Structured outputs** (`output_config.format` with a JSON schema): the reply is always valid JSON matching `CARD_SCHEMA`. Enums pin `language`, `variant`, `condition` and `confidence` to the shared vocabularies.
- **Photos by URL:** Anthropic fetches them from finn's CDN directly. If a request fails because an image URL couldn't be fetched, it is retried once with the photos downloaded and sent base64-encoded.
- **Refusal fallback:** `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`). If the model declines, the API re-runs the request on Anthropic's recommended fallback model in the same call. A refusal that survives that raises `CardIdentificationError`, as does a reply cut off by `max_tokens`.
- **No prompt caching:** each ad is one request, and the system prompt is below the minimum cacheable size, so there is nothing to reuse.

## Testing

`python -m pytest apps/finn_ad_scraper` from the repo root. Tests run offline against fixture HTML and a fake Claude client: no network, API key or Playwright install needed.
