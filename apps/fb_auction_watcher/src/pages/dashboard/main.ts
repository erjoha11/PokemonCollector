import { fullSizePhoto, type Lot } from "../../domain/bids";
import type { PostCapture } from "../../shared/capture";
import { isStoreUpdatedMessage, MSG_QUEUE_READ, MSG_SEND_WINS, type QueueReadMessage } from "../../shared/messages";
import type { ReaderState } from "../../shared/reader";
import {
  getAutoScanState,
  getClaudeState,
  getCleanupState,
  getEndedMarks,
  getInboxState,
  getSettings,
  getWonState,
  inboxOrigin,
  markEnded,
  markWon,
  updateSettings,
  type AutoScanState,
  type ClaudeState,
  type CleanupState,
  type EndedMarks,
  type InboxState,
  type Settings,
  type WonState,
} from "../../shared/settings";
import { idbStore } from "../../store";
import {
  ago,
  buildRows,
  countdown,
  countRows,
  endLabel,
  krText,
  leadingBySale,
  lotStatus,
  lotUrl,
  readAfterEnd,
  needsYou,
  saleLines,
  saleResult,
  tabs,
  wonBySeller,
  wonTotal,
  type LotStatus,
  type NeedsYouItem,
  type Row,
  type TabId,
  type WonSeller,
} from "./model";

// The overview: every sale read from the feed, grouped and sorted by end time, with live
// countdowns, and for posts you've read with the icon: lots, bids and your Leading/Outbid
// status. Reads the store directly (same extension origin as the service worker) and re-reads
// when the worker says something changed. The seller's original text is always shown next to
// interpreted values (docs/spec.md rule), and anything read by Claude is marked as such.

const store = idbStore();
type Filter = "all" | "auction" | "claim" | "fixed";

const $ = <T extends Element>(sel: string) => document.querySelector(sel) as T;
const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

/** An amount in kr, emphasized ("?" when unknown). */
const kr = (n: number | null | undefined) => el("strong", "kr", n === null || n === undefined ? "? kr" : `${n} kr`);
/** A line mixing plain text and emphasized amounts: line("div", "Your bid ", kr(20), " · highest ", kr(40)). */
function line(tag: "div" | "span", ...parts: (string | Node)[]) {
  const e = document.createElement(tag);
  e.append(...parts);
  return e;
}

// Per-viewer conveniences only; the page works without them (private windows etc.).
const local = {
  get(key: string): string | null {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  },
  set(key: string, value: string) {
    try {
      localStorage.setItem(key, value);
    } catch {
      /* ignore */
    }
  },
};

/**
 * What you've folded or unfolded in My Auctions (Leading sales "lead:<post id>", To pay sellers
 * "pay:<seller>"), remembered in this browser. Both start folded.
 */
const folds: Record<string, boolean> = (() => {
  try {
    return JSON.parse(local.get("fbaw-folds") ?? "{}") as Record<string, boolean>;
  } catch {
    return {};
  }
})();
const isFolded = (key: string) => folds[key] ?? (key.startsWith("lead:") || key.startsWith("pay:"));
function setFolded(key: string, folded: boolean) {
  folds[key] = folded;
  local.set("fbaw-folds", JSON.stringify(folds));
}
/** A <details> that remembers whether you folded it. */
function foldable(key: string, cls: string): HTMLDetailsElement {
  const d = el("details", cls);
  d.open = !isFolded(key);
  d.dataset.fold = key;
  d.addEventListener("toggle", () => setFolded(key, !d.open));
  return d;
}

const previousVisit = local.get("fbaw-last-visit");
const lastVisit = previousVisit ? new Date(previousVisit) : null;
local.set("fbaw-last-visit", new Date().toISOString());

let rows: Row[] = [];
const FILTERS: Filter[] = ["all", "auction", "claim", "fixed"];
const TAB_IDS: TabId[] = ["new", "today", "upcoming", "noend", "mine", "ended"];
let filter: Filter = FILTERS.find((f) => f === local.get("fbaw-filter")) ?? "all"; // "mine" is a tab now.
let tab: TabId = TAB_IDS.find((t) => t === local.get("fbaw-tab")) ?? "today";
let query = "";
let lastFeedReadAt: string | null = null;
let settings: Settings;
let autoScan: AutoScanState;
let claude: ClaudeState;
let cleanup: CleanupState;
let wonState: WonState = {};
let endedMarks: EndedMarks = {};
let inbox: InboxState = { lastAt: null, ok: null, count: null, outcome: null };
let sendingWins = false;
let showDone = local.get("fbaw-show-done") === "1";
let reader: ReaderState = { queue: [], current: null, lastAt: null, lastOutcome: null };
const expanded = new Set<string>();
const justClicked = new Set<string>();
let source: { posts: Awaited<ReturnType<typeof store.allPosts>>; captures: Map<string, PostCapture>; answers: Map<string, unknown> } = {
  posts: [],
  captures: new Map(),
  answers: new Map(),
};

function rebuild() {
  rows = buildRows(source.posts, new Date(), lastVisit, { ...source, myName: settings.myName, endedMarks });
}

/** The small status objects: auto-scan, Claude, the daily cleanup, the reader. */
async function loadStatus() {
  [settings, autoScan, claude, cleanup, wonState, endedMarks, inbox] = await Promise.all([
    getSettings(),
    getAutoScanState(),
    getClaudeState(),
    getCleanupState(),
    getWonState(),
    getEndedMarks(),
    getInboxState(),
  ]);
  reader = { ...reader, ...((await chrome.storage.local.get("readerState")).readerState as Partial<ReaderState> | undefined) };
  for (const id of justClicked) if (reader.current?.postId === id || reader.queue.some((j) => j.postId === id)) justClicked.delete(id);
}

async function load() {
  const [posts, captures, answers, readAt] = await Promise.all([
    store.allPosts(),
    store.allCaptures(),
    store.allAnswers(),
    store.getMeta("lastFeedReadAt"),
  ]);
  await loadStatus();
  source = {
    posts,
    captures: new Map(captures.map((c) => [c.postId, c.capture])),
    answers: new Map(answers.map((a) => [a.key, a.value])),
  };
  lastFeedReadAt = readAt;
  rebuild();
  render();
}

function matches(r: Row): boolean {
  if (filter !== "all" && r.type !== filter) return false;
  if (!query) return true;
  const q = query.toLowerCase();
  return [r.title, r.sellerName ?? "", r.text, r.description ?? ""].some((s) => s.toLowerCase().includes(q));
}

/** The sale type, then its price terms: "Auction / Min 10 kr · +5", "Fixed price / 2000 kr". */
function priceCell(r: Row): HTMLTableCellElement {
  const td = el("td", "price");
  td.append(el("div", "type", TYPE_LABEL[r.type]));
  const terms = el("div");
  if (r.type === "auction") {
    terms.append(...(r.minPrice !== null ? ["Min ", kr(r.minPrice)] : ["Min per lot"]));
    if (r.increment !== null) terms.append(` · +${r.increment}`);
  } else {
    terms.append(r.fixedPrice !== null ? kr(r.fixedPrice) : "Price per item");
  }
  td.append(terms);
  return td;
}

