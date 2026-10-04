import { MSG_READ_POST, type ReadPostMessage } from "../shared/messages";
import { getSettings } from "../shared/settings";
import type { ReaderState, ReadJob } from "../shared/reader";
import type { Store } from "../store";
import { facebookSlot } from "./slot";
import { endingSoonNote, showNotes } from "./notify";
import { waitForTabLoad } from "./tabs";
import { myAuctions, watchPlan } from "./watch";

// Post reads without the panel, one queue for all of them. Your clicks in the overview open the
// post as a normal tab you see and read it quietly there (the tab stays open); they go first and
// skip the pause. Automatic re-reads of auctions you're in (while auto-scan is on; docs/spec.md:
// every 15 min, plus one just after the close; see watchMyAuctions) use a hidden tab that's closed after. Every read takes
// the one Facebook slot first (slot.ts, review H6), so it never runs alongside the automatic
// scan or a scan/read started from the toolbar menu. Automatic re-reads are paced (30-45 s
// apart) and skipped while the screen is locked or you're away.

export type { ReaderState, ReadJob } from "../shared/reader";

export const READER_ALARM = "fbaw-reader";
/** Alarms for the reader. */
export const isReaderAlarm = (name: string) => name === READER_ALARM || name.startsWith(`${READER_ALARM}-`);
/** Pause between automatic reads. chrome.alarms can't go below 30 s. */
const GAP_MS = [30_000, 45_000] as const;
/** A read that hasn't reported back by then has failed (hidden tab: closed; your tab: left open). */
const READ_TIMEOUT_MS = 3 * 60_000;
const VISIBLE_TIMEOUT_MS = 5 * 60_000;
/** Waiting for the slot: try again this soon. */
const SLOT_RETRY_MS = 30_000;

const DEFAULT_STATE: ReaderState = { queue: [], current: null, lastAt: null, lastOutcome: null };

export async function getReaderState(): Promise<ReaderState> {
  const s = await chrome.storage.local.get("readerState");
  const { queue, current, lastAt, lastOutcome } = { ...DEFAULT_STATE, ...(s.readerState as Partial<ReaderState> | undefined) };
  // (Older versions also kept a separate "visible" map; it's dropped here.)
  return { queue: queue ?? [], current: current ?? null, lastAt: lastAt ?? null, lastOutcome: lastOutcome ?? null };
}
/**
 * Every change to the reader state goes through this one queue (review M1): a read-modify-write
 * that interleaves with another at an `await` would otherwise lose `current`, open a second tab,
 * and leave the first one open. `change` may be a function of the state at that moment.
 */
let stateQueue: Promise<unknown> = Promise.resolve();
function setReaderState(change: Partial<ReaderState> | ((s: ReaderState) => Partial<ReaderState>)): Promise<ReaderState> {
  const run = async () => {
    const current = await getReaderState();
    const next = { ...current, ...(typeof change === "function" ? change(current) : change) };
    await chrome.storage.local.set({ readerState: next });
    return next;
  };
  const result = stateQueue.then(run, run);
  stateQueue = result.catch(() => {});
  return result;
}

/**
 * Adds posts to the queue and starts if idle. A post already queued or being read isn't added
 * again, except that your click upgrades a queued background re-read to a visible read.
 */
