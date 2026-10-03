import { interpretLots, summarizeLots } from "../../domain/bids";
import { interpretListing } from "../../domain/listing";
import type { PostCapture } from "../../shared/capture";
import { getSettings } from "../../shared/settings";
import type { Panel } from "./panel";

// A small status overlay for quiet reads (a sale clicked in the overview): one line in the
// bottom-right corner while the post is read, then the result, then it fades. Same calls as the
// panel (Panel), so the read code doesn't care which one it has. Shadow DOM, like the panel,
// so Facebook's CSS can't reach it. Its links act only on the extension itself.

const STYLE = `
:host { all: initial; }
.pill {
  position: fixed; right: 16px; bottom: 16px; z-index: 2147483647;
  display: flex; align-items: center; gap: 10px; max-width: 420px;
  font: 13px/1.35 "IBM Plex Sans", system-ui, sans-serif;
  color: #1b1f24; background: #fff; border: 1px solid #d0d7de; border-left: 4px solid #2457D6;
  border-radius: 8px; box-shadow: 0 6px 18px rgba(0,0,0,.16); padding: 8px 10px;
  transition: opacity .4s;
}
.pill.done { border-left-color: #1a7f37; }
.pill.error { border-left-color: #B42318; }
.pill.fade { opacity: 0; }
.spin { width: 12px; height: 12px; border: 2px solid #2457D6; border-right-color: transparent; border-radius: 50%; animation: s .8s linear infinite; flex: none; }
.done .spin, .error .spin { display: none; }
@keyframes s { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
.text { min-width: 0; }
.title { font-weight: 600; font-size: 12px; color: #59636e; }
.lead { color: #2457D6; font-weight: 600; }
.outbid { color: #C2570C; font-weight: 600; }
button { font: inherit; font-size: 12px; background: none; border: none; color: #59636e; cursor: pointer; padding: 0 2px; }
button:hover { color: #1b1f24; text-decoration: underline; }
@media (prefers-color-scheme: dark) {
  .pill { color: #e6edf3; background: #161b22; border-color: #30363d; border-left-color: #6d9bff; }
  .pill.done { border-left-color: #3fb950; }
  .pill.error { border-left-color: #ff7b72; }
  .title, button { color: #9198a1; }
  button:hover { color: #e6edf3; }
  .lead { color: #6d9bff; }
  .outbid { color: #f0883e; }
}
`;

/** "100 lots · 13 bids · Leading 1 · Outbid 2", from a capture and your name. */
async function summary(capture: PostCapture): Promise<{ text: string; lead: number; outbid: number }> {
  const listing = interpretListing(capture.post.text, new Date(capture.capturedAt));
  const { myName } = await getSettings();
  const s = summarizeLots(interpretLots(capture, { myName, listingIncrement: listing.increment, listingMinPrice: listing.minPrice }));
  const parts = [`${s.lots} lot${s.lots === 1 ? "" : "s"}`, `${s.bids} bid${s.bids === 1 ? "" : "s"}`];
  return { text: parts.join(" · "), lead: s.lead, outbid: s.outbid };
}

export function showStatusPill(): Panel {
  document.getElementById("fbaw-pill-host")?.remove();
  document.getElementById("fbaw-panel-host")?.remove();
  const host = document.createElement("div");
  host.id = "fbaw-pill-host";
  const shadow = host.attachShadow({ mode: "open" });
  shadow.innerHTML = `
    <style>${STYLE}</style>
    <div class="pill" role="status" aria-live="polite">
      <div class="spin" aria-hidden="true"></div>
      <div class="text"><div class="title">Auction Watcher</div><div class="line">Starting…</div></div>
      <button class="stop" type="button">Stop</button>
      <button class="close" type="button" aria-label="Close">×</button>
    </div>`;
  document.documentElement.appendChild(host);

  const pill = shadow.querySelector(".pill") as HTMLDivElement;
  const line = shadow.querySelector(".line") as HTMLDivElement;
  const stop = shadow.querySelector(".stop") as HTMLButtonElement;
  let stopHandler: (() => void) | null = null;
  let fadeTimer: ReturnType<typeof setTimeout> | null = null;

  stop.addEventListener("click", () => {
    stop.remove();
    line.textContent = "Stopping… (saving what's read so far)";
    stopHandler?.();
  });
  shadow.querySelector(".close")!.addEventListener("click", () => host.remove());
  // Hovering keeps it from fading, so you can read the result.
  pill.addEventListener("mouseenter", () => fadeTimer && clearTimeout(fadeTimer));
  pill.addEventListener("mouseleave", () => pill.classList.contains("done") && fadeSoon(3000));

  function fadeSoon(ms: number) {
    if (fadeTimer) clearTimeout(fadeTimer);
    fadeTimer = setTimeout(() => {
      pill.classList.add("fade");
      setTimeout(() => host.remove(), 500);
    }, ms);
  }

  return {
    setStatus(text) {
      line.textContent = text;
    },
    onStop(handler) {
      stopHandler = handler;
    },
    showResult(capture) {
      stop.remove();
      pill.classList.add("done");
      const s = capture.stats;
      line.textContent = `Read ${s.topLevelComments} comments, ${s.replies} replies`;
      void summary(capture).then(({ text, lead, outbid }) => {
        line.textContent = `Saved · ${text}`;
        if (lead) line.append(" · ", Object.assign(document.createElement("span"), { className: "lead", textContent: `Leading ${lead}` }));
        if (outbid) line.append(" · ", Object.assign(document.createElement("span"), { className: "outbid", textContent: `Outbid ${outbid}` }));
      });
      fadeSoon(10_000);
    },
    showError(text) {
      stop.remove();
      pill.classList.add("error");
      line.textContent = text;
    },
    // Feed scans use the full panel; these aren't used by quiet reads.
    showFeedRecorder() {},
    setScanProgress() {},
    showScanDone() {},
  };
}
