import { isGroupFeedUrl } from "../../background/autoScan";
import { MSG_OPEN_OVERVIEW, MSG_RELOAD_FB_TABS, MSG_START, type StartMessage } from "../../shared/messages";
import { getAutoScanState, getSettings } from "../../shared/settings";

// The toolbar icon's menu. Nothing starts until you pick it here: read the open post, scan the
// feed, open the overview, or reload the extension / the Facebook tabs (separately).

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const url = tab?.url ?? "";
  const onFacebook = /^https:\/\/www\.facebook\.com\//.test(url);
  const onFeed = isGroupFeedUrl(url);
  const where = $("where");
  if (onFeed) {
    where.textContent = "The group feed is open.";
    $("scan").hidden = false;
  } else if (onFacebook) {
    where.textContent = "A Facebook page is open: read the post shown on it.";
    $("read").hidden = false;
  } else {
    where.textContent = "Open the group or a post on Facebook to read or scan it.";
  }

  const start = (kind: StartMessage["kind"]) => {
    if (tab?.id === undefined) return;
    const msg: StartMessage = { type: MSG_START, tabId: tab.id, kind };
    void chrome.runtime.sendMessage(msg).finally(() => window.close());
  };
  $("read").addEventListener("click", () => start("read"));
  $("scan").addEventListener("click", () => start("scan"));
  $("overview").addEventListener("click", () => {
    void chrome.runtime.sendMessage({ type: MSG_OPEN_OVERVIEW }).finally(() => window.close());
  });
  $("reload-ext").addEventListener("click", () => chrome.runtime.reload());
  $("reload-fb").addEventListener("click", () => {
    void chrome.runtime.sendMessage({ type: MSG_RELOAD_FB_TABS }).finally(() => window.close());
  });

  const [settings, auto] = await Promise.all([getSettings(), getAutoScanState()]);
  $("auto").textContent = settings.autoScan
    ? `Auto-scan on${auto.nextAt ? `, next in ${Math.max(0, Math.round((Date.parse(auto.nextAt) - Date.now()) / 60_000))} min` : ""}${auto.lastOutcome ? ` · last: ${auto.lastOutcome}` : ""}`
    : "Auto-scan is off (switch it on in the overview's Settings).";
}

void init();
