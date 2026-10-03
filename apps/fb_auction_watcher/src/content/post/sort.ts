import { isAllCommentsLabel, isAllCommentsMenuItemLabel, isCommentSortLabel } from "./patterns";

// Switches the post's comment sort to "All comments" / "Alle kommentarer", because the
// default "Most relevant" can hide comments (and so bids). Two clicks, both guarded:
// the sort control (opens its menu) and the "All comments" menu item. Nothing else.

export type SortAction = "already-all" | "switched" | "not-found" | "failed" | "aborted";

export type SortOptions = {
  signal?: AbortSignal;
  /** How long to wait for the menu to open. */
  menuTimeoutMs?: number;
  /** Pause after switching, while Facebook reloads the comments. */
  settleMs?: number;
};

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
const WRITABLE = "form, [contenteditable='true'], [role='textbox'], textarea, input";

export function findSortControl(root: Element): Element | null {
  return (
    Array.from(root.querySelectorAll("[role='button']")).find(
      (b) => isCommentSortLabel(b.textContent) && !b.closest(WRITABLE),
    ) ?? null
  );
}

/** The menu renders outside the post (a portal), so search the whole document. */
export function findAllCommentsMenuItem(doc: Document): Element | null {
  return (
    Array.from(doc.querySelectorAll("[role='menuitem'], [role='menuitemradio'], [role='option']")).find(
      (item) => isAllCommentsMenuItemLabel(item.textContent) && !item.closest(WRITABLE),
    ) ?? null
  );
}

async function waitFor<T>(find: () => T | null, timeoutMs: number, signal?: AbortSignal): Promise<T | null> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const found = find();
    if (found || signal?.aborted || Date.now() >= deadline) return found;
    await sleep(150);
  }
}

function closeMenu(doc: Document) {
  const target = doc.activeElement ?? doc.body;
  target.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true }));
}

export async function ensureAllComments(root: Element, options: SortOptions = {}): Promise<SortAction> {
  const { signal, menuTimeoutMs = 3000, settleMs = 2000 } = options;
  const doc = root.ownerDocument;
  const control = findSortControl(root);
  if (!control) return "not-found";
  if (isAllCommentsLabel(control.textContent)) return "already-all";
  if (signal?.aborted) return "aborted";

  (control as HTMLElement).click();
  const item = await waitFor(() => findAllCommentsMenuItem(doc), menuTimeoutMs, signal);
  if (signal?.aborted) {
    closeMenu(doc);
    return "aborted";
  }
  if (!item) {
    closeMenu(doc);
    return "failed";
  }
  (item as HTMLElement).click();
  await sleep(settleMs);
  return "switched";
}
