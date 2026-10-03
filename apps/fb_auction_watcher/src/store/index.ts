import { openDB, type DBSchema, type IDBPDatabase } from "idb";
import type { FeedPost, StoredPost } from "../shared/feed";

// Persistence behind a small interface (swappable for Supabase later, per docs/spec.md).
// IndexedDB in the extension's own origin: the service worker writes, the table page reads.
// Only raw captures are stored; interpretation runs on read, so better rules apply to old posts.

export interface Store {
  /** Inserts new posts and updates known ones (text, thumbnail, lastSeenAt). Returns how many were new. */
  savePosts(posts: FeedPost[], seenAt: Date): Promise<{ added: number; updated: number }>;
  allPosts(): Promise<StoredPost[]>;
  getMeta(key: string): Promise<string | null>;
  setMeta(key: string, value: string): Promise<void>;
}

interface Schema extends DBSchema {
  posts: { key: string; value: StoredPost };
  meta: { key: string; value: string };
}

const DB_NAME = "fb-auction-watcher";
const DB_VERSION = 1;

export function idbStore(): Store {
  let db: Promise<IDBPDatabase<Schema>> | null = null;
  const open = () =>
    (db ??= openDB<Schema>(DB_NAME, DB_VERSION, {
      upgrade(d) {
        d.createObjectStore("posts", { keyPath: "id" });
        d.createObjectStore("meta");
      },
    }));

  return {
    async savePosts(posts, seenAt) {
      const tx = (await open()).transaction("posts", "readwrite");
      const at = seenAt.toISOString();
      let added = 0;
      let updated = 0;
      for (const p of posts) {
        const prev = await tx.store.get(p.id);
        if (!prev) added++;
        else updated++;
        // Keep complete text over a later cut-off rendering of the same post.
        const keepOldText = prev && prev.textComplete && !p.textComplete;
        await tx.store.put({
          ...p,
          text: keepOldText ? prev.text : p.text,
          textComplete: keepOldText ? true : p.textComplete,
          thumbnailUrl: p.thumbnailUrl ?? prev?.thumbnailUrl ?? null,
          sellerName: p.sellerName ?? prev?.sellerName ?? null,
          firstSeenAt: prev?.firstSeenAt ?? at,
          lastSeenAt: at,
        });
      }
      await tx.done;
      return { added, updated };
    },
    async allPosts() {
      return (await open()).getAll("posts");
    },
    async getMeta(key) {
      return (await (await open()).get("meta", key)) ?? null;
    },
    async setMeta(key, value) {
      await (await open()).put("meta", value, key);
    },
  };
}
