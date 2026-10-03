import { describe, expect, it } from "vitest";
import { untitledLotPhotos } from "../src/domain/bids";
import { bidAnswerKey, endTimeAnswerKey, lotNameAnswerKey } from "../src/llm/prompts";
import type { CapturedComment, CapturedReply, PostCapture } from "../src/shared/capture";
import type { StoredPost } from "../src/shared/feed";
import type { Store, StoredAnswer, StoredCapture } from "../src/store";
import { applyRetention, describeRetention, planRetention, RETENTION, type RetentionData } from "../src/store/retention";

// The retention rule (review M6), on invented posts, bidders and answers.
const SELLER = "Selger Testesen";
const ME = "Erik Johansen";
const DAY = 86_400_000;

// "Sluttid: 2026-10-04 21.00" Oslo = 19:00 UTC; antisnipe 5 min → closes 19:05 UTC.
const CLOSES = Date.parse("2026-10-04T19:05:00Z");
const afterClose = (days: number) => new Date(CLOSES + days * DAY);
const AUCTION_TEXT = "AUKSJON/BUDRUNDE-annonse\nSluttid: 2026-10-04 21.00\nAntisnipe 5 min: Ja\nMinimum budøkning: 10kr";
const FIXED_TEXT = "Fastpris-annonse\nFastpris: 300kr\nObjektbeskrivelse: Testmon kort";

const post = (id: string, text: string, lastSeenAt: string): StoredPost => ({
  id, url: `https://www.facebook.com/groups/g/posts/${id}/`, groupSlug: "g", sellerName: SELLER, text, textComplete: true,
  thumbnailUrl: null, firstSeenAt: "2026-10-01T10:00:00Z", lastSeenAt,
});

let seq = 100;
const reply = (author: string, text: string): CapturedReply => {
  const id = String(3308000000000000 + seq++);
  return { id, url: null, author, text, timeText: "1 t", ariaLabel: `Svar fra ${author} på ${SELLER} sin kommentar`, images: [], truncated: false, rawText: text };
};
const lot = (replies: CapturedReply[]): CapturedComment => ({
  id: "9001", url: null, author: SELLER, text: "Testmon holo\nMp 10kr", timeText: "1 d", ariaLabel: `Kommentar fra ${SELLER}`,
  images: [{ src: "https://cdn.example/lot1.jpg", alt: "" }], truncated: false, rawText: "", index: 0, hasImage: true, replies,
});
const capture = (id: string, text: string, capturedAt: string, replies: CapturedReply[]): StoredCapture => ({
  postId: id,
  capture: {
    schemaVersion: 1, capturedAt, pageUrl: `https://www.facebook.com/groups/g/posts/${id}/`, pageLang: "nb",
    commentSortLabel: "Alle kommentarer", commentSortAction: "already-all",
    post: { url: "", author: SELLER, text, timeText: null, images: [], truncated: false },
    comments: [lot(replies)],
    stats: { expandClicks: 0, expandScrolls: 0, expandStoppedBecause: "done", topLevelComments: 1, commentsWithImage: 1, replies: replies.length, orphanReplies: 0 },
    warnings: [], completeAt: capturedAt,
  } satisfies PostCapture,
});

const SEEN_BEFORE_END = "2026-10-04T18:00:00Z";
const READ_AFTER_END = "2026-10-04T19:10:00Z";
const someoneElsesSale = (): RetentionData => ({
  posts: [post("1", AUCTION_TEXT, SEEN_BEFORE_END)],
  captures: [capture("1", AUCTION_TEXT, READ_AFTER_END, [reply("Budgiver Ola", `${SELLER} 100`), reply("Budgiver Kari", `${SELLER} 150?`)])],
  // Claude's reading of Kari's unsure "150?", used by the read above.
  answers: [{ key: bidAnswerKey(SELLER, `${SELLER} 150?`), value: 150, at: READ_AFTER_END }],
});
const mySale = (): RetentionData => ({
  posts: [post("2", AUCTION_TEXT, SEEN_BEFORE_END)],
  captures: [capture("2", AUCTION_TEXT, READ_AFTER_END, [reply("Budgiver Ola", `${SELLER} 100`), reply(ME, `${SELLER} 120`)])],
  answers: [],
});

