import { describe, expect, it } from "vitest";
import { interpretLots } from "../src/domain/bids";
import { interpretListing } from "../src/domain/listing";
import type { CapturedComment } from "../src/shared/capture";
import { capture, ME, reply, SELLER } from "./fakes/posts";

// An auction whose post is itself the lot, with bids written directly under the post (2026-10-05,
// group post 3310557635801790; text as posted, names invented).

const TEXT = `LYNAUKSJON/BUDRUNDE
Div pokemon kort (bulk) usikker på serie /en binder / sleeves ( 1 full Etb )
Minstepris: 200kr
Minimum budøkning: 25kr
Sluttid: 22:30 04/10-26
Antisnipe 5 min: Ja
Objektbeskrivelse: kommer beskrivelsen på hvert bilde
Tilstand: NM
Sender med post (pris m/emballasje): Kjøper betaler for ønsket frakt.
Betalingsalternativ: Vipps
Bekreftelse: Jeg har lest og forstått reglene før jeg poster annonsen i gruppen.
#Auksjon`;

let seq = 100;
/** A top-level comment under the post (not a lot: no photo from the seller). */
function comment(author: string, text: string, replies: CapturedComment["replies"] = []): CapturedComment {
  const id = String(3310600000000000 + seq++);
  return { id, url: null, author, text, timeText: "1 t", ariaLabel: `Kommentar fra ${author}`, images: [], truncated: false, rawText: text, index: seq, hasImage: false, replies };
}

function read(comments: CapturedComment[]) {
  const listing = interpretListing(TEXT, new Date("2026-10-04T10:00:00Z"));
  const cap = capture("3310557635801790", TEXT, comments);
  cap.post.images = [{ src: "https://scontent.example/post.jpg", alt: "" }];
  return interpretLots(cap, { myName: ME, listingIncrement: listing.increment, listingMinPrice: listing.minPrice });
}

describe("the post is the lot", () => {
  it("one lot, named from the post, with the post's start bid and raise; bids are the comments under the post", () => {
    const lots = read([
      comment("Bidder A", "."), // Following the sale: not a bid.
      comment("Bidder A", "200"),
      comment("Bidder B", "225"),
      comment(ME, "250"),
      comment(SELLER, "Høyeste bud 250"), // The seller never bids.
    ]);
    expect(lots).toHaveLength(1);
    expect(lots[0]).toMatchObject({
      wholePost: true,
      commentId: null, // Links go to the post itself.
      title: "Div pokemon kort (bulk) usikker på serie /en binder / sleeves ( 1 full Etb )",
      imageUrl: "https://scontent.example/post.jpg",
      startBid: 200,
      increment: 25,
      highestBid: 250,
      highestBidder: ME,
      myHighestBid: 250,
      myStatus: "lead",
    });
    expect(lots[0].bids.map((b) => b.amount)).toEqual([200, 225, 250]);
  });

  it("outbid by a later comment; a bid too small or below the start bid doesn't count", () => {
    const [lot] = read([comment(ME, "200"), comment("Bidder B", "210"), comment("Bidder B", "150"), comment("Bidder C", "225")]);
    expect(lot).toMatchObject({ highestBid: 225, highestBidder: "Bidder C", myStatus: "outbid" });
    expect(lot.bids.filter((b) => !b.valid).map((b) => b.amount)).toEqual([210, 150]);
  });

  it("a bid written as a reply under someone's comment isn't counted (as under a lot)", () => {
    const [lot] = read([comment("Bidder A", "200", [reply("Bidder B", "300")])]);
    expect(lot).toMatchObject({ highestBid: 200, highestBidder: "Bidder A" });
    expect(lot.bids.find((b) => b.amount === 300)).toMatchObject({ valid: false, underReply: true });
  });

  it("no comments yet (or only the seller's): no lot", () => {
    expect(read([])).toEqual([]);
    expect(read([comment(SELLER, "Oppdaterer bildene snart")])).toEqual([]);
  });

  it("a post with lot comments is read as before: the post doesn't become a lot", () => {
    const lotComment: CapturedComment = { ...comment(SELLER, "Charizard\nMp 100"), hasImage: true, images: [{ src: "https://scontent.example/l1.jpg", alt: "" }] };
    const lots = read([lotComment, comment("Bidder A", "200")]);
    expect(lots.map((l) => [l.title, !!l.wholePost])).toEqual([["Charizard", false]]);
  });
});
