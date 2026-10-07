import { beforeEach, describe, expect, it, vi } from "vitest";
import { expandAll, findExpanders, pendingLoaders } from "../src/content/post/expand";
import { interpretLots } from "../src/domain/bids";
import { isCompleteRead } from "../src/domain/captures";
import { extractCapture, findPostRoot } from "../src/content/post/extract";
import fixture from "./fixtures/post-dialog.html?raw";

const FAST = { minDelayMs: 0, maxDelayMs: 0, idleRounds: 1 };

function root(): Element {
  const r = findPostRoot(document);
  if (!r) throw new Error("fixture has no post root");
  return r;
}

beforeEach(() => {
  document.body.innerHTML = fixture;
});

describe("findPostRoot", () => {
  it("finds the dialog holding the post", () => {
    expect(root().getAttribute("role")).toBe("dialog");
  });

  it("finds a post's dialog even when the post has no comments yet", () => {
    document.body.innerHTML = `<div role="main"><div role="feed"><div role="article">a feed comment</div></div></div>
      <div role="dialog"><div data-ad-rendering-role="story_message">AUKSJON ingen bud ennå</div></div>`;
    expect(findPostRoot(document)?.getAttribute("role")).toBe("dialog");
    const capture = extractCapture(findPostRoot(document)!, {
      pageUrl: "https://www.facebook.com/groups/123/posts/555/", pageLang: "nb", expandClicks: 0,
      expandStoppedBecause: "done", commentSortAction: "not-found",
    });
    expect(capture.comments).toEqual([]);
    expect(capture.post.text).toContain("AUKSJON");
  });

  it("returns null on a page with no open post", () => {
    document.body.innerHTML = "<div role='main'><div role='feed'></div></div>";
    expect(findPostRoot(document)).toBeNull();
  });
});

describe("expandAll", () => {
  it("clicks only expanders and See more, never reply/like/sort/text box", async () => {
    const clicked: string[] = [];
    document.querySelectorAll("[role='button'], [role='textbox']").forEach((el) => {
      el.addEventListener("click", () => clicked.push(el.textContent?.trim() || el.getAttribute("aria-label") || ""));
    });
    const result = await expandAll(root(), FAST);
    expect(result.stoppedBecause).toBe("done");
    expect(clicked.sort()).toEqual(["Se mer", "Vis 2 flere svar", "Vis flere kommentarer"]);
  });

  it("keeps going as new expanders appear, then stops", async () => {
    const more = document.getElementById("more-comments")!;
    more.addEventListener("click", () => {
      const next = document.createElement("div");
      next.setAttribute("role", "button");
      next.textContent = "Vis 1 svar";
      more.after(next);
      more.remove();
    });
    const progress = vi.fn();
    const result = await expandAll(root(), { ...FAST, onProgress: progress });
    expect(result.clicks).toBe(4);
    expect(progress).toHaveBeenCalledTimes(4);
  });

  it("never clicks an expander-looking label inside a real link (review L14)", () => {
    const link = document.createElement("a");
    link.href = "https://www.facebook.com/somewhere";
    const trap = document.createElement("div");
    trap.setAttribute("role", "button");
    trap.textContent = "Se mer";
    link.appendChild(trap);
    root().appendChild(link);
    expect(findExpanders(root())).not.toContain(trap);
  });

  it("never treats an expander-looking label inside a form as clickable", () => {
    const form = document.querySelector("form")!;
    const trap = document.createElement("div");
    trap.setAttribute("role", "button");
    trap.textContent = "Vis flere kommentarer";
    form.appendChild(trap);
    expect(findExpanders(root())).not.toContain(trap);
  });

  it("stops when aborted", async () => {
    const controller = new AbortController();
    controller.abort();
    const result = await expandAll(root(), { ...FAST, signal: controller.signal });
    expect(result).toEqual({ clicks: 0, scrolls: 0, stoppedBecause: "aborted" });
  });

  it("stops at maxClicks", async () => {
    const result = await expandAll(root(), { ...FAST, maxClicks: 1 });
    expect(result).toEqual({ clicks: 1, scrolls: 0, stoppedBecause: "max-clicks" });
  });

  it("scrolls to load more comments when no button is left (dialogs load on scroll)", async () => {
    // Fake Facebook: scrolling the last comment into view appends one more comment, twice.
    let batches = 2;
    const list = document.querySelector("ul")!;
    const proto = Element.prototype as Element & { scrollIntoView: (arg?: unknown) => void };
    const original = proto.scrollIntoView;
    proto.scrollIntoView = function (this: Element) {
      if (this.getAttribute("role") !== "article" || batches === 0) return;
      batches--;
      const li = document.createElement("li");
      li.innerHTML = `<div role="article" aria-label="Kommentar fra Ny Person for 1 minutt siden"><a href="https://www.facebook.com/groups/123/posts/555/?comment_id=9${batches}">1 min</a><div dir="auto">.</div></div>`;
      list.appendChild(li);
    };
    const clicked: string[] = [];
    document.querySelectorAll("[role='button']").forEach((el) => {
      el.addEventListener("click", () => clicked.push(el.textContent?.trim() ?? ""));
    });
    try {
      const result = await expandAll(root(), { ...FAST, idleRounds: 2 });
      expect(result.scrolls).toBe(2);
      expect(result.stoppedBecause).toBe("done");
      expect(clicked.sort()).toEqual(["Se mer", "Vis 2 flere svar", "Vis flere kommentarer"]);
    } finally {
      proto.scrollIntoView = original;
    }
  });
});

