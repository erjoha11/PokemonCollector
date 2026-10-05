import { describe, expect, it } from "vitest";
import { feedToOpen, NO_GROUP_HINT, popupActions, SCAN_POST_HINT } from "../src/pages/popup/actions";
import { isPostUrl } from "../src/shared/urls";

// The toolbar menu's three buttons (#320): what Scan feed and Scan Post do per active tab.

const FEED = "https://www.facebook.com/groups/g/";
const POST = "https://www.facebook.com/groups/g/posts/123456/";

describe("popup buttons", () => {
  it("on the group feed: Scan feed scans here, Scan Post is disabled with a hint", () => {
    expect(popupActions(FEED, FEED)).toEqual({ scanFeed: { kind: "here" }, scanPost: { enabled: false, hint: SCAN_POST_HINT } });
    expect(popupActions(`${FEED}?sorting_setting=CHRONOLOGICAL`, null).scanFeed).toEqual({ kind: "here" });
  });

  it("on a post: Scan Post reads it, Scan feed opens its group's feed in a new tab", () => {
    const feedUrl = feedToOpen(POST, [], null);
    expect(feedUrl).toBe(FEED);
    expect(popupActions(POST, feedUrl)).toEqual({ scanFeed: { kind: "new-tab", url: FEED }, scanPost: { enabled: true } });
  });

  it("anywhere else: Scan feed opens the known feed, Scan Post is disabled", () => {
    const a = popupActions("https://example.com/", FEED);
    expect(a.scanFeed).toEqual({ kind: "new-tab", url: FEED });
    expect(a.scanPost).toEqual({ enabled: false, hint: SCAN_POST_HINT });
    expect(popupActions("chrome://extensions/", FEED).scanPost.enabled).toBe(false);
    expect(popupActions(undefined, FEED).scanPost.enabled).toBe(false);
  });

  it("no group known at all: Scan feed is disabled with a hint", () => {
    expect(popupActions("https://www.facebook.com/", null).scanFeed).toEqual({ kind: "unavailable", hint: NO_GROUP_HINT });
  });

  it("which feed to open: the active tab's group, then an open feed tab (pinned first), then the last seen post's group", () => {
    const tabs = [
      { url: "https://www.facebook.com/groups/other/", pinned: false },
      { url: "https://www.facebook.com/groups/pinned/", pinned: true },
    ];
    expect(feedToOpen("https://www.facebook.com/groups/here/posts/1/", tabs, "recent")).toBe("https://www.facebook.com/groups/here/");
    expect(feedToOpen("https://example.com/", tabs, "recent")).toBe("https://www.facebook.com/groups/pinned/");
    expect(feedToOpen("https://example.com/", [tabs[0]], "recent")).toBe("https://www.facebook.com/groups/other/");
    // A tab inside a group that isn't its feed (a post) doesn't count as a feed tab.
    expect(feedToOpen("https://example.com/", [{ url: POST }], "recent")).toBe("https://www.facebook.com/groups/recent/");
    expect(feedToOpen("https://example.com/", [], null)).toBeNull();
  });
});

describe("isPostUrl", () => {
  it.each([
    [POST, true],
    ["https://www.facebook.com/groups/g/posts/123456/?comment_id=9", true],
    ["https://www.facebook.com/groups/g/permalink/123456/", true],
    ["https://www.facebook.com/someone/posts/pfbid0abc", true],
    ["https://www.facebook.com/permalink.php?story_fbid=1&id=2", true],
    [FEED, false],
    ["https://www.facebook.com/groups/g/user/42/", false],
    ["https://www.facebook.com/photo/?fbid=1", false],
    ["https://www.facebook.com/", false],
    ["https://www.facebook.com/permalink.php", false],
    ["https://evil.example/groups/g/posts/1/", false],
    ["not a url", false],
  ])("%s -> %s", (url, want) => expect(isPostUrl(url)).toBe(want));
});
