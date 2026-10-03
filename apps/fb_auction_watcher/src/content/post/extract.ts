import {
  CAPTURE_SCHEMA_VERSION,
  type CapturedComment,
  type CapturedImage,
  type CapturedPost,
  type CapturedReply,
  type PostCapture,
} from "../../shared/capture";
import {
  ariaKind,
  commentIdsFromHref,
  isFilteringCommentSort,
  isSeeMoreLabel,
  isSinglePostUrl,
  normalize,
} from "./patterns";
import { findSortControl, type SortAction } from "./sort";

// Reads a fully expanded post out of the DOM. No clicks, no hovers: expanding and the
// comment sort switch happen before this, in expand.ts and sort.ts.
// Structure signals only (role, aria-label, href patterns, dir="auto"), never CSS classes.

const ARTICLE = "[role='article']";
const BLOCK_TAGS = new Set(["DIV", "P", "LI", "UL", "OL", "H1", "H2", "H3", "H4", "H5", "H6", "BR", "BLOCKQUOTE"]);
/** Images smaller than this are avatars, emoji, or icons rather than photos. */
const MIN_PHOTO_PX = 64;

/** The post being read: a post opened from the feed renders in a dialog, a permalink page in main. */
export function findPostRoot(doc: Document): Element | null {
  const dialogs = Array.from(doc.querySelectorAll("[role='dialog']")).filter((d) => d.querySelector(ARTICLE));
  if (dialogs.length > 0) return dialogs[dialogs.length - 1];
  if (isSinglePostUrl(doc.location?.href ?? "")) return doc.querySelector("[role='main']");
  return null;
}

/** Text of `el`, with line breaks at block boundaries, skipping anything `skip` rejects. */
export function collectText(el: Element, skip: (e: Element) => boolean = () => false): string {
  const parts: string[] = [];
  const walk = (node: Node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      parts.push(node.textContent ?? "");
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const e = node as Element;
    if (e !== el && skip(e)) return;
    if (e.getAttribute("aria-hidden") === "true") return;
    const tag = e.tagName.toUpperCase();
    if (tag === "SCRIPT" || tag === "STYLE") return;
    const block = BLOCK_TAGS.has(tag) || e.getAttribute("dir") === "auto";
    if (block) parts.push("\n");
    e.childNodes.forEach(walk);
    if (block) parts.push("\n");
  };
  walk(el);
  return parts
    .join("")
    .split("\n")
    .map((line) => line.replace(/[ \t ]+/g, " ").trim())
    .filter(Boolean)
    .join("\n");
}

function absoluteUrl(href: string | null): string | null {
  if (!href) return null;
  try {
    return new URL(href, "https://www.facebook.com/").href;
  } catch {
    return null;
  }
}

function photoImages(container: Element, owns: (e: Element) => boolean): CapturedImage[] {
  const seen = new Set<string>();
  const images: CapturedImage[] = [];
  for (const img of Array.from(container.querySelectorAll("img"))) {
    if (!owns(img)) continue;
    const src = img.getAttribute("src");
    if (!src || src.startsWith("data:") || seen.has(src)) continue;
    const width = Number(img.getAttribute("width")) || (img as HTMLImageElement).naturalWidth || 0;
    const height = Number(img.getAttribute("height")) || (img as HTMLImageElement).naturalHeight || 0;
    const inPhotoLink = !!img.closest("a[href*='/photo'], a[href*='photo.php'], a[href*='fbid=']");
    const big = width >= MIN_PHOTO_PX || height >= MIN_PHOTO_PX;
    if (!inPhotoLink && !big) continue;
    seen.add(src);
    images.push({ src, alt: img.getAttribute("alt") ?? "" });
  }
  return images;
}

type Thing = {
  article: Element;
  kind: "comment" | "reply";
  commentId: string | null;
  replyCommentId: string | null;
  data: CapturedReply;
};

function readArticle(article: Element): Thing | null {
  const owns = (e: Element) => e.closest(ARTICLE) === article;
  const anchors = Array.from(article.querySelectorAll("a")).filter(owns);

  // The timestamp link is the comment's permalink and carries its IDs.
  const permalink = anchors.find((a) => commentIdsFromHref(a.getAttribute("href")).commentId !== null) ?? null;
  const ids = commentIdsFromHref(permalink?.getAttribute("href"));
  const ariaLabel = article.getAttribute("aria-label");
  const labelKind = ariaKind(ariaLabel);
  if (!permalink && !labelKind) return null; // Not a comment (e.g. the post itself).

  const kind: "comment" | "reply" = ids.replyCommentId ? "reply" : (labelKind ?? "comment");
  const authorLink =
    anchors.find((a) => a !== permalink && a.getAttribute("role") !== "button" && normalize(a.textContent) !== "") ??
    null;
  const author = authorLink ? collectText(authorLink) || null : null;

  const isControl = (e: Element) =>
    e.matches(ARTICLE) || e === authorLink || e === permalink || e.getAttribute("role") === "button";
  let text = collectText(article, isControl);
  if (author && text.startsWith(author)) text = text.slice(author.length).trim();

  const truncated = Array.from(article.querySelectorAll("[role='button']")).some(
    (b) => owns(b) && isSeeMoreLabel(b.textContent),
  );

  return {
    article,
    kind,
    commentId: ids.commentId,
    replyCommentId: ids.replyCommentId,
    data: {
      id: kind === "reply" ? ids.replyCommentId : ids.commentId,
      url: absoluteUrl(permalink?.getAttribute("href") ?? null),
      author,
      text,
      timeText: permalink ? collectText(permalink) || null : null,
      ariaLabel,
      images: photoImages(article, owns),
      truncated,
      rawText: collectText(article, (e) => e.matches(ARTICLE)),
    },
  };
}

