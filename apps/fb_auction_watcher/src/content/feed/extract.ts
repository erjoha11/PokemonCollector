import type { FeedPost } from "../../shared/feed";
import { collectText } from "../post/extract";
import { isSeeMoreLabel } from "../post/patterns";
import { postId } from "./recorder";

// Reads one rendered feed post into a raw FeedPost. Structure signals only: the post's parts
// are marked with data-ad-rendering-role, never CSS classes.

const part = (post: Element, role: string) => post.querySelector(`[data-ad-rendering-role='${role}']`);

/** Facebook scrambles the hidden full-text copy in most posts; real text has normal words. */
function looksLikeText(s: string): boolean {
  const words = s.split(/\s+/).filter(Boolean);
  return words.length >= 4 && s.length / words.length < 15;
}

export function groupSlug(pageUrl: string): string | null {
  return pageUrl.match(/\/groups\/([^/?#]+)/)?.[1] ?? null;
}

export function extractFeedPost(post: Element, pageUrl: string): FeedPost | null {
  const id = postId(post);
  if (!id) return null;

  const profile = part(post, "profile_name");
  const sellerLink = profile?.querySelector("a[href*='/user/']") ?? null;
  const sellerName = (sellerLink ? collectText(sellerLink) : collectText(profile ?? post).split("\n")[0])
    .replace(/\s*·.*$/, "")
    .trim() || null;

  // The visible text; buttons ("Se mer") skipped. If it is still cut off, a readable hidden
  // copy (description) is better, when Facebook hasn't scrambled it.
  const message = part(post, "story_message");
  const isButton = (e: Element) => e.getAttribute("role") === "button";
  let text = message ? collectText(message, isButton) : "";
  const cut = !!message && Array.from(message.querySelectorAll("[role='button']")).some((b) => isSeeMoreLabel(b.textContent));
  let textComplete = !!message && !cut;
  if (!textComplete) {
    const description = part(post, "description");
    const full = description ? collectText(description) : "";
    if (looksLikeText(full) && full.length > text.length) {
      text = full;
      textComplete = true;
    }
  }
  text = text.replace(/\s*…$/, "").trim();
  if (!text) return null;

  const photo = post.querySelector("a[href*='/photo'] img, a[href*='photo.php'] img");
  const slug = groupSlug(pageUrl);
  return {
    id,
    url: slug ? `https://www.facebook.com/groups/${slug}/posts/${id}/` : `https://www.facebook.com/${id}`,
    groupSlug: slug,
    sellerName,
    text,
    textComplete,
    thumbnailUrl: photo?.getAttribute("src") ?? null,
  };
}
