import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Lot } from "../src/domain/bids";
import { endingSoonNote, outbidNotes, resultNotes } from "../src/background/notify";
import { FINAL_READ_GRACE_MS, watchPlan, type MyAuction } from "../src/background/watch";
import { fakeChrome, type FakeChrome } from "./fakes/chrome";
import { AUCTION_ENDS, auctionText, capture, lot, ME, post, reply, SELLER } from "./fakes/posts";
import { memoryStore } from "./fakes/store";

// Your auctions in the background: the final read on time, "ends in 10 min", and notifications
// for outbid / won / lost. Invented data.

const MIN = 60_000;
const NOW = Date.parse("2026-10-04T12:00:00Z");
const fbUrl = (id: string) => `https://www.facebook.com/groups/g/posts/${id}/`;
const mk = (id: string, over: Partial<MyAuction> = {}): MyAuction => ({
  postId: id, url: fbUrl(id), title: `Sale ${id}`, endsAt: NOW + 60 * MIN, closesAt: NOW + 65 * MIN,
  lastRead: NOW - 5 * MIN, lastComplete: NOW - 5 * MIN, lots: [], ...over,
});
const mkLot = (position: number, over: Partial<Lot> = {}): Lot =>
  ({ position, commentId: `c${position}`, title: `Lot ${position}`, myStatus: "none", highestBid: null, myHighestBid: null, increment: 10, ...over }) as Lot;

describe("watchPlan", () => {
  it("a running sale: re-read after 15 min; the next final read is just after its close", () => {
    const plan = watchPlan([mk("A"), mk("B", { lastRead: NOW - 20 * MIN })], NOW);
    expect(plan.due.map((a) => a.postId)).toEqual(["B"]);
    expect(plan.nextFinalAt).toBe(NOW + 65 * MIN + FINAL_READ_GRACE_MS);
  });

  it("closed: the final read is due once the grace has passed, until a complete read after the close", () => {
    const closed = mk("A", { endsAt: NOW - 10 * MIN, closesAt: NOW - MIN });
    expect(watchPlan([closed], NOW)).toMatchObject({ due: [], nextFinalAt: NOW + MIN }); // Grace: 2 min after the close.
    expect(watchPlan([closed], NOW + MIN).due.map((a) => a.postId)).toEqual(["A"]);
    expect(watchPlan([{ ...closed, lastComplete: NOW }], NOW + 5 * MIN)).toMatchObject({ due: [], nextFinalAt: null });
  });

  it("gives up on a final read 2 h after the close", () => {
    expect(watchPlan([mk("A", { endsAt: NOW - 3 * 3600_000, closesAt: NOW - 3 * 3600_000 })], NOW).due).toEqual([]);
  });

  it("ending within 10 min: notify; otherwise when the next one gets there", () => {
    const plan = watchPlan([mk("A", { endsAt: NOW + 8 * MIN }), mk("B", { endsAt: NOW + 30 * MIN }), mk("C", { endsAt: null, closesAt: null })], NOW);
    expect(plan.endingSoon.map((a) => a.postId)).toEqual(["A"]);
    expect(plan.nextEndingSoonAt).toBe(NOW + 20 * MIN);
  });
});

