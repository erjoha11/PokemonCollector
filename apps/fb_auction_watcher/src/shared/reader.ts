// The post reader's state (src/background/reader.ts), shared with the overview that shows it.

export type ReadJob = {
  postId: string;
  url: string;
  /** "click": you clicked it in the overview (goes first, no pause). "auto": a background re-read. */
  reason: "click" | "auto";
  /** Open as a normal tab you see, and leave it open (your clicks). Otherwise a hidden tab, closed after. */
  visible?: boolean;
};
export type ReaderState = {
  queue: ReadJob[];
  current: (ReadJob & { tabId: number; startedAt: string }) | null;
  lastAt: string | null;
  lastOutcome: string | null;
};
