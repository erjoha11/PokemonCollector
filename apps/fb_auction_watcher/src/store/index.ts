import { openDB, type DBSchema, type IDBPDatabase } from "idb";
import type { PostCapture } from "../shared/capture";
import type { FeedPost, StoredPost } from "../shared/feed";

/** A post read with the toolbar icon (comments and replies), latest read per post. */
export type StoredCapture = { postId: string; capture: PostCapture };

/** An answer from Claude for one item the rules couldn't read, keyed by task + a hash of the input. */
export type StoredAnswer = { key: string; value: unknown; at: string };

// Persistence behind a small interface (swappable for Supabase later, per docs/spec.md).
// IndexedDB in the extension's own origin: the service worker writes, the table page reads.
// Only raw captures are stored; interpretation runs on read, so better rules apply to old posts.

export interface Store {
  /** Inserts new posts and updates known ones (text, thumbnail, lastSeenAt). Returns how many were new. */
  savePosts(posts: FeedPost[], seenAt: Date): Promise<{ added: number; updated: number }>;
  allPosts(): Promise<StoredPost[]>;
  saveCapture(postId: string, capture: PostCapture): Promise<void>;
  getCapture(postId: string): Promise<PostCapture | null>;
  allCaptures(): Promise<StoredCapture[]>;
  saveAnswers(answers: StoredAnswer[]): Promise<void>;
  allAnswers(): Promise<StoredAnswer[]>;
  getMeta(key: string): Promise<string | null>;
  setMeta(key: string, value: string): Promise<void>;
}

interface Schema extends DBSchema {
  posts: { key: string; value: StoredPost };
  meta: { key: string; value: string };
  captures: { key: string; value: StoredCapture };
  answers: { key: string; value: StoredAnswer };
}

const DB_NAME = "fb-auction-watcher";
const DB_VERSION = 2;

export function idbStore(): Store {
  let db: Promise<IDBPDatabase<Schema>> | null = null;
  const open = () =>
    (db ??= openDB<Schema>(DB_NAME, DB_VERSION, {
      upgrade(d, oldVersion) {
        if (oldVersion < 1) {
          d.createObjectStore("posts", { keyPath: "id" });
          d.createObjectStore("meta");
        }
        if (oldVersion < 2) {
          d.createObjectStore("captures", { keyPath: "postId" });
          d.createObjectStore("answers", { keyPath: "key" });
        }
      },
      // A newer version (after an extension update) wants to upgrade: let it, reopen next time.
      blocking(_current, _blocked, event) {
        (event.target as IDBDatabase).close();
        db = null;
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
    async saveCapture(postId, capture) {
      await (await open()).put("captures", { postId, capture });
    },
    async getCapture(postId) {
      return (await (await open()).get("captures", postId))?.capture ?? null;
    },
    async allCaptures() {
      return (await open()).getAll("captures");
    },
    async saveAnswers(answers) {
      const tx = (await open()).transaction("answers", "readwrite");
      for (const a of answers) await tx.store.put(a);
      await tx.done;
    },
    async allAnswers() {
      return (await open()).getAll("answers");
    },
    async getMeta(key) {
      return (await (await open()).get("meta", key)) ?? null;
    },
    async setMeta(key, value) {
      await (await open()).put("meta", value, key);
    },
  };
}
