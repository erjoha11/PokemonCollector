# PokemonCollector

A collection of small apps for buying and collecting Pokemon cards. Each
app lives in its own folder under `apps/`, independently runnable and
testable.

## Apps

| Folder | Status | What it does |
|---|---|---|
| [`apps/finn_ad_scraper/`](apps/finn_ad_scraper) | In progress | Opens a finn.no ad, extracts its title/description/price/photos, and identifies the Pokemon cards visible in the photos (via Claude vision). |
| [`apps/tcg_inventory/`](apps/tcg_inventory) | In progress | FastAPI webapp that replaces an Excel workbook for tracking a physical Pokémon card collection: dashboard, inventory table, transaction log, Dex CSV import/sync (manual or from Dropbox). Runs locally (`python app.py`, SQLite) or deployed (Vercel + Supabase, with login). |

Notes and reports written by Claude agents (changelog, handoff log, UX
notes, reviews) live in [`notes/`](notes).

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
playwright install chromium   # only needed for finn_ad_scraper's headless-browser fallback
cp .env.example .env          # then fill in ANTHROPIC_API_KEY
```

### Secrets in Bitwarden

The `.env` files (repo root and `apps/tcg_inventory/`) are gitignored, so
the vault is their backup and the way to recreate them on a new machine.
`tools/bw_env.py` stores each one as a Secure Note in a `PokemonCollector`
folder in your Bitwarden vault, with one hidden field per variable. It only
ever prints key names, never values.

```bash
winget install Bitwarden.CLI                 # once (or: npm i -g @bitwarden/cli)
bw login                                     # once per machine
export BW_SESSION="$(bw unlock --raw)"       # per shell; PowerShell: $env:BW_SESSION = (bw unlock --raw)

python tools/bw_env.py status                # which keys are in the vault vs on disk
python tools/bw_env.py push                  # local .env files -> vault
python tools/bw_env.py pull                  # vault -> .env files (refuses to overwrite a differing one without --force)
```

Add a target (`tcg_inventory` or `root`) to act on just one file. Secrets that
aren't in any `.env` file belong in the vault as their own items, by hand:
the GitHub Actions secrets for `prod-backup.yml` (`BACKUP_*`), and above all
the **age private key** for those backups. Without it, the encrypted dumps
can't be restored.

## Testing

```bash
python -m pytest
```

Runs every app's test suite. All tests run entirely offline against fixture
data and fake API clients — no network access, API keys, or Playwright
browser install required.
