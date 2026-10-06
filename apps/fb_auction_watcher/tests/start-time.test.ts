import { describe, expect, it } from "vitest";
import { claudeEndsAt, claudeStartsAt, findStartLine, osloToUtc, parseStartTime } from "../src/domain/endTime";
import { interpretListing } from "../src/domain/listing";
import { saleLines } from "../src/domain/saleLines";
import { endTimeAnswerKey } from "../src/llm/prompts";
import { buildRows } from "../src/pages/dashboard/model";
import type { StoredPost } from "../src/shared/feed";

// The group's claim-sale template (a real post's text; it names no one). Startid is optional and
// claim sales only; sellers usually post the lots at the start time.
const CLAIM_TEMPLATE = [
  "Claim-salg (Tagg deg selv i kommentarfeltet om du ønsker å delta)",
  "Fastpris: Blir oppgitt over hvert bilde i kommentarfeltet",
  "Startid: 20:00 søndag 4. oktober",
  "Sluttid (maks 24 timer): 20:00 mandag 5. oktober",
  "Objektbeskrivelse: Kun eldre stamped kort.",
  "Tilstand: Fra NM til DMG, varierende tilstand. Kortene er stort sett i god stand, med noen unntak - se bilder",
  "Sender med post (pris m/emballasje): 30/50/80 kr",
  "Betalingsalternativ: Bankoverføring eller Vipps",
  "Bekreftelse: Jeg har lest og forstått reglene før jeg poster annonsen i gruppen.",
].join("\n");
const REF = new Date("2026-10-04T10:00:00Z");
const oslo = (y: number, mo: number, d: number, h: number, mi: number) => osloToUtc(y, mo, d, h, mi).toISOString();
const post = (id: string, text: string): StoredPost => ({
  id, url: `https://www.facebook.com/groups/g/posts/${id}/`, groupSlug: "g", sellerName: "Seller", text, textComplete: true,
  thumbnailUrl: null, firstSeenAt: REF.toISOString(), lastSeenAt: REF.toISOString(),
});

describe("start time", () => {
  it.each([
    ["Startid: 20:00 søndag 4. oktober", "Startid: 20:00 søndag 4. oktober"],
    ["Starttid: 04.10.26 kl 20:00", "Starttid: 04.10.26 kl 20:00"],
    ["Start tid: 4/10 kl 20", "Start tid: 4/10 kl 20"],
    ["Start:\n04.10 kl 20.00", "Start: 04.10 kl 20.00"],
  ])("finds %j", (text, line) => expect(findStartLine(text)).toBe(line));

  it("isn't a start bid or other text", () => {
    expect(findStartLine("Startbud: 100kr\nSluttid: 04.10 kl 20")).toBeNull();
    expect(findStartLine("Starter med de eldste kortene")).toBeNull();
  });

  it("reads it like an end time, and keeps the seller's line", () => {
    expect(parseStartTime("Startid: 20:00 søndag 4. oktober", REF)).toEqual({ startsAt: oslo(2026, 10, 4, 20, 0), startsAtText: "Startid: 20:00 søndag 4. oktober" });
    expect(parseStartTime("Startid: når jeg kommer hjem", REF)).toEqual({ startsAt: null, startsAtText: "Startid: når jeg kommer hjem" });
    expect(parseStartTime(null, REF)).toEqual({ startsAt: null, startsAtText: null });
  });

  it("Claude's answer has both times; an older answer is the end time alone", () => {
    expect(claudeEndsAt({ endsAt: "2026-10-05 20:00", startsAt: "2026-10-04 20:00" })).toBe(oslo(2026, 10, 5, 20, 0));
    expect(claudeStartsAt({ endsAt: "2026-10-05 20:00", startsAt: "2026-10-04 20:00" })).toBe(oslo(2026, 10, 4, 20, 0));
    expect(claudeEndsAt("2026-10-05 20:00")).toBe(oslo(2026, 10, 5, 20, 0));
    expect(claudeStartsAt("2026-10-05 20:00")).toBeNull();
  });
});

describe("the claim-sale template", () => {
  const i = interpretListing(CLAIM_TEMPLATE, REF);

  it("reads type, start, end, condition and terms", () => {
    expect(i).toMatchObject({
      type: "claim",
      startsAt: oslo(2026, 10, 4, 20, 0),
      endsAt: oslo(2026, 10, 5, 20, 0),
      sure: true,
      description: "Kun eldre stamped kort.",
      conditionText: "Fra NM til DMG, varierende tilstand. Kortene er stort sett i god stand, med noen unntak - se bilder",
      shippingText: "30/50/80 kr",
      paymentText: "Bankoverføring eller Vipps",
    });
  });

  it("is named by its Objektbeskrivelse, not the template's instruction", () => {
    expect(saleLines(i.title, i.description)).toEqual({ title: "Kun eldre stamped kort.", detail: null });
  });

  it("before the start it's not started; after, it is", () => {
    const p = post("1", CLAIM_TEMPLATE);
    expect(buildRows([p], new Date(oslo(2026, 10, 4, 19, 0)), null)[0]).toMatchObject({ notStarted: true, startsAtMs: Date.parse(oslo(2026, 10, 4, 20, 0)) });
    expect(buildRows([p], new Date(oslo(2026, 10, 4, 20, 1)), null)[0].notStarted).toBe(false);
  });

  it("a start line the rules can't read takes Claude's start time", () => {
    const text = CLAIM_TEMPLATE.replace("Startid: 20:00 søndag 4. oktober", "Startid: når middagen er spist");
    const answers = new Map<string, unknown>([[endTimeAnswerKey(text), { endsAt: null, startsAt: "2026-10-04 19:00" }]]);
    expect(buildRows([post("2", text)], REF, null, { answers })[0].startsAtMs).toBe(Date.parse(oslo(2026, 10, 4, 19, 0)));
  });
});

describe("sale names keep brackets that name something", () => {
  it.each([
    ["CLAIM-SALG (Delta Species)", "(Delta Species)"],
    ["Claim-salg (Tagg deg selv i kommentarfeltet om du ønsker å delta) Eeveelutions", "Eeveelutions"],
    ["AUKSJON (les reglene) Gengar", "Gengar"],
  ])("%s → %s", (title, want) => expect(saleLines(title, null).title).toBe(want));
});
