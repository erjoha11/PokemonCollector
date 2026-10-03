import { interpretListing, type Interpretation } from "../../domain/listing";
import { osloDate } from "../../domain/endTime";
import type { StoredPost } from "../../shared/feed";

// The table's logic, without any DOM: stored raw posts → interpreted rows → groups.
// Groups follow docs/spec.md "Table page": within 1 h · today · tomorrow and later ·
// claim/fixed price · ended, plus "end time unknown" for auctions the rules couldn't read.

export type Row = StoredPost &
  Interpretation & {
    endsAtMs: number | null;
    /** First seen after the previous visit to this page. */
    isNew: boolean;
    /** End time passed, but within the antisnipe window: bids may still extend it. */
    maybeEnded: boolean;
    ended: boolean;
  };

export type GroupId = "soon" | "today" | "later" | "unknown" | "claim-fixed" | "ended";
export type Group = { id: GroupId; label: string; rows: Row[] };

const HOUR = 3_600_000;

/** Sales only: wanted, trade and unrecognized posts are left out of the table. */
export function buildRows(posts: StoredPost[], now: Date, lastVisit: Date | null): Row[] {
  const rows: Row[] = [];
  for (const p of posts) {
    const i = interpretListing(p.text, new Date(p.firstSeenAt));
    if (i.type === "wanted" || i.type === "trade" || i.type === "other") continue;
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
  const groups: Record<GroupId, Row[]> = { soon: [], today: [], later: [], unknown: [], "claim-fixed": [], ended: [] };
  for (const r of rows) {
    if (r.ended) groups.ended.push(r);
    else if (r.type !== "auction") groups["claim-fixed"].push(r);
    else if (r.endsAtMs === null) groups.unknown.push(r);
    else if (r.maybeEnded || r.endsAtMs - t < HOUR) groups.soon.push(r);
    else if (sameOsloDay(new Date(r.endsAtMs), now)) groups.today.push(r);
    else groups.later.push(r);
  }
  const byEnd = (a: Row, b: Row) => (a.endsAtMs ?? Infinity) - (b.endsAtMs ?? Infinity);
  const bySeen = (a: Row, b: Row) => b.firstSeenAt.localeCompare(a.firstSeenAt);
  groups.soon.sort(byEnd);
  groups.today.sort(byEnd);
  groups.later.sort(byEnd);
  groups.unknown.sort(bySeen);
  // Claim sales by end time first, then fixed-price posts (no end) newest first.
  groups["claim-fixed"].sort((a, b) => byEnd(a, b) || bySeen(a, b));
  groups.ended.sort((a, b) => (b.endsAtMs ?? 0) - (a.endsAtMs ?? 0));
  const labels: Record<GroupId, string> = {
    soon: "Within 1 hour",
    today: "Later today",
    later: "Tomorrow and later",
    unknown: "End time unknown",
    "claim-fixed": "Claim and fixed price",
    ended: "Ended",
  };
  return (Object.keys(groups) as GroupId[]).map((id) => ({ id, label: labels[id], rows: groups[id] }));
}

export type Counts = { active: number; withinHour: number; isNew: number };

export function countRows(rows: Row[], now: Date): Counts {
  const t = now.getTime();
  const active = rows.filter((r) => !r.ended && r.type !== "fixed");
  return {
    active: active.length,
    withinHour: active.filter((r) => r.endsAtMs !== null && (r.maybeEnded || r.endsAtMs - t < HOUR)).length,
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
