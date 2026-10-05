import { capturePostId } from "../domain/bids";
import { mergeCaptures } from "../domain/captures";
import {
  isActivityDoneMessage,
  isAutoScanDoneMessage,
  isGetKnownPostsMessage,
  isReloadFbTabsMessage,
  isStartMessage,
  isQueueReadMessage,
  isReadDoneMessage,
  isOpenOverviewMessage,
  isSaveFeedPostsMessage,
  isSavePostCaptureMessage,
  isSendWinsMessage,
  isScanFeedNewTabMessage,
  MSG_READ_POST,
  MSG_STORE_UPDATED,
  type KnownPosts,
  type ReadPostMessage,
  type StoreUpdatedMessage,
} from "../shared/messages";
import { groupSlug } from "../shared/feed";
import { canonicalPostUrl, groupPostUrl } from "../shared/urls";
import { getAutoScanState, getSettings, updateAutoScanState } from "../shared/settings";
import { idbStore } from "../store";
import { AUTO_SCAN_ALARM, describeAutoScan, isGroupFeedUrl, newPostsUrl, runAutoScan, scheduleAutoScan } from "./autoScan";
import { waitForTabLoad } from "./tabs";
import { scheduleClaude } from "./claude";
import { finishRead, getReaderState, isReaderAlarm, kickReader, readNow, WATCH_ALARM, watchMyAuctions } from "./reader";
import { listenForNoteClicks, outbidNotes, resultNotes, showNotes, type Note } from "./notify";
import { isFinal, readLots } from "./watch";
import { saleLines } from "../domain/saleLines";
import type { PostCapture } from "../shared/capture";
import { facebookSlot } from "./slot";
import { CLEANUP_ALARM, runCleanup, scheduleCleanup } from "./cleanup";
import { sendWins } from "./inbox";

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
  // One tab talking to Facebook at a time (review H6): wait for whatever runs now to finish.
  const holder = kind === "scan" ? "menu-scan" : "menu-read";
  void chrome.action.setBadgeText({ tabId, text: "…" });
  if (!(await facebookSlot.acquireWhenFree(holder, tabId))) {
    void chrome.action.setBadgeText({ tabId, text: "!" });
    void chrome.action.setTitle({ tabId, title: "Facebook was busy for too long (another read or scan). Try again." });
    return;
  }
  void chrome.action.setBadgeText({ tabId, text: "" });
  // The feed scan always runs on "New posts", so "caught up" means caught up.
  if (kind === "scan" && isGroupFeedUrl(tab.url) && tab.url !== newPostsUrl(tab.url!)) {
    await chrome.tabs.update(tabId, { url: newPostsUrl(tab.url!) });
    await new Promise((r) => setTimeout(r, 500)); // Let the load start, so "complete" is the new page.
    await waitForTabLoad(tabId, 30_000);
    await new Promise((r) => setTimeout(r, 1500)); // Let the feed render its first posts.
  }
  const msg: ReadPostMessage = { type: MSG_READ_POST };
  await chrome.tabs.sendMessage(tabId, msg).catch(() => {
    void facebookSlot.release(holder, tabId);
    // The tab was open before the extension was (re)loaded: no content script yet.
    void chrome.action.setBadgeText({ tabId, text: "!" });
    void chrome.action.setTitle({ tabId, title: "Reload this Facebook tab, then try again." });
  });
}

/**
 * "Scan feed" from the menu when the active tab isn't the feed (#320): open the group's feed
 * (sorted by "New posts") in a new tab and scan it there once it has loaded. The Facebook slot is
 * taken before the tab is opened, so the new tab never talks to Facebook alongside anything else.
 */
async function scanFeedInNewTab(feedUrl: string) {
  if (!isGroupFeedUrl(feedUrl)) return;
  void chrome.action.setBadgeText({ text: "" }); // Clear a "!" left by an earlier try.
  const fail = (title: string) => {
    void chrome.action.setBadgeText({ text: "!" });
    void chrome.action.setTitle({ title });
  };
  // No tab yet: hold the slot for "no tab" (-1), then move it to the tab once it exists (as the reader does).
  if (!(await facebookSlot.acquireWhenFree("menu-scan", -1))) return fail("Facebook was busy for too long (another read or scan). Try again.");
  const tab = await chrome.tabs.create({ url: newPostsUrl(feedUrl), active: true }).catch(() => null);
  if (tab?.id === undefined) {
    await facebookSlot.release("menu-scan", -1);
    return fail("Couldn't open the group feed. Try again.");
  }
  const tabId = tab.id;
  await facebookSlot.moveTo("menu-scan", -1, tabId);
  if (!(await waitForTabLoad(tabId, 30_000))) {
    await facebookSlot.release("menu-scan", tabId);
    void chrome.action.setBadgeText({ tabId, text: "!" });
    void chrome.action.setTitle({ tabId, title: "The feed didn't finish loading. Try Scan feed again." });
    return;
  }
  await new Promise((r) => setTimeout(r, 1500)); // Let the feed render its first posts.
  const msg: ReadPostMessage = { type: MSG_READ_POST };
  await chrome.tabs.sendMessage(tabId, msg).catch(() => {
    void facebookSlot.release("menu-scan", tabId);
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
// The daily cleanup of old stored data (review M6): make sure its alarm exists.
void scheduleCleanup();
// Your auctions: the next final read / "ends in 10 min", from what's stored now.
void watchMyAuctions(store);
listenForNoteClicks();

// The automatic scan: alarm → run; switching it on/off in the overview reschedules.
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === AUTO_SCAN_ALARM) void runAutoScan();
  if (isReaderAlarm(alarm.name)) void kickReader();
  if (alarm.name === CLEANUP_ALARM) void runCleanup(store, () => broadcastUpdate());
  if (alarm.name === WATCH_ALARM) void watchMyAuctions(store);
});
// A tab was closed: if a read was running there, count it as done and go on; if it held the
// Facebook slot (a scan from the menu, say), free it.
chrome.tabs.onRemoved.addListener((tabId) => {
  void getReaderState().then((s) => {
    if (s.current?.tabId === tabId) void finishRead(tabId, false, "its tab was closed");
  });
  void facebookSlot.releaseTab(tabId);
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes.settings) return;
  const before = changes.settings.oldValue as { autoScan?: boolean; useClaude?: boolean } | undefined;
  const after = changes.settings.newValue as { autoScan?: boolean; useClaude?: boolean } | undefined;
  if (before?.autoScan !== after?.autoScan) void scheduleAutoScan();
  if (before?.autoScan !== after?.autoScan || (before as { notify?: boolean })?.notify !== (after as { notify?: boolean })?.notify) void watchMyAuctions(store);
  if (after?.useClaude && !before?.useClaude) askClaudeSoon();
});

