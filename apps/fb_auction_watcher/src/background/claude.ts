import { bidRequest, claimLotRequest, endTimeRequest, type ClaudeRequest } from "../llm/prompts";
import { getClaudeState, getSettings, updateClaudeState } from "../shared/settings";
import type { Store, StoredAnswer } from "../store";
import {
  describeRun,
  failureStatus,
  isGlobalError,
  loadFailures,
  pendingItems,
  photoCallsLeft,
  photoLimitFreesAt,
  pruneFailures,
  recentPhotoCalls,
  recordFailure,
  saveFailures,
  type ClaudeTask,
  type Failures,
  type ItemFailure,
} from "./claudeQueue";

// Sends what the rules couldn't read to Claude Code through the native bridge
// (native/fbaw_claude_host.py → `claude -p`, on the user's own Claude login; no API key).
// Only unread items of live sales, batched (one call per kind), each answer cached so it's never
// asked again. What to ask (and what to skip) is decided in claudeQueue.ts; this file runs it.
//
// Failures (review M2): one that hits everything (bridge not installed, `claude` missing or not
// logged in) stops the run and pauses Claude for ERROR_COOLDOWN_MS. One that hits a single item
// (a lot photo the CDN no longer serves, a timeout) is recorded against that item, which is tried
// again later and skipped after a few tries; the other items carry on.

export const NATIVE_HOST = "com.erjoha.fbaw.claude";
const DEBOUNCE_MS = 5_000;
const ERROR_COOLDOWN_MS = 10 * 60_000;

type HostReply =
  | { ok: true; result: { results: { id: number; [k: string]: unknown }[]; claimed?: unknown }; durationMs?: number }
  /** `global`: the failure isn't about this request's items (see isGlobalError). */
  | { ok: false; error: string; global: boolean };

let timer: ReturnType<typeof setTimeout> | null = null;
let running = false;
let again = false;

/** Asks for a run soon; bursts of saves (a scan) collapse into one run. */
export function scheduleClaude(store: Store, onAnswered: () => void): void {
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => {
    timer = null;
    void run(store, onAnswered);
  }, DEBOUNCE_MS);
}

async function ask(request: ClaudeRequest): Promise<HostReply> {
  try {
    const reply = (await chrome.runtime.sendNativeMessage(NATIVE_HOST, request)) as
      | { ok: true; result: { results: { id: number }[] } }
      | { ok: false; error: string };
    return reply.ok ? (reply as HostReply) : { ok: false, error: reply.error, global: isGlobalError(reply.error) };
  } catch (err) {
    // Chrome couldn't reach the bridge at all (not installed, not allowed, crashed): nothing will work.
    const text = err instanceof Error ? err.message : String(err);
    return {
      ok: false,
      global: true,
      error: /not found|forbidden/i.test(text)
        ? "The Claude bridge isn't installed (run apps/fb_auction_watcher/native/install.sh)"
        : text,
    };
  }
}

async function run(store: Store, onAnswered: () => void) {
  if (running) {
    again = true;
    return;
  }
  running = true;
  try {
    const settings = await getSettings();
    if (!settings.useClaude) return;
    const state = await getClaudeState();
    if (state.error && state.lastAt && Date.now() - Date.parse(state.lastAt) < ERROR_COOLDOWN_MS) return;

    const now = new Date();
    const at = now.toISOString();
    let failures: Failures = pruneFailures(await loadFailures(store), now);
    let photoCalls = recentPhotoCalls(state.photoCalls ?? [], now);
    const pending = await pendingItems(store, { myName: settings.myName, now, failures, photoCallsLeft: photoCallsLeft(photoCalls, now) });
    const { endTimes, bids, claimLots } = pending;
    if (endTimes.length === 0 && bids.length === 0 && claimLots.length === 0) {
      await saveFailures(store, failures);
      if (pending.photoLimited) await updateClaudeState({ photoLimitUntil: photoLimitFreesAt(photoCalls, now), photoCalls });
      return;
    }

    const saved: StoredAnswer[] = [];
    const read = { endTimes: 0, bids: 0, claimLots: 0 };
    let failed = 0;
    const newlySkipped: ItemFailure[] = [];
    const answered = (key: string, value: unknown) => {
      saved.push({ key, value, at });
      delete failures[key];
    };
    const failedItem = (key: string, task: ClaudeTask, error: string) => {
      failures = recordFailure(failures, key, task, error, new Date());
      if (failureStatus(failures[key], new Date()) === "skipped") newlySkipped.push(failures[key]);
      else failed++;
    };
    /** A global failure: keep what was answered, pause everything for a while. */
    const stop = async (error: string) => {
      await store.saveAnswers(saved); // Keep what earlier requests answered (review M3).
      await saveFailures(store, failures);
      await updateClaudeState({ lastAt: at, error, lastOutcome: `Failed: ${error}`, photoCalls });
      if (saved.length) onAnswered();
    };

    for (const [items, request, field, count] of [
      [endTimes, endTimes.length ? endTimeRequest(endTimes) : null, "endsAt", "endTimes"],
      [bids, bids.length ? bidRequest(bids) : null, "amount", "bids"],
    ] as const) {
      if (!request) continue;
      const reply = await ask(request);
      if (!reply.ok) {
        if (reply.global) return await stop(reply.error);
        // The batch failed as a whole: each of its items gets a failed try.
        for (const item of items) failedItem(item.key, request.task, reply.error);
        continue;
      }
      for (const r of reply.result.results) {
        const item = items.find((i) => i.id === r.id);
        if (item) {
          answered(item.key, r[field] ?? null);
          read[count]++;
        }
      }
    }
    for (const lot of claimLots) {
      // Every photo call counts against the hourly cap, failed or not, and is saved before asking
      // so a worker restart mid-run can't forget it.
      photoCalls = [...photoCalls, new Date().toISOString()];
      await updateClaudeState({ photoCalls });
      const reply = await ask(claimLotRequest(lot));
      if (!reply.ok) {
        if (reply.global) return await stop(reply.error);
        failedItem(lot.key, "claim-lot", reply.error);
        continue; // One broken lot (e.g. an expired photo URL) doesn't hold up the others.
      }
      answered(lot.key, reply.result);
      read.claimLots++;
    }

    await store.saveAnswers(saved);
    await saveFailures(store, failures);
    // Hit the cap with lots still waiting: say so, and leave the rest to later runs.
    const photoLimited = pending.photoLimited || (pending.more && photoCallsLeft(photoCalls, new Date()) === 0);
    if (pending.more && !photoLimited) again = true; // Work through the rest in the next run.
    await updateClaudeState({
      lastAt: at,
      error: null,
      lastOutcome: describeRun({ read, failed, skipped: [...pending.skipped.map((s) => s.failure), ...newlySkipped], photoLimited }),
      photoLimitUntil: photoLimited ? photoLimitFreesAt(photoCalls, new Date()) : null,
      photoCalls,
    });
    if (saved.length) onAnswered();
  } finally {
    running = false;
    if (again) {
      again = false;
      scheduleClaude(store, onAnswered);
    }
  }
}