function readPost(root: Element, commentArticles: Element[], pageUrl: string): CapturedPost {
  const inComment = (e: Element) => commentArticles.some((a) => a.contains(e));
  const owns = (e: Element) => !inComment(e);

  const message =
    Array.from(root.querySelectorAll("[data-ad-preview='message'], [data-ad-comet-preview='message']")).find(owns) ??
    null;
  let text = message ? collectText(message) : "";
  if (!text) {
    // Fallback: the longest dir="auto" block outside the comments.
    const blocks = Array.from(root.querySelectorAll("[dir='auto']")).filter(owns);
    text = blocks.map((b) => collectText(b)).sort((a, b) => b.length - a.length)[0] ?? "";
  }

  // The poster's group-member profile link (/groups/<group>/user/<id>/). On real group posts
  // the h3 heading link is the group name, so headings are only a fallback.
  const hasText = (a: Element) => owns(a) && normalize(a.textContent) !== "";
  const authorLink =
    Array.from(root.querySelectorAll("a[href*='/user/']")).find(
      (a) => hasText(a) && /\/groups\/[^/]+\/user\/\d+/.test(a.getAttribute("href") ?? ""),
    ) ?? Array.from(root.querySelectorAll("h2 a, h3 a, h4 a")).find(hasText);
  const postLink = Array.from(root.querySelectorAll("a[href*='/posts/'], a[href*='/permalink/']")).find(
    (a) => owns(a) && commentIdsFromHref(a.getAttribute("href")).commentId === null,
  );
  const truncated = Array.from(root.querySelectorAll("[role='button']")).some(
    (b) => owns(b) && isSeeMoreLabel(b.textContent),
  );

  return {
    url: pageUrl,
    author: authorLink ? collectText(authorLink) || null : null,
    text,
    timeText: postLink ? collectText(postLink) || postLink.getAttribute("aria-label") : null,
    images: photoImages(root, owns),
    truncated,
  };
}

export type ExtractContext = {
  pageUrl: string;
  pageLang: string;
  expandClicks: number;
  expandScrolls?: number;
  expandStoppedBecause: string;
  commentSortAction: SortAction;
  now?: Date;
};

export function extractCapture(root: Element, ctx: ExtractContext): PostCapture {
  const things = Array.from(root.querySelectorAll(ARTICLE))
    .map(readArticle)
    .filter((t): t is Thing => t !== null);
  const warnings: string[] = [];

  const comments: CapturedComment[] = [];
  const byCommentId = new Map<string, CapturedComment>();
  let orphanReplies = 0;
  let replyCount = 0;

  for (const t of things) {
    if (t.kind === "comment") {
      const comment: CapturedComment = {
        ...t.data,
        index: comments.length,
        hasImage: t.data.images.length > 0,
        replies: [],
      };
      comments.push(comment);
      if (t.commentId) byCommentId.set(t.commentId, comment);
      continue;
    }
    replyCount++;
    // A reply's permalink carries its parent's comment_id; page order is the fallback.
    const parent = (t.commentId && byCommentId.get(t.commentId)) || comments[comments.length - 1];
    if (!parent) {
      orphanReplies++;
      continue;
    }
    if (!(t.commentId && byCommentId.has(t.commentId))) orphanReplies++;
    parent.replies.push(t.data);
  }

  const sortControl = findSortControl(root);
  const commentSortLabel = sortControl ? collectText(sortControl) : null;

  if (comments.length === 0) {
    warnings.push("No comments found. Either the post has none, or Facebook's markup differs from what the spike expects.");
  }
  if (isFilteringCommentSort(commentSortLabel)) {
    warnings.push(
      `Comments are still sorted by "${commentSortLabel}" (switch: ${ctx.commentSortAction}), which can hide comments. Switch to "All comments" / "Alle kommentarer" by hand and read again.`,
    );
  }
  if (orphanReplies > 0) {
    warnings.push(`${orphanReplies} repl${orphanReplies === 1 ? "y" : "ies"} matched to a comment by page order, not by ID.`);
  }
  const truncatedCount = comments.filter((c) => c.truncated || c.replies.some((r) => r.truncated)).length;
  const post = readPost(root, things.map((t) => t.article), ctx.pageUrl);
  if (post.truncated || truncatedCount > 0) {
    warnings.push(
      `Text is still cut off ("See more") in ${post.truncated ? "the post" : ""}${post.truncated && truncatedCount ? " and " : ""}${truncatedCount ? `${truncatedCount} comment thread(s)` : ""} after expanding.`,
    );
  }
  if (ctx.expandStoppedBecause !== "done") {
    warnings.push(`Expanding stopped early (${ctx.expandStoppedBecause}); some comments or replies may be missing.`);
  }

  return {
    schemaVersion: CAPTURE_SCHEMA_VERSION,
    capturedAt: (ctx.now ?? new Date()).toISOString(),
    pageUrl: ctx.pageUrl,
    pageLang: ctx.pageLang,
    commentSortLabel,
    commentSortAction: ctx.commentSortAction,
    post,
    comments,
    stats: {
      expandClicks: ctx.expandClicks,
      expandScrolls: ctx.expandScrolls ?? 0,
      expandStoppedBecause: ctx.expandStoppedBecause,
      topLevelComments: comments.length,
      commentsWithImage: comments.filter((c) => c.hasImage).length,
      replies: replyCount,
      orphanReplies,
    },
    warnings,
  };
}
