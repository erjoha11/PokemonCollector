import { beforeEach, describe, expect, it } from "vitest";
import { ensureAllComments } from "../src/content/post/sort";

const FAST = { menuTimeoutMs: 300, settleMs: 0 };
let clicks: string[];

function setup(sortLabel: string, opensMenu = true) {
  document.body.innerHTML = `
    <div role="dialog" id="root">
      <div role="button" id="sort">${sortLabel}</div>
      <div role="button">Svar</div>
      <form><div role="textbox" contenteditable="true">Alle kommentarer</div></form>
    </div>`;
  clicks = [];
  document.querySelectorAll("[role='button'], [role='textbox']").forEach((el) =>
    el.addEventListener("click", () => clicks.push(el.textContent!.trim())),
  );
  const sort = document.getElementById("sort")!;
  if (!opensMenu) return;
  sort.addEventListener("click", () => {
    // Facebook renders the menu as a portal outside the post.
    const menu = document.createElement("div");
    menu.setAttribute("role", "menu");
    menu.innerHTML = `
      <div role="menuitem"><span>Mest relevante</span><span>Vis venners kommentarer først.</span></div>
      <div role="menuitem"><span>Nyeste</span><span>Vis alle kommentarer, med de nyeste først.</span></div>
      <div role="menuitem" id="all"><span>Alle kommentarer</span><span>Vis alle kommentarer, inkludert potensiell søppelpost.</span></div>`;
    menu.querySelectorAll("[role='menuitem']").forEach((item) =>
      item.addEventListener("click", () => {
        clicks.push(`menu: ${item.firstElementChild!.textContent}`);
        sort.textContent = item.firstElementChild!.textContent;
        menu.remove();
      }),
    );
    document.body.appendChild(menu);
  });
}

const root = () => document.getElementById("root")!;

describe("ensureAllComments", () => {
  beforeEach(() => setup("Mest relevante"));

  it("switches Most relevant to All comments with exactly two clicks", async () => {
    expect(await ensureAllComments(root(), FAST)).toBe("switched");
    expect(clicks).toEqual(["Mest relevante", "menu: Alle kommentarer"]);
    expect(document.getElementById("sort")!.textContent).toBe("Alle kommentarer");
  });

  it("does nothing when already on All comments", async () => {
    setup("Alle kommentarer");
    expect(await ensureAllComments(root(), FAST)).toBe("already-all");
    expect(clicks).toEqual([]);
  });

  it("reports not-found when there is no sort control", async () => {
    document.body.innerHTML = `<div role="dialog" id="root"><div role="button">Svar</div></div>`;
    expect(await ensureAllComments(root(), FAST)).toBe("not-found");
  });

  it("gives up without clicking anything else when the menu never opens", async () => {
    setup("Most relevant", false);
    expect(await ensureAllComments(root(), FAST)).toBe("failed");
    expect(clicks).toEqual(["Most relevant"]);
  });

  it("does not click when aborted", async () => {
    const controller = new AbortController();
    controller.abort();
    expect(await ensureAllComments(root(), { ...FAST, signal: controller.signal })).toBe("aborted");
    expect(clicks).toEqual([]);
  });
});
