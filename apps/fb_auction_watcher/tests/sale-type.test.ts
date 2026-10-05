import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { interpretListing, isUntypedSale, SALE_TYPE_LABEL, saleTypeLabel, type SaleType } from "../src/domain/listing";
import { typeBadge } from "../src/pages/dashboard/badge";
import { buildRows } from "../src/pages/dashboard/model";
import type { StoredPost } from "../src/shared/feed";

// The sale type shown by every sale's name (#322): one wording everywhere, "Unknown" when the
// rules can't tell, and a badge with text (not colour alone).

const REF = new Date("2026-10-03T13:00:00Z");
const stored = (id: string, text: string): StoredPost => ({
  id, url: `https://www.facebook.com/groups/g/posts/${id}/`, groupSlug: "g", sellerName: "Selger", text, textComplete: true,
  thumbnailUrl: null, firstSeenAt: "2026-10-03T12:00:00Z", lastSeenAt: "2026-10-03T12:00:00Z",
});

describe("saleTypeLabel", () => {
  it.each([
    ["auction", "Auction"],
    ["claim", "Claim"],
    ["fixed", "Fixed price"],
    ["other", "Unknown"],
    ["wanted", "Wanted"],
    ["trade", "Trade"],
  ] as [SaleType, string][])("%s → %s", (type, label) => expect(saleTypeLabel(type)).toBe(label));

  it("matches the type filter's wording on the overview", () => {
    const html = readFileSync(resolve(__dirname, "../public/dashboard.html"), "utf8");
    for (const type of ["auction", "claim", "fixed"] as const) {
      expect(html).toContain(`data-filter="${type}">${SALE_TYPE_LABEL[type]}</button>`);
    }
  });
});

describe("typeBadge", () => {
  it.each([
    ["auction", "Auction"],
    ["claim", "Claim"],
    ["fixed", "Fixed price"],
    ["other", "Unknown"],
  ] as [SaleType, string][])("%s: text, its type's class, and a tooltip", (type, label) => {
    const b = typeBadge(type);
    expect(b.tagName).toBe("SPAN");
    expect(b.textContent).toBe(label); // Never colour alone.
    expect(b.classList.contains("type-badge")).toBe(true);
    expect(b.classList.contains(type)).toBe(true);
    expect(b.title).toMatch(new RegExp(`^${label}`));
  });
});

describe("Unknown type", () => {
  it("a post laid out as a sale but naming no type stays in the table as 'other' (Unknown), not guessed", () => {
    const text = "Pikachu-samling\nObjektbeskrivelse: 20 kort\nMinstepris: 50kr\nSluttid: 04.10.26 kl 21:00";
    const i = interpretListing(text, REF);
    expect(i.type).toBe("other");
    expect(isUntypedSale(i)).toBe(true);
    const [r] = buildRows([stored("1", text)], REF, null);
    expect(r).toMatchObject({ id: "1", type: "other" });
    expect(typeBadge(r.type).textContent).toBe("Unknown");
  });

  it("chatter, wanted and trade posts are still left out", () => {
    const rows = buildRows(
      [stored("1", "Hei alle sammen! Noen som vet når neste sett kommer?"), stored("2", "ØNSKES KJØPT-annonse\nObjektbeskrivelse: Umbreon"), stored("3", "BYTTE-annonse\nØnsker å bytte kort")],
      REF,
      null,
    );
    expect(rows).toEqual([]);
  });
});
