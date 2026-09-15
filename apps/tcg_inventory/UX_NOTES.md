# UX notes

A running log of usability/design findings from the `ux` agent (see
`.claude/agents/ux.md`), so its analysis survives past the chat session it
came from. The `ux` agent itself never writes here — it's read-only by
design — whichever session consulted it appends the entry afterward.

Each entry: date, what was reviewed, what was found, and its status. Mark an
entry `Addressed` (with a short note on the fix, or a PR/commit reference)
once it's dealt with, rather than deleting it — a resolved entry is a record
that the issue was seen and handled, not just silence.

---

<!-- Example entry shape:

## 2026-09-16 — Transactions purchase-cart flow

**Reviewed:** templates/transactions.html, partials/purchase_cart*.html

**Findings:**
- ...

**Status:** Open
-->
