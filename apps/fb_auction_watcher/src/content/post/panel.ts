import type { PostCapture } from "../../shared/capture";

// Small status panel for the module 1 spike, in a Shadow DOM so Facebook's CSS can't
// reach it (and ours can't reach Facebook). Its buttons act only on the extension itself.

const STYLE = `
:host { all: initial; }
.panel {
  position: fixed; right: 16px; bottom: 16px; z-index: 2147483647;
  width: 340px; max-height: 70vh; overflow: auto;
  font: 13px/1.4 "IBM Plex Sans", system-ui, sans-serif;
  color: #1b1f24; background: #fff; border: 1px solid #d0d7de; border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0,0,0,.18); padding: 12px;
}
h1 { font-size: 14px; margin: 0 0 8px; }
.status { margin: 0 0 8px; }
.stats { font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 12px; margin: 0 0 8px; white-space: pre-wrap; }
.warn { color: #C2570C; margin: 4px 0; }
.error { color: #B42318; }
.buttons { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
button {
  font: inherit; padding: 4px 10px; border-radius: 6px; cursor: pointer;
  border: 1px solid #d0d7de; background: #f6f8fa; color: inherit;
}
button.primary { background: #2457D6; border-color: #2457D6; color: #fff; }
@media (prefers-color-scheme: dark) {
  .panel { color: #e6edf3; background: #161b22; border-color: #30363d; }
  button { background: #21262d; border-color: #30363d; }
}
`;

export type Panel = {
  setStatus(text: string): void;
  onStop(handler: () => void): void;
  showResult(capture: PostCapture, snapshotHtml: string): void;
  showError(text: string): void;
  /** Feed recording mode; `sampleHtml` builds the download from everything recorded so far. */
  showFeedRecorder(sampleHtml: () => string): void;
  setRecordedCount(count: number): void;
};

function download(filename: string, content: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

function fileStem(capture: PostCapture): string {
  const id = capture.pageUrl.match(/\/(?:posts|permalink)\/(\d+)/)?.[1] ?? "post";
  return `fbaw-${id}-${capture.capturedAt.replace(/[:.]/g, "-")}`;
}

export function showPanel(): Panel {
  document.getElementById("fbaw-panel-host")?.remove();
  const host = document.createElement("div");
  host.id = "fbaw-panel-host";
  const shadow = host.attachShadow({ mode: "open" });
  shadow.innerHTML = `
    <style>${STYLE}</style>
    <div class="panel" role="dialog" aria-label="FB Auction Watcher">
      <h1>FB Auction Watcher: read post</h1>
      <p class="status"></p>
      <div class="body"></div>
      <div class="buttons">
        <button class="stop" type="button">Stop</button>
        <button class="close" type="button">Close</button>
      </div>
    </div>`;
  document.documentElement.appendChild(host);

  const $ = <T extends Element>(sel: string) => shadow.querySelector(sel) as T;
  const status = $<HTMLParagraphElement>(".status");
  const body = $<HTMLDivElement>(".body");
  const buttons = $<HTMLDivElement>(".buttons");
  const stop = $<HTMLButtonElement>(".stop");
  let stopHandler: (() => void) | null = null;

  stop.addEventListener("click", () => {
    stop.disabled = true;
    stop.textContent = "Stopping…";
    stopHandler?.();
  });
  $<HTMLButtonElement>(".close").addEventListener("click", () => {
    stopHandler?.();
    host.remove();
  });

  return {
    setStatus(text) {
      status.textContent = text;
    },
    onStop(handler) {
      stopHandler = handler;
    },
    showResult(capture, snapshotHtml) {
      stop.remove();
      const s = capture.stats;
      status.textContent = "Done. Only clicked: comment sort, more comments/replies, See more. Scrolled to load comments.";
      const stats = document.createElement("p");
      stats.className = "stats";
      stats.textContent = [
        `Comment sort:     ${capture.commentSortLabel ?? "not found"} (${capture.commentSortAction})`,
        `Expand clicks:    ${s.expandClicks}, scrolls: ${s.expandScrolls} (${s.expandStoppedBecause})`,
        `Comments:         ${s.topLevelComments}`,
        `  with image:     ${s.commentsWithImage}  (lot candidates)`,
        `Replies:          ${s.replies}`,
        `Post text chars:  ${capture.post.text.length}`,
        `Post images:      ${capture.post.images.length}`,
      ].join("\n");
      body.replaceChildren(stats);
      for (const w of capture.warnings) {
        const p = document.createElement("p");
        p.className = "warn";
        p.textContent = `⚠ ${w}`;
        body.appendChild(p);
      }
      const stem = fileStem(capture);
      const json = document.createElement("button");
      json.type = "button";
      json.className = "primary";
      json.textContent = "Download JSON";
      json.addEventListener("click", () =>
        download(`${stem}.json`, JSON.stringify(capture, null, 2), "application/json"),
      );
      const html = document.createElement("button");
      html.type = "button";
      html.textContent = "Download HTML snapshot";
      html.title = "The post's DOM as rendered now, for samples/. Contains other people's names: never commit it.";
      html.addEventListener("click", () => download(`${stem}.html`, snapshotHtml, "text/html"));
      buttons.prepend(json, html);
    },
    showFeedRecorder(sampleHtml) {
      shadow.querySelector("h1")!.textContent = "FB Auction Watcher: record feed";
      stop.textContent = "Stop recording";
      const hint = document.createElement("p");
      hint.textContent =
        "Scroll the feed slowly. Each post is saved as it appears, because Facebook empties posts that leave the screen. Nothing is clicked. The file contains other people's names: keep it in samples/, never commit it.";
      body.replaceChildren(hint);
      const html = document.createElement("button");
      html.type = "button";
      html.className = "primary";
      html.textContent = "Download feed sample";
      html.addEventListener("click", () => {
        const stamp = new Date().toISOString().replace(/[:.]/g, "-");
        download(`fbaw-feed-${stamp}.html`, sampleHtml(), "text/html");
      });
      buttons.prepend(html);
    },
    setRecordedCount(count) {
      status.textContent = `Recording: ${count} post${count === 1 ? "" : "s"} saved.`;
    },
    showError(text) {
      stop.remove();
      status.textContent = "";
      const p = document.createElement("p");
      p.className = "error";
      p.textContent = text;
      body.replaceChildren(p);
    },
  };
}
