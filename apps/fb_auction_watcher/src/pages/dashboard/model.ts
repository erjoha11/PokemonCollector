import { interpretLots, summarizeLots, type Lot, type LotSummary } from "../../domain/bids";
export type { Lot } from "../../domain/bids";
export { saleLines } from "../../domain/saleLines";
export { lotUrl } from "../../shared/urls";
import { canonicalPostUrl } from "../../shared/urls";
import { interpretListing, isUntypedSale, type Interpretation } from "../../domain/listing";
import { claudeEndsAt, osloDate } from "../../domain/endTime";
import { bidAnswerKey, claimLotAnswerKey, endTimeAnswerKey, lotNameAnswerKey, type ClaimLotAnswer } from "../../llm/prompts";
import type { PostCapture } from "../../shared/capture";
import type { StoredPost } from "../../shared/feed";

// The table's logic, without any DOM: stored raw posts → interpreted rows → tabs (see `tabs`).

export type Row = StoredPost &
  Interpretation & {
    endsAtMs: number | null;
    /** First seen after the previous visit to this page, or less than 30 min ago (NEW_WINDOW_MS; a rescan or visit inside that window doesn't clear it). */
    isNew: boolean;
    /** End time passed, but within the antisnipe window: bids may still extend it. */
    maybeEnded: boolean;
    ended: boolean;
    /** When it became ended: its end time + antisnipe window, or your mark if that came first. Null while not ended. */
    endedAtMs: number | null;
    /** Ended less than 30 min ago (ENDED_GRACE_MS): still shown, as ended, in the tabs it was in just before (#320). */
    justEnded: boolean;
    /** You marked the sale as ended yourself (ISO), or null. It then counts as ended from that moment. */
    endedByYouAt: string | null;
    /** The end time came from Claude (the rules couldn't read it). */
    endsViaClaude: boolean;
    /** From the latest post read with the icon, if any. */
    lots: Lot[] | null;
    summary: LotSummary | null;
    lastReadAt: string | null;
    /** The last read that loaded every comment (reads are merged; see src/domain/captures.ts). */
    lastCompleteReadAt: string | null;
    /** Lots you marked as outbid yourself (#329; "Not won" marks): lot ref (`lotRef`) → when. Optional so test rows can leave it out. */
    notWon?: Record<string, string>;
  };

/** Post reads, Claude's cached answers, and your name, for lots, bids and your status. */
export type RowExtras = {
  captures?: Map<string, PostCapture>;
  answers?: Map<string, unknown>;
  myName?: string;
  /** Sales you marked as ended yourself (post ID → when). */
  endedMarks?: Record<string, string>;
  /** Lots you marked "Not won" yourself (`notWonKey` → when). */
  notWonMarks?: Record<string, string>;
};

/** A lot's ref within its sale: its comment ID, or "pos<n>" when it has none (as in the inbox's external_ref). */
export const lotRef = (l: Pick<Lot, "commentId" | "position">) => l.commentId ?? `pos${l.position}`;
/** The key of a lot's "Not won" mark: `<post ID>:<lot ref>`. */
export const notWonKey = (postId: string, l: Pick<Lot, "commentId" | "position">) => `${postId}:${lotRef(l)}`;

export type TabId = "new" | "today" | "upcoming" | "noend" | "mine" | "ended";
/** A tab's sales, in sections when it holds more than one kind ("Yours" / "Everyone else"). */
export type Tab = { id: TabId; label: string; sections: { label: string | null; rows: Row[] }[]; count: number };

const HOUR = 3_600_000;
/** A sale stays New this long after it was first seen, whatever the last visit (#320). */
export const NEW_WINDOW_MS = 30 * 60_000;
/** An ended sale stays in the active tabs (all but Ended) this long after it ended (#320). */
export const ENDED_GRACE_MS = 30 * 60_000;

/**
 * Sales only: wanted and trade posts are left out of the table, and so are posts of no known type
 * unless they're laid out as a sale (`isUntypedSale`): those stay, typed "other" (shown "Unknown").
 */
