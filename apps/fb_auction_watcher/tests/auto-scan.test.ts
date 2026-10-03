import { describe, expect, it } from "vitest";
import { describeAutoScan, isGroupFeedUrl, nextDelayMinutes, newPostsUrl } from "../src/background/autoScan";

describe("automatic scan", () => {
  it("always loads the feed sorted by New posts", () => {
    expect(newPostsUrl("https://www.facebook.com/groups/pokemonkortnorge/")).toBe("https://www.facebook.com/groups/pokemonkortnorge/?sorting_setting=CHRONOLOGICAL");
    expect(newPostsUrl("https://www.facebook.com/groups/pokemonkortnorge/?sorting_setting=RECENT_ACTIVITY")).toBe(
      "https://www.facebook.com/groups/pokemonkortnorge/?sorting_setting=CHRONOLOGICAL",
    );
  });

  it("only uses the group feed itself, not a post or photo in it", () => {
    expect(isGroupFeedUrl("https://www.facebook.com/groups/pokemonkortnorge/")).toBe(true);
    expect(isGroupFeedUrl("https://www.facebook.com/groups/pokemonkortnorge/?sorting_setting=CHRONOLOGICAL")).toBe(true);
    expect(isGroupFeedUrl("https://www.facebook.com/groups/pokemonkortnorge/posts/123/")).toBe(false);
    expect(isGroupFeedUrl("https://www.facebook.com/photo/?fbid=1")).toBe(false);
  });

  it("waits 10-15 min, ±20 %", () => {
    expect(nextDelayMinutes(() => 0)).toBeCloseTo(8);
    expect(nextDelayMinutes(() => 0.999999)).toBeCloseTo(18, 3);
  });

  it("says plainly what a round found", () => {
    expect(describeAutoScan("caught-up", 6, 1)).toBe("Done: 1 new post, caught up");
    expect(describeAutoScan("hidden", 4, 1)).toBe("Done: 1 new post, caught up");
    expect(describeAutoScan("hidden", 4, 4)).toMatch(/may have missed older ones/);
  });
});
