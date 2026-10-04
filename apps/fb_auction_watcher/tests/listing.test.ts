import { describe, expect, it } from "vitest";
import { interpretListing, saleTitle, saleType } from "../src/domain/listing";
import { buildRows, countdown, countRows, groupRows } from "../src/pages/dashboard/model";
import { endTimeAnswerKey } from "../src/llm/prompts";
import type { StoredPost } from "../src/shared/feed";

// Post texts shaped like real ones in samples/ (no names).
const AUCTION = "AUKSJON/BUDRUNDE-annonse\nMinstepris: 500,-\nMinimum budøkning: 10kr\nSluttid: 04.10.26 kl 21:00\nAntisnipe 5 min: Ja\nBetalingsalternativ: Vipps";
const CLAIM = "Claim salg-annonse - Etter sluttid merkes innlegget “Solgt”\nFastpris: Oppgis over hvert bilde i kommentarfeltet.\nSluttid (maks 24 timer): 04.10.26 kl: 14:00";
const FIXED = "FASTPRIS-annonse\nFastpris: 14 000kr\nObjektbeskrivelse: Pikachu";
const REF = new Date("2026-10-03T13:00:00Z");

describe("saleType", () => {
  it.each([
    [AUCTION, "auction"], [CLAIM, "claim"], [FIXED, "fixed"], ["LYNAUKSJON/BUDRUNDE-annonse", "auction"],
    ["ØNSKES KJØPT-annonse\nØnskes Kjøpt: Umbreon v", "wanted"], ["BYTTE-annonse\nØnsker å bytte kort", "trade"],
    ["SØTE FAIRY KORT CLAIM SALG", "claim"], ["Hei alle sammen!", "other"],
    // Review H5: a word in the rules must not change the type; the headline / hashtag decides.
    ["AUKSJON/BUDRUNDE\nMinstepris: 10kr\nIngen claim etter sluttid", "auction"],
    ["Pikachu, Glassbirds, Gengar\nAUKSJON/BUDRUNDE-annonse\nMinstepris: 20\nClaim etter sluttid er bindende", "auction"],
    ["Diverse kort\nTar imot bud\nkan claimes etter avtale\n#Auksjon", "auction"],
    ["Stor low-pop slab Claim salg-annonse\nFastpris: Oppgis over hvert bilde\n#Claimsalg", "claim"],
    ["Claimsalg\nInnlegget vil bli markert som SOLGT\nPris: står på kortet\n#Fastpris", "claim"], // Headline beats a wrong hashtag.
  ])("%#", (text, want) => expect(saleType(text)).toBe(want));
});

describe("interpretListing", () => {
  it("reads the auction template", () => {
    const i = interpretListing(AUCTION, REF);
    expect(i).toMatchObject({ type: "auction", title: "AUKSJON/BUDRUNDE", minPrice: 500, increment: 10, softCloseMinutes: 5, sure: true });
    expect(i.endsAt).toBe("2026-10-04T19:00:00.000Z");
  });
  it("leaves per-lot prices empty and reads 'Nei' as no soft close", () => {
    const i = interpretListing("AUKSJON\nMinstepris: Kommer på hvert bilde\nMinimum budøkning: kommer under hvert bilde\nAntisnipe 5 min: Nei", REF);
    expect(i).toMatchObject({ minPrice: null, increment: null, softCloseMinutes: 0, endsAt: null });
  });
  it("doesn't take 'LAV MINSTEPRIS' as a price line", () => {
    expect(interpretListing("Vintage AUKSJON/BUDRUNDE-annonse\nLAV MINSTEPRIS\nMinstepris: 10kr pr kort.", REF).minPrice).toBe(10);
  });
  it("reads fixed prices", () => {
    expect(interpretListing(FIXED, REF)).toMatchObject({ type: "fixed", fixedPrice: 14000, endsAt: null });
  });
  it("reads the item description, and titles a post that starts with a template line by it", () => {
    expect(interpretListing(FIXED, REF).description).toBe("Pikachu");
    expect(saleTitle("Fastpris: 950kr\nObjektbeskrivelse: Gardevoir ex Delta Species")).toBe("Gardevoir ex Delta Species");
  });
  it("titles drop the template suffix", () => {
    expect(saleTitle("FASTPRIS-annonse Van Gogh Pikachu")).toBe("FASTPRIS Van Gogh Pikachu");
  });
});

