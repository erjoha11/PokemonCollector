import { isStoreUpdatedMessage } from "../../shared/messages";
import { idbStore } from "../../store";
import { ago, buildRows, countdown, countRows, endLabel, groupRows, type Row } from "./model";

// The overview: every sale read from the feed, grouped and sorted by end time, with live
// countdowns. Reads the store directly (same extension origin as the service worker) and
// re-reads when the worker says a scan saved posts. The seller's original end-time text is
// always shown next to the interpreted time (docs/spec.md rule).

const store = idbStore();
type Filter = "all" | "auction" | "claim" | "fixed";

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

async function load() {
  const [posts, readAt] = await Promise.all([store.allPosts(), store.getMeta("lastFeedReadAt")]);
  rows = buildRows(posts, new Date(), lastVisit);
  lastFeedReadAt = readAt;
  render();
}

function matches(r: Row): boolean {
  if (filter !== "all" && r.type !== filter) return false;
  if (!query) return true;
  const q = query.toLowerCase();
  return [r.title, r.sellerName ?? "", r.text].some((s) => s.toLowerCase().includes(q));
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
  if (r.endsAt && !r.sure) td.append(el("div", "flag", "? check the original text"));
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

function render() {
  const now = new Date();
  const visible = rows.filter(matches);
  const counts = countRows(rows, now);
  $("#count-active").textContent = String(counts.active);
  $("#count-hour").textContent = String(counts.withinHour);
  $("#count-new").textContent = String(counts.isNew);
  $("#last-read").textContent = lastFeedReadAt ? `Feed last read ${ago(lastFeedReadAt, now)}` : "Feed not read yet";
  document.querySelectorAll<HTMLButtonElement>("[data-filter]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.filter === filter)));

  const main = $<HTMLElement>("#groups");
  main.replaceChildren();
  if (rows.length === 0) {
    main.append(el("p", "empty", "No sales yet. Open the group's feed on Facebook and click the extension icon to scan it."));
    return;
  }
  for (const g of groupRows(visible, now)) {
    if (g.rows.length === 0) continue;
    const section = el("section", `group group-${g.id}`);
    const wrap = g.id === "ended" ? el("details") : section;
    const heading = el(g.id === "ended" ? "summary" : "h2", "group-title", `${g.label} `);
    heading.append(el("span", "group-count", String(g.rows.length)));
    wrap.append(heading);
    const table = el("table");
    const head = el("tr");
    for (const h of ["Ends", "Sale", "Type", "Price", "Seen"]) head.append(el("th", undefined, h));
    table.append(el("thead"), el("tbody"));
    table.tHead!.append(head);
    for (const r of g.rows) {
      const tr = el("tr");
      tr.append(endsCell(r, now), saleCell(r), el("td", "type", TYPE_LABEL[r.type]), el("td", "price", priceText(r)));
      const seen = el("td", "seen", ago(r.lastSeenAt, now));
      seen.title = `First seen ${new Date(r.firstSeenAt).toLocaleString("en-GB")}`;
      tr.append(seen);
      table.tBodies[0].append(tr);
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
chrome.runtime.onMessage.addListener((msg) => {
  if (isStoreUpdatedMessage(msg)) void load();
});
setInterval(tick, 1000);
setInterval(() => {
  rows = buildRows(rows, new Date(), lastVisit);
  render();
}, 30_000);
void load();
