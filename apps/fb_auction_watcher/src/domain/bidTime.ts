// When bids were placed, and when a lot really ends (#329). Pure.
//
// The end is strict, at minute resolution (notes/fb_auction_watcher/group-domain.md §4.1): end
// 20:00 → a bid at 19:59 counts, one at 20:00 doesn't. With antisnipe ("Antisnipe 5 min: Ja",
// §4.2), each bid placed in the last 5 minutes before the *current* end moves the end to that bid's
// time + 5 min, again and again, per lot. So a lot's real end can only be worked out from its bids'
// times. Facebook only shows a reply's age ("5 min", "2 t"), relative to when it was read, so each
// bid's time is a window, and the end is a window too: a bid is late only when it is late for
// every time in its window, on time only when it is on time for every one, and otherwise unsure.

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** A read's clock and Facebook's rounding aren't exact: windows are widened by this on each side. */
export const TIME_SLACK_MS = MINUTE;

const UNITS: [RegExp, number][] = [
  [/^(?:s|sek|sekund(?:er)?|secs?|seconds?)$/, 1_000],
  [/^(?:m|min|mins|minutt(?:er)?|minutes?)$/, MINUTE],
  [/^(?:t|time|timer|h|hr|hrs|hours?)$/, HOUR],
  [/^(?:d|dag|dager|dg|days?)$/, DAY],
  [/^(?:u|uke|uker|w|wk|wks|weeks?)$/, 7 * DAY],
  [/^(?:år|y|yr|yrs|years?)$/, 365 * DAY],
];
const WORD_NUMBERS: Record<string, number> = { en: 1, ett: 1, et: 1, ei: 1, a: 1, an: 1, one: 1, to: 2, two: 2, tre: 3, three: 3 };

/**
 * How old a reply is, from Facebook's relative time: "5 min", "2 t", "1 d", "3 u", "5m", "2h",
 * "Akkurat nå" / "Just now", "for 5 minutter siden", "for omtrent en time siden". Facebook shows
 * whole units, rounded down ("59 min", then "1 t"), so "2 t" is 2 h up to 3 h. "omtrent"/"about"
 * widens it by a unit each way. Null when not understood (a date like "3. oktober" is too old to matter).
 */
export function relativeAge(text: string | null | undefined): { min: number; max: number } | null {
  if (!text) return null;
  let t = text.normalize("NFC").toLowerCase().replace(/\s+/g, " ").trim();
  if (/^(?:akkurat nå|nå|just now|now)$/.test(t)) return { min: 0, max: MINUTE };
  t = t.replace(/^for /, "").replace(/ (?:siden|ago)$/, "");
  const about = /^(?:omtrent|ca\.?|cirka|about) /.test(t);
  t = t.replace(/^(?:omtrent|ca\.?|cirka|about) /, "");
  const m = t.match(/^(\d+|[a-zå]+)\s*([a-zå]+)$/);
  if (!m) return null;
  const n = /^\d+$/.test(m[1]) ? Number(m[1]) : WORD_NUMBERS[m[1]];
  const unit = UNITS.find(([re]) => re.test(m[2]))?.[1];
  if (n === undefined || unit === undefined) return null;
  if (unit < MINUTE) return { min: 0, max: MINUTE };
  return about ? { min: Math.max(0, n - 1) * unit, max: (n + 1) * unit } : { min: n * unit, max: (n + 1) * unit };
}

/** When a bid was placed, as a window [lo, hi] in ms; lo is -Infinity when nothing says how early. */
export type TimeWindow = { lo: number; hi: number };

/**
 * A reply's time window. `seenAt` is when the read that saw its `timeText` ran (null when unknown,
 * e.g. an old merged read); `existedBy` is a time it's known to have existed by (the latest read
 * holding it). Without a readable age, all we know is that it was there by `existedBy`.
 */
