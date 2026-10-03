import { describe, expect, it } from "vitest";
import { makeSlot, type Slot } from "../src/background/slot";

// The one "Facebook slot" (review H6), with in-memory storage and a fake clock.
function setup() {
  let stored: Slot | null = null;
  let t = Date.parse("2026-10-04T12:00:00Z");
  const slot = makeSlot({ get: async () => stored, set: async (s) => void (stored = s) }, () => t);
  return { slot, advance: (ms: number) => (t += ms) };
}

describe("facebook slot", () => {
  it("only one holder at a time", async () => {
    const { slot } = setup();
    expect(await slot.acquire("auto-scan", 1)).toBe(true);
    expect(await slot.acquire("reader", 2)).toBe(false);
    await slot.release("auto-scan", 1);
    expect(await slot.acquire("reader", 2)).toBe(true);
  });

  it("two asking at the same moment: exactly one gets it", async () => {
    const { slot } = setup();
    const results = await Promise.all([slot.acquire("reader", 1), slot.acquire("menu-scan", 2), slot.acquire("auto-scan", 3)]);
    expect(results.filter(Boolean)).toHaveLength(1);
  });

  it("only the holder (in its tab) can free it", async () => {
    const { slot } = setup();
    await slot.acquire("reader", 1);
    await slot.release("auto-scan", 1);
    await slot.release("reader", 2);
    expect((await slot.holder())?.holder).toBe("reader");
  });

  it("closing the holder's tab frees it", async () => {
    const { slot } = setup();
    await slot.acquire("menu-scan", 7);
    await slot.releaseTab(7);
    expect(await slot.holder()).toBeNull();
  });

  it("a holder that never frees it expires (a scan from the menu gets longer)", async () => {
    const { slot, advance } = setup();
    await slot.acquire("reader", 1);
    advance(7 * 60_000);
    expect(await slot.acquire("auto-scan", 2)).toBe(true);
    advance(5 * 60_000);
    expect(await slot.acquire("menu-scan", 3)).toBe(false); // auto-scan still within its 10 min.
    advance(6 * 60_000);
    expect(await slot.acquire("menu-scan", 3)).toBe(true);
    advance(20 * 60_000);
    expect(await slot.acquire("reader", 4)).toBe(false); // menu-scan: 25 min.
  });
});
