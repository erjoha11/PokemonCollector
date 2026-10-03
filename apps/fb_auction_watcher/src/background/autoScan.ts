import { MSG_AUTO_SCAN, type AutoScanMessage } from "../shared/messages";
import { getAutoScanState, getSettings, updateAutoScanState } from "../shared/settings";
import { getReaderState } from "./reader";

// The automatic feed scan (docs/spec.md "Slow pacing"):
// - every 10-15 min ±20 % jitter, one-shot alarms rescheduled after each run;
// - skipped while the machine is locked or idle (chrome.idle);
// - only ever the one pinned group-feed tab: never opens tabs, never two at once;
// - skipped when you're looking at that tab (a reload would yank it from under you);
// - off unless switched on in the overview (Settings.autoScan), the off switch.
// The run itself: reload the tab (the newest posts render at the top), then ask its content
// script to scan in background mode. It reports back with MSG_AUTO_SCAN_DONE.

export const AUTO_SCAN_ALARM = "fbaw-auto-scan";
/** A run that hasn't reported back after this long is considered dead. */
const RUN_TIMEOUT_MS = 10 * 60_000;

/** Minutes until the next scan: 10-15, then ±20 %. */
export function nextDelayMinutes(random: () => number = Math.random): number {
  return (10 + random() * 5) * (0.8 + random() * 0.4);
}

export async function scheduleAutoScan(): Promise<void> {
  const settings = await getSettings();
  if (!settings.autoScan) {
    await chrome.alarms.clear(AUTO_SCAN_ALARM);
    await updateAutoScanState({ nextAt: null, running: false });
    return;
  }
  const when = Date.now() + nextDelayMinutes() * 60_000;
  await chrome.alarms.create(AUTO_SCAN_ALARM, { when });
  await updateAutoScanState({ nextAt: new Date(when).toISOString() });
}

/** The group feed itself, not a post, photo or profile inside the group. */
export function isGroupFeedUrl(url: string | undefined): boolean {
  if (!url) return false;
  const m = url.match(/^https:\/\/www\.facebook\.com\/groups\/[^/?#]+\/?(\?[^#]*)?(#.*)?$/);
  return !!m;
}

async function findFeedTab(): Promise<chrome.tabs.Tab | null> {
  const tabs = await chrome.tabs.query({ url: "https://www.facebook.com/groups/*" });
  const feeds = tabs.filter((t) => isGroupFeedUrl(t.url));
  return feeds.find((t) => t.pinned) ?? null;
}

function waitForLoad(tabId: number, timeoutMs = 30_000): Promise<boolean> {
  return new Promise((resolve) => {
    const timer = setTimeout(() => finish(false), timeoutMs);
    const listener = (id: number, info: { status?: string }) => {
      if (id === tabId && info.status === "complete") finish(true);
    };
    function finish(ok: boolean) {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve(ok);
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function skip(outcome: string) {
  await updateAutoScanState({ lastAt: new Date().toISOString(), lastOutcome: outcome, running: false });
}

/** One automatic scan attempt. Always reschedules afterwards (when still switched on). */
export async function runAutoScan(): Promise<void> {
  try {
    if (!(await getSettings()).autoScan) return;
    const state = await getAutoScanState();
    if (state.running && state.lastAt && Date.now() - Date.parse(state.lastAt) < RUN_TIMEOUT_MS) return;

    const idle = await chrome.idle.queryState(120);
    if (idle !== "active") return await skip(idle === "locked" ? "Skipped: screen locked" : "Skipped: you're away (idle)");

    // One tab talking to Facebook at a time: not while a post is being read in the background.
    if ((await getReaderState()).current) return await skip("Skipped: a post was being read");

    const tab = await findFeedTab();
    if (!tab?.id) return await skip("Skipped: no pinned tab with the group feed");
    if (tab.active) {
      const win = await chrome.windows.get(tab.windowId);
      if (win.focused) return await skip("Skipped: you're looking at the feed tab");
    }

    await updateAutoScanState({ lastAt: new Date().toISOString(), lastOutcome: "Scanning…", running: true });
    const loaded = waitForLoad(tab.id);
    await chrome.tabs.reload(tab.id);
    if (!(await loaded)) return await skip("Failed: the feed tab didn't finish loading");
    const msg: AutoScanMessage = { type: MSG_AUTO_SCAN };
    await chrome.tabs.sendMessage(tab.id, msg).catch(async () => {
      await skip("Failed: no content script in the feed tab");
    });
  } finally {
    await scheduleAutoScan();
  }
}

/** Plain-language outcome of a finished automatic scan. */
export function describeAutoScan(stoppedBecause: string, posts: number, added: number): string {
  const what = added === 0 ? "no new posts" : `${added} new post${added === 1 ? "" : "s"}`;
  switch (stoppedBecause) {
    case "caught-up":
      return `Done: ${what}, caught up`;
    case "hidden":
      // Newest first: if any post read was already saved, nothing in between was skipped.
      return added > 0 && added >= posts
        ? `Done: ${what}; may have missed older ones (open the feed tab and scan to catch up)`
        : `Done: ${what}, caught up`;
    case "no-posts-rendered":
      return "Failed: the feed showed no posts in the background tab";
    default:
      return `Done: ${what} (${stoppedBecause}, ${posts} read)`;
  }
}
