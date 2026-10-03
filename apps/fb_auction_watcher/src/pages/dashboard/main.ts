import { fullSizePhoto, type Lot } from "../../domain/bids";
import type { PostCapture } from "../../shared/capture";
import { isStoreUpdatedMessage, MSG_QUEUE_READ, type QueueReadMessage } from "../../shared/messages";
import type { ReaderState } from "../../background/reader";
import {
  getAutoScanState,
  getClaudeState,
  getSettings,
  updateSettings,
  type AutoScanState,
  type ClaudeState,
  type Settings,
} from "../../shared/settings";
import { idbStore } from "../../store";
import { ago, buildRows, countdown, countRows, endLabel, groupRows, type Row } from "./model";

// The overview: every sale read from the feed, grouped and sorted by end time, with live
// countdowns, and for posts you've read with the icon: lots, bids and your Leading/Outbid
// status. Reads the store directly (same extension origin as the service worker) and re-reads
// when the worker says something changed. The seller's original text is always shown next to
// interpreted values (docs/spec.md rule), and anything read by Claude is marked as such.

const store = idbStore();
type Filter = "all" | "auction" | "claim" | "fixed" | "mine";

const $ = <T extends Element>(sel: string) => document.querySelector(sel) as T;
const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

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

const previousVisit = local.get("fbaw-last-visit");
const lastVisit = previousVisit ? new Date(previousVisit) : null;
local.set("fbaw-last-visit", new Date().toISOString());

let rows: Row[] = [];
let filter: Filter = (local.get("fbaw-filter") as Filter | null) ?? "all";
let query = "";
let lastFeedReadAt: string | null = null;
let settings: Settings;
let autoScan: AutoScanState;
let claude: ClaudeState;
let reader: ReaderState = { visible: {}, queue: [], current: null, lastAt: null, lastOutcome: null };
const expanded = new Set<string>();
const justClicked = new Set<string>();
let source: { posts: Awaited<ReturnType<typeof store.allPosts>>; captures: Map<string, PostCapture>; answers: Map<string, unknown> } = {
  posts: [],
  captures: new Map(),
  answers: new Map(),
};

function rebuild() {
  rows = buildRows(source.posts, new Date(), lastVisit, { ...source, myName: settings.myName });
}

async function load() {
  const [posts, captures, answers, readAt] = await Promise.all([
    store.allPosts(),
    store.allCaptures(),
    store.allAnswers(),
    store.getMeta("lastFeedReadAt"),
  ]);
  [settings, autoScan, claude] = await Promise.all([getSettings(), getAutoScanState(), getClaudeState()]);
  reader = { ...reader, ...((await chrome.storage.local.get("readerState")).readerState as Partial<ReaderState> | undefined) };
  for (const id of justClicked) if (visiblePostIds().includes(id)) justClicked.delete(id);
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
  if (filter === "mine") {
    if (!r.summary || r.summary.lead + r.summary.outbid + r.summary.claimed + r.summary.check === 0) return false;
  } else if (filter !== "all" && r.type !== filter) return false;
  if (!query) return true;
  const q = query.toLowerCase();
  return [r.title, r.sellerName ?? "", r.text, r.description ?? ""].some((s) => s.toLowerCase().includes(q));
}

function priceText(r: Row): string {
  if (r.type === "auction") {
    const min = r.minPrice !== null ? `Min ${r.minPrice} kr` : "Min per lot";
    const inc = r.increment !== null ? ` · +${r.increment}` : "";
    return min + inc;
  }
  return r.fixedPrice !== null ? `${r.fixedPrice} kr` : "Price per item";
}

const TYPE_LABEL: Record<Row["type"], string> = {
  auction: "Auction", claim: "Claim", fixed: "Fixed price", wanted: "Wanted", trade: "Trade", other: "Other",
};

