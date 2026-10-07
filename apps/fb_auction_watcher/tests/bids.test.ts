import { describe, expect, it } from "vitest";
import { capturePostId, claimItems, lotStartBid, normalizeCondition, lotTextInfo, lotTextPrice, claimLotsToRead, fullSizePhoto, interpretLots, namesCard, readBid, summarizeLots, tagAsSeller, untitledLotPhotos, unsureReplies, type PhotoCards } from "../src/domain/bids";
import { claimMatchRequest, claimPhotoAnswerKey, claimPhotoRequest, lotNameAnswerKey, lotNameRequest } from "../src/llm/prompts";
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

  it("reads a start bid mid-line, and titles a lot whose text names nothing by its number", () => {
    const c2 = capture([lot(0, "Holo, mp 30kr", []), lot(1, "Mp 15kr", []), lot(2, "Holo ( promo) mp 40", [])]);
    expect(interpretLots(c2, OPTS).map((l) => [l.title, l.startBid, l.untitled])).toEqual([
      ["Lot 1", 30, true], ["Lot 2", 15, true], ["Lot 3", 40, true],
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

describe("claim lots: Claude reads the photo once, the rules match the claims", () => {
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
  // As Claude read a real lot's photo: every card and its price, nothing about who claimed what.
  const PHOTO: PhotoCards = [
    { card: "Kadabra", price: 250 }, { card: "Rapidash", price: 250 }, { card: "Marowak", price: 200 }, { card: "Castform", price: 200 },
    { card: "Castform", price: 200 }, { card: "Feraligatr", price: 200 }, { card: "Pidgeot", price: 200 }, { card: "Kingler", price: 250 },
  ];

  it("sends Claude only the photo and the lot's text: no replies, no names; the lots you claimed on first", () => {
    expect(claimLotsToRead(c, ME)).toEqual([{ imageUrl: "lot0.jpg", lotText: "" }, { imageUrl: "lot1.jpg", lotText: "" }]);
    const r = claimPhotoRequest({ imageUrl: "lot0.jpg", lotText: "Holo 5kr per stk" });
    expect(r).toMatchObject({ task: "claim-lot", model: "sonnet", images: ["lot0.jpg"] });
    expect(r.input).not.toContain(SELLER);
  });

  it("matches the claims by the rules, misspellings too: the cards you won and their prices", () => {
    const [l0, l1] = interpretLots(c, { ...OPTS, claims: true, claimPhoto: (input) => (input.imageUrl === "lot0.jpg" ? PHOTO : undefined) });
    expect(l0.myClaim).toBe("claimed");
    expect(l0.claimCards!.filter((x) => x.isMe).map((x) => [x.card, x.price])).toEqual([["Marowak", 200], ["Feraligatr", 200]]);
    expect(l0.claimCards!.filter((x) => x.claimedBy === "Bidder A").map((x) => x.card)).toEqual(["Rapidash", "Kingler"]);
    expect(l0.available).toBe(4); // Still for sale: the lot stays open.
    expect(l0.claimMatchInput).toBeNull(); // The rules were sure: nothing for Claude.
    expect(l0.title).toBe("8 cards"); // Named from the photo's cards: no second look at it.
    expect(l1.claimCards).toBeNull();
    expect(l1.available).toBeNull(); // Photo not read yet.
  });

  it("first to claim a card gets it; a second copy goes to the next one", () => {
    const sale = capture([
      lot(0, "", [
        reply("Bidder A", "claim castform", { id: 10 }),
        reply(ME, "castform", { id: 11 }),
        reply("Bidder B", "den til venstre", { id: 12 }), // "The one on the left": names no card, so Claude.
      ]),
    ]);
    const [l] = interpretLots(sale, { ...OPTS, claims: true, claimPhoto: () => [{ card: "Castform", price: 10 }, { card: "Castform", price: 10 }] });
    expect(l.claimMatchInput).toEqual({
      cards: ["Castform", "Castform"],
      claims: [{ who: "Claimer 1", text: "claim castform" }, { who: "Me", text: "castform" }, { who: "Claimer 2", text: "den til venstre" }],
    });
    const [answered] = interpretLots(sale, { ...OPTS, claims: true, claimPhoto: () => [{ card: "Castform", price: 10 }, { card: "Castform", price: 10 }], claimMatch: () => ["Claimer 1", "Me"] });
    expect(answered.claimCards!.map((x) => [x.claimedBy, x.isMe])).toEqual([["Bidder A", false], [ME, true]]);
    expect(answered.myClaim).toBe("claimed");
  });

  it("'alle' takes what's left, and a bare claim takes a one-card lot", () => {
    const all = capture([lot(0, "", [reply("Bidder A", "marowak", { id: 10 }), reply(ME, "alle", { id: 11 })])]);
    const [l] = interpretLots(all, { ...OPTS, claims: true, claimPhoto: () => PHOTO.slice(0, 3) });
    expect(l.claimCards!.map((x) => x.claimedBy)).toEqual([ME, ME, "Bidder A"]);
    const one = capture([lot(0, "", [reply(ME, "claim", { id: 10 })])]);
    expect(interpretLots(one, { ...OPTS, claims: true, claimPhoto: () => [{ card: "Mew", price: 50 }] })[0].claimCards![0].isMe).toBe(true);
  });

  it("you claimed, but someone was first: check", () => {
    const sale = capture([lot(0, "", [reply("Bidder A", "marowak", { id: 10 }), reply(ME, "marowak", { id: 11 })])]);
    const [l] = interpretLots(sale, { ...OPTS, claims: true, claimPhoto: () => [{ card: "Marowak", price: 200 }] });
    expect(l.myClaim).toBe("check");
    expect(l.available).toBe(0); // Sold out.
  });

  it("Claude matches without names: the seller is '@Seller', claimers are labels", () => {
    expect(tagAsSeller(`${SELLER} claim zard`, SELLER)).toBe("@Seller claim zard");
    expect(tagAsSeller("Selger: zard", SELLER)).toBe("@Seller: zard"); // First name alone.
    const r = claimMatchRequest([{ id: 0, cards: ["Charizard"], claims: [{ who: "Me", text: "@Seller zard" }] }]);
    expect(r).toMatchObject({ task: "claim-match" });
    expect(r.model).toBeUndefined(); // Haiku: text only.
    expect(r.images).toBeUndefined();
    expect(r.input).toContain('1. Me: "@Seller zard"');
  });

  it("claims that name a card loosely", () => {
    expect(namesCard("Feraligatr", "feraligator")).toBe(true);
    expect(namesCard("Charizard ex", "charizard")).toBe(true);
    expect(namesCard("Pokémon Center Lady", "pokemon center lady")).toBe(true);
    expect(namesCard("Charizard", "zard")).toBe(true); // Inside the name.
    expect(namesCard("Pidgeot", "pikachu")).toBe(false);
    expect(namesCard("Mew", "ew")).toBe(false); // Too short to tell.
  });

  it("a claim that fits two different cards isn't sure: Claude decides", () => {
    const sale = capture([lot(0, "", [reply(ME, "pikachu", { id: 10 })])]);
    const [l] = interpretLots(sale, { ...OPTS, claims: true, claimPhoto: () => [{ card: "Pikachu", price: 10 }, { card: "Pikachu V", price: 30 }] });
    expect(l.claimCards).toBeNull();
    expect(l.claimMatchInput?.claims).toEqual([{ who: "Me", text: "pikachu" }]);
  });

  it("the photo is read once: a new reply doesn't change its key", () => {
    expect(claimPhotoAnswerKey({ imageUrl: "https://x.fbcdn.net/a.jpg?oe=1", lotText: "Holo" })).toBe(claimPhotoAnswerKey({ imageUrl: "https://x.fbcdn.net/a.jpg?oe=2", lotText: "Holo" }));
  });
});

describe("a lot's name from its own text (rules first)", () => {
  it.each([
    // Lot texts as sellers write them (group-domain.md and samples/, no names).
    ["Iron Jugulis 216/182 – Illustration Rare | MP: 20", "Iron Jugulis 216/182 – Illustration Rare", null],
    ["Morpeko 206/182 | MP 100", "Morpeko 206/182", null],
    ["Charizard ex 199/165\nMp 500", "Charizard ex 199/165", null],
    ["Mp 50kr\nCharizard ex 199/165", "Charizard ex 199/165", null], // The name on line 2.
    ["NM - 1200kr", null, "NM"],
    ["Umbreon VMAX 215/203 NM - 1200kr", "Umbreon VMAX 215/203", "NM"],
    ["Gengar 151 holo\nMp 10kr", "Gengar 151 holo", null],
    ["Gengar reverse holo\n700kr", "Gengar reverse holo", null],
    ["Lugia V 186/195 PSA 10\nMP: 2000", "Lugia V 186/195", "PSA 10"],
    ["Pikachu m/nm, mp 30kr, mb 10", "Pikachu", "M/NM"],
    ["Lot 1: Charizard ex 199/165", "Charizard ex 199/165", null],
    ["Mewtwo\nTilstand: MP\nMp 100", "Mewtwo", "MP"], // MP after "Tilstand:" is the condition.
    ["Holo, mp 30kr", null, null],
    ["Holo/rev.holo\n5kr per stk", null, null],
    ["Holo ( promo) mp 40", null, null],
    ["Mp 15kr", null, null],
    ["Lot\nMp 100kr", null, null],
    ["", null, null],
    // #352: price labels the start bid reads are never the name.
    ["Startpris: 200kr", null, null],
    ["Min pris 150", null, null],
    ["Pris: 200", null, null],
    ["Mp. 100", null, null],
    ["Lot 2 - 300,-", null, null],
    ["Startpris 150kr\nVenusaur 15/102", "Venusaur 15/102", null],
  ])("%j", (text, name, condition) => expect(lotTextInfo(text)).toEqual({ name, condition }));
});

describe("naming lots from their text and photo", () => {
  const c = capture([lot(0, "Holo, mp 20kr", []), lot(1, "Charizard ex 199/165 NM\nMp 500", []), lot(2, "", [])]);

  it("only lots whose text doesn't name them are sent to Claude, with their text", () => {
    expect(untitledLotPhotos(c)).toEqual([{ imageUrl: "lot0.jpg", text: "Holo, mp 20kr" }, { imageUrl: "lot2.jpg", text: "" }]);
  });

  it("uses Claude's name for those, adds the condition the text gives, and says where the name came from", () => {
    const names: Record<string, string> = { "lot0.jpg": "Pikachu 58/102 holo" };
    const lots = interpretLots(c, { ...OPTS, lotName: (url) => names[url] });
    expect(lots.map((l) => [l.title, l.namedByClaude, l.untitled])).toEqual([
      ["Pikachu 58/102 holo", true, true],
      ["Charizard ex 199/165 · NM", false, false],
      ["Lot 3", false, true], // Not named yet.
    ]);
  });

  it("caches by the photo's path (its query string changes per read) and the text", () => {
    expect(lotNameAnswerKey("https://x.fbcdn.net/a_n.jpg?oh=1&oe=2", "Holo")).toBe(lotNameAnswerKey("https://x.fbcdn.net/a_n.jpg?oh=9&oe=8", "Holo"));
    expect(lotNameAnswerKey("https://x.fbcdn.net/a_n.jpg", "Holo")).not.toBe(lotNameAnswerKey("https://x.fbcdn.net/a_n.jpg", "Holo, mp 30"));
  });

  it("the request gives Claude each photo's text", () => {
    const r = lotNameRequest([{ imageUrl: "a.jpg", text: "Holo, mp 20kr" }, { imageUrl: "b.jpg", text: "" }]);
    expect(r.images).toEqual(["a.jpg", "b.jpg"]);
    expect(r.input).toContain('Photo 1: "Holo, mp 20kr"');
    expect(r.input).toContain("Photo 2: (no text)");
  });
});

describe("claim lots priced in the lot's own text (\"Fastpris: Blir oppgitt over hvert bilde\")", () => {
  it.each([
    ["Holo/rev.holo\n5kr per stk", { kr: 5, perCard: true }],
    ["Ulike språk . Ulike varianter. EX/V/IR10kr per stk", { kr: 10, perCard: true }],
    ["Alle kortene 10 kr pr kort", { kr: 10, perCard: true }],
    ["NM/LP+ - 1200kr", { kr: 1200, perCard: false }],
    ["MP - 250kr", { kr: 250, perCard: false }],
    ["Kanda-lot, se bilder", null],
  ])("%s", (text, want) => expect(lotTextPrice(text)).toEqual(want));

  const c = capture([
    lot(0, "Holo/rev.holo\n5kr per stk", [reply(ME, `${SELLER} claim pikachu og eevee`, { id: 10 })]),
  ]);

  it("cards Claude found without a price take the lot's per-card price", () => {
    const [l] = interpretLots(c, {
      ...OPTS, claims: true,
      claimPhoto: () => [
        { card: "Pikachu", price: null }, { card: "Eevee", price: null },
        { card: "Ditto", price: 20 }, // A price on the photo wins.
      ],
    });
    expect(l.claimCards!.map((x) => [x.card, x.price])).toEqual([["Pikachu", 5], ["Eevee", 5], ["Ditto", 20]]);
    expect(l.textPrice).toEqual({ kr: 5, perCard: true });
  });
});

// #352: a multi-lot auction where each lot comment is "Lot N" plus a price in a form the start-bid
// rule didn't know. Shape assumed from the report (no sample was available); names invented.
describe("a lot's start bid from its own text (#352)", () => {
  it.each([
    ["MP: 1400", 1400],
    ["Mp 10kr", 10],
    ["Holo, mp 30kr, mb 20", 30],
    ["Minstepris 500", 500],
    ["700kr", 700],
    ["Mp. 200", 200],
    ["M.p 200", 200],
    ["MP=200", 200],
    ["Pris: 200", 200],
    ["Pris kr 250", 250],
    ["Startpris: 150kr", 150],
    ["Start bud 120", 120],
    ["Start: 90", 90],
    ["Min. pris 75,-", 75],
    ["Minstebud 60", 60],
    ["Lot 1 - 200kr", 200],
    ["Lot 3: 1 200,-", 1200],
    ["Blastoise 2/102 - 300,-", 300],
    ["Blastoise 2/102 NM 300kr", 300],
    ["MB 10kr\nCharizard 4/102 400kr", 400], // The bid step isn't the start bid.
    ["Mewtwo\nTilstand: MP\nMp 100", 100],
    ["Markedspris 900kr\nMp 300", 300],
    ["Markedspris 900kr", null], // What it's worth, not the start bid.
    ["Charizard 4/102", null], // A card number, not a price.
    ["Lot 4", null],
    ["MB: 10", null],
    ["", null],
  ])("%j", (text, want) => expect(lotStartBid(text)).toBe(want));
});

describe("multi-lot auction: each lot comment is its number and a price (#352)", () => {
  const SELLER2 = "Hanna Auksjonsen";
  const lot2 = (index: number, text: string, replies: CapturedReply[] = []) => lot(index, text, replies, SELLER2);
  const bid = (author: string, text: string, id: number): CapturedReply => ({ ...reply(author, text, { id }), ariaLabel: `Svar fra ${author} på ${SELLER2} sin kommentar` });
  const c: PostCapture = {
    ...capture([
      lot2(0, "Lot 1 - Pris: 200kr", [bid("Bidder A", `${SELLER2} 150`, 1), bid("Bidder B", `${SELLER2} 210`, 2)]),
      lot2(1, "Lot 2\nStartpris 150kr"),
      lot2(2, "Lot 3: Mp. 100"),
      lot2(3, "Lot 4 - Blastoise 2/102 - 300,-"),
      lot2(4, "Lot 5\nVenusaur 15/102 holo\nMinstepris: 250\nMB 20"),
    ]),
    post: { url: "", author: SELLER2, text: "AUKSJON/BUDRUNDE\nMinstepris: Står under hvert bilde\nMinimum budøkning: 10kr", timeText: null, images: [], truncated: false },
  };

  it("reads every lot's start bid from its text", () => {
    expect(interpretLots(c, OPTS).map((l) => [l.title, l.startBid, l.increment, l.untitled])).toEqual([
      ["Lot 1", 200, 10, true],
      ["Lot 2", 150, 10, true],
      ["Lot 3", 100, 10, true],
      ["Blastoise 2/102", 300, 10, false],
      ["Venusaur 15/102 holo", 250, 20, false],
    ]);
  });

  it("judges bids against that start bid", () => {
    const [first] = interpretLots(c, OPTS);
    expect(first.bids.map((b) => [b.amount, b.valid, b.note])).toEqual([[150, false, "below the start bid (200)"], [210, true, null]]);
    expect(first.highestBid).toBe(210);
  });

  it("sends the lots their text doesn't name to Claude, and uses its names", () => {
    expect(untitledLotPhotos(c).map((x) => x.text)).toEqual(["Lot 1 - Pris: 200kr", "Lot 2\nStartpris 150kr", "Lot 3: Mp. 100"]);
    const names: Record<string, string> = { "lot0.jpg": "Charizard 4/102", "lot1.jpg": "Pikachu 58/102", "lot2.jpg": "Mewtwo 10/102" };
    expect(interpretLots(c, { ...OPTS, lotName: (url) => names[url] }).map((l) => [l.title, l.namedByClaude])).toEqual([
      ["Charizard 4/102", true],
      ["Pikachu 58/102", true],
      ["Mewtwo 10/102", true],
      ["Blastoise 2/102", false],
      ["Venusaur 15/102 holo", false],
    ]);
  });
});

// #352 (2026-10-07): a lot comment is free text where the card's name, its condition and its price
// come in any order and any mix. Shapes assumed (no saved sample of the group's lot comments was
// available); names are Pokémon, never people.
describe("free-text lot comments: name, condition and start bid in any order (#352)", () => {
  it.each([
    ["Charizard ex NM 300kr", "Charizard ex", "NM", 300],
    ["NM - Pikachu 151 - Pris: 200", "Pikachu 151", "NM", 200],
    ["PSA 9 Umbreon VMAX, mp 500", "Umbreon VMAX", "PSA 9", 500],
    ["Mint Mew 150,-", "Mew", "Mint", 150],
    ["200kr Gengar LP", "Gengar", "LP", 200],
    ["Near mint Lugia\nMp: 400", "Lugia", "NM", 400],
    ["Lot 3: Blastoise (LP) startbud 90", "Blastoise", "LP", 90],
    ["Pikachu SV3 125/197 NM\nMp 100", "Pikachu SV3 125/197", "NM", 100],
    ["Charizard ex\n199/165\nLP+\nMp 500", "Charizard ex 199/165", "LP+", 500], // The number on its own line joins the name.
    ["Gardevoir ex\nSV3 245 / 197\nMp 80", "Gardevoir ex SV3 245/197", null, 80],
    ["Lightly played Snorlax 30kr", "Snorlax", "LP", 30],
    ["Damaged Machamp, mp 10", "Machamp", "DMG", 10],
    ["Heavily played Dragonite\nStartpris 40kr", "Dragonite", "HP", 40],
    ["Rayquaza VMAX 218/203 NM/M - Mp 900", "Rayquaza VMAX 218/203", "NM/M", 900],
    ["Espeon m/nm\nMinstepris: 250", "Espeon", "M/NM", 250],
    ["Charizard 4/102 PSA 10 Gem Mint\nMP 5000", "Charizard 4/102", "PSA 10", 5000],
    ["CGC 9.5 Pikachu illustrator promo", "Pikachu illustrator promo", "CGC 9.5", null],
    ["BGS 9,5 Lugia 9/111 mp 2000", "Lugia 9/111", "BGS 9.5", 2000],
    ["PSA10 Mewtwo 300kr", "Mewtwo", "PSA 10", 300], // The grade's 10 isn't the price ("10 300kr").
    ["Beckett 8 Gyarados\nMp 150", "Gyarados", "Beckett 8", 150],
    ["Blastoise EX 2/102 mp 200", "Blastoise EX 2/102", null, 200], // "EX" in a name is the card, not Excellent.
    ["Tilstand: EX\nMewtwo 10/102", "Mewtwo 10/102", "EX", null],
    ["Condition: Near Mint\nPrice: 300kr\nAlakazam 1/102", "Alakazam 1/102", "NM", 300],
  ])("%j", (text, name, condition, startBid) => {
    expect(lotTextInfo(text)).toEqual({ name, condition });
    expect(lotStartBid(text)).toBe(startBid);
  });
});

// "MP" is Minstepris (the start bid) in this group's lot comments, and Moderately Played on the
// TCGplayer scale (notes/fb_auction_watcher/group-domain.md §3.2, §5.2). The rule, in order.
describe("\"MP\": start bid or condition (#352)", () => {
  it.each([
    // 1. After a condition label, or written out: the condition. Its number is then not a price.
    ["Mewtwo\nTilstand: MP\nMp 100", "Mewtwo", "MP", 100],
    ["Tilstand: MP\nGolem 36/62", "Golem 36/62", "MP", null],
    ["Golem Tilstand: MP 100", "Golem 100", "MP", null], // Ambiguous: the label wins; "100" is a bare number, left in the name.
    ["Condition: MP - Golem 300kr", "Golem", "MP", 300],
    ["Moderately played Onix 20kr", "Onix", "MP", 20],
    // 3. Followed by an amount, with ":", ".", "=", "-" or "kr" between or none: the start bid.
    ["Onix MP 200", "Onix", null, 200],
    ["Onix Mp: 200kr", "Onix", null, 200],
    ["Onix M.P. 200", "Onix", null, 200],
    ["Onix MP - 200", "Onix", null, 200],
    ["Onix mp kr 200", "Onix", null, 200],
    ["MP - 250kr", null, null, 250], // In an auction, a start bid (a claim lot reads it as the condition: below).
    ["Onix NM mp 200", "Onix", "NM", 200],
    // 4. No amount after it: the condition, unless the text gives another.
    ["Onix MP\n200kr", "Onix", "MP", 200],
    ["Onix mp", "Onix", "MP", null],
    ["MP", null, "MP", null],
    ["Onix LP MP\nStartbud 50", "Onix", "LP", 50], // LP is given: the bare MP is dropped, not a name.
    ["Onix MP 4/102", "Onix 4/102", "MP", null], // Ambiguous: a card number after MP is not its amount, so this MP is bare.
  ])("%j", (text, name, condition, startBid) => {
    expect(lotTextInfo(text)).toEqual({ name, condition });
    expect(lotStartBid(text)).toBe(startBid);
  });

  it.each([
    // 2. A claim or fixed-price sale has no minimum price: "MP" is always the condition there.
    ["MP - 250kr", null, "MP"],
    ["Onix MP 200kr", "Onix", "MP"],
    ["Onix Mp", "Onix", "MP"],
    ["Onix NM, mp", "Onix", "NM"],
  ])("claim lot %j", (text, name, condition) => expect(lotTextInfo(text, { claims: true })).toEqual({ name, condition }));
});

describe("conditions, normalized for display (#352)", () => {
  it.each([
    ["nm", "NM"], ["Lp+", "LP+"], ["m/nm", "M/NM"], ["NM / M", "NM/M"], ["near  mint", "NM"], ["Near Mint", "NM"],
    ["lightly played", "LP"], ["moderately played", "MP"], ["heavily played", "HP"], ["damaged", "DMG"], ["dmg", "DMG"],
    ["mint", "Mint"], ["gem mint", "Gem Mint"], ["psa10", "PSA 10"], ["cgc 9,5", "CGC 9.5"], ["BGS 9.5", "BGS 9.5"],
    ["beckett 8", "Beckett 8"], ["tag 10", "TAG 10"], ["ex", "EX"], ["good", "GD"],
  ])("%j → %j", (raw, want) => expect(normalizeCondition(raw)).toBe(want));

  it("a bare number is never a price: it may be a card number or a set", () => {
    expect(lotStartBid("Pikachu 151\n58")).toBeNull();
    expect(lotTextInfo("Pikachu 151\n58")).toEqual({ name: "Pikachu 151", condition: null });
    expect(lotStartBid("Gengar 94/165 NM")).toBeNull();
  });
});

describe("free-text lots are named by the rules as soon as the post is read (#352)", () => {
  const c = capture([
    lot(0, "200kr Gengar LP", []),
    lot(1, "NM - Pikachu 151 - Pris: 200", []),
    lot(2, "Near mint Lugia\nMp: 400", [reply("Bidder A", `${SELLER} 400`, { id: 5 })]),
    lot(3, "PSA 9\nMp 500", []), // No name: Claude names it from the photo; the grade stays.
  ]);

  it("titles, conditions and start bids come from the text, with no Claude answer", () => {
    expect(interpretLots(c, OPTS).map((l) => [l.title, l.startBid, l.untitled, l.namedByClaude])).toEqual([
      ["Gengar · LP", 200, false, false],
      ["Pikachu 151 · NM", 200, false, false],
      ["Lugia · NM", 400, false, false],
      ["Lot 4 · PSA 9", 500, true, false],
    ]);
  });

  it("only the lot with no name in its text goes to Claude's photo naming", () => {
    expect(untitledLotPhotos(c).map((x) => x.text)).toEqual(["PSA 9\nMp 500"]);
    expect(interpretLots(c, { ...OPTS, lotName: () => "Mew 151/165" }).map((l) => l.title)).toEqual([
      "Gengar · LP", "Pikachu 151 · NM", "Lugia · NM", "Mew 151/165 · PSA 9",
    ]);
  });

  it("keeps the seller's raw text next to the interpreted values", () => {
    expect(interpretLots(c, OPTS).map((l) => l.rawText)).toEqual(["200kr Gengar LP", "NM - Pikachu 151 - Pris: 200", "Near mint Lugia\nMp: 400", "PSA 9\nMp 500"]);
  });
});
