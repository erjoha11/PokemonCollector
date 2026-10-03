import type { CapturedComment, CapturedReply, PostCapture } from "../shared/capture";

// Merging reads of the same post (review H2). A read can miss replies: a hidden background tab
// doesn't load comments by scrolling, a quiet read stops after 4 minutes, the sort switch to
// "All comments" can fail. Replacing the stored read with such a partial one could hide the bid
// that beat you, and the overview would say "Leading". So reads are merged: comments and replies
// are matched by Facebook ID, everything seen before is kept, new ones are added, and the newest
// text wins. A reply deleted on Facebook is therefore kept (we can't tell it from one a partial
// read didn't load); that errs towards "Outbid", the safe side.

const total = (c: PostCapture) => c.comments.length + c.comments.reduce((n, x) => n + x.replies.length, 0);

/** Comment/reply IDs increase with time; no ID sorts last (stable). */
function byId<T extends { id: string | null }>(a: T, b: T): number {
  if (a.id === null || b.id === null) return a.id === b.id ? 0 : a.id === null ? 1 : -1;
  if (a.id.length !== b.id.length) return a.id.length - b.id.length;
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}

/**
 * Whether a read looks complete: comments sorted by "All comments", expanding finished on its
 * own, and at least as many comments and replies as the stored read had.
 */
export function isCompleteRead(read: PostCapture, before: PostCapture | null): boolean {
  const sortOk = read.commentSortAction === "already-all" || read.commentSortAction === "switched";
  const done = read.stats.expandStoppedBecause === "done";
  return sortOk && done && (!before || total(read) >= total(before));
}

/** Union of two lists by ID (newer wins per ID); items without an ID come from the newer list if it has any. */
function mergeById<T extends { id: string | null }>(older: T[], newer: T[], combine: (o: T, n: T) => T = (_o, n) => n): T[] {
  const out = new Map<string, T>();
  for (const x of older) if (x.id) out.set(x.id, x);
  for (const x of newer) if (x.id) out.set(x.id, out.has(x.id) ? combine(out.get(x.id)!, x) : x);
  const newerNoId = newer.filter((x) => !x.id);
  const noId = newerNoId.length ? newerNoId : older.filter((x) => !x.id);
  return [...[...out.values()].sort(byId), ...noId];
}

function mergeComment(older: CapturedComment, newer: CapturedComment): CapturedComment {
  return {
    ...newer,
    // A read that lost the photo (still loading) keeps the earlier one.
    images: newer.images.length ? newer.images : older.images,
    hasImage: newer.hasImage || older.hasImage,
    text: newer.text || older.text,
    replies: mergeById<CapturedReply>(older.replies, newer.replies, (o, n) => ({
      ...n,
      text: n.text || o.text,
      images: n.images.length ? n.images : o.images,
    })),
  };
}

/** Merges a new read of a post into the stored one (null on the first read). */
export function mergeCaptures(stored: PostCapture | null, read: PostCapture): PostCapture {
  const complete = isCompleteRead(read, stored);
  const completeAt = complete ? read.capturedAt : (stored?.completeAt ?? null);
  if (!stored) return { ...read, completeAt, reads: 1 };

  const comments = mergeById(stored.comments, read.comments, mergeComment).map((c, index) => ({ ...c, index }));
  const replies = comments.reduce((n, c) => n + c.replies.length, 0);
  return {
    ...read,
    // The fuller post text wins (a read can catch it before "See more" opened).
    post: read.post.text.length >= stored.post.text.length ? read.post : { ...read.post, text: stored.post.text },
    comments,
    stats: {
      ...read.stats,
      topLevelComments: comments.length,
      commentsWithImage: comments.filter((c) => c.hasImage).length,
      replies,
    },
    warnings: complete ? read.warnings : [...read.warnings, "This read looked incomplete; merged with what earlier reads saw."],
    completeAt,
    reads: (stored.reads ?? 1) + 1,
  };
}
