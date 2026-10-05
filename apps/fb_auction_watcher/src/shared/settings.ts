// User settings and the scheduler's state, in chrome.storage.local (small values, shared by the
// service worker, content scripts and pages, with change events). Bulk data lives in the store.

export type Settings = {
  /** Scan the feed by itself every 10-15 min (docs/spec.md "Slow pacing"). The off switch. */
  autoScan: boolean;
  /** Your Facebook name as it appears on bids, for Leading/Outbid. */
  myName: string;
  /** Send what the rules can't read to Claude Code (claude -p) through the native bridge. */
  useClaude: boolean;
  /** Desktop notifications: outbid, ending in 10 min, won/lost (src/background/notify.ts). */
  notify: boolean;
  /** Your tcg_inventory's address (https://…, or http://localhost for local dev), for "Send wins to inventory" (#309). Empty = not set up. */
  inboxUrl: string;
  /** The INBOX_TOKEN set on that tcg_inventory, sent as a Bearer token. */
  inboxToken: string;
};

export const DEFAULT_SETTINGS: Settings = { autoScan: false, myName: "Erik Johansen", useClaude: true, notify: true, inboxUrl: "", inboxToken: "" };

export type AutoScanState = {
  /** When the next automatic scan is due (ISO), or null when auto-scan is off. */
  nextAt: string | null;
  /** The last attempt: when, and what happened. */
  lastAt: string | null;
  lastOutcome: string | null;
  running: boolean;
};

export const DEFAULT_AUTO_SCAN_STATE: AutoScanState = { nextAt: null, lastAt: null, lastOutcome: null, running: false };

export type ClaudeState = {
  lastAt: string | null;
  lastOutcome: string | null;
  /** A failure that pauses all of Claude for a while (bridge missing, not logged in); per-item failures don't set it. */
  error: string | null;
  /** The hourly cap on photo calls is reached until then (ISO), or null. */
  photoLimitUntil: string | null;
  /** When Sonnet photo calls were made in the last hour (ISO), for the hourly cap; survives worker restarts. */
  photoCalls: string[];
};
export const DEFAULT_CLAUDE_STATE: ClaudeState = { lastAt: null, lastOutcome: null, error: null, photoLimitUntil: null, photoCalls: [] };

/** The daily cleanup of old stored data (src/store/retention.ts): when it last ran, and what it removed. */
export type CleanupState = { lastAt: string | null; lastOutcome: string | null };
export const DEFAULT_CLEANUP_STATE: CleanupState = { lastAt: null, lastOutcome: null };

/** The last "Send wins to inventory" (src/background/inbox.ts): when, whether it worked, how many wins, and what came back. */
export type InboxState = { lastAt: string | null; ok: boolean | null; count: number | null; outcome: string | null };
export const DEFAULT_INBOX_STATE: InboxState = { lastAt: null, ok: null, count: null, outcome: null };

async function read<T extends object>(key: string, defaults: T): Promise<T> {
  const stored = await chrome.storage.local.get(key);
  return { ...defaults, ...(stored[key] as Partial<T> | undefined) };
}
async function patch<T extends object>(key: string, defaults: T, change: Partial<T>): Promise<T> {
  const next = { ...(await read(key, defaults)), ...change };
  await chrome.storage.local.set({ [key]: next });
  return next;
}

export const getSettings = () => read("settings", DEFAULT_SETTINGS);
export const updateSettings = (change: Partial<Settings>) => patch("settings", DEFAULT_SETTINGS, change);
export const getAutoScanState = () => read("autoScanState", DEFAULT_AUTO_SCAN_STATE);
export const updateAutoScanState = (change: Partial<AutoScanState>) => patch("autoScanState", DEFAULT_AUTO_SCAN_STATE, change);
export const getClaudeState = () => read("claudeState", DEFAULT_CLAUDE_STATE);
export const updateClaudeState = (change: Partial<ClaudeState>) => patch("claudeState", DEFAULT_CLAUDE_STATE, change);
export const getCleanupState = () => read("cleanupState", DEFAULT_CLEANUP_STATE);
export const updateCleanupState = (change: Partial<CleanupState>) => patch("cleanupState", DEFAULT_CLEANUP_STATE, change);
export const getInboxState = () => read("inboxState", DEFAULT_INBOX_STATE);

