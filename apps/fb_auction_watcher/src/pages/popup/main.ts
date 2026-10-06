import { MSG_OPEN_OVERVIEW, MSG_SCAN_FEED_NEW_TAB, MSG_START, type ScanFeedNewTabMessage, type StartMessage } from "../../shared/messages";
import { getAutoScanState, getSettings } from "../../shared/settings";
import { scanAction } from "./actions";

// The toolbar icon's menu: three buttons, Open Dashboard · Scan (post, feed, or open the feed and
// scan, by the active tab: actions.ts) · Reload extension. Nothing starts until you pick it here. Reload extension is chrome.runtime.reload(), as before #320 (back on the user's
// word); reloading the Facebook tabs stays on the icon's right-click menu only. Auto-scan's
// on/off switch is in the dashboard's Settings (its status is shown here as text).

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const url = tab?.url;

  $("overview").addEventListener("click", () => {
    void chrome.runtime.sendMessage({ type: MSG_OPEN_OVERVIEW }).finally(() => window.close());
  });

  // One Scan button: a post here → read it; the feed here → scan it; anywhere else → open the feed and scan.
  const scan = $<HTMLButtonElement>("scan");
  const action = scanAction(url);
  scan.textContent = action.label;
  scan.title = action.title;
  scan.addEventListener("click", () => {
    if (action.kind === "post") return start("read");
    if (action.kind === "feed") return start("scan");
    const msg: ScanFeedNewTabMessage = { type: MSG_SCAN_FEED_NEW_TAB, url: action.url };
    void chrome.runtime.sendMessage(msg).finally(() => window.close());
  });

  $("reload-ext").addEventListener("click", () => chrome.runtime.reload());

  function start(kind: StartMessage["kind"]) {
    if (tab?.id === undefined) return;
    const msg: StartMessage = { type: MSG_START, tabId: tab.id, kind };
    void chrome.runtime.sendMessage(msg).finally(() => window.close());
  }

  const [settings, auto] = await Promise.all([getSettings(), getAutoScanState()]);
  const minutes = auto.nextAt ? Math.round((Date.parse(auto.nextAt) - Date.now()) / 60_000) : null;
  const next = minutes === null ? "" : minutes < 1 ? ", next in under a minute" : `, next in ${minutes} min`;
  $("auto").textContent = settings.autoScan
    ? `Auto-scan on${next}${auto.lastOutcome ? ` · last: ${auto.lastOutcome}` : ""}. Switch it off in the dashboard's Settings.`
    : "Auto-scan is off (switch it on in the dashboard's Settings).";
}

void init();
