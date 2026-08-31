# finn_ad_scraper

Opens a finn.no ad and identifies its content: the ad's title/description/
price/photos, and the Pokemon cards visible in those photos.

## How it works

1. **`fetch_finn_ad(url)`** (`finn_ad.py`) — opens the ad. It first tries a
   plain HTTP GET (finn.no ad pages embed a JSON-LD `Product` block with
   title/description/price/images that doesn't need JS). If that fails, it
   falls back to rendering the page with headless Chromium via Playwright.
2. **`identify_cards(images, ad_context)`** (`card_identifier.py`) — sends
   the ad's photos to Claude (vision) and asks it to identify each visible
   card's name, set, card number, holo status, and condition.

## Usage

```bash
pip install -r requirements.txt
playwright install chromium   # only needed for the headless-browser fallback
export ANTHROPIC_API_KEY=sk-...
python -m finn_ad_scraper.cli "https://www.finn.no/recommerce/forsale/item/123456789"
```

```python
from finn_ad_scraper import fetch_finn_ad, identify_cards

ad = fetch_finn_ad("https://www.finn.no/recommerce/forsale/item/123456789")
cards = identify_cards(ad.images, ad_context=f"{ad.title}\n{ad.description}")
```

## Testing

Run from the repo root: `python -m pytest apps/finn_ad_scraper`. Tests run
offline against fixture HTML and a fake Claude client — no network, API
keys, or Playwright browser install required.