/** An ended sale's outcome: "9 of 12 sold · 6220 kr", then "final" or "at last read <when>". */
function resultCell(r: Row, now: Date): HTMLTableCellElement {
  const td = el("td", "price result");
  const res = saleResult(r);
  if (!res) {
    td.append(el("span", "muted", "Not read"));
    td.title = "Never read, so there's no result. Click the sale's name to read it.";
    return td;
  }
  const claims = r.type === "claim" || r.type === "fixed";
  td.append(el("div", undefined, `${res.sold} of ${res.lots} ${claims ? "claimed" : "sold"}`));
  // Line 2: what it went for, and whether that's final.
  const sum = line("div");
  sum.className = "one-line";
  if (res.kr || res.unknownPrices) sum.append(kr(res.kr), res.unknownPrices ? " + ?" : "", " · ");
  if (res.final) sum.append(el("span", "result-final", "final"));
  else {
    const note = el("span", "flag", res.readAt ? `at last read ${endLabel(Date.parse(res.readAt), now)}` : "partial read");
    note.title = "Not read in full after the end: bids may have come in after this. Open the post to see the final result.";
    sum.append(note);
  }
  td.append(sum);
  td.title = claims
    ? "Lots someone claimed, and what the claimed cards cost (where Claude has read the prices)"
    : "Lots with a valid bid, and the sum of the winning bids";
  return td;
}

const TYPE_LABEL: Record<Row["type"], string> = {
  auction: "Auction", claim: "Claim", fixed: "Fixed price", wanted: "Wanted", trade: "Trade", other: "Other",
};

/**
 * "Mark as ended" for a sale that's over though its end time says otherwise (none could be read,
 * or the seller closed early), or "Undo" for one you marked. Your own mark, in chrome.storage.
 */
function endedToggle(r: Row, label = "Mark as ended"): HTMLButtonElement | null {
  if (r.ended && !r.endedByYouAt) return null; // Ended by its end time: nothing to mark.
  const button = el("button", "linkish end-toggle", r.endedByYouAt ? "Undo ended" : label);
  button.type = "button";
  button.title = r.endedByYouAt
    ? `You marked this sale as ended ${ago(r.endedByYouAt, new Date())}. Undo puts it back by its end time.`
    : "The sale is over: stop counting down, and take your lots' statuses from the last full read (Leading becomes Won).";
  button.addEventListener("click", (e) => {
    e.preventDefault(); // Inside a <summary>: don't fold or unfold.
    e.stopPropagation();
    void markEnded(r.id, !r.endedByYouAt);
  });
  return button;
}

/**
 * Ends, in two lines: the countdown, then the end time and antisnipe. The seller's own words go in
 * the tooltip, and take line 2 when the rules weren't sure or Claude read the time (decided
 * 2026-10-04: the original text is shown in the row whenever there's reason to check it).
 */
function endsCell(r: Row, now: Date): HTMLTableCellElement {
  const td = el("td", "ends");
  const orig = r.endsAtText ? `The seller's text: ${r.endsAtText}` : "";
  if (r.endedByYouAt) {
    td.append(el("div", "countdown", "Ended"), line("div", el("span", "when", `marked by you ${ago(r.endedByYouAt, now)} · `), endedToggle(r)!));
    td.title = orig;
    return td;
  }
  if (r.ended && r.endsAtMs !== null) {
    td.append(el("div", "countdown ended", `Ended ${ago(new Date(r.endsAtMs).toISOString(), now)}`), el("div", "when one-line", endLabel(r.endsAtMs, now)));
    td.title = [`Ended ${endLabel(r.endsAtMs, now)}`, orig].filter(Boolean).join("\n");
  } else if (r.endsAtMs !== null) {
    const cd = el("div", "countdown", r.maybeEnded ? "Ended?" : countdown(r.endsAtMs, now));
    cd.dataset.ends = String(r.endsAtMs);
    if (!r.ended && r.endsAtMs - now.getTime() < 3_600_000) cd.classList.add("soon");
    td.append(cd);
    const when = endLabel(r.endsAtMs, now) + (r.softCloseMinutes ? ` · +${r.softCloseMinutes}m` : "");
    if (r.endsViaClaude) td.append(el("div", "via one-line", `Claude read: ${r.endsAtText ?? when}`));
    else if (!r.sure) td.append(el("div", "flag one-line", `? ${r.endsAtText ?? when}`));
    else td.append(el("div", "when one-line", when));
    td.title = [
      `Ends ${when}`,
      r.softCloseMinutes ? `Antisnipe: a late bid extends the end by ${r.softCloseMinutes} min` : "",
      orig,
      r.endsViaClaude ? "The rules couldn't read it; Claude did." : !r.sure ? "The rules weren't sure: check the seller's text." : "",
    ]
      .filter(Boolean)
      .join("\n");
  } else {
    // No end time: fixed price, or one nobody could read. (When it was added is in its own column.)
    const cutOff = r.type !== "fixed" && !r.textComplete && !r.endsAtText;
    const head = line("div", el("span", "countdown unknown", r.type === "fixed" ? "No end" : "Unknown"));
    if (cutOff) head.append(" ", el("span", "flag", "cut off"));
    td.append(head);
    td.title = [
      r.type === "fixed" ? "Fixed price: no end time." : orig || "No end time in the post's text that the rules or Claude could read.",
      cutOff ? "The post's text was cut off before an end time." : "",
    ]
      .filter(Boolean)
      .join("\n");
  }
  return td;
}

/** When the sale was added: when a scan first saw the post (close to when it was posted while auto-scan runs). */
function addedCell(r: Row, now: Date): HTMLTableCellElement {
  const td = el("td", "added");
  const seen = Date.parse(r.firstSeenAt);
  td.append(el("div", undefined, ago(r.firstSeenAt, now)), el("div", "when", endLabel(seen, now)));
  td.title = `Added ${new Date(seen).toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })} (when a scan first saw it)`;
  return td;
}

/** Clicking a row (not its links, photos or buttons) shows or hides its lots. It never opens a tab. */
function makeExpandable(tr: HTMLTableRowElement, id: string) {
  const open = expanded.has(id);
  tr.classList.add("expandable");
  tr.tabIndex = 0;
  tr.setAttribute("aria-expanded", String(open));
  tr.title = open ? "Click to hide the lots" : "Click to show the lots";
  const toggle = () => {
    if (expanded.has(id)) expanded.delete(id);
    else expanded.add(id);
    render();
  };
  tr.addEventListener("click", (e) => {
    if ((e.target as Element).closest("a, button, img, input, summary")) return;
    if (window.getSelection()?.toString()) return; // Selecting text isn't a click.
    toggle();
  });
  tr.addEventListener("keydown", (e) => {
    if (e.target === tr && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      toggle();
    }
  });
}

/** The sale's name without the template's type words ("AUKSJON/BUDRUNDE"); see `saleLines`. */
const saleTitle = (r: Row) => saleLines(r.title, r.description).title;

/** A sale's name: a plain link to the post on Facebook (it opens it, nothing else). */
function titleLink(r: Row): HTMLAnchorElement {
  const a = el("a", "title", saleTitle(r));
  a.href = r.url;
  a.target = "_blank";
  a.rel = "noopener";
  a.title = "Open the post on Facebook";
  return a;
}

