import { isExpanderLabel } from "./patterns";

// Expands a post's comment thread by clicking "View more comments" / "View N replies"
// until none are left. These are the only clicks this module makes: see isSafeToClick.

export type ExpandProgress = { clicks: number; lastLabel: string };

export type ExpandOptions = {
  signal?: AbortSignal;
  onProgress?: (progress: ExpandProgress) => void;
  maxClicks?: number;
  /** Pause between clicks; jittered so the pace isn't mechanical. */
  minDelayMs?: number;
  maxDelayMs?: number;
  /** Rounds with no expander found before giving up (content can load late). */
  idleRounds?: number;
};

export type ExpandResult = {
  clicks: number;
  stoppedBecause: "done" | "aborted" | "max-clicks";
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
  if (!isExpanderLabel(label(el))) return false;
  if (el.closest("form, [contenteditable='true'], [role='textbox'], textarea, input")) return false;
  if (el.querySelector("[contenteditable='true'], [role='textbox'], textarea, input")) return false;
  const tag = el.tagName.toLowerCase();
  if (tag === "a" && el.getAttribute("href") && !el.getAttribute("href")!.startsWith("#")) return false;
  return true;
}

export function findExpanders(root: Element): Element[] {
  return Array.from(root.querySelectorAll("[role='button'], button")).filter(isSafeToClick);
}

export async function expandAll(root: Element, options: ExpandOptions = {}): Promise<ExpandResult> {
  const { signal, onProgress, maxClicks = 400, minDelayMs = 700, maxDelayMs = 1600, idleRounds = 3 } = options;
  const clicked = new WeakSet<Element>();
  let clicks = 0;
  let idle = 0;

  while (clicks < maxClicks) {
    if (signal?.aborted) return { clicks, stoppedBecause: "aborted" };
    const next = findExpanders(root).find((el) => !clicked.has(el));
    if (!next) {
      if (++idle >= idleRounds) return { clicks, stoppedBecause: "done" };
      await sleep(jitter(minDelayMs, maxDelayMs) * 1.5);
      continue;
    }
    idle = 0;
    clicked.add(next);
    next.scrollIntoView?.({ block: "center" });
    await sleep(jitter(minDelayMs / 2, maxDelayMs / 2));
    // Re-check right before clicking: Facebook re-renders, and a node can change label.
    if (signal?.aborted) return { clicks, stoppedBecause: "aborted" };
    if (!isSafeToClick(next)) continue;
    const text = label(next).trim();
    (next as HTMLElement).click();
    clicks++;
    onProgress?.({ clicks, lastLabel: text });
    await sleep(jitter(minDelayMs, maxDelayMs));
  }
  return { clicks, stoppedBecause: "max-clicks" };
}
