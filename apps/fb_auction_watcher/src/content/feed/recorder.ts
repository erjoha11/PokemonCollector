// Records group-feed posts as they render. Facebook virtualizes the feed: posts that scroll
// out of view are emptied to placeholders (data-virtualized="true"), so a one-off snapshot
// only ever holds the 2-3 posts near the screen. The recorder watches the feed and keeps a
// copy of each post while it has content (the latest rendering seen), while the feed scrolls.
// Read-only: it observes the DOM, never clicks or scrolls.

export type RecordedPost<T = unknown> = { key: string; html: string; text: string; data: T | null };

/** Marks the post's own parts (author, message, full text); comments under a post don't have it. */
const POST_PART = "[data-ad-rendering-role]";

/**
 * Posts currently rendered in the feed: direct children of the feed that hold a post's own
 * parts. In the feed the post itself has no role="article"; only the preview comments under
 * it do, so articles can't be used to find posts. Emptied (virtualized) posts are skipped.
 */
export function feedPosts(feed: Element): Element[] {
  return Array.from(feed.children).filter((c) => c.querySelector(POST_PART));
}

/** The post's ID: from a /posts/<id> link (comment permalinks), else a photo link's set=gm.<id> (one photo) or set=pcb.<id> (several). */
export function postId(post: Element): string | null {
  for (const a of Array.from(post.querySelectorAll("a[href]"))) {
    const href = a.getAttribute("href")!;
    const id = href.match(/\/(?:posts|permalink)\/(\d+)/)?.[1] ?? href.match(/[?&]set=(?:gm|pcb)\.(\d+)/)?.[1];
    if (id) return id;
  }
  return null;
}

const squash = (s: string | null | undefined) => (s ?? "").replace(/\s+/g, " ").trim();

/** A stable key for a post: its ID if found, else author + start of the full text. */
export function postKey(post: Element): string {
  const id = postId(post);
  if (id) return `post:${id}`;
  const author = squash(post.querySelector("[data-ad-rendering-role='profile_name']")?.textContent);
  const text = squash(post.querySelector("[data-ad-rendering-role='description']")?.textContent);
  return `text:${author}|${text.slice(0, 120)}`;
}

export type FeedRecorder<T = unknown> = {
  posts(): RecordedPost<T>[];
  /** Posts recorded or updated since the last call. */
  takeChanged(): RecordedPost<T>[];
  /** Record what's rendered right now, without waiting for the observer's batching. */
  flush(): void;
  stop(): void;
};

export function recordFeed<T = unknown>(
  feed: Element,
  onChange: (count: number) => void,
  /** Reads a post into structured data while it's rendered (e.g. extractFeedPost). */
  extract: (post: Element) => T | null = () => null,
): FeedRecorder<T> {
  const byKey = new Map<string, RecordedPost<T>>();
  const changedKeys = new Set<string>();

  const scan = () => {
    let changed = false;
    for (const post of feedPosts(feed)) {
      const text = squash(post.textContent);
      const key = postKey(post);
      const prev = byKey.get(key);
      // Keep the latest rendering: posts fill in progressively, and "See more" expands the
      // text (while removing the button, so it isn't necessarily longer). Emptied posts never
      // get here, since feedPosts skips them.
      if (prev && prev.html === post.outerHTML) continue;
      byKey.set(key, { key, html: post.outerHTML, text, data: extract(post) });
      changedKeys.add(key);
      changed = true;
    }
    if (changed) onChange(byKey.size);
  };

  let pending = false;
  const observer = new MutationObserver(() => {
    if (pending) return;
    pending = true;
    // Batch bursts of mutations; Facebook renders a post in many small steps.
    setTimeout(() => {
      pending = false;
      scan();
    }, 300);
  });
  observer.observe(feed, { childList: true, subtree: true, characterData: true });
  scan();

  return {
    posts: () => Array.from(byKey.values()),
    takeChanged: () => {
      const out = Array.from(changedKeys, (k) => byKey.get(k)!);
      changedKeys.clear();
      return out;
    },
    flush: scan,
    stop: () => observer.disconnect(),
  };
}

/** One HTML file holding every recorded post, each in its own <section>. */
export function feedSampleHtml(pageUrl: string, posts: RecordedPost[]): string {
  const sections = posts.map(
    (p) => `<section data-fbaw-key="${p.key.replace(/"/g, "&quot;")}">\n${p.html}\n</section>`,
  );
  return `<!doctype html>\n<!-- ${pageUrl} -->\n<!-- fbaw feed sample: ${posts.length} posts -->\n<div role="feed">\n${sections.join("\n")}\n</div>\n`;
}
