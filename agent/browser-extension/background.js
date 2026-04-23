// background.js — Service Worker (Manifest V3)
// Captures: page loads, SPA navigation, tab switches, XHR requests
// Forwards to native messaging host → WinDiagSvc EventStore

const HOST_NAME = "com.windiag.host";

let port        = null;
let reconnectMs = 200;    // start fast (domain: agent may not be ready at Chrome startup)
let reconnAttempts = 0;
let reconnTimer = null;

function connectNative() {
  clearTimeout(reconnTimer);
  try {
    port = chrome.runtime.connectNative(HOST_NAME);
    reconnectMs    = 200;   // reset on success
    reconnAttempts = 0;

    port.onDisconnect.addListener(() => {
      port = null;
      const _err = chrome.runtime.lastError;  // consumed to suppress noise
      reconnAttempts++;
      // Fast retries for the first 10 attempts (handles domain startup timing),
      // then exponential backoff up to 60 s
      reconnectMs = reconnAttempts <= 10
        ? Math.min(reconnectMs * 2, 2_000)
        : Math.min(reconnectMs * 2, 60_000);
      reconnTimer = setTimeout(connectNative, reconnectMs);
    });
  } catch {
    reconnAttempts++;
    reconnectMs = reconnAttempts <= 10
      ? Math.min(reconnectMs * 2, 2_000)
      : Math.min(reconnectMs * 2, 60_000);
    reconnTimer = setTimeout(connectNative, reconnectMs);
  }
}

function send(msg) {
  if (!port) {
    // Service worker was terminated and restarted — reconnect immediately.
    // This message is dropped but the next event (within the same wake-up) will go through.
    connectNative();
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