/** Reads a sale now in a background tab that closes itself; the row updates when it's done. */
function readSale(r: Row) {
  const msg: QueueReadMessage = { type: MSG_QUEUE_READ, postId: r.id, url: r.url };
  justClicked.add(r.id); // Shows "Reading…" until the worker's state catches up.
  setTimeout(() => {
    justClicked.delete(r.id); // The worker never picked it up: don't show "Reading…" forever.
    render();
  }, 60_000);
  render();
  void chrome.runtime.sendMessage(msg).catch(() => {
    justClicked.delete(r.id);
    render();
  });
}

/** Read: reads the sale's lots and bids now. (Its name is the link to Facebook.) */
function saleActions(r: Row): HTMLSpanElement {
  const box = el("span", "sale-actions");
  const state = readState(r.id);
  const read = el("button", `act-btn${state ? " busy" : ""}`, state ?? "Read");
  read.type = "button";
  read.disabled = !!state;
  read.title = state
    ? "Reading this post in a background tab"
    : `Read its lots and bids now, in a background tab that closes itself${r.lastReadAt ? ` (last read ${ago(r.lastReadAt, new Date())})` : ""}`;
  read.addEventListener("click", (e) => {
    e.preventDefault(); // Inside a Leading card's <summary>: don't fold it.
    e.stopPropagation(); // Inside a table row: don't expand it.
    readSale(r);
  });
  box.append(read);
  return box;
}

/** The sale's name as a link. */
function titleLine(r: Row): HTMLDivElement {
  const line = el("div", "title-line");
  line.append(titleLink(r));
  return line;
}

/** Photo, then two lines (the sale's name, a link to Facebook, and New; its description), then Read. */
function saleCell(r: Row): HTMLTableCellElement {
  const td = el("td", "sale");
  const wrap = el("div", "sale-wrap");
  td.append(wrap);
  if (r.thumbnailUrl) {
    const img = el("img", "thumb");
    img.src = r.thumbnailUrl;
    img.alt = "";
    img.loading = "lazy";
    img.referrerPolicy = "no-referrer";
    // From the sale's thumbnail, ← / → go on through its lots.
    zoomable(img, [{ src: img.src, caption: r.title }, ...lotPhotos(r.lots ?? [])], 0);
    wrap.append(img);
  }
  const box = el("div", "sale-text");
  const title = titleLine(r);
  if (r.isNew) title.prepend(el("span", "badge new", "New"), " ");
  box.append(title);
  const { detail } = saleLines(r.title, r.description);
  if (detail) box.append(el("div", "desc", detail));
  wrap.append(box, saleActions(r));
  // The full text, and when it was first seen (the Seen column until 2026-10-04).
  td.title = [r.title, r.description && r.description !== r.title ? r.description : ""]
    .filter(Boolean)
    .join("\n");
  return td;
}

function sellerCell(r: Row): HTMLTableCellElement {
  const td = el("td", "seller-col", r.sellerName ?? "Unknown");
  if (r.sellerName) td.title = r.sellerName;
  return td;
}

/** A photo in the viewer, and the group it belongs to (a sale's lots), for ← / →. */
type Photo = { src: string; caption: string };
let gallery: Photo[] = [];
let galleryIndex = 0;

/** A bigger view of a photo; ← / → (or ‹ ›) step through its group; Esc, × or a click outside closes it. */
function showPhoto(photos: Photo[], index: number) {
  gallery = photos;
  galleryIndex = index;
  const dialog = $<HTMLDialogElement>("#photo");
  const photo = photos[index];
  const img = dialog.querySelector("img")!;
  img.src = fullSizePhoto(photo.src);
  img.alt = photo.caption;
  dialog.querySelector(".photo-caption")!.textContent =
    photos.length > 1 ? `${photo.caption} · ${index + 1} / ${photos.length}` : photo.caption;
  dialog.querySelectorAll<HTMLButtonElement>(".photo-nav").forEach((b) => (b.hidden = photos.length < 2));
  if (!dialog.open) dialog.showModal();
}

function stepPhoto(delta: number) {
  if (gallery.length < 2) return;
  showPhoto(gallery, (galleryIndex + delta + gallery.length) % gallery.length);
}

/** The photos of a sale's lots (those that have one), in order. */
function lotPhotos(lots: Lot[]): Photo[] {
  return lots.filter((l) => l.imageUrl).map((l) => ({ src: l.imageUrl!, caption: `${l.position}. ${l.title}` }));
}

/** Makes a photo open in the viewer, at `index` within `photos`. */
function zoomable(img: HTMLImageElement, photos: Photo[], index: number) {
  img.classList.add("zoomable");
  img.tabIndex = 0;
  img.title = photos.length > 1 ? "Click for a bigger picture (← / → for the other lots)" : "Click for a bigger picture";
  img.addEventListener("click", (e) => {
    e.preventDefault(); // Inside a card's header (<summary>): show the photo, don't fold the card.
    showPhoto(photos, index);
  });
  img.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      showPhoto(photos, index);
    }
  });
}

/** "Reading…" / "Queued" while the reader has this post. */
function readState(postId: string): string | null {
  if (reader.current?.postId === postId || justClicked.has(postId)) return "Reading…";
  if (reader.queue.some((j) => j.postId === postId)) return "Queued";
  return null;
}

/** Lots · bids, and your status, from the latest post read; a nudge to read it otherwise. */
function statusCells(r: Row, now: Date): HTMLTableCellElement[] {
  const lotsTd = el("td", "lots");
  const youTd = el("td", "you");
  if (!r.summary) {
    lotsTd.append(el("span", "muted", "–"));
    youTd.append(readState(r.id) ? el("span", "muted small", "Reading…") : el("span", "muted", "–"));
    lotsTd.title = youTd.title = "Not read yet: click Read to read its lots and bids";
    lotsTd.classList.add("unread");
    youTd.classList.add("unread");
    return [lotsTd, youTd];
  }
  const s = r.summary;
  const isClaims = r.type === "claim" || r.type === "fixed";
  const count = isClaims ? `${s.claims} claim${s.claims === 1 ? "" : "s"}` : `${s.bids} bid${s.bids === 1 ? "" : "s"}`;
  lotsTd.append(el("div", undefined, `${s.lots} lot${s.lots === 1 ? "" : "s"} · ${count}`));
  const read = (r.lots ?? []).filter((l) => l.available !== null);
  if (isClaims && read.length) {
    const open = read.reduce((n, l) => n + (l.available ?? 0), 0);
    const note = read.length < (r.lots?.length ?? 0) ? ` (${read.length} of ${r.lots!.length} lots read)` : "";
    const avail = el("div", open ? "avail-sum" : "muted small", open ? `${open} card${open === 1 ? "" : "s"} available${note}` : `Sold out${note}`);
    avail.title = avail.textContent ?? "";
    lotsTd.append(avail);
  }
  if (s.unsure) lotsTd.append(el("div", "flag", `${s.unsure} unsure`));
  const counts = statusCounts(r);
  if (counts.length === 0) {
    youTd.append(el("span", "muted", "–"));
    youTd.title = isClaims ? "You didn't claim anything here" : "You didn't bid here";
  }
  for (const { status, count } of counts) {
    let text = `${status.label} ${count}`;
    if (isClaims && status.key === "won") {
      const t = wonTotal(r.lots ?? []); // Prices Claude read off the photos, when it has.
      if (t.cards) text = `Won ${t.cards} card${t.cards === 1 ? "" : "s"} · ${krText(t)}`;
    }
    const chip = el("span", `status ${status.cls}`, text);
    if (status.key === "unclear") chip.title = "A reply that may be a higher bid came after yours and couldn't be read yet";
    if (status.key === "check") chip.title = "Someone claimed the same before you: first come wins";
    if (status.key.endsWith("at-last-read")) chip.title = "Not read since the auction ended: open it to see the final result";
    youTd.append(chip);
  }
  const justRead = now.getTime() - Date.parse(r.lastReadAt!) < 60_000;
  youTd.append(justRead ? el("div", "badge just-read", "Just read") : el("div", "muted small", `Read ${ago(r.lastReadAt!, now)}`));
  // The latest read missed comments (merged with earlier ones, nothing lost), so say when the last full one was.
  if (r.lastCompleteReadAt !== r.lastReadAt) {
    const note = el("div", "flag", r.lastCompleteReadAt ? `partial read · last full ${ago(r.lastCompleteReadAt, now)}` : "partial read");
    note.title = "The latest read didn't load every comment. Earlier reads are kept, but new bids may be missing: open the post to read it again.";
    youTd.append(note);
  }
  return [lotsTd, youTd];
}

