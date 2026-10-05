import { describe, expect, it } from "vitest";
import { effectiveEnd, judgeBidTimes, relativeAge, replyWindow, saleClosesAt, TIME_SLACK_MS } from "../src/domain/bidTime";
import { osloToUtc, parseEndTime } from "../src/domain/endTime";

// Bid times vs the lot's end, with chained antisnipe (#329; notes/fb_auction_watcher/group-domain.md §4.2).

const MIN = 60_000;
const HOUR = 60 * MIN;
const at = (h: number, m: number) => osloToUtc(2026, 10, 5, h, m).getTime(); // Monday 5 Oct 2026, Oslo

describe("relativeAge", () => {
  it.each([
    ["5 min", 5 * MIN, 6 * MIN],
    ["1 t", HOUR, 2 * HOUR],
    ["2 t", 2 * HOUR, 3 * HOUR],
    ["1 d", 24 * HOUR, 48 * HOUR],
    ["3 u", 21 * 24 * HOUR, 28 * 24 * HOUR],
    ["5m", 5 * MIN, 6 * MIN],
    ["2h", 2 * HOUR, 3 * HOUR],
    ["Akkurat nå", 0, MIN],
    ["Just now", 0, MIN],
    ["30 sek", 0, MIN],
    ["for 5 minutter siden", 5 * MIN, 6 * MIN],
    ["for omtrent en time siden", 0, 2 * HOUR],
  ])("%s → [%d, %d)", (text, min, max) => expect(relativeAge(text)).toEqual({ min, max }));

  it("is null when it isn't a relative time", () => {
    expect(relativeAge(null)).toBeNull();
    expect(relativeAge("")).toBeNull();
    expect(relativeAge("3. oktober")).toBeNull();
    expect(relativeAge("Liker")).toBeNull();
  });
});

describe("replyWindow", () => {
  it("is the read time minus the age, widened by the slack, never after the read", () => {
    const seen = at(21, 10);
    expect(replyWindow("5 min", seen, seen)).toEqual({ lo: seen - 6 * MIN - TIME_SLACK_MS, hi: seen - 5 * MIN + TIME_SLACK_MS });
    expect(replyWindow("Akkurat nå", seen, seen).hi).toBe(seen);
  });

  it("without a readable age or read time: only that it existed by the last read", () => {
    expect(replyWindow(null, at(21, 10), at(21, 10))).toEqual({ lo: -Infinity, hi: at(21, 10) });
    expect(replyWindow("5 min", null, at(22, 0))).toEqual({ lo: -Infinity, hi: at(22, 0) });
  });
});

describe("effectiveEnd (chained antisnipe)", () => {
  it("the rules' examples: end 18:00, bid 17:58 → 18:03; end 20:00, bid 19:59 → 20:04", () => {
    expect(effectiveEnd(at(18, 0), 5, [at(17, 58)])).toBe(at(18, 3));
    expect(effectiveEnd(at(20, 0), 5, [at(19, 59)])).toBe(at(20, 4));
  });

  it("chains: each bid before the current end, in its last 5 min, moves it again", () => {
    expect(effectiveEnd(at(20, 0), 5, [at(19, 58), at(20, 2), at(20, 6)])).toBe(at(20, 11));
  });

  it("a bid at or after the current end moves nothing; an early bid moves nothing", () => {
    expect(effectiveEnd(at(20, 0), 5, [at(19, 58), at(20, 3)])).toBe(at(20, 3));
    expect(effectiveEnd(at(20, 0), 5, [at(19, 0)])).toBe(at(20, 0));
  });

  it("no antisnipe: the end as written", () => {
    expect(effectiveEnd(at(20, 0), 0, [at(19, 59)])).toBe(at(20, 0));
    expect(effectiveEnd(at(20, 0), null, [at(19, 59)])).toBe(at(20, 0));
  });
});

describe("judgeBidTimes", () => {
  const exact = (t: number) => ({ lo: t, hi: t });

  it("on time, late, and the chain with exact times", () => {
    const r = judgeBidTimes(at(20, 0), 5, [exact(at(19, 58)), exact(at(20, 2)), exact(at(20, 8))]);
    expect(r.verdicts).toEqual(["on-time", "on-time", "late"]);
    expect(r.endLo).toBe(at(20, 7));
    expect(r.endHi).toBe(at(20, 7));
  });

  it("no antisnipe: a bid at the end minute is late (end 20:00 → 19:59 counts, 20:00 doesn't)", () => {
    const r = judgeBidTimes(at(20, 0), 0, [exact(at(19, 59)), exact(at(20, 0))]);
    expect(r.verdicts).toEqual(["on-time", "late"]);
  });

  it("a window straddling the end is unsure; it may extend the end at most to just before it + 5", () => {
    const r = judgeBidTimes(at(20, 0), 5, [{ lo: at(19, 30), hi: at(20, 30) }]);
    expect(r.verdicts).toEqual(["unsure"]);
    expect(r.endLo).toBe(at(20, 0));
    expect(r.endHi).toBe(at(20, 5) - 1);
  });

  it("no evidence of when it came (read after the end, no age): unknown, moves nothing", () => {
    const r = judgeBidTimes(at(20, 0), 5, [{ lo: -Infinity, hi: at(20, 30) }]);
    expect(r.verdicts).toEqual(["unknown"]);
    expect(r.endHi).toBe(at(20, 0));
  });

  it("a bid that doesn't count (too low, under a reply) doesn't move the end", () => {
    const r = judgeBidTimes(at(20, 0), 5, [exact(at(19, 58)), exact(at(20, 2))], (i) => i !== 0);
    expect(r.verdicts).toEqual(["on-time", "late"]);
  });
});

describe("saleClosesAt", () => {
  it("never before end + antisnipe window; later when a lot's chain runs past it", () => {
    expect(saleClosesAt(at(20, 0), 5)).toBe(at(20, 5));
    expect(saleClosesAt(at(20, 0), 5, [at(20, 3), at(20, 11), null])).toBe(at(20, 11));
    expect(saleClosesAt(at(20, 0), null, [])).toBe(at(20, 0));
    expect(saleClosesAt(null, 5, [at(20, 11)])).toBeNull();
  });
});

describe("parseEndTime: 'klokken' (group-domain §11)", () => {
  const REF = new Date("2026-10-05T10:00:00Z");
  it("Tirsdag klokken 23, 6 oktober → Tue 6 Oct 23:00, sure", () => {
    expect(parseEndTime("Sluttid: Tirsdag klokken 23, 6 oktober", REF)).toMatchObject({ endsAt: osloToUtc(2026, 10, 6, 23, 0).toISOString(), sure: true });
  });
  it("klokka 21.30 too", () => {
    expect(parseEndTime("Sluttid: 07.10 klokka 21.30", REF).endsAt).toBe(osloToUtc(2026, 10, 7, 21, 30).toISOString());
  });
});
