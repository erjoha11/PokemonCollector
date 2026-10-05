import { describe, expect, it } from "vitest";
import { pendingItems } from "../src/background/claudeQueue";
import { closesAtOf, readLots } from "../src/background/watch";
import { classifySellerReply, interpretLots, unsureSellerReplies, type LotOptions } from "../src/domain/bids";
import { mergeCaptures } from "../src/domain/captures";
import { sellerReplyAnswerKey, sellerReplyRequest } from "../src/llm/prompts";
import { buildRows, lotStatus, wonBySeller } from "../src/pages/dashboard/model";
import type { CapturedReply, PostCapture } from "../src/shared/capture";
import { AUCTION_ENDS, auctionText, capture, lot, ME, post, reply, SELLER } from "./fakes/posts";
import { memoryStore } from "./fakes/store";

// Late bids counted as wins (#329): the seller's "too late" replies under a bid, and bid times vs
// the lot's end with chained antisnipe. Invented names; wording modelled on the group's usage.

const OTHER = "Kari Budgiver";
const MIN = 60_000;

/** The seller's reply directly under a bid by `bidder` (Facebook: "… på <bidder> sitt svar"). */
function sellerUnder(bidder: string, text: string, id: number): CapturedReply {
  return { ...reply(SELLER, text, id), ariaLabel: `Svar fra ${SELLER} på ${bidder} sitt svar` };
}
/** A reply with its own Facebook age ("3 min"). */
const timed = (r: CapturedReply, timeText: string): CapturedReply => ({ ...r, timeText });

const opts = (over: Partial<LotOptions> = {}): LotOptions => ({ myName: ME, listingIncrement: 10, listingMinPrice: null, ...over });
const read = (comments: PostCapture["comments"], capturedAt = "2026-10-04T19:30:00Z", over: Partial<PostCapture> = {}) =>
  capture("3001", auctionText(), comments, { capturedAt, ...over });

describe("classifySellerReply", () => {
  it.each([
    "for sent", "Dessverre for seint", "Kom for sent", "kom etter sluttid", "Auksjonen er avsluttet", "auksjonen er ferdig",
    "Ikke gyldig, kom etter tiden", "ugyldig bud", "Det teller ikke", "Too late, sorry", `${ME} for sent dessverre`,
  ])("%s → too-late", (text) => expect(classifySellerReply(text, ME)).toBe("too-late"));

  it.each(["", "Den er grei", "ok", "Takk!", "Sendt PM", "Gratulerer!", "👍", `${ME}`, `${ME} sjekk pm`, "Ikke for sent, den teller"])(
    "%s → ok",
    (text) => expect(classifySellerReply(text, ME)).toBe("ok"),
  );

  it("mixed or unknown wording is unsure (it goes to Claude when it's under your bid)", () => {
    expect(classifySellerReply("Auksjonen er avsluttet, gratulerer!", ME)).toBe("unsure");
    expect(classifySellerReply("Hmm, sjekker klokka", ME)).toBe("unsure");
  });
});

