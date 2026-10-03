import { MSG_READ_POST, type ReadPostMessage } from "../shared/messages";

// Module 1 spike: clicking the toolbar icon asks the content script in the active tab
// to read the open post.
chrome.action.onClicked.addListener((tab) => {
  if (tab.id === undefined) return;
  const tabId = tab.id;
  const msg: ReadPostMessage = { type: MSG_READ_POST };
  chrome.tabs.sendMessage(tabId, msg).catch(() => {
    // Not a Facebook tab, or the tab was open before the extension was (re)loaded and
    // has no content script yet. A badge is the only feedback we can give from here.
    void chrome.action.setBadgeText({ tabId, text: "!" });
    void chrome.action.setTitle({ tabId, title: "Open a Facebook post and reload the tab, then try again." });
  });
});
