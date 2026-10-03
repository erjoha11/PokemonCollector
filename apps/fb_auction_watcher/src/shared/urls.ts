// Facebook URL helpers shared by the service worker, the toolbar menu and tests.

/** The group feed itself, not a post, photo or profile inside the group. */
export function isGroupFeedUrl(url: string | undefined): boolean {
  if (!url) return false;
  const m = url.match(/^https:\/\/www\.facebook\.com\/groups\/[^/?#]+\/?(\?[^#]*)?(#.*)?$/);
  return !!m;
}

/** The group's feed sorted by "New posts": its URL with sorting_setting=CHRONOLOGICAL. */
export function newPostsUrl(feedUrl: string): string {
  const url = new URL(feedUrl);
  url.search = "";
  url.hash = "";
  url.searchParams.set("sorting_setting", "CHRONOLOGICAL");
  return url.toString();
}
