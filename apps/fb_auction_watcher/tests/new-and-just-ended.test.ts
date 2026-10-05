import { describe, expect, it } from "vitest";
import { buildRows, countRows, ENDED_GRACE_MS, NEW_WINDOW_MS, tabs, type Tab } from "../src/pages/dashboard/model";
import { capture, lot, ME, post, reply, SELLER } from "./fakes/posts";

// #320: "New" survives a rescan for 30 min after first seen, and an ended sale stays in the
// active tabs (all but Ended) for 30 min after it ended, shown as ended.

const ids = (t: Tab[]) => Object.fromEntries(t.map((x) => [x.id, x.sections.flatMap((s) => s.rows.map((r) => r.id))]));
const at = (iso: string) => new Date(iso);
const MIN = 60_000;

/** An auction ending 04.10.26 15:00 Oslo (13:00 UTC) by default, with or without a 5-min antisnipe. */
const auction = (antisnipe: "Ja" | "Nei" = "Nei", end = "04.10.26 kl 15:00") =>
  `AUKSJON/BUDRUNDE-annonse\nMinimum budøkning: 10kr\nSluttid: ${end}\nAntisnipe 5 min: ${antisnipe}`;
const END = Date.parse("2026-10-04T13:00:00Z");

describe("New: first seen after the last visit, or under 30 min ago", () => {
  const seen = "2026-10-04T10:00:00Z";
  const p = post("1", auction("Nei", "05.10.26 kl 21:00"), { firstSeenAt: seen, lastSeenAt: seen });

  it("a visit (or rescan) inside the window doesn't clear it", () => {
    // You visited 10 min after it was first seen; a rescan since only moved lastSeenAt.
    const rescanned = { ...p, lastSeenAt: "2026-10-04T10:15:00Z" };
    const now = at("2026-10-04T10:20:00Z");
    const [r] = buildRows([rescanned], now, at("2026-10-04T10:10:00Z"));
    expect(r.isNew).toBe(true);
    expect(ids(tabs([r], now)).new).toEqual(["1"]);
  });

  it("just under 30 min: still New; at 30 min and after: only the last-visit rule", () => {
    const visit = at("2026-10-04T10:10:00Z");
    const t0 = Date.parse(seen);
    expect(buildRows([p], new Date(t0 + NEW_WINDOW_MS - 1), visit)[0].isNew).toBe(true);
    expect(buildRows([p], new Date(t0 + NEW_WINDOW_MS), visit)[0].isNew).toBe(false);
    expect(buildRows([p], new Date(t0 + 2 * NEW_WINDOW_MS), visit)[0].isNew).toBe(false);
    // Outside the window, the old rule still holds: first seen after the last visit is New.
    expect(buildRows([p], new Date(t0 + 2 * NEW_WINDOW_MS), at("2026-10-04T09:00:00Z"))[0].isNew).toBe(true);
  });

  it("is New inside the window even on the first visit ever (no last visit)", () => {
    expect(buildRows([p], at("2026-10-04T10:05:00Z"), null)[0].isNew).toBe(true);
    expect(buildRows([p], at("2026-10-04T11:00:00Z"), null)[0].isNew).toBe(false);
  });
});

