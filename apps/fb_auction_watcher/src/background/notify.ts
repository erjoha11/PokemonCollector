import type { Lot } from "../domain/bids";
import { getSettings } from "../shared/settings";
import { lotUrl } from "../shared/urls";
import type { MyAuction } from "./watch";

// Desktop notifications (chrome.notifications): you were outbid, a sale you're in ends in 10
// minutes, and what you won or lost once its final read is in. Clicking one opens that lot (or the
// post) on Facebook. Each is shown once (by key). Off with Settings → "Desktop notifications".
// Read-only like everything else: a notification never bids, it only opens the page.

export type Note = {
  /** Shown once per key, e.g. "outbid:<post>:<comment>:<highest>". */
  key: string;
  title: string;
  message: string;
  /** Opened when you click it. */
  url: string;
};

export type Sale = { postId: string; title: string; url: string };

const lotKey = (l: Lot) => l.commentId ?? `#${l.position}`;
const kr = (n: number | null) => (n === null ? "?" : `${n} kr`);

/** Lots you were leading (or might have been) at the previous read, and are outbid on now. */
export function outbidNotes(before: Lot[] | null, after: Lot[], sale: Sale): Note[] {
  const was = new Map((before ?? []).map((l) => [lotKey(l), l.myStatus]));
  return after
    .filter((l) => l.myStatus === "outbid" && (was.get(lotKey(l)) === "lead" || was.get(lotKey(l)) === "unclear"))
    .map((l) => {
      const next = l.highestBid !== null ? l.highestBid + (l.increment ?? 1) : null;
      return {
        key: `outbid:${sale.postId}:${lotKey(l)}:${l.highestBid}`,
        title: `Outbid: ${l.title}`,
        message: `${sale.title} · highest ${kr(l.highestBid)} (you ${kr(l.myHighestBid)})${next !== null ? ` · next bid ${next} kr` : ""}`,
        url: lotUrl(sale, l),
      };
    });
}

/** Once a sale's first final read is in: what you won (and for how much) and what you lost. */
export function resultNotes(after: Lot[], sale: Sale): Note[] {
  const won = after.filter((l) => l.myStatus === "lead");
  const lost = after.filter((l) => l.myStatus === "outbid");
  if (!won.length && !lost.length) return [];
  const names = (ls: Lot[]) => ls.map((l) => l.title).join(", ");
  const total = won.reduce((n, l) => n + (l.myHighestBid ?? 0), 0);
  const title = won.length
    ? `Won ${won.length} lot${won.length === 1 ? "" : "s"} · ${total} kr${lost.length ? ` · lost ${lost.length}` : ""}`
    : `Lost ${lost.length} lot${lost.length === 1 ? "" : "s"}`;
  return [
    {
      key: `result:${sale.postId}`,
      title,
      message: `${sale.title}${won.length ? ` · won: ${names(won)}` : ""}${lost.length ? ` · lost: ${names(lost)}` : ""}`,
      url: sale.url,
    },
  ];
}

/** A sale you're in ends within 10 minutes: where you stand at its last read. */
export function endingSoonNote(a: MyAuction, now: number): Note {
  const min = Math.max(1, Math.round((a.endsAt! - now) / 60_000));
  const lead = a.lots.filter((l) => l.myStatus === "lead").length;
  const outbid = a.lots.filter((l) => l.myStatus === "outbid").length;
  const unclear = a.lots.filter((l) => l.myStatus === "unclear").length;
  const parts = [lead && `leading ${lead}`, outbid && `outbid ${outbid}`, unclear && `unclear ${unclear}`].filter(Boolean);
  return {
    key: `ending:${a.postId}:${a.endsAt}`,
    title: `Ends in ${min} min: ${a.title}`,
    message: `${parts.join(" · ")} (at the last read, ${Math.round((now - a.lastRead) / 60_000)} min ago)`,
    url: a.url,
  };
}

/** Keys shown, kept 3 days so a note isn't repeated (a re-read sees the same outbid again). */
const SHOWN_KEPT_MS = 3 * 24 * 3600_000;

/** Shows the notes not shown before, if notifications are on. */
export async function showNotes(notes: Note[]): Promise<void> {
  if (!notes.length || !(await getSettings()).notify) return;
  const now = Date.now();
  const stored = ((await chrome.storage.local.get("notified")).notified as Record<string, number> | undefined) ?? {};
  const shown = Object.fromEntries(Object.entries(stored).filter(([, t]) => now - t < SHOWN_KEPT_MS));
  const urls = ((await chrome.storage.session.get("noteUrls")).noteUrls as Record<string, string> | undefined) ?? {};
  for (const n of notes) {
    if (shown[n.key]) continue;
    shown[n.key] = now;
    const id = `fbaw:${n.key}`;
    urls[id] = n.url;
    await chrome.notifications
      .create(id, { type: "basic", iconUrl: chrome.runtime.getURL("icon-128.png"), title: n.title, message: n.message, priority: 1 })
      .catch(() => {});
  }
  await chrome.storage.local.set({ notified: shown });
  await chrome.storage.session.set({ noteUrls: urls });
}

/** Clicking a notification opens its lot or post in a new tab (nothing else). */
export function listenForNoteClicks(): void {
  chrome.notifications.onClicked.addListener((id) => {
    void (async () => {
      const urls = ((await chrome.storage.session.get("noteUrls")).noteUrls as Record<string, string> | undefined) ?? {};
      const url = urls[id];
      if (url && /^https:\/\/www\.facebook\.com\//.test(url)) await chrome.tabs.create({ url });
      await chrome.notifications.clear(id);
    })();
  });
}
