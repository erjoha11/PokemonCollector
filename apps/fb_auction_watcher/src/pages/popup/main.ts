import { MSG_OPEN_OVERVIEW, MSG_SCAN_FEED_NEW_TAB, MSG_START, type ScanFeedNewTabMessage, type StartMessage } from "../../shared/messages";
import { getAutoScanState, getSettings } from "../../shared/settings";
import { idbStore } from "../../store";
import { feedToOpen, popupActions } from "./actions";

// The toolbar icon's menu (#320): exactly three buttons, Open Dashboard · Scan feed · Scan Post.
// Nothing starts until you pick it here. Which button does what for the active tab: actions.ts.
// Reloading the extension / the Facebook tabs is on the icon's right-click menu; auto-scan's
// on/off switch is in the dashboard's Settings (its status is shown here as text).

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

/** The group of the post seen most recently, for "Scan feed" when no group tab is open. */
async function recentGroupSlug(): Promise<string | null> {
  const posts = await idbStore().allPosts().catch(() => []);
  const latest = posts.filter((p) => p.groupSlug).sort((a, b) => b.lastSeenAt.localeCompare(a.lastSeenAt))[0];
  return latest?.groupSlug ?? null;
}

async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const url = tab?.url;
  const feedTabs = await chrome.tabs.query({ url: "https://www.facebook.com/groups/*" }).catch(() => [] as chrome.tabs.Tab[]);
  let feedUrl = feedToOpen(url, feedTabs, null);
  if (!feedUrl) feedUrl = feedToOpen(url, [], await recentGroupSlug());
  const actions = popupActions(url, feedUrl);

  $("overview").addEventListener("click", () => {
    void chrome.runtime.sendMessage({ type: MSG_OPEN_OVERVIEW }).finally(() => window.close());
  });

  const scan = $<HTMLButtonElement>("scan");
  const scanFeed = actions.scanFeed;
  if (scanFeed.kind === "unavailable") {
    scan.disabled = true;
    $("scan-hint").textContent = scanFeed.hint;
  } else {
    if (scanFeed.kind === "here") scan.classList.add("primary");
    scan.addEventListener("click", () => {
      if (scanFeed.kind === "here") return start("scan");
      const msg: ScanFeedNewTabMessage = { type: MSG_SCAN_FEED_NEW_TAB, url: scanFeed.url };
      void chrome.runtime.sendMessage(msg).finally(() => window.close());
    });
  }

  const read = $<HTMLButtonElement>("read");
  if (actions.scanPost.enabled) {
    read.classList.add("primary");
    read.addEventListener("click", () => start("read"));
  } else {
    read.disabled = true;
    $("read-hint").textContent = actions.scanPost.hint;
  }

  function start(kind: StartMessage["kind"]) {
    if (tab?.id === undefined) return;
    const msg: StartMessage = { type: MSG_START, tabId: tab.id, kind };
    void chrome.runtime.sendMessage(msg).finally(() => window.close());
  }

  const [settings, auto] = await Promise.all([getSettings(), getAutoScanState()]);
  $("auto").textContent = settings.autoScan
    ? `Auto-scan on${auto.nextAt ? `, next in ${Math.max(0, Math.round((Date.parse(auto.nextAt) - Date.now()) / 60_000))} min` : ""}${auto.lastOutcome ? ` · last: ${auto.lastOutcome}` : ""}. Switch it off in the dashboard's Settings.`
    : "Auto-scan is off (switch it on in the dashboard's Settings).";
}

void init();
