import { describe, expect, it } from "vitest";
import {
  describeRun,
  ENDED_GRACE_MS,
  failureStatus,
  isGlobalError,
  isPhotoError,
  isSaleOver,
  loadFailures,
  MAX_CLAIM_LOTS,
  pendingItems,
  PHOTO_CALLS_PER_HOUR,
  photoCallsLeft,
  photoLimitFreesAt,
  pruneFailures,
  recordFailure,
  saveFailures,
  STALE_AFTER_MS,
  type Failures,
} from "../src/background/claudeQueue";
import { bidAnswerKey, claimLotAnswerKey, endTimeAnswerKey } from "../src/llm/prompts";
import type { CapturedComment, PostCapture } from "../src/shared/capture";
import type { StoredPost } from "../src/shared/feed";
import * as fx from "./fakes/posts";
import { memoryStore, type MemoryStore } from "./fakes/store";

// Review M2: which items go to Claude, which wait after a failure, which are skipped, and the
// hourly photo cap. All offline: an in-memory Store (tests/fakes/store.ts), no bridge, no Claude.

const HOUR = 3_600_000;
const NOW = new Date("2026-10-03T12:00:00Z");
const ago = (ms: number) => new Date(NOW.getTime() - ms).toISOString();

const SELLER = "Selger Testesen";
const CLAIM_TEXT = (end: string) => `Claim salg-annonse\nSluttid: ${end}\nPris: står på kortet`;

function post(id: string, text: string, seenAt: string): StoredPost {
  return { id, url: `https://www.facebook.com/groups/g/posts/${id}/`, groupSlug: "g", sellerName: SELLER, text, textComplete: true, thumbnailUrl: null, firstSeenAt: seenAt, lastSeenAt: seenAt };
}

function lotComment(index: number, replies: { author: string; text: string }[], postId = "p"): CapturedComment {
  return {
    id: `c${index}`, url: null, author: SELLER, text: "", timeText: null, ariaLabel: null, truncated: false, rawText: "",
    images: [{ src: `https://scontent.xx.fbcdn.net/v/${postId}-lot${index}.jpg?oe=ABC`, alt: "" }],
    index, hasImage: true,
    replies: replies.map((r, i) => ({ id: `${index}0${i}`, url: null, author: r.author, text: r.text, timeText: null, ariaLabel: null, images: [], truncated: false, rawText: r.text })),
  };
}

function capture(text: string, capturedAt: string, comments: CapturedComment[]): PostCapture {
  return {
    schemaVersion: 1, capturedAt, pageUrl: "", pageLang: "nb", commentSortLabel: null, commentSortAction: "already-all",
    post: { url: "", author: SELLER, text, timeText: null, images: [], truncated: false },
    comments,
    stats: { expandClicks: 0, expandScrolls: 0, expandStoppedBecause: "", topLevelComments: comments.length, commentsWithImage: comments.length, replies: 0, orphanReplies: 0 },
    warnings: [],
  };
}

/** A claim sale with `lots` lots, ending at `end` (Oslo, "dd.mm.yy kl HH:mm"), seen and read at `at`. */
function claimSale(store: MemoryStore, id: string, end: string, at: string, lots: number) {
  const text = CLAIM_TEXT(end);
  store.posts.set(id, post(id, text, at));
  const comments = Array.from({ length: lots }, (_, i) => lotComment(i + 1, [{ author: "Kjøper En", text: "claim" }], id));
  store.captures.set(id, capture(text, at, comments));
}

describe("isSaleOver", () => {
  it("a sale with an end time is over a few hours after it", () => {
    const endsAt = ago(1 * HOUR);
    expect(isSaleOver(endsAt, ago(0), NOW)).toBe(false);
    expect(isSaleOver(ago(ENDED_GRACE_MS + 60_000), ago(0), NOW)).toBe(true);
  });

  it("no end time (fixed price): over when not seen or read for a few days", () => {
    expect(isSaleOver(null, ago(STALE_AFTER_MS - HOUR), NOW)).toBe(false);
    expect(isSaleOver(null, ago(STALE_AFTER_MS + HOUR), NOW)).toBe(true);
  });
});

