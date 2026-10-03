// One "Facebook slot" (review H6): docs/spec.md and CLAUDE.md require that never more than one
// tab talks to Facebook at a time. Every activity that does (the automatic scan, post reads,
// scans and reads started from the toolbar menu) takes this slot first and frees it when done.
// Kept in chrome.storage.session, so it survives the service worker being stopped (MV3) but not
// a browser restart. A holder that never frees it (a crashed page, a closed tab the worker didn't
// see) expires after its time limit.

export type SlotHolder = "auto-scan" | "reader" | "menu-scan" | "menu-read";
export type Slot = { holder: SlotHolder; tabId: number; since: string };

/** How long a holder may keep the slot without freeing it. A scan from the menu can run long. */
const LIMIT_MS: Record<SlotHolder, number> = {
  "auto-scan": 10 * 60_000,
  reader: 6 * 60_000,
  "menu-read": 10 * 60_000,
  "menu-scan": 25 * 60_000,
};

const KEY = "facebookSlot";

/** Storage in one place, so tests can swap it. */
export type SlotStorage = { get(): Promise<Slot | null>; set(slot: Slot | null): Promise<void> };
const sessionStorage: SlotStorage = {
  async get() {
    return ((await chrome.storage.session.get(KEY))[KEY] as Slot | undefined) ?? null;
  },
  async set(slot) {
    await chrome.storage.session.set({ [KEY]: slot });
  },
};

const expired = (slot: Slot, now: number) => now - Date.parse(slot.since) > LIMIT_MS[slot.holder];

// Every slot change goes through one queue: two activities asking at the same moment must not
// both get it (a read-modify-write race at an `await`).
let queue: Promise<unknown> = Promise.resolve();
function serial<T>(fn: () => Promise<T>): Promise<T> {
  const result = queue.then(fn, fn);
  queue = result.catch(() => {});
  return result;
}

export function makeSlot(storage: SlotStorage = sessionStorage, now: () => number = Date.now) {
  return {
    /** Takes the slot if it's free (or its holder expired). True if you got it. */
    acquire(holder: SlotHolder, tabId: number): Promise<boolean> {
      return serial(async () => {
        const current = await storage.get();
        if (current && !expired(current, now())) return false;
        await storage.set({ holder, tabId, since: new Date(now()).toISOString() });
        return true;
      });
    },
    /** Frees the slot, if this holder (in this tab) still has it. */
    release(holder: SlotHolder, tabId: number): Promise<void> {
      return serial(async () => {
        const current = await storage.get();
        if (current && current.holder === holder && current.tabId === tabId) await storage.set(null);
      });
    },
    /** The holder took the slot before its tab existed: record the tab it's in now. */
    moveTo(holder: SlotHolder, fromTabId: number, toTabId: number): Promise<void> {
      return serial(async () => {
        const current = await storage.get();
        if (current && current.holder === holder && current.tabId === fromTabId) await storage.set({ ...current, tabId: toTabId });
      });
    },
    /** Frees the slot if its tab was closed. */
    releaseTab(tabId: number): Promise<void> {
      return serial(async () => {
        const current = await storage.get();
        if (current?.tabId === tabId) await storage.set(null);
      });
    },
    /** Who has it right now (null when free or expired). */
    async holder(): Promise<Slot | null> {
      const current = await storage.get();
      return current && !expired(current, now()) ? current : null;
    },
    /** Waits (polling) until the slot is free and takes it; false if that took longer than `maxWaitMs`. */
    async acquireWhenFree(holder: SlotHolder, tabId: number, maxWaitMs = 5 * 60_000): Promise<boolean> {
      const until = now() + maxWaitMs;
      while (now() < until) {
        if (await this.acquire(holder, tabId)) return true;
        await new Promise((r) => setTimeout(r, 2000));
      }
      return false;
    },
  };
}

export const facebookSlot = makeSlot();
