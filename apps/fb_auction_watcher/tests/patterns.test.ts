import { describe, expect, it } from "vitest";
import {
  ariaKind,
  commentIdsFromHref,
  isCommentSortLabel,
  isExpanderLabel,
  isSeeMoreLabel,
  isSinglePostUrl,
} from "../src/content/post/patterns";

describe("isExpanderLabel", () => {
  it.each([
    "Vis flere kommentarer",
    "Vis tidligere kommentarer",
    "Se 5 flere kommentarer",
    "Vis alle 12 svar",
    "Vis 1 svar",
    "Vis 2 flere svar",
    "3 svar",
    "Ola Nordmann svarte · 3 svar",
    "View more comments",
    "View previous comments",
    "View 5 more comments",
    "View all 12 replies",
    "View 1 reply",
    "View 2 more replies",
    "3 replies",
    "Ola Nordmann replied · 3 replies",
    "  Vis   flere\nkommentarer ",
  ])("allows %j", (label) => {
    expect(isExpanderLabel(label)).toBe(true);
  });

  // Everything that could write, react, or change what is shown must never match.
  it.each([
    "Svar",
    "Reply",
    "Liker",
    "Lik",
    "Like",
    "Del",
    "Share",
    "Send",
    "Kommenter",
    "Comment",
    "Skriv en kommentar …",
    "Write a comment…",
    "Skriv et svar …",
    "Se mer",
    "See more",
    "Mest relevante",
    "Alle kommentarer",
    "Most relevant",
    "All comments",
    "Svar fra Ola Nordmann",
    "Vis 3 svar og skriv et svar",
    "Byr 300",
    "Claim",
    "",
  ])("rejects %j", (label) => {
    expect(isExpanderLabel(label)).toBe(false);
  });
});

describe("other labels", () => {
  it("detects See more", () => {
    expect(isSeeMoreLabel("Se mer")).toBe(true);
    expect(isSeeMoreLabel("See more")).toBe(true);
    expect(isSeeMoreLabel("Se mer av dette")).toBe(false);
  });

  it("detects the comment sort control", () => {
    expect(isCommentSortLabel("Mest relevante")).toBe(true);
    expect(isCommentSortLabel("All comments")).toBe(true);
    expect(isCommentSortLabel("Vis flere kommentarer")).toBe(false);
  });

  it("classifies comment aria-labels", () => {
    expect(ariaKind("Kommentar fra Ola Nordmann for 2 timer siden")).toBe("comment");
    expect(ariaKind("Comment by Ola Nordmann 2 hours ago")).toBe("comment");
    expect(ariaKind("Svar fra Ola på kommentaren til Kari")).toBe("reply");
    expect(ariaKind("Reply by Ola to Kari's comment")).toBe("reply");
    expect(ariaKind(null)).toBe(null);
  });
});

describe("commentIdsFromHref", () => {
  it("reads comment and reply IDs", () => {
    expect(commentIdsFromHref("/groups/1/posts/2/?comment_id=10&reply_comment_id=11")).toEqual({
      commentId: "10",
      replyCommentId: "11",
    });
    expect(commentIdsFromHref("https://www.facebook.com/groups/1/posts/2/?comment_id=10")).toEqual({
      commentId: "10",
      replyCommentId: null,
    });
    expect(commentIdsFromHref(null)).toEqual({ commentId: null, replyCommentId: null });
  });
});

describe("isSinglePostUrl", () => {
  it("matches post and permalink URLs only", () => {
    expect(isSinglePostUrl("https://www.facebook.com/groups/123/posts/555/")).toBe(true);
    expect(isSinglePostUrl("https://www.facebook.com/groups/pokemonsalg/permalink/555/?comment_id=1")).toBe(true);
    expect(isSinglePostUrl("https://www.facebook.com/groups/123/")).toBe(false);
    expect(isSinglePostUrl("https://www.facebook.com/groups/123/?sorting_setting=CHRONOLOGICAL")).toBe(false);
  });
});
