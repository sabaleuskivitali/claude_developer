// background.js — Service Worker (Manifest V3)
// Captures: page loads, SPA navigation, tab switches, XHR requests
// Forwards to native messaging host → WinDiagSvc EventStore

const HOST_NAME = "com.windiag.host";

let port        = null;
let reconnectMs = 2000;   // start at 2 s, doubles on each failure, cap at 60 s
let reconnTimer = null;

function connectNative() {
  clearTimeout(reconnTimer);
  try {
    port = chrome.runtime.connectNative(HOST_NAME);
    reconnectMs = 2000;   // reset backoff on successful connect

    port.onDisconnect.addListener(() => {
      port = null;
      // chrome.runtime.lastError consumed here to suppress console noise
      const _err = chrome.runtime.lastError;
      reconnTimer = setTimeout(connectNative, reconnectMs);
      reconnectMs = Math.min(reconnectMs * 2, 60_000);
    });
  } catch {
    // connectNative itself failed (e.g. host manifest missing) — retry with backoff
    reconnTimer = setTimeout(connectNative, reconnectMs);
    reconnectMs = Math.min(reconnectMs * 2, 60_000);
  }
}

function send(msg) {
  if (!port) {
    // Not connected yet — drop the message rather than queuing unboundedly
    return;
  }
  try {
    port.postMessage(msg);
  } catch {
    port = null;
    reconnTimer = setTimeout(connectNative, reconnectMs);
    reconnectMs = Math.min(reconnectMs * 2, 60_000);
  }
}

function browserName() {
  return navigator?.userAgent?.includes("Edg/") ? "edge" : "chrome";
}

// ---------------------------------------------------------------------------
// Page load
// ---------------------------------------------------------------------------
chrome.webNavigation.onCompleted.addListener((details) => {
  if (details.frameId !== 0) return;
  send({
    type:      "pageLoad",
    browser:   browserName(),
    url:       details.url,
    tabId:     details.tabId,
  });
});

// ---------------------------------------------------------------------------
// SPA navigation (pushState / hash change)
// ---------------------------------------------------------------------------
chrome.webNavigation.onHistoryStateUpdated.addListener((details) => {
  if (details.frameId !== 0) return;
  send({
    type:    "navigation",
    browser: browserName(),
    url:     details.url,
    tabId:   details.tabId,
  });
});

// ---------------------------------------------------------------------------
// Tab activated
// ---------------------------------------------------------------------------
chrome.tabs.onActivated.addListener(async (activeInfo) => {
  try {
    const tab = await chrome.tabs.get(activeInfo.tabId);
    send({
      type:       "tabActivated",
      browser:    browserName(),
      url:        tab.url,
      pageTitle:  tab.title,
      tabId:      activeInfo.tabId,
    });
  } catch { }
});

// ---------------------------------------------------------------------------
// XHR / fetch interception via declarativeNetRequest is not available for this
// purpose in MV3. XHR events come from content.js via runtime messages.
// ---------------------------------------------------------------------------
chrome.runtime.onMessage.addListener((message, sender) => {
  if (!message || !message.type) return;
  // Tag with browser name and forward
  send({ ...message, browser: browserName() });
});

// Initial connection
connectNative();
