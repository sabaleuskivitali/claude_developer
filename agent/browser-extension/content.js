// content.js — Content Script
// Captures: DOM form fields (focus/blur), element clicks, XHR/fetch, form submits
// Privacy: no field values, no password fields, sensitive query params stripped

"use strict";

const SENSITIVE_PARAM_RE = /token|key|secret|auth|session|password|passwd|access_token/i;

function sanitizeUrl(url) {
  try {
    const u = new URL(url);
    for (const [k] of u.searchParams) {
      if (SENSITIVE_PARAM_RE.test(k)) u.searchParams.delete(k);
    }
    return u.toString();
  } catch {
    return url;
  }
}

function send(msg) {
  // Guard against invalidated extension context (page opened before extension loaded/reloaded).
  // chrome.runtime.id is undefined when the context is invalidated — accessing sendMessage
  // on undefined throws TypeError. Catch everything so the page is never affected.
  try {
    if (typeof chrome === "undefined" || !chrome.runtime?.id) return;
    chrome.runtime.sendMessage(msg).catch(() => {});
  } catch {
    // Extension context invalidated — ignore silently
  }
}

function elementInfo(el) {
  if (!el) return {};
  const label = findLabel(el);
  return {
    elementTag:   el.tagName?.toLowerCase(),
    elementId:    el.id || null,
    elementName:  el.name || null,
    elementLabel: label,
  };
}

function findLabel(el) {
  // 1. aria-label
  if (el.getAttribute("aria-label")) return el.getAttribute("aria-label");
  // 2. <label for="id">
  if (el.id) {
    const lbl = document.querySelector(`label[for="${el.id}"]`);
    if (lbl) return lbl.innerText.trim().slice(0, 100);
  }
  // 3. aria-labelledby
  const lbId = el.getAttribute("aria-labelledby");
  if (lbId) {
    const lbl = document.getElementById(lbId);
    if (lbl) return lbl.innerText.trim().slice(0, 100);
  }
  return null;
}

function formInfo(form) {
  if (!form) return {};
  return {
    formAction:     sanitizeUrl(form.action || ""),
    formFieldCount: form.elements.length,
  };
}

function classifyValue(value) {
  if (!value || value.trim() === "") return "EMPTY";
  if (/^\d+([.,]\d+)?$/.test(value))  return "NUMBER";
  if (!isNaN(Date.parse(value)))       return "DATE";
  if (/@/.test(value))                 return "EMAIL";
  return "TEXT";
}

// ---------------------------------------------------------------------------
// Focus / blur on input fields
// ---------------------------------------------------------------------------
document.addEventListener("focusin", (e) => {
  const el = e.target;
  if (!["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName)) return;
  if (el.type === "password") return;   // never capture password fields

  send({
    type:  "fieldFocus",
    url:   sanitizeUrl(location.href),
    ...elementInfo(el),
    ...formInfo(el.form),
  });
}, true);

document.addEventListener("focusout", (e) => {
  const el = e.target;
  if (!["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName)) return;
  if (el.type === "password") return;

  const value = el.value || "";
  send({
    type:        "fieldBlur",
    url:         sanitizeUrl(location.href),
    ...elementInfo(el),
    inputLength: value.length,
    inputType:   classifyValue(value),
    // No actual value transmitted
  });
}, true);

// ---------------------------------------------------------------------------
// Clicks
// ---------------------------------------------------------------------------
document.addEventListener("click", (e) => {
  const el = e.target?.closest("a, button, [role=button], [onclick], input[type=submit], input[type=button]");
  if (!el) return;

  const text = (el.innerText || el.value || el.getAttribute("aria-label") || "")
    .trim().slice(0, 100);

  send({
    type:         "elementClick",
    url:          sanitizeUrl(location.href),
    elementTag:   el.tagName?.toLowerCase(),
    elementId:    el.id || null,
    elementName:  el.name || null,
    elementLabel: text,
  });
}, true);

// ---------------------------------------------------------------------------
// Form submit
// ---------------------------------------------------------------------------
document.addEventListener("submit", (e) => {
  const form = e.target;
  send({
    type:  "formSubmit",
    url:   sanitizeUrl(location.href),
    ...formInfo(form),
  });
}, true);

// ---------------------------------------------------------------------------
// XHR interception
// ---------------------------------------------------------------------------
(function patchXhr() {
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url) {
    this._wdMethod = method;
    this._wdUrl    = url;
    return origOpen.apply(this, arguments);
  };

  XMLHttpRequest.prototype.send = function () {
    this.addEventListener("loadend", () => {
      try {
        const url = sanitizeUrl(new URL(this._wdUrl, location.href).toString());
        send({
          type:      "xhrRequest",
          url:       sanitizeUrl(location.href),
          xhrMethod: this._wdMethod,
          xhrStatus: this.status,
          xhrUrl:    url,
        });
      } catch { }
    });
    return origSend.apply(this, arguments);
  };
})();

// ---------------------------------------------------------------------------
// fetch() interception
// ---------------------------------------------------------------------------
(function patchFetch() {
  const origFetch = window.fetch;
  window.fetch = async function (input, init) {
    const method = (init?.method || "GET").toUpperCase();
    const url    = typeof input === "string" ? input : input?.url;
    try {
      const response = await origFetch.apply(this, arguments);
      try {
        send({
          type:      "xhrRequest",
          url:       sanitizeUrl(location.href),
          xhrMethod: method,
          xhrStatus: response.status,
          xhrUrl:    sanitizeUrl(new URL(url, location.href).toString()),
        });
      } catch { }
      return response;
    } catch (err) {
      throw err;
    }
  };
})();
