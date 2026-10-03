// Messages between the service worker and content scripts.

export const MSG_READ_POST = "fbaw/read-post" as const;

export type ReadPostMessage = { type: typeof MSG_READ_POST };

export function isReadPostMessage(msg: unknown): msg is ReadPostMessage {
  return typeof msg === "object" && msg !== null && (msg as { type?: unknown }).type === MSG_READ_POST;
}