describe("the seller says too late", () => {
  it("your late bid doesn't count and the next valid bid wins", () => {
    const c = read([lot(1, [reply(OTHER, `${SELLER} 40`, 1), reply(ME, `${SELLER} 60`, 2), sellerUnder(ME, "Dessverre, for sent", 3)])]);
    const [l] = interpretLots(c, opts());
    const mine = l.bids.find((b) => b.isMe)!;
    expect(mine).toMatchObject({ valid: false, late: "seller", sellerReply: { text: "Dessverre, for sent", verdict: "too-late", viaClaude: false } });
    expect(mine.note).toBe('seller: too late ("Dessverre, for sent")');
    expect(l).toMatchObject({ highestBid: 40, highestBidder: OTHER, myStatus: "outbid" });
  });

  it("finds the bid by a tag in a reply to the lot, too; only that bidder's latest bid before it", () => {
    const c = read([
      lot(1, [reply(OTHER, `${SELLER} 40`, 1), reply(ME, `${SELLER} 50`, 2), reply(OTHER, `${SELLER} 60`, 3), reply(SELLER, `${OTHER} kom etter sluttid`, 4)]),
    ]);
    const [l] = interpretLots(c, opts());
    expect(l.bids.map((b) => [b.amount, b.valid, b.late])).toEqual([[40, true, null], [50, true, null], [60, false, "seller"]]);
    expect(l).toMatchObject({ highestBid: 50, myStatus: "lead" });
  });

  it("'ikke for sent' doesn't reject; an unrelated seller reply changes nothing", () => {
    const c = read([lot(1, [reply(ME, `${SELLER} 60`, 2), sellerUnder(ME, "Ikke for sent, den teller", 3)]), lot(2, [reply(ME, `${SELLER} 20`, 5), reply(SELLER, "Sendt PM", 6)])]);
    const lots = interpretLots(c, opts());
    expect(lots.map((l) => l.myStatus)).toEqual(["lead", "lead"]);
  });

  it("when the post is the lot: a seller's reply under your bid comment", () => {
    const c = capture("3002", `${auctionText()}\nMinstepris: 10kr`, [
      { ...lot(1, [], `${SELLER} 40`), author: OTHER, images: [], hasImage: false, id: "5001" },
      { ...lot(2, [{ ...reply(SELLER, "for sent", 9), ariaLabel: `Svar fra ${SELLER} på ${ME} sin kommentar` }], `${SELLER} 60`), author: ME, images: [], hasImage: false, id: "5002" },
    ], { capturedAt: "2026-10-04T19:30:00Z" });
    const [l] = interpretLots(c, opts());
    expect(l.wholePost).toBe(true);
    expect(l).toMatchObject({ highestBid: 40, highestBidder: OTHER, myStatus: "outbid" });
  });

  it("an unclassified reply under your bid waits for Claude (raw text kept), then takes its answer", () => {
    const text = "Auksjonen er avsluttet, gratulerer!";
    const c = read([lot(1, [reply(OTHER, `${SELLER} 40`, 1), reply(ME, `${SELLER} 60`, 2), sellerUnder(ME, text, 3)])]);
    expect(unsureSellerReplies(c, ME)).toEqual([text]);
    const [waiting] = interpretLots(c, opts());
    expect(waiting.myStatus).toBe("lead");
    expect(waiting.lateCheck).toBe(`the seller replied under your bid: "${text}"`);
    const [rejected] = interpretLots(c, opts({ sellerReplyAnswer: (t) => (t === text ? true : undefined) }));
    expect(rejected).toMatchObject({ highestBid: 40, myStatus: "outbid", lateCheck: null });
    expect(rejected.bids[1].note).toBe(`seller: too late ("${text}") (read by Claude)`);
    const [accepted] = interpretLots(c, opts({ sellerReplyAnswer: () => false }));
    expect(accepted).toMatchObject({ highestBid: 60, myStatus: "lead", lateCheck: null });
  });

  it("an unclassified reply under someone else's bid isn't sent to Claude", () => {
    const c = read([lot(1, [reply(OTHER, `${SELLER} 40`, 1), sellerUnder(OTHER, "Hmm, sjekker klokka", 2)])]);
    expect(unsureSellerReplies(c, ME)).toEqual([]);
  });
});