// The shape of a real claim-sale read (2026-10-07; names and wording invented): a dialog with
// 121 comments showed the first 10, all "." from people following the sale, then three of
// Facebook's "Laster inn…" placeholders. No expander, and scrolling the last "." comment loaded
// nothing, so the read stopped "done" at 10 with every lot comment from the seller missing.
const CLAIM_DIALOG = (followers: number) => `
<div role="dialog">
  <h2><span>Selger Testesen sitt innlegg</span></h2>
  <div role="article">
    <a href="https://www.facebook.com/groups/123/user/900/">Selger Testesen</a>
    <div data-ad-preview="message"><div dir="auto">Claim salg-annonse
Fastpris: Blir oppgitt over hvert bilde i kommentarfeltet
Sluttid (maks 24 timer): 7 Oktober kl19:00</div></div>
    <div role="button">Alle kommentarer</div>
    <ul id="comments">${Array.from({ length: followers }, (_, i) => `
      <li><div role="article" aria-label="Kommentar fra Følger ${i + 1} for én dag siden">
        <a href="https://www.facebook.com/groups/123/user/${950 + i}/">Følger ${i + 1}</a>
        <div dir="auto">.</div>
        <a href="https://www.facebook.com/groups/123/posts/777/?comment_id=${200 + i}">1 d</a>
        <div role="button">Liker</div><div role="button">Svar</div>
      </div></li>`).join("")}
    </ul>
    <div id="loaders">
      <div aria-label="Laster inn …" role="status" data-visualcompletion="loading-state" tabindex="-1"><div></div></div>
      <div aria-label="Laster inn …" role="status" data-visualcompletion="loading-state" tabindex="-1"><div></div></div>
    </div>
  </div>
  <form><div role="textbox" contenteditable="true" aria-label="Kommenter som Meg"></div></form>
</div>`;

const LOT = (n: number) => `
  <div role="article" aria-label="Kommentar fra Selger Testesen for én dag siden">
    <a href="https://www.facebook.com/groups/123/user/900/">Selger Testesen</a>
    <div dir="auto">Fastpris ${n}00kr</div>
    <a href="https://www.facebook.com/photo/?fbid=30${n}"><img src="https://scontent.example/claim${n}.jpg" alt="" width="300" height="420"></a>
    <a href="https://www.facebook.com/groups/123/posts/777/?comment_id=${300 + n}">1 d</a>
  </div>`;

describe("expandAll: comments still loading (real claim-sale shape)", () => {
  type ScrollProto = Element & { scrollIntoView: (arg?: unknown) => void };
  const proto = Element.prototype as ScrollProto;
  let original: ScrollProto["scrollIntoView"];
  beforeEach(() => {
    original = proto.scrollIntoView;
  });
  const restore = () => {
    proto.scrollIntoView = original;
  };
  const capture = (stoppedBecause: string) =>
    extractCapture(findPostRoot(document)!, {
      pageUrl: "https://www.facebook.com/groups/123/posts/777/", pageLang: "nb", expandClicks: 0,
      expandStoppedBecause: stoppedBecause, commentSortAction: "already-all",
    });

  it("sees the placeholders below the last comment as pending, not ones inside or above it", () => {
    document.body.innerHTML = CLAIM_DIALOG(10);
    expect(pendingLoaders(findPostRoot(document)!)).toHaveLength(2);
    document.getElementById("loaders")!.remove();
    // A loading image inside a comment, or a glimmer above the comments, isn't a comment batch.
    const first = document.querySelector("#comments [role='article']")!;
    first.insertAdjacentHTML("beforeend", `<div role="status" data-visualcompletion="loading-state"></div>`);
    document.getElementById("comments")!.insertAdjacentHTML("beforebegin", `<div role="status" data-visualcompletion="loading-state"></div>`);
    expect(pendingLoaders(findPostRoot(document)!)).toHaveLength(0);
  });

  it("scrolls the loading placeholder into view and reads the lots it loads", async () => {
    document.body.innerHTML = CLAIM_DIALOG(10);
    // Fake Facebook: only the placeholder coming into view loads the next batch (the seller's
    // lots); scrolling the last comment alone loads nothing, as in the real read.
    let batches = 2;
    proto.scrollIntoView = function (this: Element) {
      if (this.getAttribute("data-visualcompletion") !== "loading-state" || batches === 0) return;
      batches--;
      const list = document.getElementById("comments")!;
      list.insertAdjacentHTML("beforeend", `<li>${LOT(2 - batches)}</li>`);
      if (batches === 0) document.getElementById("loaders")!.remove();
    };
    try {
      const result = await expandAll(findPostRoot(document)!, { ...FAST, idleRounds: 2 });
      expect(result).toMatchObject({ scrolls: 2, stoppedBecause: "done" });
      const read = capture(result.stoppedBecause);
      expect(read.comments).toHaveLength(12);
      const lots = interpretLots(read, { myName: "Meg", claims: true, listingIncrement: null, listingMinPrice: null });
      expect(lots.map((l) => l.startBid)).toEqual([100, 200]);
    } finally {
      restore();
    }
  });

  it("gives up as still-loading (not done) when the placeholders never resolve", async () => {
    document.body.innerHTML = CLAIM_DIALOG(10);
    proto.scrollIntoView = () => {};
    try {
      const result = await expandAll(findPostRoot(document)!, { ...FAST, idleRounds: 1, loadingRounds: 3 });
      expect(result.stoppedBecause).toBe("still-loading");
      const read = capture(result.stoppedBecause);
      expect(read.warnings.join("\n")).toMatch(/still-loading/);
      // A partial read: it can't settle "Won"/"Lost" or replace what an earlier read saw.
      expect(isCompleteRead(read, null)).toBe(false);
    } finally {
      restore();
    }
  });

  it("waits longer while loading, then stops done once Facebook has loaded everything", async () => {
    document.body.innerHTML = CLAIM_DIALOG(10);
    // Facebook is slow: the placeholders go away (nothing more to load) after a few scrolls.
    let calls = 0;
    proto.scrollIntoView = function (this: Element) {
      if (++calls === 6) document.getElementById("loaders")?.remove();
    };
    try {
      const result = await expandAll(findPostRoot(document)!, { ...FAST, idleRounds: 1, loadingRounds: 10 });
      expect(result.stoppedBecause).toBe("done");
      expect(calls).toBeGreaterThanOrEqual(6);
    } finally {
      restore();
    }
  });
});

