import { interpretLots, summarizeLots, type Lot, type LotSummary } from "../../domain/bids";
export type { Lot } from "../../domain/bids";
import { interpretListing, type Interpretation } from "../../domain/listing";
import { claudeEndsAt, osloDate } from "../../domain/endTime";
import { bidAnswerKey, claimLotAnswerKey, endTimeAnswerKey, lotNameAnswerKey, type ClaimLotAnswer } from "../../llm/prompts";
import type { PostCapture } from "../../shared/capture";
import type { StoredPost } from "../../shared/feed";

// The table's logic, without any DOM: stored raw posts → interpreted rows → groups.
// Groups: today (including anything within the hour) · tomorrow and later · claim/fixed price ·
// ended, plus "end time unknown" for auctions the rules couldn't read. (docs/spec.md planned a
// separate "within 1 h" group; merged into Today on 2026-10-04, since the countdown turns red under
// an hour and the "Within 1 hour" counter stays.)

export type Row = StoredPost &
  Interpretation & {
    endsAtMs: number | null;
    /** First seen after the previous visit to this page. */
    isNew: boolean;
    /** End time passed, but within the antisnipe window: bids may still extend it. */
    maybeEnded: boolean;
    ended: boolean;
    /** The end time came from Claude (the rules couldn't read it). */
    endsViaClaude: boolean;
    /** From the latest post read with the icon, if any. */
    lots: Lot[] | null;
    summary: LotSummary | null;
    lastReadAt: string | null;
    /** The last read that loaded every comment (reads are merged; see src/domain/captures.ts). */
    lastCompleteReadAt: string | null;
  };

/** Post reads, Claude's cached answers, and your name, for lots, bids and your status. */
export type RowExtras = {
  captures?: Map<string, PostCapture>;
  answers?: Map<string, unknown>;
  myName?: string;
};

export type GroupId = "today" | "later" | "unknown" | "claim-fixed" | "ended";
export type Group = { id: GroupId; label: string; rows: Row[] };

const HOUR = 3_600_000;

/** Sales only: wanted, trade and unrecognized posts are left out of the table. */
export function buildRows(posts: StoredPost[], now: Date, lastVisit: Date | null, extras: RowExtras = {}): Row[] {
  const { captures = new Map(), answers = new Map(), myName = "" } = extras;
  const rows: Row[] = [];
  for (const p of posts) {
    const i = interpretListing(p.text, new Date(p.firstSeenAt));
    if (i.type === "wanted" || i.type === "trade" || i.type === "other") continue;
    let endsViaClaude = false;
    if (!i.endsAt) {
      const fromClaude = claudeEndsAt(answers.get(endTimeAnswerKey(p.text)));
      if (fromClaude) {
        i.endsAt = fromClaude;
        i.sure = true;
        endsViaClaude = true;
      }
    }
    const capture = captures.get(p.id) ?? null;
    const lots = capture
      ? interpretLots(capture, {
          myName,
          claims: i.type === "claim" || i.type === "fixed",
          listingIncrement: i.increment,
          listingMinPrice: i.minPrice,
          claimAnswer: (input) => answers.get(claimLotAnswerKey(input)) as ClaimLotAnswer | undefined,
          lotName: (imageUrl) => answers.get(lotNameAnswerKey(imageUrl)) as string | null | undefined,
          answer: (seller, text) => {
            const key = bidAnswerKey(seller, text);
            return answers.has(key) ? (answers.get(key) as number | null) : undefined;
          },
        })
      : null;
    const endsAtMs = i.endsAt ? Date.parse(i.endsAt) : null;
    const softMs = (i.softCloseMinutes ?? 0) * 60_000;
    const t = now.getTime();
    rows.push({
      ...p,
      ...i,
      endsAtMs,
      isNew: lastVisit !== null && Date.parse(p.firstSeenAt) > lastVisit.getTime(),
      maybeEnded: endsAtMs !== null && t >= endsAtMs && t < endsAtMs + softMs,
      ended: endsAtMs !== null && t >= endsAtMs + softMs,
      endsViaClaude,
      lots,
      summary: lots ? summarizeLots(lots) : null,
      lastReadAt: capture?.capturedAt ?? null,
      // Reads saved before merging existed have no completeAt: treat them as complete, as before.
      lastCompleteReadAt: capture ? (capture.completeAt === undefined ? capture.capturedAt : capture.completeAt) : null,
    });
  }
  return rows;
}

