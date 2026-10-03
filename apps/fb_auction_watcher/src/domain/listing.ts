import { parseAmount } from "./amount";
import { findEndLine, parseEndTime, type EndTime } from "./endTime";

// Interprets a sale post's text (the group template) into listing fields. Pure and cheap, so
// the table re-runs it on the stored raw text every time: better rules apply to old posts too.

export type SaleType = "auction" | "claim" | "fixed" | "wanted" | "trade" | "other";

export type Interpretation = EndTime & {
  type: SaleType;
  title: string;
  /** The template's "Objektbeskrivelse:" (what's for sale), or null. */
  description: string | null;
  /** Antisnipe minutes when "Ja", 0 when "Nei", null when not stated. */
  softCloseMinutes: number | null;
  /** Minimum increment for the whole sale, or null (often "per lot", written on each image). */
  increment: number | null;
  minPrice: number | null;
  fixedPrice: number | null;
};

/** The type a piece of text names, by the template's words; claim before fixed price (claim sales have a "Fastpris:" line). */
function typeIn(text: string): SaleType | null {
  if (/ønskes\s+kjøpt/i.test(text)) return "wanted";
  if (/bytte-?annonse|ønsker\s+å\s+bytte/i.test(text)) return "trade";
  if (/claim|clame/i.test(text)) return "claim";
  if (/auksjon|budrunde|auction/i.test(text)) return "auction";
  if (/fastpris/i.test(text)) return "fixed";
  return null;
}

/**
 * The sale type, from where the group's template puts it: the headline in the first two lines
 * ("AUKSJON/BUDRUNDE-annonse", "Claim salg-annonse", "Claimsalg"), else the closing hashtag
 * (#Auksjon, #Claimsalg, #Fastpris; sellers get it wrong more often than the headline, e.g.
 * "Claimsalg" with #Fastpris), and only then anywhere in the text. A word in the rules
 * ("Ingen claim etter sluttid") must not turn an auction into a claim sale.
 */
export function saleType(text: string): SaleType {
  const headline = text.split("\n").map((l) => l.trim()).filter(Boolean).slice(0, 2);
  const tags = (text.match(/#[\p{L}-]+/gu) ?? []).join(" ");
  return headline.map(typeIn).find((t) => t !== null) ?? ((tags && typeIn(tags)) || typeIn(text) || "other");
}

/** First line of the post, without the template's "-annonse" suffix and the "… Se mer" cut. */
export function saleTitle(text: string): string {
  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
  // Some posts start straight with a template line ("Fastpris: 950kr"); the description then
  // says more about what's for sale.
  const desc = lines.find((l) => /^objektbeskrivelse\s*:/i.test(l))?.replace(/^objektbeskrivelse\s*:\s*/i, "");
  const first = (/^[\p{L} ]{3,20}:/u.test(lines[0] ?? "") && desc ? desc : lines[0]) ?? "";
  return first.replace(/[-\s]*annonse\b/i, "").replace(/\s*…?\s*(se mer|see more)$/i, "").trim() || first;
}

/** The value after a template label ("Minstepris: 500,-"), from the first matching line. */
function labelled(text: string, label: RegExp): string | null {
  for (const line of text.split("\n")) {
    const m = line.match(label);
    if (m) return line.slice(m.index! + m[0].length);
  }
  return null;
}

export function interpretListing(text: string, capturedAt: Date): Interpretation {
  const antisnipe = text.match(/antisnipe\s*(\d+)\s*min[^:\n]*:\s*(ja|nei)/i);
  return {
    ...parseEndTime(findEndLine(text), capturedAt),
    type: saleType(text),
    title: saleTitle(text),
    description: labelled(text, /objektbeskrivelse\s*:?/i)?.trim() || null,
    softCloseMinutes: antisnipe ? (antisnipe[2].toLowerCase() === "ja" ? Number(antisnipe[1]) : 0) : null,
    increment: parseAmount(labelled(text, /minimum\s+budøkning\s*:?/i)),
    minPrice: parseAmount(labelled(text, /(?<!lav\s)minstepris\s*:?/i)),
    fixedPrice: parseAmount(labelled(text, /fastpris\s*:/i)),
  };
}
