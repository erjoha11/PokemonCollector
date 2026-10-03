// A small in-memory `chrome` for tests of the service worker (review M8). Only what the worker
// uses, and only as much behaviour as the tests need: storage with change events, alarms you
// fire by hand, tabs that load at once (or never, per tab), idle state, and recorded badge and
// message calls. Install it per test:
//
//   const fake = fakeChrome();
//   vi.stubGlobal("chrome", fake.chrome);

type Listener<A extends unknown[]> = (...args: A) => unknown;

/** A chrome.events.Event: add/remove/has, plus `dispatch` for the test to fire it. */
export class FakeEvent<A extends unknown[]> {
  readonly listeners = new Set<Listener<A>>();
  addListener = (fn: Listener<A>) => void this.listeners.add(fn);
  removeListener = (fn: Listener<A>) => void this.listeners.delete(fn);
  hasListener = (fn: Listener<A>) => this.listeners.has(fn);
  hasListeners = () => this.listeners.size > 0;
  /** Calls every listener; returns what they returned. */
  dispatch(...args: A): unknown[] {
    return [...this.listeners].map((fn) => fn(...args));
  }
}

type Changes = Record<string, { oldValue?: unknown; newValue?: unknown }>;

function storageArea(name: "local" | "session", onChanged: FakeEvent<[Changes, string]>) {
  const data = new Map<string, unknown>();
  const copy = <T>(v: T): T => (v === undefined ? v : structuredClone(v));
  const pick = (keys?: string | string[] | Record<string, unknown> | null) => {
    if (keys === undefined || keys === null) return Object.fromEntries([...data].map(([k, v]) => [k, copy(v)]));
    const defaults = typeof keys === "string" ? { [keys]: undefined } : Array.isArray(keys) ? Object.fromEntries(keys.map((k) => [k, undefined])) : keys;
    const out: Record<string, unknown> = {};
    for (const [k, d] of Object.entries(defaults)) {
      if (data.has(k)) out[k] = copy(data.get(k));
      else if (d !== undefined) out[k] = d;
    }
    return out;
  };
  return {
    data,
    async get(keys?: string | string[] | Record<string, unknown> | null) {
      return pick(keys);
    },
    async set(items: Record<string, unknown>) {
      const changes: Changes = {};
      for (const [k, v] of Object.entries(items)) {
        changes[k] = { oldValue: copy(data.get(k)), newValue: copy(v) };
        data.set(k, copy(v));
      }
      onChanged.dispatch(changes, name);
    },
    async remove(keys: string | string[]) {
      const changes: Changes = {};
      for (const k of typeof keys === "string" ? [keys] : keys) {
        if (!data.has(k)) continue;
        changes[k] = { oldValue: data.get(k) };
        data.delete(k);
      }
      if (Object.keys(changes).length) onChanged.dispatch(changes, name);
    },
    async clear() {
      data.clear();
    },
  };
}

export type FakeTab = { id: number; url: string; active: boolean; status: "loading" | "complete"; windowId: number };

export type FakeChromeOptions = {
  /** Whether a new tab finishes loading at once (default) or stays "loading" until `loadTab`. */
  tabsLoad?: boolean;
  /** Whether tabs have a content script that answers tabs.sendMessage (default true). */
  contentScript?: boolean;
};