describe("ended sales stay in their active tabs for 30 min", () => {
  it("by its end time: in Today, as ended and not active; at 30 min and after only in Ended", () => {
    const p = post("1", auction());
    const inside = at("2026-10-04T13:10:00Z");
    const [r] = buildRows([p], inside, at("2026-10-04T09:00:00Z"));
    expect(r).toMatchObject({ ended: true, justEnded: true, endedAtMs: END });
    expect(ids(tabs([r], inside))).toMatchObject({ today: ["1"], upcoming: [], ended: ["1"] });
    expect(countRows([r], inside)).toMatchObject({ active: 0, withinHour: 0 });

    const justBefore = new Date(END + ENDED_GRACE_MS - 1);
    expect(ids(tabs(buildRows([p], justBefore, null), justBefore)).today).toEqual(["1"]);
    for (const t of [new Date(END + ENDED_GRACE_MS), new Date(END + ENDED_GRACE_MS + 5 * MIN)]) {
      const rows = buildRows([p], t, null);
      expect(rows[0].justEnded).toBe(false);
      expect(ids(tabs(rows, t))).toMatchObject({ today: [], ended: ["1"] });
    }
  });

  it("counts from the end of the antisnipe window", () => {
    const p = post("1", auction("Ja"));
    const t = new Date(END + 5 * MIN + ENDED_GRACE_MS - 1); // 34:59 after the end time.
    const [r] = buildRows([p], t, null);
    expect(r).toMatchObject({ ended: true, justEnded: true, endedAtMs: END + 5 * MIN });
    expect(ids(tabs([r], t)).today).toEqual(["1"]);
  });

  it("marked ended by you: counts from the mark, in the tab it was in at the mark", () => {
    // Ends tomorrow, marked today: it was Upcoming when you marked it.
    const later = post("2", auction("Nei", "05.10.26 kl 21:00"));
    // No end time, marked: it was under No end.
    const unknown = post("3", "AUKSJON\nSluttid: snart");
    const mark = "2026-10-04T10:00:00Z";
    const marks = { "2": mark, "3": mark };
    const inside = new Date(Date.parse(mark) + 10 * MIN);
    const rows = buildRows([later, unknown], inside, null, { endedMarks: marks });
    expect(rows.map((r) => [r.ended, r.justEnded, r.endedAtMs])).toEqual([
      [true, true, Date.parse(mark)],
      [true, true, Date.parse(mark)],
    ]);
    expect(ids(tabs(rows, inside))).toMatchObject({ upcoming: ["2"], noend: ["3"] });
    expect(ids(tabs(rows, inside)).ended.sort()).toEqual(["2", "3"]);
    expect(countRows(rows, inside).active).toBe(0);

    const at30 = new Date(Date.parse(mark) + ENDED_GRACE_MS);
    const after = ids(tabs(buildRows([later, unknown], at30, null, { endedMarks: marks }), at30));
    expect(after).toMatchObject({ upcoming: [], noend: [] });
    expect(after.ended.sort()).toEqual(["2", "3"]);
  });

  it("a mark long after the end time doesn't restart the 30 min", () => {
    const p = post("1", auction());
    const t = new Date(END + 2 * 3_600_000);
    const [r] = buildRows([p], t, null, { endedMarks: { "1": new Date(END + 2 * 3_600_000 - MIN).toISOString() } });
    expect(r).toMatchObject({ ended: true, justEnded: false, endedAtMs: END });
  });

  it("New stays New for its window after the sale ended, then leaves New", () => {
    const p = post("1", auction(), { firstSeenAt: "2026-10-04T12:50:00Z", lastSeenAt: "2026-10-04T12:50:00Z" });
    const t = at("2026-10-04T13:05:00Z"); // Ended 5 min ago, first seen 15 min ago.
    const rows = buildRows([p], t, at("2026-10-04T12:55:00Z"));
    expect(ids(tabs(rows, t))).toMatchObject({ new: ["1"], today: ["1"], ended: ["1"] });
    expect(countRows(rows, t).isNew).toBe(0); // Ended: not counted in the numbers at the top.
    const t2 = at("2026-10-04T13:25:00Z"); // First seen 35 min ago (after the last visit), ended 25 min ago.
    expect(ids(tabs(buildRows([p], t2, at("2026-10-04T12:00:00Z")), t2))).toMatchObject({ new: ["1"], today: ["1"] });
    const t3 = at("2026-10-04T13:30:00Z"); // Ended 30 min ago: gone from New and Today.
    expect(ids(tabs(buildRows([p], t3, at("2026-10-04T12:00:00Z")), t3))).toMatchObject({ new: [], today: [], ended: ["1"] });
  });

  it("My bids: under Running for 30 min after the end, then under its Ended section", () => {
    const p = post("1", auction());
    const read = capture("1", auction(), [lot(1, [reply(ME, `${SELLER} 40`)])]);
    const extras = { captures: new Map([["1", read]]), myName: ME };
    const mineSections = (t: Date) => {
      const mine = tabs(buildRows([p], t, null, extras), t).find((x) => x.id === "mine")!;
      return Object.fromEntries(mine.sections.map((s) => [s.label, s.rows.map((r) => r.id)]));
    };
    expect(mineSections(new Date(END + 10 * MIN))).toEqual({ Running: ["1"] });
    expect(mineSections(new Date(END + ENDED_GRACE_MS))).toEqual({ Ended: ["1"] });
  });
});