/** Expanded before there are lots to show: say why. */
function pendingLotsRow(r: Row, columns: number): HTMLTableRowElement {
  const tr = el("tr", "lots-row");
  const td = el("td", "muted small");
  td.colSpan = columns;
  td.textContent = readState(r.id)
    ? "Reading the post… its lots show up here when it's done."
    : r.lots
      ? "No lots found in this post (a lot is a comment with a photo from the seller)."
      : "Not read yet: click Read to read its lots and bids.";
  const toggle = endedToggle(r);
  if (toggle) td.append(" · ", toggle);
  tr.append(td);
  return tr;
}

/** The expanded lots: yours when you have bids, else all; image, highest bid, your bid, status. */
function lotsRow(r: Row, columns: number): HTMLTableRowElement {
  const tr = el("tr", "lots-row");
  const td = el("td");
  td.colSpan = columns;
  const mine = (r.lots ?? []).filter((l) => l.myStatus !== "none" || l.myClaim !== "none");
  // Claim sales: every lot (yours first), to see what's still for sale. Auctions: yours, or all.
  const lots: Lot[] =
    r.type === "claim" || r.type === "fixed"
      ? [...mine, ...(r.lots ?? []).filter((l) => !mine.includes(l))]
      : r.ended
        ? [...mine, ...(r.lots ?? []).filter((l) => !mine.includes(l))] // Ended: yours first, then every result.
        : mine.length
          ? mine
          : (r.lots ?? []);
  const list = el("div", "lot-list");
  const photos = lotPhotos(lots);
  for (const l of lots) {
    const isClaims = r.type === "claim" || r.type === "fixed";
    const status = lotStatus(r, l);
    const item = el("div", `lot lot-${status.cls}`);
    if (l.imageUrl) {
      const img = el("img", "lot-img");
      img.src = l.imageUrl;
      img.alt = "";
      img.loading = "lazy";
      img.referrerPolicy = "no-referrer";
      zoomable(img, photos, photos.findIndex((p) => p.src === l.imageUrl));
      item.append(img);
    }
    const body = el("div", "lot-body");
    body.append(line("div", lotLink(r, l, `${l.position}. ${l.title}`, "lot-title")));
    if (l.namedByClaude) body.append(el("div", "via", "Named by Claude from the photo"));
    body.append(el("div", "orig", l.rawText.split("\n").slice(1).join(" · ")));
    if (isClaims) {
      if (l.claimCards) {
        // Claude read the photo and the replies: every card, its price, taken or still for sale.
        const mine = l.claimCards.filter((x) => x.isMe);
        const total = mine.reduce((n, x) => n + (x.price ?? 0), 0);
        const open = l.available ?? 0;
        body.append(
          el("div", `availability ${open ? "open" : "sold-out"}`, open ? `${open} of ${l.claimCards.length} available` : "Sold out"),
        );
        if (mine.length) body.append(line("div", el("span", "status won", "You won"), " ", kr(total), mine.some((x) => x.price === null) ? " + ?" : ""));
        else if (l.myClaim !== "none") body.append(el("div", "status outbid", "Someone claimed it before you"));
        const cards = el("ul", "card-list");
        for (const x of l.claimCards) {
          const li = el("li", x.isMe ? "card mine" : x.claimedBy ? "card taken" : "card available");
          li.append(el("span", "card-name", x.card), " ", kr(x.price));
          li.append(" ", el("span", "card-state", x.isMe ? "yours" : x.claimedBy ? `taken · ${x.claimedBy}` : "available"));
          cards.append(li);
        }
        body.append(cards);
        body.append(el("div", "via", "Cards, prices and claims read by Claude from the photo"));
        item.append(body);
        list.append(item);
        continue;
      }
      if (l.myClaim !== "none") {
        body.append(el("div", `status ${l.myClaim === "claimed" ? "won" : "outbid"}`, l.myClaim === "claimed" ? "You claimed first: won (prices: waiting for Claude)" : "Someone claimed the same before you"));
      }
      if (l.claims.length === 0) body.append(el("div", "muted small", "No claims"));
      for (const c of l.claims) {
        const what = c.all ? "everything" : c.items.length ? c.items.join(", ") : "the lot";
        body.append(el("div", `small ${c.isMe ? "" : "muted"}`, `${c.isMe ? "You" : c.claimer}: ${what}${c.contested ? " (someone was earlier)" : ""}`));
      }
      item.append(body);
      list.append(item);
      continue;
    }
    if (r.ended) {
      // After the end, the highest bid is what it sold for; the start bid and raise no longer matter.
      body.append(
        l.highestBid === null
          ? el("div", "muted", "Unsold: no valid bids")
          : line("div", "Sold ", kr(l.highestBid), ` · ${l.highestBidder}`),
      );
      if (!readAfterEnd(r)) body.append(el("div", "flag", "at last read: bids may have come in after"));
      if (status.key !== "none") body.append(el("div", `status ${status.cls}`, `${status.label} · your bid ${l.myHighestBid} kr`));
      item.append(body);
      list.append(item);
      continue;
    }
    // The lot's start bid (minimum price) and minimum raise, from the lot's own text or the post's.
    const terms = line("div", ...(l.startBid !== null ? ["Start bid ", kr(l.startBid)] : ["Start bid not stated"]));
    terms.className = "lot-terms";
    if (l.increment !== null) terms.append(` · min. raise +${l.increment} kr`);
    body.append(terms);
    const hi =
      l.highestBid === null
        ? line("div", "No bids yet")
        : line("div", "Highest ", kr(l.highestBid), ` (${l.highestBidder})`, l.belowStart ? ` · below the start bid ${l.startBid} kr` : "");
    if (l.belowStart) hi.className = "flag";
    body.append(hi);
    if (status.key !== "none") body.append(el("div", `status ${status.cls}`, `${status.label} · your bid ${l.myHighestBid} kr`));
    // Details only where they matter to you: your own bids' notes and Claude's readings.
    for (const b of l.bids.filter((x) => (x.isMe && x.note && !x.valid) || x.viaClaude)) {
      body.append(el("div", "small muted", `${b.isMe ? "You" : b.bidder}: "${b.rawText}" → ${b.amount ?? "?"}${b.viaClaude ? " (read by Claude)" : ""}${b.note ? ` · ${b.note}` : ""}`));
    }
    const notCounted = l.bids.filter((x) => !x.valid).length;
    if (notCounted) {
      const why = [...new Set(l.bids.filter((x) => !x.valid).map((x) => (x.underReply ? "under another reply" : "too low")))].join(", ");
      body.append(el("div", "small muted", `${notCounted} bid${notCounted === 1 ? "" : "s"} not counted (${why})`));
    }
    if (l.unsureCount) body.append(el("div", "flag", `${l.unsureCount} repl${l.unsureCount === 1 ? "y" : "ies"} that may be bids couldn't be read yet`));
    item.append(body);
    list.append(item);
  }
  td.append(list);
  const toggle = endedToggle(r);
  if (toggle) td.append(line("div", toggle));
  tr.append(td);
  return tr;
}

