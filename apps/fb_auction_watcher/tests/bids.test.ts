import { describe, expect, it } from "vitest";
import { capturePostId, claimItems, interpretLots, readBid, summarizeLots, unsureReplies } from "../src/domain/bids";
import type { CapturedComment, CapturedReply, PostCapture } from "../src/shared/capture";

// Synthetic capture shaped like the real Gengar auction in samples/ (all names invented):
// replies out of time order, the seller's photo reply first, a bid placed under that photo
// reply, a bare-number bidder, a "." follower, a question bid, the seller's chat.
const SELLER = "Selger Testesen";
const ME = "Erik Johansen";
let seq = 100;
const reply = (author: string, text: string, opts: { id?: number; toReply?: boolean; image?: boolean } = {}): CapturedReply => {
  const id = String(3308000000000000 + (opts.id ?? seq++));
  return {
    id, url: `https://www.facebook.com/groups/g/posts/555/?comment_id=1&reply_comment_id=${id}`, author, text, timeText: "1 t",
    ariaLabel: `Svar fra ${author} på ${opts.toReply ? `${SELLER} sitt svar` : `${SELLER} sin kommentar`} for 1 time siden`,
    images: opts.image ? [{ src: "photo.jpg", alt: "" }] : [], truncated: false, rawText: text,
  };
};
const lot = (index: number, text: string, replies: CapturedReply[], author = SELLER): CapturedComment => ({
  id: String(9000 + index), url: `https://www.facebook.com/groups/g/posts/555/?comment_id=${9000 + index}`, author, text,
  timeText: "19 t", ariaLabel: `Kommentar fra ${author}`, images: [{ src: `lot${index}.jpg`, alt: "" }], truncated: false,
  rawText: text, index, hasImage: true, replies,
});

function capture(comments: CapturedComment[]): PostCapture {
  return {
    schemaVersion: 1, capturedAt: "2026-10-03T13:00:00Z", pageUrl: "https://www.facebook.com/groups/g/posts/555/", pageLang: "nb",
    commentSortLabel: "Alle kommentarer", commentSortAction: "already-all",
    post: { url: "", author: SELLER, text: "AUKSJON\nMinimum budøkning: 10kr", timeText: null, images: [], truncated: false },
    comments, stats: { expandClicks: 0, expandScrolls: 0, expandStoppedBecause: "done", topLevelComments: comments.length, commentsWithImage: comments.length, replies: 0, orphanReplies: 0 },
    warnings: [],
  };
}

const OPTS = { myName: ME, listingIncrement: 10, listingMinPrice: null };

describe("readBid", () => {
  it.each([
    [`${SELLER} 250`, { kind: "bid", amount: 250 }],
    ["850", { kind: "bid", amount: 850 }],
    [`${SELLER} 110kr`, { kind: "bid", amount: 110 }],
    [`${SELLER} 1.400`, { kind: "bid", amount: 1400 }],
    ["Selger 300", { kind: "bid", amount: 300 }], // First name only.
    ["bud 2.5k", { kind: "bid", amount: 2500 }],
    [`${SELLER} 580?`, { kind: "unsure", amount: 580 }],
    [`${SELLER} 200 sorry mente 250`, { kind: "unsure", amount: null }],
    [`${SELLER} .`, { kind: "none" }],
    ["", { kind: "none" }],
    ["Sendt PM, sjekk meldingsforespørsler", { kind: "none" }],
  ])("%s", (text, want) => expect(readBid(text, SELLER)).toEqual(want));
});

