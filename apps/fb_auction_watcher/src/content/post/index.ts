import type { FeedPost } from "../../shared/feed";
import {
  isAutoScanMessage,
  isReadPostMessage,
  MSG_AUTO_SCAN_DONE,
  MSG_GET_KNOWN_POSTS,
  MSG_READ_DONE,
  MSG_OPEN_OVERVIEW,
  MSG_SAVE_FEED_POSTS,
  MSG_SAVE_POST_CAPTURE,
  type AutoScanDoneMessage,
  type KnownPosts,
  type ReadDoneMessage,
  type SaveFeedPostsMessage,
  type SavePostCaptureMessage,
} from "../../shared/messages";
import { extractFeedPost } from "../feed/extract";
import { feedPosts, feedSampleHtml, recordFeed } from "../feed/recorder";
import { scanFeed, type ScanOptions, type ScanResult } from "../feed/scan";
import { expandAll } from "./expand";
import { extractCapture, findPostRoot } from "./extract";
import { showPanel, silentPanel, type Panel } from "./panel";
import { ensureAllComments } from "./sort";

// Content script on facebook.com. Two jobs, both read-only:
// - Toolbar icon on an open post: switch its comments to "All comments", expand comments,
//   replies and "See more", read it, and save it (bids, your status) to the overview.
// - Toolbar icon on the group feed, or the automatic scan: scroll the feed (or, in a background
//   tab, read what's rendered), open "Se mer" on sale posts, and save each post.
// The clicks are the allowlisted ones only (patterns.ts); nothing is ever written.

let running = false;
let activeRecorder: { stop(): void } | null = null;

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/** Scans the feed and saves posts as it goes. Shared by the icon (with a panel) and the automatic scan. */
async function runFeedScan(feed: Element, options: ScanOptions & { panel?: Panel }): Promise<ScanResult> {
  const { panel, ...scanOptions } = options;
  const recorder = recordFeed<FeedPost>(feed, () => {}, (el) => extractFeedPost(el, location.href));
  activeRecorder = recorder;
  // Save as we go, so nothing is lost if the tab is closed mid-scan.
  const saveChanged = () => {
    const posts = recorder.takeChanged().flatMap((r) => (r.data ? [r.data] : []));
    if (posts.length === 0) return;
    const msg: SaveFeedPostsMessage = { type: MSG_SAVE_FEED_POSTS, posts, seenAt: new Date().toISOString() };
    chrome.runtime.sendMessage(msg).catch(() => {});
  };
  panel?.showFeedRecorder(
    () => feedSampleHtml(location.href, recorder.posts()),
    () => void chrome.runtime.sendMessage({ type: MSG_OPEN_OVERVIEW }).catch(() => {}),
  );
  // Posts saved by earlier scans: stop once caught up, and don't reopen their "Se mer".
  const known: KnownPosts | undefined = await chrome.runtime.sendMessage({ type: MSG_GET_KNOWN_POSTS }).catch(() => undefined);
  try {
    return await scanFeed(feed, recorder, {
      ...scanOptions,
      knownIds: new Set(known?.ids ?? []),
      completeIds: new Set(known?.completeIds ?? []),
      onProgress: (p) => {
        panel?.setScanProgress(p);
        saveChanged();
      },
    });
  } finally {
    recorder.flush();
    saveChanged();
    recorder.stop();
  }
}

