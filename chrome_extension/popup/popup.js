const API_BASE = "http://127.0.0.1:8000";

let activeTab = null;

function setFeedback(text) {
  const el = document.getElementById("feedback-msg");
  if (el) {
    el.textContent = text;
  }
}

function setServerState(online, statusText) {
  const dot = document.getElementById("server-dot");
  const pill = document.getElementById("status-pill");
  if (dot) {
    dot.classList.toggle("online", Boolean(online));
  }
  if (pill) {
    pill.textContent = statusText;
  }
}

async function checkLocalServer() {
  try {
    const resp = await fetch(`${API_BASE}/api/status`, { method: "GET" });
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
    }
    const data = await resp.json();
    const stateLabel = data.status === "playing"
      ? `Streaming (${data.target || "TV"})`
      : "Server Online";
    setServerState(true, stateLabel);
    return true;
  } catch (_err) {
    setServerState(false, "App Offline (URI Mode)");
    return false;
  }
}

async function sendCast(mode) {
  const url = activeTab?.url || "";
  const title = activeTab?.title || "Chrome Tab";
  const tabId = activeTab?.id || null;

  setFeedback(`Starting ${mode === "url" ? "URL cast" : "screen mirror"}...`);

  try {
    const resp = await fetch(`${API_BASE}/api/cast`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, url, title })
    });
    const data = await resp.json();
    if (!resp.ok || data.status === "error") {
      throw new Error(data.error || `HTTP ${resp.status}`);
    }
    setServerState(true, `Playing → ${data.target}`);
    setFeedback(`Streaming to TV at ${data.target}`);
  } catch (_err) {
    // Fallback to hotelcast:// custom URI scheme so Ubuntu launches HotelCast automatically
    const params = new URLSearchParams({ mode });
    if (url) {
      params.set("url", url);
    }
    if (title) {
      params.set("title", title);
    }
    const fallbackUri = `hotelcast://cast?${params.toString()}`;
    setFeedback("HotelCast server not running — launching via hotelcast:// URI...");
    if (tabId) {
      await chrome.tabs.update(tabId, { url: fallbackUri });
    } else {
      window.open(fallbackUri, "_blank");
    }
  }
}

async function stopCast() {
  setFeedback("Stopping stream...");
  try {
    const response = await chrome.runtime.sendMessage({ type: "STOP" });
    if (response?.ok) {
      setServerState(true, "Stopped");
      setFeedback("Stream stopped.");
    } else {
      setFeedback("Server is not currently active.");
    }
  } catch (err) {
    setFeedback(`Stop failed: ${err.message || err}`);
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    activeTab = tab || null;
    document.getElementById("tab-title").textContent = tab?.title || "No active tab";
    document.getElementById("tab-url").textContent = tab?.url || "";
  } catch (err) {
    document.getElementById("tab-title").textContent = "Unable to read tab";
    setFeedback(String(err));
  }

  await checkLocalServer();

  document.getElementById("btn-cast-url").addEventListener("click", async () => {
    await sendCast("url");
  });

  document.getElementById("btn-cast-mirror").addEventListener("click", async () => {
    await sendCast("mirror");
  });

  document.getElementById("btn-stop").addEventListener("click", async () => {
    await stopCast();
  });
});