const sameOsloDay = (a: Date, b: Date) => {
  const x = osloDate(a);
  const y = osloDate(b);
  return x.year === y.year && x.month === y.month && x.day === y.day;
};

export function groupRows(rows: Row[], now: Date): Group[] {
  const t = now.getTime();
  const groups: Record<GroupId, Row[]> = { today: [], later: [], unknown: [], "claim-fixed": [], ended: [] };
  for (const r of rows) {
    if (r.ended) groups.ended.push(r);
    else if (r.type !== "auction") groups["claim-fixed"].push(r);
    else if (r.endsAtMs === null) groups.unknown.push(r);
    // Today, or within the hour (just before midnight); the countdown turns red under an hour.
    else if (r.maybeEnded || r.endsAtMs - t < HOUR || sameOsloDay(new Date(r.endsAtMs), now)) groups.today.push(r);
    else groups.later.push(r);
  }
  const byEnd = (a: Row, b: Row) => (a.endsAtMs ?? Infinity) - (b.endsAtMs ?? Infinity);
  const bySeen = (a: Row, b: Row) => b.firstSeenAt.localeCompare(a.firstSeenAt);
  groups.today.sort(byEnd); // "Ended?" (in the antisnipe window) first: their end time has passed.
  groups.later.sort(byEnd);
  groups.unknown.sort(bySeen);
  // Claim sales by end time first, then fixed-price posts (no end) newest first.
  groups["claim-fixed"].sort((a, b) => byEnd(a, b) || bySeen(a, b));
  groups.ended.sort((a, b) => (b.endsAtMs ?? 0) - (a.endsAtMs ?? 0));
  const labels: Record<GroupId, string> = {
    today: "Today",
    later: "Tomorrow and later",
    unknown: "End time unknown",
    "claim-fixed": "Claim and fixed price",
    ended: "Ended",
  };
  return (Object.keys(groups) as GroupId[]).map((id) => ({ id, label: labels[id], rows: groups[id] }));
}

export type Counts = { active: number; withinHour: number; outbid: number; isNew: number };

export function countRows(rows: Row[], now: Date): Counts {
  const t = now.getTime();
  const active = rows.filter((r) => !r.ended && r.type !== "fixed");
  return {
    active: active.length,
    withinHour: active.filter((r) => r.endsAtMs !== null && (r.maybeEnded || r.endsAtMs - t < HOUR)).length,
    // "Leading?" counts with outbid: act on it as if you might be.
    outbid: active.filter((r) => (r.summary?.outbid ?? 0) + (r.summary?.unclear ?? 0) + (r.summary?.check ?? 0) > 0).length,
    isNew: rows.filter((r) => r.isNew && !r.ended).length,
  };
}