/** Your lots' statuses in a sale, counted by label ("Leading 2", "Outbid 1"), in a sensible order. */
function statusCounts(r: Row): { status: LotStatus; count: number }[] {
  const order: LotStatus["key"][] = ["outbid", "unclear", "check", "outbid-at-last-read", "lost", "leading", "leading-at-last-read", "won"];
  const byKey = new Map<LotStatus["key"], { status: LotStatus; count: number }>();
  for (const l of r.lots ?? []) {
    const st = lotStatus(r, l);
    if (st.key === "none") continue;
    const entry = byKey.get(st.key) ?? { status: st, count: 0 };
    entry.count++;
    byKey.set(st.key, entry);
  }
  return order.flatMap((k) => byKey.get(k) ?? []);
}

/** A sale's edge colour: orange if anything needs you, green if all won, blue if leading. */
function rowTone(r: Row): "outbid" | "won" | "lead" | "lost" | null {
  const keys = statusCounts(r).map((c) => c.status.key);
  if (keys.length === 0) return null;
  // Something still needs you (outbid, unclear, someone earlier on a claim): orange.
  if (keys.some((k) => k === "outbid" || k === "unclear" || k === "check" || k === "outbid-at-last-read")) return "outbid";
  if (keys.some((k) => k === "leading" || k === "leading-at-last-read")) return "lead";
  // Finished: green if you won anything (a lost lot beside it doesn't make it a failure), else grey.
  return keys.includes("won") ? "won" : "lost";
}
const isDone = (g: WonSeller) => g.rows.every((r) => wonState[r.id]?.paidAt && wonState[r.id]?.receivedAt);
const isPaid = (g: WonSeller) => g.rows.every((r) => wonState[r.id]?.paidAt);
const isReceived = (g: WonSeller) => g.rows.every((r) => wonState[r.id]?.receivedAt);

/** "400 kr", "400 kr + ?" (a price not known yet). */
const sellerTotal = (g: { kr: number; unknown: number }) => `${g.kr} kr${g.unknown ? " + ?" : ""}`;

/** A small lot photo that opens the viewer (← / → through `photos`). */
function lotThumb(l: Lot, photos: Photo[]): HTMLElement {
  if (!l.imageUrl) return el("span", "line-thumb empty");
  const img = el("img", "line-thumb");
  img.src = l.imageUrl;
  img.alt = "";
  img.loading = "lazy";
  img.referrerPolicy = "no-referrer";
  zoomable(img, photos, photos.findIndex((p) => p.src === l.imageUrl));
  return img;
}

/** A live countdown (ticks with the others; red under an hour). */
function countdownEl(r: Row, now: Date): HTMLElement {
  if (r.endedByYouAt) return el("span", "countdown", "Ended");
  if (r.endsAtMs === null) return el("span", "countdown unknown", r.type === "fixed" ? "No end" : "?");
  const cd = el("span", "countdown", r.maybeEnded ? "Ended?" : r.ended ? "Ended" : countdown(r.endsAtMs, now));
  if (!r.ended) cd.dataset.ends = String(r.endsAtMs);
  if (!r.ended && r.endsAtMs - now.getTime() < 3_600_000) cd.classList.add("soon");
  cd.title = endLabel(r.endsAtMs, now);
  return cd;
}

/** One lot that needs you, on one line: photo · name · sale · countdown · price · what to do. */
function needsYouLine(item: NeedsYouItem, photos: Photo[], now: Date): HTMLLIElement {
  const { row: r, lot: l, status } = item;
  const li = el("li", `lot-line ${status.key}`);
  li.append(lotThumb(l, photos));
  const name = lotLink(r, l, l.title, "line-name");
  const sale = el("span", "line-sale", `${saleTitle(r)} · ${r.sellerName ?? ""}`);
  sale.title = sale.textContent ?? "";
  li.append(line("span", name, sale), countdownEl(r, now));
  if (status.key === "check") {
    li.append(el("span", "line-price", "Someone claimed it first"));
  } else {
    const price = line("span", kr(l.highestBid), ` (you ${l.myHighestBid ?? "?"})`);
    price.className = "line-price";
    if (status.key === "unclear") price.append(" · a reply couldn't be read");
    li.append(price);
  }
  // The lowest bid that counts now; you bid on Facebook (the lot's name links there). The extension never bids.
  const actions = el("span", "line-actions");
  if (item.nextBid !== null) {
    const next = line("span", "next ", kr(item.nextBid), "+");
    next.className = "line-next";
    next.title = `The lowest bid that counts now is ${item.nextBid} kr. Click the lot's name to bid on Facebook.`;
    actions.append(next);
  }
  const toggle = endedToggle(r, "Sale ended");
  if (toggle) actions.append(toggle);
  li.append(actions);
  return li;
}

/** A lot's name as a link to its own comment on Facebook (the post, when the lot has no comment ID). */
function lotLink(r: Pick<Row, "url">, l: Lot, text: string, cls: string): HTMLAnchorElement {
  const a = el("a", `lot-link ${cls}`, text);
  a.href = lotUrl(r, l);
  a.target = "_blank";
  a.rel = "noopener";
  a.title = (l.commentId ? "Open this lot on Facebook" : "Open the post on Facebook (this lot has no link of its own)") + (l.namedByClaude ? " · named by Claude from the photo" : "");
  return a;
}

/** A lot you're leading, inside its sale. */
function leadingLine(r: Row, l: Lot, photos: Photo[]): HTMLLIElement {
  const li = el("li", "lot-line lead");
  li.append(lotThumb(l, photos), lotLink(r, l, l.title, "line-name"), line("span", "Your bid ", kr(l.myHighestBid)));
  return li;
}