export function replyWindow(timeText: string | null, seenAt: number | null, existedBy: number): TimeWindow {
  const age = seenAt === null ? null : relativeAge(timeText);
  if (!age || seenAt === null) return { lo: -Infinity, hi: existedBy };
  return { lo: seenAt - age.max - TIME_SLACK_MS, hi: Math.min(existedBy, seenAt - age.min + TIME_SLACK_MS) };
}

/**
 * A lot's end with chained antisnipe (§4.2), for bids at known times (ms, any order): each bid
 * before the current end and within its last `softCloseMinutes` moves the end to bid + that many
 * minutes. Bids at or after the current end are late and move nothing. No antisnipe (null or 0):
 * the end as written. This is the rule; `judgeBidTimes` applies it to time windows.
 */
export function effectiveEnd(endsAt: number, softCloseMinutes: number | null, bidTimes: number[]): number {
  const soft = (softCloseMinutes ?? 0) * MINUTE;
  let end = endsAt;
  if (!soft) return end;
  for (const t of [...bidTimes].sort((a, b) => a - b)) {
    if (t < end) end = Math.max(end, t + soft);
  }
  return end;
}

/**
 * "unsure": its time window straddles the end. "unknown": nothing says how early it was (no
 * readable age, or an older merged read without `seenAt`), and it can't be proven on time either:
 * treated as before #329 (counts, no flag, doesn't move the end).
 */
export type TimeVerdict = "on-time" | "late" | "unsure" | "unknown";

/**
 * Runs the chained rule over bids in the order they were placed (by reply ID), each with a time
 * window, keeping the end as a window [endLo, endHi]: endLo counts only bids proven on time at
 * their earliest; endHi counts every bid that may be on time, at its latest possible time before
 * the end (a valid bid is always before the end). Each bid is judged against the end as it stood
 * when it came: late when even its earliest time is at or past the latest possible end, on time
 * when even its latest time is before the earliest possible end, otherwise unsure (or unknown,
 * when nothing bounds how early it was).
 * `counts(i, verdict)` is called for every bid, in order, right after its verdict: it says whether
 * bid i counts (an invalid bid can't move the end; a late one never does). Returns the verdicts and
 * the lot's end window.
 */
export function judgeBidTimes(
  endsAt: number,
  softCloseMinutes: number | null,
  windows: TimeWindow[],
  counts: (i: number, verdict: TimeVerdict) => boolean = () => true,
): { verdicts: TimeVerdict[]; endLo: number; endHi: number } {
  const soft = (softCloseMinutes ?? 0) * MINUTE;
  let endLo = endsAt;
  let endHi = endsAt;
  const verdicts: TimeVerdict[] = [];
  windows.forEach((w, i) => {
    const verdict: TimeVerdict = w.lo >= endHi ? "late" : w.hi < endLo ? "on-time" : w.lo === -Infinity ? "unknown" : "unsure";
    verdicts.push(verdict);
    const valid = counts(i, verdict); // Always asked: the caller judges every bid here, in order.
    // No evidence of when it came: it moves nothing (saleClosesAt's floor covers a last-minute bid).
    if (verdict === "late" || verdict === "unknown" || !soft || !valid) return;
    if (verdict === "on-time") endLo = Math.max(endLo, w.lo + soft);
    endHi = Math.max(endHi, Math.min(w.hi, endHi - 1) + soft);
  });
  return { verdicts, endLo, endHi };
}

/**
 * When a sale is over, so no bid counts any more: the latest of its lots' ends (each from
 * `judgeBidTimes`), and never before end + the antisnipe window, since a bid in the last minutes
 * may not have been read yet. Null without an end time. Used for "Ended?", the final read and
 * notifications, in place of the fixed end + antisnipe they used before (#329).
 */
export function saleClosesAt(endsAt: number | null, softCloseMinutes: number | null, lotEnds: (number | null | undefined)[] = []): number | null {
  if (endsAt === null) return null;
  const floor = endsAt + (softCloseMinutes ?? 0) * MINUTE;
  return Math.max(floor, ...lotEnds.filter((x): x is number => typeof x === "number"));
}