export async function enqueueReads(jobs: ReadJob[]): Promise<void> {
  let added = 0;
  await setReaderState((state) => {
    let queue = [...state.queue];
    for (const job of jobs) {
      if (!/^https:\/\/www\.facebook\.com\//.test(job.url) || state.current?.postId === job.postId) continue;
      const i = queue.findIndex((q) => q.postId === job.postId);
      if (i >= 0 && !(job.reason === "click" && queue[i].reason === "auto")) continue;
      if (i >= 0) queue.splice(i, 1);
      queue.push(job);
      added++;
    }
    // Your clicks go first; automatic re-reads after.
    queue = queue.sort((a, b) => (a.reason === b.reason ? 0 : a.reason === "click" ? -1 : 1));
    return { queue };
  });
  if (added > 0) await kickReader();
}

/**
 * A post you asked to read (the overview's Read button): ahead of the automatic re-reads and
 * without their pause. In a background tab that closes itself, or `visible` in a tab you see.
 */
export function readNow(url: string, postId: string, visible = false): Promise<void> {
  return enqueueReads([{ postId, url, reason: "click", visible }]);
}

async function later(ms: number) {
  await chrome.alarms.create(READER_ALARM, { when: Date.now() + ms });
}

let kicking: Promise<void> | null = null;

/** Starts the next read if nothing is in flight; reschedules itself when it has to wait. */
export function kickReader(): Promise<void> {
  // Serialize: two triggers at once must not open two tabs.
  kicking = (kicking ?? Promise.resolve()).then(kickOnce, kickOnce);
  return kicking;
}

async function kickOnce(): Promise<void> {
  let state = await getReaderState();
  if (state.current) {
    const limit = state.current.visible ? VISIBLE_TIMEOUT_MS : READ_TIMEOUT_MS;
    if (Date.now() - Date.parse(state.current.startedAt) < limit) return;
    await finishRead(state.current.tabId, false, "it took too long");
    state = await getReaderState();
  }
  if (state.queue.length === 0) return;

  const [job, ...rest] = state.queue;
  if (job.reason === "auto") {
    // Pace the automatic ones; your clicks don't wait for this.
    if (state.lastAt && Date.now() - Date.parse(state.lastAt) < GAP_MS[0]) {
      return later(GAP_MS[0] - (Date.now() - Date.parse(state.lastAt)) + 1000);
    }
    const idle = await chrome.idle.queryState(120);
    if (idle !== "active") {
      // Don't read in the background while you're away; drop the automatic ones.
      await setReaderState({ queue: rest.filter((j) => j.reason === "click") });
      return kickOnce();
    }
  }

  // One tab talking to Facebook at a time: wait for the slot (review H6). It's taken before the
  // tab exists (tab ID -1), then moved to the tab.
  if (!(await facebookSlot.acquire("reader", -1))) return later(SLOT_RETRY_MS);
  const tab = await chrome.tabs.create({ url: job.url, active: !!job.visible }).catch(() => null);
  if (tab?.id === undefined) {
    await facebookSlot.release("reader", -1);
    await setReaderState({ queue: rest, lastAt: new Date().toISOString(), lastOutcome: "Couldn't read: no tab" });
    return;
  }
  const tabId = tab.id;
  await facebookSlot.moveTo("reader", -1, tabId);
  await setReaderState({ queue: rest, current: { ...job, tabId, startedAt: new Date().toISOString() } });
  if (job.visible) await setTabBadge(tabId, "…", "#2457D6");
  await later((job.visible ? VISIBLE_TIMEOUT_MS : READ_TIMEOUT_MS) + 5000); // Safety net if the read never reports back.

  if (!(await waitForTabLoad(tabId))) return finishRead(tabId, false, "the post didn't load");
  const msg: ReadPostMessage = { type: MSG_READ_POST, waitForPost: true, silent: true };
  await chrome.tabs.sendMessage(tabId, msg).catch(() => finishRead(tabId, false, "no content script in the tab"));
}

async function setTabBadge(tabId: number, text: string, color: string) {
  await chrome.action.setBadgeBackgroundColor({ tabId, color }).catch(() => {});
  await chrome.action.setBadgeText({ tabId, text }).catch(() => {});
}

/** A read finished (reported by the content script, timed out, or its tab closed): free the slot, go on. */
export async function finishRead(tabId: number, ok: boolean, outcome: string): Promise<void> {
  const state = await getReaderState();
  if (!state.current || state.current.tabId !== tabId) return;
  const job = state.current;
  // Done first, then close the tab: closing it fires tabs.onRemoved, which would otherwise find
  // this read still current and record it as "its tab was closed".
  const next = await setReaderState({
    current: null,
    lastAt: new Date().toISOString(),
    lastOutcome: `${ok ? "Read" : "Couldn't read"}: ${outcome}`,
  });
  if (job.visible) {
    // Your tab: leave it open; a ✓ (or !) on the toolbar icon there for a little while.
    await setTabBadge(tabId, ok ? "✓" : "!", ok ? "#1a7f37" : "#B42318");
    setTimeout(() => void setTabBadge(tabId, "", "#2457D6"), 20_000);
  } else {
    await chrome.tabs.remove(tabId).catch(() => {});
  }
  await facebookSlot.release("reader", tabId);
  if (next.queue.length === 0) return;
  // Your next click starts right away; automatic re-reads after the pause.
  if (next.queue[0].reason === "click") void kickReader();
  else await later(GAP_MS[0] + Math.random() * (GAP_MS[1] - GAP_MS[0]));
}

export const WATCH_ALARM = "fbaw-watch";

/**
 * Your auctions (watch.ts): re-read the due ones in the background (only while auto-scan is on:
 * running ones every 15 min, and one final read just after each closes, so Leading becomes Won
 * or Lost), notify the ones ending within 10 min, and set an alarm for the next of those moments.
 */
export async function watchMyAuctions(store: Store): Promise<void> {
  const settings = await getSettings();
  const [posts, captures, answers] = await Promise.all([store.allPosts(), store.allCaptures(), store.allAnswers()]);
  const auctions = myAuctions(posts, captures, new Map(answers.map((a) => [a.key, a.value])), settings.myName);
  const now = Date.now();
  const plan = watchPlan(auctions, now);
  if (settings.autoScan) await enqueueReads(plan.due.map((a) => ({ postId: a.postId, url: a.url, reason: "auto" as const })));
  await showNotes(plan.endingSoon.map((a) => endingSoonNote(a, now)));
  const next = [settings.autoScan ? plan.nextFinalAt : null, settings.notify ? plan.nextEndingSoonAt : null].filter((t): t is number => t !== null);
  if (next.length) await chrome.alarms.create(WATCH_ALARM, { when: Math.max(Math.min(...next), now + 30_000) });
  else await chrome.alarms.clear(WATCH_ALARM);
}
