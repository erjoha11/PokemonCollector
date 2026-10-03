// Messages between the service worker and content scripts.
import type { FeedPost } from "./feed";

export const MSG_READ_POST = "fbaw/read-post" as const;

export type ReadPostMessage = { type: typeof MSG_READ_POST };

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
