import { groupSlug } from "../../shared/feed";
import { groupFeedUrl, isGroupFeedUrl, isPostUrl } from "../../shared/urls";

// The toolbar menu's three buttons, without any DOM (#320): what each does for the active tab.
// Open Dashboard always works; Scan feed scans the feed here, or opens it in a new tab and scans
// there; Scan Post reads the post shown here, and is disabled with a hint anywhere else.

export type ScanFeedAction =
  /** The active tab is the group feed: scan it there. */
  | { kind: "here" }
  /** Open this feed in a new tab and scan it once it has loaded. */
  | { kind: "new-tab"; url: string }
  /** No group known yet (never scanned, no group tab open): nothing to open. */
  | { kind: "unavailable"; hint: string };

export type ScanPostAction = { enabled: true } | { enabled: false; hint: string };

export type PopupActions = { scanFeed: ScanFeedAction; scanPost: ScanPostAction };

export const SCAN_POST_HINT = "Open a post on Facebook to read it.";
export const NO_GROUP_HINT = "Open the group on Facebook once to scan its feed.";

/**
 * Which group's feed to open when the active tab isn't it: the group of the active tab (a post in
 * it, say), else an open feed tab's group (the pinned one first), else the group of the post seen
 * most recently. Null when none is known.
 */
export function feedToOpen(
  activeUrl: string | undefined,
  feedTabs: { url?: string; pinned?: boolean }[],
  recentGroupSlug: string | null,
): string | null {
  const here = activeUrl && /^https:\/\/www\.facebook\.com\/groups\//.test(activeUrl) ? groupSlug(activeUrl) : null;
  if (here) return groupFeedUrl(here);
  const feeds = feedTabs.filter((t) => isGroupFeedUrl(t.url));
  const tab = feeds.find((t) => t.pinned) ?? feeds[0];
  const slug = tab?.url ? groupSlug(tab.url) : recentGroupSlug;
  return slug ? groupFeedUrl(slug) : null;
}

export function popupActions(activeUrl: string | undefined, feedUrl: string | null): PopupActions {
  const scanFeed: ScanFeedAction = isGroupFeedUrl(activeUrl)
    ? { kind: "here" }
    : feedUrl
      ? { kind: "new-tab", url: feedUrl }
      : { kind: "unavailable", hint: NO_GROUP_HINT };
  const scanPost: ScanPostAction = isPostUrl(activeUrl) ? { enabled: true } : { enabled: false, hint: SCAN_POST_HINT };
  return { scanFeed, scanPost };
}