describe("per-item failures", () => {
  it("waits after a failure, longer after the second, and skips after the third", () => {
    let f: Failures = {};
    f = recordFailure(f, "k", "claim-lot", "host error: HTTP Error 403: Forbidden", NOW);
    expect(f.k.attempts).toBe(1);
    expect(failureStatus(f.k, new Date(NOW.getTime() + 5 * 60_000))).toBe("waiting");
    expect(failureStatus(f.k, new Date(NOW.getTime() + 16 * 60_000))).toBe("ok");
    f = recordFailure(f, "k", "claim-lot", "again", NOW);
    expect(failureStatus(f.k, new Date(NOW.getTime() + 30 * 60_000))).toBe("waiting");
    expect(failureStatus(f.k, new Date(NOW.getTime() + 61 * 60_000))).toBe("ok");
    f = recordFailure(f, "k", "claim-lot", "again", NOW);
    expect(failureStatus(f.k, new Date(NOW.getTime() + 100 * HOUR))).toBe("skipped");
    expect(failureStatus(undefined, NOW)).toBe("ok");
  });

  it("forgets old records", () => {
    const f = recordFailure(recordFailure({}, "old", "bid", "x", new Date(NOW.getTime() - 8 * 24 * HOUR)), "new", "bid", "y", NOW);
    expect(Object.keys(pruneFailures(f, NOW))).toEqual(["new"]);
  });

  it("are kept in the store's meta and survive a bad value", async () => {
    const store = memoryStore();
    expect(await loadFailures(store)).toEqual({});
    const f = recordFailure({}, "k", "end-time", "timeout", NOW);
    await saveFailures(store, f);
    expect(await loadFailures(store)).toEqual(f);
    await store.setMeta("claudeFailures", "not json");
    expect(await loadFailures(store)).toEqual({});
  });

  it("tells errors that stop everything from an item's own", () => {
    expect(isGlobalError("Claude Code (claude) not found on this Mac")).toBe(true);
    expect(isGlobalError("Invalid API key · Please run /login")).toBe(true);
    expect(isGlobalError("Claude AI usage limit reached|1759500000")).toBe(true);
    expect(isGlobalError("host error: HTTP Error 403: Forbidden")).toBe(false);
    expect(isGlobalError("claude -p took more than 180 s")).toBe(false);
    expect(isPhotoError("host error: HTTP Error 403: Forbidden")).toBe(true);
    expect(isPhotoError("host error: HTTP Error 404: Not Found")).toBe(true);
    expect(isPhotoError("claude -p took more than 180 s")).toBe(false);
  });
});

describe("hourly photo cap", () => {
  it("counts calls in the last hour only, and says when the next is allowed", () => {
    const calls = Array.from({ length: PHOTO_CALLS_PER_HOUR }, (_, i) => ago(59 * 60_000 - i * 60_000));
    expect(photoCallsLeft(calls, NOW)).toBe(0);
    // The oldest (59 min ago) frees up in a minute.
    expect(photoLimitFreesAt(calls, NOW)).toBe(new Date(NOW.getTime() + 60_000).toISOString());
    expect(photoCallsLeft([...calls.slice(1), ago(2 * HOUR)], NOW)).toBe(1);
    expect(photoLimitFreesAt(calls.slice(1), NOW)).toBeNull();
  });
});

