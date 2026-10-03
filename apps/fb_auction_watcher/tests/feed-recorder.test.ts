import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { feedPosts, feedSampleHtml, postKey, recordFeed } from "../src/content/feed/recorder";

// Synthetic feed: invented names. Mimics the virtualization seen on real Facebook, where a
// post scrolled out of view is replaced by an empty data-virtualized="true" placeholder.

// A feed post as Facebook renders it: no role="article" on the post itself, its parts marked
// with data-ad-rendering-role, the ID only in a photo link (set=gm.<id>), and one preview
// comment (role="article") underneath.
const post = (id: string, text: string) =>
  `<div data-virtualized="false">
     <div data-ad-rendering-role="profile_name"><a href="/groups/123/user/9${id}/">Selger ${id}</a></div>
     <div data-ad-rendering-role="story_message">${text.slice(0, 12)} … <div role="button">Se mer</div></div>
     <a href="https://www.facebook.com/photo/?fbid=5${id}&set=gm.${id}&idorvanity=123"><img src="x.jpg" width="500"></a>
     <span data-ad-rendering-role="description">${text}</span>
     <div role="article" aria-label="Kommentar fra Noen"><div dir="auto">.</div></div>
   </div>`;
const placeholder = `<div data-virtualized="true" style="min-height: 900px"><div hidden></div></div>`;

const flush = () => new Promise((r) => setTimeout(r, 350));

let feed: HTMLElement;
beforeEach(() => {
  document.body.innerHTML = `<div role="feed"><div class="slot1">${post("111", "AUKSJON Sluttid: 04.10 kl 22:00 Antisnipe 5 min: Ja")}</div><div class="slot2"></div><div class="loading"><div role="article">loading</div></div></div>`;
  feed = document.querySelector("[role='feed']")!;
});
afterEach(() => {
  document.body.innerHTML = "";
});

describe("feed recorder", () => {
  it("finds posts by their marked parts, not by articles (those are comments)", () => {
    expect(feedPosts(feed)).toHaveLength(1);
  });

  it("keys a post by the post ID in its photo link", () => {
    expect(postKey(feedPosts(feed)[0])).toBe("post:111");
  });

  it("keys a multi-photo post by set=pcb.<id>", () => {
    feed.querySelector(".slot1")!.innerHTML = post("333", "FASTPRIS-annonse").replace("set=gm.333", "set=pcb.333");
    expect(postKey(feedPosts(feed)[0])).toBe("post:333");
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
