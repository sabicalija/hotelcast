const DEFAULT_API_BASE = "http://127.0.0.1:8000";

async function getApiBase() {
  const { apiBase = DEFAULT_API_BASE } = await chrome.storage.local.get("apiBase");
  return apiBase.replace(/\/+$/, "");
}

async function notifyUser(title, message, badgeText = "") {
  try {
    await chrome.action.setBadgeText({ text: badgeText });
    if (badgeText) {
      await chrome.action.setBadgeBackgroundColor({
        color: badgeText === "CAST" ? "#16a34a" : "#dc2626"
      });
    }
    await chrome.notifications.create({
      type: "basic",
      iconUrl: "icons/icon-128.png",
      title,
      message
    });
  } catch (err) {
    console.error("Notification error:", err);
  }
}

async function buildFallbackUri(mode, url, title) {
  const params = new URLSearchParams({ mode });
  if (url) {
    params.set("url", url);
  }
  if (title) {
    params.set("title", title);
  }
  return `hotelcast://cast?${params.toString()}`;
}

async function triggerCast(payload, fallbackTabId = null) {
  const apiBase = await getApiBase();
  try {
    const response = await fetch(`${apiBase}/api/cast`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const data = await response.json();
    if (!response.ok || data.status === "error") {
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    await chrome.storage.local.set({
      lastStatus: "playing",
      lastTarget: data.target || "",
      lastMode: payload.mode
    });
    await notifyUser(
      "HotelCast Active",
      `Streaming (${payload.mode}) to TV ${data.target || ""}`,
      "CAST"
    );
    return { ok: true, via: "api", data };
  } catch (apiErr) {
    // Fallback: launch desktop app via custom hotelcast:// URI scheme
    const uri = await buildFallbackUri(payload.mode, payload.url, payload.title);
    if (fallbackTabId) {
      try {
        await chrome.tabs.update(fallbackTabId, { url: uri });
      } catch (tabErr) {
        console.error("Failed to open hotelcast:// URI on tab:", tabErr);
      }
    }
    await chrome.storage.local.set({
      lastStatus: "launching",
      lastMode: payload.mode
    });
    await notifyUser(
      "Launching HotelCast",
      "Starting HotelCast desktop app via hotelcast:// protocol...",
      "CAST"
    );
    return { ok: true, via: "uri", uri, error: String(apiErr) };
  }
}

chrome.runtime.onInstalled.addListener(async () => {
  try {
    await chrome.contextMenus.removeAll();
    chrome.contextMenus.create({
      id: "hotelcast-cast-url",
      title: "Cast Video / Page to Hotel TV (HotelCast)",
      contexts: ["page", "video", "audio", "link"]
    });
    chrome.contextMenus.create({
      id: "hotelcast-mirror",
      title: "Mirror Chrome Window to Hotel TV (HotelCast)",
      contexts: ["page"]
    });
  } catch (err) {
    console.error("Failed to register context menus:", err);
  }
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const targetUrl = info.srcUrl || info.linkUrl || info.pageUrl || tab?.url || "";
  const title = tab?.title || "Chrome Tab";
  const tabId = tab?.id || null;

  if (info.menuItemId === "hotelcast-cast-url") {
    await triggerCast({ mode: "url", url: targetUrl, title }, tabId);
  } else if (info.menuItemId === "hotelcast-mirror") {
    await triggerCast({ mode: "mirror", url: targetUrl, title }, tabId);
  }
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  (async () => {
    try {
      if (message?.type === "CAST") {
        const res = await triggerCast(message.payload, message.tabId || null);
        sendResponse(res);
        return;
      }
      if (message?.type === "STOP") {
        const apiBase = await getApiBase();
        const response = await fetch(`${apiBase}/api/stop`, { method: "POST" });
        const data = await response.json();
        await chrome.storage.local.set({ lastStatus: "stopped" });
        await chrome.action.setBadgeText({ text: "" });
        sendResponse({ ok: response.ok, data });
        return;
      }
      sendResponse({ ok: false, error: "Unknown message type" });
    } catch (err) {
      sendResponse({ ok: false, error: String(err) });
    }
  })();
  return true;
});
