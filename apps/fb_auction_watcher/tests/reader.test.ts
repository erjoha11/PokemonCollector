import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Store } from "../src/store";
import { fakeChrome, type FakeChrome } from "./fakes/chrome";
import { AUCTION_ENDS, auctionText, capture, lot, ME, post, reply, SELLER } from "./fakes/posts";
import { memoryStore } from "./fakes/store";

// The post-read queue (src/background/reader.ts, review M8) against a fake `chrome` and a fake
// clock. The reader and the Facebook slot keep module state (their serializing queues, the
// slot's clock), so each test loads fresh copies after the fake timers are in place.

type Reader = typeof import("../src/background/reader");
type SlotModule = typeof import("../src/background/slot");

const NOW = Date.parse("2026-10-04T12:00:00Z");
const fbUrl = (id: string) => `https://www.facebook.com/groups/g/posts/${id}/`;
const click = (id: string) => ({ postId: id, url: fbUrl(id), reason: "click" as const, visible: true });
const auto = (id: string) => ({ postId: id, url: fbUrl(id), reason: "auto" as const });

let fake: FakeChrome;
let reader: Reader;
let slot: SlotModule["facebookSlot"];

async function load(options: Parameters<typeof fakeChrome>[0] = {}) {
  fake = fakeChrome(options);
  vi.stubGlobal("chrome", fake.chrome);
  vi.resetModules();
  reader = await import("../src/background/reader");
  slot = (await import("../src/background/slot")).facebookSlot;
  // As the worker wires it (src/background/index.ts): the reader's alarm kicks the reader.
  fake.chrome.alarms.onAlarm.addListener((alarm) => (reader.isReaderAlarm(alarm.name) ? reader.kickReader() : undefined));
}

const state = () => reader.getReaderState();
const queued = async () => (await state()).queue.map((j) => `${j.reason}:${j.postId}`);
/** Something else (the automatic scan) holds the Facebook slot. */
const blockSlot = () => fake.session.set({ facebookSlot: { holder: "auto-scan", tabId: 1, since: new Date().toISOString() } });
const advance = (ms: number) => vi.advanceTimersByTimeAsync(ms);

