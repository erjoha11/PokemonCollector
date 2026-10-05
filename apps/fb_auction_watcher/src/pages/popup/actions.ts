import { GROUP_FEED_URL, isGroupFeedUrl, isPostUrl } from "../../shared/urls";

// The toolbar menu's buttons that depend on the active tab, without any DOM (#320). (The fourth,
// Reload extension, is the same everywhere: chrome.runtime.reload(), wired in main.ts.)
// Open Dashboard always works; Scan feed scans the feed here, or opens the group's feed
// (GROUP_FEED_URL, sorted by "New posts" by the worker) in a new tab and scans there; Scan Post
// reads the post shown here, and is disabled with a hint anywhere else.

export type ScanFeedAction =
  /** The active tab is the group feed: scan it there. */
  | { kind: "here" }
  /** Open this feed in a new tab and scan it once it has loaded. */
  | { kind: "new-tab"; url: string };

export type ScanPostAction = { enabled: true } | { enabled: false; hint: string };

export type PopupActions = { scanFeed: ScanFeedAction; scanPost: ScanPostAction };

export const SCAN_POST_HINT = "Open a post on Facebook to read it.";

export function popupActions(activeUrl: string | undefined): PopupActions {
  const scanFeed: ScanFeedAction = isGroupFeedUrl(activeUrl) ? { kind: "here" } : { kind: "new-tab", url: GROUP_FEED_URL };
  const scanPost: ScanPostAction = isPostUrl(activeUrl) ? { enabled: true } : { enabled: false, hint: SCAN_POST_HINT };
  return { scanFeed, scanPost };
}
