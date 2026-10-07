// Amounts in NOK as sellers write them: "250kr", "250,-", "14 000kr", "1.400", "800.-",
// "10,-kr", "2.5k". Pure; no locale APIs, since the input is free text.

/**
 * The digits of one amount found inside free text, for regexes that search a line (`u` flag
 * needed). A space or dot joins thousands ("1 200", "14 000", "1.400"), except where the first
 * group is three digits written straight after a word, which reads as part of the name (#356):
 * "Pikachu 151 200kr" is "Pikachu 151" at 200, not 151 200. Such a group is still joined when the
 * next group can't be a price on its own ("Charizard 120 000kr": "000"), or the separator is a
 * dot ("151.200kr"). A label or punctuation before the amount ("Pris: 150 500", "NM - 120 500kr")
 * means it is the price, so it joins. One- or two-digit first groups always join ("Charizard 1
 * 200kr" is 1 200), so a short number in the name before a price is read as thousands ("Pikachu 25
 * 200kr" is 25 200): the known limit. `parseAmount` itself is context-free: callers pass it the
 * amount they found.
 */
export const AMOUNT_DIGITS = String.raw`(?<!\d)(?:(?:(?<![\p{L}\d][ \t\u00a0]+)\d{1,3}|\d{1,2}|\d{3}(?=\.|[ \u00a0]0))(?:[ .\u00a0]\d{3})+|\d+)`;

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
