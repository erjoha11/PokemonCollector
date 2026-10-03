import { capturePostId } from "../domain/bids";
import {
  isAutoScanDoneMessage,
  isGetKnownPostsMessage,
  isQueueReadMessage,
  isReadDoneMessage,
  isOpenOverviewMessage,
  isSaveFeedPostsMessage,
  isSavePostCaptureMessage,
  MSG_READ_POST,
  MSG_STORE_UPDATED,
  type KnownPosts,
  type ReadPostMessage,
  type StoreUpdatedMessage,
} from "../shared/messages";
import { groupSlug } from "../shared/feed";
import { getAutoScanState, updateAutoScanState } from "../shared/settings";
import { idbStore } from "../store";
import { AUTO_SCAN_ALARM, describeAutoScan, isGroupFeedUrl, newPostsUrl, runAutoScan, scheduleAutoScan } from "./autoScan";
import { waitForTabLoad } from "./tabs";
import { scheduleClaude } from "./claude";
import { finishRead, getReaderState, isReaderAlarm, kickReader, openAndReadVisible, queueMyAuctionRereads } from "./reader";

// Service worker: storage, the automatic scan's schedule, the Claude bridge, and wiring
// between the toolbar icon, the content script and the overview page.

const store = idbStore();

/** Tells open overview pages to re-read the store. No listener (no page open) is fine. */
function broadcastUpdate(added = 0, updated = 0) {
  const msg: StoreUpdatedMessage = { type: MSG_STORE_UPDATED, added, updated };
  chrome.runtime.sendMessage(msg).catch(() => {});
}
const askClaudeSoon = () => scheduleClaude(store, () => broadcastUpdate());

// Toolbar icon: read the open post, or scan the feed (the content script decides which).
chrome.action.onClicked.addListener(async (tab) => {
  if (tab.id === undefined) return;
  const tabId = tab.id;
  // On the group feed: always scan it sorted by "New posts", so "caught up" means caught up.
  if (isGroupFeedUrl(tab.url) && tab.url !== newPostsUrl(tab.url!)) {
    await chrome.tabs.update(tabId, { url: newPostsUrl(tab.url!) });
    await new Promise((r) => setTimeout(r, 500)); // Let the load start, so "complete" is the new page.
    await waitForTabLoad(tabId, 30_000);
    await new Promise((r) => setTimeout(r, 1500)); // Let the feed render its first posts.
  }
  const msg: ReadPostMessage = { type: MSG_READ_POST };
  chrome.tabs.sendMessage(tabId, msg).catch(() => {
    // Not a Facebook tab, or the tab was open before the extension was (re)loaded and
    // has no content script yet. A badge is the only feedback we can give from here.
    void chrome.action.setBadgeText({ tabId, text: "!" });
    void chrome.action.setTitle({ tabId, title: "Open a Facebook post and reload the tab, then try again." });
  });
});

// Right-click menu on the icon: the overview, and (dev) reload extension + Facebook tabs.
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
  void scheduleAutoScan();
});
chrome.runtime.onStartup.addListener(() => void scheduleAutoScan());

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

// Each time the worker starts: finish or time out reads left over from before (and go on with the queue).
void kickReader();

// The automatic scan: alarm → run; switching it on/off in the overview reschedules.
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === AUTO_SCAN_ALARM) void runAutoScan();
  if (isReaderAlarm(alarm.name)) void kickReader();
});
// You closed the background reader's tab: count that read as done and go on.
chrome.tabs.onRemoved.addListener((tabId) => {
  void getReaderState().then((s) => {
    if (s.current?.tabId === tabId || s.visible[tabId] !== undefined) void finishRead(tabId, false, "its tab was closed");
  });
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes.settings) return;
  const before = changes.settings.oldValue as { autoScan?: boolean; useClaude?: boolean } | undefined;
  const after = changes.settings.newValue as { autoScan?: boolean; useClaude?: boolean } | undefined;
  if (before?.autoScan !== after?.autoScan) void scheduleAutoScan();
  if (after?.useClaude && !before?.useClaude) askClaudeSoon();
});

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

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (isOpenOverviewMessage(msg)) {
    void openOverview();
    return;
  }
  if (isQueueReadMessage(msg)) {
    void openAndReadVisible(msg.url, msg.postId);
    return;
  }
  if (isReadDoneMessage(msg)) {
    if (sender.tab?.id !== undefined) void finishRead(sender.tab.id, msg.ok, msg.outcome);
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
      broadcastUpdate(result.added, result.updated);
      askClaudeSoon();
    });
    return true;
  }
  if (isSavePostCaptureMessage(msg)) {
    void (async () => {
      const { capture } = msg;
      const id = capturePostId(capture);
      if (!id) return;
      await store.saveCapture(id, capture);
      // A post read directly also belongs in the overview, even if no scan has seen it.
      const slug = groupSlug(capture.pageUrl);
      await store.savePosts(
        [
          {
            id,
            url: slug ? `https://www.facebook.com/groups/${slug}/posts/${id}/` : capture.pageUrl,
            groupSlug: slug,
            sellerName: capture.post.author,
            text: capture.post.text,
            textComplete: !capture.post.truncated && capture.post.text.length > 0,
            thumbnailUrl: capture.post.images[0]?.src ?? null,
          },
        ],
        new Date(capture.capturedAt),
      );
      broadcastUpdate();
      askClaudeSoon();
    })();
    return;
  }
  if (isAutoScanDoneMessage(msg)) {
    void (async () => {
      const state = await getAutoScanState();
      const since = state.lastAt ? Date.parse(state.lastAt) : Date.now();
      const added = (await store.allPosts()).filter((p) => Date.parse(p.firstSeenAt) >= since).length;
      await updateAutoScanState({ running: false, lastOutcome: describeAutoScan(msg.stoppedBecause, msg.posts, added) });
      broadcastUpdate(added);
      // While auto-scan is on, keep your auctions fresh: re-read the ones you're bidding in.
      await queueMyAuctionRereads(store);
    })();
    return;
  }
});
