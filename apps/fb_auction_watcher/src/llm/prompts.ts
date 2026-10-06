// Prompts for what the rules can't read, sent to Claude Code (`claude -p`) through the native
// bridge (native/fbaw_claude_host.py). No API key: it runs on the user's own Claude login.
// Tried on src/llm/cases.ts (23 cases, Haiku, batched): 23/23 on 2026-10-03.

import { tagAsSeller, type ClaimMatchInput, type ClaimPhotoInput, type PhotoCards } from "../domain/bids";

export type ClaudeRequest = {
  /** A short name for logs. */
  task: "end-time" | "bid" | "claim-lot" | "claim-match" | "lot-name";
  /** Haiku unless set; photos need Sonnet (Haiku misread prices on a real lot). */
  model?: "haiku" | "sonnet";
  /** Photo URLs (Facebook's CDN) the bridge downloads and sends along. */
  images?: string[];
  system: string;
  input: string;
  /** JSON Schema for the structured output. */
  schema: object;
};

export type EndTimeItem = { id: number; text: string; capturedAt: string };
export type BidItem = { id: number; seller: string | null; text: string };

const WEEKDAY = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Oslo", weekday: "long" });
const DAY = new Intl.DateTimeFormat("sv-SE", { timeZone: "Europe/Oslo", year: "numeric", month: "2-digit", day: "2-digit" });
/** "Saturday 2026-10-03" in Oslo time. */
export const osloDay = (iso: string) => `${WEEKDAY.format(new Date(iso))} ${DAY.format(new Date(iso))}`;

export function endTimeRequest(items: EndTimeItem[]): ClaudeRequest {
  return {
    task: "end-time",
    system: `You read Norwegian Facebook posts selling Pokémon cards. For each post, find when the sale ends, and when it starts if the post says ("Startid:", claim sales).
Each post says the day it was captured, time zone Europe/Oslo. Resolve weekdays and words like "ikveld" (tonight) and "i morgen" (tomorrow) relative to that day.
Give endsAt and startsAt as local time "YYYY-MM-DD HH:mm", or null if the post gives no clock time for it (a day alone is not enough) or doesn't say at all.`,
    input: items.map((i) => `### post ${i.id} (captured ${osloDay(i.capturedAt)})\n${i.text}`).join("\n\n"),
    schema: {
      type: "object",
      properties: {
        results: {
          type: "array",
          items: {
            type: "object",
            properties: { id: { type: "integer" }, endsAt: { type: ["string", "null"] }, startsAt: { type: ["string", "null"] } },
            required: ["id", "endsAt", "startsAt"],
          },
        },
      },
      required: ["results"],
    },
  };
}

/** What's stored for a post's times: see claudeEndsAt / claudeStartsAt in src/domain/endTime.ts. */
export type TimesAnswer = { endsAt: string | null; startsAt: string | null };

export function bidRequest(items: BidItem[]): ClaudeRequest {
  return {
    task: "bid",
    system: `You read replies under a lot in a Norwegian Facebook auction for Pokémon cards. Bidders usually tag the seller, shown as "@Seller", then give an amount in NOK.
For each reply give amount: the bid as an integer in NOK, or null if the reply is not a bid with a definite amount (a question, a dot to follow the lot, a message, or a relative amount like "10 more than the highest").
If the bidder corrects themselves, use the corrected amount. "2.5k" means 2500, "1.400" means 1400. A number followed by "?" is still a bid.`,
    // No names (2026-10-06): the seller's tag is "@Seller"; the cache key (bidAnswerKey) still uses the real text.
    input: items.map((i) => `${i.id}: ${JSON.stringify(tagAsSeller(i.text, i.seller))}`).join("\n"),
    schema: {
      type: "object",
      properties: {
        results: {
          type: "array",
          items: {
            type: "object",
            properties: { id: { type: "integer" }, amount: { type: ["integer", "null"] } },
            required: ["id", "amount"],
          },
        },
      },
      required: ["results"],
    },
  };
}

/** A short stable hash (FNV-1a) of an input, for caching answers so nothing is asked twice. */
export function hashText(s: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, "0") + s.length.toString(16);
}

export const endTimeAnswerKey = (text: string) => `end-time:${hashText(text)}`;

export const bidAnswerKey = (seller: string | null, text: string) => `bid:${hashText(`${seller ?? ""}\n${text}`)}`;

/** Every card in a claim lot's photo and its price (null = can't tell). */
export type ClaimPhotoAnswer = { cards: PhotoCards };

