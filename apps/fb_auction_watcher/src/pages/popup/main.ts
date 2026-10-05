import { MSG_OPEN_OVERVIEW, MSG_SCAN_FEED_NEW_TAB, MSG_START, type ScanFeedNewTabMessage, type StartMessage } from "../../shared/messages";
import { getAutoScanState, getSettings } from "../../shared/settings";
import { popupActions } from "./actions";

// The toolbar icon's menu (#320): four buttons, Open Dashboard · Scan feed · Scan Post · Reload
// extension. Nothing starts until you pick it here. Which button does what for the active tab:
// actions.ts. Reload extension is chrome.runtime.reload(), as before #320 (back on the user's
// word); reloading the Facebook tabs stays on the icon's right-click menu only. Auto-scan's
// on/off switch is in the dashboard's Settings (its status is shown here as text).

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const url = tab?.url;
  const actions = popupActions(url);

  $("overview").addEventListener("click", () => {
    void chrome.runtime.sendMessage({ type: MSG_OPEN_OVERVIEW }).finally(() => window.close());
  });

  const scan = $<HTMLButtonElement>("scan");
  const scanFeed = actions.scanFeed;
  if (scanFeed.kind === "here") scan.classList.add("primary");
  scan.addEventListener("click", () => {
    if (scanFeed.kind === "here") return start("scan");
    const msg: ScanFeedNewTabMessage = { type: MSG_SCAN_FEED_NEW_TAB, url: scanFeed.url };
    void chrome.runtime.sendMessage(msg).finally(() => window.close());
  });

  const read = $<HTMLButtonElement>("read");
  if (actions.scanPost.enabled) {
    read.classList.add("primary");
    read.addEventListener("click", () => start("read"));
  } else {
    read.disabled = true;
    $("read-hint").textContent = actions.scanPost.hint;
  }

  $("reload-ext").addEventListener("click", () => chrome.runtime.reload());

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
