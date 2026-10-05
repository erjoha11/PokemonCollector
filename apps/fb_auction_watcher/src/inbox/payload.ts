import { osloDate } from "../domain/endTime";
import { lotUrl } from "../shared/urls";
import type { WonState } from "../shared/settings";
import { wonBySeller, type Row } from "../pages/dashboard/model";

// What leaves the browser for tcg_inventory (issue #309): your own wins, nothing else. One item
// per won lot, from the same rules as To pay (`wonBySeller`): seller, sale type, end date, links,
// the lot's label and price, the seller's shipping/payment terms, and your Paid/Received marks.
// Never raw captures, other bidders' names or comments. The contract is in docs/spec.md "Sending
// wins to tcg_inventory"; tcg_inventory's won_inbox.py is the reader, and the committed fixture
// (repo root tests/fixtures/won-inbox.v1.json) keeps the two in step.

export const WON_FORMAT = "fbaw-won";
export const WON_VERSION = 1;

export type WonPayloadItem = {
  /** `fbaw:<postId>:<commentId>`, or `fbaw:<postId>:pos<n>` when the lot has no comment ID. */
  external_ref: string;
  seller: string | null;
  sale_type: "auction" | "claim" | "fixed";
  /** The sale's end date in Europe/Oslo (YYYY-MM-DD): its end time, or when you marked it ended if earlier. Null when it has neither. */
  ended_on: string | null;
  post_url: string;
  lot_url: string;
  /** "9. Gengar 151 reverse holo"; for a claim lot, the cards you got. */
  label: string;
  /** What you pay in kr (the winning bid, or the claimed cards' prices), or null when not known yet. */
  price: number | null;
  shipping_text: string | null;
  payment_text: string | null;
  paid_at: string | null;
  received_at: string | null;
};

export type WonPayload = { format: typeof WON_FORMAT; version: typeof WON_VERSION; sent_at: string; items: WonPayloadItem[] };

export function externalRef(postId: string, lot: { commentId: string | null; position: number }): string {
  return lot.commentId ? `fbaw:${postId}:${lot.commentId}` : `fbaw:${postId}:pos${lot.position}`;
}

const pad = (n: number) => String(n).padStart(2, "0");

/** When the sale ended (your mark if it came first), as an Oslo date, or null. */
function endedOn(r: Pick<Row, "endsAtMs" | "endedByYouAt">): string | null {
  const times = [r.endsAtMs, r.endedByYouAt ? Date.parse(r.endedByYouAt) : null].filter((x): x is number => x !== null && !Number.isNaN(x));
  if (!times.length) return null;
  const d = osloDate(new Date(Math.min(...times)));
  return `${d.year}-${pad(d.month)}-${pad(d.day)}`;
}

/**
 * The v1 payload of everything you've won (pure). Claim lots are always one lot-level item, even
 * when Claude priced each card: per-card refs would change once the photo is read and leave
 * duplicates behind in the inbox.
 */
export function buildWonPayload(rows: Row[], wonState: WonState, sentAt: Date, onlyPostIds?: ReadonlySet<string>): WonPayload {
  const items: WonPayloadItem[] = [];
  for (const group of wonBySeller(rows)) {
    for (const { row: r, lot: l, label, kr } of group.items) {
      if (onlyPostIds && !onlyPostIds.has(r.id)) continue; // Only the won auctions you picked.
      const mark = wonState[r.id];
      items.push({
        external_ref: externalRef(r.id, l),
        seller: r.sellerName ?? null,
        sale_type: r.type === "claim" || r.type === "fixed" ? r.type : "auction",
        ended_on: endedOn(r),
        post_url: r.url,
        lot_url: lotUrl(r, l),
        label,
        price: kr,
        shipping_text: r.shippingText ?? null,
        payment_text: r.paymentText ?? null,
        paid_at: mark?.paidAt ?? null,
        received_at: mark?.receivedAt ?? null,
      });
    }
  }
  return { format: WON_FORMAT, version: WON_VERSION, sent_at: sentAt.toISOString(), items };
}
