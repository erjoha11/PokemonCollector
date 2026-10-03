import { isSafeToClick } from "../post/expand";
import { isSeeMoreLabel } from "../post/patterns";
import { feedPosts, type FeedRecorder } from "./recorder";

// On-demand feed scan: scrolls the group feed slowly while the recorder saves each post, and
// clicks "Se mer" / "See more" on auction and claim-sale posts so their full text (with the
// end time) is captured. Those "See more" clicks are the only clicks; everything else is
// scrolling. Started by the user from the toolbar icon, stopped by the panel's Stop button.

/** Auction and claim-sale posts, by the group template's headline words. */
export function isSaleText(text: string | null | undefined): boolean {
  return /auksjon|budrunde|claim|clame|auction/i.test(text ?? "");
}

/**
 * "See more" buttons on sale posts' own text (data-ad-rendering-role="story_message"),
 * never in comments or elsewhere in the post.
 */
export function findSaleSeeMore(feed: Element): Element[] {
  const buttons: Element[] = [];
  for (const post of feedPosts(feed)) {
    const message = post.querySelector("[data-ad-rendering-role='story_message']");
    if (!message || !isSaleText(message.textContent)) continue;
    for (const b of Array.from(message.querySelectorAll("[role='button']"))) {
      if (isSeeMoreLabel(b.textContent) && isSafeToClick(b)) buttons.push(b);
    }
  }
  return buttons;
}

export type ScanProgress = { posts: number; scrolls: number; seeMoreClicks: number };

export type ScanOptions = {
  signal?: AbortSignal;
  onProgress?: (progress: ScanProgress) => void;
  /** Stop after this many recorded posts. */
  maxPosts?: number;
  maxScrolls?: number;
  /** Scrolls in a row with no new post before treating it as the end of the feed. */
  idleRounds?: number;
  /** Pause after each scroll or click; jittered so the pace isn't mechanical. */
  minDelayMs?: number;
  maxDelayMs?: number;
  /** One scroll step. Defaults to most of a screen height on the page. */
  scrollStep?: () => void;
};

export type ScanResult = ScanProgress & {
  stoppedBecause: "end-of-feed" | "max-posts" | "max-scrolls" | "aborted" | "dialog-opened";
};

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
const jitter = (min: number, max: number) => min + Math.random() * Math.max(0, max - min);

function defaultScrollStep() {
  window.scrollBy({ top: Math.round(window.innerHeight * jitter(0.6, 0.9)), behavior: "smooth" });
}

const openDialogs = () => document.querySelectorAll("[role='dialog']").length;

export async function scanFeed(feed: Element, recorder: FeedRecorder, options: ScanOptions = {}): Promise<ScanResult> {
  const {
    signal,
    onProgress,
    maxPosts = 150,
    maxScrolls = 400,
    idleRounds = 6,
    minDelayMs = 1500,
    maxDelayMs = 3500,
    scrollStep = defaultScrollStep,
  } = options;
  const clicked = new WeakSet<Element>();
  let scrolls = 0;
  let seeMoreClicks = 0;
  let idle = 0;
  const count = () => {
    recorder.flush();
    return recorder.posts().length;
  };
  const progress = (): ScanProgress => ({ posts: count(), scrolls, seeMoreClicks });
  const done = (stoppedBecause: ScanResult["stoppedBecause"]): ScanResult => ({ ...progress(), stoppedBecause });

  while (true) {
    if (signal?.aborted) return done("aborted");

    // First open up any cut-off sale post on screen, one at a time.
    const seeMore = findSaleSeeMore(feed).find((b) => !clicked.has(b));
    if (seeMore) {
      clicked.add(seeMore);
      seeMore.scrollIntoView?.({ block: "center" });
      await sleep(jitter(minDelayMs / 3, maxDelayMs / 3));
      if (signal?.aborted) return done("aborted");
      // Re-check right before clicking: Facebook re-renders, and a node can change label.
      if (!isSafeToClick(seeMore) || !isSeeMoreLabel(seeMore.textContent)) continue;
      const dialogsBefore = openDialogs();
      (seeMore as HTMLElement).click();
      seeMoreClicks++;
      onProgress?.(progress());
      await sleep(jitter(minDelayMs / 2, maxDelayMs / 2));
      // "See more" should expand the text in place. If Facebook opened the post instead,
      // stop rather than keep scrolling behind a dialog.
      if (openDialogs() > dialogsBefore) return done("dialog-opened");
      continue;
    }

    if (count() >= maxPosts) return done("max-posts");
    if (scrolls >= maxScrolls) return done("max-scrolls");

    const before = count();
    scrollStep();
    scrolls++;
    await sleep(jitter(minDelayMs, maxDelayMs));
    onProgress?.(progress());
    if (count() > before) idle = 0;
    else if (++idle >= idleRounds) return done("end-of-feed");
  }
}
