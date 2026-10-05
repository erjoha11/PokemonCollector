import { buildWonPayload } from "../inbox/payload";
import { buildRows } from "../pages/dashboard/model";
import { getEndedMarks, getNotWonMarks, getSettings, getWonState, inboxOrigin, markInboxSent, setInboxState, type InboxState } from "../shared/settings";
import type { Store } from "../store";

// "Send wins to inventory" (#309): posts your own wins (src/inbox/payload.ts) to your
// tcg_inventory's POST /inbox/fb-wins, from the service worker. A fetch from here, to an origin the
// extension has host permission for, isn't subject to CORS. Only on your click for now; nothing
// talks to Facebook here. The last result is kept as `inboxState` for the overview.

export const INBOX_PATH = "/inbox/fb-wins";
const TIMEOUT_MS = 30_000;

export type SendDeps = {
  fetch: typeof fetch;
  /** Whether the extension may reach this origin (the optional host permission granted in Settings). */
  hasPermission: (origin: string) => Promise<boolean>;
  now: () => Date;
};

const defaultDeps: SendDeps = {
  fetch: (...args) => fetch(...args),
  hasPermission: (origin) => chrome.permissions.contains({ origins: [`${origin}/*`] }),
  now: () => new Date(),
};

/** "3 new, 1 updated, 2 already registered or ignored". */
function describe(r: { added?: number; updated?: number; unchanged?: number; kept?: number }): string {
  const parts = [`${r.added ?? 0} new`, `${r.updated ?? 0} updated`];
  if (r.unchanged) parts.push(`${r.unchanged} unchanged`);
  if (r.kept) parts.push(`${r.kept} already registered or ignored`);
  return parts.join(", ");
}

/** Sends the won auctions you picked (`postIds`), or all of them when it's omitted. */
export async function sendWins(store: Store, deps: SendDeps = defaultDeps, postIds?: string[]): Promise<InboxState> {
  const now = deps.now();
  const done = async (ok: boolean, count: number | null, outcome: string): Promise<InboxState> => {
    const state: InboxState = { lastAt: now.toISOString(), ok, count, outcome };
    await setInboxState(state);
    return state;
  };

  if (postIds && postIds.length === 0) return done(false, null, "Tick the won auctions to send first.");
  const settings = await getSettings();
  const origin = inboxOrigin(settings.inboxUrl);
  if (!origin) return done(false, null, "Set your tcg_inventory address in Settings first.");
  if (!settings.inboxToken) return done(false, null, "Set the inbox token in Settings first.");
  if (!(await deps.hasPermission(origin))) return done(false, null, `No permission to reach ${origin}: save the address in Settings again.`);

  const [posts, captures, answers, endedMarks, notWonMarks, wonState] = await Promise.all([
    store.allPosts(),
    store.allCaptures(),
    store.allAnswers(),
    getEndedMarks(),
    getNotWonMarks(),
    getWonState(),
  ]);
  // Lots you marked "Not won" (#329) aren't wins: wonBySeller leaves them out, so they're never sent.
  const rows = buildRows(posts, now, null, {
    captures: new Map(captures.map((c) => [c.postId, c.capture])),
    answers: new Map(answers.map((a) => [a.key, a.value])),
    myName: settings.myName,
    endedMarks,
    notWonMarks,
  });
  const payload = buildWonPayload(rows, wonState, now, postIds ? new Set(postIds) : undefined);
  const count = payload.items.length;
  if (count === 0) return done(true, 0, "Nothing won to send.");

  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), TIMEOUT_MS);
  try {
    const res = await deps.fetch(`${origin}${INBOX_PATH}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${settings.inboxToken}` },
      body: JSON.stringify(payload),
      signal: abort.signal,
      // Never follow a redirect (e.g. to a login page) with the token.
      redirect: "error",
      credentials: "omit",
    });
    type Answer = { error?: string; added?: number; updated?: number; unchanged?: number; kept?: number };
    const body = (await res.json().catch(() => null)) as Answer | null;
    if (!res.ok) return done(false, count, `HTTP ${res.status}: ${body?.error ?? (res.statusText || "error")}`);
    await markInboxSent([...new Set(payload.items.map((i) => i.external_ref.split(":")[1]))], now);
    return done(true, count, describe(body ?? {}));
  } catch (err) {
    const why = abort.signal.aborted ? `no answer in ${TIMEOUT_MS / 1000} s` : err instanceof Error ? err.message : String(err);
    return done(false, count, `Couldn't reach ${origin}: ${why}`);
  } finally {
    clearTimeout(timer);
  }
}
