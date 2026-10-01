"""Guard: a model/table change must come with a CURRENT_SCHEMA_VERSION bump.

init_db() skips its whole migration chain when the database's stored
schema_meta version already equals db.CURRENT_SCHEMA_VERSION (the serverless
cold-start fast path). So a new column added to a model *without* bumping the
version never gets created on an already-migrated production database --
#181 shipped exactly that and needed a manual prod fix (HANDOFF 2026-09-23).

This test fingerprints every table/column on Base.metadata (name, type,
nullable) and compares it with the committed snapshot in
schema_snapshot.json:

- fingerprint changed, version not bumped -> fail: bump the version (and
  make sure init_db()'s chain creates the change), then regenerate.
- version bumped, snapshot not regenerated -> fail: regenerate.

Regenerate the snapshot (from the repo root) with:

    python apps/tcg_inventory/tests/test_schema_guard.py --update
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = Path(__file__).resolve().parent / "schema_snapshot.json"
REGENERATE_CMD = "python apps/tcg_inventory/tests/test_schema_guard.py --update"

if str(APP_DIR) not in sys.path:  # conftest.py does this under pytest; not when run as a script
    sys.path.insert(0, str(APP_DIR))

import db as db_module  # noqa: E402
import models  # noqa: E402,F401  (registers tables on Base.metadata)


def current_tables() -> dict[str, list[list]]:
    """{table: [[column, type, nullable], ...]}, sorted, from Base.metadata."""
    tables = {}
    for table in db_module.Base.metadata.sorted_tables:
        tables[table.name] = sorted([col.name, str(col.type), bool(col.nullable)] for col in table.columns)
    return dict(sorted(tables.items()))


def fingerprint(tables: dict[str, list[list]]) -> str:
    return hashlib.sha256(json.dumps(tables, sort_keys=True).encode()).hexdigest()


def build_snapshot() -> dict:
    tables = current_tables()
    return {
        "schema_version": db_module.CURRENT_SCHEMA_VERSION,
        "fingerprint": fingerprint(tables),
        "tables": tables,
    }


def _describe_diff(old: dict[str, list[list]], new: dict[str, list[list]]) -> str:
    lines = []
    for name in sorted(set(old) | set(new)):
        if name not in old:
            lines.append(f"  + table {name}")
            continue
        if name not in new:
            lines.append(f"  - table {name}")
            continue
        old_cols = {c[0]: c for c in old[name]}
        new_cols = {c[0]: c for c in new[name]}
        for col in sorted(set(old_cols) | set(new_cols)):
            if col not in old_cols:
                lines.append(f"  + {name}.{col} {new_cols[col][1]} nullable={new_cols[col][2]}")
            elif col not in new_cols:
                lines.append(f"  - {name}.{col}")
            elif old_cols[col] != new_cols[col]:
                lines.append(f"  ~ {name}.{col}: {old_cols[col][1:]} -> {new_cols[col][1:]}")
    return "\n".join(lines) or "  (no column-level difference)"


def test_schema_change_bumps_schema_version():
    assert SNAPSHOT_PATH.exists(), f"Missing {SNAPSHOT_PATH.name}. Create it with:\n    {REGENERATE_CMD}"
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    current = build_snapshot()
    version = current["schema_version"]
    changed = current["fingerprint"] != snapshot["fingerprint"]
    diff = _describe_diff(snapshot["tables"], current["tables"]) if changed else ""

    if changed and version == snapshot["schema_version"]:
        raise AssertionError(
            "The model schema changed but db.CURRENT_SCHEMA_VERSION is still "
            f"{version}.\n{diff}\n\n"
            "init_db() skips its migration chain when the stored version already "
            "matches, so this change would never reach an already-migrated "
            "production database (see #181 / HANDOFF 2026-09-23). Fix:\n"
            f"  1. Bump CURRENT_SCHEMA_VERSION in apps/tcg_inventory/db.py to {version + 1} "
            "(and note what changed in its comment).\n"
            "  2. Make sure init_db()'s chain creates the change (new columns are "
            "added by _add_missing_columns(); anything else needs a migration step).\n"
            f"  3. Regenerate the snapshot:\n       {REGENERATE_CMD}"
        )
    if version != snapshot["schema_version"]:
        raise AssertionError(
            f"db.CURRENT_SCHEMA_VERSION is {version} but {SNAPSHOT_PATH.name} was "
            f"recorded at version {snapshot['schema_version']}"
            + (f", and the schema changed:\n{diff}\n" if changed else " (schema unchanged).\n")
            + f"Regenerate the snapshot and commit it:\n    {REGENERATE_CMD}"
        )


if __name__ == "__main__":
    if "--update" not in sys.argv[1:]:
        sys.exit(f"usage: {REGENERATE_CMD}")
    snap = build_snapshot()
    # One column per line so a schema change reads as a small diff in review.
    table_blocks = ",\n".join(
        f"    {json.dumps(name)}: [\n" + ",\n".join(f"      {json.dumps(col)}" for col in cols) + "\n    ]"
        for name, cols in snap["tables"].items()
    )
    SNAPSHOT_PATH.write_text(
        "{\n"
        f'  "schema_version": {snap["schema_version"]},\n'
        f'  "fingerprint": {json.dumps(snap["fingerprint"])},\n'
        '  "tables": {\n' + table_blocks + "\n  }\n}\n",
        encoding="utf-8",
    )
    print(f"Wrote {SNAPSHOT_PATH} (schema_version={snap['schema_version']}, {len(snap['tables'])} tables)")
