import { describe, expect, it } from "vitest";
import type { Lot } from "../src/domain/bids";
import { interpretListing } from "../src/domain/listing";
import { wonBySeller, type Row } from "../src/pages/dashboard/model";

// "What I won and what I owe": everything won, grouped by seller (invented names).
const END = Date.parse("2026-10-03T16:00:00Z");
const auction = (seller: string, lots: Partial<Lot>[], over: Partial<Row> = {}): Row =>
  ({
    id: seller, type: "auction", sellerName: seller, ended: true, endsAtMs: END, softCloseMinutes: 5,
    lastSeenAt: "2026-10-03T10:00:00Z", lastReadAt: "2026-10-03T17:00:00Z", lastCompleteReadAt: "2026-10-03T17:00:00Z",
    lots: lots.map((l, i) => ({ position: i + 1, title: `Lot ${i + 1}`, myStatus: "none", myClaim: "none", claimCards: null, claims: [], highestBid: null, ...l })),
    ...over,
  }) as Row;

describe("wonBySeller", () => {
  it("collects won auction lots per seller, priced at the winning bid; lost and other lots left out", () => {
    const rows = [auction("Selger A", [{ myStatus: "lead", highestBid: 20 }, { myStatus: "outbid", highestBid: 40 }, { myStatus: "lead", highestBid: 10 }])];
    const [a] = wonBySeller(rows);
    expect(a).toMatchObject({ seller: "Selger A", kr: 30, unknown: 0 });
    expect(a.items.map((i) => [i.label, i.kr])).toEqual([["1. Lot 1", 20], ["3. Lot 3", 10]]);
  });

  it("an ended auction not read completely after the end isn't won yet", () => {
    const rows = [auction("Selger A", [{ myStatus: "lead", highestBid: 20 }], { lastCompleteReadAt: "2026-10-03T15:30:00Z" })];
    expect(wonBySeller(rows)).toEqual([]);
  });

  it("claims: the cards you got with Claude's prices, or '?' until it has read the photo", () => {
    const read = auction("Selger B", [
      { myClaim: "claimed", claimCards: [
        { card: "Marowak", price: 200, claimedBy: "Me", isMe: true },
        { card: "Kingler", price: 250, claimedBy: "X", isMe: false },
        { card: "Feraligatr", price: 200, claimedBy: "Me", isMe: true },
      ] },
    ], { type: "fixed", ended: false, endsAtMs: null });
    const notRead = auction("Selger C", [
      { myClaim: "claimed", claims: [{ isMe: true, all: false, items: ["pidgeot"] } as never] },
    ], { type: "claim", ended: false });
    const groups = wonBySeller([read, notRead]);
    const b = groups.find((g) => g.seller === "Selger B")!;
    expect(b.items.map((i) => [i.label, i.kr])).toEqual([["1. Marowak, Feraligatr", 400]]);
    const c = groups.find((g) => g.seller === "Selger C")!;
    expect(c).toMatchObject({ kr: 0, unknown: 1 });
    expect(c.items[0].label).toBe("1. pidgeot");
  });

  it("puts the seller with the most recent sale first, and merges a seller's sales", () => {
    const older = auction("Selger A", [{ myStatus: "lead", highestBid: 5 }], { id: "1", endsAtMs: END - 86_400_000 });
    const newer = auction("Selger B", [{ myStatus: "lead", highestBid: 7 }], { id: "2" });
    const again = auction("Selger A", [{ myStatus: "lead", highestBid: 3 }], { id: "3", endsAtMs: END - 3_600_000 });
    const groups = wonBySeller([older, newer, again]);
    expect(groups.map((g) => [g.seller, g.kr, g.rows.length])).toEqual([["Selger B", 7, 1], ["Selger A", 8, 2]]);
  });
});

describe("shipping and payment lines", () => {
  it("reads them as written", () => {
    const i = interpretListing("AUKSJON\nSender med post (pris m/emballasje): Kjøper betaler frakt\nBetalingsalternativ: Vipps/Bankoverføring", new Date());
    expect(i).toMatchObject({ shippingText: "Kjøper betaler frakt", paymentText: "Vipps/Bankoverføring" });
  });
});
