import { interpretLots, summarizeLots } from "../domain/bids";
import { interpretListing } from "../domain/listing";
import { bidAnswerKey } from "../llm/prompts";
import { MSG_READ_POST, type ReadPostMessage } from "../shared/messages";
import { getAutoScanState, getSettings } from "../shared/settings";
import type { Store } from "../store";

// Post reads without the panel. A click in the overview opens the post in a normal tab and
// reads it silently there (openAndReadVisible). Automatic re-reads of auctions you're bidding
// in (while auto-scan is on; docs/spec.md: every 15 min) go through a queue: a background tab,
// read, closed. Paced like the feed
// scan: one at a time, a pause between reads, never while the automatic feed scan runs (one tab
// talking to Facebook), automatic re-reads skipped while the screen is locked or you're away.

export const READER_ALARM = "fbaw-reader";
/** Pause between reads. chrome.alarms can't go below 30 s. */
const GAP_MS = [30_000, 45_000] as const;
/** A read that hasn't reported back by then has failed; close its tab and move on. */
const READ_TIMEOUT_MS = 3 * 60_000;
const REREAD_AFTER_MS = 15 * 60_000;

export type ReadJob = { postId: string; url: string; reason: "click" | "auto" };
export type ReaderState = {
  /** Posts you opened from the overview, being read silently in their (visible) tab: tabId → postId. */
  visible: Record<string, string>;
  queue: ReadJob[];
  current: (ReadJob & { tabId: number; startedAt: string }) | null;
  lastAt: string | null;
  lastOutcome: string | null;
};
const DEFAULT_STATE: ReaderState = { visible: {}, queue: [], current: null, lastAt: null, lastOutcome: null };

export async function getReaderState(): Promise<ReaderState> {
  const s = await chrome.storage.local.get("readerState");
  return { ...DEFAULT_STATE, ...(s.readerState as Partial<ReaderState> | undefined) };
}
async function setReaderState(change: Partial<ReaderState>): Promise<ReaderState> {
  const next = { ...(await getReaderState()), ...change };
  await chrome.storage.local.set({ readerState: next });
  return next;
}