/**
 * After a read of an auction you're in: notify the lots you've been outbid on since the previous
 * read, or, when this is its first read in full after the close, what you won and lost.
 */
async function noticeChanges(postId: string, previous: PostCapture | null, merged: PostCapture): Promise<void> {
  const [settings, posts, answers] = await Promise.all([getSettings(), store.allPosts(), store.allAnswers()]);
  if (!settings.notify) return;
  const post = posts.find((p) => p.id === postId);
  if (!post) return;
  const answerMap = new Map(answers.map((a) => [a.key, a.value]));
  const { listing, lots } = readLots(post, merged, answerMap, settings.myName);
  if (listing.type !== "auction") return;
  const endsAt = listing.endsAt ? Date.parse(listing.endsAt) : null;
  const closesAt = endsAt === null ? null : endsAt + (listing.softCloseMinutes ?? 0) * 60_000;
  const sale = { postId, url: canonicalPostUrl(post.url, postId, post.groupSlug), title: saleLines(listing.title, listing.description).title, type: listing.type };
  let notes: Note[];
  if (isFinal(merged, closesAt)) {
    // The result, once: not again for later reads after the close.
    notes = previous && isFinal(previous, closesAt) ? [] : resultNotes(lots, sale);
  } else {
    notes = outbidNotes(previous ? readLots(post, previous, answerMap, settings.myName).lots : null, lots, sale);
  }
  await showNotes(notes);
}

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
  if (isScanFeedNewTabMessage(msg)) {
    void scanFeedInNewTab(msg.url);
    return;
  }
  if (isReloadFbTabsMessage(msg)) {
    void reloadFacebookTabs();
    return;
  }
  if (isQueueReadMessage(msg)) {
    void readNow(msg.url, msg.postId, !!msg.visible);
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
      const previous = await store.getCapture(id);
      const merged = mergeCaptures(previous, capture);
      await store.saveCapture(id, merged);
      // A post read directly also belongs in the overview, even if no scan has seen it. Its link is
      // always the group post's own address, also when it was read in the photo viewer
      // (photo.php?fbid=…, which has no group in it): lot links append ?comment_id= to it.
      const slug = groupSlug(capture.pageUrl) ?? groupSlug(capture.post.url ?? "") ?? capture.comments.map((c) => groupSlug(c.url ?? "")).find(Boolean) ?? null;
      await store.savePosts(
        [
          {
            id,
            url: groupPostUrl(id, slug),
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
      await noticeChanges(id, previous, merged);
      // A read can settle a sale or change what's next: reschedule.
      await watchMyAuctions(store);
    })();
    return;
  }
  if (isSendWinsMessage(msg)) {
    // "Send wins to inventory" (#309): your own wins to your tcg_inventory, nothing else.
    void sendWins(store, undefined, msg.postIds).then(sendResponse);
    return true;
  }
  if (isActivityDoneMessage(msg)) {
    // A scan or read you started from the menu finished: free the Facebook slot.
    const tabId = sender.tab?.id;
    if (tabId !== undefined) void facebookSlot.release("menu-scan", tabId).then(() => facebookSlot.release("menu-read", tabId));
    return;
  }
  if (isAutoScanDoneMessage(msg)) {
    if (sender.tab?.id !== undefined) void facebookSlot.release("auto-scan", sender.tab.id);
    void (async () => {
      const state = await getAutoScanState();
      const since = state.lastAt ? Date.parse(state.lastAt) : Date.now();
      const added = (await store.allPosts()).filter((p) => Date.parse(p.firstSeenAt) >= since).length;
      await updateAutoScanState({ running: false, lastOutcome: describeAutoScan(msg.stoppedBecause, msg.posts, added) });
      broadcastUpdate(added);
      // While auto-scan is on, keep your auctions fresh: re-read the ones you're bidding in.
      await watchMyAuctions(store);
    })();
    return;
  }
});
