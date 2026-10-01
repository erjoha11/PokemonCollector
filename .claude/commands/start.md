---
description: Introduction to the repo - apps, agents, slash commands, and where to start
---

Give the user an onboarding introduction to this repo. Do not spawn any agent — read the files yourself so the intro always reflects what's actually checked in, not a remembered list:

- Root `CLAUDE.md` (repo structure, architecture notes, agent/handoff conventions)
- Every `.claude/agents/*.md` (frontmatter `name`, `description`, `tools`)
- Every `.claude/commands/*.md` (filename = command name, frontmatter `description`)
- The first section of each `apps/*/README.md`
- `git log --oneline -5` for a sense of recent activity

Then present, short and scannable (tables/bullets, no long prose), in the language the user writes in:

1. **What this repo is** — one or two sentences, then a table of the apps under `apps/` (name, what it does, how to run it).
2. **Getting started** — the setup and test commands from `CLAUDE.md`.
3. **Agents** — table: agent, role, what it can/can't do (e.g. writes code or not, can merge or not, who it can spawn). Follow with a simple flow of how they hand off to each other, e.g. `/new_feature → architect (→ ux) → issue → project-manager → developer → PR`.
4. **Slash commands** — table: command, what it does, which agent it spawns. Include `/start` itself.
5. **Which to use when** — a short "I want to… → use…" list (new idea, bug/small change, status overview, quick untracked fix, UX review, architecture question, git housekeeping).
6. **Things to know before touching code** — the handful of rules from `CLAUDE.md` that bite if missed (e.g. bump `CURRENT_SCHEMA_VERSION` when adding a column, computed-not-stored values, keep `card_ids.py` in sync with `masterdata.py`, read `notes/<app>/HANDOFF.md` before trusting the DB matches the code, log `ux` findings to `notes/tcg_inventory/UX_NOTES.md`, agent-written files go in `notes/`).
7. **Recent activity** — the last few commits in one line each.

$ARGUMENTS

If arguments were given above, treat them as a focus area (e.g. an app name or "agents") and go deeper on that part while keeping the rest brief.