/** One seller you owe: a folded line (total, how to pay, Paid / Received); unfolds to the items and terms. */
function toPayCard(g: WonSeller, now: Date): HTMLDetailsElement {
  const card = foldable(`pay:${g.seller}`, `pay-card${isDone(g) ? " done" : ""}`);
  const head = el("summary", "pay-head");
  const pay = [...new Set(g.rows.map((r) => r.paymentText).filter(Boolean))].join(" / ");
  head.append(
    el("strong", "pay-seller", g.seller),
    el("span", "muted", `${g.items.length} ${g.items.length === 1 ? "lot" : "lots"}`),
    line("span", kr(g.kr), g.unknown ? " + ?" : "", " + shipping"),
  );
  if (pay) head.append(el("span", "muted small pay-how", `Pay: ${pay}`));
  // Paid / Received: your own marks, for all of this seller's sales listed here.
  for (const [field, label, done] of [
    ["paidAt", "Paid", isPaid(g)],
    ["receivedAt", "Received", isReceived(g)],
  ] as const) {
    const box = el("label", "won-mark");
    const input = el("input");
    input.type = "checkbox";
    input.checked = done;
    input.addEventListener("change", () => {
      void markWon(
        g.rows.map((r) => r.id),
        { [field]: input.checked ? new Date().toISOString() : null },
      );
    });
    const when = done ? g.rows.map((r) => wonState[r.id]?.[field]).filter(Boolean).sort().at(-1) : null;
    box.append(input, ` ${label}`);
    if (when) box.title = `${label} ${ago(when, now)}`;
    head.append(box);
  }
  card.append(head);

  const items = el("ul", "pay-items");
  const photos = lotPhotos(g.items.map((i) => i.lot));
  for (const item of g.items) {
    const li = el("li", "lot-line");
    li.append(lotThumb(item.lot, photos), lotLink(item.row, item.lot, item.label, "line-name"), item.kr !== null ? kr(item.kr) : el("span", "flag", "price not read yet"));
    items.append(li);
  }
  card.append(items);
  for (const r of g.rows) {
    const terms = el("div", "won-terms");
    const link = el("a", "open-link", `${r.title} ↗`);
    link.href = r.url;
    link.target = "_blank";
    link.rel = "noopener";
    terms.append(link);
    if (r.shippingText) terms.append(" · ", el("span", undefined, `Shipping: ${r.shippingText}`));
    if (r.paymentText) terms.append(" · ", el("span", undefined, `Pay: ${r.paymentText}`));
    if (r.endedByYouAt) {
      terms.append(" · ", el("span", "muted", `marked ended by you ${ago(r.endedByYouAt, now)}`), " ");
      terms.append(endedToggle(r)!);
    } else terms.append(" · ", el("span", "muted", r.endsAtMs !== null ? `ended ${endLabel(r.endsAtMs, now)}` : "fixed price"));
    card.append(terms);
  }
  return card;
}

/** How many "Needs you" lines show before "Show all". */
const NEEDS_YOU_SHOWN = 5;
let needsShowAll = false;

/**
 * My Auctions, in order of urgency: Needs you (act now, across sales, soonest first) · Leading
 * (one folded line per sale, what it costs if it holds) · To pay (one folded line per seller).
 * Lost lots and finished sales live in the table below.
 */
function renderMine(now: Date) {
  const needs = needsYou(rows);
  const leading = leadingBySale(rows);
  const groups = wonBySeller(rows);
  const toPay = groups.filter((g) => !isPaid(g));
  const leadingLots = leading.reduce((n, s) => n + s.lots.length, 0);
  const leadingKr = leading.reduce((n, s) => n + s.kr, 0);
  const owe = { kr: toPay.reduce((n, g) => n + g.kr, 0), unknown: toPay.reduce((n, g) => n + g.unknown, 0) };

  // The whole picture in one sentence.
  const parts: string[] = [];
  if (needs.length) parts.push(`${needs.length} need${needs.length === 1 ? "s" : ""} you`);
  if (leadingLots) parts.push(`${leadingLots} leading (${leadingKr} kr if they hold)`);
  if (toPay.length) parts.push(`${sellerTotal(owe)} to pay`);
  $("#mine-summary").textContent = parts.join(" · ") || "Nothing open. Click a sale's title below to read its bids.";

  // Needs you.
  $("#needs-count").textContent = String(needs.length);
  const shown = needsShowAll ? needs : needs.slice(0, NEEDS_YOU_SHOWN);
  const photos = lotPhotos(shown.map((i) => i.lot));
  $("#needs-list").replaceChildren(...shown.map((i) => needsYouLine(i, photos, now)));
  $<HTMLElement>("#needs-empty").hidden = needs.length > 0;
  const more = $<HTMLButtonElement>("#needs-more");
  more.hidden = needs.length <= NEEDS_YOU_SHOWN;
  more.textContent = needsShowAll ? "Show fewer" : `Show all ${needs.length}`;

  // Leading: one folded line per sale.
  $<HTMLElement>("#leading").hidden = leading.length === 0;
  $("#leading-sum").textContent = leading.length
    ? `${leadingLots} lot${leadingLots === 1 ? "" : "s"} in ${leading.length} sale${leading.length === 1 ? "" : "s"} · ${leadingKr} kr if they hold`
    : "";
  $("#leading-list").replaceChildren(
    ...leading.map((s) => {
      const d = foldable(`lead:${s.row.id}`, "lead-card");
      const head = el("summary", "lead-head");
      head.append(countdownEl(s.row, now), titleLine(s.row), el("span", "muted small", s.row.sellerName ?? ""), saleActions(s.row));
      head.append(line("span", `${s.lots.length} lot${s.lots.length === 1 ? "" : "s"} · `, kr(s.kr)));
      if (s.awaitingFinalRead) {
        // The worker reads it just after the close while auto-scan is on (src/background/watch.ts).
        const flag = el("span", "flag", settings.autoScan ? "ended: final read coming" : "ended: open it to see the result");
        flag.title = settings.autoScan
          ? "It's read once more in the background just after it closes, and Leading becomes Won or Lost."
          : "Auto-scan is off, so nothing reads it in the background: click Read to read it now.";
        head.append(flag);
      }
      const toggle = endedToggle(s.row);
      if (toggle) head.append(toggle);
      d.append(head);
      const lotPics = lotPhotos(s.lots);
      const list = el("ul", "lead-lots");
      list.append(...s.lots.map((l) => leadingLine(s.row, l, lotPics)));
      d.append(list);
      return d;
    }),
  );

  // To pay: one folded line per seller; paid and received ones fold away entirely.
  const open = groups.filter((g) => !isDone(g));
  const done = groups.length - open.length;
  $<HTMLElement>("#topay").hidden = groups.length === 0;
  $("#topay-sum").textContent = groups.length
    ? toPay.length
      ? `${toPay.length} seller${toPay.length === 1 ? "" : "s"} · ${sellerTotal(owe)}`
      : "all paid"
    : "";
  const toggle = $<HTMLInputElement>("#show-done");
  toggle.checked = showDone;
  $<HTMLElement>("#show-done-label").hidden = done === 0;
  $("#show-done-count").textContent = String(done);
  $("#topay-list").replaceChildren(...(showDone ? groups : open).map((g) => toPayCard(g, now)));
  $("#count-won").textContent = String(groups.reduce((n, g) => n + g.items.length, 0));
  $("#count-needs").textContent = String(needs.length);
  renderSendWins(now);
}

/** "Send wins to inventory" (#309): the button and the last send's result. */
function renderSendWins(now: Date) {
  $<HTMLButtonElement>("#send-wins").disabled = sendingWins;
  const status = $<HTMLElement>("#send-wins-status");
  status.classList.toggle("error", inbox.ok === false && !sendingWins);
  if (sendingWins) status.textContent = "Sending…";
  else if (inbox.lastAt && inbox.outcome) {
    const what = inbox.count ? `${inbox.count} win${inbox.count === 1 ? "" : "s"} · ` : "";
    status.textContent = `${inbox.ok ? "Sent" : "Failed"} ${ago(inbox.lastAt, now)}: ${what}${inbox.outcome}`;
  } else status.textContent = inboxOrigin(settings.inboxUrl) ? "Not sent yet." : "Set up the address and token in Settings.";
}

