// User settings and the scheduler's state, in chrome.storage.local (small values, shared by the
// service worker, content scripts and pages, with change events). Bulk data lives in the store.

export type Settings = {
  /** Scan the feed by itself every 10-15 min (docs/spec.md "Slow pacing"). The off switch. */
  autoScan: boolean;
  /** Your Facebook name as it appears on bids, for Leading/Outbid. */
  myName: string;
  /** Send what the rules can't read to Claude Code (claude -p) through the native bridge. */
  useClaude: boolean;
};

export const DEFAULT_SETTINGS: Settings = { autoScan: false, myName: "Erik Johansen", useClaude: true };

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
