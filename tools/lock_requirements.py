"""Regenerate the pinned requirements lockfiles from their `.in` inputs.

    python tools/lock_requirements.py            # re-lock, keeping existing pins where still valid
    python tools/lock_requirements.py --upgrade  # re-lock, bumping everything to the newest allowed

The `.in` files hold the loose `>=` requirements a human edits; the `.txt`
files next to them are the exact pins that CI (`requirements-dev.txt`) and
Vercel (`apps/tcg_inventory/requirements.txt`) install. Never hand-edit a
`.txt` lockfile -- edit the `.in` and re-run this.

requirements-dev.txt is resolved first (both apps + pytest together); each
app's lock is then resolved with it as a constraint, so a package shared by
both apps is pinned to the same version everywhere and CI tests exactly what
Vercel ships.

Locks are "universal" (uv's platform markers), resolved for the Python
version in apps/tcg_inventory/.python-version, so the same file installs on
Linux (Vercel, CI), Windows and macOS. Needs uv (`pip install uv`).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON_VERSION_FILE = REPO_ROOT / "apps" / "tcg_inventory" / ".python-version"

# (input, output, constraint) relative to the repo root, in resolution order.
LOCKS = [
    ("requirements-dev.in", "requirements-dev.txt", None),
    ("apps/tcg_inventory/requirements.in", "apps/tcg_inventory/requirements.txt", "requirements-dev.txt"),
    ("apps/finn_ad_scraper/requirements.in", "apps/finn_ad_scraper/requirements.txt", "requirements-dev.txt"),
]


def main(argv: list[str]) -> int:
    uv = shutil.which("uv")
    if uv is None:
        print("uv not found -- install it with `pip install uv`.", file=sys.stderr)
        return 1
    python_version = PYTHON_VERSION_FILE.read_text().strip()
    upgrade = "--upgrade" in argv
    for src, out, constraint in LOCKS:
        cmd = [
            uv, "pip", "compile", src, "-o", out,
            "--universal", "--python-version", python_version,
            "--annotation-style", "line", "--quiet",
            "--custom-compile-command", "python tools/lock_requirements.py",
        ]
        if constraint:
            cmd += ["-c", constraint]
        elif upgrade:
            # Only the first (unconstrained) lock upgrades; the others follow it.
            cmd.append("--upgrade")
        print(f"locking {out}")
        subprocess.run(cmd, cwd=REPO_ROOT, check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
