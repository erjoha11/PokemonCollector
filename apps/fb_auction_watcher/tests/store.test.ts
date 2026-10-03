import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { FeedPost } from "../src/shared/feed";
import { idbStore } from "../src/store";
import { auctionText, capture, lot, reply, SELLER } from "./fakes/posts";

// The IndexedDB store (src/store/index.ts, review M8) on fake-indexeddb: a fresh, empty
// database per test.

beforeEach(() => {
  vi.stubGlobal("indexedDB", new IDBFactory());
});

const feedPost = (id: string, over: Partial<FeedPost> = {}): FeedPost => ({
  id, url: `https://www.facebook.com/groups/g/posts/${id}/`, groupSlug: "g", sellerName: SELLER,
  text: "AUKSJON\nfull text", textComplete: true, thumbnailUrl: "thumb.jpg", ...over,
});
const t1 = new Date("2026-10-04T10:00:00Z");
const t2 = new Date("2026-10-04T11:00:00Z");

describe("savePosts", () => {
  it("counts new and known posts; firstSeenAt stays, lastSeenAt moves", async () => {
    const store = idbStore();
    expect(await store.savePosts([feedPost("1"), feedPost("2")], t1)).toEqual({ added: 2, updated: 0 });
    expect(await store.savePosts([feedPost("2"), feedPost("3")], t2)).toEqual({ added: 1, updated: 1 });
    const byId = new Map((await store.allPosts()).map((p) => [p.id, p]));
    expect([...byId.keys()].sort()).toEqual(["1", "2", "3"]);
    expect(byId.get("2")).toMatchObject({ firstSeenAt: t1.toISOString(), lastSeenAt: t2.toISOString() });
    expect(byId.get("3")).toMatchObject({ firstSeenAt: t2.toISOString(), lastSeenAt: t2.toISOString() });
  });

  it("keeps complete text over a later cut-off rendering of the same post", async () => {
    const store = idbStore();
    await store.savePosts([feedPost("1", { text: "AUKSJON\nfull text\nSluttid: 04.10.26 kl 21:00" })], t1);
    await store.savePosts([feedPost("1", { text: "AUKSJON\nfull … Se mer", textComplete: false })], t2);
    const [p] = await store.allPosts();
    expect(p).toMatchObject({ text: "AUKSJON\nfull text\nSluttid: 04.10.26 kl 21:00", textComplete: true, lastSeenAt: t2.toISOString() });
  });

  it("newer text wins when it's complete, or when the old one wasn't", async () => {
    const store = idbStore();
    await store.savePosts([feedPost("1", { text: "cut … Se mer", textComplete: false }), feedPost("2", { text: "old" })], t1);
    await store.savePosts([feedPost("1", { text: "cut again … Se mer", textComplete: false }), feedPost("2", { text: "edited" })], t2);
    await store.savePosts([feedPost("3", { text: "cut", textComplete: false })], t1);
    await store.savePosts([feedPost("3", { text: "whole" })], t2);
    const text = Object.fromEntries((await store.allPosts()).map((p) => [p.id, [p.text, p.textComplete]]));
    expect(text).toEqual({ 1: ["cut again … Se mer", false], 2: ["edited", true], 3: ["whole", true] });
  });

  it("keeps the thumbnail and seller when a later rendering lacks them", async () => {
    const store = idbStore();
    await store.savePosts([feedPost("1")], t1);
    await store.savePosts([feedPost("1", { thumbnailUrl: null, sellerName: null })], t2);
    expect((await store.allPosts())[0]).toMatchObject({ thumbnailUrl: "thumb.jpg", sellerName: SELLER });
  });
});

describe("captures, answers, meta", () => {
  it("round-trip; a capture is replaced by the next save for that post", async () => {
    const store = idbStore();
    const first = capture("1", auctionText(), [lot(1, [reply("Bidder A", `${SELLER} 10`)])]);
    const second = { ...first, capturedAt: "2026-10-04T13:00:00Z", reads: 2 };
    expect(await store.getCapture("1")).toBeNull();
    await store.saveCapture("1", first);
    await store.saveCapture("2", first);
    await store.saveCapture("1", second);
    expect(await store.getCapture("1")).toEqual(second);
    expect((await store.allCaptures()).map((c) => c.postId).sort()).toEqual(["1", "2"]);

    await store.saveAnswers([{ key: "bid:a", value: 580, at: "x" }, { key: "end-time:b", value: null, at: "x" }]);
    await store.saveAnswers([{ key: "bid:a", value: 600, at: "y" }]);
    expect(Object.fromEntries((await store.allAnswers()).map((a) => [a.key, a.value]))).toEqual({ "bid:a": 600, "end-time:b": null });

    expect(await store.getMeta("lastFeedReadAt")).toBeNull();
    await store.setMeta("lastFeedReadAt", "2026-10-04T12:00:00Z");
    expect(await store.getMeta("lastFeedReadAt")).toBe("2026-10-04T12:00:00Z");
  });

  it("two store instances (worker and overview page) see the same data", async () => {
    await idbStore().savePosts([feedPost("1")], t1);
    expect((await idbStore().allPosts()).map((p) => p.id)).toEqual(["1"]);
  });
});

describe("upgrade", () => {
  it("v1 (posts, meta) → v2 keeps the posts and adds captures and answers", async () => {
    await new Promise<void>((resolve, reject) => {
      const req = indexedDB.open("fb-auction-watcher", 1);
      req.onupgradeneeded = () => {
        req.result.createObjectStore("posts", { keyPath: "id" });
        req.result.createObjectStore("meta").put("2026-10-01T00:00:00Z", "lastFeedReadAt");
      };
      req.onsuccess = () => {
        const tx = req.result.transaction("posts", "readwrite");
        tx.objectStore("posts").put({ ...feedPost("old"), firstSeenAt: t1.toISOString(), lastSeenAt: t1.toISOString() });
        tx.oncomplete = () => (req.result.close(), resolve());
      };
      req.onerror = () => reject(req.error);
    });
    const store = idbStore();
    expect((await store.allPosts()).map((p) => p.id)).toEqual(["old"]);
    expect(await store.getMeta("lastFeedReadAt")).toBe("2026-10-01T00:00:00Z");
    await store.saveCapture("old", capture("old", auctionText(), []));
    await store.saveAnswers([{ key: "k", value: 1, at: "x" }]);
    expect(await store.allCaptures()).toHaveLength(1);
    expect(await store.allAnswers()).toHaveLength(1);
  });
});
