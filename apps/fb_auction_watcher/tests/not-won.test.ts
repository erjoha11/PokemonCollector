import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { sendWins, type SendDeps } from "../src/background/inbox";
import { buildWonPayload } from "../src/inbox/payload";
import { buildRows, lotStatus, notWonKey, notWonLots, wonBySeller } from "../src/pages/dashboard/model";
import { getNotWonMarks, markNotWon } from "../src/shared/settings";
import { fakeChrome, type FakeChrome } from "./fakes/chrome";
import { auctionText, capture, lot, ME, post, reply, SELLER } from "./fakes/posts";
import { memoryStore } from "./fakes/store";

// A manual "Not won" per lot (#329): for a win the rules got wrong (your bid came after the end and
// the seller said so). The lot leaves To pay and the inbox payload, and the mark can be undone.
// Invented names throughout.

const OTHER = "Kari Budgiver";
const AFTER_END = new Date("2026-10-04T20:00:00Z");

function seed() {
  // Ends 04.10.26 21:00 Oslo; read after the end. Lots 1-3 won; lot 3 has no comment ID.
  const lot3 = { ...lot(3, [reply(ME, `${SELLER} 20`)], "Charizard\nMp 10kr"), id: null };
  const c = capture(
    "2001",
    auctionText(),
    [
      lot(1, [reply(OTHER, `${SELLER} 40`), reply(ME, `${SELLER} 50`)], "Gengar\nMp 10kr"),
      lot(2, [reply(ME, `${SELLER} 30`)], "Pikachu\nMp 10kr"),
      lot3,
    ],
    { capturedAt: "2026-10-04T19:30:00Z" },
  );
  return { posts: [post("2001", c.post.text)], captures: [{ postId: "2001", capture: c }] };
}

function rows(notWonMarks: Record<string, string> = {}) {
  const { posts, captures } = seed();
  return buildRows(posts, AFTER_END, null, { captures: new Map(captures.map((c) => [c.postId, c.capture])), myName: ME, notWonMarks });
}

describe("Not won (your mark)", () => {
  it("keys a lot by post and comment ID, or its position when it has none", () => {
    expect(notWonKey("7", { commentId: "88", position: 2 })).toBe("7:88");
    expect(notWonKey("7", { commentId: null, position: 4 })).toBe("7:pos4");
  });

  it("without marks, all three lots are won", () => {
    const [r] = rows();
    expect(r.lots!.map((l) => lotStatus(r, l).key)).toEqual(["won", "won", "won"]);
    expect(wonBySeller([r])[0].items).toHaveLength(3);
  });

  it("a marked lot is 'not-won': off To pay, out of the payload, in the Not won list", () => {
    const at = "2026-10-04T21:00:00.000Z";
    const all = rows({ "2001:9002": at, "2001:pos3": at });
    const [r] = all;
    expect(r.lots!.map((l) => lotStatus(r, l).key)).toEqual(["won", "not-won", "not-won"]);
    expect(lotStatus(r, r.lots![1]).label).toBe("Not won (your mark)");
    const [g] = wonBySeller(all);
    expect(g.items.map((i) => i.label)).toEqual(["1. Gengar"]);
    expect(g.kr).toBe(50);
    const refs = buildWonPayload(all, {}, AFTER_END).items.map((i) => i.external_ref);
    expect(refs).toEqual(["fbaw:2001:9001"]);
    expect(notWonLots(all).map((i) => [i.key, i.lot.title])).toEqual([["2001:9002", "Pikachu"], ["2001:pos3", "Charizard"]]);
  });

  it("a mark on a lot you didn't bid on changes nothing", () => {
    const { posts, captures } = seed();
    const [r] = buildRows(posts, AFTER_END, null, { captures: new Map(captures.map((c) => [c.postId, c.capture])), myName: "Someone Else", notWonMarks: { "2001:9001": "2026-10-04T21:00:00Z" } });
    expect(lotStatus(r, r.lots![0]).key).toBe("none");
    expect(notWonLots([r])).toEqual([]);
  });

  describe("stored in chrome.storage.local, undoable", () => {
    let fake: FakeChrome;
    beforeEach(() => {
      fake = fakeChrome();
      vi.stubGlobal("chrome", fake.chrome);
    });
    afterEach(() => vi.unstubAllGlobals());

    it("marks and undoes", async () => {
      await markNotWon("2001:9002", true);
      expect(Object.keys(await getNotWonMarks())).toEqual(["2001:9002"]);
      await markNotWon("2001:9002", false);
      expect(await getNotWonMarks()).toEqual({});
    });

    it("the service worker's send leaves marked lots out", async () => {
      await fake.local.set({ settings: { myName: ME, inboxUrl: "https://inv.example.com", inboxToken: "tok" } });
      await markNotWon("2001:9002", true);
      const calls: RequestInit[] = [];
      const deps: SendDeps = {
        now: () => AFTER_END,
        hasPermission: async () => true,
        fetch: (async (_url: string, init: RequestInit) => {
          calls.push(init);
          return new Response(JSON.stringify({ added: 2 }), { status: 200 });
        }) as typeof fetch,
      };
      const state = await sendWins(memoryStore(seed()), deps);
      const body = JSON.parse(calls[0].body as string);
      expect(body.items.map((i: { external_ref: string }) => i.external_ref).sort()).toEqual(["fbaw:2001:9001", "fbaw:2001:pos3"]);
      expect(state).toMatchObject({ ok: true, count: 2 });
    });
  });
});
