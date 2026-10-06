// End times as sellers write them in the group template, interpreted in Europe/Oslo.
// Seen so far (docs/spec.md): "Sluttid: 04.10.26 kl 21:00", "Sluttid: 2026-10-02 22.00",
// "Sluttid (Lørdag 3. oktober 23.59):", "Slutt: 05.10.26 kl 21:00", "Sluttid: Ikveld 3/10,
// kl 22.00", "Sluttid: Søndag kl. 22:00 (04.10.2026)", "Sluttid: 3.10 kl 23", and an empty
// "Sluttid:" with the time on the next line. Anything not understood is left unsure, never guessed.

export type EndTime = {
  /** ISO timestamp (UTC), or null when no date and time could be read. */
  endsAt: string | null;
  /** The seller's original line, always shown next to the interpreted value. */
  endsAtText: string | null;
  /** False when something was missing or contradictory (e.g. weekday doesn't match the date). */
  sure: boolean;
};

const TZ = "Europe/Oslo";

const MONTHS: Record<string, number> = {
  januar: 1, jan: 1, februar: 2, feb: 2, mars: 3, mar: 3, april: 4, apr: 4, mai: 5,
  juni: 6, jun: 6, juli: 7, jul: 7, august: 8, aug: 8, september: 9, sept: 9, sep: 9,
  oktober: 10, okt: 10, november: 11, nov: 11, desember: 12, des: 12,
};
const WEEKDAYS: Record<string, number> = {
  søndag: 0, mandag: 1, tirsdag: 2, onsdag: 3, torsdag: 4, fredag: 5, lørdag: 6,
};

const LABEL = /^slutt?\s*(?:tid)?\b/i; // "Sluttid", "Slutt", "Slutt tid"

/** The end-time line from a post's text ("Sluttid: …" / "Slutt: …"), joined with the next line when empty. */
export function findEndLine(text: string): string | null {
  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
  for (let i = 0; i < lines.length; i++) {
    if (!LABEL.test(lines[i])) continue;
    const rest = lines[i].replace(LABEL, "");
    const hasData = /\d/.test(rest) || /(i\s?kveld|i\s?dag|søndag|mandag|tirsdag|onsdag|torsdag|fredag|lørdag)/i.test(rest);
    return hasData || i + 1 >= lines.length ? lines[i] : `${lines[i]} ${lines[i + 1]}`;
  }
  return null;
}

// "Startid", "Starttid", "Start tid", "Start:"; not "Startbud" (a start bid).
const START_LABEL = /^start(?:\s*-?\s*t?id\b|\s*:)/i;

/** The start-time line ("Startid: 20:00 søndag 4. oktober"), joined with the next line when empty. Claim sales use it. */
export function findStartLine(text: string): string | null {
  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
  for (let i = 0; i < lines.length; i++) {
    if (!START_LABEL.test(lines[i])) continue;
    // Only an empty "Startid:" takes the next line: anything else written there is the start.
    const empty = /^[\s:.\-–]*$/.test(lines[i].replace(START_LABEL, ""));
    return empty && i + 1 < lines.length && !/^slutt/i.test(lines[i + 1]) ? `${lines[i]} ${lines[i + 1]}` : lines[i];
  }
  return null;
}

/** When the sale starts (lots are usually posted then), read like an end time. */
export type StartTime = { startsAt: string | null; startsAtText: string | null };

export function parseStartTime(line: string | null, ref: Date): StartTime {
  if (!line) return { startsAt: null, startsAtText: null };
  const { endsAt } = parseEndTime(line.replace(START_LABEL, " "), ref);
  return { startsAt: endsAt, startsAtText: line };
}

/** Offset of Oslo local time from UTC at `utcMs`, in ms. */
function osloOffsetMs(utcMs: number): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: TZ, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).formatToParts(new Date(utcMs));
  const get = (t: string) => Number(parts.find((p) => p.type === t)!.value);
  return Date.UTC(get("year"), get("month") - 1, get("day"), get("hour"), get("minute"), get("second")) - utcMs;
}

/** An Oslo wall-clock time as a UTC Date (DST-aware). */
export function osloToUtc(year: number, month: number, day: number, hour: number, minute: number): Date {
  const guess = Date.UTC(year, month - 1, day, hour, minute);
  let t = guess - osloOffsetMs(guess);
  t = guess - osloOffsetMs(t); // Second pass settles the DST switch hours.
  return new Date(t);
}

/** "YYYY-MM-DD HH:mm" (Oslo) as an ISO timestamp, or null. */
function osloStamp(value: unknown): string | null {
  const m = typeof value === "string" ? value.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/) : null;
  return m ? osloToUtc(+m[1], +m[2], +m[3], +m[4], +m[5]).toISOString() : null;
}

/**
 * Claude's answer about a post's times: `{ endsAt, startsAt }`, each "YYYY-MM-DD HH:mm" (Oslo) or
 * null. Answers stored before start times were asked for are the end time alone, as a string.
 */