describe("planRetention", () => {
  it("a running sale is never deleted, however long ago it was seen or read", () => {
    const data: RetentionData = {
      posts: [post("1", AUCTION_TEXT, "2026-08-01T10:00:00Z")],
      captures: [capture("1", AUCTION_TEXT, "2026-08-01T10:00:00Z", [reply("Budgiver Kari", `${SELLER} 150?`)])],
      answers: [{ key: bidAnswerKey(SELLER, `${SELLER} 150?`), value: 150, at: "2026-08-01T10:00:00Z" }],
    };
    // Inside the antisnipe window counts as running too.
    for (const now of [new Date(CLOSES - 60 * DAY), new Date(CLOSES - 60_000)]) {
      expect(planRetention(data, ME, now)).toEqual({ posts: [], captures: [], answers: [] });
    }
  });

  it("someone else's ended sale: the read goes after 7 days, the slim post stays until unseen for 14", () => {
    const data = someoneElsesSale();
    expect(planRetention(data, ME, afterClose(RETENTION.readDaysAfterEnd - 0.5))).toEqual({ posts: [], captures: [], answers: [] });
    // The read and the Claude answer only it used are gone; the post row stays (seen < 14 days ago).
    expect(planRetention(data, ME, afterClose(8))).toEqual({ posts: [], captures: ["1"], answers: [data.answers[0].key] });
    // Not seen in the feed for 14 days: the post goes too.
    expect(planRetention(data, ME, afterClose(15))).toEqual({ posts: ["1"], captures: ["1"], answers: [data.answers[0].key] });
  });

  it("an ended sale you bid on is kept until its 30-day window passes", () => {
    const data = mySale();
    expect(planRetention(data, ME, afterClose(8))).toEqual({ posts: [], captures: [], answers: [] });
    expect(planRetention(data, ME, afterClose(RETENTION.myReadDays - 1))).toEqual({ posts: [], captures: [], answers: [] });
    expect(planRetention(data, ME, afterClose(RETENTION.myReadDays + 1))).toEqual({ posts: ["2"], captures: ["2"], answers: [] });
    // With a different name it's not yours: the normal 7 days.
    expect(planRetention(data, "Someone Else", afterClose(8)).captures).toEqual(["2"]);
  });

  it("a post still seen in the feed keeps its slim row after the read is gone", () => {
    const data = someoneElsesSale();
    data.posts[0].lastSeenAt = afterClose(19).toISOString(); // "New activity" scans still see it.
    expect(planRetention(data, ME, afterClose(20))).toMatchObject({ posts: [], captures: ["1"] });
  });

  it("no end time (fixed price): counts from when it was last seen, 14 days", () => {
    const seen = Date.parse("2026-10-10T12:00:00Z");
    const data: RetentionData = { posts: [post("3", FIXED_TEXT, new Date(seen).toISOString())], captures: [], answers: [] };
    expect(planRetention(data, ME, new Date(seen + 13 * DAY)).posts).toEqual([]);
    expect(planRetention(data, ME, new Date(seen + 15 * DAY)).posts).toEqual(["3"]);
  });

  it("a fixed-price sale you claimed in keeps its read 30 days after it was last seen", () => {
    const seen = "2026-10-10T12:00:00Z";
    const data: RetentionData = {
      posts: [post("4", FIXED_TEXT, seen)],
      captures: [capture("4", FIXED_TEXT, seen, [reply(ME, `${SELLER} claim`)])],
      answers: [],
    };
    const at = (days: number) => new Date(Date.parse(seen) + days * DAY);
    expect(planRetention(data, ME, at(20))).toEqual({ posts: [], captures: [], answers: [] });
    expect(planRetention(data, ME, at(31))).toEqual({ posts: ["4"], captures: ["4"], answers: [] });
  });

  it("Claude's end time counts, and keeps its answer while the post is kept", () => {
    const text = "AUKSJON/BUDRUNDE-annonse\nSlutter søndag en gang på kvelden";
    const answer: StoredAnswer = { key: endTimeAnswerKey(text), value: "2026-10-04 21:05", at: "2026-10-02T10:00:00Z" };
    const data: RetentionData = { posts: [post("5", text, "2026-09-01T10:00:00Z")], captures: [], answers: [answer] };
    // Seen long ago, but Claude says it's still running: nothing goes.
    expect(planRetention(data, ME, new Date(CLOSES - DAY))).toEqual({ posts: [], captures: [], answers: [] });
    // Ended over 7 days ago and unseen for 14: the post goes, and with it the answer.
    expect(planRetention(data, ME, afterClose(8))).toEqual({ posts: ["5"], captures: [], answers: [answer.key] });
  });

  it("answers nothing refers to are deleted; ones in use are kept whatever their age", () => {
    const data = someoneElsesSale();
    data.answers[0].at = "2025-01-01T00:00:00Z"; // Old, but the read still uses it.
    data.answers.push({ key: "bid:stale-reading", value: 99, at: READ_AFTER_END });
    expect(planRetention(data, ME, afterClose(1)).answers).toEqual(["bid:stale-reading"]);
  });

  it("a lot name Claude read from the photo stays while the read is kept, and goes with it", () => {
    const data = someoneElsesSale();
    const c = data.captures[0].capture;
    c.comments[0] = { ...c.comments[0], text: "Mp 10kr", rawText: "Mp 10kr" }; // Only a price: named from the photo.
    const [photo] = untitledLotPhotos(c);
    data.answers.push({ key: lotNameAnswerKey(photo), value: "Testmon 4/102", at: READ_AFTER_END });
    expect(planRetention(data, ME, afterClose(1)).answers).toEqual([]);
    expect(planRetention(data, ME, afterClose(8)).answers).toContain(lotNameAnswerKey(photo));
  });
});

