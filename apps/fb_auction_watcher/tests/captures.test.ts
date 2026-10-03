import { describe, expect, it } from "vitest";
import { interpretLots } from "../src/domain/bids";
import { isCompleteRead, mergeCaptures } from "../src/domain/captures";
import type { CapturedComment, CapturedReply, PostCapture } from "../src/shared/capture";

// Merging reads of the same post (review H2). Invented names.
const SELLER = "Selger Testesen";
const ME = "Erik Johansen";
const reply = (id: number, author: string, text: string): CapturedReply => ({
  id: String(3308000000000000 + id), url: null, author, text, timeText: "1 t",
  ariaLabel: `Svar fra ${author} på ${SELLER} sin kommentar`, images: [], truncated: false, rawText: text,
});
const lot = (id: number, replies: CapturedReply[], text = "Lot\nMp 10kr"): CapturedComment => ({
  id: String(9000 + id), url: null, author: SELLER, text, timeText: "1 d", ariaLabel: `Kommentar fra ${SELLER}`,
  images: [{ src: `lot${id}.jpg`, alt: "" }], truncated: false, rawText: text, index: 0, hasImage: true, replies,
});
const read = (at: string, comments: CapturedComment[], over: Partial<PostCapture> = {}): PostCapture => ({
  schemaVersion: 1, capturedAt: at, pageUrl: "https://www.facebook.com/groups/g/posts/555/", pageLang: "nb",
  commentSortLabel: "Alle kommentarer", commentSortAction: "already-all",
  post: { url: "", author: SELLER, text: "AUKSJON\nMinimum budøkning: 10kr", timeText: null, images: [], truncated: false },
  comments, warnings: [],
  stats: { expandClicks: 0, expandScrolls: 0, expandStoppedBecause: "done", topLevelComments: comments.length, commentsWithImage: comments.length, replies: 0, orphanReplies: 0 },
  ...over,
});
const bids = (from: number, to: number, author: (i: number) => string) =>
  Array.from({ length: to - from + 1 }, (_, k) => reply(from + k, author(from + k), `${SELLER} ${(from + k) * 10}`));

describe("mergeCaptures", () => {
  // A full read: 10 bids, the last (100) by someone else. Then a partial background read sees
  // only the first 6 (you at 60 = highest of those).
  const full = read("2026-10-04T12:00:00Z", [lot(1, bids(1, 10, (i) => (i === 6 ? ME : `Bidder ${i}`)))]);
  const partial = read("2026-10-04T12:15:00Z", [lot(1, bids(1, 6, (i) => (i === 6 ? ME : `Bidder ${i}`)))]);

  it("a partial read doesn't drop replies an earlier read saw: you stay Outbid, not Leading", () => {
    const merged = mergeCaptures(mergeCaptures(null, full), partial);
    expect(merged.comments[0].replies).toHaveLength(10);
    const [l] = interpretLots(merged, { myName: ME, listingIncrement: 10, listingMinPrice: null });
    expect(l).toMatchObject({ highestBid: 100, myStatus: "outbid" });
    // Replacing instead of merging would have said Leading:
    expect(interpretLots(partial, { myName: ME, listingIncrement: 10, listingMinPrice: null })[0].myStatus).toBe("lead");
  });

  it("adds new replies and new lots, keeps lots only an earlier read saw, newest text wins", () => {
    const first = read("t1", [lot(1, [reply(1, "A", "Selger Testesen 10")]), lot(2, [])]);
    const second = read("t2", [lot(1, [reply(1, "A", "Selger Testesen 15"), reply(2, ME, "Selger Testesen 30")]), lot(3, [])]);
    const merged = mergeCaptures(mergeCaptures(null, first), second);
    expect(merged.comments.map((c) => c.id)).toEqual(["9001", "9002", "9003"]);
    expect(merged.comments[0].replies.map((r) => r.text)).toEqual(["Selger Testesen 15", "Selger Testesen 30"]);
    expect(merged.stats).toMatchObject({ topLevelComments: 3, replies: 2 });
    expect(merged.reads).toBe(2);
  });

  it("keeps a lot's photo when a later read caught it before the image loaded", () => {
    const noPhoto = { ...lot(1, []), images: [], hasImage: false };
    const merged = mergeCaptures(mergeCaptures(null, read("t1", [lot(1, [])])), read("t2", [noPhoto]));
    expect(merged.comments[0]).toMatchObject({ hasImage: true, images: [{ src: "lot1.jpg", alt: "" }] });
  });

  it("completeAt: only complete reads count (for Won / Lost)", () => {
    const first = mergeCaptures(null, full);
    expect(first.completeAt).toBe("2026-10-04T12:00:00Z");
    // Fewer replies than before: partial, completeAt stays.
    expect(mergeCaptures(first, partial)).toMatchObject({ completeAt: "2026-10-04T12:00:00Z", capturedAt: "2026-10-04T12:15:00Z" });
    // Sort switch failed ("Most relevant" hides comments): partial.
    const badSort = read("2026-10-04T12:30:00Z", full.comments, { commentSortAction: "failed" });
    expect(mergeCaptures(first, badSort).completeAt).toBe("2026-10-04T12:00:00Z");
    // Stopped by the time limit: partial.
    const capped = read("2026-10-04T12:30:00Z", full.comments, { stats: { ...full.stats, expandStoppedBecause: "aborted" } });
    expect(isCompleteRead(capped, first)).toBe(false);
    // A full read again: complete.
    const again = read("2026-10-04T12:45:00Z", full.comments);
    expect(mergeCaptures(first, again).completeAt).toBe("2026-10-04T12:45:00Z");
  });
});
