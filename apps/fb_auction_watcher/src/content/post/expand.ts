import { isClickableExpanderLabel } from "./patterns";

// Expands a post's comment thread by clicking "View more comments" / "View N replies" /
// "See more" until none are left. These are the only clicks this module makes: see isSafeToClick.
// A post opened in a dialog loads further comments on scroll instead of a button, so when no
// expander is left it scrolls the last comment into view (a scroll, never a click) and waits.
// While Facebook still shows its "Laster inn…" placeholders below the last comment, more
// comments are on their way: it scrolls those into view too and waits longer before giving up.

export type ExpandProgress = { clicks: number; scrolls: number; lastLabel: string };

export type ExpandOptions = {
  signal?: AbortSignal;
  onProgress?: (progress: ExpandProgress) => void;
  maxClicks?: number;
  /** Cap on scroll-to-load attempts. */
  maxScrolls?: number;
  /** Pause between clicks; jittered so the pace isn't mechanical. */
  minDelayMs?: number;
  maxDelayMs?: number;
  /** Rounds with no expander found before giving up (content can load late). */
  idleRounds?: number;
  /** Idle rounds allowed while Facebook still shows a loading placeholder below the comments. */
  loadingRounds?: number;
};

export type ExpandResult = {
  clicks: number;
  /** Scrolls that loaded more comments. */
  scrolls: number;
  /** "still-loading": gave up while Facebook still showed comments loading, so some are missing. */
  stoppedBecause: "done" | "aborted" | "max-clicks" | "still-loading";
};

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
const jitter = (min: number, max: number) => min + Math.random() * Math.max(0, max - min);

function label(el: Element): string {
  return el.textContent ?? "";
}

/**
 * Last check before any click. The label must be an allowed expander, and the control
 * must not sit anywhere that can write: a form, a text box, or an editable area.
 */
export function isSafeToClick(el: Element): boolean {
  if (!el.isConnected) return false;
  if (!isClickableExpanderLabel(label(el))) return false;
  if (el.closest("form, [contenteditable='true'], [role='textbox'], textarea, input")) return false;
  if (el.querySelector("[contenteditable='true'], [role='textbox'], textarea, input")) return false;
  const tag = el.tagName.toLowerCase();
  if (tag === "a" && el.getAttribute("href") && !el.getAttribute("href")!.startsWith("#")) return false;
  // Nor anything inside a real link: clicking it would follow the link (review L14).
  if (el.parentElement?.closest("a[href]:not([href^='#'])")) return false;
  return true;
}

export function findExpanders(root: Element): Element[] {
  return Array.from(root.querySelectorAll("[role='button'], button")).filter(isSafeToClick);
}

const ARTICLE = "[role='article']";

// Facebook's grey "Laster inn…" glimmer where the next comments will appear. Matched by role
// and a data attribute, never by class or by its (translated) label.
const LOADING = "[role='status'][data-visualcompletion='loading-state']";

/**
 * Loading placeholders after the last loaded comment: more comments are still coming. A real
 * read (2026-10-07, a claim sale with 121 comments) stopped at the first 10 with three of these
 * left below them, and every lot comment missing.
 */
export function pendingLoaders(root: Element): Element[] {
  const articles = root.querySelectorAll(ARTICLE);
  const last = articles[articles.length - 1];
  return Array.from(root.querySelectorAll(LOADING)).filter(
    (el) => !last || (!last.contains(el) && !!(last.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)),
  );
}

/**
 * Scrolls the last loaded comment into view so Facebook loads the next batch, then the loading
 * placeholder below it, if any: Facebook loads the next batch when that comes into view, and
 * the last comment's bottom edge alone may stop short of it.
 */
function scrollToLastComment(root: Element): void {
  const articles = root.querySelectorAll(ARTICLE);
  articles[articles.length - 1]?.scrollIntoView?.({ block: "end" });
  pendingLoaders(root)[0]?.scrollIntoView?.({ block: "end" });
}

export async function expandAll(root: Element, options: ExpandOptions = {}): Promise<ExpandResult> {
  const {
    signal,
    onProgress,
    maxClicks = 400,
    maxScrolls = 200,
    minDelayMs = 700,
    maxDelayMs = 1600,
    idleRounds = 4,
    loadingRounds = 12,
  } = options;
  const clicked = new WeakSet<Element>();
  let clicks = 0;
  let scrolls = 0;
  let scrollAttempts = 0;
  let idle = 0;

  while (clicks < maxClicks) {
    if (signal?.aborted) return { clicks, scrolls, stoppedBecause: "aborted" };
    const next = findExpanders(root).find((el) => !clicked.has(el));
    if (!next) {
      const before = root.querySelectorAll(ARTICLE).length;
      if (scrollAttempts < maxScrolls) {
        scrollToLastComment(root);
        scrollAttempts++;
      }
      await sleep(jitter(minDelayMs, maxDelayMs) * 1.5);
      if (root.querySelectorAll(ARTICLE).length > before) {
        idle = 0;
        scrolls++;
        onProgress?.({ clicks, scrolls, lastLabel: "scrolled for more comments" });
        continue;
      }
      ++idle;
      if (pendingLoaders(root).length > 0) {
        // Still loading: wait longer, but not forever (a placeholder that never resolves).
        if (idle >= Math.max(idleRounds, loadingRounds)) return { clicks, scrolls, stoppedBecause: "still-loading" };
        continue;
      }
      if (idle >= idleRounds) return { clicks, scrolls, stoppedBecause: "done" };
      continue;
    }
    idle = 0;
    clicked.add(next);
    next.scrollIntoView?.({ block: "center" });
    await sleep(jitter(minDelayMs / 2, maxDelayMs / 2));
    // Re-check right before clicking: Facebook re-renders, and a node can change label.
    if (signal?.aborted) return { clicks, scrolls, stoppedBecause: "aborted" };
    if (!isSafeToClick(next)) continue;
    const text = label(next).trim();
    (next as HTMLElement).click();
    clicks++;
    onProgress?.({ clicks, scrolls, lastLabel: text });
    await sleep(jitter(minDelayMs, maxDelayMs));
  }
  return { clicks, scrolls, stoppedBecause: "max-clicks" };
}