describe("interpretLots", () => {
  const c = capture([
    lot(0, "Gengar 151 holo\nMp 10kr", [
      reply(SELLER, "", { id: 1, image: true }),
      reply("Bidder A", `${SELLER} 120`, { id: 50, toReply: true }), // Shown first, placed late.
      reply("Bidder B", `${SELLER} 10`, { id: 10 }),
      reply("Bidder C", `${SELLER} 20`, { id: 20 }),
      reply("Bidder B", "30", { id: 30 }),
      reply(ME, `${SELLER} 40`, { id: 40 }),
      reply("Bidder D", `${SELLER} .`, { id: 41 }),
    ]),
    lot(1, "Gengar Arceus\nMp 1500kr", [
      reply("Bidder A", `${SELLER} 1500`, { id: 60 }),
      reply(ME, `${SELLER} 1600`, { id: 61 }),
      reply("Bidder C", `${SELLER} 1600`, { id: 62 }), // Same amount, later: doesn't count.
      reply("Bidder D", `${SELLER} 1000`, { id: 63 }), // Below the highest.
    ]),
    lot(2, "Gengar reverse holo\n700kr", [reply("Bidder A", `${SELLER} 580?`, { id: 70 }), reply(SELLER, "Bidder A den er grei", { id: 71 })]),
    lot(3, "Not a lot: posted by someone else", [], "Bidder Z"),
  ]);

  it("finds the seller's image comments as lots, with their start bids", () => {
    const lots = interpretLots(c, OPTS);
    expect(lots.map((l) => [l.title, l.startBid, l.increment])).toEqual([
      ["Gengar 151 holo", 10, 10], ["Gengar Arceus", 1500, 10], ["Gengar reverse holo", 700, 10],
    ]);
  });

  it("orders bids by when they were placed, never counts the seller, and flags bids under a reply", () => {
    const [l0] = interpretLots(c, OPTS);
    expect(l0.bids.map((b) => [b.bidder, b.amount])).toEqual([
      ["Bidder B", 10], ["Bidder C", 20], ["Bidder B", 30], [ME, 40], ["Bidder A", 120],
    ]);
    // Bidder A's 120 sits under the seller's photo reply: shown, not counted.
    expect(l0).toMatchObject({ highestBid: 40, highestBidder: ME, myHighestBid: 40, myStatus: "lead" });
    expect(l0.bids.at(-1)).toMatchObject({ underReply: true, valid: false });
  });

  it("first bidder wins a tie; bids under the highest don't count", () => {
    const [, l1] = interpretLots(c, OPTS);
    expect(l1).toMatchObject({ highestBid: 1600, highestBidder: ME, myStatus: "lead" });
    expect(l1.bids.map((b) => b.valid)).toEqual([true, true, false, false]);
  });

  it("counts a question bid as unsure until Claude has answered, then uses the answer", () => {
    expect(interpretLots(c, OPTS)[2]).toMatchObject({ unsureCount: 1, highestBid: null });
    const withAnswer = interpretLots(c, { ...OPTS, answer: () => 580 })[2];
    expect(withAnswer).toMatchObject({ unsureCount: 0 });
    // The only bid is under the start bid: still the highest, flagged (the seller accepted it).
    expect(withAnswer).toMatchObject({ highestBid: 580, highestBidder: "Bidder A", belowStart: true });
    expect(withAnswer.bids[0]).toMatchObject({ amount: 580, viaClaude: true, note: "below the start bid (700)" });
  });

  it("summarizes and lists unsure replies for Claude", () => {
    expect(summarizeLots(interpretLots(c, OPTS))).toEqual({ lots: 3, bids: 9, lead: 2, outbid: 0, unsure: 1, claims: 0, claimed: 0, check: 0 });
    expect(unsureReplies(c)).toEqual([{ seller: SELLER, text: `${SELLER} 580?` }]);
  });

  it("falls back to the main image poster when the post author doesn't match (old captures)", () => {
    const old = { ...c, post: { ...c.post, author: "Some Group Name" } };
    expect(interpretLots(old, OPTS)).toHaveLength(3);
  });

  it("reads a start bid mid-line, and titles a price-only lot by its number", () => {
    const c2 = capture([lot(0, "Holo, mp 30kr", []), lot(1, "Mp 15kr", []), lot(2, "Holo ( promo) mp 40", [])]);
    expect(interpretLots(c2, OPTS).map((l) => [l.title, l.startBid])).toEqual([
      ["Holo, mp 30kr", 30], ["Lot 2", 15], ["Holo ( promo) mp 40", 40],
    ]);
  });

  it("knows which post a capture belongs to", () => {
    expect(capturePostId(c)).toBe("555");
  });
});

describe("claims (claim sales and fixed price)", () => {
  it.each([
    [`${SELLER} claim morpeko`, { items: ["morpeko"], all: false }],
    [`${SELLER} claim Persian og Clefairy`, { items: ["persian", "clefairy"], all: false }],
    [`${SELLER} clame salazzle`, { items: ["salazzle"], all: false }],
    [`${SELLER} claim alle`, { items: [], all: true }],
    [`${SELLER} eevee, slowbro og slowpoke`, { items: ["eevee", "slowbro", "slowpoke"], all: false }],
    [`${SELLER} marowak og feraligatr`, { items: ["marowak", "feraligatr"], all: false }],
    [`${SELLER} claim`, { items: [], all: false }],
    [`${SELLER} .`, null],
    [`${SELLER} er denne fortsatt ledig?`, null],
  ])("%s", (text, want) => expect(claimItems(text, SELLER)).toEqual(want));

  const c = capture([
    // One photo, several cards: different people claim different cards.
    lot(0, "", [
      reply("Bidder A", `${SELLER} claim Persian og Clefairy`, { id: 10 }),
      reply(ME, `${SELLER} marowak og feraligatr`, { id: 11 }),
      reply(SELLER, "Bidder A sendt PM", { id: 12 }),
    ]),
    // Someone was earlier on the same card.
    lot(1, "", [reply("Bidder B", `${SELLER} claim feraligatr`, { id: 20 }), reply(ME, `${SELLER} claim Feraligatr`, { id: 21 })]),
    // "claim alle" first: everything in the photo is taken.
    lot(2, "", [reply("Bidder C", `${SELLER} claim alle`, { id: 30 }), reply(ME, `${SELLER} claim marowak`, { id: 31 })]),
    lot(3, "", [reply("Bidder D", `${SELLER} .`, { id: 40 })]),
  ]);
  const lots = interpretLots(c, { ...OPTS, claims: true });

  it("reads claims, in order, never the seller's own replies", () => {
    expect(lots[0].claims.map((x) => [x.claimer, x.items])).toEqual([
      ["Bidder A", ["persian", "clefairy"]],
      [ME, ["marowak", "feraligatr"]],
    ]);
    expect(lots[0].bids).toEqual([]);
  });

  it("you're first on what you named, even if others claimed other cards in the same photo", () => {
    expect(lots[0].myClaim).toBe("claimed");
  });

  it("flags a claim where someone was earlier on the same card, or claimed everything", () => {
    expect(lots[1].myClaim).toBe("check");
    expect(lots[2].myClaim).toBe("check");
    expect(lots[3]).toMatchObject({ myClaim: "none", claims: [] });
  });

  it("summarizes claims", () => {
    expect(summarizeLots(lots)).toMatchObject({ lots: 4, claims: 6, claimed: 1, check: 2, bids: 0 });
  });
});
