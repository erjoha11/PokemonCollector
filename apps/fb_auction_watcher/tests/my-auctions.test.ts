import { describe, expect, it } from "vitest";
import type { Lot } from "../src/domain/bids";
import { leadingBySale, lotUrl, needsYou, type Row } from "../src/pages/dashboard/model";

// My Auctions, redesigned: what needs you (across sales, soonest first), what you're leading,
// and links to each lot. Invented data.
const now = Date.parse("2026-10-04T12:00:00Z");
const sale = (id: string, endsInMin: number, lots: Partial<Lot>[], over: Partial<Row> = {}): Row =>
  ({
    id, url: `https://www.facebook.com/groups/g/posts/${id}/`, type: "auction", sellerName: `Selger ${id}`,
    ended: false, endsAtMs: now + endsInMin * 60_000, softCloseMinutes: 5,
    lastReadAt: "2026-10-04T11:50:00Z", lastCompleteReadAt: "2026-10-04T11:50:00Z",
    lots: lots.map((l, i) => ({ position: i + 1, title: `Lot ${i + 1}`, commentId: `${id}${i}`, myStatus: "none", myClaim: "none",
      claimCards: null, claims: [], highestBid: null, myHighestBid: null, startBid: null, increment: 10, ...l })),
    ...over,
  }) as Row;

describe("needsYou", () => {
  const rows = [
    sale("A", 120, [{ myStatus: "outbid", highestBid: 180, myHighestBid: 160 }, { myStatus: "lead", myHighestBid: 50 }]),
    sale("B", 15, [{ myStatus: "unclear", highestBid: 100, myHighestBid: 100, increment: 5 }]),
    sale("C", 30, [{ myClaim: "check" }], { type: "claim" }),
    sale("D", -60, [{ myStatus: "outbid", highestBid: 40 }], { ended: true }), // Ended: nothing to do.
  ];

  it("lists outbid, unclear and contested claims across sales, soonest ending first", () => {
    expect(needsYou(rows).map((i) => [i.row.id, i.status.key])).toEqual([["B", "unclear"], ["C", "check"], ["A", "outbid"]]);
  });

  it("gives the next valid bid: highest + minimum raise (none for claims)", () => {
    expect(needsYou(rows).map((i) => i.nextBid)).toEqual([105, null, 190]);
  });

  it("with no valid bid yet, the next valid bid is the start bid", () => {
    const [i] = needsYou([sale("E", 10, [{ myStatus: "outbid", highestBid: null, startBid: 700 }])]);
    expect(i.nextBid).toBe(700);
  });
});

describe("a sale you marked as ended", () => {
  it("leaves Needs you and Leading: its leads count as won from the last full read", () => {
    const marked = { ended: true, endedByYouAt: "2026-10-04T11:55:00Z" };
    const rows = [sale("M", 600, [{ myStatus: "outbid", highestBid: 50 }, { myStatus: "lead", myHighestBid: 80 }], marked)];
    expect(needsYou(rows)).toEqual([]);
    expect(leadingBySale(rows)).toEqual([]);
  });
});

describe("leadingBySale", () => {
  it("per sale, what you'd pay if your leads hold; an ended sale waits for its final read", () => {
    const rows = [
      sale("A", 120, [{ myStatus: "lead", myHighestBid: 280 }, { myStatus: "lead", myHighestBid: 200 }, { myStatus: "outbid" }]),
      sale("B", -10, [{ myStatus: "lead", myHighestBid: 30 }], { ended: true, lastCompleteReadAt: "2026-10-04T11:00:00Z" }),
    ];
    expect(leadingBySale(rows).map((s) => [s.row.id, s.lots.length, s.kr, s.awaitingFinalRead])).toEqual([["B", 1, 30, true], ["A", 2, 480, false]]);
  });
});

describe("lotUrl", () => {
  it("links to the lot's comment, or the post", () => {
    expect(lotUrl({ url: "https://www.facebook.com/groups/g/posts/1/" }, { commentId: "99" })).toBe("https://www.facebook.com/groups/g/posts/1/?comment_id=99");
    expect(lotUrl({ url: "https://www.facebook.com/groups/g/posts/1/" }, { commentId: null })).toBe("https://www.facebook.com/groups/g/posts/1/");
  });
});
