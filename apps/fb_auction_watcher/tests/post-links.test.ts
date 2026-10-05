import { describe, expect, it } from "vitest";
import { buildRows } from "../src/pages/dashboard/model";
import { canonicalPostUrl, groupPostUrl, lotUrl } from "../src/shared/urls";
import { post } from "./fakes/posts";

// A post read while Facebook showed it in the photo viewer was saved with that page's address
// (photo.php?fbid=…), so "11. Dark Octillery" linked to a photo with ?comment_id= on it instead of
// to its comment (2026-10-05). Links are now always the group post's own address.

const PHOTO = "https://www.facebook.com/photo.php?fbid=29539049065697822&set=p.29539049065697822&type=3";
const ID = "3310557635801790";

describe("canonicalPostUrl", () => {
  it("keeps the group post's own address", () => {
    expect(canonicalPostUrl(`https://www.facebook.com/groups/pokemonkortnorge/posts/${ID}/`, ID)).toBe(`https://www.facebook.com/groups/pokemonkortnorge/posts/${ID}/`);
    expect(canonicalPostUrl(`https://www.facebook.com/groups/other/permalink/${ID}/`, ID)).toBe(`https://www.facebook.com/groups/other/permalink/${ID}/`);
  });

  it("rebuilds anything else from the post ID, in its group (the watched one when unknown)", () => {
    expect(canonicalPostUrl(PHOTO, ID)).toBe(`https://www.facebook.com/groups/pokemonkortnorge/posts/${ID}/`);
    expect(canonicalPostUrl(PHOTO, ID, "g")).toBe(groupPostUrl(ID, "g"));
    expect(canonicalPostUrl(`https://www.facebook.com/groups/pokemonkortnorge/posts/999/`, ID)).toBe(groupPostUrl(ID)); // Another post's address.
  });

  it("leaves a post without a numeric ID alone (nothing to rebuild from)", () => {
    expect(canonicalPostUrl("https://www.facebook.com/x/posts/pfbid0abc", "pfbid0abc")).toBe("https://www.facebook.com/x/posts/pfbid0abc");
  });
});

describe("stored posts with a photo link", () => {
  it("the overview links the post, and its lots, by the post's own address", () => {
    const [r] = buildRows([post(ID, "AUKSJON/BUDRUNDE-annonse\nSluttid: 04.10.26 kl 21:00", { url: PHOTO, groupSlug: null })], new Date("2026-10-04T12:00:00Z"), null);
    expect(r.url).toBe(`https://www.facebook.com/groups/pokemonkortnorge/posts/${ID}/`);
    expect(lotUrl(r, { commentId: "3310956189095268" })).toBe(`https://www.facebook.com/groups/pokemonkortnorge/posts/${ID}/?comment_id=3310956189095268`);
  });
});
