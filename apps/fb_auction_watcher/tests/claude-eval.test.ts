import { spawnSync } from "node:child_process";
import { describe, expect, it } from "vitest";
import { BID_CASES, END_TIME_CASES } from "../src/llm/cases";
import { bidRequest, endTimeRequest, type ClaudeRequest } from "../src/llm/prompts";

// Opt-in evaluation of the Claude prompts on src/llm/cases.ts, through the real bridge host
// (claude -p on your login). Uses your Claude plan, so it's skipped unless FBAW_EVAL=1:
//   npm run eval:claude

const enabled = process.env.FBAW_EVAL === "1";

function ask(request: ClaudeRequest): { results: { id: number; [k: string]: unknown }[] } {
  const body = Buffer.from(JSON.stringify(request));
  const header = Buffer.alloc(4);
  header.writeUInt32LE(body.length);
  const r = spawnSync("native/fbaw_claude_host.py", { input: Buffer.concat([header, body]), timeout: 300_000 });
  const reply = JSON.parse(r.stdout.subarray(4).toString());
  if (!reply.ok) throw new Error(reply.error);
  return reply.result;
}

describe.skipIf(!enabled)("Claude prompts on the evaluation cases", () => {
  it("end times", () => {
    const items = END_TIME_CASES.map((c, id) => ({ id, text: c.text, capturedAt: "2026-10-03T12:00:00Z" }));
    const got = new Map(ask(endTimeRequest(items)).results.map((r) => [r.id, r.endsAt]));
    const wrong = END_TIME_CASES.filter((c, id) => got.get(id) !== c.want).map((c) => `${c.text.slice(0, 50)} → ${got.get(END_TIME_CASES.indexOf(c))}`);
    expect(wrong).toEqual([]);
  }, 300_000);

  it("bids", () => {
    const items = BID_CASES.map((c, id) => ({ id, seller: c.seller, text: c.text }));
    const got = new Map(ask(bidRequest(items)).results.map((r) => [r.id, r.amount]));
    const wrong = BID_CASES.filter((c, id) => got.get(id) !== c.want).map((c) => `${c.text} → ${got.get(BID_CASES.indexOf(c))}`);
    expect(wrong).toEqual([]);
  }, 300_000);
});
