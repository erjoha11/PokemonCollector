import { afterEach, describe, expect, it, vi } from "vitest";
import { scanAction } from "../src/pages/popup/actions";
import { GROUP_FEED_URL, isGroupFeedUrl, isPostUrl, newPostsUrl } from "../src/shared/urls";
import popupHtml from "../public/popup.html?raw";
import { fakeChrome } from "./fakes/chrome";

// The toolbar menu: three buttons, with one Scan button whose label and action follow the active
// tab (post → Scan post, feed → Scan feed, anywhere else → Open feed and scan), and the page
// itself (public/popup.html + main.ts).

const FEED = "https://www.facebook.com/groups/g/";
const POST = "https://www.facebook.com/groups/g/posts/123456/";

describe("the Scan button", () => {
  it("on a post: Scan post, which reads it here", () => {
    expect(scanAction(POST)).toMatchObject({ kind: "post", label: "Scan post" });
  });

  it("on the group feed (any sort): Scan feed, which scans it here", () => {
    for (const url of [FEED, `${FEED}?sorting_setting=CHRONOLOGICAL`, newPostsUrl(GROUP_FEED_URL)]) {
      expect(scanAction(url)).toMatchObject({ kind: "feed", label: "Scan feed" });
    }
  });

  it("anywhere else: Open feed and scan, with the group's feed (never disabled)", () => {
    for (const url of ["https://example.com/", "https://www.facebook.com/", "chrome://extensions/", undefined]) {
      expect(scanAction(url)).toMatchObject({ kind: "open-feed", label: "Open feed and scan", url: GROUP_FEED_URL });
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

  it("has exactly three buttons, in order: Open Dashboard, Scan (here: Open feed and scan), Reload extension", async () => {
    await loadPopup();
    const labels = [...document.querySelectorAll("button")].map((b) => b.textContent?.trim());
    expect(labels).toEqual(["Open Dashboard", "Open feed and scan", "Reload extension"]);
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
