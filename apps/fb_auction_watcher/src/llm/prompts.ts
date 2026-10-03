// Prompts for what the rules can't read, sent to Claude Code (`claude -p`) through the native
// bridge (native/fbaw_claude_host.py). No API key: it runs on the user's own Claude login.
// Tried on src/llm/cases.ts (23 cases, Haiku, batched): 23/23 on 2026-10-03.

export type ClaudeRequest = {
  /** A short name for logs. */
  task: "end-time" | "bid" | "claim-lot";
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
    system: `You read Norwegian Facebook posts selling Pokémon cards. For each post, find when the sale ends.
Each post says the day it was captured, time zone Europe/Oslo. Resolve weekdays and words like "ikveld" (tonight) and "i morgen" (tomorrow) relative to that day.
Give endsAt as local time "YYYY-MM-DD HH:mm", or null if the post gives no clock time for the end (a day alone is not enough) or no end at all.`,
    input: items.map((i) => `### post ${i.id} (captured ${osloDay(i.capturedAt)})\n${i.text}`).join("\n\n"),
    schema: {
      type: "object",
      properties: {
        results: {
          type: "array",
          items: {
            type: "object",
            properties: { id: { type: "integer" }, endsAt: { type: ["string", "null"] } },
            required: ["id", "endsAt"],
          },
        },
      },
      required: ["results"],
    },
  };
}

export function bidRequest(items: BidItem[]): ClaudeRequest {
  return {
    task: "bid",
    system: `You read replies under a lot in a Norwegian Facebook auction for Pokémon cards. Each reply says who the seller is; bidders usually tag the seller's name, then give an amount in NOK.
For each reply give amount: the bid as an integer in NOK, or null if the reply is not a bid with a definite amount (a question, a dot to follow the lot, a message, or a relative amount like "10 more than the highest").
If the bidder corrects themselves, use the corrected amount. "2.5k" means 2500, "1.400" means 1400. A number followed by "?" is still a bid.`,
    input: items.map((i) => `${i.id}: (seller: ${i.seller ?? "unknown"}) ${JSON.stringify(i.text)}`).join("\n"),
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

/** One claim-sale lot: its full-size photo and the replies under it, oldest first. */
export type ClaimLotItem = { seller: string | null; imageUrl: string; replies: { author: string; text: string }[] };
/** Every card in the photo: its price, and who claimed it first (null = still for sale). */
export type ClaimLotAnswer = { cards: { card: string; price: number | null; claimedBy: string | null }[] };

// Tried 2026-10-03 on a real lot (8 cards, prices on notes, 2 claimers): Sonnet listed all 8 with the
// right prices and claims in 9 s; with only the claimed cards asked for, Haiku misread a price.
export function claimLotRequest(item: ClaimLotItem): ClaudeRequest {
  return {
    task: "claim-lot",
    model: "sonnet",
    images: [item.imageUrl],
    system: `You read one lot in a Norwegian Facebook claim sale for Pokémon cards: a photo of the cards with each price written on a note, and the replies under it, oldest first. Replies claim cards by name (often tagging the seller first, sometimes misspelled, e.g. "feraligator"), or "alle" for everything.
First to claim a card gets it. List every card in the photo, left to right, top to bottom: its name as printed on the card, the price written next to it on the photo, and who claimed it first, or null if nobody has (it's still for sale). Use null for a price you can't read. Two copies of the same card are two entries.`,
    input: `Seller: ${item.seller ?? "unknown"}\nReplies (oldest first):\n${item.replies.length ? item.replies.map((r, i) => `${i + 1}. ${r.author}: ${r.text || "(photo)"}`).join("\n") : "(none yet)"}`,
    schema: {
      type: "object",
      properties: {
        cards: {
          type: "array",
          items: {
            type: "object",
            properties: { card: { type: "string" }, price: { type: ["integer", "null"] }, claimedBy: { type: ["string", "null"] } },
            required: ["card", "price", "claimedBy"],
          },
        },
      },
      required: ["cards"],
    },
  };
}

/** Same photo and same replies → same answer; a new reply asks again. The URL's query changes per read, so only its path counts. */
export const claimLotAnswerKey = (item: ClaimLotItem) =>
  `claim-lot-cards:${hashText(`${item.imageUrl.split("?")[0]}\n${item.replies.map((r) => `${r.author}: ${r.text}`).join("\n")}`)}`;
