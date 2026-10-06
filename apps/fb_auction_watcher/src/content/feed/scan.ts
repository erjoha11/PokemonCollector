import { saleType } from "../../domain/listing";
import { isSafeToClick } from "../post/expand";
import { isSeeMoreLabel } from "../post/patterns";
import { feedPosts, postId, type FeedRecorder } from "./recorder";

// On-demand feed scan: scrolls the group feed slowly while the recorder saves each post, and
// clicks "Se mer" / "See more" on auction and claim-sale posts so their full text (with the
// end time) is captured. Those "See more" clicks are the only clicks; everything else is
// scrolling. Started by the user from the toolbar icon, stopped by the panel's Stop button.
// A repeat scan stops once it reaches posts already saved, and the scan pauses while its tab
// is hidden (Chrome barely runs hidden tabs, so the feed wouldn't load and it'd stop early).

/** Auction and claim-sale posts, by the group template's headline (see saleType). */
export function isSaleText(text: string | null | undefined): boolean {
  const type = saleType(text ?? "");
  return type === "auction" || type === "claim";
}

/**
 * "See more" buttons on sale posts' own text (data-ad-rendering-role="story_message"),
 * never in comments or elsewhere in the post.
 */
export function findSaleSeeMore(feed: Element, skipIds: ReadonlySet<string> = new Set()): Element[] {
  const buttons: Element[] = [];
  for (const post of feedPosts(feed)) {
    const id = postId(post);
    if (id && skipIds.has(id)) continue; // Full text already saved.
    const message = post.querySelector("[data-ad-rendering-role='story_message']");
    if (!message || !isSaleText(message.textContent)) continue;
    for (const b of Array.from(message.querySelectorAll("[role='button']"))) {
      if (isSeeMoreLabel(b.textContent) && isSafeToClick(b)) buttons.push(b);
    }
  }
  return buttons;
}

/** `posts`: recorded this run, `newPosts`: of those, ones no earlier scan had saved. */
export type ScanProgress = { posts: number; newPosts: number; scrolls: number; seeMoreClicks: number; paused: boolean };

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
  /** Post IDs already saved: a run of `stopAfterKnown` of them in a row means caught up (Infinity: never, see CONTINUE_SCAN). */
  knownIds?: ReadonlySet<string>;
  /** Saved posts whose full text is stored: no need to open their "Se mer" again. */
  completeIds?: ReadonlySet<string>;
  stopAfterKnown?: number;
  /** Whether the page is hidden (a background tab). Defaults to document.hidden. */
  isHidden?: () => boolean;
  /**
   * What to do when the tab is hidden and it's time to scroll. "pause" (a scan you started)
   * waits until you come back; "stop" (the automatic scan in a background tab) records what's
   * rendered, opens its "Se mer", and stops, since a hidden tab won't load more posts.
   */
  whenHidden?: "pause" | "stop";
};

export type ScanResult = ScanProgress & {
  stoppedBecause: "caught-up" | "hidden" | "end-of-feed" | "max-posts" | "max-scrolls" | "aborted" | "dialog-opened";
};

/**
 * "Continue to older posts" (the scan panel, after a scan is done): goes on from where the feed
 * is, past posts already saved, so the older ones an earlier scan never reached (it was stopped,
 * hit its limit, or later scans stopped at the saved posts above them) get saved too. Stops at
 * the end of the feed, after this many posts, or when you press Stop.
 */
export const CONTINUE_SCAN: Pick<ScanOptions, "stopAfterKnown" | "maxPosts"> = { stopAfterKnown: Infinity, maxPosts: 400 };

/** Whether a finished scan can go on to older posts: not past the end of the feed, nor behind a dialog. */
export const canContinue = (stoppedBecause: ScanResult["stoppedBecause"]) => stoppedBecause !== "end-of-feed" && stoppedBecause !== "dialog-opened";

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
    knownIds = new Set<string>(),
    completeIds = new Set<string>(),
    stopAfterKnown = 5,
    isHidden = () => document.hidden,
    whenHidden = "pause",
  } = options;
  const clicked = new WeakSet<Element>();
  let scrolls = 0;
  let seeMoreClicks = 0;
  let idle = 0;
  const count = () => {
    recorder.flush();
    return recorder.posts().length;
  };
  let paused = false;
  const isKnown = (key: string) => key.startsWith("post:") && knownIds.has(key.slice(5));
  const progress = (): ScanProgress => ({ posts: count(), newPosts: recorder.posts().filter((p) => !isKnown(p.key)).length, scrolls, seeMoreClicks, paused });
  /** Known posts at the end of what's been recorded so far, in feed order (newest first). */
  const knownRun = () => {
    let run = 0;
    for (const p of recorder.posts()) run = isKnown(p.key) ? run + 1 : 0;
    return run;
  };
  /** Waits while the tab is hidden. Returns true if it had to wait. */
  const waitWhileHidden = async () => {
    if (!isHidden()) return false;
    paused = true;
    onProgress?.(progress());
    while (isHidden() && !signal?.aborted) await sleep(500);
    paused = false;
    onProgress?.(progress());
    return true;
  };
  const done = (stoppedBecause: ScanResult["stoppedBecause"]): ScanResult => ({ ...progress(), stoppedBecause });

  while (true) {
    if (whenHidden === "pause") await waitWhileHidden();
    if (signal?.aborted) return done("aborted");

    // First open up any cut-off sale post on screen, one at a time.
    const seeMore = findSaleSeeMore(feed, completeIds).find((b) => !clicked.has(b));
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

    if (knownIds.size > 0 && knownRun() >= stopAfterKnown) return done("caught-up");
    if (whenHidden === "stop" && isHidden()) return done("hidden");
    if (count() >= maxPosts) return done("max-posts");
    if (scrolls >= maxScrolls) return done("max-scrolls");

    const before = count();
    scrollStep();
    scrolls++;
    await sleep(jitter(minDelayMs, maxDelayMs));
    onProgress?.(progress());
    if (count() > before) idle = 0;
    // A tab hidden mid-wait loads nothing; that's not the end of the feed.
    else if (whenHidden === "pause" && (await waitWhileHidden())) continue;
    else if (whenHidden === "stop" && isHidden()) return done("hidden");
    else if (++idle >= idleRounds) return done("end-of-feed");
  }
}