/** Adds posts to the queue (skipping ones already queued or being read) and starts if idle. */
export async function enqueueReads(jobs: ReadJob[]): Promise<void> {
  const state = await getReaderState();
  const busy = new Set([...state.queue.map((j) => j.postId), state.current?.postId].filter(Boolean));
  const fresh = jobs.filter((j) => !busy.has(j.postId) && /^https:\/\/www\.facebook\.com\//.test(j.url));
  if (fresh.length === 0) return;
  // Your clicks go first; automatic re-reads after.
  const queue = [...state.queue, ...fresh].sort((a, b) => (a.reason === b.reason ? 0 : a.reason === "click" ? -1 : 1));
  await setReaderState({ queue });
  await kickReader();
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
    if (Date.now() - Date.parse(state.current.startedAt) < READ_TIMEOUT_MS) return;
    await finishRead(state.current.tabId, false, "timed out");
    state = await getReaderState();
  }
  if (state.queue.length === 0) return;
  const auto = await getAutoScanState();
  if (auto.running) return later(60_000); // The feed scan has the Facebook tab slot.
  if (state.lastAt && Date.now() - Date.parse(state.lastAt) < GAP_MS[0]) {
    return later(GAP_MS[0] - (Date.now() - Date.parse(state.lastAt)) + 1000);
  }

  const [job, ...rest] = state.queue;
  if (job.reason === "auto") {
    const idle = await chrome.idle.queryState(120);
    if (idle !== "active") {
      // Don't read in the background while you're away; drop the automatic ones.
      await setReaderState({ queue: rest.filter((j) => j.reason === "click") });
      return kickOnce();
    }
  }
  const tab = await chrome.tabs.create({ url: job.url, active: false });
  if (tab.id === undefined) return;
  await setReaderState({ queue: rest, current: { ...job, tabId: tab.id, startedAt: new Date().toISOString() } });
  await later(READ_TIMEOUT_MS + 5000); // Safety net if the read never reports back.

  const loaded = await new Promise<boolean>((resolve) => {
    const timer = setTimeout(() => done(false), 45_000);
    const listener = (id: number, info: { status?: string }) => {
      if (id === tab.id && info.status === "complete") done(true);
    };
    function done(ok: boolean) {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve(ok);
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
  if (!loaded) return finishRead(tab.id, false, "the post didn't load");
  const msg: ReadPostMessage = { type: MSG_READ_POST, waitForPost: true, silent: true };
  await chrome.tabs.sendMessage(tab.id, msg).catch(() => finishRead(tab.id!, false, "no content script in the tab"));
}

/**
 * A post you clicked in the overview: open it in a normal tab for you to look at, and read it
 * silently in that same tab (no panel; the tab stays open). Not queued: you asked for it now.
 */
export async function openAndReadVisible(url: string, postId: string): Promise<void> {
  if (!/^https:\/\/www\.facebook\.com\//.test(url)) return;
  const tab = await chrome.tabs.create({ url, active: true });
  if (tab.id === undefined) return;
  const tabId = tab.id;
  const state = await getReaderState();
  await setReaderState({ visible: { ...state.visible, [tabId]: postId } });
  const loaded = await new Promise<boolean>((resolve) => {
    const timer = setTimeout(() => done(false), 45_000);
    const listener = (id: number, info: { status?: string }) => {
      if (id === tabId && info.status === "complete") done(true);
    };
    function done(ok: boolean) {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve(ok);
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
  if (!loaded) return finishRead(tabId, false, "the post didn't load");
  const msg: ReadPostMessage = { type: MSG_READ_POST, waitForPost: true, silent: true };
  await chrome.tabs.sendMessage(tabId, msg).catch(() => finishRead(tabId, false, "no content script in the tab"));
}

/** A read finished (reported by the content script, or timed out): close its tab, go on. */
export async function finishRead(tabId: number, ok: boolean, outcome: string): Promise<void> {
  const state = await getReaderState();
  if (state.visible[tabId] !== undefined) {
    // Your own tab: leave it open, just record the result.
    const { [tabId]: _done, ...visible } = state.visible;
    await setReaderState({ visible, lastAt: new Date().toISOString(), lastOutcome: `${ok ? "Read" : "Couldn't read"}: ${outcome}` });
    return;
  }
  if (!state.current || state.current.tabId !== tabId) return;
  await chrome.tabs.remove(tabId).catch(() => {});
  const gap = GAP_MS[0] + Math.random() * (GAP_MS[1] - GAP_MS[0]);
  await setReaderState({
    current: null,
    lastAt: new Date().toISOString(),
    lastOutcome: `${ok ? "Read" : "Couldn't read"}: ${outcome}`,
  });
  if ((await getReaderState()).queue.length > 0) await later(gap);
}

/**
 * Auctions you're bidding in that haven't ended and weren't read in the last 15 min: re-read
 * them in the background (only while auto-scan is on).
 */
export async function queueMyAuctionRereads(store: Store): Promise<void> {
  const settings = await getSettings();
  if (!settings.autoScan) return;
  const [posts, captures, answers] = await Promise.all([store.allPosts(), store.allCaptures(), store.allAnswers()]);
  const byId = new Map(posts.map((p) => [p.id, p]));
  const answerMap = new Map(answers.map((a) => [a.key, a.value]));
  const now = Date.now();
  const jobs: ReadJob[] = [];
  for (const { postId, capture } of captures) {
    const post = byId.get(postId);
    if (!post || now - Date.parse(capture.capturedAt) < REREAD_AFTER_MS) continue;
    const listing = interpretListing(post.text, new Date(post.firstSeenAt));
    if (listing.type !== "auction") continue;
    const endsAt = listing.endsAt ? Date.parse(listing.endsAt) : null;
    const softMs = (listing.softCloseMinutes ?? 0) * 60_000;
    if (endsAt !== null && now > endsAt + softMs) continue; // Ended.
    const lots = interpretLots(capture, {
      myName: settings.myName,
      listingIncrement: listing.increment,
      listingMinPrice: listing.minPrice,
      answer: (seller, text) => {
        const key = bidAnswerKey(seller, text);
        return answerMap.has(key) ? (answerMap.get(key) as number | null) : undefined;
      },
    });
    const s = summarizeLots(lots);
    if (s.lead + s.outbid > 0) jobs.push({ postId, url: post.url, reason: "auto" });
  }
  await enqueueReads(jobs);
}
