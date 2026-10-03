// Records group-feed posts as they render. Facebook virtualizes the feed: posts that scroll
// out of view are emptied to placeholders (data-virtualized="true"), so a one-off snapshot
// only ever holds the 2-3 posts near the screen. The recorder watches the feed and keeps a
// copy of each post the first time it has content, while the user scrolls.
// Read-only: it observes the DOM, never clicks or scrolls.

const ARTICLE = "[role='article']";

export type RecordedPost = { key: string; html: string; text: string };

/** Top-level posts in the feed (articles not nested in another article, i.e. not comments). */
export function feedPosts(feed: Element): Element[] {
  return Array.from(feed.querySelectorAll(ARTICLE)).filter((a) => !a.parentElement?.closest(ARTICLE));
}

/** A stable key for a post: its numeric post ID if a link carries one, else author + start of text. */
export function postKey(post: Element): string {
  for (const a of Array.from(post.querySelectorAll("a[href]"))) {
    const id = a.getAttribute("href")!.match(/\/(?:posts|permalink)\/(\d+)/)?.[1];
    if (id) return `post:${id}`;
  }
  const text = (post.textContent ?? "").replace(/\s+/g, " ").trim();
  return `text:${text.slice(0, 160)}`;
}

export type FeedRecorder = {
  posts(): RecordedPost[];
  stop(): void;
};

export function recordFeed(feed: Element, onChange: (count: number) => void): FeedRecorder {
  const byKey = new Map<string, RecordedPost>();

  const scan = () => {
    let changed = false;
    for (const post of feedPosts(feed)) {
      const text = (post.textContent ?? "").replace(/\s+/g, " ").trim();
      if (text.length < 40) continue; // Placeholder or still loading.
      const key = postKey(post);
      const prev = byKey.get(key);
      // Keep the fullest version: posts fill in progressively (images, comment counts).
      if (prev && prev.html.length >= post.outerHTML.length) continue;
      byKey.set(key, { key, html: post.outerHTML, text });
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
