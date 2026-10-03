import {
  isGetKnownPostsMessage,
  isOpenOverviewMessage,
  isSaveFeedPostsMessage,
  MSG_READ_POST,
  MSG_STORE_UPDATED,
  type KnownPosts,
  type ReadPostMessage,
  type StoreUpdatedMessage,
} from "../shared/messages";
import { idbStore } from "../store";

const store = idbStore();

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
const OVERVIEW_MENU_ID = "fbaw-overview";
const RELOAD_TABS_FLAG = "fbaw-reload-tabs-pending";

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: OVERVIEW_MENU_ID, title: "Open overview", contexts: ["action"] });
    chrome.contextMenus.create({
      id: RELOAD_MENU_ID,
      title: "Reload extension and Facebook tabs",
      contexts: ["action"],
    });
  });
});

chrome.contextMenus.onClicked.addListener((info) => {
  if (info.menuItemId === OVERVIEW_MENU_ID) void openOverview();
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

// The overview (table page): focus an open one rather than opening another.
async function openOverview() {
  const url = chrome.runtime.getURL("dashboard.html");
  const [existing] = await chrome.tabs.query({ url }).catch(() => [] as chrome.tabs.Tab[]);
  if (existing?.id !== undefined) {
    await chrome.tabs.update(existing.id, { active: true });
    if (existing.windowId !== undefined) await chrome.windows.update(existing.windowId, { focused: true });
    return;
  }
  await chrome.tabs.create({ url });
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (isOpenOverviewMessage(msg)) {
    void openOverview();
    return;
  }
  if (isGetKnownPostsMessage(msg)) {
    void store.allPosts().then((posts) => {
      const known: KnownPosts = {
        ids: posts.map((p) => p.id),
        completeIds: posts.filter((p) => p.textComplete).map((p) => p.id),
      };
      sendResponse(known);
    });
    return true; // Responds asynchronously.
  }
  if (isSaveFeedPostsMessage(msg)) {
    void store.savePosts(msg.posts, new Date(msg.seenAt)).then(async (result) => {
      await store.setMeta("lastFeedReadAt", msg.seenAt);
      sendResponse(result);
      const update: StoreUpdatedMessage = { type: MSG_STORE_UPDATED, ...result };
      // No open table page means no listener; that's fine.
      chrome.runtime.sendMessage(update).catch(() => {});
    });
    return true; // Responds asynchronously.
  }
});
