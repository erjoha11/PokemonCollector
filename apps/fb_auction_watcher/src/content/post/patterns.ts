// Text patterns for Facebook's UI. Facebook renders labels in the account's language
// (the user's is Norwegian), so every pattern covers both Norwegian and English.
// CSS classes are never used: Facebook obfuscates them.

export function normalize(text: string | null | undefined): string {
  return (text ?? "").replace(/\s+/g, " ").trim().toLowerCase();
}

const N = String.raw`\d[\d.,\s]*`;

/**
 * The ONLY controls the extension may click: ones that load more comments or replies.
 * Every pattern is anchored to the whole label, so "Svar" / "Reply" (opens the reply
 * box), "Liker" / "Like", "Se mer" / "See more" and the comment sort menu never match.
 */
const EXPANDER_PATTERNS: RegExp[] = [
  // "View more comments", "View previous comments", "View 5 more comments"
  new RegExp(String.raw`^(view|see) (${N} )?(more|previous|earlier|older) comments?$`),
  // "View all 3 replies", "View 1 reply", "View 2 more replies"
  new RegExp(String.raw`^(view|see) (all )?${N} (more )?repl(y|ies)$`),
  /^(view|see) (more|previous) repl(y|ies)$/,
  // "3 replies", "Ola replied · 3 replies"
  new RegExp(String.raw`^${N} (more )?repl(y|ies)$`),
  new RegExp(String.raw`^.{1,80} replied · ${N} repl(y|ies)$`),
  // "Vis flere kommentarer", "Vis tidligere kommentarer", "Se 5 flere kommentarer"
  new RegExp(String.raw`^(vis|se) (${N} )?(flere|tidligere|eldre) kommentar(er)?$`),
  // "Vis alle 3 svar", "Vis 1 svar", "Vis 2 flere svar"
  new RegExp(String.raw`^(vis|se) (alle )?${N} (flere )?svar$`),
  /^(vis|se) (flere|tidligere) svar$/,
  // "3 svar", "Ola svarte · 3 svar"
  new RegExp(String.raw`^${N} (flere )?svar$`),
  new RegExp(String.raw`^.{1,80} svarte · ${N} svar$`),
];

export function isExpanderLabel(label: string | null | undefined): boolean {
  const text = normalize(label);
  if (!text || text.length > 120) return false;
  return EXPANDER_PATTERNS.some((re) => re.test(text));
}

/** "See more" on truncated text. Detected only, never clicked: it isn't on the allowed list. */
export function isSeeMoreLabel(label: string | null | undefined): boolean {
  return /^(see more|se mer|vis mer)$/.test(normalize(label));
}

/** The comment sort control. Detected only, never clicked. */
export function isCommentSortLabel(label: string | null | undefined): boolean {
  return /^(most relevant|newest|all comments|mest relevante|nyeste|alle kommentarer)$/.test(normalize(label));
}

/** Comment sorts that may hide comments (Facebook filters "most relevant"). */
export function isFilteringCommentSort(label: string | null | undefined): boolean {
  return /^(most relevant|mest relevante)$/.test(normalize(label));
}

/** aria-label on a comment's role="article": "Comment by Ola ..." / "Kommentar fra Ola ...". */
export function ariaKind(ariaLabel: string | null | undefined): "comment" | "reply" | null {
  const text = normalize(ariaLabel);
  if (/^(reply|svar) (by|from|fra) /.test(text)) return "reply";
  if (/^(comment|kommentar) (by|from|fra) /.test(text)) return "comment";
  return null;
}

/** IDs from a comment permalink: ?comment_id=1 for a comment, plus &reply_comment_id=2 for a reply. */
export function commentIdsFromHref(href: string | null | undefined): {
  commentId: string | null;
  replyCommentId: string | null;
} {
  if (!href) return { commentId: null, replyCommentId: null };
  let params: URLSearchParams;
  try {
    params = new URL(href, "https://www.facebook.com/").searchParams;
  } catch {
    return { commentId: null, replyCommentId: null };
  }
  return { commentId: params.get("comment_id"), replyCommentId: params.get("reply_comment_id") };
}

/** A single post: /groups/<group>/posts/<id> or /groups/<group>/permalink/<id>. */
export function isSinglePostUrl(url: string): boolean {
  try {
    return /^\/groups\/[^/]+\/(posts|permalink)\/[^/]+/.test(new URL(url).pathname);
  } catch {
    return false;
  }
}
