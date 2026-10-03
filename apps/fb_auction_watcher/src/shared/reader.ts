// The post reader's state (src/background/reader.ts), shared with the overview that shows it.

export type ReadJob = { postId: string; url: string; reason: "click" | "auto" };
export type ReaderState = {
  /** Posts you opened from the overview, being read silently in their (visible) tab, by tab ID. */
  visible: Record<string, { postId: string; startedAt: string }>;
  queue: ReadJob[];
  current: (ReadJob & { tabId: number; startedAt: string }) | null;
  lastAt: string | null;
  lastOutcome: string | null;
};
