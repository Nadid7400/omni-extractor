import { toNetscape } from "./netscape.js";

const COOKIE_DOMAINS = ["youtube.com", "google.com"];
const ALLOWED_ORIGINS = ["https://omni-extractor.onrender.com", "http://localhost", "http://127.0.0.1"];

async function collectCookies() {
  const seen = new Set();
  const cookies = [];
  for (const domain of COOKIE_DOMAINS) {
    const found = await chrome.cookies.getAll({ domain });
    for (const cookie of found) {
      const key = `${cookie.domain}|${cookie.path}|${cookie.name}`;
      if (seen.has(key)) continue;
      seen.add(key);
      cookies.push(cookie);
    }
  }
  return toNetscape(cookies);
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.type !== "omni-extractor:get-cookies") return;
  const origin = sender.origin || (sender.tab && sender.tab.url) || "";
  if (!ALLOWED_ORIGINS.some((allowed) => origin.startsWith(allowed))) {
    sendResponse({ ok: false, error: "This page is not allowed to request your session." });
    return;
  }
  collectCookies()
    .then((blob) => sendResponse({ ok: true, blob }))
    .catch((error) => sendResponse({ ok: false, error: String(error) }));
  return true;
});