// Tried 2026-10-03 on a real lot (8 cards, prices on notes, 2 claimers): Sonnet listed all 8 with the
// right prices in 9 s; Haiku misread a price. Since 2026-10-06 the photo is read once, without the
// replies: who got what is matched by the rules (src/domain/bids.ts matchClaims), else claimMatchRequest.
export function claimPhotoRequest(item: ClaimPhotoInput): ClaudeRequest {
  return {
    task: "claim-lot",
    model: "sonnet",
    images: [item.imageUrl],
    system: `You read one lot in a Norwegian Facebook claim sale for Pokémon cards: a photo of the cards and the seller's text with the photo. Prices are written on the photo (a note by each card) or in the seller's text, sometimes per card for a kind of card ("Holo/rev.holo 5kr per stk", "EX/V/IR 10kr per stk": each such card costs that).
List every card in the photo, left to right, top to bottom: its name as printed on the card, and its price (from the photo or the seller's text). Use null for a price you can't tell. Two copies of the same card are two entries.`,
    input: item.lotText.trim() ? `The seller's text with the photo: ${JSON.stringify(item.lotText.trim())}` : "The seller wrote no text with the photo.",
    schema: {
      type: "object",
      properties: {
        cards: {
          type: "array",
          items: {
            type: "object",
            properties: { card: { type: "string" }, price: { type: ["integer", "null"] } },
            required: ["card", "price"],
          },
        },
      },
      required: ["cards"],
    },
  };
}

/** The same photo and text, the same cards: read once. The URL's query changes per read, so only its path counts. */
export const claimPhotoAnswerKey = (item: ClaimPhotoInput) => `claim-photo:${hashText(`${item.imageUrl.split("?")[0]}\n${item.lotText.trim()}`)}`;

export type ClaimMatchItem = ClaimMatchInput & { id: number };

/** Claims the rules couldn't match to a lot's cards, several lots per call (Haiku, text only, no names). */
export function claimMatchRequest(items: ClaimMatchItem[]): ClaudeRequest {
  return {
    task: "claim-match",
    system: `You read claims under lots in a Norwegian Facebook claim sale for Pokémon cards. For each lot you get its cards (numbered, as read from the photo) and the replies claiming them, oldest first. Each reply says who wrote it ("Me", "Claimer 1", ...); "@Seller" is the seller being tagged. Replies claim cards by name (sometimes misspelled or shortened, e.g. "feraligator", "zard"), several at once ("kingler og rapidash"), or "alle" for everything; a reply that's a question or a message claims nothing.
The first to claim a card gets it; a later claim on a taken card gets nothing, but a second copy of the same card goes to the next one to claim it. For each lot, give owners: one entry per card, in the cards' order, with who got it ("Me", "Claimer 1", ...) or null if nobody has claimed it.`,
    input: items
      .map(
        (i) =>
          `### lot ${i.id}\nCards:\n${i.cards.map((c, k) => `${k + 1}. ${c}`).join("\n")}\nReplies (oldest first):\n${i.claims.map((c, k) => `${k + 1}. ${c.who}: ${JSON.stringify(c.text)}`).join("\n")}`,
      )
      .join("\n\n"),
    schema: {
      type: "object",
      properties: {
        results: {
          type: "array",
          items: {
            type: "object",
            properties: { id: { type: "integer" }, owners: { type: "array", items: { type: ["string", "null"] } } },
            required: ["id", "owners"],
          },
        },
      },
      required: ["results"],
    },
  };
}

/** Same cards and same claims (by label, never names) → same answer; a new claim asks again. */
export const claimMatchAnswerKey = (input: ClaimMatchInput) => `claim-match:${hashText(JSON.stringify([input.cards, input.claims]))}`;

/**
 * A lot to name: the rules found no name in its text (only a price, a word like "Holo", or
 * nothing; see lotTextInfo), so Claude reads the text and the photo.
 */
export type LotNameItem = { imageUrl: string; text: string };
export type LotNameAnswer = string | null;

// Tried 2026-10-04 on 12 real lot photos in one call: Sonnet named all 12 with set numbers in 6 s;
// Haiku named them too but without numbers, in 20 s.
export function lotNameRequest(items: LotNameItem[]): ClaudeRequest {
  return {
    task: "lot-name",
    model: "sonnet",
    images: items.map((i) => i.imageUrl),
    system: `You name lots in a Norwegian Facebook auction or claim sale for Pokémon cards. Each photo is one lot, with the seller's text for it (often only a price, or a word like "Holo"). For each photo give a short name for the lot (at most about 60 characters). Use what the seller's text says first; read the photo for what it leaves out: text the seller wrote on or over the photo naming what it is, otherwise the card name as printed on the card, plus its set number (e.g. 74/112) if you can read it; for several cards, name them briefly (e.g. "Pikachu, Raichu" or "3 Eevee cards"). Keep a finish the text gives ("Charizard 4/102 holo"). Leave out prices and the condition. Use null if you can't tell.`,
    input: `There ${items.length === 1 ? "is 1 photo" : `are ${items.length} photos`}, numbered in order. The seller's text for each:\n${items
      .map((i, n) => `Photo ${n + 1}: ${i.text.trim() ? JSON.stringify(i.text.trim()) : "(no text)"}`)
      .join("\n")}\nName each lot.`,
    schema: {
      type: "object",
      properties: {
        lots: {
          type: "array",
          items: { type: "object", properties: { photo: { type: "integer" }, name: { type: ["string", "null"] } }, required: ["photo", "name"] },
        },
      },
      required: ["lots"],
    },
  };
}

/** Same photo and text, same name: only the photo's path counts (its query changes per read). An edited text asks again. */
export const lotNameAnswerKey = (imageUrl: string, text: string) => `lot-name:${hashText(`${imageUrl.split("?")[0]}\n${text.trim()}`)}`;