describe("pendingItems", () => {
  // NOW is Saturday 3 October 2026, 14:00 in Oslo.
  it("asks about lots of a running claim sale", async () => {
    const store = memoryStore();
    claimSale(store, "p1", "03.10.26 kl 21:00", ago(HOUR), 2);
    const p = await pendingItems(store, { now: NOW });
    expect(p.claimLots).toHaveLength(2);
    expect(p.more).toBe(false);
  });

  it("leaves out sales that ended hours ago, but not one that just ended", async () => {
    const store = memoryStore();
    claimSale(store, "old", "02.10.26 kl 21:00", ago(20 * HOUR), 2); // Ended 15 h ago.
    claimSale(store, "just", "03.10.26 kl 13:00", ago(2 * HOUR), 1); // Ended 1 h ago (Oslo 13:00 = 11:00Z).
    const p = await pendingItems(store, { now: NOW });
    expect(p.claimLots.map((l) => l.imageUrl)).toEqual([expect.stringContaining("/just-lot1.jpg")]);
    expect(p.claimLots).toHaveLength(1);
  });

  it("uses Claude's end time when the rules had none", async () => {
    const store = memoryStore();
    const text = "Claim salg-annonse\nAvsluttes i går kveld\nPris: står på kortet";
    store.posts.set("p", post("p", text, ago(30 * HOUR)));
    store.captures.set("p", capture(text, ago(HOUR), [lotComment(1, [])]));
    expect((await pendingItems(store, { now: NOW })).claimLots).toHaveLength(1);
    await store.saveAnswers([{ key: endTimeAnswerKey(text), value: "2026-10-02 21:00", at: ago(HOUR) }]);
    expect((await pendingItems(store, { now: NOW })).claimLots).toHaveLength(0);
  });

  it("fixed price: asked while recently seen or read, not after days", async () => {
    const store = memoryStore();
    const text = "FASTPRIS-annonse\nFastpris: står på bildet";
    store.posts.set("f", post("f", text, ago(5 * 24 * HOUR)));
    store.captures.set("f", capture(text, ago(5 * 24 * HOUR), [lotComment(1, [])]));
    expect((await pendingItems(store, { now: NOW })).claimLots).toHaveLength(0);
    // Read again today: the post is live again.
    store.captures.set("f", capture(text, ago(HOUR), [lotComment(1, [])]));
    expect((await pendingItems(store, { now: NOW })).claimLots).toHaveLength(1);
  });

  it("end-time questions only for posts seen in the last few days", async () => {
    const store = memoryStore();
    store.posts.set("a", post("a", "AUKSJON\nAvsluttes søndag kveld klokka ni", ago(HOUR)));
    store.posts.set("b", post("b", "AUKSJON\nAvsluttes fredag kveld klokka ni", ago(4 * 24 * HOUR)));
    const p = await pendingItems(store, { now: NOW });
    expect(p.endTimes.map((e) => e.text)).toEqual(["AUKSJON\nAvsluttes søndag kveld klokka ni"]);
  });

  it("a failed lot waits, then is skipped, and never blocks the lots behind it", async () => {
    const store = memoryStore();
    claimSale(store, "p1", "03.10.26 kl 21:00", ago(HOUR), MAX_CLAIM_LOTS + 1);
    const first = await pendingItems(store, { now: NOW });
    expect(first.claimLots).toHaveLength(MAX_CLAIM_LOTS);
    expect(first.more).toBe(true);
    const broken = first.claimLots[0].key;

    // Failed just now: the run takes the next lots instead.
    let failures = recordFailure({}, broken, "claim-lot", "host error: HTTP Error 403: Forbidden", NOW);
    const second = await pendingItems(store, { now: NOW, failures });
    expect(second.claimLots.map((l) => l.key)).not.toContain(broken);
    expect(second.claimLots).toHaveLength(MAX_CLAIM_LOTS);

    // Three failures: skipped for good, and reported.
    failures = recordFailure(recordFailure(failures, broken, "claim-lot", "403", NOW), broken, "claim-lot", "host error: HTTP Error 403: Forbidden", NOW);
    const later = await pendingItems(store, { now: new Date(NOW.getTime() + 2 * HOUR), failures });
    expect(later.claimLots.map((l) => l.key)).not.toContain(broken);
    expect(later.skipped.map((s) => s.key)).toEqual([broken]);
  });

  it("a new reply on a skipped lot is a new question", async () => {
    const store = memoryStore();
    claimSale(store, "p1", "03.10.26 kl 21:00", ago(HOUR), 1);
    const [lot] = (await pendingItems(store, { now: NOW })).claimLots;
    let failures: Failures = {};
    for (let i = 0; i < 3; i++) failures = recordFailure(failures, lot.key, "claim-lot", "403", NOW);
    expect((await pendingItems(store, { now: NOW, failures })).claimLots).toHaveLength(0);
    const c = store.captures.get("p1")!;
    c.comments[0].replies.push({ ...c.comments[0].replies[0], id: "199", author: "Kjøper To", text: "claim" });
    const again = await pendingItems(store, { now: NOW, failures });
    expect(again.claimLots).toHaveLength(1);
    expect(again.claimLots[0].key).not.toBe(lot.key);
  });

  it("answered lots aren't asked again", async () => {
    const store = memoryStore();
    claimSale(store, "p1", "03.10.26 kl 21:00", ago(HOUR), 1);
    const [lot] = (await pendingItems(store, { now: NOW })).claimLots;
    expect(lot.key).toBe(claimLotAnswerKey(lot));
    await store.saveAnswers([{ key: lot.key, value: { cards: [] }, at: NOW.toISOString() }]);
    expect((await pendingItems(store, { now: NOW })).claimLots).toHaveLength(0);
  });

  it("the photo cap holds lots back and says so, without asking for another run", async () => {
    const store = memoryStore();
    claimSale(store, "p1", "03.10.26 kl 21:00", ago(HOUR), 4);
    const some = await pendingItems(store, { now: NOW, photoCallsLeft: 2 });
    expect(some.claimLots).toHaveLength(2);
    expect(some).toMatchObject({ photoLimited: true, more: false });
    const none = await pendingItems(store, { now: NOW, photoCallsLeft: 0 });
    expect(none.claimLots).toHaveLength(0);
    expect(none).toMatchObject({ photoLimited: true, more: false });
    expect((await pendingItems(store, { now: NOW, photoCallsLeft: 10 })).photoLimited).toBe(false);
  });
});

