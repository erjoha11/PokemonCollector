import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { showStatusPill } from "../src/content/post/pill";
import type { PostCapture } from "../src/shared/capture";

// The quiet read's status overlay: progress line, then a green "Saved" with lots and your
// status, or a red error that stays. Invented names.
const pill = () => document.getElementById("fbaw-pill-host")!.shadowRoot!;

beforeEach(() => {
  vi.stubGlobal("chrome", { storage: { local: { get: async () => ({ settings: { myName: "Erik Johansen" } }) } } });
});
afterEach(() => {
  document.documentElement.querySelectorAll("#fbaw-pill-host").forEach((e) => e.remove());
  vi.unstubAllGlobals();
});

const capture: PostCapture = {
  schemaVersion: 1, capturedAt: "2026-10-03T13:00:00Z", pageUrl: "https://www.facebook.com/groups/g/posts/1/", pageLang: "nb",
  commentSortLabel: "Alle kommentarer", commentSortAction: "already-all",
  post: { url: "", author: "Selger", text: "AUKSJON\nMinimum budøkning: 5kr", timeText: null, images: [], truncated: false },
  comments: [{
    id: "10", url: null, author: "Selger", text: "Pop serie 2\nMp 50kr", timeText: "1 t", ariaLabel: "Kommentar fra Selger",
    images: [{ src: "x.jpg", alt: "" }], truncated: false, rawText: "", index: 0, hasImage: true,
    replies: [{ id: "11", url: null, author: "Erik Johansen", text: "Selger 60", timeText: "1 t", ariaLabel: "Svar fra Erik Johansen på Selger sin kommentar", images: [], truncated: false, rawText: "" }],
  }],
  stats: { expandClicks: 1, expandScrolls: 0, expandStoppedBecause: "done", topLevelComments: 1, commentsWithImage: 1, replies: 1, orphanReplies: 0 },
  warnings: [],
};

describe("status overlay", () => {
  it("shows progress, then a green 'Saved' with your status", async () => {
    const p = showStatusPill();
    p.setStatus("Loading comments and bids… 3 steps");
    expect(pill().querySelector(".line")!.textContent).toBe("Loading comments and bids… 3 steps");
    p.showResult(capture, "");
    await new Promise((r) => setTimeout(r, 0));
    expect(pill().querySelector(".pill")!.classList.contains("done")).toBe(true);
    expect(pill().querySelector(".line")!.textContent).toBe("Saved · 1 lot · 1 bid · Leading 1");
    expect(pill().querySelector(".stop")).toBeNull();
  });

  it("stops the read when you press Stop", () => {
    const p = showStatusPill();
    const stop = vi.fn();
    p.onStop(stop);
    (pill().querySelector(".stop") as HTMLButtonElement).click();
    expect(stop).toHaveBeenCalled();
  });

  it("turns red on an error and stays", () => {
    const p = showStatusPill();
    p.showError("Failed: the post didn't load");
    expect(pill().querySelector(".pill")!.classList.contains("error")).toBe(true);
    expect(pill().querySelector(".line")!.textContent).toBe("Failed: the post didn't load");
  });
});