describe("bid time vs the end (chained antisnipe)", () => {
  // auctionText ends 04.10.26 21:00 Oslo = 19:00Z. Read at 19:10Z.
  const READ = "2026-10-04T19:10:00Z";
  const ends = AUCTION_ENDS;

  it("a bid whose age proves it came after the end doesn't count", () => {
    const c = read([lot(1, [timed(reply(OTHER, `${SELLER} 40`, 1), "20 min"), timed(reply(ME, `${SELLER} 60`, 2), "3 min")])], READ);
    const [l] = interpretLots(c, opts({ endsAt: ends, softCloseMinutes: 0 }));
    expect(l.bids[1]).toMatchObject({ valid: false, late: "time", timeVerdict: "late" });
    expect(l).toMatchObject({ highestBid: 40, myStatus: "outbid", closesAt: ends });
  });

  it("with antisnipe, a bid in the last 5 min moves the end: the next one still counts, one after that chain doesn't", () => {
    // Read at 19:12Z. ~18:58Z (14 min old) → end ~19:03Z; ~19:02Z (10 min old) is past the written
    // end but inside the moved one. Minute-level ages can't prove that to the minute, so it counts,
    // flagged. ~19:12Z ("Akkurat nå") is past any end the chain allows: late.
    const c = read(
      [lot(1, [timed(reply(OTHER, `${SELLER} 40`, 1), "14 min"), timed(reply(ME, `${SELLER} 60`, 2), "10 min"), timed(reply(OTHER, `${SELLER} 80`, 3), "Akkurat nå")])],
      "2026-10-04T19:12:00Z",
    );
    const [l] = interpretLots(c, opts({ endsAt: ends, softCloseMinutes: 5 }));
    expect(l.bids.map((b) => b.timeVerdict)).toEqual(["on-time", "unsure", "late"]);
    expect(l).toMatchObject({ highestBid: 60, myStatus: "lead", lateCheck: "the highest bid may have come after the end" });
    expect(l.closesAt).toBeGreaterThan(ends + 5 * MIN);
    // Without antisnipe the same "8 min" bid is simply late.
    const [hard] = interpretLots(c, opts({ endsAt: ends, softCloseMinutes: 0 }));
    expect(hard.bids.map((b) => b.timeVerdict)).toEqual(["on-time", "late", "late"]);
    expect(hard).toMatchObject({ highestBid: 40, myStatus: "outbid" });
  });

  it("can't be proven late: kept valid, the lot is flagged 'check'", () => {
    // "1 t" read 30 min after the end: placed between 1.5 h before and 30 min before... or within the slack. Read 19:40Z: 17:39-18:41 → on time.
    // "2 t" read 2.5 h after: 16:29-18:31 → on time. Use "1 t" read 1 h 10 min after: 17:09-18:11, on time; so pick a straddle:
    const c = read([lot(1, [timed(reply(OTHER, `${SELLER} 40`, 1), "2 t"), timed(reply(ME, `${SELLER} 60`, 2), "1 t")])], "2026-10-04T20:30:00Z");
    const [l] = interpretLots(c, opts({ endsAt: ends, softCloseMinutes: 5 }));
    // Your "1 t" at 20:30Z: 18:29-19:31Z, the end is 19:00Z: unsure.
    expect(l.bids[1]).toMatchObject({ valid: true, timeVerdict: "unsure", late: null });
    expect(l).toMatchObject({ highestBid: 60, myStatus: "lead", lateCheck: "the highest bid may have come after the end" });
  });

  it("a read before the end proves every bid in it on time", () => {
    const c = read([lot(1, [reply(ME, `${SELLER} 60`, 2)])], "2026-10-04T18:30:00Z");
    const [l] = interpretLots(c, opts({ endsAt: ends, softCloseMinutes: 5 }));
    expect(l.bids[0].timeVerdict).toBe("on-time");
  });

  it("an older merged read (no seenAt) read after the end: no evidence, so no flag and no change", () => {
    const c = read([lot(1, [timed(reply(ME, `${SELLER} 60`, 2), "3 t")])], "2026-10-04T22:00:00Z", { reads: 3 });
    const [l] = interpretLots(c, opts({ endsAt: ends, softCloseMinutes: 5 }));
    expect(l.bids[0]).toMatchObject({ valid: true, timeVerdict: "unknown" });
    expect(l).toMatchObject({ myStatus: "lead", lateCheck: null, closesAt: ends });
  });

  it("no end time: no time checks", () => {
    const c = read([lot(1, [timed(reply(ME, `${SELLER} 60`, 2), "3 min")])], READ);
    const [l] = interpretLots(c, opts());
    expect(l.bids[0]).toMatchObject({ valid: true, timeVerdict: null });
  });
});

