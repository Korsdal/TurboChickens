/* TurboChickens coop SPA — dependency-free, hash-routed, polling every 2 s.
   The token lives in sessionStorage only and goes out as Authorization: Bearer.
   No cookies. All data comes from /api/* (thin mirrors of the daemon socket ops). */
(() => {
  "use strict";

  const TOKEN_KEY = "tc.coop.token";
  const POLL_MS = 2000;
  const VIEWS = ["dashboard", "runs", "jobs", "schedules", "stats", "audit", "settings"];
  /* views with a live /api loader (runs/schedules/stats land in specs 14/16/17) */
  const VIEW_LOADERS = { dashboard: loadStatus, jobs: loadJobs, audit: loadAudit, settings: loadSettings };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  let token = sessionStorage.getItem(TOKEN_KEY);
  let pollTimer = null;
  let lastStats = {};
  let auditFilter = null; // active event-type filter chip, null = all

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
    stopPolling();
    const loader = VIEW_LOADERS[view];
    if (loader) {
      loader();
      pollTimer = setInterval(loader, POLL_MS);
    }
  }

  window.addEventListener("hashchange", () => {
    if ($("#view-login").hidden) route();
  });

  /* --- dashboard --- */

  function stopPolling() {
    if (pollTimer !== null) clearInterval(pollTimer);
    pollTimer = null;
  }

  /* --- shared view helpers --- */

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function flashCard(card) {
    card.classList.remove("flash");
    void card.offsetWidth; /* restart the one-shot flash */
    card.classList.add("flash");
  }

  /* A payload is "down" when the network failed or the mirror reported the
     daemon socket unreachable — the view then renders its empty state. */
  function payloadDown(data) {
    return !data || data.__networkError || data.ok === false || (data.daemon && data.daemon.up === false);
  }

  function showEmpty(emptySel, cardSel, msg) {
    const card = $(cardSel);
    const empty = $(emptySel);
    if (card) card.hidden = true;
    if (empty) {
      empty.hidden = false;
      const p = empty.querySelector("p");
      if (p && msg) p.textContent = msg;
    }
  }

  function showCard(emptySel, cardSel) {
    const card = $(cardSel);
    const empty = $(emptySel);
    if (empty) empty.hidden = true;
    if (card) card.hidden = false;
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

  /* --- jobs view (spec 13): registered specs table --- */

  function shortHash(hash) {
    const full = "sha256:" + hash;
    return full.length > 27 ? full.slice(0, 27) + "…" : full;
  }

  async function loadJobs() {
    const data = await api("/api/jobs");
    if (data === null) return; /* 401 → login view shown */
    if (payloadDown(data)) {
      showEmpty("#jobs-empty", "#jobs-card", "Jobs unavailable — " + (data.error || "the supervisor is not reachable") + ".");
      return;
    }
    const jobs = data.jobs || [];
    if (!jobs.length) {
      showEmpty("#jobs-empty", "#jobs-card", "No registered jobs yet — run tc run <job.toml> once to register one.");
      return;
    }
    showCard("#jobs-empty", "#jobs-card");
    const tbody = $("#jobs-tbody");
    const card = $("#jobs-card");
    const sig = JSON.stringify(jobs);
    if (tbody.dataset.sig !== sig) {
      tbody.dataset.sig = sig;
      tbody.replaceChildren();
      for (const j of jobs) {
        const tr = el("tr");
        tr.appendChild(el("td", "mono job-name", j.name));
        const hashTd = el("td", "mono hash", shortHash(j.spec_hash));
        hashTd.title = "sha256:" + j.spec_hash;
        tr.appendChild(hashTd);
        tr.appendChild(el("td", "mono muted", j.registered_at || "—"));
        tbody.appendChild(tr);
      }
      flashCard(card);
    }
  }

  /* --- audit view (spec 13): mono tail, newest first, event filter chips --- */

  function renderAuditChips(types) {
    const row = $("#audit-chips");
    row.replaceChildren();
    const mk = (label, value) => {
      const b = el("button", "chip", label);
      b.type = "button";
      b.setAttribute("aria-pressed", String(auditFilter === value));
      b.addEventListener("click", () => {
        auditFilter = value;
        renderAuditChips(types);
        loadAudit();
      });
      row.appendChild(b);
    };
    mk("all", null);
    for (const t of types) mk(t, t);
  }

  function auditLine(row) {
    const line = el("div", "log-line mono");
    line.appendChild(el("span", "ts", row.ts || "—"));
    line.appendChild(el("span", "ev", row.event || "?"));
    const rest = { ...row };
    delete rest.ts;
    delete rest.event;
    line.appendChild(el("span", "detail", JSON.stringify(rest)));
    return line;
  }

  async function loadAudit() {
    // chips always come from an unfiltered tail; rows honor the active filter
    const chipData = await api("/api/audit?limit=200");
    if (chipData === null) return; /* 401 → login view shown */
    const types = [...new Set((chipData.events || []).map((e) => e.event).filter(Boolean))].sort();
    renderAuditChips(types);
    const data = auditFilter
      ? await api("/api/audit?limit=100&event=" + encodeURIComponent(auditFilter))
      : chipData;
    if (data === null) return;
    if (payloadDown(data)) {
      showEmpty("#audit-empty", "#audit-card", "Audit unavailable — " + (data.error || "the supervisor is not reachable") + ".");
      return;
    }
    const events = data.events || [];
    if (!events.length) {
      showEmpty("#audit-empty", "#audit-card", auditFilter ? "No audit events of type '" + auditFilter + "' yet." : "The audit log is empty.");
      return;
    }
    showCard("#audit-empty", "#audit-card");
    const log = $("#audit-log");
    const card = $("#audit-card");
    const sig = JSON.stringify(events);
    if (log.dataset.sig !== sig) {
      log.dataset.sig = sig;
      log.replaceChildren();
      for (const e of events) log.appendChild(auditLine(e));
      flashCard(card);
    }
  }

  /* --- settings view (spec 13): read-only rows of the effective settings --- */

  async function loadSettings() {
    const data = await api("/api/settings");
    if (data === null) return; /* 401 → login view shown */
    if (payloadDown(data)) {
      showEmpty("#settings-empty", "#settings-card", "Settings unavailable — " + (data.error || "the supervisor is not reachable") + ".");
      return;
    }
    const s = data.settings || {};
    const coop = s.coop || {};
    const rows = [
      ["data_dir", s.data_dir],
      ["socket_path", s.socket_path],
      ["db_path", s.db_path],
      ["audit_log_path", s.audit_log_path],
      ["results_dir", s.results_dir],
      ["quarantine_dir", s.quarantine_dir],
      ["seats_dir", s.seats_dir],
      ["providers_path", s.providers_path || "—"],
      ["weight_budget", String(s.weight_budget) + (s.weight_budget_override != null ? " (override)" : " (derived)")],
      ["audit_retention", s.audit_retention],
      ["results_retention_months", s.results_retention_months != null ? s.results_retention_months + " months" : "—"],
      ["coop", coop.enabled ? coop.url + " · auth " + (coop.auth_required ? "required" : "open") : "disabled"],
    ];
    showCard("#settings-empty", "#settings-card");
    const list = $("#settings-list");
    const card = $("#settings-card");
    const sig = JSON.stringify(rows);
    if (list.dataset.sig !== sig) {
      list.dataset.sig = sig;
      list.replaceChildren();
      for (const [k, v] of rows) {
        const row = el("div", "settings-row");
        row.appendChild(el("dt", null, k));
        row.appendChild(el("dd", null, v == null ? "—" : String(v)));
        list.appendChild(row);
      }
      flashCard(card);
    }
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
