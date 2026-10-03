import { MSG_READ_POST, type ReadPostMessage } from "../shared/messages";

// Module 1 spike: clicking the toolbar icon asks the content script in the active tab
// to read the open post.
chrome.action.onClicked.addListener((tab) => {
  if (tab.id === undefined) return;
  const tabId = tab.id;
  const msg: ReadPostMessage = { type: MSG_READ_POST };
  chrome.tabs.sendMessage(tabId, msg).catch(() => {
    // Not a Facebook tab, or the tab was open before the extension was (re)loaded and
    // has no content script yet. A badge is the only feedback we can give from here.
    void chrome.action.setBadgeText({ tabId, text: "!" });
    void chrome.action.setTitle({ tabId, title: "Open a Facebook post and reload the tab, then try again." });
  });
});

// Dev convenience: right-click the toolbar icon → "Reload extension and Facebook tabs".
// Reloading the extension orphans the content script in open tabs, so after the reload the
// new service worker reloads every Facebook tab. A flag in storage carries that intent
// across the reload (the old worker is gone by then).
const RELOAD_MENU_ID = "fbaw-reload";
const RELOAD_TABS_FLAG = "fbaw-reload-tabs-pending";

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: RELOAD_MENU_ID,
      title: "Reload extension and Facebook tabs",
      contexts: ["action"],
    });
  });
});

chrome.contextMenus.onClicked.addListener((info) => {
  if (info.menuItemId !== RELOAD_MENU_ID) return;
  void chrome.storage.local.set({ [RELOAD_TABS_FLAG]: true }).then(() => chrome.runtime.reload());
});

void (async () => {
  const stored = await chrome.storage.local.get(RELOAD_TABS_FLAG);
  if (!stored[RELOAD_TABS_FLAG]) return;
  await chrome.storage.local.remove(RELOAD_TABS_FLAG);
  const tabs = await chrome.tabs.query({ url: "https://www.facebook.com/*" });
  for (const tab of tabs) if (tab.id !== undefined) void chrome.tabs.reload(tab.id);
})();
