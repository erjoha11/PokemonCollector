import type { FeedPost } from "../../shared/feed";
import {
  isReadPostMessage,
  MSG_GET_KNOWN_POSTS,
  MSG_OPEN_OVERVIEW,
  MSG_SAVE_FEED_POSTS,
  type KnownPosts,
  type SaveFeedPostsMessage,
} from "../../shared/messages";
import { extractFeedPost } from "../feed/extract";
import { feedSampleHtml, recordFeed } from "../feed/recorder";
import { scanFeed } from "../feed/scan";
import { expandAll } from "./expand";
import { extractCapture, findPostRoot } from "./extract";
import { showPanel } from "./panel";
import { ensureAllComments } from "./sort";

// Module 1 spike: on request (toolbar icon), switch the open post's comments to "All
// comments", expand comments, replies and "See more", then offer the raw capture as JSON.
// Those are the only clicks; nothing is ever written.

let running = false;
let activeRecorder: { stop(): void } | null = null;

async function readOpenPost() {
  if (running) return;
  running = true;
  activeRecorder?.stop();
  activeRecorder = null;
  const panel = showPanel();
  try {
    const root = findPostRoot(document);
    if (!root) {
      const feed = document.querySelector("[role='feed']");
      if (feed) {
        // On the group feed with no post open: scan it. Scrolls slowly, records each post as
        // it renders (Facebook empties posts that leave the screen), and clicks "Se mer" on
        // auction and claim-sale posts for their full text. No other clicks.
        const controller = new AbortController();
        const recorder = recordFeed<FeedPost>(feed, () => {}, (el) => extractFeedPost(el, location.href));
        activeRecorder = recorder;
        // Save as we go, so nothing is lost if the tab is closed mid-scan.
        const saveChanged = () => {
          const posts = recorder.takeChanged().flatMap((r) => (r.data ? [r.data] : []));
          if (posts.length === 0) return;
          const msg: SaveFeedPostsMessage = { type: MSG_SAVE_FEED_POSTS, posts, seenAt: new Date().toISOString() };
          chrome.runtime.sendMessage(msg).catch(() => {});
        };
        panel.onStop(() => controller.abort());
        panel.showFeedRecorder(
          () => feedSampleHtml(location.href, recorder.posts()),
          () => void chrome.runtime.sendMessage({ type: MSG_OPEN_OVERVIEW }).catch(() => {}),
        );
        // Posts saved by earlier scans: stop once caught up, and don't reopen their "Se mer".
        const known: KnownPosts = await chrome.runtime
          .sendMessage({ type: MSG_GET_KNOWN_POSTS })
          .catch(() => ({ ids: [], completeIds: [] }));
        const result = await scanFeed(feed, recorder, {
          signal: controller.signal,
          knownIds: new Set(known?.ids ?? []),
          completeIds: new Set(known?.completeIds ?? []),
          onProgress: (p) => {
            panel.setScanProgress(p);
            saveChanged();
          },
        });
        recorder.flush();
        saveChanged();
        recorder.stop();
        panel.showScanDone(result);
        return;
      }
      panel.showError("No open post found. Open a single post (click its timestamp, or open it in a dialog) and try again.");
      return;
    }
    const controller = new AbortController();
    panel.onStop(() => controller.abort());
    panel.setStatus("Switching comments to All comments…");
    const commentSortAction = await ensureAllComments(root, { signal: controller.signal });
    panel.setStatus("Loading all comments and replies…");
    const result = await expandAll(root, {
      signal: controller.signal,
      onProgress: ({ clicks, scrolls, lastLabel }) =>
        panel.setStatus(`Expanding… ${clicks} clicked, ${scrolls} scrolled (last: "${lastLabel}")`),
    });
    panel.setStatus("Reading…");
    const capture = extractCapture(root, {
      pageUrl: location.href,
      pageLang: document.documentElement.lang,
      expandClicks: result.clicks,
      expandScrolls: result.scrolls,
      expandStoppedBecause: result.stoppedBecause,
      commentSortAction,
    });
    panel.showResult(capture, `<!doctype html>\n<!-- ${location.href} -->\n${root.outerHTML}`);
  } catch (err) {
    panel.showError(`Failed: ${err instanceof Error ? err.message : String(err)}`);
  } finally {
    running = false;
  }
}

chrome.runtime.onMessage.addListener((msg) => {
  if (isReadPostMessage(msg)) void readOpenPost();
});