describe("extractCapture", () => {
  const capture = () =>
    extractCapture(root(), {
      pageUrl: "https://www.facebook.com/groups/123/posts/555/",
      pageLang: "nb",
      expandClicks: 2,
      expandStoppedBecause: "done",
      commentSortAction: "failed",
      now: new Date("2026-10-04T10:00:00Z"),
    });

  it("reads the post", () => {
    const { post } = capture();
    expect(post.author).toBe("Selger Testesen");
    expect(post.text).toBe("Auksjon! Slutter søndag kl 20:00.\nSoft close 5 min. Minstebud 50 kr, budøkning 10 kr.");
    expect(post.timeText).toBe("2 t");
    expect(post.images.map((i) => i.src)).toEqual(["https://scontent.example/overview1.jpg"]);
  });

  it("takes the post author from the member profile link, not a group-name heading", () => {
    // Real group posts: the h3 heading links to the group, the poster is a /groups/<id>/user/<id>/ link.
    const heading = root().querySelector("h2")!;
    heading.outerHTML =
      '<h3><a href="/groups/123/">Testgruppe - Kjøp/selg</a></h3>' +
      '<span><a href="/groups/123/user/900/">Selger Testesen</a></span>';
    expect(capture().post.author).toBe("Selger Testesen");
  });

  it("reads top-level comments as lot candidates (image) or chatter (no image)", () => {
    const { comments } = capture();
    expect(comments.map((c) => [c.id, c.author, c.hasImage])).toEqual([
      ["10", "Selger Testesen", true],
      ["20", "Selger Testesen", true],
      ["30", "Per Hansen", false],
    ]);
    expect(comments[0].text).toBe("Lot 1: Charizard ex 199/165");
    expect(comments[0].images[0].src).toBe("https://scontent.example/lot1.jpg");
    expect(comments[0].url).toBe("https://www.facebook.com/groups/123/posts/555/?comment_id=10");
    expect(comments[2].images).toEqual([]); // 16px emoji is not a photo
  });

  it("nests replies under their comment by comment_id", () => {
    const [lot1, lot2] = capture().comments;
    expect(lot1.replies.map((r) => [r.id, r.author, r.text, r.timeText])).toEqual([
      ["11", "Ola Nordmann", "250", "1 t"],
      ["12", "Kari Nordmann", "bud 300", "45 min"],
    ]);
    expect(lot2.replies).toEqual([]);
  });

  it("does not leak button labels into comment text", () => {
    const [lot1] = capture().comments;
    expect(lot1.text).not.toMatch(/Liker|Svar/);
    expect(lot1.replies[0].text).toBe("250");
  });

  it("flags text still cut off and a comment sort that is still filtering", () => {
    const c = capture();
    expect(c.comments[1].truncated).toBe(true);
    expect(c.commentSortLabel).toBe("Mest relevante");
    expect(c.warnings.join("\n")).toMatch(/Mest relevante/);
    expect(c.warnings.join("\n")).toMatch(/See more/);
  });

  it("reports stats", () => {
    expect(capture().stats).toEqual({
      expandClicks: 2,
      expandScrolls: 0,
      expandStoppedBecause: "done",
      topLevelComments: 3,
      commentsWithImage: 2,
      replies: 2,
      orphanReplies: 0,
    });
  });
});
