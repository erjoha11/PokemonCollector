import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Store } from "../src/store";
import { fakeChrome, type FakeChrome } from "./fakes/chrome";
import { auctionText, capture, lot, ME, reply, SELLER } from "./fakes/posts";

// Message routing in the service worker (src/background/index.ts, review M8): a few messages
// end to end, through the fake `chrome` and fake-indexeddb. Loading the module wires its
// listeners, so each test gets a fresh copy.

let fake: FakeChrome;
let store: Store;
let reader: typeof import("../src/background/reader");

beforeEach(async () => {
  // Only setTimeout: fake-indexeddb schedules with setImmediate, and the Claude debounce must not
  // fire after the test.
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  vi.stubGlobal("indexedDB", new IDBFactory());
  fake = fakeChrome();
  vi.stubGlobal("chrome", fake.chrome);
  await fake.local.set({ settings: { autoScan: false, myName: ME, useClaude: false } });
  vi.resetModules();
  await import("../src/background/index");
  store = (await import("../src/store")).idbStore();
  reader = await import("../src/background/reader");
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const fbUrl = (id: string) => `https://www.facebook.com/groups/g/posts/${id}/`;
const feedPost = (id: string, textComplete = true) => ({
  id, url: fbUrl(id), groupSlug: "g", sellerName: SELLER, text: "AUKSJON", textComplete, thumbnailUrl: null,
});

describe("worker messages", () => {
  it("save-feed-posts: stores them, answers with the counts, tells open pages", async () => {
    const reply1 = await fake.sendToWorker({ type: "fbaw/save-feed-posts", posts: [feedPost("1"), feedPost("2")], seenAt: "2026-10-04T12:00:00Z" }, 5);
    expect(reply1).toEqual({ added: 2, updated: 0 });
    const reply2 = await fake.sendToWorker({ type: "fbaw/save-feed-posts", posts: [feedPost("2")], seenAt: "2026-10-04T12:10:00Z" }, 5);
    expect(reply2).toEqual({ added: 0, updated: 1 });
    expect(await store.getMeta("lastFeedReadAt")).toBe("2026-10-04T12:10:00Z");
    expect(fake.runtimeMessages).toContainEqual({ type: "fbaw/store-updated", added: 2, updated: 0 });
  });

  it("get-known-posts: every stored ID, and which have complete text", async () => {
    await store.savePosts([feedPost("1"), feedPost("2", false)], new Date());
    const known = (await fake.sendToWorker({ type: "fbaw/get-known-posts" }, 5)) as { ids: string[]; completeIds: string[] };
    expect(known.ids.sort()).toEqual(["1", "2"]);
    expect(known.completeIds).toEqual(["1"]);
  });

  it("save-post-capture: merges the read into the store and adds its post", async () => {
    const read = capture("555", auctionText(), [lot(1, [reply(ME, `${SELLER} 40`)])]);
    await fake.sendToWorker({ type: "fbaw/save-post-capture", capture: read }, 5);
    await vi.waitFor(async () => expect(await store.getCapture("555")).not.toBeNull());
    expect(await store.getCapture("555")).toMatchObject({ reads: 1, completeAt: read.capturedAt });
    await vi.waitFor(async () => expect((await store.allPosts()).map((p) => p.id)).toEqual(["555"]));
    expect((await store.allPosts())[0]).toMatchObject({ url: fbUrl("555"), groupSlug: "g", textComplete: true, firstSeenAt: new Date(read.capturedAt).toISOString() });
  });

  it("queue-read opens the post; read-done from that tab finishes it", async () => {
    await fake.sendToWorker({ type: "fbaw/queue-read", postId: "1", url: fbUrl("1") });
    await vi.waitFor(async () => expect((await reader.getReaderState()).current?.tabId).toBe(100));
    await fake.sendToWorker({ type: "fbaw/read-done", ok: true, outcome: "2 lots" }, 999); // Not the reader's tab.
    await fake.sendToWorker({ type: "fbaw/read-done", ok: true, outcome: "2 lots" }, 100);
    await vi.waitFor(async () => expect((await reader.getReaderState()).lastOutcome).toBe("Read: 2 lots"));
    expect((await reader.getReaderState()).current).toBeNull();
  });

  it("a background read's tab is closed after it's recorded as read (not as 'its tab was closed')", async () => {
    await fake.sendToWorker({ type: "fbaw/queue-read", postId: "1", url: fbUrl("1") });
    await vi.waitFor(async () => expect((await reader.getReaderState()).current?.tabId).toBe(100));
    expect(fake.created).toEqual([{ url: fbUrl("1"), active: false }]); // Read: a background tab.
    await fake.sendToWorker({ type: "fbaw/read-done", ok: true, outcome: "2 lots" }, 100);
    await vi.waitFor(() => expect(fake.tabs.has(100)).toBe(false));
    expect((await reader.getReaderState()).lastOutcome).toBe("Read: 2 lots");
  });

  it("closing the reader's tab counts the read as done and frees the slot", async () => {
    await fake.sendToWorker({ type: "fbaw/queue-read", postId: "1", url: fbUrl("1") });
    await vi.waitFor(async () => expect((await reader.getReaderState()).current?.tabId).toBe(100));
    fake.closeTab(100);
    await vi.waitFor(async () => expect((await reader.getReaderState()).lastOutcome).toBe("Couldn't read: its tab was closed"));
    await vi.waitFor(async () => expect(fake.session.data.get("facebookSlot")).toBeNull());
  });
});