/** A Store in memory, for the parts of the interface retention uses. */
function memoryStore(data: RetentionData): Store & { data: RetentionData } {
  const drop = <T>(list: T[], key: (x: T) => string, keys: string[]) => list.filter((x) => !keys.includes(key(x)));
  const s = {
    data,
    async savePosts() {
      return { added: 0, updated: 0 };
    },
    allPosts: async () => s.data.posts,
    async saveCapture() {},
    getCapture: async () => null,
    allCaptures: async () => s.data.captures,
    async saveAnswers() {},
    allAnswers: async () => s.data.answers,
    getMeta: async () => null,
    async setMeta() {},
    async deletePosts(ids: string[]) {
      s.data.posts = drop(s.data.posts, (p) => p.id, ids);
    },
    async deleteCaptures(ids: string[]) {
      s.data.captures = drop(s.data.captures, (c) => c.postId, ids);
    },
    async deleteAnswers(keys: string[]) {
      s.data.answers = drop(s.data.answers, (a) => a.key, keys);
    },
    async clearAll() {
      s.data = { posts: [], captures: [], answers: [] };
    },
  };
  return s;
}

describe("applyRetention", () => {
  it("deletes what the plan says and reports it", async () => {
    const other = someoneElsesSale();
    const mine = mySale();
    const store = memoryStore({
      posts: [...other.posts, ...mine.posts],
      captures: [...other.captures, ...mine.captures],
      answers: other.answers,
    });
    const result = await applyRetention(store, ME, afterClose(15));
    expect(result).toEqual({ captures: 1, posts: 1, answers: 1 });
    expect(store.data.posts.map((p) => p.id)).toEqual(["2"]);
    expect(store.data.captures.map((c) => c.postId)).toEqual(["2"]);
    expect(store.data.answers).toEqual([]);
    expect(describeRetention(result)).toBe("Removed 1 old post read, 1 post and 1 Claude answer");
  });
});

describe("describeRetention", () => {
  it("says what went, in plain words", () => {
    expect(describeRetention({ captures: 0, posts: 0, answers: 0 })).toBe("Nothing old to remove");
    expect(describeRetention({ captures: 12, posts: 30, answers: 0 })).toBe("Removed 12 old post reads and 30 posts");
    expect(describeRetention({ captures: 0, posts: 1, answers: 0 })).toBe("Removed 1 post");
  });
});
