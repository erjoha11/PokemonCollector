import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { feedPosts, feedSampleHtml, postKey, recordFeed } from "../src/content/feed/recorder";

// Synthetic feed: invented names. Mimics the virtualization seen on real Facebook, where a
// post scrolled out of view is replaced by an empty data-virtualized="true" placeholder.

const post = (id: string, text: string) =>
  `<div data-virtualized="false"><div role="article">
     <a href="/groups/123/user/9${id}/">Selger ${id}</a>
     <a href="https://www.facebook.com/groups/123/posts/${id}/">5 min</a>
     <div dir="auto">${text}</div>
     <div role="article" aria-label="Kommentar fra Noen"><div dir="auto">.</div></div>
   </div></div>`;
const placeholder = `<div data-virtualized="true" style="min-height: 900px"><div hidden></div></div>`;

const flush = () => new Promise((r) => setTimeout(r, 350));

let feed: HTMLElement;
beforeEach(() => {
  document.body.innerHTML = `<div role="feed"><div class="slot1">${post("111", "AUKSJON Sluttid: 04.10 kl 22:00 Antisnipe 5 min: Ja")}</div><div class="slot2"></div></div>`;
  feed = document.querySelector("[role='feed']")!;
});
afterEach(() => {
  document.body.innerHTML = "";
});

describe("feed recorder", () => {
  it("counts only top-level posts, not comments inside them", () => {
    expect(feedPosts(feed)).toHaveLength(1);
  });

  it("keys a post by its numeric post ID", () => {
    expect(postKey(feedPosts(feed)[0])).toBe("post:111");
  });

  it("keeps posts after Facebook virtualizes them away", async () => {
    const counts: number[] = [];
    const rec = recordFeed(feed, (n) => counts.push(n));
    // Scroll: post 111 becomes a placeholder, post 222 renders.
    feed.querySelector(".slot1")!.innerHTML = placeholder;
    feed.querySelector(".slot2")!.innerHTML = post("222", "Claim salg-annonse Sluttid (maks 24 timer): 04.10.26 kl: 14:00");
    await flush();
    rec.stop();
    expect(rec.posts().map((p) => p.key)).toEqual(["post:111", "post:222"]);
    expect(counts.at(-1)).toBe(2);
    expect(rec.posts()[0].text).toContain("Sluttid: 04.10 kl 22:00");
  });

  it("writes one sample file with a section per post", () => {
    const rec = recordFeed(feed, () => {});
    rec.stop();
    const html = feedSampleHtml("https://www.facebook.com/groups/123", rec.posts());
    expect(html).toContain('<section data-fbaw-key="post:111">');
    expect(html).toContain("fbaw feed sample: 1 posts");
  });
});
