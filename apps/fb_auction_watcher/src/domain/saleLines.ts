// A sale's name for display (the overview's table, notifications). Pure.

// The group's posting template puts the sale type in the title ("AUKSJON/BUDRUNDE", "FASTPRIS",
// "Claim salg"); the Type is shown on its own, so the table leaves those words out.
const TEMPLATE_WORDS = /\b(?:lyn)?auksjon(?:en)?\b|\bbudrunde\b|\bclaim[\s-]*salg(?:et)?\b|\bfastpris\b/gi;
// Leading and trailing separators; a trailing ".-" / ",-" is a price ("800.-"), kept.
const SEPARATORS = /^[\s\-–—:/|,.!]+|(?<![.,])[\s\-–—:/|,!]+$/g;

/**
 * A sale's two lines for the table: its title without the template words, then its description.
 * When nothing is left of the title (it was only "AUKSJON/BUDRUNDE"), the description moves up.
 */
export function saleLines(title: string, description: string | null): { title: string; detail: string | null } {
  const cleaned = title
    .replace(TEMPLATE_WORDS, " ")
    .replace(/\s*([\-–—:/|])(?:\s*[\-–—:/|])+\s*/g, " $1 ") // "Slab Claim salg - Etter" → one separator.
    .replace(/\s+/g, " ")
    .replace(SEPARATORS, "")
    .trim();
  const detail = description && description.trim() !== title.trim() ? description.trim() : null;
  if (cleaned.length >= 3) return { title: cleaned, detail };
  return detail ? { title: detail, detail: null } : { title: title.trim(), detail: null };
}
