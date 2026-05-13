/**
 * JobJarvis browser-extension popup script.
 *
 * Reads the current tab's URL, lets the user save it to their JobJarvis index.
 * Backend URL + JWT are saved to chrome.storage so the user only sets them once.
 */

const $url      = document.getElementById("url");
const $btn      = document.getElementById("save-btn");
const $result   = document.getElementById("result");
const $backend  = document.getElementById("backend");
const $token    = document.getElementById("token");

// Restore saved settings
chrome.storage.local.get(["backend", "token"], (data) => {
  $backend.value = data.backend || "http://localhost:8000";
  $token.value   = data.token   || "";
});

// Persist on change
[$backend, $token].forEach((el) => {
  el.addEventListener("change", () => {
    chrome.storage.local.set({
      backend: $backend.value.trim().replace(/\/$/, ""),
      token:   $token.value.trim(),
    });
  });
});

// Show current tab URL
chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
  const url = tabs[0]?.url || "";
  $url.textContent = url || "(no URL)";
});

$btn.addEventListener("click", async () => {
  $btn.disabled = true;
  $result.style.display = "none";

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const url   = tab?.url || "";
  const back  = $backend.value.trim().replace(/\/$/, "") || "http://localhost:8000";
  const token = $token.value.trim();

  if (!token) {
    showResult("error", "Set your JWT token first (find it in localStorage on JobJarvis).");
    $btn.disabled = false;
    return;
  }

  try {
    const r = await fetch(`${back}/api/extension/save_url`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization:  `Bearer ${token}`,
      },
      body: JSON.stringify({ url }),
    });
    const data = await r.json();
    if (!r.ok) {
      showResult("error", data?.detail || `Failed: ${r.status}`);
    } else if (data.action === "unsupported_ats") {
      showResult("unsupported", data.message);
    } else if (data.action === "exists") {
      showResult("ok", `✓ ${data.message}`);
    } else {
      showResult("ok", `✓ ${data.message}`);
    }
  } catch (e) {
    showResult("error", `Network error: ${e.message}`);
  } finally {
    $btn.disabled = false;
  }
});

function showResult(kind, message) {
  $result.style.display = "block";
  $result.className = "result " + kind;
  $result.textContent = message;
}