describe("describeRun", () => {
  it("says what was read, what failed, what was skipped and why, and the photo limit", () => {
    const photo403 = { task: "claim-lot" as const, attempts: 3, lastError: "host error: HTTP Error 403: Forbidden", lastAt: NOW.toISOString() };
    const timeout = { ...photo403, lastError: "claude -p took more than 180 s" };
    expect(describeRun({ read: { endTimes: 0, bids: 0, claimLots: 5 }, failed: 0, skipped: [photo403, photo403], photoLimited: true })).toBe(
      "Read 5 claim lot photos · 2 skipped (photo unavailable) · photo limit reached, rest later",
    );
    expect(describeRun({ read: { endTimes: 1, bids: 2, claimLots: 0 }, failed: 1, skipped: [timeout], photoLimited: false })).toBe(
      "Read 1 end time and 2 bids · 1 failed, will retry · 1 skipped (failed 3 times)",
    );
    expect(describeRun({ read: { endTimes: 0, bids: 0, claimLots: 0 }, failed: 2, skipped: [], photoLimited: false })).toBe("Nothing read · 2 failed, will retry");
  });
});

// What goes to Claude at all (review M8): only what the rules couldn't read, and never something
// already answered. Uses the shared builders in tests/fakes/posts.ts (posts seen 4 October 2026).
describe("pendingItems: what the rules couldn't read", () => {
  const AT = new Date("2026-10-04T12:00:00Z");
  const opts = { myName: fx.ME, now: AT };
  const NO_END = "AUKSJON/BUDRUNDE-annonse\nMinimum budøkning: 10kr\nSluttid: når budene stilner";
  const CLAIM_NO_END = "Claim salg-annonse\nFastpris: Oppgis over hvert bilde";
  const FIXED = "Fastpris: 950kr\nObjektbeskrivelse: Charizard";
  const answer = (key: string) => ({ key, value: null, at: AT.toISOString() });

  it("end times of auctions and claim sales the rules couldn't read, from complete text only", async () => {
    const store = memoryStore({
      posts: [
        fx.post("1", NO_END),
        fx.post("2", fx.auctionText()), // The rules read it.
        fx.post("3", NO_END + "\nmer", { textComplete: false }), // Cut off: the end time may be in the rest.
        fx.post("4", CLAIM_NO_END),
        fx.post("5", FIXED), // No end time to look for.
        fx.post("6", "Hei alle sammen!"),
      ],
    });
    const { endTimes } = await pendingItems(store, opts);
    expect(endTimes.map((e) => e.text)).toEqual([NO_END, CLAIM_NO_END]);
    expect(endTimes.map((e) => e.id)).toEqual([0, 1]);
    expect(endTimes[0]).toMatchObject({ capturedAt: "2026-10-04T08:00:00Z", key: endTimeAnswerKey(NO_END) });
  });

  it("skips end-time text already answered, and asks once for the same text in two posts", async () => {
    const store = memoryStore({ posts: [fx.post("1", NO_END), fx.post("2", NO_END), fx.post("3", CLAIM_NO_END)], answers: [answer(endTimeAnswerKey(CLAIM_NO_END))] });
    expect((await pendingItems(store, opts)).endTimes.map((e) => e.text)).toEqual([NO_END]);
  });

  const unsure = `${fx.SELLER} 580?`;
  const auction = fx.capture("1", fx.auctionText(), [fx.lot(1, [fx.reply("Bidder A", unsure), fx.reply("Bidder B", `${fx.SELLER} 600`)])]);

  it("sends unsure bids from auctions, tagged with the seller", async () => {
    const { bids } = await pendingItems(memoryStore({ captures: [{ postId: "1", capture: auction }] }), opts);
    expect(bids).toEqual([{ id: 0, seller: fx.SELLER, text: unsure, key: bidAnswerKey(fx.SELLER, unsure) }]);
  });

  it("no bids from claim or fixed-price sales, and not once answered", async () => {
    const claim = { ...auction, post: { ...auction.post, text: CLAIM_NO_END } };
    expect((await pendingItems(memoryStore({ captures: [{ postId: "2", capture: claim }] }), opts)).bids).toEqual([]);
    const answered = memoryStore({ captures: [{ postId: "1", capture: auction }], answers: [answer(bidAnswerKey(fx.SELLER, unsure))] });
    expect((await pendingItems(answered, opts)).bids).toEqual([]);
  });

  const sale = (id: string, text = CLAIM_NO_END) =>
    fx.capture(id, text, [fx.lot(1, [fx.reply("Buyer A", "claim")]), fx.lot(2, [fx.reply(fx.ME, "claim Pikachu")]), fx.lot(3, [])]);

  it("every lot of claim and fixed-price sales, yours first; none from auctions", async () => {
    const { claimLots } = await pendingItems(memoryStore({ captures: [{ postId: "1", capture: sale("1") }] }), opts);
    expect(claimLots.map((l) => l.imageUrl)).toEqual([2, 1, 3].map((n) => `https://scontent.example/lot${n}.jpg`));
    expect(claimLots[0]).toMatchObject({ seller: fx.SELLER, replies: [{ author: fx.ME, text: "claim Pikachu" }] });
    expect((await pendingItems(memoryStore({ captures: [{ postId: "1", capture: sale("1", FIXED) }] }), opts)).claimLots).toHaveLength(3);
    expect((await pendingItems(memoryStore({ captures: [{ postId: "1", capture: sale("1", fx.auctionText()) }] }), opts)).claimLots).toEqual([]);
  });
});