beforeEach(async () => {
  vi.useFakeTimers({ now: NOW });
  await load();
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("enqueueReads", () => {
  it("your clicks go before automatic re-reads, in the order they came", async () => {
    await blockSlot();
    await reader.enqueueReads([auto("1"), auto("2")]);
    await reader.enqueueReads([click("3")]);
    await reader.enqueueReads([auto("4"), click("5")]);
    expect(await queued()).toEqual(["click:3", "click:5", "auto:1", "auto:2", "auto:4"]);
    expect(fake.created).toEqual([]); // The slot is taken: nothing opened...
    expect(fake.alarms.get(reader.READER_ALARM)?.scheduledTime).toBe(Date.now() + 30_000); // ...try again later.
  });

  it("a click upgrades a queued automatic re-read of the same post; nothing else is added twice", async () => {
    await blockSlot();
    await reader.enqueueReads([auto("1"), auto("2")]);
    await reader.enqueueReads([auto("1"), click("2")]);
    await reader.enqueueReads([click("2"), auto("2")]);
    const s = await state();
    expect(s.queue).toEqual([click("2"), auto("1")]);
  });

  it("ignores URLs outside Facebook and the post being read right now", async () => {
    await reader.enqueueReads([click("1")]);
    expect((await state()).current?.postId).toBe("1");
    await reader.enqueueReads([click("1"), auto("1"), { ...click("2"), url: "https://example.com/posts/2/" }]);
    expect(await queued()).toEqual([]);
    expect(fake.created).toHaveLength(1);
  });
});

describe("one read at a time", () => {
  it("a click opens a visible tab, takes the slot, and asks the content script for a quiet read", async () => {
    await reader.readNow(fbUrl("1"), "1", true);
    const s = await state();
    expect(fake.created).toEqual([{ url: fbUrl("1"), active: true }]);
    expect(s.current).toMatchObject({ postId: "1", tabId: 100, startedAt: new Date(NOW).toISOString() });
    expect(await slot.holder()).toMatchObject({ holder: "reader", tabId: 100 });
    expect(fake.tabMessages).toEqual([{ tabId: 100, message: { type: "fbaw/read-post", waitForPost: true, silent: true } }]);
    expect(fake.badges).toContainEqual({ call: "text", tabId: 100, value: "…" });
  });

  it("Read (the default): a background tab, ahead of the queue, closed when the read is done", async () => {
    await reader.readNow(fbUrl("1"), "1");
    expect(fake.created).toEqual([{ url: fbUrl("1"), active: false }]);
    expect((await state()).current).toMatchObject({ postId: "1", reason: "click", tabId: 100 });
    await reader.finishRead(100, true, "3 lots");
    expect(fake.tabs.has(100)).toBe(false);
  });

  it("a second job waits for the first: one tab talking to Facebook", async () => {
    await reader.enqueueReads([click("1")]);
    await reader.enqueueReads([click("2")]);
    await reader.kickReader();
    expect(fake.created).toHaveLength(1);
    expect(await queued()).toEqual(["click:2"]);

    await reader.finishRead(100, true, "3 lots");
    await reader.kickReader(); // Waits for the kick finishRead started.
    expect(fake.created.map((t) => t.url)).toEqual([fbUrl("1"), fbUrl("2")]);
    expect((await state()).current).toMatchObject({ postId: "2", tabId: 101 });
    expect(await slot.holder()).toMatchObject({ holder: "reader", tabId: 101 });
  });

  it("waits while another activity holds the slot, then goes on when the alarm fires", async () => {
    await blockSlot();
    await reader.enqueueReads([click("1")]);
    expect(fake.created).toEqual([]);
    await fake.session.set({ facebookSlot: null }); // The scan finished.
    await fake.fireAlarm(reader.READER_ALARM);
    await reader.kickReader();
    expect((await state()).current?.postId).toBe("1");
  });

  it("two kicks at the same moment open one tab", async () => {
    await blockSlot();
    await reader.enqueueReads([click("1"), click("2")]);
    await fake.session.set({ facebookSlot: null });
    await Promise.all([reader.kickReader(), reader.kickReader(), reader.kickReader()]);
    expect(fake.created).toHaveLength(1);
  });
});

describe("finishRead", () => {
  it("leaves your (visible) tab open with a mark on the icon, frees the slot, records the outcome", async () => {
    await reader.readNow(fbUrl("1"), "1", true);
    await reader.finishRead(100, true, "3 lots");
    expect(fake.tabs.has(100)).toBe(true);
    expect(fake.badges).toContainEqual({ call: "text", tabId: 100, value: "✓" });
    expect(await slot.holder()).toBeNull();
    expect(await state()).toMatchObject({ current: null, lastOutcome: "Read: 3 lots", lastAt: new Date(NOW).toISOString() });
    await advance(20_000);
    expect(fake.badges.at(-1)).toEqual({ call: "text", tabId: 100, value: "" }); // The mark goes away.
  });

  it("closes a hidden tab after an automatic read", async () => {
    await reader.enqueueReads([auto("1")]);
    expect(fake.created).toEqual([{ url: fbUrl("1"), active: false }]);
    await reader.finishRead(100, false, "the post was gone");
    expect(fake.tabs.has(100)).toBe(false);
    expect(await slot.holder()).toBeNull();
    expect((await state()).lastOutcome).toBe("Couldn't read: the post was gone");
  });

  it("ignores a report from a tab that isn't the current read", async () => {
    await reader.enqueueReads([auto("1")]);
    await reader.finishRead(999, true, "stray");
    expect((await state()).current?.tabId).toBe(100);
    expect(fake.tabs.has(100)).toBe(true);
  });

  it("the next automatic read waits for the pause; a click doesn't", async () => {
    await blockSlot();
    await reader.enqueueReads([auto("1"), auto("2")]);
    await fake.session.set({ facebookSlot: null });
    await reader.kickReader();
    await reader.finishRead(100, true, "ok");
    await reader.kickReader();
    expect(fake.created).toHaveLength(1); // auto:2 waits 30-45 s.
    const due = fake.alarms.get(reader.READER_ALARM)!.scheduledTime - Date.now();
    expect(due).toBeGreaterThanOrEqual(30_000);
    expect(due).toBeLessThanOrEqual(45_000);

    // Fired early (another trigger): still paced, rescheduled rather than read.
    await advance(10_000);
    await fake.fireAlarm(reader.READER_ALARM);
    expect(fake.created).toHaveLength(1);
    await reader.enqueueReads([click("3")]);
    expect(fake.created.map((t) => t.url)).toEqual([fbUrl("1"), fbUrl("3")]);
  });
});

describe("failures and timeouts", () => {
  it("a read that never reports back is finished after 3 min (hidden tab closed)", async () => {
    await reader.enqueueReads([auto("1")]);
    await advance(3 * 60_000 - 1000);
    await reader.kickReader();
    expect((await state()).current?.postId).toBe("1");
    await advance(2000);
    await reader.kickReader();
    expect(await state()).toMatchObject({ current: null, lastOutcome: "Couldn't read: it took too long" });
    expect(fake.tabs.has(100)).toBe(false);
    expect(await slot.holder()).toBeNull();
  });

  it("the safety-net alarm fires after the limit and finishes it", async () => {
    await reader.enqueueReads([auto("1")]);
    const alarm = fake.alarms.get(reader.READER_ALARM)!;
    expect(alarm.scheduledTime).toBe(NOW + 3 * 60_000 + 5000);
    await advance(alarm.scheduledTime - Date.now());
    await fake.fireAlarm(reader.READER_ALARM);
    expect((await state()).lastOutcome).toBe("Couldn't read: it took too long");
  });

  it("your visible read gets 5 min, and its tab stays open when it times out", async () => {
    await reader.readNow(fbUrl("1"), "1", true);
    await advance(4 * 60_000);
    await reader.kickReader();
    expect((await state()).current?.postId).toBe("1");
    await advance(60_000 + 1000);
    await reader.kickReader();
    expect((await state()).lastOutcome).toBe("Couldn't read: it took too long");
    expect(fake.tabs.has(100)).toBe(true);
  });

  it("a stuck read doesn't block the queue: the next job starts once it times out", async () => {
    await reader.enqueueReads([click("1"), click("2")]);
    await advance(5 * 60_000 + 1000);
    await reader.kickReader();
    expect((await state()).current?.postId).toBe("2");
    expect(fake.created).toHaveLength(2);
  });

  it("a page that doesn't load is given up after 45 s", async () => {
    await load({ tabsLoad: false });
    const kicked = reader.enqueueReads([auto("1")]);
    await advance(45_000);
    await kicked;
    expect(await state()).toMatchObject({ current: null, lastOutcome: "Couldn't read: the post didn't load" });
    expect(fake.tabs.has(100)).toBe(false);
  });

  it("a page that loads late is still read", async () => {
    await load({ tabsLoad: false });
    const kicked = reader.enqueueReads([auto("1")]);
    await advance(5000);
    fake.loadTab(100);
    await kicked;
    expect(fake.tabMessages).toHaveLength(1);
  });

  it("no content script in the tab: finished as failed", async () => {
    await load({ contentScript: false });
    await reader.enqueueReads([auto("1")]);
    expect(await state()).toMatchObject({ current: null, lastOutcome: "Couldn't read: no content script in the tab" });
    expect(await slot.holder()).toBeNull();
  });

  it("no tab could be opened: the job is dropped and the slot freed", async () => {
    fake.state.failNextCreate = true;
    await reader.enqueueReads([click("1")]);
    expect(await state()).toMatchObject({ current: null, queue: [], lastOutcome: "Couldn't read: no tab" });
    expect(await slot.holder()).toBeNull();
  });
});

describe("while you're away", () => {
  it("automatic re-reads are dropped while the machine is idle or locked", async () => {
    fake.state.idle = "locked";
    await reader.enqueueReads([auto("1"), auto("2")]);
    expect(fake.created).toEqual([]);
    expect(await queued()).toEqual([]);
  });

  it("your clicks are still read", async () => {
    fake.state.idle = "idle";
    await reader.enqueueReads([auto("1"), click("2")]);
    expect(fake.created).toEqual([{ url: fbUrl("2"), active: true }]);
    await reader.finishRead(100, true, "ok");
    await advance(30_000);
    await fake.fireAlarm(reader.READER_ALARM);
    expect(await queued()).toEqual([]); // auto:1 dropped, not read.
    expect(fake.created).toHaveLength(1);
  });
});

describe("watchMyAuctions (re-reads)", () => {
  // You lead lot 1 (40 over 10).
  const leading = (id: string, over: Parameters<typeof capture>[3] = {}) =>
    capture(id, auctionText(), [lot(1, [reply("Bidder A", `${SELLER} 10`), reply(ME, `${SELLER} 40`)])], over);
  const at = (ms: number) => new Date(ms).toISOString();

  async function rereads(store: Store, autoScan = true) {
    await fake.local.set({ settings: { autoScan, myName: ME, useClaude: false } });
    await blockSlot(); // Keep the jobs in the queue so the test can see them.
    await reader.watchMyAuctions(store);
    return (await state()).queue.map((j) => j.postId);
  }

  it("re-reads running auctions you're in when the last read is 15+ min old", async () => {
    const store = memoryStore({
      posts: ["old", "fresh", "notMine", "claim"].map((id) => post(id, id === "claim" ? "Claim salg-annonse\nSluttid: 04.10.26 kl 21:00" : auctionText())),
      captures: [
        { postId: "old", capture: leading("old", { capturedAt: at(NOW - 20 * 60_000) }) },
        { postId: "fresh", capture: leading("fresh", { capturedAt: at(NOW - 5 * 60_000) }) },
        { postId: "notMine", capture: capture("notMine", auctionText(), [lot(1, [reply("Bidder A", `${SELLER} 10`)])], { capturedAt: at(NOW - 60 * 60_000) }) },
        { postId: "claim", capture: leading("claim", { capturedAt: at(NOW - 60 * 60_000) }) },
      ],
    });
    expect(await rereads(store)).toEqual(["old"]);
  });

  it("outbid and unclear auctions count too; only auto-reads, hidden", async () => {
    const outbid = capture("o", auctionText(), [lot(1, [reply(ME, `${SELLER} 10`), reply("Bidder A", `${SELLER} 40`)])], { capturedAt: at(NOW - 20 * 60_000) });
    const store = memoryStore({ posts: [post("o", auctionText())], captures: [{ postId: "o", capture: outbid }] });
    expect(await rereads(store)).toEqual(["o"]);
    expect((await state()).queue[0]).toEqual({ postId: "o", url: fbUrl("o"), reason: "auto" });
  });

  it("does nothing while auto-scan is off, or for a read without its post", async () => {
    const store = memoryStore({ posts: [post("1", auctionText())], captures: [{ postId: "1", capture: leading("1", { capturedAt: at(NOW - 60 * 60_000) }) }] });
    expect(await rereads(store, false)).toEqual([]);
    const orphan = memoryStore({ captures: [{ postId: "1", capture: leading("1", { capturedAt: at(NOW - 60 * 60_000) }) }] });
    expect(await rereads(orphan)).toEqual([]);
  });

  describe("the final read after the end", () => {
    const store = (c: Parameters<typeof leading>[1], text = auctionText()) =>
      memoryStore({ posts: [post("1", text)], captures: [{ postId: "1", capture: { ...leading("1"), ...c, post: { ...leading("1").post, text } } }] });

    it("ended 30 min ago, last complete read before the end: read once more (even if just read)", async () => {
      vi.setSystemTime(AUCTION_ENDS + 30 * 60_000);
      expect(await rereads(store({ capturedAt: at(AUCTION_ENDS - 60_000), completeAt: at(AUCTION_ENDS - 60_000) }))).toEqual(["1"]);
    });

    it("a complete read after the end settles it", async () => {
      vi.setSystemTime(AUCTION_ENDS + 30 * 60_000);
      expect(await rereads(store({ capturedAt: at(AUCTION_ENDS + 60_000), completeAt: at(AUCTION_ENDS + 60_000) }))).toEqual([]);
    });

    it("a partial read after the end is tried again", async () => {
      vi.setSystemTime(AUCTION_ENDS + 30 * 60_000);
      expect(await rereads(store({ capturedAt: at(AUCTION_ENDS + 60_000), completeAt: at(AUCTION_ENDS - 60 * 60_000) }))).toEqual(["1"]);
    });

    it("a read that never was complete counts as before the end", async () => {
      vi.setSystemTime(AUCTION_ENDS + 30 * 60_000);
      expect(await rereads(store({ capturedAt: at(AUCTION_ENDS + 60_000), completeAt: null }))).toEqual(["1"]);
    });

    it("older stored reads (no completeAt) count as complete when read", async () => {
      vi.setSystemTime(AUCTION_ENDS + 30 * 60_000);
      expect(await rereads(store({ capturedAt: at(AUCTION_ENDS + 60_000), completeAt: undefined }))).toEqual([]);
    });

    it("not for auctions that ended more than 2 h ago", async () => {
      vi.setSystemTime(AUCTION_ENDS + 2 * 60 * 60_000 + 60_000);
      expect(await rereads(store({ capturedAt: at(AUCTION_ENDS - 60_000), completeAt: at(AUCTION_ENDS - 60_000) }))).toEqual([]);
    });

    it("soft close: the end is 5 min later, so just after the stated time it's still running", async () => {
      vi.setSystemTime(AUCTION_ENDS + 3 * 60_000);
      const read = { capturedAt: at(AUCTION_ENDS - 60_000), completeAt: at(AUCTION_ENDS - 60_000) };
      expect(await rereads(store(read, auctionText("Ja")))).toEqual([]); // Running, read 4 min ago.
      expect(await rereads(store(read, auctionText("Nei")))).toEqual(["1"]); // Ended: final read.
    });
  });
});
