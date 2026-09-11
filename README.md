# PokemonCollector

A collection of small apps for buying and collecting Pokemon cards. Each
app lives in its own folder under `apps/`, independently runnable and
testable.

## Apps

| Folder | Status | What it does |
|---|---|---|
| [`apps/finn_ad_scraper/`](apps/finn_ad_scraper) | In progress | Opens a finn.no ad, extracts its title/description/price/photos, and identifies the Pokemon cards visible in the photos (via Claude vision). |
| [`apps/tcg_inventory/`](apps/tcg_inventory) | In progress | FastAPI webapp that replaces an Excel workbook for tracking a physical Pokémon card collection: dashboard, inventory table, transaction log, Dex CSV import/sync (manual or from Dropbox). Runs locally (`python app.py`, SQLite) or deployed (Vercel + Supabase, with login). |

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
playwright install chromium   # only needed for finn_ad_scraper's headless-browser fallback
cp .env.example .env          # then fill in ANTHROPIC_API_KEY
```

## Testing

```bash
python -m pytest
```

Runs every app's test suite. All tests run entirely offline against fixture
data and fake API clients — no network access, API keys, or Playwright
browser install required.