/** "12:05" under an hour (mm:ss), "5 h 12 min" under a day, else "2 d 4 h". */
export function countdown(endsAtMs: number, now: Date): string {
  const ms = endsAtMs - now.getTime();
  if (ms <= 0) return "ended";
  const s = Math.floor(ms / 1000);
  const d = Math.floor(s / 86_400);
  const h = Math.floor((s % 86_400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (s < 3600) return `${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  if (d === 0) return `${h} h ${m} min`;
  return `${d} d ${h} h`;
}

const WHEN = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Oslo", weekday: "short", hour: "2-digit", minute: "2-digit" });
const DAY = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Oslo", day: "numeric", month: "short" });

/** "Sun 21:00", plus the date when it isn't within the coming week. */
export function endLabel(endsAtMs: number, now: Date): string {
  const at = new Date(endsAtMs);
  const far = Math.abs(endsAtMs - now.getTime()) > 6 * 86_400_000;
  return far ? `${WHEN.format(at)}, ${DAY.format(at)}` : WHEN.format(at);
}

/** "5 min ago", "3 h ago", "2 d ago". */
export function ago(iso: string, now: Date): string {
  const s = Math.max(0, Math.floor((now.getTime() - Date.parse(iso)) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86_400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86_400)} d ago`;
}

/** How a lot shows for you: one place for these rules (they used to be repeated in main.ts). */
export type LotStatus = {
  key: "none" | "leading" | "unclear" | "outbid" | "won" | "lost" | "leading-at-last-read" | "outbid-at-last-read" | "check";
  label: string;
  /** CSS class: lead (blue), won (green), outbid (orange). */
  cls: "lead" | "won" | "outbid" | "none";
};

/**
 * Has the sale been read completely after it ended (end time + antisnipe)? Only then can
 * "Leading" become "Won": the last minutes are exactly when people get outbid (review H3), and a
 * partial read may have missed the bid that beat you (review H2).
 */
export function readAfterEnd(r: Pick<Row, "ended" | "endsAtMs" | "softCloseMinutes" | "lastCompleteReadAt">): boolean {
  if (!r.ended || r.endsAtMs === null || !r.lastCompleteReadAt) return false;
  return Date.parse(r.lastCompleteReadAt) >= r.endsAtMs + (r.softCloseMinutes ?? 0) * 60_000;
}

export function lotStatus(r: Row, l: Lot): LotStatus {
  if (r.type === "claim" || r.type === "fixed") {
    if (l.myClaim === "claimed") return { key: "won", label: "Won", cls: "won" };
    if (l.myClaim === "check") return { key: "check", label: "Check: someone was earlier", cls: "outbid" };
    return { key: "none", label: "", cls: "none" };
  }
  const final = readAfterEnd(r);
  switch (l.myStatus) {
    case "lead":
      if (!r.ended) return { key: "leading", label: "Leading", cls: "lead" };
      return final ? { key: "won", label: "Won", cls: "won" } : { key: "leading-at-last-read", label: "Leading at last read", cls: "lead" };
    case "unclear":
      return { key: "unclear", label: "Leading?", cls: "outbid" };
    case "outbid":
      if (!r.ended) return { key: "outbid", label: "Outbid", cls: "outbid" };
      return final ? { key: "lost", label: "Lost", cls: "outbid" } : { key: "outbid-at-last-read", label: "Outbid at last read", cls: "outbid" };
    default:
      return { key: "none", label: "", cls: "none" };
  }
}

/** What you won in a claim sale (from Claude's reading of the photos): total kr, and cards whose price couldn't be read. */
export function wonTotal(lots: Lot[]): { cards: number; kr: number; unknown: number } {
  const mine = lots.flatMap((l) => l.claimCards?.filter((x) => x.isMe) ?? []);
  return {
    cards: mine.length,
    kr: mine.reduce((n, x) => n + (x.price ?? 0), 0),
    unknown: mine.filter((x) => x.price === null).length,
  };
}

/** "400 kr", or "400 kr + ?" when some prices couldn't be read. */
export const krText = (t: { kr: number; unknown: number }) => `${t.kr} kr${t.unknown ? " + ?" : ""}`;

/** One thing you won: a lot (auction) or the cards you got in a claim lot, and what it costs. */
export type WonItem = {
  row: Row;
  lot: Lot;
  /** "Lot 9" / "7. Marowak, Feraligatr". */
  label: string;
  /** What you pay, or null when it isn't known yet (a claim lot Claude hasn't read). */
  kr: number | null;
};

/** Everything won from one seller: you pay per seller. */
export type WonSeller = {
  seller: string;
  items: WonItem[];
  /** Sum of the known prices. */
  kr: number;
  /** Items whose price isn't known yet. */
  unknown: number;
  /** The sales involved, newest first (for their shipping/payment lines and links). */
  rows: Row[];
};

/**
 * Everything you've won, grouped by seller, sellers with the most recent sale first. Auctions
 * count once a complete read after the end confirms the win (lotStatus "won"); claims count when
 * you were first on what you claimed, priced from Claude's reading of the photo when it has one.
 */
export function wonBySeller(rows: Row[]): WonSeller[] {
  const bySeller = new Map<string, WonSeller>();
  for (const r of rows) {
    for (const l of r.lots ?? []) {
      if (lotStatus(r, l).key !== "won") continue;
      let label: string;
      let kr: number | null;
      if (r.type === "claim" || r.type === "fixed") {
        const mine = l.claimCards?.filter((x) => x.isMe) ?? [];
        const named = l.claims.filter((x) => x.isMe).flatMap((x) => (x.all ? ["everything"] : x.items));
        label = `${l.position}. ${mine.length ? mine.map((x) => x.card).join(", ") : named.join(", ") || l.title}`;
        if (mine.length) {
          kr = mine.every((x) => x.price !== null) ? mine.reduce((n, x) => n + x.price!, 0) : null;
        } else if (l.textPrice) {
          // Claude hasn't read the photo yet: the lot's text price, per card you named if it's per card.
          kr = l.textPrice.perCard ? (named.length && !named.includes("everything") ? l.textPrice.kr * named.length : null) : l.textPrice.kr;
        } else {
          kr = null;
        }
      } else {
        label = `${l.position}. ${l.title}`;
        kr = l.highestBid;
      }
      const seller = r.sellerName ?? "Unknown seller";
      const group = bySeller.get(seller) ?? { seller, items: [], kr: 0, unknown: 0, rows: [] };
      group.items.push({ row: r, lot: l, label, kr });
      if (kr === null) group.unknown++;
      else group.kr += kr;
      if (!group.rows.includes(r)) group.rows.push(r);
      bySeller.set(seller, group);
    }
  }
  const latest = (g: WonSeller) => Math.max(...g.rows.map((r) => r.endsAtMs ?? Date.parse(r.lastSeenAt)));
  return [...bySeller.values()].sort((a, b) => latest(b) - latest(a));
}

// ── My Auctions (2026-10-04 redesign) ─────────────────────────────────────────────────────────
// Its purpose, in order of urgency: 1. do I need to act now (outbid, "Leading?", a claim someone
// was earlier on)? 2. am I fine (leading; what it costs if it holds)? 3. what do I owe (won, per
// seller, until paid and received)? Lost lots and finished sales live in the table, not here.

/** A lot that needs you: you can still act on it (a running auction, or a claim). */
export type NeedsYouItem = {
  row: Row;
  lot: Lot;
  status: LotStatus;
  /** The lowest bid that would count now: highest + minimum raise (or the start bid); null for claims. */
  nextBid: number | null;
};

const NEEDS_YOU: LotStatus["key"][] = ["outbid", "unclear", "check"];

/** Every lot that needs you, across sales, the one ending soonest first. */
export function needsYou(rows: Row[]): NeedsYouItem[] {
  const items: NeedsYouItem[] = [];
  for (const row of rows) {
    if (row.ended) continue;
    for (const lot of row.lots ?? []) {
      const status = lotStatus(row, lot);
      if (!NEEDS_YOU.includes(status.key)) continue;
      const nextBid =
        status.key === "check" ? null : lot.highestBid !== null ? lot.highestBid + (lot.increment ?? 1) : (lot.startBid ?? null);
      items.push({ row, lot, status, nextBid });
    }
  }
  const end = (i: NeedsYouItem) => i.row.endsAtMs ?? Infinity;
  return items.sort((a, b) => end(a) - end(b) || a.lot.position - b.lot.position);
}

/** A sale where you're leading on some lots: what you'd pay if they hold. */
export type LeadingSale = {
  row: Row;
  lots: Lot[];
  kr: number;
  /** Ended, but not read since: it's "Leading at last read" until a final read confirms it. */
  awaitingFinalRead: boolean;
};

export function leadingBySale(rows: Row[]): LeadingSale[] {
  const sales: LeadingSale[] = [];
  for (const row of rows) {
    const lots = (row.lots ?? []).filter((l) => {
      const k = lotStatus(row, l).key;
      return k === "leading" || k === "leading-at-last-read";
    });
    if (lots.length === 0) continue;
    sales.push({ row, lots, kr: lots.reduce((n, l) => n + (l.myHighestBid ?? 0), 0), awaitingFinalRead: row.ended });
  }
  return sales.sort((a, b) => (a.row.endsAtMs ?? Infinity) - (b.row.endsAtMs ?? Infinity));
}

/** A link to the lot's own comment on Facebook (where you'd bid), else to the post. */
export function lotUrl(row: Pick<Row, "url">, lot: Pick<Lot, "commentId">): string {
  if (!lot.commentId) return row.url;
  return `${row.url}${row.url.includes("?") ? "&" : "?"}comment_id=${lot.commentId}`;
}
