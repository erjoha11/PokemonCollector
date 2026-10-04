import { describe, expect, it } from "vitest";
import type { Lot } from "../src/domain/bids";
import { saleResult, splitEnded, type Row } from "../src/pages/dashboard/model";

// The Ended group: your sales first, and what each sale went for. Invented data.
const END = Date.parse("2026-10-03T20:00:00Z");
const sale = (id: string, lots: Partial<Lot>[] | null, over: Partial<Row> = {}): Row =>
  ({
    id, type: "auction", ended: true, endsAtMs: END, softCloseMinutes: 5, endedByYouAt: null,
    lastCompleteReadAt: "2026-10-03T20:10:00Z",
    lots: lots?.map((l) => ({ myStatus: "none", myClaim: "none", claimCards: null, claims: [], highestBid: null, ...l })) ?? null,
    summary: lots ? { unclear: 0, claimed: 0, check: 0, ...summaryOf(lots) } : null,
    ...over,
  }) as Row;
const summaryOf = (lots: Partial<Lot>[]) => ({
  lead: lots.filter((l) => l.myStatus === "lead").length,
  outbid: lots.filter((l) => l.myStatus === "outbid").length,
});

describe("splitEnded", () => {
  it("puts sales you bid in first, keeping the order within each part", () => {
    const rows = [sale("A", [{ highestBid: 10 }]), sale("B", [{ myStatus: "outbid" }]), sale("C", null), sale("D", [{ myStatus: "lead" }])];
    const { yours, others } = splitEnded(rows);
    expect(yours.map((r) => r.id)).toEqual(["B", "D"]);
    expect(others.map((r) => r.id)).toEqual(["A", "C"]);
  });
});

describe("saleResult", () => {
  it("auction: lots with a valid bid sold, for the sum of the winning bids; final when read after the end", () => {
    const r = saleResult(sale("A", [{ highestBid: 1100 }, { highestBid: null }, { highestBid: 200 }]));
    expect(r).toMatchObject({ lots: 3, sold: 2, kr: 1300, unknownPrices: 0, final: true });
  });

  it("read only before the end: the same numbers, but not final", () => {
    const r = saleResult(sale("A", [{ highestBid: 50 }], { lastCompleteReadAt: "2026-10-03T19:30:00Z" }));
    expect(r).toMatchObject({ sold: 1, kr: 50, final: false, readAt: "2026-10-03T19:30:00Z" });
  });

  it("claim sale: claimed lots sold; prices from Claude's reading, unknown otherwise", () => {
    const r = saleResult(
      sale("C", [
        { claimCards: [{ card: "Pikachu", price: 50, claimedBy: "X", isMe: false }, { card: "Eevee", price: 20, claimedBy: null, isMe: false }], claims: [{} as never] },
        { claims: [{} as never] },
        {},
      ], { type: "claim" }),
    );
    expect(r).toMatchObject({ lots: 3, sold: 2, kr: 50, unknownPrices: 1 });
  });

  it("never read: no result", () => {
    expect(saleResult(sale("N", null))).toBeNull();
  });
});
