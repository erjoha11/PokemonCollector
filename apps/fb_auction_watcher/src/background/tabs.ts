// Waits for a tab to finish loading. Listens first, then checks the tab's current status, so a
// page that finished before the listener was added isn't missed (that wait would just time out).
export function waitForTabLoad(tabId: number, timeoutMs = 45_000): Promise<boolean> {
  return new Promise((resolve) => {
    let settled = false;
    const timer = setTimeout(() => done(false), timeoutMs);
    const listener = (id: number, info: { status?: string }) => {
      if (id === tabId && info.status === "complete") done(true);
    };
    function done(ok: boolean) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve(ok);
    }
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs
      .get(tabId)
      .then((tab) => {
        if (tab.status === "complete") done(true);
      })
      .catch(() => done(false)); // The tab is gone.
  });
}
