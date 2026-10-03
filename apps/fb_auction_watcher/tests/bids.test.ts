import { describe, expect, it } from "vitest";
import { capturePostId, claimItems, claimLotInput, claimLotsToRead, fullSizePhoto, interpretLots, myClaimLots, readBid, summarizeLots, untitledLotPhotos, unsureReplies } from "../src/domain/bids";
import { lotNameAnswerKey } from "../src/llm/prompts";
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
    expect(summarizeLots(interpretLots(c, OPTS))).toEqual({ lots: 3, bids: 9, lead: 2, outbid: 0, unclear: 0, unsure: 1, claims: 0, claimed: 0, check: 0 });
    expect(unsureReplies(c)).toEqual([{ seller: SELLER, text: `${SELLER} 580?` }]);
  });

  it("an unreadable reply after your highest bid makes 'Leading' unclear (review H4)", () => {
    const c2 = capture([
      lot(0, "Lot\nMp 100kr", [reply(ME, `${SELLER} 300`, { id: 10 }), reply("Bidder X", `${SELLER} 500?`, { id: 11 })]),
      // An unreadable reply *before* your bid doesn't matter.
      lot(1, "Lot\nMp 100kr", [reply("Bidder X", `${SELLER} 500?`, { id: 20 }), reply(ME, `${SELLER} 300`, { id: 21 })]),
      // A bid with no ID can't be placed in time: unclear too (review M4).
      lot(2, "Lot\nMp 100kr", [reply(ME, `${SELLER} 300`, { id: 30 }), { ...reply("Bidder Y", `${SELLER} 250`), id: null }]),
    ]);
    expect(interpretLots(c2, OPTS).map((l) => l.myStatus)).toEqual(["unclear", "lead", "unclear"]);
    // Once Claude has read "500?" as 500, you're plainly outbid.
    expect(interpretLots(c2, { ...OPTS, answer: () => 500 })[0].myStatus).toBe("outbid");
  });

  it("falls back to the main image poster when the post author doesn't match (old captures)", () => {
    const old = { ...c, post: { ...c.post, author: "Some Group Name" } };
    expect(interpretLots(old, OPTS)).toHaveLength(3);
  });

  it("reads the minimum raise mid-line too (review L3)", () => {
    const c3 = capture([lot(0, "Holo, mp 30kr, mb 20", [])]);
    expect(interpretLots(c3, OPTS)[0]).toMatchObject({ startBid: 30, increment: 20 });
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

describe("claim lots read by Claude (photo prices, who got what)", () => {
  it("asks Facebook's CDN for the full-size photo", () => {
    expect(fullSizePhoto("https://x.fbcdn.net/a_n.jpg?stp=dst-jpg_tt6&cstp=mx540x960&ctp=p240x240&_nc_cat=1&oh=z"))
      .toBe("https://x.fbcdn.net/a_n.jpg?stp=dst-jpg_tt6&cstp=mx540x960&_nc_cat=1&oh=z");
    expect(fullSizePhoto("https://x.fbcdn.net/a_n.jpg?ctp=p240x240")).toBe("https://x.fbcdn.net/a_n.jpg");
  });

  const c = capture([
    lot(0, "", [
      reply(SELLER, "", { id: 1, image: true }),
      reply("Bidder A", `${SELLER} kingler og rapidash`, { id: 10 }),
      reply(ME, `${SELLER} claim marowak og feraligator`, { id: 11 }),
    ]),
    lot(1, "", [reply("Bidder B", `${SELLER} claim pidgeot`, { id: 20 })]),
  ]);

  it("gives Claude the lot's replies (not the seller's), oldest first, for lots you claimed on", () => {
    const lots = myClaimLots(c, ME);
    expect(lots).toHaveLength(1);
    expect(lots[0].replies).toEqual([
      { author: "Bidder A", text: `${SELLER} kingler og rapidash` },
      { author: ME, text: `${SELLER} claim marowak og feraligator` },
    ]);
    expect(lots[0].imageUrl).toBe("lot0.jpg");
  });

  it("uses Claude's answer: the cards you won and their prices", () => {
    // As Claude answered for a real lot: every card, taken or still for sale.
    const answer = { cards: [
      { card: "Kadabra", price: 250, claimedBy: null }, { card: "Rapidash", price: 250, claimedBy: "Bidder A" },
      { card: "Marowak", price: 200, claimedBy: ME }, { card: "Castform", price: 200, claimedBy: null },
      { card: "Castform", price: 200, claimedBy: null }, { card: "Feraligatr", price: 200, claimedBy: ME },
      { card: "Pidgeot", price: 200, claimedBy: null }, { card: "Kingler", price: 250, claimedBy: "Bidder A" },
    ] };
    const [l0, l1] = interpretLots(c, { ...OPTS, claims: true, claimAnswer: (input) => (input.imageUrl === "lot0.jpg" ? answer : undefined) });
    expect(l0.myClaim).toBe("claimed");
    expect(l0.claimCards!.filter((x) => x.isMe).map((x) => [x.card, x.price])).toEqual([["Marowak", 200], ["Feraligatr", 200]]);
    expect(l0.available).toBe(4); // Still for sale: the lot stays open.
    expect(l1.claimCards).toBeNull();
    expect(l1.available).toBeNull(); // Not read by Claude yet.
  });

  it("a lot with every card claimed is sold out", () => {
    const [l0] = interpretLots(c, { ...OPTS, claims: true, claimAnswer: () => ({ cards: [{ card: "Marowak", price: 200, claimedBy: ME }] }) });
    expect(l0.available).toBe(0);
  });

  it("you claimed, but Claude says someone else got it: check", () => {
    const [l0] = interpretLots(c, { ...OPTS, claims: true, claimAnswer: () => ({ cards: [{ card: "Marowak", price: 200, claimedBy: "Bidder A" }] }) });
    expect(l0.myClaim).toBe("check");
  });

  it("reads every lot of a claim sale, yours first", () => {
    expect(claimLotsToRead(c, ME).map((x) => x.imageUrl)).toEqual(["lot0.jpg", "lot1.jpg"]);
  });

  it("knows a lot's input even with no replies", () => {
    expect(claimLotInput(c.comments[1], SELLER)?.replies).toHaveLength(1);
  });
});

describe("naming lots from their photo", () => {
  const c = capture([lot(0, "Mp 20kr", []), lot(1, "Charizard ex 199/165\nMp 500", []), lot(2, "", [])]);

  it("only lots whose text doesn't name them are sent to Claude", () => {
    expect(untitledLotPhotos(c)).toEqual(["lot0.jpg", "lot2.jpg"]);
  });

  it("uses Claude's name for those, keeps the seller's own text, and says where the name came from", () => {
    const names: Record<string, string> = { "lot0.jpg": "Pikachu (74/112)" };
    const lots = interpretLots(c, { ...OPTS, lotName: (url) => names[url] });
    expect(lots.map((l) => [l.title, l.namedByClaude, l.untitled])).toEqual([
      ["Pikachu (74/112)", true, true],
      ["Charizard ex 199/165", false, false],
      ["Lot 3", false, true], // Not named yet.
    ]);
  });

  it("caches by the photo's path (its query string changes per read)", () => {
    expect(lotNameAnswerKey("https://x.fbcdn.net/a_n.jpg?oh=1&oe=2")).toBe(lotNameAnswerKey("https://x.fbcdn.net/a_n.jpg?oh=9&oe=8"));
  });
});