async function readOpenPost({ waitForPost = false, silent = false } = {}) {
  // A background read always reports back, so the reader queue can close the tab and move on.
  const report = (ok: boolean, outcome: string) => {
    if (!silent) return;
    const msg: ReadDoneMessage = { type: MSG_READ_DONE, ok, outcome };
    chrome.runtime.sendMessage(msg).catch(() => {});
  };
  if (running) return report(false, "busy");
  running = true;
  activeRecorder?.stop();
  activeRecorder = null;
  const panel = silent ? silentPanel() : showPanel();
  try {
    // Opened from the overview: Facebook renders the post a moment after the page loads.
    if (waitForPost) {
      panel.setStatus("Waiting for the post to load…");
      for (let i = 0; i < 30 && !findPostRoot(document)?.querySelector("[role='article']"); i++) await sleep(500);
      await sleep(1000); // Let the comment area settle before switching sort / expanding.
    }
    const root = findPostRoot(document);
    if (!root && silent) return report(false, "the post didn't load");
    if (!root) {
      const feed = document.querySelector("[role='feed']");
      if (feed) {
        const controller = new AbortController();
        panel.onStop(() => controller.abort());
        panel.showScanDone(await runFeedScan(feed, { panel, signal: controller.signal }));
        return;
      }
      panel.showError("No open post found. Open a single post (click its timestamp, or open it in a dialog) and try again.");
      return;
    }
    const controller = new AbortController();
    panel.onStop(() => controller.abort());
    // A quiet read must always report back: cap it, and read whatever has loaded by then.
    const cap = silent ? setTimeout(() => controller.abort(), 4 * 60_000) : null;
    panel.setStatus("Switching comments to All comments…");
    const commentSortAction = await ensureAllComments(root, { signal: controller.signal });
    panel.setStatus("Loading all comments and replies…");
    const result = await expandAll(root, {
      signal: controller.signal,
      onProgress: ({ clicks, scrolls, lastLabel }) =>
        panel.setStatus(`Expanding… ${clicks} clicked, ${scrolls} scrolled (last: "${lastLabel}")`),
    });
    if (cap) clearTimeout(cap);
    panel.setStatus("Reading…");
    const capture = extractCapture(root, {
      pageUrl: location.href,
      pageLang: document.documentElement.lang,
      expandClicks: result.clicks,
      expandScrolls: result.scrolls,
      expandStoppedBecause: result.stoppedBecause,
      commentSortAction,
    });
    // Save it for the overview (lots, bids, your status), unless you stopped it. A quiet read
    // that hit its time cap is saved anyway: partial bids beat none, and it's flagged.
    if (result.stoppedBecause !== "aborted" || silent) {
      const msg: SavePostCaptureMessage = { type: MSG_SAVE_POST_CAPTURE, capture };
      await chrome.runtime.sendMessage(msg).catch(() => {});
    }
    report(
      true,
      `${capture.stats.topLevelComments} comments, ${capture.stats.replies} replies` +
        (result.stoppedBecause === "aborted" ? " (took too long; some may be missing)" : ""),
    );
    panel.showResult(capture, `<!doctype html>\n<!-- ${location.href} -->\n${root.outerHTML}`);
  } catch (err) {
    report(false, err instanceof Error ? err.message : String(err));
    panel.showError(`Failed: ${err instanceof Error ? err.message : String(err)}`);
  } finally {
    running = false;
  }
}

/** The automatic scan, asked for by the service worker after it reloaded this (pinned) feed tab. */
async function autoScan() {
  if (running) return;
  running = true;
  let result: ScanResult | { stoppedBecause: string; posts: number };
  try {
    // The tab was just reloaded: wait for the feed and its first posts to render.
    let feed: Element | null = null;
    for (let i = 0; i < 40 && !(feed && feedPosts(feed).length > 0); i++) {
      await sleep(500);
      feed = document.querySelector("[role='feed']");
    }
    result = feed && feedPosts(feed).length > 0
      ? await runFeedScan(feed, { whenHidden: "stop", maxPosts: 60 })
      : { stoppedBecause: "no-posts-rendered", posts: 0 };
  } catch (err) {
    result = { stoppedBecause: `failed: ${err instanceof Error ? err.message : String(err)}`, posts: 0 };
  } finally {
    running = false;
  }
  const done: AutoScanDoneMessage = { type: MSG_AUTO_SCAN_DONE, stoppedBecause: result.stoppedBecause, posts: result.posts };
  chrome.runtime.sendMessage(done).catch(() => {});
}

chrome.runtime.onMessage.addListener((msg) => {
  if (isReadPostMessage(msg)) void readOpenPost({ waitForPost: msg.waitForPost, silent: msg.silent });
  if (isAutoScanMessage(msg)) void autoScan();
});
