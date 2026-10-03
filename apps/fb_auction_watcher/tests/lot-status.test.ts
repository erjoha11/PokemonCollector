import { describe, expect, it } from "vitest";
import type { Lot } from "../src/domain/bids";
import { krText, lotStatus, readAfterEnd, wonTotal, type Row } from "../src/pages/dashboard/model";

// The overview's one place for "how does this lot show for you" (review H3, H4, M7).
const END = Date.parse("2026-10-04T16:00:00Z");
const row = (over: Partial<Row>): Row =>
  ({ type: "auction", ended: false, endsAtMs: END, softCloseMinutes: 5, lastReadAt: "2026-10-04T15:30:00Z", lastCompleteReadAt: "2026-10-04T15:30:00Z", ...over }) as Row;
const lot = (over: Partial<Lot>): Lot => ({ myStatus: "none", myClaim: "none", claimCards: null, ...over }) as Lot;

describe("lotStatus", () => {
  it("running auction: Leading, Leading? (unclear), Outbid", () => {
    expect(lotStatus(row({}), lot({ myStatus: "lead" }))).toMatchObject({ label: "Leading", cls: "lead" });
    expect(lotStatus(row({}), lot({ myStatus: "unclear" }))).toMatchObject({ label: "Leading?", cls: "outbid" });
    expect(lotStatus(row({}), lot({ myStatus: "outbid" }))).toMatchObject({ label: "Outbid", cls: "outbid" });
  });

  it("ended: Won / Lost only when read after the end plus antisnipe", () => {
    const readAfter = row({ ended: true, lastReadAt: "2026-10-04T16:06:00Z", lastCompleteReadAt: "2026-10-04T16:06:00Z" });
    expect(lotStatus(readAfter, lot({ myStatus: "lead" }))).toMatchObject({ label: "Won", cls: "won" });
    expect(lotStatus(readAfter, lot({ myStatus: "outbid" }))).toMatchObject({ label: "Lost" });
    // Read before the end (or inside the antisnipe window): only what was true then.
    const readBefore = row({ ended: true, lastReadAt: "2026-10-04T16:03:00Z", lastCompleteReadAt: "2026-10-04T16:03:00Z" });
    expect(lotStatus(readBefore, lot({ myStatus: "lead" }))).toMatchObject({ label: "Leading at last read", cls: "lead" });
    expect(lotStatus(readBefore, lot({ myStatus: "outbid" }))).toMatchObject({ label: "Outbid at last read" });
  });

  it("marked as ended by you: the last full read is final, even before the end time", () => {
    const marked = row({ ended: true, endedByYouAt: "2026-10-04T15:40:00Z" });
    expect(lotStatus(marked, lot({ myStatus: "lead" }))).toMatchObject({ key: "won" });
    expect(lotStatus(marked, lot({ myStatus: "outbid" }))).toMatchObject({ key: "lost" });
    expect(readAfterEnd(row({ ended: true, endsAtMs: null, endedByYouAt: "2026-10-04T15:40:00Z" }))).toBe(true);
    // Never read in full: nothing to go by yet.
    expect(lotStatus(row({ ended: true, endedByYouAt: "2026-10-04T15:40:00Z", lastCompleteReadAt: null }), lot({ myStatus: "lead" })).key).toBe("leading-at-last-read");
  });

  it("claim sales: Won / Check", () => {
    expect(lotStatus(row({ type: "claim" }), lot({ myClaim: "claimed" }))).toMatchObject({ label: "Won", cls: "won" });
    expect(lotStatus(row({ type: "fixed" }), lot({ myClaim: "check" }))).toMatchObject({ key: "check", cls: "outbid" });
    expect(lotStatus(row({ type: "claim" }), lot({}))).toMatchObject({ key: "none" });
  });

  it("readAfterEnd needs an end time and a complete read after it", () => {
    expect(readAfterEnd(row({ ended: true, endsAtMs: null }))).toBe(false);
    expect(readAfterEnd(row({ ended: true, lastCompleteReadAt: null }))).toBe(false);
    // A partial read after the end doesn't count (review H2): only "Leading at last read".
    const partialAfter = row({ ended: true, lastReadAt: "2026-10-04T16:10:00Z", lastCompleteReadAt: "2026-10-04T15:30:00Z" });
    expect(lotStatus(partialAfter, lot({ myStatus: "lead" })).label).toBe("Leading at last read");
  });
});

describe("wonTotal", () => {
  it("adds your cards' prices and flags ones Claude couldn't read", () => {
    const lots = [
      lot({ claimCards: [{ card: "Marowak", price: 200, claimedBy: "Me", isMe: true }, { card: "Kingler", price: 250, claimedBy: "X", isMe: false }] }),
      lot({ claimCards: [{ card: "Feraligatr", price: null, claimedBy: "Me", isMe: true }] }),
    ];
    const t = wonTotal(lots);
    expect(t).toEqual({ cards: 2, kr: 200, unknown: 1 });
    expect(krText(t)).toBe("200 kr + ?");
  });
});