function endsCell(r: Row, now: Date): HTMLTableCellElement {
  const td = el("td", "ends");
  if (r.endsAtMs !== null) {
    const cd = el("div", "countdown", r.maybeEnded ? "Ended?" : countdown(r.endsAtMs, now));
    cd.dataset.ends = String(r.endsAtMs);
    if (!r.ended && r.endsAtMs - now.getTime() < 3_600_000) cd.classList.add("soon");
    td.append(cd, el("div", "when", endLabel(r.endsAtMs, now) + (r.softCloseMinutes ? ` · +${r.softCloseMinutes} min antisnipe` : "")));
  } else if (r.type === "fixed") {
    td.append(el("div", "when", "No end time"));
  } else {
    td.append(el("div", "countdown unknown", "Unknown"));
  }
  if (r.endsAtText) {
    const orig = el("div", "orig", r.endsAtText);
    orig.title = "The seller's original text";
    td.append(orig);
  }
  if (r.endsViaClaude) td.append(el("div", "via", "End time read by Claude"));
  else if (r.endsAt && !r.sure) td.append(el("div", "flag", "? check the original text"));
  if (!r.textComplete && !r.endsAtText && r.type !== "fixed") td.append(el("div", "flag", "Text was cut off"));
  return td;
}

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
    zoomable(img, r.title);
    wrap.append(img);
  }
  const box = el("div", "sale-text");
  const a = el("a", "title", r.title);
  a.href = r.url;
  a.target = "_blank";
  a.rel = "noopener";
  a.title = "Open the post and read its bids quietly (Ctrl/Cmd-click: just open it)";
  // A plain click opens the post and reads it silently in that tab; the row updates when done.
  a.addEventListener("click", (e) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    const msg: QueueReadMessage = { type: MSG_QUEUE_READ, postId: r.id, url: r.url };
    justClicked.add(r.id); // Shows "Reading…" until the worker's state catches up.
    setTimeout(() => {
      justClicked.delete(r.id); // The worker never picked it up: don't show "Reading…" forever.
      render();
    }, 60_000);
    render();
    void chrome.runtime.sendMessage(msg).catch(() => {});
  });
  const titleLine = el("div", "title-line");
  titleLine.append(a);
  const state = readState(r.id);
  if (state) titleLine.append(" ", el("span", "badge reading", state));
  box.append(titleLine);
  if (r.description && r.description !== r.title) box.append(el("div", "desc", r.description));
  const meta = el("div", "seller", r.sellerName ?? "Unknown seller");
  if (r.isNew) meta.append(" ", el("span", "badge new", "New"));
  box.append(meta);
  wrap.append(box);
  return td;
}

/** Posts being read in a tab you opened, leaving out ones the worker will time out anyway. */
function visiblePostIds(): string[] {
  return Object.values(reader.visible ?? {})
    .filter((v) => typeof v === "object" && Date.now() - Date.parse(v.startedAt) < 5 * 60_000)
    .map((v) => v.postId);
}

/** A bigger view of a photo: click a thumbnail or a lot image; Esc, click or × closes it. */
function showPhoto(src: string, caption: string) {
  const dialog = $<HTMLDialogElement>("#photo");
  const img = dialog.querySelector("img")!;
  img.src = src;
  img.alt = caption;
  dialog.querySelector(".photo-caption")!.textContent = caption;
  dialog.showModal();
}

function zoomable(img: HTMLImageElement, caption: string) {
  img.classList.add("zoomable");
  img.tabIndex = 0;
  img.title = "Click for a bigger picture";
  img.addEventListener("click", () => showPhoto(fullSizePhoto(img.src), caption));
  img.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      showPhoto(fullSizePhoto(img.src), caption);
    }
  });
}

/** "Reading…" / "Queued" while the reader has this post. */
function readState(postId: string): string | null {
  if (reader.current?.postId === postId || visiblePostIds().includes(postId) || justClicked.has(postId)) return "Reading…";
  if (reader.queue.some((j) => j.postId === postId)) return "Queued";
  return null;
}

/** Lots · bids, and your status, from the latest post read; a nudge to read it otherwise. */
function statusCells(r: Row, now: Date): HTMLTableCellElement[] {
  const lotsTd = el("td", "lots");
  const youTd = el("td", "you");
  if (!r.summary) {
    lotsTd.append(el("span", "muted", "–"));
    if (r.type === "auction" && !r.ended) {
      const hint = el("span", "muted small", "Click the title to read bids");
      if (readState(r.id)) hint.textContent = "Reading in the background…";
      youTd.append(hint);
    }
    return [lotsTd, youTd];
  }
  const s = r.summary;
  const isClaims = r.type === "claim" || r.type === "fixed";
  const count = isClaims ? `${s.claims} claim${s.claims === 1 ? "" : "s"}` : `${s.bids} bid${s.bids === 1 ? "" : "s"}`;
  lotsTd.append(el("div", undefined, `${s.lots} lot${s.lots === 1 ? "" : "s"} · ${count}`));
  if (s.unsure) lotsTd.append(el("div", "flag", `${s.unsure} unsure`));
  const mineCount = s.lead + s.outbid + s.claimed + s.check;
  if (mineCount === 0) youTd.append(el("span", "muted", isClaims ? "No claims" : "No bids"));
  if (s.lead) youTd.append(el("span", "status lead", `Leading ${s.lead}`));
  if (s.outbid) youTd.append(el("span", "status outbid", `Outbid ${s.outbid}`));
  if (s.claimed) {
    // What you won, with the prices Claude read off the photos (when it has).
    const mine = (r.lots ?? []).flatMap((l) => l.claimCards?.filter((x) => x.isMe) ?? []);
    const total = mine.reduce((n, x) => n + (x.price ?? 0), 0);
    youTd.append(el("span", "status lead", mine.length ? `Won ${mine.length} · ${total} kr` : `Won ${s.claimed}`));
  }
  if (s.check) {
    const check = el("span", "status outbid", `Check ${s.check}`);
    check.title = "Someone claimed the same before you: first come wins";
    youTd.append(check);
  }
  const justRead = now.getTime() - Date.parse(r.lastReadAt!) < 60_000;
  youTd.append(justRead ? el("div", "badge just-read", "Just read") : el("div", "muted small", `Read ${ago(r.lastReadAt!, now)}`));
  if (mineCount > 0 || s.lots > 0) {
    const toggle = el("button", "linkish", expanded.has(r.id) ? "Hide lots" : mineCount > 0 ? "Your lots" : "Lots");
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", String(expanded.has(r.id)));
    toggle.addEventListener("click", () => {
      if (expanded.has(r.id)) expanded.delete(r.id);
      else expanded.add(r.id);
      render();
    });
    youTd.append(toggle);
  }
  return [lotsTd, youTd];
}

