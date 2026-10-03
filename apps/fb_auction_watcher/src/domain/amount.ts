// Amounts in NOK as sellers write them: "250kr", "250,-", "14 000kr", "1.400", "800.-",
// "10,-kr", "2.5k". Pure; no locale APIs, since the input is free text.

/** The first amount in `text`, or null. */
export function parseAmount(text: string | null | undefined): number | null {
  const s = (text ?? "").toLowerCase();
  // "2.5k" / "2,5k" / "3k"
  const k = s.match(/(?<![\d.,])(\d+(?:[.,]\d+)?)\s*k(?![a-zæøå])/);
  if (k) return Math.round(parseFloat(k[1].replace(",", ".")) * 1000);
  // Digits with thousands separators: "14 000", "1.400", "1 400 000".
  const m = s.match(/(?<![\d])(\d{1,3}(?:[ . ]\d{3})+|\d+)(?:[.,]-|,\d{1,2})?/);
  if (!m) return null;
  return parseInt(m[1].replace(/[ . ]/g, ""), 10);
}
