/* TurboChickens coop SPA — dependency-free, hash-routed, polling every 2 s.
   The token lives in sessionStorage only and goes out as Authorization: Bearer.
   No cookies. All data comes from /api/* (thin mirrors of the daemon socket ops). */
(() => {
  "use strict";

  const TOKEN_KEY = "tc.coop.token";
  const POLL_MS = 2000;
  const VIEWS = ["dashboard", "runs", "jobs", "schedules", "stats", "audit", "settings"];

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  let token = sessionStorage.getItem(TOKEN_KEY);
  let pollTimer = null;
  let lastStats = {};

  /* --- API --- */

  async function api(path) {
    const headers = {};
    if (token) headers["Authorization"] = "Bearer " + token;
    let resp;
    try {
      resp = await fetch(path, { headers, cache: "no-store" });
    } catch (err) {
      return { __networkError: true };
    }
    if (resp.status === 401) {
      showLogin();
      return null;
    }
    try {
      return await resp.json();
    } catch (err) {
      return { __networkError: true };
    }
  }

  /* --- login view --- */

  function showLogin() {
    stopPolling();
    $$(".view").forEach((v) => { v.hidden = true; });
    $("#view-login").hidden = false;
    $("#signout").hidden = !token;
    const input = $("#token-input");
    input.focus();
  }

  function hideLogin() {
    $("#view-login").hidden = true;
    $("#signout").hidden = !token;
  }

  $("#login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const input = $("#token-input");
    const err = $("#login-error");
    const candidate = input.value.trim();
    if (!candidate) {
      err.textContent = "Enter the access token.";
      err.hidden = false;
      return;
    }
    token = candidate;
    const data = await api("/api/status");
    if (data === null) {
      token = null;
      sessionStorage.removeItem(TOKEN_KEY);
      err.textContent = "Token rejected — check the token printed at startup.";
      err.hidden = false;
      return;
    }
    sessionStorage.setItem(TOKEN_KEY, candidate);
    err.hidden = true;
    hideLogin();
    route();
  });

  $("#signout").addEventListener("click", () => {
    token = null;
    sessionStorage.removeItem(TOKEN_KEY);
    showLogin();
  });

  /* --- routing --- */

  function route() {
    const name = (location.hash.replace(/^#\/?/, "") || "dashboard").split("?")[0];
    const view = VIEWS.includes(name) ? name : "dashboard";
    $$(".view").forEach((v) => { v.hidden = true; });
    $("#view-" + view).hidden = false;
    $$(".nav a").forEach((a) => {
      if (a.dataset.view === view) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    if (view === "dashboard") startPolling();
    else stopPolling();
  }

  window.addEventListener("hashchange", () => {
    if ($("#view-login").hidden) route();
  });

  /* --- dashboard --- */

  function startPolling() {
    stopPolling();
    loadStatus();
    pollTimer = setInterval(loadStatus, POLL_MS);
  }

  function stopPolling() {
    if (pollTimer !== null) clearInterval(pollTimer);
    pollTimer = null;
  }

  function fmtUptime(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (d > 0) return d + "d " + h + "h";
    if (h > 0) return h + "h " + m + "m";
    if (m > 0) return m + "m " + s + "s";
    return s + "s";
  }

  function setStat(name, value) {
    const el = document.querySelector('[data-stat="' + name + '"]');
    if (!el) return;
    const text = String(value);
    if (lastStats[name] !== undefined && lastStats[name] !== text) {
      const card = el.closest(".stat");
      if (card) {
        card.classList.remove("flash");
        void card.offsetWidth; /* restart the one-shot flash */
        card.classList.add("flash");
      }
    }
    lastStats[name] = text;
    if (el.textContent !== text) el.textContent = text;
  }

  function setDiagram(id, value) {
    const el = $(id);
    if (el) el.textContent = String(value);
  }

  function setPill(up, text) {
    const pill = $("#daemon-pill");
    pill.classList.toggle("up", up);
    pill.classList.toggle("down", !up);
    $("#pill-text").textContent = text;
  }

  function showBanner(msg) {
    const b = $("#banner");
    b.textContent = msg;
    b.hidden = false;
  }

  async function loadStatus() {
    const data = await api("/api/status");
    if (data === null) return; /* 401 → login view shown */
    if (data.__networkError || !data.daemon || data.daemon.up === false) {
      const reason = data.error || "the GUI could not reach the supervisor";
      showBanner("coop · daemon not reachable — " + reason + ". The GUI is up; this banner clears when the socket answers again.");
      setPill(false, "coop · down");
      return;
    }
    $("#banner").hidden = true;
    const d = data.daemon;
    const inFlight = data.in_flight || [];
    const weight = inFlight.reduce((sum, r) => sum + (r.weight || 0), 0);
    const retention = data.retention || {};
    const sweep = retention.last_sweep;
    const retentionText =
      "audit " + (retention.audit_retention || "forever") +
      " · results " + (retention.results_retention_months != null ? retention.results_retention_months + " mo" : "—") +
      (sweep ? " · sweep " + (sweep.deleted || 0) + " expired" : "");

    setStat("uptime", fmtUptime(d.uptime_sec));
    setStat("queue_depth", data.queue_depth != null ? data.queue_depth : "—");
    setStat("in_flight", inFlight.length + " · " + weight + " / " + (data.weight_budget != null ? data.weight_budget : "?") + " wt");
    setStat("alerts", (data.alerts || []).length);
    setStat("schedules", data.schedules != null ? data.schedules : "—");
    setStat("retention", retentionText);
    setPill(true, "coop · up " + fmtUptime(d.uptime_sec));

    if (data.coop && data.coop.url) $("#coop-url").textContent = data.coop.url;

    setDiagram("#dg-queue", data.queue_depth != null ? data.queue_depth : 0);
    setDiagram("#dg-schedules", data.schedules != null ? data.schedules : 0);
    setDiagram("#dg-inflight", inFlight.length);
  }

  /* restart the diagram pulses on hover (CSS restarts on a new animation-name) */
  $("#diagram-card").addEventListener("mouseenter", () => {
    $$(".flow").forEach((line) => {
      line.style.animation = "none";
      void line.getBoundingClientRect();
      line.style.animation = "";
    });
  });

  /* --- boot --- */

  if (token) $("#signout").hidden = false;
  if (location.hash === "" ) location.hash = "#/dashboard";
  route();
})();
