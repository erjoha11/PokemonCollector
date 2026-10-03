// A sale post as read from the group feed: raw only, nothing interpreted. Interpretation
// (src/domain/listing.ts) runs on `text` whenever the table renders.

export type FeedPost = {
  /** Facebook's post ID. */
  id: string;
  url: string;
  groupSlug: string | null;
  sellerName: string | null;
  /** The post's full text as captured (after "Se mer" when it was opened). */
  text: string;
  /** False if the text still ended in "… Se mer" when captured. */
  textComplete: boolean;
  thumbnailUrl: string | null;
};

export type StoredPost = FeedPost & {
  firstSeenAt: string;
  lastSeenAt: string;
};

/** The group's slug from a Facebook URL ("pokemonkortnorge" from /groups/pokemonkortnorge/...). */
export function groupSlug(pageUrl: string): string | null {
  return pageUrl.match(/\/groups\/([^/?#]+)/)?.[1] ?? null;
}
