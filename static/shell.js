function tickClock() {
  const el = document.getElementById("liveClock");
  if (!el) return;
  const now = new Date();
  const ist = new Date(now.toLocaleString("en-US", { timeZone: "Asia/Kolkata" }));
  el.textContent = "IST " + ist.toTimeString().slice(0, 8);
}

function syncModeToggle(mode) {
  const paper = document.getElementById("modePaper");
  const live = document.getElementById("modeLive");
  if (!paper || !live) return;
  const isPaper = String(mode || "live").toLowerCase() === "paper";
  paper.classList.toggle("active", isPaper);
  live.classList.toggle("active", !isPaper);
}

async function setDeskMode(mode) {
  const status = document.getElementById("engineControlStatus");
  if (mode === "live") {
    if (!confirm("Switch today's desk to LIVE? Real Dhan orders can be sent from Execute.")) return;
    if (window.HedgeKeys) {
      const state = HedgeKeys.expiryState(HedgeKeys.loadKeys());
      if (state.kind === "missing" || state.kind === "expired") {
        window.location.href = "/settings";
        return;
      }
      try { await HedgeKeys.syncToServer(); } catch (error) { /* live POST fails closed */ }
    }
  }
  const response = await fetch("/api/desk-mode", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    if (status) status.textContent = data.error || "Mode change blocked";
    return;
  }
  syncModeToggle(data.desk_mode || mode);
  if (status) {
    status.textContent = data.desk_mode === "paper" ? "Paper today" : "Live today";
  }
}

async function loadMode() {
  try {
    const response = await fetch("/api/status");
    const data = await response.json();
    syncModeToggle(data.desk_mode || (data.paper_trade ? "paper" : "live"));
  } catch (error) {
    syncModeToggle("paper");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  tickClock();
  setInterval(tickClock, 1000);
  loadMode();
  if (window.HedgeKeys) {
    HedgeKeys.renderBanner(document.getElementById("keyExpiryBanner"));
  }
});
