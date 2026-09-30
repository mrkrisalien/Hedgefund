(function (root) {
  const STORAGE_KEY = "hedge.desk.keys";
  const WARN_SECONDS = 12 * 60 * 60;

  function emptyKeys() {
    return {
      dhan_client_id: "",
      dhan_access_token: "",
      groq_api_key: "",
      deepseek_api_key: "",
      delta_api_key: "",
      delta_api_secret: "",
    };
  }

  function loadKeys() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return emptyKeys();
      return { ...emptyKeys(), ...JSON.parse(raw) };
    } catch (error) {
      return emptyKeys();
    }
  }

  function saveKeys(keys) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...emptyKeys(), ...keys }));
  }

  function clearKeys() {
    localStorage.removeItem(STORAGE_KEY);
  }

  function jwtExpiryUnix(token) {
    const parts = String(token || "").split(".");
    if (parts.length < 2) return null;
    try {
      const padded = parts[1].replace(/-/g, "+").replace(/_/g, "/");
      const json = JSON.parse(atob(padded));
      const exp = Number(json.exp || 0);
      return exp > 0 ? exp : null;
    } catch (error) {
      return null;
    }
  }

  function expiryState(keys) {
    const token = (keys || loadKeys()).dhan_access_token || "";
    const client = (keys || loadKeys()).dhan_client_id || "";
    const exp = jwtExpiryUnix(token);
    const now = Math.floor(Date.now() / 1000);
    if (!client || !token) {
      return { kind: "missing", message: "Dhan keys are not saved. Open Settings before starting live.", exp: null };
    }
    if (exp == null) {
      return { kind: "unknown", message: "Dhan token saved. Expiry could not be read from the JWT.", exp: null };
    }
    if (exp <= now) {
      return { kind: "expired", message: "Dhan access token expired. Generate a new token and save it in Settings.", exp };
    }
    const left = exp - now;
    const when = new Date(exp * 1000).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" });
    if (left <= WARN_SECONDS) {
      return { kind: "soon", message: `Dhan access token expires ${when} IST. Renew it in Settings.`, exp };
    }
    return { kind: "ok", message: `Dhan token valid until ${when} IST.`, exp };
  }

  function renderBanner(el) {
    if (!el) return expiryState();
    const state = expiryState();
    el.hidden = false;
    el.className = "key-banner key-" + state.kind;
    el.textContent = state.message;
    if (state.kind === "ok") el.hidden = true;
    return state;
  }

  async function syncToServer() {
    const keys = loadKeys();
    const state = expiryState(keys);
    if (state.kind === "missing") return state;
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(keys),
    });
    const data = await response.json();
    if (!data.ok) {
      state.kind = state.kind === "expired" ? "expired" : "error";
      state.message = data.error || state.message;
    }
    return state;
  }

  async function clearServer() {
    clearKeys();
    await fetch("/api/settings/clear", { method: "POST" });
  }

  root.HedgeKeys = {
    loadKeys,
    saveKeys,
    clearKeys,
    expiryState,
    renderBanner,
    syncToServer,
    clearServer,
    jwtExpiryUnix,
  };
})(window);
