import { afterEach, describe, expect, it, vi } from "vitest";
import { popupActions, SCAN_POST_HINT } from "../src/pages/popup/actions";
import { GROUP_FEED_URL, isGroupFeedUrl, isPostUrl, newPostsUrl } from "../src/shared/urls";
import popupHtml from "../public/popup.html?raw";
import { fakeChrome } from "./fakes/chrome";

// The toolbar menu's four buttons (#320): what Scan feed and Scan Post do per active tab, and
// the page itself (public/popup.html + main.ts) with its Reload extension button.

const FEED = "https://www.facebook.com/groups/g/";
const POST = "https://www.facebook.com/groups/g/posts/123456/";
const NEW_TAB = { kind: "new-tab", url: GROUP_FEED_URL };

describe("popup buttons", () => {
  it("on the group feed: Scan feed scans here, Scan Post is disabled with a hint", () => {
    expect(popupActions(FEED)).toEqual({ scanFeed: { kind: "here" }, scanPost: { enabled: false, hint: SCAN_POST_HINT } });
    expect(popupActions(`${FEED}?sorting_setting=CHRONOLOGICAL`).scanFeed).toEqual({ kind: "here" });
    expect(popupActions(newPostsUrl(GROUP_FEED_URL)).scanFeed).toEqual({ kind: "here" });
  });

  it("on a post: Scan Post reads it, Scan feed opens the group's feed in a new tab", () => {
    expect(popupActions(POST)).toEqual({ scanFeed: NEW_TAB, scanPost: { enabled: true } });
  });

  it("anywhere else, with no group known: Scan feed still opens the group's feed (never disabled), Scan Post is disabled", () => {
    for (const url of ["https://example.com/", "https://www.facebook.com/", "chrome://extensions/", undefined]) {
      const a = popupActions(url);
      expect(a.scanFeed).toEqual(NEW_TAB);
      expect(a.scanPost).toEqual({ enabled: false, hint: SCAN_POST_HINT });
    }
  });

  it("the group is pokemonkortnorge, a feed URL by its vanity slug, opened sorted by New posts", () => {
    expect(GROUP_FEED_URL).toBe("https://www.facebook.com/groups/pokemonkortnorge/");
    expect(isGroupFeedUrl(GROUP_FEED_URL)).toBe(true);
    expect(newPostsUrl(GROUP_FEED_URL)).toBe("https://www.facebook.com/groups/pokemonkortnorge/?sorting_setting=CHRONOLOGICAL");
    expect(isGroupFeedUrl(newPostsUrl(GROUP_FEED_URL))).toBe(true);
    expect(isGroupFeedUrl("https://www.facebook.com/groups/pokemonkortnorge")).toBe(true);
  });
});

describe("popup page", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    document.body.innerHTML = "";
  });

  async function loadPopup() {
    // The page's body, without its <script> (main.ts is imported below instead).
    document.body.innerHTML = /<body>([\s\S]*)<\/body>/.exec(popupHtml)![1].replace(/<script[\s\S]*?<\/script>/g, "");
    const fake = fakeChrome();
    const reload = vi.spyOn(fake.chrome.runtime, "reload");
    vi.stubGlobal("chrome", fake.chrome);
    vi.resetModules();
    await import("../src/pages/popup/main");
    await vi.waitFor(() => expect(document.getElementById("auto")!.textContent).not.toBe(""));
    return { fake, reload };
  }

  it("has exactly four buttons, in order: Open Dashboard, Scan feed, Scan Post, Reload extension", async () => {
    await loadPopup();
    const labels = [...document.querySelectorAll("button")].map((b) => b.textContent?.trim());
    expect(labels).toEqual(["Open Dashboard", "Scan feed", "Scan Post", "Reload extension"]);
    expect(document.body.textContent).not.toContain("Reload Facebook tabs");
  });

  it("Reload extension calls chrome.runtime.reload() (the path from before #320), and nothing else", async () => {
    const { fake, reload } = await loadPopup();
    document.getElementById("reload-ext")!.click();
    expect(reload).toHaveBeenCalledTimes(1);
    expect(fake.runtimeMessages).toEqual([]);
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