export function fakeChrome(options: FakeChromeOptions = {}) {
  const storageChanged = new FakeEvent<[Changes, string]>();
  const local = storageArea("local", storageChanged);
  const session = storageArea("session", storageChanged);

  const alarms = new Map<string, { name: string; scheduledTime: number }>();
  const onAlarm = new FakeEvent<[{ name: string; scheduledTime: number }]>();

  const tabs = new Map<number, FakeTab>();
  let nextTabId = 100;
  const onUpdated = new FakeEvent<[number, { status?: string }, FakeTab]>();
  const onRemoved = new FakeEvent<[number, { windowId: number; isWindowClosing: boolean }]>();
  /** Every tabs.create, in order (also the ones that failed). */
  const created: { url: string; active: boolean }[] = [];
  /** Every tabs.sendMessage that reached a tab. */
  const tabMessages: { tabId: number; message: unknown }[] = [];

  const onMessage = new FakeEvent<[unknown, { tab?: { id?: number } }, (response?: unknown) => void]>();
  /** Every runtime.sendMessage (to extension pages). */
  const runtimeMessages: unknown[] = [];

  /** Every badge/title call on the toolbar icon. */
  const badges: { call: "text" | "color" | "title"; tabId?: number; value: string }[] = [];

  const state = {
    idle: "active" as "active" | "idle" | "locked",
    tabsLoad: options.tabsLoad ?? true,
    contentScript: options.contentScript ?? true,
    /** Make the next tabs.create fail. */
    failNextCreate: false,
    /** What runtime.sendNativeMessage answers (the Claude bridge). */
    nativeReply: (_host: string, _msg: unknown): unknown => {
      throw new Error("Specified native messaging host not found.");
    },
  };

  const event = () => new FakeEvent<unknown[]>();

  const chrome = {
    storage: { local, session, onChanged: storageChanged },
    alarms: {
      async create(name: string, info: { when?: number; delayInMinutes?: number; periodInMinutes?: number }) {
        const when = info.when ?? Date.now() + (info.delayInMinutes ?? info.periodInMinutes ?? 0) * 60_000;
        alarms.set(name, { name, scheduledTime: when });
      },
      async clear(name: string) {
        return alarms.delete(name);
      },
      async get(name: string) {
        return alarms.get(name);
      },
      async getAll() {
        return [...alarms.values()];
      },
      onAlarm,
    },
    tabs: {
      async create(props: { url: string; active?: boolean }) {
        created.push({ url: props.url, active: props.active ?? true });
        if (state.failNextCreate) {
          state.failNextCreate = false;
          throw new Error("No current window");
        }
        const tab: FakeTab = { id: nextTabId++, url: props.url, active: props.active ?? true, status: state.tabsLoad ? "complete" : "loading", windowId: 1 };
        tabs.set(tab.id, tab);
        return { ...tab };
      },
      async get(tabId: number) {
        const tab = tabs.get(tabId);
        if (!tab) throw new Error(`No tab with id: ${tabId}.`);
        return { ...tab };
      },
      async update(tabId: number, props: Partial<FakeTab>) {
        const tab = tabs.get(tabId);
        if (!tab) throw new Error(`No tab with id: ${tabId}.`);
        Object.assign(tab, props);
        return { ...tab };
      },
      async remove(tabId: number) {
        if (!tabs.delete(tabId)) throw new Error(`No tab with id: ${tabId}.`);
        onRemoved.dispatch(tabId, { windowId: 1, isWindowClosing: false });
      },
      async reload(tabId: number) {
        if (!tabs.has(tabId)) throw new Error(`No tab with id: ${tabId}.`);
      },
      async query(q: { url?: string } = {}) {
        // Match patterns: only a trailing "*" wildcard is supported.
        const match = (url: string) => !q.url || (q.url.endsWith("*") ? url.startsWith(q.url.slice(0, -1)) : url === q.url);
        return [...tabs.values()].filter((t) => match(t.url)).map((t) => ({ ...t }));
      },
      async sendMessage(tabId: number, message: unknown) {
        if (!tabs.has(tabId) || !state.contentScript) throw new Error("Could not establish connection. Receiving end does not exist.");
        tabMessages.push({ tabId, message });
      },
      onUpdated,
      onRemoved,
    },
    windows: {
      async update() {},
    },
    idle: {
      async queryState(_detectionIntervalInSeconds: number) {
        return state.idle;
      },
    },
    action: {
      async setBadgeText(d: { tabId?: number; text: string }) {
        badges.push({ call: "text", tabId: d.tabId, value: d.text });
      },
      async setBadgeBackgroundColor(d: { tabId?: number; color: string }) {
        badges.push({ call: "color", tabId: d.tabId, value: d.color });
      },
      async setTitle(d: { tabId?: number; title: string }) {
        badges.push({ call: "title", tabId: d.tabId, value: d.title });
      },
    },
    runtime: {
      async sendMessage(message: unknown) {
        runtimeMessages.push(message);
      },
      async sendNativeMessage(host: string, message: unknown) {
        return state.nativeReply(host, message);
      },
      getURL: (path: string) => `chrome-extension://fake-id/${path}`,
      reload() {},
      onMessage,
      onInstalled: event(),
      onStartup: event(),
    },
    contextMenus: {
      removeAll(cb?: () => void) {
        cb?.();
      },
      create() {},
      onClicked: event(),
    },
  };

  return {
    /** Pass to `vi.stubGlobal("chrome", …)`. */
    chrome: chrome as unknown as typeof globalThis.chrome,
    state,
    local,
    session,
    alarms,
    tabs,
    created,
    tabMessages,
    runtimeMessages,
    badges,
    /** Fires an alarm (as Chrome would when it's due) and removes it. */
    fireAlarm(name: string) {
      const alarm = alarms.get(name);
      if (!alarm) throw new Error(`No alarm "${name}"`);
      alarms.delete(name);
      return Promise.all(onAlarm.dispatch(alarm));
    },
    /** A tab created with `tabsLoad: false` finishes loading now. */
    loadTab(tabId: number) {
      const tab = tabs.get(tabId)!;
      tab.status = "complete";
      onUpdated.dispatch(tabId, { status: "complete" }, { ...tab });
    },
    /** The user (or Chrome) closes a tab. */
    closeTab(tabId: number) {
      tabs.delete(tabId);
      onRemoved.dispatch(tabId, { windowId: 1, isWindowClosing: false });
    },
    /** A message to the service worker, as from a content script in `tabId` (or a page, without). Resolves with the response, if any. */
    sendToWorker(message: unknown, tabId?: number): Promise<unknown> {
      return new Promise((resolve) => {
        const results = onMessage.dispatch(message, tabId === undefined ? {} : { tab: { id: tabId } }, resolve);
        if (!results.includes(true)) resolve(undefined); // No async response coming.
      });
    },
  };
}

export type FakeChrome = ReturnType<typeof fakeChrome>;