describe("the overview and the watcher use the chained end", () => {
  it("a bidding war past end + 5 min keeps the sale open ('Ended?') until its chained end", () => {
    // Ends 19:00Z, antisnipe 5. Bids at 18:58Z, 19:02Z, 19:06Z (read at 19:08Z) → chained end 19:11Z.
    const text = auctionText("Ja");
    const c = capture("3003", text, [lot(1, [timed(reply(OTHER, `${SELLER} 40`, 1), "10 min"), timed(reply(ME, `${SELLER} 60`, 2), "6 min"), timed(reply(OTHER, `${SELLER} 70`, 3), "2 min")])], {
      capturedAt: "2026-10-04T19:08:00Z",
    });
    const p = post("3003", text);
    const now = new Date("2026-10-04T19:09:00Z"); // past end + 5 min, before the chained end
    const [r] = buildRows([p], now, null, { captures: new Map([["3003", c]]), myName: ME });
    expect(r).toMatchObject({ ended: false, maybeEnded: true });
    expect(r.closesAtMs).toBeGreaterThanOrEqual(Date.parse("2026-10-04T19:11:00Z"));
    const { listing, lots } = readLots(p, c, new Map(), ME);
    expect(closesAtOf(listing, lots)).toBe(r.closesAtMs);
  });

  it("a win the seller rejected as too late isn't on To pay", () => {
    const text = auctionText();
    const c = capture("3004", text, [lot(1, [reply(ME, `${SELLER} 60`, 2), sellerUnder(ME, "for seint", 3)]), lot(2, [reply(ME, `${SELLER} 20`, 5)])], {
      capturedAt: "2026-10-04T19:30:00Z",
    });
    const [r] = buildRows([post("3004", text)], new Date("2026-10-04T20:00:00Z"), null, { captures: new Map([["3004", c]]), myName: ME });
    expect(r.lots!.map((l) => lotStatus(r, l).key)).toEqual(["lost", "won"]);
    expect(wonBySeller([r])[0].items.map((i) => i.lot.position)).toEqual([2]);
  });
});

describe("Claude: seller replies under your bids", () => {
  it("pendingItems asks about them (even a day after the end), once each", async () => {
    const text = "Hmm, sjekker klokka";
    const c = capture("3005", auctionText(), [lot(1, [reply(ME, `${SELLER} 60`, 2), sellerUnder(ME, text, 3)])], { capturedAt: "2026-10-05T10:00:00Z" });
    const store = memoryStore({ posts: [post("3005", auctionText())], captures: [{ postId: "3005", capture: c }] });
    const now = new Date("2026-10-05T19:00:00Z"); // a day after the end: past the 6 h grace for bids
    const pending = await pendingItems(store, { myName: ME, now });
    expect(pending.sellerReplies.map((s) => [s.text, s.key])).toEqual([[text, sellerReplyAnswerKey(text)]]);
    store.answers.set(sellerReplyAnswerKey(text), { key: sellerReplyAnswerKey(text), value: true, at: now.toISOString() });
    expect((await pendingItems(store, { myName: ME, now })).sellerReplies).toEqual([]);
  });

  it("the request asks rejects true/false/null per reply, with the raw text", () => {
    const req = sellerReplyRequest([{ id: 0, text: "Hmm, sjekker klokka" }]);
    expect(req.task).toBe("seller-reply");
    expect(req.input).toBe('0: "Hmm, sjekker klokka"');
    expect(JSON.stringify(req.schema)).toContain("rejects");
  });
});

describe("merged reads keep when each reply was seen", () => {
  it("stamps seenAt, and keeps the more precise age across reads", () => {
    const first = read([lot(1, [timed(reply(ME, `${SELLER} 60`, 2), "7 min")])], "2026-10-04T19:10:00Z");
    const later = read([lot(1, [timed(reply(ME, `${SELLER} 60`, 2), "3 t")])], "2026-10-04T22:30:00Z");
    const merged = mergeCaptures(mergeCaptures(null, first), later);
    expect(merged.comments[0].replies[0]).toMatchObject({ timeText: "7 min", seenAt: "2026-10-04T19:10:00Z" });
    const fresh = mergeCaptures(null, later);
    expect(fresh.comments[0].replies[0].seenAt).toBe("2026-10-04T22:30:00Z");
  });
});
