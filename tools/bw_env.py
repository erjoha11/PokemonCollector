"""Keep this repo's .env files in a Bitwarden vault instead of only on disk.

Each .env file is one Secure Note in the "PokemonCollector" folder, with one
hidden custom field per variable. Values pass straight between the vault and
the file -- this script never prints them, and only ever reports key names.

    python tools/bw_env.py status [target]   # which keys are in the vault vs on disk
    python tools/bw_env.py push   [target]   # local .env -> vault (creates the item)
    python tools/bw_env.py pull   [target]   # vault -> local .env (from .env.example)

target is one of the keys in TARGETS below; omit it to act on all of them.
Needs the Bitwarden CLI (`bw`), logged in and unlocked -- see README.md
"Secrets in Bitwarden".
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FOLDER_NAME = "PokemonCollector"

# target -> (vault item name, .env path, template path), paths relative to the repo root.
TARGETS = {
    "tcg_inventory": (
        "PokemonCollector - tcg_inventory .env",
        "apps/tcg_inventory/.env",
        "apps/tcg_inventory/.env.example",
    ),
    "root": ("PokemonCollector - root .env", ".env", ".env.example"),
}

HIDDEN_FIELD = 1  # Bitwarden custom field type: 0 text, 1 hidden, 2 boolean
SECURE_NOTE = 2  # Bitwarden item type


class BwError(RuntimeError):
    pass


# --- .env parsing/rendering (pure, no bw) ------------------------------------


def parse_env(text: str) -> dict[str, str]:
    """KEY=VALUE pairs in file order; comments, blanks and malformed lines skipped."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key:
            values[key] = value
    return values


def render_env(template: str, values: dict[str, str]) -> str:
    """Fill the template's KEY= lines from values, keeping its comments and order.

    Keys in the template but not in values keep the template's default; keys
    in values but not in the template are appended at the end.
    """
    out: list[str] = []
    seen: set[str] = set()
    for line in template.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in values:
                out.append(f"{key}={values[key]}")
                seen.add(key)
                continue
        out.append(line)
    extra = [k for k in values if k not in seen]
    if extra:
        out += ["", "# Not in the template, kept from the vault:"]
        out += [f"{k}={values[k]}" for k in extra]
    return "\n".join(out) + "\n"


# --- bw CLI wrapper -----------------------------------------------------------


def bw(*args: str, stdin: str | None = None) -> str:
    exe = shutil.which("bw")
    if not exe:
        raise BwError("Bitwarden CLI not found. Install it: winget install Bitwarden.CLI")
    proc = subprocess.run(
        [exe, *args], input=stdin, capture_output=True, text=True, encoding="utf-8"
    )
    if proc.returncode != 0:
        raise BwError(f"bw {args[0]} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def encode(obj: dict) -> str:
    # Same as `bw encode`; fed over stdin so values never land on a command line.
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")


def require_unlocked() -> None:
    status = json.loads(bw("status")).get("status")
    if status == "unlocked":
        return
    if status == "unauthenticated":
        raise BwError("Not logged in. Run: bw login")
    raise BwError(
        "Vault is locked. Unlock it for this shell first:\n"
        '  PowerShell: $env:BW_SESSION = (bw unlock --raw)\n'
        '  bash:       export BW_SESSION="$(bw unlock --raw)"'
    )


def folder_id(create: bool) -> str | None:
    folders = json.loads(bw("list", "folders", "--search", FOLDER_NAME))
    for f in folders:
        if f["name"] == FOLDER_NAME:
            return f["id"]
    if not create:
        return None
    return json.loads(bw("create", "folder", stdin=encode({"name": FOLDER_NAME})))["id"]


def find_item(name: str) -> dict | None:
    matches = [i for i in json.loads(bw("list", "items", "--search", name)) if i["name"] == name]
    if len(matches) > 1:
        raise BwError(f'{len(matches)} vault items are named "{name}"; delete the duplicates.')
    return matches[0] if matches else None


def item_values(item: dict) -> dict[str, str]:
    return {f["name"]: f.get("value") or "" for f in item.get("fields") or []}


# --- commands -----------------------------------------------------------------


def cmd_status(target: str) -> None:
    item_name, env_rel, _ = TARGETS[target]
    env_path = REPO_ROOT / env_rel
    local = parse_env(env_path.read_text(encoding="utf-8")) if env_path.exists() else {}
    item = find_item(item_name)
    vault = item_values(item) if item else {}
    print(f"[{target}] vault item: {'found' if item else 'missing'}; {env_rel}: "
          f"{'found' if env_path.exists() else 'missing'}")
    for key in sorted(set(local) | set(vault)):
        if key not in vault:
            state = "local only"
        elif key not in local:
            state = "vault only"
        else:
            state = "same" if local[key] == vault[key] else "DIFFERS"
        print(f"  {key:<28} {state}")


def cmd_push(target: str) -> None:
    item_name, env_rel, _ = TARGETS[target]
    env_path = REPO_ROOT / env_rel
    if not env_path.exists():
        print(f"[{target}] {env_rel} doesn't exist, nothing to push.")
        return
    local = {k: v for k, v in parse_env(env_path.read_text(encoding="utf-8")).items() if v}
    fields = [{"name": k, "value": v, "type": HIDDEN_FIELD} for k, v in local.items()]
    item = find_item(item_name)
    if item is None:
        new_item = {
            "type": SECURE_NOTE,
            "secureNote": {"type": 0},
            "name": item_name,
            "notes": f"Synced from {env_rel} by tools/bw_env.py. One hidden field per variable.",
            "folderId": folder_id(create=True),
            "fields": fields,
        }
        bw("create", "item", stdin=encode(new_item))
        print(f"[{target}] created vault item with {len(fields)} keys: {', '.join(local)}")
        return
    before = item_values(item)
    item["fields"] = fields
    bw("edit", "item", item["id"], stdin=encode(item))
    changed = [k for k in local if before.get(k) != local[k]]
    removed = [k for k in before if k not in local]
    print(f"[{target}] updated vault item. changed/added: {', '.join(changed) or 'none'}; "
          f"removed: {', '.join(removed) or 'none'}")


def cmd_pull(target: str, force: bool) -> None:
    item_name, env_rel, template_rel = TARGETS[target]
    item = find_item(item_name)
    if item is None:
        print(f"[{target}] no vault item \"{item_name}\" yet; run push first.")
        return
    template_path = REPO_ROOT / template_rel
    template = template_path.read_text(encoding="utf-8") if template_path.exists() else ""
    content = render_env(template, item_values(item))
    env_path = REPO_ROOT / env_rel
    if env_path.exists() and not force:
        if env_path.read_text(encoding="utf-8") == content:
            print(f"[{target}] {env_rel} already matches the vault.")
            return
        raise BwError(f"{env_rel} exists and differs from the vault. Run status to see "
                      "which keys, push if local is newer, or pull --force to overwrite it.")
    env_path.write_text(content, encoding="utf-8")
    print(f"[{target}] wrote {env_rel} ({len(item_values(item))} keys from the vault)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["status", "push", "pull"])
    parser.add_argument("target", nargs="?", choices=sorted(TARGETS))
    parser.add_argument("--force", action="store_true", help="pull: overwrite a differing .env")
    args = parser.parse_args(argv)
    targets = [args.target] if args.target else list(TARGETS)
    try:
        require_unlocked()
        bw("sync")
        for target in targets:
            if args.command == "status":
                cmd_status(target)
            elif args.command == "push":
                cmd_push(target)
            else:
                cmd_pull(target, args.force)
    except BwError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
