import type { CapturedComment, CapturedReply, PostCapture } from "../../src/shared/capture";
import type { StoredPost } from "../../src/shared/feed";

// Builders for stored posts and post reads, shared by the worker and store tests. Invented names.

export const SELLER = "Selger Testesen";
export const ME = "Erik Johansen";

let seq = 1000;

export function reply(author: string, text: string, id = seq++): CapturedReply {
  return {
    id: String(3308000000000000 + id), url: null, author, text, timeText: "1 t",
    ariaLabel: `Svar fra ${author} på ${SELLER} sin kommentar`, images: [], truncated: false, rawText: text,
  };
}

/** A lot: the seller's comment with a photo. */
export function lot(id: number, replies: CapturedReply[], text = "Lot\nMp 10kr"): CapturedComment {
  return {
    id: String(9000 + id), url: null, author: SELLER, text, timeText: "1 d", ariaLabel: `Kommentar fra ${SELLER}`,
    images: [{ src: `https://scontent.example/lot${id}.jpg`, alt: "" }], truncated: false, rawText: text, index: id, hasImage: true, replies,
  };
}

export function capture(postId: string, text: string, comments: CapturedComment[], over: Partial<PostCapture> = {}): PostCapture {
  return {
    schemaVersion: 1, capturedAt: "2026-10-04T12:00:00Z", pageUrl: `https://www.facebook.com/groups/g/posts/${postId}/`, pageLang: "nb",
    commentSortLabel: "Alle kommentarer", commentSortAction: "already-all",
    post: { url: "", author: SELLER, text, timeText: null, images: [], truncated: false },
    comments, warnings: [],
    stats: { expandClicks: 0, expandScrolls: 0, expandStoppedBecause: "done", topLevelComments: comments.length, commentsWithImage: comments.length, replies: 0, orphanReplies: 0 },
    ...over,
  };
}

export function post(id: string, text: string, over: Partial<StoredPost> = {}): StoredPost {
  return {
    id, url: `https://www.facebook.com/groups/g/posts/${id}/`, groupSlug: "g", sellerName: SELLER, text, textComplete: true,
    thumbnailUrl: null, firstSeenAt: "2026-10-04T08:00:00Z", lastSeenAt: "2026-10-04T08:00:00Z", ...over,
  };
}

/** An auction ending 04.10.26 21:00 Oslo (19:00 UTC), no soft close unless asked. */
export const auctionText = (antisnipe: "Ja" | "Nei" = "Nei") =>
  `AUKSJON/BUDRUNDE-annonse\nMinimum budøkning: 10kr\nSluttid: 04.10.26 kl 21:00\nAntisnipe 5 min: ${antisnipe}`;
export const AUCTION_ENDS = Date.parse("2026-10-04T19:00:00Z");
