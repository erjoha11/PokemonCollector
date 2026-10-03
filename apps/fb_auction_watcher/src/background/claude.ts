import { claimLotsToRead, unsureReplies, type ClaimLotInput } from "../domain/bids";
import { interpretListing } from "../domain/listing";
import {
  bidAnswerKey,
  bidRequest,
  claimLotAnswerKey,
  claimLotRequest,
  endTimeAnswerKey,
  endTimeRequest,
  type BidItem,
  type ClaudeRequest,
  type EndTimeItem,
} from "../llm/prompts";
import { getClaudeState, getSettings, updateClaudeState } from "../shared/settings";
import type { Store, StoredAnswer } from "../store";

// Sends what the rules couldn't read to Claude Code through the native bridge
// (native/fbaw_claude_host.py → `claude -p`, on the user's own Claude login; no API key).
// Only unread items, batched (one call per kind), each answer cached so it's never asked again.

export const NATIVE_HOST = "com.erjoha.fbaw.claude";
const DEBOUNCE_MS = 5_000;
const ERROR_COOLDOWN_MS = 10 * 60_000;
const MAX_END_TIMES = 20;
const MAX_BIDS = 40;
/** Photo questions are one call each (Sonnet), so fewer per run. */
const MAX_CLAIM_LOTS = 5;

type HostReply =
  | { ok: true; result: { results: { id: number; [k: string]: unknown }[]; claimed?: unknown }; durationMs?: number }
  | { ok: false; error: string };

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
    return (await chrome.runtime.sendNativeMessage(NATIVE_HOST, request)) as HostReply;
  } catch (err) {
    const text = err instanceof Error ? err.message : String(err);
    return {
      ok: false,
      error: /not found|forbidden/i.test(text)
        ? "The Claude bridge isn't installed (run apps/fb_auction_watcher/native/install.sh)"
        : text,
    };
  }
}

/** What still needs Claude: end times the rules couldn't find in complete text, and unsure bids. */
export async function pendingItems(store: Store, myName = "") {
  const [posts, captures, answers] = await Promise.all([store.allPosts(), store.allCaptures(), store.allAnswers()]);
  const answered = new Set(answers.map((a) => a.key));
  const endTimes: (EndTimeItem & { key: string })[] = [];
  for (const p of posts) {
    if (!p.textComplete) continue;
    const i = interpretListing(p.text, new Date(p.firstSeenAt));
    if ((i.type !== "auction" && i.type !== "claim") || i.endsAt) continue;
    const key = endTimeAnswerKey(p.text);
    if (answered.has(key) || endTimes.some((e) => e.key === key)) continue;
    endTimes.push({ id: endTimes.length, text: p.text, capturedAt: p.firstSeenAt, key });
  }
  const bids: (BidItem & { key: string })[] = [];
  for (const { capture } of captures) {
    // Only auctions have bids to read; claim and fixed-price replies are claims.
    if (interpretListing(capture.post.text, new Date(capture.capturedAt)).type !== "auction") continue;
    for (const u of unsureReplies(capture)) {
      const key = bidAnswerKey(u.seller, u.text);
      if (answered.has(key) || bids.some((b) => b.key === key)) continue;
      bids.push({ id: bids.length, seller: u.seller, text: u.text, key });
    }
  }
  // Claim and fixed-price lots (yours first): every card, its price on the photo, taken or for sale.
  const claimLots: (ClaimLotInput & { key: string })[] = [];
  for (const { capture } of captures) {
    const type = interpretListing(capture.post.text, new Date(capture.capturedAt)).type;
    if (type !== "claim" && type !== "fixed") continue;
    for (const lot of claimLotsToRead(capture, myName)) {
      const key = claimLotAnswerKey(lot);
      if (!answered.has(key) && !claimLots.some((x) => x.key === key)) claimLots.push({ ...lot, key });
    }
  }
  return {
    endTimes: endTimes.slice(0, MAX_END_TIMES),
    bids: bids.slice(0, MAX_BIDS),
    claimLots: claimLots.slice(0, MAX_CLAIM_LOTS),
    /** More claim lots wait than fit in one run: run again after this one. */
    more: claimLots.length > MAX_CLAIM_LOTS,
  };
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

    const { endTimes, bids, claimLots, more } = await pendingItems(store, settings.myName);
    if (more) again = true; // Work through the rest in the next run.
    if (endTimes.length === 0 && bids.length === 0 && claimLots.length === 0) return;

    const at = new Date().toISOString();
    const saved: StoredAnswer[] = [];
    const done: string[] = [];
    for (const [items, request, field, label] of [
      [endTimes, endTimes.length ? endTimeRequest(endTimes) : null, "endsAt", "end time"],
      [bids, bids.length ? bidRequest(bids) : null, "amount", "bid"],
    ] as const) {
      if (!request) continue;
      const reply = await ask(request);
      if (!reply.ok) {
        await store.saveAnswers(saved); // Keep what earlier batches answered (review M3).
        await updateClaudeState({ lastAt: at, error: reply.error, lastOutcome: `Failed: ${reply.error}` });
        return;
      }
      for (const r of reply.result.results) {
        const item = items.find((i) => i.id === r.id);
        if (item) saved.push({ key: item.key, value: r[field] ?? null, at });
      }
      done.push(`${items.length} ${label}${items.length === 1 ? "" : "s"}`);
    }
    for (const lot of claimLots) {
      const reply = await ask(claimLotRequest(lot));
      if (!reply.ok) {
        await store.saveAnswers(saved); // Keep what was answered before the failure.
        await updateClaudeState({ lastAt: at, error: reply.error, lastOutcome: `Failed: ${reply.error}` });
        return;
      }
      saved.push({ key: lot.key, value: reply.result, at });
    }
    if (claimLots.length) done.push(`${claimLots.length} claim lot photo${claimLots.length === 1 ? "" : "s"}`);
    await store.saveAnswers(saved);
    await updateClaudeState({ lastAt: at, error: null, lastOutcome: `Read ${done.join(" and ")}` });
    onAnswered();
  } finally {
    running = false;
    if (again) {
      again = false;
      scheduleClaude(store, onAnswered);
    }
  }
}