export function buildRows(posts: StoredPost[], now: Date, lastVisit: Date | null, extras: RowExtras = {}): Row[] {
  const { captures = new Map(), answers = new Map(), myName = "", endedMarks = {}, notWonMarks = {} } = extras;
  const rows: Row[] = [];
  for (const p of posts) {
    const i = interpretListing(p.text, new Date(p.firstSeenAt));
    if (i.type === "wanted" || i.type === "trade" || (i.type === "other" && !isUntypedSale(i))) continue;
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
    const endedByYouAt = endedMarks[p.id] ?? null;
    const ended = endedByYouAt !== null || (endsAtMs !== null && t >= endsAtMs + softMs);
    const closes = [endsAtMs === null ? null : endsAtMs + softMs, endedByYouAt ? Date.parse(endedByYouAt) : null].filter((x): x is number => x !== null);
    const endedAt = ended ? Math.min(...closes) : null;
    const firstSeen = Date.parse(p.firstSeenAt);
    const notWon: Record<string, string> = {};
    for (const l of lots ?? []) {
      const at = notWonMarks[notWonKey(p.id, l)];
      if (at) notWon[lotRef(l)] = at;
    }
    rows.push({
      ...p,
      // Posts saved from the photo viewer had a photo.php link; link them by their own address.
      url: canonicalPostUrl(p.url, p.id, p.groupSlug),
      ...i,
      endsAtMs,
      isNew: (lastVisit !== null && firstSeen > lastVisit.getTime()) || t - firstSeen < NEW_WINDOW_MS,
      maybeEnded: !endedByYouAt && endsAtMs !== null && t >= endsAtMs && t < endsAtMs + softMs,
      ended,
      endedAtMs: endedAt,
      justEnded: endedAt !== null && t - endedAt < ENDED_GRACE_MS,
      endedByYouAt,
      endsViaClaude,
      lots,
      summary: lots ? summarizeLots(lots) : null,
      lastReadAt: capture?.capturedAt ?? null,
      // Reads saved before merging existed have no completeAt: treat them as complete, as before.
      lastCompleteReadAt: capture ? (capture.completeAt === undefined ? capture.capturedAt : capture.completeAt) : null,
      notWon,
    });
  }
  return rows;
}

/** For sorting ended sales: your mark if it came first (or there's no end time), else its end time. */
function endedSortMs(r: Row): number {
  const times = [r.endsAtMs, r.endedByYouAt ? Date.parse(r.endedByYouAt) : null].filter((x): x is number => x !== null);
  return times.length ? Math.min(...times) : 0;
}

const sameOsloDay = (a: Date, b: Date) => {
  const x = osloDate(a);
  const y = osloDate(b);
  return x.year === y.year && x.month === y.month && x.day === y.day;
};

/**
 * The overview's tabs (2026-10-04; they replaced stacked groups). Claim sales sit in Today /
 * Upcoming by their end time like auctions. A tab with more than one kind of sale has sections.
 */
