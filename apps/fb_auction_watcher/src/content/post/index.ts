import { isReadPostMessage } from "../../shared/messages";
import { expandAll } from "./expand";
import { extractCapture, findPostRoot } from "./extract";
import { showPanel } from "./panel";

// Module 1 spike: on request (toolbar icon), expand the open post's comments and
// replies, then offer the raw capture as JSON. Read-only apart from expander clicks.

let running = false;

async function readOpenPost() {
  if (running) return;
  running = true;
  const panel = showPanel();
  try {
    const root = findPostRoot(document);
    if (!root) {
      panel.showError("No open post found. Open a single post (click its timestamp, or open it in a dialog) and try again.");
      return;
    }
    const controller = new AbortController();
    panel.onStop(() => controller.abort());
    panel.setStatus("Loading all comments and replies…");
    const result = await expandAll(root, {
      signal: controller.signal,
      onProgress: ({ clicks, lastLabel }) => panel.setStatus(`Expanding… ${clicks} clicked (last: "${lastLabel}")`),
    });
    panel.setStatus("Reading…");
    const capture = extractCapture(root, {
      pageUrl: location.href,
      pageLang: document.documentElement.lang,
      expandClicks: result.clicks,
      expandStoppedBecause: result.stoppedBecause,
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
