import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { sendWins, type SendDeps } from "../src/background/inbox";
import { buildWonPayload, externalRef } from "../src/inbox/payload";
import { buildRows } from "../src/pages/dashboard/model";
import { inboxOrigin, type WonState } from "../src/shared/settings";
import { fakeChrome, type FakeChrome } from "./fakes/chrome";
import { auctionText, capture, lot, ME, post, reply, SELLER } from "./fakes/posts";
import { memoryStore } from "./fakes/store";

// "Send wins to inventory" (#309): the v1 payload (only your own wins leave the browser) and the
// service worker's send. Invented names throughout; the payload is also written to the committed
// fixture tests/fixtures/won-inbox.v1.json (repo root), which tcg_inventory's parser reads in
// tests/test_cross_app_won_inbox.py. Never build it from samples/.

const OTHER_BIDDER = "Kari Budgiver";
const OTHER_CLAIMER = "Ola Klemmesen";
const TERMS = "\nSender med post (pris m/emballasje): 50kr med sporing\nBetalingsalternativ: Vipps eller bank";
const AFTER_END = new Date("2026-10-04T20:00:00Z");

function seed() {
  // An auction (ends 04.10.26 21:00 Oslo): lot 1 won, lot 2 lost to someone else, lot 3 won but
  // without a comment ID of its own (the pos<n> fallback).
  const lot3 = { ...lot(3, [reply(ME, `${SELLER} 20`)], "Charizard\nMp 10kr"), id: null };
  const auction = capture(
    "1001",
    auctionText() + TERMS,
    [
      lot(1, [reply(OTHER_BIDDER, `${SELLER} 40`), reply(ME, `${SELLER} 50`)], "Gengar 151 reverse holo\nMp 10kr"),
      lot(2, [reply(ME, `${SELLER} 30`), reply(OTHER_BIDDER, `${SELLER} 40 kommer du med mer?`)], "Pikachu\nMp 10kr"),
      lot3,
    ],
    { capturedAt: "2026-10-04T19:30:00Z" },
  );
  // A claim sale: you claimed lot 1 first; lot 2 went to someone else.
  const claimText = "Claim salg-annonse\nSluttid: 03.10.26 kl 20:00\nPris: 25kr per stk" + TERMS;
  const claim = capture("1002", claimText, [
    lot(11, [reply(ME, "claim"), reply(OTHER_CLAIMER, "claim")], "Eevee\n25kr"),
    lot(12, [reply(OTHER_CLAIMER, "claim")], "Snorlax\n25kr"),
    // No price in the text (it's on the photo, which Claude hasn't read): price unknown.
    lot(13, [reply(ME, "claim")], "Mew"),
  ], { capturedAt: "2026-10-03T19:00:00Z" });
  const posts = [post("1001", auction.post.text), post("1002", claim.post.text, { firstSeenAt: "2026-10-03T08:00:00Z" })];
  return { posts, captures: [{ postId: "1001", capture: auction }, { postId: "1002", capture: claim }] };
}

const wonState: WonState = { "1001": { paidAt: "2026-10-04T21:00:00.000Z", receivedAt: null } };

function payloadFromSeed() {
  const { posts, captures } = seed();
  const rows = buildRows(posts, AFTER_END, null, { captures: new Map(captures.map((c) => [c.postId, c.capture])), myName: ME });
  return buildWonPayload(rows, wonState, AFTER_END);
}

