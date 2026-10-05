"""Import the latest Dex export from Dropbox into tcg_inventory (runs /cron/dropbox-sync).

Usage: python tools/import_from_dropbox.py            # Dropbox import (default)
       python tools/import_from_dropbox.py price-refresh|set-sync|image-backfill
CRON_SECRET: environment, then apps/tcg_inventory/.env / .env, then the Bitwarden
CLI (`bw get password <BW_CRON_ITEM>`, default item name "CRON_SECRET"; needs an
unlocked vault, i.e. BW_SESSION set). TCG_APP_URL: environment or .env.
Prints only the HTTP status and response body, never the secret.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

JOBS = {"dropbox-sync", "price-refresh", "set-sync", "image-backfill"}
ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_env():
    env = {}
    for p in (ROOT / ".env", ROOT / "apps" / "tcg_inventory" / ".env"):
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items()
                if k in ("CRON_SECRET", "TCG_APP_URL", "BW_CRON_ITEM")})
    return env


def secret_from_bitwarden(item):
    bw = shutil.which("bw")
    if not bw:
        return None
    r = subprocess.run([bw, "get", "password", item], capture_output=True, text=True)
    if r.returncode != 0:
        msg = (r.stderr or "").strip().splitlines()
        sys.exit(f"Bitwarden lookup for '{item}' failed: {msg[-1] if msg else 'unknown error'}"
                 " (run `bw unlock` and set BW_SESSION)")
    return r.stdout.strip() or None


def main():
    job = sys.argv[1] if len(sys.argv) > 1 else "dropbox-sync"
    if len(sys.argv) > 2 or job not in JOBS:
        sys.exit(f"usage: import_from_dropbox.py [{'|'.join(sorted(JOBS))}]")

    env = load_env()
    base = env.get("TCG_APP_URL", "").rstrip("/")
    if not base.startswith("https://"):
        sys.exit("TCG_APP_URL must be set to an https:// URL (env or .env)")
    secret = env.get("CRON_SECRET") or secret_from_bitwarden(env.get("BW_CRON_ITEM", "CRON_SECRET"))
    if not secret:
        sys.exit("CRON_SECRET not found in env, .env, or Bitwarden")

    req = urllib.request.Request(f"{base}/cron/{job}",
                                 headers={"Authorization": f"Bearer {secret}"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            status, body = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        sys.exit(f"Could not reach {base}: {e.reason}")

    print(f"HTTP {status}")
    try:
        print(json.dumps(json.loads(body), indent=2, ensure_ascii=False))
    except ValueError:
        print(body[:2000])


if __name__ == "__main__":
    main()