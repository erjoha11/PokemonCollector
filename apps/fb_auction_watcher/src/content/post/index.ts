import { isReadPostMessage } from "../../shared/messages";
import { expandAll } from "./expand";
import { extractCapture, findPostRoot } from "./extract";
import { showPanel } from "./panel";
import { ensureAllComments } from "./sort";

// Module 1 spike: on request (toolbar icon), switch the open post's comments to "All
// comments", expand comments, replies and "See more", then offer the raw capture as JSON.
// Those are the only clicks; nothing is ever written.

let running = false;

async function readOpenPost() {
  if (running) return;
  running = true;
  const panel = showPanel();
  try {
    const root = findPostRoot(document);
    if (!root) {
      const feed = document.querySelector("[role='feed']");
      if (feed) {
        // On the group feed with no post open: offer a raw snapshot of the rendered feed, as a
        // sample for building the feed reader. Read-only: nothing is clicked or scrolled.
        const posts = Array.from(feed.querySelectorAll("[role='article']")).filter(
          (a) => !a.parentElement?.closest("[role='article']"),
        ).length;
        panel.showFeedSnapshot(posts, `<!doctype html>\n<!-- ${location.href} -->\n${feed.outerHTML}`);
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
