import { capturePostId } from "../domain/bids";
import { mergeCaptures } from "../domain/captures";
import {
  isAutoScanDoneMessage,
  isGetKnownPostsMessage,
  isReloadFbTabsMessage,
  isStartMessage,
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

// Toolbar menu (popup.html): reading a post or scanning the feed starts only when chosen there.
async function startInTab(tabId: number, kind: "read" | "scan") {
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!tab) return;
  // The feed scan always runs on "New posts", so "caught up" means caught up.
  if (kind === "scan" && isGroupFeedUrl(tab.url) && tab.url !== newPostsUrl(tab.url!)) {
    await chrome.tabs.update(tabId, { url: newPostsUrl(tab.url!) });
    await new Promise((r) => setTimeout(r, 500)); // Let the load start, so "complete" is the new page.
    await waitForTabLoad(tabId, 30_000);
    await new Promise((r) => setTimeout(r, 1500)); // Let the feed render its first posts.
  }
  const msg: ReadPostMessage = { type: MSG_READ_POST };
  await chrome.tabs.sendMessage(tabId, msg).catch(() => {
    // The tab was open before the extension was (re)loaded: no content script yet.
    void chrome.action.setBadgeText({ tabId, text: "!" });
    void chrome.action.setTitle({ tabId, title: "Reload this Facebook tab, then try again." });
  });
}

async function reloadFacebookTabs() {
  const tabs = await chrome.tabs.query({ url: "https://www.facebook.com/*" });
  for (const tab of tabs) if (tab.id !== undefined) void chrome.tabs.reload(tab.id);
}

// Right-click menu on the icon: the overview, and (separately) reload the extension or the
// Facebook tabs. After reloading the extension, open Facebook tabs need a reload too to get the
// new content script; that's its own item, so either can be done alone.
const OVERVIEW_MENU_ID = "fbaw-overview";
const RELOAD_EXT_MENU_ID = "fbaw-reload-extension";
const RELOAD_FB_MENU_ID = "fbaw-reload-facebook";

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: OVERVIEW_MENU_ID, title: "Open overview", contexts: ["action"] });
    chrome.contextMenus.create({ id: RELOAD_EXT_MENU_ID, title: "Reload extension", contexts: ["action"] });
    chrome.contextMenus.create({ id: RELOAD_FB_MENU_ID, title: "Reload Facebook tabs", contexts: ["action"] });
  });
  void scheduleAutoScan();
});
chrome.runtime.onStartup.addListener(() => void scheduleAutoScan());

chrome.contextMenus.onClicked.addListener((info) => {
  if (info.menuItemId === OVERVIEW_MENU_ID) void openOverview();
  if (info.menuItemId === RELOAD_EXT_MENU_ID) chrome.runtime.reload();
  if (info.menuItemId === RELOAD_FB_MENU_ID) void reloadFacebookTabs();
});

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
  if (isStartMessage(msg)) {
    void startInTab(msg.tabId, msg.kind);
    return;
  }
  if (isReloadFbTabsMessage(msg)) {
    void reloadFacebookTabs();
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
      // Merge with what earlier reads saw: a partial read must not hide a bid (review H2).
      await store.saveCapture(id, mergeCaptures(await store.getCapture(id), capture));
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
