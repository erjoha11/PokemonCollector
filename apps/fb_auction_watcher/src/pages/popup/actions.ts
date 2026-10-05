import { GROUP_FEED_URL, isGroupFeedUrl, isPostUrl } from "../../shared/urls";

// The toolbar menu's one Scan button, without any DOM: what it says and does depends on the
// active tab. (Open Dashboard and Reload extension are the same everywhere, wired in main.ts.)
//   - on a post: "Scan post" reads that post here;
//   - on the group feed: "Scan feed" scans it here (the worker switches it to "New posts" first);
//   - anywhere else: "Open feed and scan" opens the group's feed (GROUP_FEED_URL, "New posts") in
//     a new tab and scans it there.

export type ScanAction =
  | { kind: "post"; label: string; title: string }
  | { kind: "feed"; label: string; title: string }
  | { kind: "open-feed"; label: string; title: string; url: string };

export function scanAction(activeUrl: string | undefined): ScanAction {
  if (isPostUrl(activeUrl)) {
    return { kind: "post", label: "Scan post", title: "Read this post: its lots, bids and claims." };
  }
  if (isGroupFeedUrl(activeUrl)) {
    return { kind: "feed", label: "Scan feed", title: 'Scan this feed for new sales, sorted by "New posts".' };
  }
  return {
    kind: "open-feed",
    label: "Open feed and scan",
    title: 'Open the group\'s feed, sorted by "New posts", in a new tab and scan it.',
    url: GROUP_FEED_URL,
  };
}