function renderSettings(now: Date) {
  const auto = $<HTMLInputElement>("#auto-scan");
  auto.checked = settings.autoScan;
  const parts: string[] = [];
  if (!settings.autoScan) parts.push("Off. Needs a pinned tab with the group's feed.");
  else if (autoScan.nextAt) parts.push(`Next in ${countdown(Date.parse(autoScan.nextAt), now)}`);
  if (autoScan.lastAt && autoScan.lastOutcome) parts.push(`Last ${ago(autoScan.lastAt, now)}: ${autoScan.lastOutcome}`);
  $("#auto-scan-status").textContent = parts.join(" · ");

  const useClaude = $<HTMLInputElement>("#use-claude");
  useClaude.checked = settings.useClaude;
  $("#claude-status").textContent = !settings.useClaude
    ? "Off: what the rules can't read stays unsure."
    : claude.lastAt && claude.lastOutcome
      ? `Last ${ago(claude.lastAt, now)}: ${claude.lastOutcome}` +
        // The hourly cap on photo calls (src/background/claudeQueue.ts): when the rest goes on.
        (claude.photoLimitUntil && Date.parse(claude.photoLimitUntil) > now.getTime()
          ? ` · more photos in ${countdown(Date.parse(claude.photoLimitUntil), now)}`
          : "")
      : "Asks Claude Code (claude -p, your login) only about what the rules can't read.";

  $("#reader-status").textContent =
    (reader.current ? "Reading a post now. " : "") +
    (reader.queue.length ? `${reader.queue.length} queued. ` : "") +
    (reader.lastAt && reader.lastOutcome ? `Last ${ago(reader.lastAt, now)}: ${reader.lastOutcome}` : "No background reads yet.");

  $<HTMLInputElement>("#notify").checked = settings.notify;

  const name = $<HTMLInputElement>("#my-name");
  if (document.activeElement !== name) name.value = settings.myName;

  // Only fill these when nobody is typing in the box, so an edit in progress isn't overwritten.
  const inboxBox = $<HTMLElement>("#inbox-url").closest(".setting");
  if (!inboxBox?.contains(document.activeElement)) {
    $<HTMLInputElement>("#inbox-url").value = settings.inboxUrl;
    $<HTMLInputElement>("#inbox-token").value = settings.inboxToken;
  }

  $("#cleanup-status").textContent =
    cleanup.lastAt && cleanup.lastOutcome ? `Last cleanup ${ago(cleanup.lastAt, now)}: ${cleanup.lastOutcome}` : "No cleanup has run yet.";
}

