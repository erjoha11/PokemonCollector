// Messages between the service worker and content scripts.
import type { PostCapture } from "./capture";
import type { FeedPost } from "./feed";

export const MSG_READ_POST = "fbaw/read-post" as const;

export type ReadPostMessage = {
  type: typeof MSG_READ_POST;
  /** Opened from the overview: the page just loaded, so wait for the post to render first. */
  waitForPost?: boolean;
  /** A background read: no panel, report back with MSG_READ_DONE. */
  silent?: boolean;
};

export function isReadPostMessage(msg: unknown): msg is ReadPostMessage {
  return typeof msg === "object" && msg !== null && (msg as { type?: unknown }).type === MSG_READ_POST;
}

/** Content script → service worker: sale posts read from the feed, to store. */
export const MSG_SAVE_FEED_POSTS = "fbaw/save-feed-posts" as const;
export type SaveFeedPostsMessage = { type: typeof MSG_SAVE_FEED_POSTS; posts: FeedPost[]; seenAt: string };

/** Content script → service worker: open (or focus) the overview page. */
export const MSG_OPEN_OVERVIEW = "fbaw/open-overview" as const;
export type OpenOverviewMessage = { type: typeof MSG_OPEN_OVERVIEW };

/** Service worker → extension pages: the store changed, re-read it. */
export const MSG_STORE_UPDATED = "fbaw/store-updated" as const;
export type StoreUpdatedMessage = { type: typeof MSG_STORE_UPDATED; added: number; updated: number };

function hasType<T extends string>(msg: unknown, type: T): boolean {
  return typeof msg === "object" && msg !== null && (msg as { type?: unknown }).type === type;
}
export const isSaveFeedPostsMessage = (m: unknown): m is SaveFeedPostsMessage => hasType(m, MSG_SAVE_FEED_POSTS);
export const isOpenOverviewMessage = (m: unknown): m is OpenOverviewMessage => hasType(m, MSG_OPEN_OVERVIEW);
export const isStoreUpdatedMessage = (m: unknown): m is StoreUpdatedMessage => hasType(m, MSG_STORE_UPDATED);

/** Content script → service worker: which posts are already stored (for stopping a scan early). */
export const MSG_GET_KNOWN_POSTS = "fbaw/get-known-posts" as const;
export type GetKnownPostsMessage = { type: typeof MSG_GET_KNOWN_POSTS };
export type KnownPosts = { ids: string[]; completeIds: string[] };
export const isGetKnownPostsMessage = (m: unknown): m is GetKnownPostsMessage => hasType(m, MSG_GET_KNOWN_POSTS);

/** Service worker → content script in the pinned feed tab: run an automatic (background) scan. */
export const MSG_AUTO_SCAN = "fbaw/auto-scan" as const;
export type AutoScanMessage = { type: typeof MSG_AUTO_SCAN };
export const isAutoScanMessage = (m: unknown): m is AutoScanMessage => hasType(m, MSG_AUTO_SCAN);

/** Content script → service worker: an automatic scan finished. */
export const MSG_AUTO_SCAN_DONE = "fbaw/auto-scan-done" as const;
export type AutoScanDoneMessage = { type: typeof MSG_AUTO_SCAN_DONE; stoppedBecause: string; posts: number };
export const isAutoScanDoneMessage = (m: unknown): m is AutoScanDoneMessage => hasType(m, MSG_AUTO_SCAN_DONE);

/** Content script → service worker: a post read with the icon (comments, replies), to store. */
export const MSG_SAVE_POST_CAPTURE = "fbaw/save-post-capture" as const;
export type SavePostCaptureMessage = { type: typeof MSG_SAVE_POST_CAPTURE; capture: PostCapture };
export const isSavePostCaptureMessage = (m: unknown): m is SavePostCaptureMessage => hasType(m, MSG_SAVE_POST_CAPTURE);

/** Content script → service worker: a background read (from the reader queue) finished. */
export const MSG_READ_DONE = "fbaw/read-done" as const;
export type ReadDoneMessage = { type: typeof MSG_READ_DONE; ok: boolean; outcome: string };
export const isReadDoneMessage = (m: unknown): m is ReadDoneMessage => hasType(m, MSG_READ_DONE);

/** Overview → service worker: open this post in a tab and read it silently there. */
export const MSG_QUEUE_READ = "fbaw/queue-read" as const;
export type QueueReadMessage = { type: typeof MSG_QUEUE_READ; postId: string; url: string };
export const isQueueReadMessage = (m: unknown): m is QueueReadMessage => hasType(m, MSG_QUEUE_READ);

/** Toolbar menu → service worker: start reading the post, or scanning the feed, in this tab. */
export const MSG_START = "fbaw/start" as const;
export type StartMessage = { type: typeof MSG_START; tabId: number; kind: "read" | "scan" };
export const isStartMessage = (m: unknown): m is StartMessage => hasType(m, MSG_START);

/** Toolbar menu → service worker: reload every Facebook tab (e.g. after reloading the extension). */
export const MSG_RELOAD_FB_TABS = "fbaw/reload-fb-tabs" as const;
export const isReloadFbTabsMessage = (m: unknown): m is { type: typeof MSG_RELOAD_FB_TABS } => hasType(m, MSG_RELOAD_FB_TABS);
