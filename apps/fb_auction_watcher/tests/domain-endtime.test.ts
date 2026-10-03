import { describe, expect, it } from "vitest";
import { parseAmount } from "../src/domain/amount";
import { findEndLine, osloToUtc, parseEndTime } from "../src/domain/endTime";

// Real lines from samples/ (no names). Captured on Saturday 3 October 2026.
const REF = new Date("2026-10-03T13:00:00Z");
const oslo = (y: number, m: number, d: number, h: number, mi: number) => osloToUtc(y, m, d, h, mi).toISOString();

describe("parseAmount", () => {
  it.each([
    ["250kr", 250], ["250,-", 250], ["14 000kr", 14000], ["13999,-", 13999], ["800.-", 800],
    ["10,-kr", 10], ["1.400", 1400], ["2.5k", 2500], ["Mp 10kr", 10], ["MP: 1400", 1400],
    ["Jens Sveaass 110kr", 110], ["Fastpris: 2300kr (Ny pris)", 2300], ["kommer på hvert bilde", null],
  ])("%s → %s", (text, want) => expect(parseAmount(text)).toBe(want));
});

describe("findEndLine", () => {
  it("takes the Sluttid/Slutt line, not rule sentences that mention sluttid", () => {
    const text = "Claim salg-annonse - Etter sluttid merkes innlegget “Solgt”\nFastpris: 100\nSluttid (maks 24 timer): 04.10.26 kl: 14:00\nBetaling: Vipps";
    expect(findEndLine(text)).toBe("Sluttid (maks 24 timer): 04.10.26 kl: 14:00");
  });
  it("joins an empty 'Sluttid:' with the next line", () => {
    expect(findEndLine("Sluttid:\n04.10.26  kl 22.00\nAntisnipe 5 min: Ja")).toBe("Sluttid: 04.10.26  kl 22.00");
  });
  it("returns null when there is no end-time line", () => {
    expect(findEndLine("FASTPRIS-annonse\nFastpris: 950kr")).toBeNull();
  });
});

describe("parseEndTime", () => {
  it.each([
    ["Slutt: 05.10.26 kl 21:00", oslo(2026, 10, 5, 21, 0)],
    ["Sluttid: (maks 24 timer): 04/10-26 kl20:00", oslo(2026, 10, 4, 20, 0)],
    ["Sluttid: 03.10 Lørdag kl22:00", oslo(2026, 10, 3, 22, 0)],
    ["Sluttid: 03.10 kl 22:00", oslo(2026, 10, 3, 22, 0)],
    ["Sluttid: 03.10.2026 kl 21:00", oslo(2026, 10, 3, 21, 0)],
    ["Sluttid: 04.10 16:00", oslo(2026, 10, 4, 16, 0)],
    ["Sluttid: 04.10.2026 kl. 19:30", oslo(2026, 10, 4, 19, 30)],
    ["Sluttid: 04.10.26 20:00", oslo(2026, 10, 4, 20, 0)],
    ["Sluttid: 05.10.26 kl.20.00", oslo(2026, 10, 5, 20, 0)],
    ["Sluttid: 2026-10-02 22.00", oslo(2026, 10, 2, 22, 0)],
    ["Sluttid: 3.10 kl 23", oslo(2026, 10, 3, 23, 0)],
    ["Sluttid: 4 oktober (04.10.26) 15:00", oslo(2026, 10, 4, 15, 0)],
    ["Sluttid: 5.10.26 21:00", oslo(2026, 10, 5, 21, 0)],
    ["Sluttid: Ikveld 3/10, kl 22.00", oslo(2026, 10, 3, 22, 0)],
    ["Sluttid: Mandag 05/10 kl 21:00", oslo(2026, 10, 5, 21, 0)],
    ["Sluttid: Søndag 04.10 kl 21:00", oslo(2026, 10, 4, 21, 0)],
    ["Sluttid: Søndag 04.10.2026 KL 18.00", oslo(2026, 10, 4, 18, 0)],
    ["Sluttid: Søndag 4/10 kl.18:00", oslo(2026, 10, 4, 18, 0)],
    ["Sluttid: Søndag kl. 22:00 (04.10.2026)", oslo(2026, 10, 4, 22, 0)],
    ["Sluttid: Søndag, 04.10.26 kl 15:00…", oslo(2026, 10, 4, 15, 0)],
    ["Sluttid:søndag 04/10 kl. 21:00", oslo(2026, 10, 4, 21, 0)],
    ["Sluttid (Lørdag 3. oktober 23.59):", oslo(2026, 10, 3, 23, 59)],
    ["Sluttid (maks 24 timer): 04.10.26 kl: 14:00", oslo(2026, 10, 4, 14, 0)],
    ["Sluttid 05.10.2026 23:00", oslo(2026, 10, 5, 23, 0)],
  ])("%s", (line, want) => {
    const r = parseEndTime(line, REF);
    expect(r.endsAt).toBe(want);
    expect(r.sure).toBe(true);
    expect(r.endsAtText).toBe(line);
  });

  it("is 21:00 Oslo summer time = 19:00 UTC", () => {
    expect(parseEndTime("Sluttid: 04.10.26 kl 21:00", REF).endsAt).toBe("2026-10-04T19:00:00.000Z");
  });

  it("resolves a weekday-only end time to the next such day, but unsure", () => {
    const r = parseEndTime("Sluttid: Søndag kl 22", REF);
    expect(r.endsAt).toBe(oslo(2026, 10, 4, 22, 0));
    expect(r.sure).toBe(false);
  });

  it("flags a weekday that doesn't match the date", () => {
    expect(parseEndTime("Sluttid: Mandag 04.10 kl 21:00", REF).sure).toBe(false);
  });

  it("gives no time when there's a date but no time, or nothing readable", () => {
    expect(parseEndTime("Sluttid: 04.10", REF)).toEqual({ endsAt: null, endsAtText: "Sluttid: 04.10", sure: false });
    expect(parseEndTime("Sluttid: når jeg har lyst", REF).endsAt).toBeNull();
    expect(parseEndTime(null, REF).endsAt).toBeNull();
  });
});
