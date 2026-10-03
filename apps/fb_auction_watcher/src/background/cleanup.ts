import { getSettings, updateCleanupState } from "../shared/settings";
import type { Store } from "../store";
import { applyRetention, describeRetention } from "../store/retention";

// The daily cleanup of old stored data (review M6; the rule is in src/store/retention.ts).
// A repeating chrome.alarms alarm, created when missing each time the worker starts (alarms
// usually survive restarts, but not always). It only touches the local store, never Facebook,
// so it doesn't need the Facebook slot or the idle check.

export const CLEANUP_ALARM = "fbaw-cleanup";
const EVERY_MINUTES = 24 * 60;
/** The first run, a little after the worker starts (not in the middle of its startup work). */
const FIRST_AFTER_MINUTES = 5;

export async function scheduleCleanup(): Promise<void> {
  if (await chrome.alarms.get(CLEANUP_ALARM)) return;
  await chrome.alarms.create(CLEANUP_ALARM, { delayInMinutes: FIRST_AFTER_MINUTES, periodInMinutes: EVERY_MINUTES });
}

/** One cleanup: delete what's past its time, record the outcome for the overview, report what went. */
export async function runCleanup(store: Store, onRemoved: () => void): Promise<void> {
  const at = new Date();
  try {
    const result = await applyRetention(store, (await getSettings()).myName, at);
    await updateCleanupState({ lastAt: at.toISOString(), lastOutcome: describeRetention(result) });
    if (result.captures + result.posts + result.answers > 0) onRemoved();
  } catch (err) {
    await updateCleanupState({ lastAt: at.toISOString(), lastOutcome: `Failed: ${err instanceof Error ? err.message : String(err)}` });
  }
}