export function tabs(rows: Row[], now: Date): Tab[] {
  const t = now.getTime();
  const byEnd = (a: Row, b: Row) => (a.endsAtMs ?? Infinity) - (b.endsAtMs ?? Infinity);
  const bySeen = (a: Row, b: Row) => b.firstSeenAt.localeCompare(a.firstSeenAt);
  const newestEnded = (a: Row, b: Row) => endedSortMs(b) - endedSortMs(a);
  // The active tabs (all but Ended) hold running sales, plus sales that ended under 30 min ago
  // (#320), shown as ended, in the tab they were in just before they ended.
  const running = rows.filter((r) => !r.ended || r.justEnded);
  const timed = running.filter((r) => r.type !== "fixed" && r.endsAtMs !== null);
  // Today, or within the hour (just before midnight); the countdown turns red under an hour. A
  // just-ended sale is judged at the moment it ended (a sale marked ended early can be Upcoming).
  const isToday = (r: Row) => {
    const at = r.ended ? r.endedAtMs! : t;
    return r.maybeEnded || r.endsAtMs! - at < HOUR || sameOsloDay(new Date(r.endsAtMs!), new Date(at));
  };
  const ended = rows.filter((r) => r.ended).sort(newestEnded);
  const mine = rows.filter(isMine);
  const { yours, others } = splitEnded(ended);
  const list: [TabId, string, [string | null, Row[]][]][] = [
    ["new", "New", [[null, running.filter((r) => r.isNew).sort(bySeen)]]],
    ["today", "Today", [[null, timed.filter(isToday).sort(byEnd)]]], // "Ended?" (antisnipe window) first.
    ["upcoming", "Upcoming", [[null, timed.filter((r) => !isToday(r)).sort(byEnd)]]],
    [
      "noend",
      "No end",
      [
        ["End time unknown", running.filter((r) => r.type !== "fixed" && r.endsAtMs === null).sort(bySeen)],
        ["Fixed price", running.filter((r) => r.type === "fixed").sort(bySeen)],
      ],
    ],
    [
      "mine",
      "My bids",
      [
        // A sale that ended under 30 min ago stays under Running (shown as ended), then moves down.
        ["Running", mine.filter((r) => !r.ended || r.justEnded).sort(byEnd)],
        ["Ended", mine.filter((r) => r.ended && !r.justEnded).sort(newestEnded)],
      ],
    ],
    ["ended", "Ended", [["Yours", yours], ["Everyone else", others]]],
  ];
  return list.map(([id, label, parts]) => {
    const sections = parts.filter(([, r]) => r.length).map(([l, r]) => ({ label: l, rows: r }));
    return { id, label, sections, count: sections.reduce((n, x) => n + x.rows.length, 0) };
  });
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
 * partial read may have missed the bid that beat you (review H2). A sale you marked as ended
 * yourself takes your word for it: its last full read is final.
 */
export function readAfterEnd(r: Pick<Row, "ended" | "endsAtMs" | "softCloseMinutes" | "lastCompleteReadAt" | "endedByYouAt">): boolean {
  if (!r.ended || !r.lastCompleteReadAt) return false;
  if (r.endedByYouAt) return true;
  if (r.endsAtMs === null) return false;
  return Date.parse(r.lastCompleteReadAt) >= r.endsAtMs + (r.softCloseMinutes ?? 0) * 60_000;
}

/** When you marked this lot as outbid yourself (#329; stored as a "Not won" mark), or null. */
export const notWonAt = (r: Pick<Row, "notWon">, l: Pick<Lot, "commentId" | "position">): string | null => r.notWon?.[lotRef(l)] ?? null;

/**
 * Your status on a lot. Your outbid mark (#329) overrides what the rules read for a lot you bid on
 * or claimed: the lot is lost to someone else, as if a bid had beaten yours (an orange "Outbid"),
 * so it's off To pay and out of what's sent to tcg_inventory, until you undo it.
 */
export function lotStatus(r: Row, l: Lot): LotStatus {
  const status = lotStatusByRules(r, l);
  if (status.key !== "none" && notWonAt(r, l)) return { key: "lost", label: "Outbid (your mark)", cls: "outbid" };
  return status;
}

function lotStatusByRules(r: Row, l: Lot): LotStatus {
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

/** A lot you marked as outbid (#329), for To pay's list of them with Undo. */
export type NotWonItem = { row: Row; lot: Lot; key: string; at: string };

/** Every lot you marked as outbid, the latest mark first. */
export function notWonLots(rows: Row[]): NotWonItem[] {
  const items: NotWonItem[] = [];
  for (const row of rows) {
    for (const lot of row.lots ?? []) {
      const at = notWonAt(row, lot);
      if (at && lotStatusByRules(row, lot).key !== "none") items.push({ row, lot, key: notWonKey(row.id, lot), at });
    }
  }
  return items.sort((a, b) => b.at.localeCompare(a.at));
}

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
    sales.push({ row, lots, kr: lots.reduce((n, l) => n + (l.myHighestBid ?? 0), 0), awaitingFinalRead: row.ended && !readAfterEnd(row) });
  }
  return sales.sort((a, b) => (a.row.endsAtMs ?? Infinity) - (b.row.endsAtMs ?? Infinity));
}

/** A link to the lot's own comment on Facebook (where you'd bid), else to the post. */


/** Did you bid or claim in this sale (from its latest read)? */
export const isMine = (r: Pick<Row, "summary">): boolean =>
  !!r.summary && r.summary.lead + r.summary.outbid + r.summary.unclear + r.summary.claimed + r.summary.check > 0;

/** The Ended group in two parts: sales you were in first (where Lost lots live), then the rest. Order kept. */
export function splitEnded(rows: Row[]): { yours: Row[]; others: Row[] } {
  return { yours: rows.filter(isMine), others: rows.filter((r) => !isMine(r)) };
}

/**
 * How an ended sale went, from its latest read: lots that sold (a valid bid, or a claim) and what
 * they went for (winning bids; claim cards Claude priced). `final` when it was read in full after
 * the end; otherwise it's "at last read". Null when the sale was never read.
 */
export type SaleResult = { lots: number; sold: number; kr: number; unknownPrices: number; final: boolean; readAt: string | null };

export function saleResult(r: Row): SaleResult | null {
  if (!r.lots) return null;
  const claims = r.type === "claim" || r.type === "fixed";
  let sold = 0;
  let kr = 0;
  let unknownPrices = 0;
  for (const l of r.lots) {
    if (claims) {
      const taken = l.claimCards?.filter((x) => x.claimedBy) ?? [];
      if (taken.length || l.claims.length) sold++;
      for (const x of taken) {
        if (x.price === null) unknownPrices++;
        else kr += x.price;
      }
      if (!l.claimCards && l.claims.length) unknownPrices++; // Claimed, but nobody has read the photo's prices yet.
    } else if (l.highestBid !== null) {
      sold++;
      kr += l.highestBid;
    }
  }
  return { lots: r.lots.length, sold, kr, unknownPrices, final: readAfterEnd(r), readAt: r.lastCompleteReadAt };
}