function render() {
  const now = new Date();
  const visible = rows.filter(matches);
  const counts = countRows(rows, now);
  $("#count-active").textContent = String(counts.active);
  $("#count-hour").textContent = String(counts.withinHour);
  $("#count-new").textContent = String(counts.isNew);
  $("#last-read").textContent = lastFeedReadAt ? `Feed last read ${ago(lastFeedReadAt, now)}` : "Feed not read yet";
  document.querySelectorAll<HTMLButtonElement>("[data-filter]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.filter === filter)));
  renderSettings(now);
  renderMine(now);

  const main = $<HTMLElement>("#groups");
  main.replaceChildren();
  if (rows.length === 0) {
    main.append(el("p", "empty", "No sales yet. Open the group's feed on Facebook and click the extension icon to scan it."));
    return;
  }
  // Tabs, with how many sales each holds under the current filter and search.
  const all = tabs(visible, now);
  const bar = $("#tabs");
  bar.replaceChildren(
    ...all.map((x) => {
      const b = el("button", "tab");
      b.type = "button";
      b.id = `tab-${x.id}`;
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", String(x.id === tab));
      b.setAttribute("aria-controls", "groups");
      b.tabIndex = x.id === tab ? 0 : -1;
      b.dataset.tab = x.id;
      b.append(x.label, " ", el("span", "group-count", String(x.count)));
      return b;
    }),
  );
  main.setAttribute("aria-labelledby", `tab-${tab}`);
  const current = all.find((x) => x.id === tab)!;
  main.append(el("p", "table-hint", "Click a sale's name to open it on Facebook · Read reads its lots and bids now · click a row to show its lots"));
  if (current.count === 0) {
    main.append(el("p", "empty", query || filter !== "all" ? "Nothing here matches the filter." : EMPTY_TAB[tab]));
    return;
  }
  const headers = ["Ends", "Added", "Sale", "Seller", "Price", "Lots", "You"];
  const table = el("table");
  const head = el("tr");
  // Ended sales show their result where running ones show their price.
  const allEnded = current.sections.every((x) => x.rows.every((r) => r.ended));
  const someEnded = current.sections.some((x) => x.rows.some((r) => r.ended)); // e.g. just ended, still in Today (#320).
  for (const h of headers) head.append(el("th", undefined, h === "Price" ? (allEnded ? "Result" : tab === "mine" || someEnded ? "Price / result" : h) : h));
  table.append(el("thead"), el("tbody"));
  table.tHead!.append(head);
  const body = table.tBodies[0];
  for (const section of current.sections) {
    // Sections only when the tab has more than one ("Yours" / "Everyone else").
    if (current.sections.length > 1 && section.label) {
      const sub = el("tr", "subhead");
      const td = el("td", undefined, `${section.label} · ${section.rows.length}`);
      td.colSpan = headers.length;
      sub.append(td);
      body.append(sub);
    }
    for (const r of section.rows) {
      const tr = el("tr");
      const tone = rowTone(r);
      if (tone) tr.classList.add(`mine-${tone}`);
      // Ended under 30 min ago, still in an active tab (#320): shown as ended, dimmed.
      if (r.justEnded && tab !== "ended") {
        tr.classList.add("just-ended");
        tr.title = "Ended: it stays in this tab for 30 minutes after the end, then only under Ended.";
      }
      tr.append(endsCell(r, now), addedCell(r, now), saleCell(r), sellerCell(r), r.ended ? resultCell(r, now) : priceCell(r), ...statusCells(r, now));
      makeExpandable(tr, r.id);
      body.append(tr);
      if (expanded.has(r.id)) body.append(r.lots?.length ? lotsRow(r, headers.length) : pendingLotsRow(r, headers.length));
    }
  }
  main.append(table);
}

const EMPTY_TAB: Record<TabId, string> = {
  new: "Nothing new since your last visit or in the last 30 minutes.",
  today: "Nothing ends today.",
  upcoming: "Nothing ends after today yet.",
  noend: "Every sale has an end time.",
  mine: "You haven't bid or claimed in any sale the overview has read.",
  ended: "No ended sales stored.",
};

/** Countdowns tick every second; a full re-render (regrouping) every 30 s. */
function tick() {
  const now = new Date();
  document.querySelectorAll<HTMLElement>(".countdown[data-ends]").forEach((c) => {
    const ends = Number(c.dataset.ends);
    if (c.textContent !== "Ended?") c.textContent = countdown(ends, now);
    c.classList.toggle("soon", ends - now.getTime() < 3_600_000);
  });
}

{
  const dialog = $<HTMLDialogElement>("#photo");
  // ‹ › step through the sale's lots; any other click (backdrop, photo, ×) closes; Esc is built in.
  dialog.addEventListener("click", (e) => {
    const nav = (e.target as Element).closest<HTMLElement>(".photo-nav");
    if (nav) stepPhoto(nav.dataset.step === "prev" ? -1 : 1);
    else dialog.close();
  });
  dialog.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      stepPhoto(-1);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      stepPhoto(1);
    }
  });
}
document.querySelectorAll<HTMLButtonElement>("[data-filter]").forEach((b) =>
  b.addEventListener("click", () => {
    filter = b.dataset.filter as Filter;
    local.set("fbaw-filter", filter);
    render();
  }),
);
// Tabs: click, or ← / → between them (the ARIA tabs pattern).
$("#tabs").addEventListener("click", (e) => {
  const b = (e.target as Element).closest<HTMLButtonElement>("[data-tab]");
  if (!b) return;
  tab = b.dataset.tab as TabId;
  local.set("fbaw-tab", tab);
  render();
});
$<HTMLElement>("#tabs").addEventListener("keydown", (e) => {
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  const i = TAB_IDS.indexOf(tab) + (e.key === "ArrowRight" ? 1 : -1);
  tab = TAB_IDS[(i + TAB_IDS.length) % TAB_IDS.length];
  local.set("fbaw-tab", tab);
  render();
  $<HTMLButtonElement>(`#tab-${tab}`).focus();
});
$<HTMLInputElement>("#search").addEventListener("input", (e) => {
  query = (e.target as HTMLInputElement).value.trim();
  render();
});
$("#needs-more").addEventListener("click", () => {
  needsShowAll = !needsShowAll;
  render();
});
$<HTMLInputElement>("#show-done").addEventListener("change", (e) => {
  showDone = (e.target as HTMLInputElement).checked;
  local.set("fbaw-show-done", showDone ? "1" : "0");
  render();
});
$<HTMLInputElement>("#auto-scan").addEventListener("change", (e) => {
  void updateSettings({ autoScan: (e.target as HTMLInputElement).checked });
});
$<HTMLInputElement>("#notify").addEventListener("change", (e) => {
  void updateSettings({ notify: (e.target as HTMLInputElement).checked });
});
$<HTMLInputElement>("#use-claude").addEventListener("change", (e) => {
  void updateSettings({ useClaude: (e.target as HTMLInputElement).checked });
});
$<HTMLInputElement>("#my-name").addEventListener("change", (e) => {
  const name = (e.target as HTMLInputElement).value.trim();
  if (name) void updateSettings({ myName: name });
});
// tcg_inventory's address and token (#309). Saving asks Chrome for permission to reach that
// one address (an optional host permission, so the extension holds none until you set it up);
// the request has to come straight from the click, before anything is awaited.
$<HTMLButtonElement>("#inbox-save").addEventListener("click", () => {
  const status = $<HTMLElement>("#inbox-save-status");
  const raw = $<HTMLInputElement>("#inbox-url").value.trim();
  const token = $<HTMLInputElement>("#inbox-token").value.trim();
  if (!raw) {
    void updateSettings({ inboxUrl: "", inboxToken: token }).then(() => (status.textContent = "Saved (no address: sending is off)."));
    return;
  }
  const origin = inboxOrigin(raw);
  if (!origin) {
    status.textContent = "Use an https:// address (or http://localhost for a local tcg_inventory).";
    return;
  }
  void chrome.permissions.request({ origins: [`${origin}/*`] }).then(async (granted) => {
    if (!granted) {
      status.textContent = `Not saved: Chrome's permission to reach ${origin} wasn't granted.`;
      return;
    }
    await updateSettings({ inboxUrl: origin, inboxToken: token });
    status.textContent = token ? `Saved: ${origin}` : `Saved: ${origin} (no token yet)`;
  }, (err: unknown) => {
    status.textContent = `Not saved: ${err instanceof Error ? err.message : String(err)}`;
  });
});
$<HTMLButtonElement>("#send-wins").addEventListener("click", () => {
  sendingWins = true;
  renderSendWins(new Date());
  chrome.runtime
    .sendMessage({ type: MSG_SEND_WINS })
    .then((state: InboxState | undefined) => {
      if (state) inbox = state;
    })
    .catch((err: unknown) => {
      inbox = { lastAt: new Date().toISOString(), ok: false, count: null, outcome: err instanceof Error ? err.message : String(err) };
    })
    .finally(() => {
      sendingWins = false;
      renderSendWins(new Date());
    });
});
// "Clear stored data" (review M6): asks first, inline, then empties the store (posts, post
// reads, Claude's answers, meta) and shows the empty overview. Settings are in chrome.storage
// and stay. A scan or read still running will store what it finds after this, as usual.
{
  const start = $<HTMLButtonElement>("#clear-data");
  const confirmBox = $<HTMLElement>("#clear-confirm");
  const done = $<HTMLElement>("#clear-done");
  const ask = (open: boolean) => {
    confirmBox.hidden = !open;
    start.hidden = open;
    if (open) $<HTMLButtonElement>("#clear-no").focus();
  };
  start.addEventListener("click", () => {
    done.textContent = "";
    ask(true);
  });
  $<HTMLButtonElement>("#clear-no").addEventListener("click", () => {
    ask(false);
    start.focus();
  });
  $<HTMLButtonElement>("#clear-yes").addEventListener("click", async () => {
    ask(false);
    try {
      await store.clearAll();
      expanded.clear();
      done.textContent = "Cleared.";
    } catch (err) {
      done.textContent = `Couldn't clear: ${err instanceof Error ? err.message : String(err)}`;
    }
    await load();
  });
}
// Settings: a dialog from the top-right button. Esc, ×, or a click outside closes it.
const settingsDialog = $<HTMLDialogElement>("#settings");
$("#open-settings").addEventListener("click", () => settingsDialog.showModal());
settingsDialog.querySelector(".settings-close")!.addEventListener("click", () => settingsDialog.close());
settingsDialog.addEventListener("click", (e) => {
  // Only a click outside the box (on the backdrop); the dialog's own padding also targets it.
  const box = settingsDialog.getBoundingClientRect();
  const inside = e.clientX >= box.left && e.clientX <= box.right && e.clientY >= box.top && e.clientY <= box.bottom;
  if (e.target === settingsDialog && !inside) settingsDialog.close();
});
chrome.runtime.onMessage.addListener((msg) => {
  if (isStoreUpdatedMessage(msg)) void load();
});
// Settings and scan/Claude status live in chrome.storage; re-read when they change.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  // Your name changes how every lot reads: rebuild. Status ticks (scan, reader, Claude) only
  // need their own small state, not a reload of every post from the database (review M6).
  if ("settings" in changes) {
    void load();
  } else if ("endedMarks" in changes) {
    // Marking a sale ended changes its row and its lots' statuses: rebuild from what's loaded.
    void loadStatus().then(() => {
      rebuild();
      render();
    });
  } else if (["autoScanState", "claudeState", "readerState", "cleanupState", "wonState", "inboxState"].some((k) => k in changes)) {
    void loadStatus().then(render);
  }
});
setInterval(tick, 1000);
setInterval(() => {
  rebuild();
  render();
}, 30_000);
void load();
