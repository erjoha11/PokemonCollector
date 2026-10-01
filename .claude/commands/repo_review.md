---
description: Professional read-only review of the repo (or one app/area) - where the code falls short and what to fix first
argument-hint: [scope, e.g. "tcg_inventory" or "security" - defaults to the whole repo]
---

Spawn the `reviewer` agent for a professional, read-only review. Scope: $ARGUMENTS (if empty, review the whole repo — both apps plus repo-level hygiene).

It must not edit files, commit, or write anything to GitHub. Relay its report back in full once it's done. Afterwards, offer to save it under `notes/` (e.g. `notes/REVIEW.md`, never the repo root) or to turn chosen findings into tracked work via `/new_fix`. Don't do either unless the user asks.
