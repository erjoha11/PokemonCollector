import type { Lot } from "../../domain/bids";
import type { PostCapture } from "../../shared/capture";
import { isStoreUpdatedMessage } from "../../shared/messages";
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
const expanded = new Set<string>();
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
    if (!r.summary || r.summary.lead + r.summary.outbid === 0) return false;
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
    wrap.append(img);
  }
  const box = el("div", "sale-text");
  const a = el("a", "title", r.title);
  a.href = r.url;
  a.target = "_blank";
  a.rel = "noopener";
  box.append(a);
  if (r.description && r.description !== r.title) box.append(el("div", "desc", r.description));
  const meta = el("div", "seller", r.sellerName ?? "Unknown seller");
  if (r.isNew) meta.append(" ", el("span", "badge new", "New"));
  box.append(meta);
  wrap.append(box);
  return td;
}

/** Lots · bids, and your status, from the latest post read; a nudge to read it otherwise. */
function statusCells(r: Row, now: Date): HTMLTableCellElement[] {
  const lotsTd = el("td", "lots");
  const youTd = el("td", "you");
  if (!r.summary) {
    lotsTd.append(el("span", "muted", "–"));
    if (r.type === "auction" && !r.ended) {
      const hint = el("span", "muted small", "Open the post and click the icon to read bids");
      youTd.append(hint);
    }
    return [lotsTd, youTd];
  }
  const s = r.summary;
  lotsTd.append(el("div", undefined, `${s.lots} lot${s.lots === 1 ? "" : "s"} · ${s.bids} bid${s.bids === 1 ? "" : "s"}`));
  if (s.unsure) lotsTd.append(el("div", "flag", `${s.unsure} unsure`));
  if (s.lead + s.outbid === 0) youTd.append(el("span", "muted", "No bids"));
  if (s.lead) youTd.append(el("span", "status lead", `Leading ${s.lead}`));
  if (s.outbid) youTd.append(el("span", "status outbid", `Outbid ${s.outbid}`));
  youTd.append(el("div", "muted small", `Read ${ago(r.lastReadAt!, now)}`));
  if (s.lead + s.outbid > 0 || s.lots > 0) {
    const toggle = el("button", "linkish", expanded.has(r.id) ? "Hide lots" : s.lead + s.outbid > 0 ? "Your lots" : "Lots");
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
  const mine = (r.lots ?? []).filter((l) => l.myStatus !== "none");
  const lots: Lot[] = mine.length ? mine : r.lots ?? [];
  const list = el("div", "lot-list");
  for (const l of lots) {
    const item = el("div", `lot lot-${l.myStatus}`);
    if (l.imageUrl) {
      const img = el("img", "lot-img");
      img.src = l.imageUrl;
      img.alt = "";
      img.loading = "lazy";
      img.referrerPolicy = "no-referrer";
      item.append(img);
    }
    const body = el("div", "lot-body");
    body.append(el("div", "lot-title", `${l.position}. ${l.title}`));
    body.append(el("div", "orig", l.rawText.split("\n").slice(1).join(" · ")));
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
      if (r.summary?.outbid) tr.classList.add("mine-outbid");
      else if (r.summary?.lead) tr.classList.add("mine-lead");
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
