// Raw capture of one Facebook post, as produced by the module 1 spike.
// Nothing here is interpreted: no bids, amounts, or end times. That is module 3's job.

export const CAPTURE_SCHEMA_VERSION = 1;

export type CapturedImage = {
  src: string;
  alt: string;
};

export type CapturedReply = {
  /** reply_comment_id from the reply's permalink, if found. */
  id: string | null;
  url: string | null;
  author: string | null;
  text: string;
  /** Relative time as Facebook shows it ("2 t", "3h"). Absolute times need a hover, which we don't do. */
  timeText: string | null;
  ariaLabel: string | null;
  images: CapturedImage[];
  /** A "See more" / "Se mer" button was still present after expanding, so the text may be cut off. */
  truncated: boolean;
  rawText: string;
};

export type CapturedComment = CapturedReply & {
  /** Position among top-level comments, in page order. */
  index: number;
  /** Lot candidate: a top-level comment with an image. */
  hasImage: boolean;
  replies: CapturedReply[];
};

export type CapturedPost = {
  url: string;
  author: string | null;
  text: string;
  timeText: string | null;
  images: CapturedImage[];
  truncated: boolean;
};

export type CaptureStats = {
  expandClicks: number;
  /** Scrolls that loaded more comments (dialogs load comments on scroll, not by button). */
  expandScrolls: number;
  expandStoppedBecause: string;
  topLevelComments: number;
  commentsWithImage: number;
  replies: number;
  /** Replies whose parent comment couldn't be found; attached to the nearest preceding comment instead. */
  orphanReplies: number;
};

export type PostCapture = {
  schemaVersion: typeof CAPTURE_SCHEMA_VERSION;
  capturedAt: string;
  pageUrl: string;
  pageLang: string;
  /** Label of the comment sort control after any switch ("Alle kommentarer"...), if found. */
  commentSortLabel: string | null;
  /** What the reader did about the comment sort before expanding. */
  commentSortAction: "already-all" | "switched" | "not-found" | "failed" | "aborted";
  post: CapturedPost;
  comments: CapturedComment[];
  stats: CaptureStats;
  warnings: string[];
};