describe("buildWonPayload", () => {
  it("one item per won lot, with the v1 envelope", () => {
    const p = payloadFromSeed();
    expect(p).toMatchObject({ format: "fbaw-won", version: 1, sent_at: "2026-10-04T20:00:00.000Z" });
    expect(p.items.map((i) => i.external_ref).sort()).toEqual(["fbaw:1001:9001", "fbaw:1001:pos3", "fbaw:1002:9011", "fbaw:1002:9013"]);
  });

  it("an unknown price is null, not 0", () => {
    const mew = payloadFromSeed().items.find((i) => i.external_ref === "fbaw:1002:9013")!;
    expect(mew).toMatchObject({ label: "3. Mew", price: null });
  });

  it("carries seller, type, Oslo end date, links, label, price, the seller's terms and your marks", () => {
    const won = payloadFromSeed().items.find((i) => i.external_ref === "fbaw:1001:9001")!;
    expect(won).toEqual({
      external_ref: "fbaw:1001:9001",
      seller: SELLER,
      sale_type: "auction",
      ended_on: "2026-10-04",
      post_url: "https://www.facebook.com/groups/g/posts/1001/",
      lot_url: "https://www.facebook.com/groups/g/posts/1001/?comment_id=9001",
      label: "1. Gengar 151 reverse holo",
      price: 50,
      shipping_text: "50kr med sporing",
      payment_text: "Vipps eller bank",
      paid_at: "2026-10-04T21:00:00.000Z",
      received_at: null,
    });
    const claim = payloadFromSeed().items.find((i) => i.external_ref === "fbaw:1002:9011")!;
    expect(claim).toMatchObject({ sale_type: "claim", ended_on: "2026-10-03", paid_at: null });
  });

  it("a lot without a comment ID links to the post and gets a position ref", () => {
    const item = payloadFromSeed().items.find((i) => i.external_ref === "fbaw:1001:pos3")!;
    expect(item.lot_url).toBe("https://www.facebook.com/groups/g/posts/1001/");
    expect(externalRef("7", { commentId: null, position: 4 })).toBe("fbaw:7:pos4");
    expect(externalRef("7", { commentId: "88", position: 4 })).toBe("fbaw:7:88");
  });

  it("the end date is Oslo's: 23:30 Oslo on the 4th is still the 4th", () => {
    const readLater = "2026-10-05T06:00:00Z"; // a complete read after the moved end, so the lots still count as won
    const late = [{ ...buildRowsOne(), endsAtMs: Date.parse("2026-10-04T21:30:00Z"), lastCompleteReadAt: readLater }];
    expect(buildWonPayload(late, {}, AFTER_END).items[0].ended_on).toBe("2026-10-04");
    const afterMidnight = [{ ...buildRowsOne(), endsAtMs: Date.parse("2026-10-04T22:30:00Z"), lastCompleteReadAt: readLater }];
    expect(buildWonPayload(afterMidnight, {}, AFTER_END).items[0].ended_on).toBe("2026-10-05");
    // Marked ended by you before the end time: your mark's date.
    const marked = [{ ...buildRowsOne(), endedByYouAt: "2026-10-02T10:00:00Z" }];
    expect(buildWonPayload(marked, {}, AFTER_END).items[0].ended_on).toBe("2026-10-02");
  });

  it("never carries other people's names, comments or raw text", () => {
    const json = JSON.stringify(payloadFromSeed());
    for (const s of [OTHER_BIDDER, OTHER_CLAIMER, ME, "kommer du med mer", "scontent", "Mp 10kr", "rawText", "bids", "claims"]) {
      expect(json).not.toContain(s);
    }
  });

  it("writes the committed cross-app fixture (tests/fixtures/won-inbox.v1.json at the repo root)", async () => {
    await expect(`${JSON.stringify(payloadFromSeed(), null, 2)}\n`).toMatchFileSnapshot("../../../tests/fixtures/won-inbox.v1.json");
  });
});

/** The seed's auction row on its own (its end time is overridden by the test). */
function buildRowsOne() {
  const { posts, captures } = seed();
  return buildRows([posts[0]], AFTER_END, null, { captures: new Map([[captures[0].postId, captures[0].capture]]), myName: ME })[0];
}

describe("inboxOrigin", () => {
  it("https anywhere, http only on localhost; returns the origin", () => {
    expect(inboxOrigin("https://my-app.vercel.app/orders/purchased")).toBe("https://my-app.vercel.app");
    expect(inboxOrigin("http://localhost:8000")).toBe("http://localhost:8000");
    expect(inboxOrigin("http://127.0.0.1:8000/")).toBe("http://127.0.0.1:8000");
    expect(inboxOrigin("http://my-app.vercel.app")).toBeNull();
    expect(inboxOrigin("https://user:pw@my-app.vercel.app")).toBeNull();
    expect(inboxOrigin("javascript:alert(1)")).toBeNull();
    expect(inboxOrigin("")).toBeNull();
  });
});

