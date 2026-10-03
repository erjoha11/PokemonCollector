import type { PostCapture } from "../../src/shared/capture";
import type { FeedPost, StoredPost } from "../../src/shared/feed";
import type { Store, StoredAnswer, StoredCapture } from "../../src/store";

// An in-memory Store (review M8) for tests of code that reads or writes the store: the reader's
// re-read choice, what goes to Claude (tests/claude-queue.test.ts). Seed it directly through `posts` / `captures` / `answers`.
// savePosts follows the real store's merge loosely; the real one (IndexedDB) has its own tests
// in tests/store.test.ts against fake-indexeddb.

export type MemoryStore = Store & {
  posts: Map<string, StoredPost>;
  captures: Map<string, PostCapture>;
  answers: Map<string, StoredAnswer>;
  meta: Map<string, string>;
};

export function memoryStore(seed: { posts?: StoredPost[]; captures?: StoredCapture[]; answers?: StoredAnswer[] } = {}): MemoryStore {
  const posts = new Map((seed.posts ?? []).map((p) => [p.id, p]));
  const captures = new Map((seed.captures ?? []).map((c) => [c.postId, c.capture]));
  const answers = new Map((seed.answers ?? []).map((a) => [a.key, a]));
  const meta = new Map<string, string>();
  return {
    posts,
    captures,
    answers,
    meta,
    async savePosts(list: FeedPost[], seenAt: Date) {
      const at = seenAt.toISOString();
      let added = 0;
      let updated = 0;
      for (const p of list) {
        const prev = posts.get(p.id);
        if (prev) updated++;
        else added++;
        const keepOldText = !!prev && prev.textComplete && !p.textComplete;
        posts.set(p.id, {
          ...p,
          text: keepOldText ? prev!.text : p.text,
          textComplete: keepOldText || p.textComplete,
          thumbnailUrl: p.thumbnailUrl ?? prev?.thumbnailUrl ?? null,
          sellerName: p.sellerName ?? prev?.sellerName ?? null,
          firstSeenAt: prev?.firstSeenAt ?? at,
          lastSeenAt: at,
        });
      }
      return { added, updated };
    },
    async allPosts() {
      return [...posts.values()];
    },
    async saveCapture(postId: string, capture: PostCapture) {
      captures.set(postId, capture);
    },
    async getCapture(postId: string) {
      return captures.get(postId) ?? null;
    },
    async allCaptures() {
      return [...captures].map(([postId, capture]) => ({ postId, capture }));
    },
    async saveAnswers(list: StoredAnswer[]) {
      for (const a of list) answers.set(a.key, a);
    },
    async allAnswers() {
      return [...answers.values()];
    },
    async getMeta(key: string) {
      return meta.get(key) ?? null;
    },
    async setMeta(key: string, value: string) {
      meta.set(key, value);
    },
    async deletePosts(ids: string[]) {
      for (const id of ids) posts.delete(id);
    },
    async deleteCaptures(postIds: string[]) {
      for (const id of postIds) captures.delete(id);
    },
    async deleteAnswers(keys: string[]) {
      for (const k of keys) answers.delete(k);
    },
    async clearAll() {
      for (const m of [posts, captures, answers, meta]) m.clear();
    },
  };
}