describe("notes", () => {
  const sale = { postId: "1", title: "Gengar", url: fbUrl("1") };

  it("outbid: only lots you were leading (or maybe leading) before; links to the lot, with the next bid", () => {
    const before = [mkLot(1, { myStatus: "lead" }), mkLot(2, { myStatus: "outbid" }), mkLot(3, { myStatus: "unclear" })];
    const after = [
      mkLot(1, { myStatus: "outbid", highestBid: 190, myHighestBid: 160 }),
      mkLot(2, { myStatus: "outbid", highestBid: 300 }),
      mkLot(3, { myStatus: "outbid", highestBid: 50, myHighestBid: 40 }),
    ];
    const notes = outbidNotes(before, after, sale);
    expect(notes.map((n) => n.title)).toEqual(["Outbid: Lot 1", "Outbid: Lot 3"]);
    expect(notes[0]).toMatchObject({ message: "Gengar · highest 190 kr (you 160 kr) · next bid 200 kr", url: `${fbUrl("1")}?comment_id=c1` });
    expect(outbidNotes(null, after, sale)).toEqual([]); // The first read: nothing to compare with.
  });

  it("result: what you won (and for how much) and what you lost", () => {
    const after = [mkLot(1, { myStatus: "lead", myHighestBid: 280 }), mkLot(2, { myStatus: "lead", myHighestBid: 200 }), mkLot(3, { myStatus: "outbid" })];
    expect(resultNotes(after, sale)).toEqual([
      { key: "result:1", title: "Won 2 lots · 480 kr · lost 1", message: "Gengar · won: Lot 1, Lot 2 · lost: Lot 3", url: fbUrl("1") },
    ]);
    expect(resultNotes([mkLot(1, { myStatus: "outbid" })], sale)[0].title).toBe("Lost 1 lot");
    expect(resultNotes([mkLot(1)], sale)).toEqual([]);
  });

  it("ending soon: minutes left and where you stand", () => {
    const a = mk("1", { title: "Gengar", endsAt: NOW + 9 * MIN, lastRead: NOW - 4 * MIN, lots: [mkLot(1, { myStatus: "lead" }), mkLot(2, { myStatus: "outbid" })] });
    expect(endingSoonNote(a, NOW)).toMatchObject({ title: "Ends in 9 min: Gengar", message: "leading 1 · outbid 1 (at the last read, 4 min ago)", url: fbUrl("1") });
  });
});

describe("watchMyAuctions (alarms and notifications)", () => {
  let fake: FakeChrome;
  let reader: typeof import("../src/background/reader");
  // You lead lot 1 (40 over 10) in an auction ending 21:00 Oslo (19:00 UTC), no antisnipe.
  const store = (capturedAt: string) =>
    memoryStore({
      posts: [post("1", auctionText())],
      captures: [{ postId: "1", capture: capture("1", auctionText(), [lot(1, [reply("Bidder A", `${SELLER} 10`), reply(ME, `${SELLER} 40`)])], { capturedAt }) }],
    });

  beforeEach(async () => {
    vi.useFakeTimers({ now: NOW });
    fake = fakeChrome();
    vi.stubGlobal("chrome", fake.chrome);
    vi.resetModules();
    reader = await import("../src/background/reader");
    (await import("../src/background/notify")).listenForNoteClicks(); // As the worker does.
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });
  const settings = (s: { autoScan: boolean; notify: boolean }) => fake.local.set({ settings: { myName: ME, useClaude: false, ...s } });

  it("auto-scan on: an alarm for the final read just after the close", async () => {
    await settings({ autoScan: true, notify: false });
    await reader.watchMyAuctions(store(new Date(NOW).toISOString()));
    expect(fake.alarms.get(reader.WATCH_ALARM)?.scheduledTime).toBe(AUCTION_ENDS + FINAL_READ_GRACE_MS);
  });

  it("notifications on: an alarm 10 min before the end, then one notification, shown once", async () => {
    await settings({ autoScan: false, notify: true });
    const s = store(new Date(NOW).toISOString());
    await reader.watchMyAuctions(s);
    expect(fake.alarms.get(reader.WATCH_ALARM)?.scheduledTime).toBe(AUCTION_ENDS - 10 * MIN);
    vi.setSystemTime(AUCTION_ENDS - 10 * MIN);
    await reader.watchMyAuctions(s);
    await reader.watchMyAuctions(s); // Again: not shown twice.
    expect(fake.notifications.map((n) => n.title)).toEqual(["Ends in 10 min: AUKSJON/BUDRUNDE"]);
    // Clicking it opens the post.
    await fake.clickNotification(fake.notifications[0].id);
    expect(fake.created.at(-1)).toEqual({ url: fbUrl("1"), active: true });
  });

  it("both off: no alarm, no notification", async () => {
    await settings({ autoScan: false, notify: false });
    vi.setSystemTime(AUCTION_ENDS - 5 * MIN);
    await reader.watchMyAuctions(store(new Date(NOW).toISOString()));
    expect(fake.alarms.has(reader.WATCH_ALARM)).toBe(false);
    expect(fake.notifications).toEqual([]);
  });
});
