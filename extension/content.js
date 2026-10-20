const SOURCE = "omni-extractor";

function reply(payload) {
  window.postMessage(Object.assign({ source: SOURCE }, payload), window.location.origin);
}

reply({ type: "extension-available", version: chrome.runtime.getManifest().version });

window.addEventListener("message", (event) => {
  if (event.source !== window) return;
  if (event.origin !== window.location.origin) return;
  const data = event.data;
  if (!data || data.source !== SOURCE) return;

  if (data.type === "ping") {
    reply({ type: "extension-available", version: chrome.runtime.getManifest().version });
    return;
  }

  if (data.type !== "request-cookies") return;

  chrome.runtime.sendMessage({ type: "omni-extractor:get-cookies" }, (response) => {
    let ok = false;
    let blob = "";
    let error = "";
    if (chrome.runtime.lastError) {
      error = chrome.runtime.lastError.message || "The connector had an internal error.";
    } else if (!response) {
      error = "The connector did not respond.";
    } else if (!response.ok) {
      error = response.error || "The connector could not read your cookies.";
    } else {
      ok = true;
      blob = response.blob || "";
    }
    reply({ type: "cookies-result", ok, blob, error });
  });
});