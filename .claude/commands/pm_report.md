---
description: Get a status overview from project-manager - latest work, active issues, branch/repo sync status
---

Spawn the `project-manager` agent for a status overview. It should re-verify against current `gh`/`git` state (not recall a prior report) and cover:

1. **Latest work** — recently merged/closed PRs and issues, what shipped.
2. **Active issues** — open issues and in-flight PRs, their CI status, what's blocked or stale.
3. **Branch/repo sync status** — open branches vs. `main` (ahead/behind), anything unmerged or abandoned, worktree branches (`worktree-*`) that haven't been cleaned up.

Relay its report back verbatim/summarized once it's done.
