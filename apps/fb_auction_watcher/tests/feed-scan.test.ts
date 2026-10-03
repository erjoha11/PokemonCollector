import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { recordFeed } from "../src/content/feed/recorder";
import { findSaleSeeMore, isSaleText, scanFeed } from "../src/content/feed/scan";

// Synthetic feed (invented names), shaped like the real one: posts are direct children of the
// feed with data-ad-rendering-role parts; the post text is cut at "Se mer"; a preview comment
// (role="article") sits under each post with its own "Se mer", "Liker" and "Svar".

const post = (id: string, headline: string) =>
  `<div data-virtualized="false">
     <div data-ad-rendering-role="profile_name"><a href="/groups/123/user/9${id}/">Selger ${id}</a></div>
     <div data-ad-rendering-role="story_message"><div dir="auto">${headline} Minstepris: 50 …</div><div role="button" class="sm">Se mer</div></div>
     <a href="https://www.facebook.com/photo/?fbid=5${id}&set=pcb.${id}"><img src="x.jpg" width="500"></a>
     <div role="article" aria-label="Kommentar fra Noen">
       <div dir="auto">Lang kommentar …</div><div role="button">Se mer</div>
       <div role="button">Liker</div><div role="button">Svar</div>
     </div>
   </div>`;

const FAST = { minDelayMs: 0, maxDelayMs: 0 };

let feed: HTMLElement;
let clicked: string[];

function trackClicks() {
  document.querySelectorAll("[role='button']").forEach((el) => {
    if ((el as HTMLElement).dataset.tracked) return;
    (el as HTMLElement).dataset.tracked = "1";
    el.addEventListener("click", () => {
      const p = el.closest("[data-virtualized]")?.querySelector("a[href*='set=']")?.getAttribute("href");
      clicked.push(`${el.textContent?.trim()}@${p?.match(/pcb\.(\d+)/)?.[1]}`);
      // "Se mer" on a post's text expands it in place, revealing the end time.
      if (el.classList.contains("sm")) {
        el.parentElement!.querySelector("[dir='auto']")!.textContent += " Sluttid: 04.10 kl 22:00";
        el.remove();
      }
    });
  });
}

beforeEach(() => {
  document.body.innerHTML = `<div role="feed">${post("1", "AUKSJON/BUDRUNDE-annonse")}${post("2", "FASTPRIS-annonse")}</div>`;
  feed = document.querySelector("[role='feed']")!;
  clicked = [];
  trackClicks();
});
afterEach(() => {
  document.body.innerHTML = "";
});

describe("isSaleText", () => {
  it.each(["AUKSJON/BUDRUNDE-annonse", "LYNAUKSJON", "Claim salg-annonse", "SØTE FAIRY KORT CLAIM SALG"])("%s is a sale", (t) =>
    expect(isSaleText(t)).toBe(true),
  );
  it.each(["FASTPRIS-annonse", "ØNSKES KJØPT-annonse", "BYTTE-annonse"])("%s is not", (t) => expect(isSaleText(t)).toBe(false));
});

describe("findSaleSeeMore", () => {
  it("finds only the post-text 'Se mer' of sale posts, never a comment's", () => {
    const found = findSaleSeeMore(feed);
    expect(found).toHaveLength(1);
    expect(found[0].closest("[data-ad-rendering-role='story_message']")).not.toBeNull();
  });
});

describe("scanFeed", () => {
  it("expands sale posts, scrolls for more, and stops at the end of the feed", async () => {
    let next = 3;
    const scrollStep = () => {
      if (next > 4) return; // End of the feed: scrolling loads nothing new.
      feed.insertAdjacentHTML("beforeend", post(String(next), next === 3 ? "Claim salg-annonse" : "BYTTE-annonse"));
      next++;
      trackClicks();
    };
    const rec = recordFeed(feed, () => {});
    const result = await scanFeed(feed, rec, { ...FAST, idleRounds: 2, scrollStep });
    rec.stop();

    expect(result.stoppedBecause).toBe("end-of-feed");
    expect(result.posts).toBe(4);
    // Only "Se mer" on the auction (1) and the claim sale (3): not fixed price, trade, or comments.
    expect(clicked).toEqual(["Se mer@1", "Se mer@3"]);
    const auction = rec.posts().find((p) => p.key === "post:1")!;
    expect(auction.text).toContain("Sluttid: 04.10 kl 22:00");
  });

  it("stops when aborted", async () => {
    const controller = new AbortController();
    controller.abort();
    const rec = recordFeed(feed, () => {});
    const result = await scanFeed(feed, rec, { ...FAST, signal: controller.signal, scrollStep: () => {} });
    rec.stop();
    expect(result.stoppedBecause).toBe("aborted");
    expect(clicked).toEqual([]);
  });

  it("stops at maxPosts", async () => {
    const rec = recordFeed(feed, () => {});
    const result = await scanFeed(feed, rec, { ...FAST, maxPosts: 2, scrollStep: () => {} });
    rec.stop();
    expect(result.stoppedBecause).toBe("max-posts");
  });

  it("stops if 'Se mer' opens a dialog instead of expanding", async () => {
    const seeMore = feed.querySelector(".sm")!;
    seeMore.addEventListener("click", () => document.body.insertAdjacentHTML("beforeend", "<div role='dialog'></div>"));
    const rec = recordFeed(feed, () => {});
    const result = await scanFeed(feed, rec, { ...FAST, scrollStep: () => {} });
    rec.stop();
    expect(result.stoppedBecause).toBe("dialog-opened");
  });
});
