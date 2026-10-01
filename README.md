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

### Python version and dependencies

Use **Python 3.12** for the local venv. Vercel reads it from
`apps/tcg_inventory/.python-version`, and CI pins the same version in
`.github/workflows/tests.yml`, so all three run the same interpreter.
(Vercel's Python runtime supports 3.12, 3.13 and 3.14.)

Dependencies come in pairs:

- `*.in` files (`requirements-dev.in`, `apps/*/requirements.in`) hold the
  loose `>=` requirements. These are the ones you edit.
- `*.txt` files next to them are pinned lockfiles generated from the `.in`
  files. CI installs `requirements-dev.txt`, Vercel installs
  `apps/tcg_inventory/requirements.txt`. Never hand-edit them.

The locks are made with [uv](https://docs.astral.sh/uv/) (`pip install uv`)
in universal mode for the pinned Python version, so the same file installs
on Linux (Vercel, CI), Windows and macOS. A package used by both apps gets
the same pin in every lockfile.

To add or upgrade a dependency:

1. Edit the relevant `.in` file (add the package, or raise its `>=` floor).
2. Re-lock: `python tools/lock_requirements.py` (keeps existing pins where
   they still satisfy the inputs), or `python tools/lock_requirements.py
   --upgrade` to bump everything to the newest allowed versions.
3. `pip install -r requirements-dev.txt` and run `python -m pytest`.
4. Commit the `.in` and `.txt` changes together and open a PR. Merge once CI
   and the Vercel preview build are green.

To move to a new Python version, change `apps/tcg_inventory/.python-version`
(it must be one Vercel supports) and `python-version` in
`.github/workflows/tests.yml` together, re-lock, recreate your venv on that
version, and go through the same PR steps.

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
