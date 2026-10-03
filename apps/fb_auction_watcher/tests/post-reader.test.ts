import { beforeEach, describe, expect, it, vi } from "vitest";
import { expandAll, findExpanders } from "../src/content/post/expand";
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
    expect(result).toEqual({ clicks: 0, stoppedBecause: "aborted" });
  });

  it("stops at maxClicks", async () => {
    const result = await expandAll(root(), { ...FAST, maxClicks: 1 });
    expect(result).toEqual({ clicks: 1, stoppedBecause: "max-clicks" });
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
      expandStoppedBecause: "done",
      topLevelComments: 3,
      commentsWithImage: 2,
      replies: 2,
      orphanReplies: 0,
    });
  });
});