/** The expanded lots: yours when you have bids, else all; image, highest bid, your bid, status. */
function lotsRow(r: Row, columns: number): HTMLTableRowElement {
  const tr = el("tr", "lots-row");
  const td = el("td");
  td.colSpan = columns;
  const mine = (r.lots ?? []).filter((l) => l.myStatus !== "none" || l.myClaim !== "none");
  const lots: Lot[] = mine.length ? mine : r.lots ?? [];
  const list = el("div", "lot-list");
  for (const l of lots) {
    const isClaims = r.type === "claim" || r.type === "fixed";
    const item = el("div", `lot lot-${isClaims ? { none: "none", claimed: "lead", check: "outbid" }[l.myClaim] : l.myStatus}`);
    if (l.imageUrl) {
      const img = el("img", "lot-img");
      img.src = l.imageUrl;
      img.alt = "";
      img.loading = "lazy";
      img.referrerPolicy = "no-referrer";
      zoomable(img, `${l.position}. ${l.title}`);
      item.append(img);
    }
    const body = el("div", "lot-body");
    body.append(el("div", "lot-title", `${l.position}. ${l.title}`));
    body.append(el("div", "orig", l.rawText.split("\n").slice(1).join(" · ")));
    if (isClaims) {
      if (l.claimCards) {
        // Claude read the photo and the replies: who got which card, at what price.
        const mine = l.claimCards.filter((x) => x.isMe);
        const total = mine.reduce((n, x) => n + (x.price ?? 0), 0);
        if (mine.length) {
          body.append(el("div", "status lead", `You won ${mine.length} · ${total} kr`));
          for (const x of mine) body.append(el("div", "small", `${x.card}: ${x.price ?? "?"} kr`));
        } else if (l.myClaim !== "none") {
          body.append(el("div", "status outbid", "Someone claimed it before you"));
        }
        for (const x of l.claimCards.filter((y) => !y.isMe)) body.append(el("div", "small muted", `${x.claimedBy}: ${x.card} ${x.price ?? "?"} kr`));
        body.append(el("div", "via", "Cards and prices read by Claude from the photo"));
        item.append(body);
        list.append(item);
        continue;
      }
      if (l.myClaim !== "none") {
        body.append(el("div", `status ${l.myClaim === "claimed" ? "lead" : "outbid"}`, l.myClaim === "claimed" ? "You claimed first (prices: waiting for Claude)" : "Someone claimed the same before you"));
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
    const hi =
      l.highestBid === null
        ? "No bids yet"
        : `Highest ${l.highestBid} kr (${l.highestBidder})${l.belowStart ? ` · below the start bid ${l.startBid} kr` : ""}`;
    body.append(el("div", l.belowStart ? "flag" : undefined, hi));
    if (l.myStatus !== "none") body.append(el("div", `status ${l.myStatus}`, `${l.myStatus === "lead" ? "Leading" : "Outbid"} · your bid ${l.myHighestBid} kr`));
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
  tr.append(td);
  return tr;
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
      ? `Last ${ago(claude.lastAt, now)}: ${claude.lastOutcome}`
      : "Asks Claude Code (claude -p, your login) only about what the rules can't read.";

  $("#reader-status").textContent =
    (reader.current || visiblePostIds().length ? "Reading a post now. " : "") +
    (reader.queue.length ? `${reader.queue.length} queued. ` : "") +
    (reader.lastAt && reader.lastOutcome ? `Last ${ago(reader.lastAt, now)}: ${reader.lastOutcome}` : "No background reads yet.");

  const name = $<HTMLInputElement>("#my-name");
  if (document.activeElement !== name) name.value = settings.myName;
}

function render() {
  const now = new Date();
  const visible = rows.filter(matches);
  const counts = countRows(rows, now);
  $("#count-active").textContent = String(counts.active);
  $("#count-hour").textContent = String(counts.withinHour);
  $("#count-outbid").textContent = String(counts.outbid);
  $("#count-new").textContent = String(counts.isNew);
  $("#last-read").textContent = lastFeedReadAt ? `Feed last read ${ago(lastFeedReadAt, now)}` : "Feed not read yet";
  {
  const dialog = $<HTMLDialogElement>("#photo");
  // A click anywhere (on the backdrop, the photo or ×) closes it; Esc is built in.
  dialog.addEventListener("click", () => dialog.close());
}
document.querySelectorAll<HTMLButtonElement>("[data-filter]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.filter === filter)));
  renderSettings(now);

  const main = $<HTMLElement>("#groups");
  main.replaceChildren();
  if (rows.length === 0) {
    main.append(el("p", "empty", "No sales yet. Open the group's feed on Facebook and click the extension icon to scan it."));
    return;
  }
  const headers = ["Ends", "Sale", "Type", "Price", "Lots", "You", "Seen"];
  for (const g of groupRows(visible, now)) {
    if (g.rows.length === 0) continue;
    const section = el("section", `group group-${g.id}`);
    const wrap = g.id === "ended" ? el("details") : section;
    const heading = el(g.id === "ended" ? "summary" : "h2", "group-title", `${g.label} `);
    heading.append(el("span", "group-count", String(g.rows.length)));
    wrap.append(heading);
    const table = el("table");
    const head = el("tr");
    for (const h of headers) head.append(el("th", undefined, h));
    table.append(el("thead"), el("tbody"));
    table.tHead!.append(head);
    for (const r of g.rows) {
      const tr = el("tr");
      if (r.summary?.outbid || r.summary?.check) tr.classList.add("mine-outbid");
      else if (r.summary?.lead || r.summary?.claimed) tr.classList.add("mine-lead");
      tr.append(endsCell(r, now), saleCell(r), el("td", "type", TYPE_LABEL[r.type]), el("td", "price", priceText(r)), ...statusCells(r, now));
      const seen = el("td", "seen", ago(r.lastSeenAt, now));
      seen.title = `First seen ${new Date(r.firstSeenAt).toLocaleString("en-GB")}`;
      tr.append(seen);
      table.tBodies[0].append(tr);
      if (expanded.has(r.id) && r.lots) table.tBodies[0].append(lotsRow(r, headers.length));
    }
    wrap.append(table);
    if (wrap !== section) section.append(wrap);
    main.append(section);
  }
  if (visible.length === 0) main.append(el("p", "empty", "Nothing matches the filter."));
}

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
  // A click anywhere (on the backdrop, the photo or ×) closes it; Esc is built in.
  dialog.addEventListener("click", () => dialog.close());
}
document.querySelectorAll<HTMLButtonElement>("[data-filter]").forEach((b) =>
  b.addEventListener("click", () => {
    filter = b.dataset.filter as Filter;
    local.set("fbaw-filter", filter);
    render();
  }),
);
$<HTMLInputElement>("#search").addEventListener("input", (e) => {
  query = (e.target as HTMLInputElement).value.trim();
  render();
});
$<HTMLInputElement>("#auto-scan").addEventListener("change", (e) => {
  void updateSettings({ autoScan: (e.target as HTMLInputElement).checked });
});
$<HTMLInputElement>("#use-claude").addEventListener("change", (e) => {
  void updateSettings({ useClaude: (e.target as HTMLInputElement).checked });
});
$<HTMLInputElement>("#my-name").addEventListener("change", (e) => {
  const name = (e.target as HTMLInputElement).value.trim();
  if (name) void updateSettings({ myName: name });
});
chrome.runtime.onMessage.addListener((msg) => {
  if (isStoreUpdatedMessage(msg)) void load();
});
// Settings and scan/Claude status live in chrome.storage; re-read when they change.
chrome.storage.onChanged.addListener((_changes, area) => {
  if (area === "local") void load();
});
setInterval(tick, 1000);
setInterval(() => {
  rebuild();
  render();
}, 30_000);
void load();
