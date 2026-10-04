import { describe, expect, it } from "vitest";
import { saleLines } from "../src/pages/dashboard/model";

// The table's two lines per sale: the title without the template's type words, then the description.
// Titles as sellers write them (invented sales).
describe("saleLines", () => {
  it.each([
    ["Gengar AUKSJON", "Gengar"],
    ["30th CELEBRATION AUKSJON", "30th CELEBRATION"],
    ["AUKSJON / 30-ÅRS JUBILEUM", "30-ÅRS JUBILEUM"],
    ["LYNAUKSJON/BUDRUNDE 30TH Celebration (JAPANSK)", "30TH Celebration (JAPANSK)"],
    ["Stor low-pop slab Claim salg - Etter sluttid merkes innlegget", "Stor low-pop slab - Etter sluttid merkes innlegget"],
    ["CLAIM-SALG – EEVEELUTIONS", "EEVEELUTIONS"],
    ["FASTPRIS 151 UPC", "151 UPC"],
    ["Fastpris: 800.-", "800.-"],
    ["Masse 30th Celebration up for grabs", "Masse 30th Celebration up for grabs"],
    ["Mega Darkrai EX", "Mega Darkrai EX"],
  ])("%s → %s", (title, expected) => {
    expect(saleLines(title, null).title).toBe(expected);
  });

  it("a title that was only the template words gives way to the description", () => {
    expect(saleLines("AUKSJON/BUDRUNDE", "Hits fra div sets")).toEqual({ title: "Hits fra div sets", detail: null });
    expect(saleLines("FASTPRIS", null)).toEqual({ title: "FASTPRIS", detail: null });
  });

  it("keeps the description as the second line, unless it repeats the title", () => {
    expect(saleLines("Gengar AUKSJON", "Selger unna min samling")).toEqual({ title: "Gengar", detail: "Selger unna min samling" });
    expect(saleLines("Mega Darkrai EX", "Mega Darkrai EX")).toEqual({ title: "Mega Darkrai EX", detail: null });
  });
});
