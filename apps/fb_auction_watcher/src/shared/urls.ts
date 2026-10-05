// Facebook URL helpers shared by the service worker, the toolbar menu and tests.

/**
 * The one group this extension watches (docs/spec.md "Toolbar menu"): what the menu's "Scan feed"
 * opens when the active tab isn't a group feed. The worker opens it sorted by "New posts"
 * (`newPostsUrl`). A constant, not a setting: one user, one group.
 */
export const GROUP_FEED_URL = "https://www.facebook.com/groups/pokemonkortnorge/";

/** The watched group's URL name ("pokemonkortnorge"), from GROUP_FEED_URL. */
export const GROUP_SLUG = GROUP_FEED_URL.match(/\/groups\/([^/]+)/)![1];

/** A post's own address in its group, the one place a comment link (`?comment_id=`) works. */
export function groupPostUrl(postId: string, slug?: string | null): string {
  return `https://www.facebook.com/groups/${slug || GROUP_SLUG}/posts/${postId}/`;
}

/**
 * The address to link a stored post by: the group post's own address. A post read while Facebook
 * showed it in the photo viewer was saved with that page's `photo.php?fbid=…` address, so its lot
 * links went to the photo (2026-10-05); anything that isn't the post's own address is rebuilt.
 */
export function canonicalPostUrl(url: string, postId: string, slug?: string | null): string {
  if (!/^\d+$/.test(postId)) return url; // Not a group post ID: nothing to rebuild from.
  return new RegExp(`^https://www\\.facebook\\.com/groups/[^/?#]+/(?:posts|permalink)/${postId}/?$`).test(url) ? url : groupPostUrl(postId, slug);
}

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

/** A lot's own comment on Facebook (where you'd bid), or the post when the lot has no comment ID. */
export function lotUrl(post: { url: string }, lot: { commentId: string | null }): string {
  if (!lot.commentId) return post.url;
  return `${post.url}${post.url.includes("?") ? "&" : "?"}comment_id=${lot.commentId}`;
}

/**
 * A single Facebook post (what "Scan Post" reads): a group post or permalink, a profile/page
 * post, or the old permalink.php / story.php links. Not the feed, a photo, or a profile.
 */
export function isPostUrl(url: string | undefined): boolean {
  if (!url) return false;
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    return false;
  }
  if (u.origin !== "https://www.facebook.com") return false;
  if (/^\/groups\/[^/]+\/(posts|permalink)\/[^/]+\/?$/.test(u.pathname)) return true;
  if (/^\/(?!groups\/)[^/]+\/posts\/[^/]+\/?$/.test(u.pathname)) return true;
  if (u.pathname === "/permalink.php" || u.pathname === "/story.php") return u.searchParams.has("story_fbid");
  return false;
}