describe("sendWins (service worker)", () => {
  let fake: FakeChrome;
  beforeEach(() => {
    fake = fakeChrome();
    vi.stubGlobal("chrome", fake.chrome);
  });
  afterEach(() => vi.unstubAllGlobals());

  const deps = (over: Partial<SendDeps> = {}): SendDeps & { calls: { url: string; init: RequestInit }[] } => {
    const calls: { url: string; init: RequestInit }[] = [];
    return {
      calls,
      now: () => AFTER_END,
      hasPermission: async () => true,
      fetch: (async (url: string, init: RequestInit) => {
        calls.push({ url, init });
        return new Response(JSON.stringify({ status: "ok", received: 4, added: 3, updated: 1, unchanged: 0, kept: 0 }), { status: 200 });
      }) as typeof fetch,
      ...over,
    };
  };
  const configure = (over: Record<string, unknown> = {}) =>
    fake.local.set({ settings: { myName: ME, inboxUrl: "https://inv.example.com", inboxToken: "tok-123", ...over }, wonState });

  it("posts the payload with the Bearer token and keeps the result", async () => {
    await configure();
    const d = deps();
    const state = await sendWins(memoryStore(seed()), d);
    expect(d.calls).toHaveLength(1);
    expect(d.calls[0].url).toBe("https://inv.example.com/inbox/fb-wins");
    expect(d.calls[0].init.method).toBe("POST");
    expect((d.calls[0].init.headers as Record<string, string>).Authorization).toBe("Bearer tok-123");
    expect(d.calls[0].init.redirect).toBe("error");
    const body = JSON.parse(d.calls[0].init.body as string);
    expect(body.items).toHaveLength(4);
    expect(state).toEqual({ lastAt: AFTER_END.toISOString(), ok: true, count: 4, outcome: "3 new, 1 updated" });
    expect((await fake.local.get("inboxState")).inboxState).toEqual(state);
  });

  it("only the won auctions you ticked are sent, and they're remembered as sent", async () => {
    await configure();
    const d = deps();
    const state = await sendWins(memoryStore(seed()), d, ["1002"]);
    const body = JSON.parse(d.calls[0].init.body as string);
    expect(body.items.map((i: { external_ref: string }) => i.external_ref.split(":")[1])).toEqual(["1002", "1002"]);
    expect(state).toMatchObject({ ok: true, count: 2 });
    expect((await fake.local.get("inboxSent")).inboxSent).toEqual({ "1002": AFTER_END.toISOString() });
  });

  it("nothing ticked: nothing sent", async () => {
    await configure();
    const d = deps();
    expect(await sendWins(memoryStore(seed()), d, [])).toMatchObject({ ok: false, outcome: "Tick the won auctions to send first." });
    expect(d.calls).toHaveLength(0);
  });

  it("a refused send isn't marked as sent", async () => {
    await configure();
    const fetch = (async () => new Response("{}", { status: 401 })) as typeof globalThis.fetch;
    await sendWins(memoryStore(seed()), deps({ fetch }), ["1001"]);
    expect((await fake.local.get("inboxSent")).inboxSent).toBeUndefined();
  });

  it("not set up, or no permission: nothing is sent, and it says what to do", async () => {
    const d = deps();
    await configure({ inboxUrl: "" });
    expect((await sendWins(memoryStore(seed()), d)).outcome).toMatch(/address in Settings/);
    await configure({ inboxToken: "" });
    expect((await sendWins(memoryStore(seed()), d)).outcome).toMatch(/token in Settings/);
    await configure();
    const state = await sendWins(memoryStore(seed()), { ...d, hasPermission: async () => false });
    expect(state).toMatchObject({ ok: false, outcome: expect.stringMatching(/No permission/) });
    expect(d.calls).toHaveLength(0);
  });

  it("nothing won: nothing sent", async () => {
    await configure();
    const d = deps();
    expect(await sendWins(memoryStore(), d)).toMatchObject({ ok: true, count: 0, outcome: "Nothing won to send." });
    expect(d.calls).toHaveLength(0);
  });

  it("a refusal shows the server's reason", async () => {
    await configure();
    const fetch = (async () => new Response(JSON.stringify({ status: "error", error: "Missing or wrong token" }), { status: 401 })) as typeof globalThis.fetch;
    expect(await sendWins(memoryStore(seed()), deps({ fetch }))).toMatchObject({ ok: false, count: 4, outcome: "HTTP 401: Missing or wrong token" });
  });

  it("a network failure is kept as an error", async () => {
    await configure();
    const fetch = (async () => {
      throw new TypeError("Failed to fetch");
    }) as typeof globalThis.fetch;
    expect(await sendWins(memoryStore(seed()), deps({ fetch }))).toMatchObject({ ok: false, outcome: "Couldn't reach https://inv.example.com: Failed to fetch" });
  });
});