/** Won auctions (post ID → when) sent to tcg_inventory, so To pay can say "sent" and you can pick what's left. */
export type InboxSent = Record<string, string>;
export async function getInboxSent(): Promise<InboxSent> {
  return ((await chrome.storage.local.get("inboxSent")).inboxSent as InboxSent | undefined) ?? {};
}
export async function markInboxSent(postIds: string[], at: Date): Promise<void> {
  const sent = await getInboxSent();
  for (const id of postIds) sent[id] = at.toISOString();
  await chrome.storage.local.set({ inboxSent: sent });
}
export const setInboxState = (state: InboxState) => chrome.storage.local.set({ inboxState: state });

/**
 * Checks a tcg_inventory address and returns its origin ("https://host[:port]"), or null.
 * https only, except http on localhost / 127.0.0.1 for a local `python app.py`.
 */
export function inboxOrigin(raw: string): string | null {
  let url: URL;
  try {
    url = new URL(raw.trim());
  } catch {
    return null;
  }
  const local = url.hostname === "localhost" || url.hostname === "127.0.0.1";
  if (url.protocol !== "https:" && !(url.protocol === "http:" && local)) return null;
  if (url.username || url.password) return null;
  return url.origin;
}

/** Your own marks on what you won, per sale (post ID): when you paid, and when it arrived. */
export type WonMark = { paidAt: string | null; receivedAt: string | null };
export type WonState = Record<string, WonMark>;

export async function getWonState(): Promise<WonState> {
  return ((await chrome.storage.local.get("wonState")).wonState as WonState | undefined) ?? {};
}

/** Marks (or unmarks) these sales as paid / received. */
export async function markWon(postIds: string[], change: Partial<WonMark>): Promise<void> {
  const state = await getWonState();
  const none: WonMark = { paidAt: null, receivedAt: null };
  for (const id of postIds) state[id] = { ...none, ...state[id], ...change };
  await chrome.storage.local.set({ wonState: state });
}

/** Sales you've marked as ended yourself (post ID → when), e.g. an end time nobody could read or a seller who closed early. */
export type EndedMarks = Record<string, string>;

export async function getEndedMarks(): Promise<EndedMarks> {
  return ((await chrome.storage.local.get("endedMarks")).endedMarks as EndedMarks | undefined) ?? {};
}

/** Marks a sale as ended now, or (ended = false) takes the mark back. */
export async function markEnded(postId: string, ended: boolean): Promise<void> {
  const marks = await getEndedMarks();
  if (ended) marks[postId] = new Date().toISOString();
  else delete marks[postId];
  await chrome.storage.local.set({ endedMarks: marks });
}

/**
 * Lots you've marked "Not won" yourself (#329), by `notWonKey` (`<post ID>:<lot ref>`, in the
 * dashboard model) → when. For a win the rules got wrong (e.g. your bid came after the end and the
 * seller said so): the lot leaves To pay and isn't sent to tcg_inventory. Undoable.
 */
export type NotWonMarks = Record<string, string>;

export async function getNotWonMarks(): Promise<NotWonMarks> {
  return ((await chrome.storage.local.get("notWonMarks")).notWonMarks as NotWonMarks | undefined) ?? {};
}

/** Marks a lot as not won now, or (notWon = false) takes the mark back. */
export async function markNotWon(key: string, notWon: boolean): Promise<void> {
  const marks = await getNotWonMarks();
  if (notWon) marks[key] = new Date().toISOString();
  else delete marks[key];
  await chrome.storage.local.set({ notWonMarks: marks });
}