const stored = (id: string, text: string, firstSeenAt = "2026-10-03T12:00:00Z"): StoredPost => ({
  id, url: `https://www.facebook.com/groups/g/posts/${id}/`, groupSlug: "g", sellerName: "Selger", text, textComplete: true,
  thumbnailUrl: null, firstSeenAt, lastSeenAt: firstSeenAt,
});

describe("table model", () => {
  const posts = [
    stored("1", AUCTION.replace("04.10.26 kl 21:00", "03.10.26 kl 15:30")), // in 30 min
    stored("2", AUCTION.replace("04.10.26 kl 21:00", "03.10.26 kl 22:00")), // later today
    stored("3", AUCTION), // tomorrow
    stored("4", "AUKSJON\nSluttid: snart"), // unknown
    stored("5", CLAIM),
    stored("6", FIXED),
    stored("7", AUCTION.replace("04.10.26 kl 21:00", "03.10.26 kl 14:57")), // 3 min ago, within antisnipe
    stored("8", AUCTION.replace("04.10.26 kl 21:00", "02.10.26 kl 21:00")), // ended yesterday
    stored("9", "ØNSKES KJØPT-annonse"), // not a sale
  ];
  const now = REF; // 15:00 Oslo
  const rows = buildRows(posts, now, new Date("2026-10-03T11:00:00Z"));

  it("leaves out posts that aren't sales", () => {
    expect(rows.map((r) => r.id)).not.toContain("9");
  });

  it("groups by end time", () => {
    const g = Object.fromEntries(groupRows(rows, now).map((x) => [x.id, x.rows.map((r) => r.id)]));
    expect(g).toEqual({ today: ["7", "1", "2"], later: ["3"], unknown: ["4"], claim: ["5"], fixed: ["6"], ended: ["8"] });
  });

  it("an auction ending within the hour but after midnight is still Today", () => {
    const lateNight = new Date("2026-10-03T21:50:00Z"); // 23:50 in Oslo.
    const [r] = buildRows([stored("30", AUCTION.replace("04.10.26 kl 21:00", "04.10.26 kl 00:20"))], lateNight, null);
    expect(groupRows([r], lateNight).find((x) => x.id === "today")!.rows).toHaveLength(1);
  });

  it("marks 'Ended?' inside the antisnipe window", () => {
    expect(rows.find((r) => r.id === "7")).toMatchObject({ maybeEnded: true, ended: false });
  });

  it("counts", () => {
    expect(countRows(rows, now)).toEqual({ active: 6, withinHour: 2, outbid: 0, isNew: 7 });
  });

  it("formats countdowns", () => {
    const t = now.getTime();
    expect(countdown(t + 65_000, now)).toBe("01:05");
    expect(countdown(t + 5 * 3_600_000 + 12 * 60_000, now)).toBe("5 h 12 min");
    expect(countdown(t + 50 * 3_600_000, now)).toBe("2 d 2 h");
    expect(countdown(t - 1, now)).toBe("ended");
  });

  it("a sale you marked as ended is ended, whatever its end time says", () => {
    const marks = { "3": "2026-10-03T12:00:00Z", "4": "2026-10-03T12:30:00Z", "7": "2026-10-03T12:58:00Z" }; // 14:00, 14:30, 14:58 Oslo.
    const marked = buildRows(posts, now, null, { endedMarks: marks });
    expect(marked.find((r) => r.id === "3")).toMatchObject({ ended: true, maybeEnded: false, endedByYouAt: "2026-10-03T12:00:00Z" });
    expect(marked.find((r) => r.id === "7")).toMatchObject({ ended: true, maybeEnded: false });
    const g = Object.fromEntries(groupRows(marked, now).map((x) => [x.id, x.rows.map((r) => r.id)]));
    // Newest ended first: 7 (its end time, 14:57 Oslo), 4 (marked 14:30, no end time), 3 (marked 14:00, before its end), 8.
    expect(g.ended).toEqual(["7", "4", "3", "8"]);
    expect(g.unknown).toEqual([]);
    expect(rows.find((r) => r.id === "3")).toMatchObject({ ended: false, endedByYouAt: null });
  });

  it("uses Claude's end time when the rules found none, and marks it", () => {
    const text = "AUKSJON\nAuksjonen avsluttes søndag kveld klokka ni";
    const [r] = buildRows([stored("20", text)], now, null, { answers: new Map([[endTimeAnswerKey(text), "2026-10-04 21:00"]]) });
    expect(r).toMatchObject({ endsAt: "2026-10-04T19:00:00.000Z", endsViaClaude: true, sure: true });
  });
});