export function claudeEndsAt(value: unknown): string | null {
  return osloStamp(value && typeof value === "object" ? (value as { endsAt?: unknown }).endsAt : value);
}

export function claudeStartsAt(value: unknown): string | null {
  return value && typeof value === "object" ? osloStamp((value as { startsAt?: unknown }).startsAt) : null;
}

/** The Oslo calendar date of a moment. */
export function osloDate(at: Date): { year: number; month: number; day: number } {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(at);
  const get = (t: string) => Number(parts.find((p) => p.type === t)!.value);
  return { year: get("year"), month: get("month"), day: get("day") };
}

const weekdayOf = (y: number, m: number, d: number) => new Date(Date.UTC(y, m - 1, d)).getUTCDay();
const validDate = (m: number, d: number) => m >= 1 && m <= 12 && d >= 1 && d <= 31;

type DateParts = { year: number | null; month: number; day: number; match: string };

function findDate(s: string): DateParts | null {
  const iso = s.match(/(\d{4})-(\d{1,2})-(\d{1,2})/);
  if (iso && validDate(+iso[2], +iso[3])) return { year: +iso[1], month: +iso[2], day: +iso[3], match: iso[0] };

  const named = s.match(new RegExp(String.raw`(\d{1,2})\.?\s*(${Object.keys(MONTHS).join("|")})\b`));
  if (named) return { year: null, month: MONTHS[named[2]], day: +named[1], match: named[0] };

  // "04.10.26", "4/10", "04/10-26", "05.10.2026", but not a time after "kl" ("kl 21.10").
  for (const m of s.matchAll(/(?<!\d)(\d{1,2})\s*[./]\s*(\d{1,2})(?:\s*[./-]\s*(\d{4}|\d{2}))?(?!\d)/g)) {
    const before = s.slice(0, m.index);
    if (/kl\.?:?\s*$/.test(before)) continue;
    if (!validDate(+m[2], +m[1])) continue;
    const y = m[3] ? (m[3].length === 2 ? 2000 + +m[3] : +m[3]) : null;
    return { year: y, month: +m[2], day: +m[1], match: m[0] };
  }
  return null;
}

function findTime(s: string): { hour: number; minute: number } | null {
  const kl = s.match(/kl\.?:?\s*(\d{1,2})(?:\s*[:.]\s*(\d{2}))?(?!\d)/);
  const plain = s.match(/(?<![\d.:/])(\d{1,2})[:.](\d{2})(?![\d.:/])/);
  const t = kl ?? plain;
  if (!t) return null;
  const hour = +t[1];
  const minute = t[2] ? +t[2] : 0;
  if (hour > 24 || minute > 59) return null;
  return { hour, minute };
}

/**
 * Interprets an end-time line. `ref` is when the post was captured: it fills in a missing
 * year and resolves weekday-only or "ikveld" ("tonight") end times.
 */
export function parseEndTime(line: string | null, ref: Date): EndTime {
  if (!line) return { endsAt: null, endsAtText: null, sure: false };
  const s = line.toLowerCase().replace(LABEL, " ");
  const today = osloDate(ref);
  let sure = true;

  const date = findDate(s);
  const time = findTime(date ? s.replace(date.match, " ") : s);
  const weekdayName = Object.keys(WEEKDAYS).find((w) => s.includes(w));
  const tonight = /\bi\s?kveld\b|\bi\s?dag\b/.test(s);

  let year: number;
  let month: number;
  let day: number;
  if (date) {
    month = date.month;
    day = date.day;
    year = date.year ?? today.year;
    if (date.year === null) {
      // No year written: the nearest such date to the capture day, allowing a little past.
      const asIs = Date.UTC(year, month - 1, day);
      if (asIs < Date.UTC(today.year, today.month - 1, today.day) - 180 * 864e5) year++;
    }
    if (weekdayName && weekdayOf(year, month, day) !== WEEKDAYS[weekdayName]) sure = false;
  } else if (tonight) {
    // "Tonight" means the day it was posted, but we only know when it was first seen, which can
    // be later: plausible, not sure (review L1).
    ({ year, month, day } = today);
    sure = false;
  } else if (weekdayName) {
    // Weekday only: the next such day, counting today.
    const base = new Date(Date.UTC(today.year, today.month - 1, today.day));
    const ahead = (WEEKDAYS[weekdayName] - base.getUTCDay() + 7) % 7;
    const d = new Date(base.getTime() + ahead * 864e5);
    ({ year, month, day } = { year: d.getUTCFullYear(), month: d.getUTCMonth() + 1, day: d.getUTCDate() });
    sure = false; // Plausible, but "søndag" could mean next week.
  } else {
    return { endsAt: null, endsAtText: line, sure: false };
  }
  if (!time) return { endsAt: null, endsAtText: line, sure: false };
  const at = time.hour === 24 ? osloToUtc(year, month, day, 23, 59) : osloToUtc(year, month, day, time.hour, time.minute);
  return { endsAt: at.toISOString(), endsAtText: line, sure };
}
