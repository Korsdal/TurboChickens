/* TurboChickens coop SPA — dependency-free, hash-routed, polling every 2 s.
   The token lives in sessionStorage only and goes out as Authorization: Bearer.
   No cookies. All data comes from /api/* (thin mirrors of the daemon socket ops). */
(() => {
  "use strict";

  const TOKEN_KEY = "tc.coop.token";
  const POLL_MS = 2000;
  const VIEWS = ["dashboard", "runs", "jobs", "schedules", "stats", "audit", "settings", "auth", "farmhand", "connectors"];
  const VIEW_LOADERS = { dashboard: loadStatus, runs: loadRuns, jobs: loadJobs, schedules: loadSchedules, stats: loadStats, audit: loadAudit, settings: loadSettings, auth: loadAuth, farmhand: loadFarmhand, connectors: loadConnectors };

  const RUN_STATUSES = ["queued", "provisioning", "running", "collecting", "finalizing", "succeeded", "failed", "cancelled", "stopped"];
  // spec 30: terminal states — a terminal chicken can be re-run, a live one stopped
  const TERMINAL_RUN_STATES = ["succeeded", "failed", "cancelled", "stopped"];

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  /* The Fox only links https: an untrusted URI (e.g. a `javascript:` scheme
     injected by a hostile auth server) must never become an href in the coop
     origin — it would run with access to the sessionStorage bearer. */
  function httpsOnlyHref(uri) {
    try {
      const u = new URL(uri);
      return u.protocol === "https:" ? u.href : null;
    } catch (err) {
      return null;
    }
  }

  let token = sessionStorage.getItem(TOKEN_KEY);
  let pollTimer = null;
  let lastStats = {};
  let auditFilter = null; // active event-type filter chip, null = all
  let runsJobFilter = null; // active runs job filter chip, null = all
  let runsStatusFilter = null; // active runs status filter chip, null = all
  let selectedRunId = null; // open run detail pane
  let beakSelectedPath = null; // workspace file selected in the Beak tab (spec 29)
  let runsTab = "manifest";
  let lastRunSigs = {}; // run id -> signature, for the one-shot row flash
  let jobChipSig = "";

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

  async function apiPost(path, body) {
    const headers = { "Content-Type": "application/json" };
    if (token) headers["Authorization"] = "Bearer " + token;
    let resp;
    try {
      resp = await fetch(path, { method: "POST", headers, body: JSON.stringify(body), cache: "no-store" });
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

  async function apiDelete(path) {
    const headers = {};
    if (token) headers["Authorization"] = "Bearer " + token;
    let resp;
    try {
      resp = await fetch(path, { method: "DELETE", headers, cache: "no-store" });
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
      const reason = data.error || "the GUI could not reach coopd";
      showBanner("coop · coopd unreachable — " + reason + ". The GUI is up; this banner clears when the socket answers again.");
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
      "headcount " + (retention.audit_retention || "forever") +
      " · clutches " + (retention.results_retention_months != null ? retention.results_retention_months + " mo" : "—") +
      (sweep ? " · composted " + (sweep.deleted || 0) : "");

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

    renderRetentionCard(retention);
  }

  /* --- dashboard retention card (spec 17): summary + last sweep outcome ------ */

  function sweepError(msg) {
    const p = $("#sweep-error");
    if (!msg) {
      p.hidden = true;
      p.textContent = "";
      return;
    }
    p.textContent = msg;
    p.hidden = false;
  }

  function renderSweepOutcome(sweep) {
    const outcome = $("#sweep-outcome");
    const summary = $("#sweep-summary");
    const list = $("#sweep-list");
    if (!sweep) {
      outcome.hidden = true;
      list.replaceChildren();
      return;
    }
    const expired = sweep.expired || [];
    summary.textContent =
      "Compost turned at " + (sweep.cutoff || "—") + " (clutches older than " +
      (sweep.results_retention_months != null ? sweep.results_retention_months + " months" : "—") +
      ") — " + (sweep.deleted || 0) + " composted" + (expired.length ? ":" : ".");
    const sig = JSON.stringify(expired);
    if (list.dataset.sig !== sig) {
      list.dataset.sig = sig;
      list.replaceChildren();
      for (const e of expired) {
        const li = el("li");
        li.appendChild(el("span", "run", e.run_id + " "));
        li.appendChild(el("span", "kind", "(" + (e.kind || "clutch") + ", finished " + (e.finished_at || "—") + ")"));
        list.appendChild(li);
      }
    }
    outcome.hidden = false;
  }

  function renderRetentionCard(retention) {
    const months = retention.results_retention_months;
    const sweep = retention.last_sweep;
    $("#retention-summary").textContent =
      "headcount " + (retention.audit_retention || "forever") +
      " · clutches rot after " + (months != null ? months + " months" : "—") +
      (sweep ? " · last compost deleted " + (sweep.deleted || 0) : " · no compost yet");
    renderSweepOutcome(sweep || null);
  }

  $("#sweep-now").addEventListener("click", async () => {
    sweepError("");
    const btn = $("#sweep-now");
    btn.disabled = true;
    const resp = await apiPost("/api/retention/sweep", {});
    btn.disabled = false;
    if (resp === null) return; /* 401 → login view shown */
    if (resp.__networkError) {
      sweepError("The GUI could not reach the coop server.");
      return;
    }
    if (resp.daemon && resp.daemon.up === false) {
      sweepError("coopd is not reachable — " + (resp.error || "") + ".");
      return;
    }
    if (!resp.ok) {
      sweepError(resp.error || "compost refused");
      return;
    }
    renderSweepOutcome(resp); // the clutch.composted entries of this sweep, listed inline
    loadStatus();
  });

  /* --- jobs view (spec 13): registered specs table --- */

  function shortHash(hash) {
    const full = "sha256:" + hash;
    return full.length > 27 ? full.slice(0, 27) + "…" : full;
  }

  async function loadJobs() {
    const data = await api("/api/jobs");
    if (data === null) return; /* 401 → login view shown */
    if (payloadDown(data)) {
      showEmpty("#jobs-empty", "#jobs-card", "Eggs unavailable — " + (data.error || "coopd is not reachable") + ".");
      return;
    }
    const jobs = data.jobs || [];
    if (!jobs.length) {
      showEmpty("#jobs-empty", "#jobs-card", "No eggs yet — register one with tc run <job.toml>.");
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

  /* --- jobs view upload (spec 15): validate a bundle, then run it ---------- */

  let uploadValid = false; // the currently shown bundle passed validation
  let uploadSig = ""; // signature of the bundle that was validated
  let uploadBundle = null; // {files: {path: base64}} of the validated bundle

  function readFileB64(file) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => {
        const bytes = new Uint8Array(r.result);
        let bin = "";
        const CH = 0x8000;
        for (let i = 0; i < bytes.length; i += CH) {
          bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
        }
        resolve(btoa(bin));
      };
      r.onerror = () => reject(r.error);
      r.readAsArrayBuffer(file);
    });
  }

  function readFileText(file) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result));
      r.onerror = () => reject(r.error);
      r.readAsText(file);
    });
  }

  function b64Utf8(text) {
    const bytes = new TextEncoder().encode(text);
    let bin = "";
    const CH = 0x8000;
    for (let i = 0; i < bytes.length; i += CH) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
    }
    return btoa(bin);
  }

  /* --- spec 30: hatchery knobs — the upload form writes these into the egg's
         job.toml ([resources] / [harness]) before the bundle is candled, so the
         validator's field-named errors land under the matching field ---------- */

  function tomlSectionOf(line) {
    const m = line.match(/^\s*\[\s*([A-Za-z0-9_.-]+)\s*\]\s*(?:#.*)?$/);
    return m ? m[1] : null;
  }

  function getTomlKey(text, section, key) {
    let cur = null;
    for (const line of text.split("\n")) {
      const h = tomlSectionOf(line);
      if (h) { cur = h; continue; }
      if (cur === section) {
        const m = line.match(new RegExp("^\\s*" + key + "\\s*=\\s*(.*?)\\s*(?:#.*)?$"));
        if (m) return m[1];
      }
    }
    return null;
  }

  function setTomlKey(text, section, key, valueLine) {
    // valueLine === null removes the key from the section; a missing section is appended
    const keyRe = new RegExp("^\\s*" + key + "\\s*=");
    const out = [];
    let cur = null;
    let seen = false;
    for (const line of text.split("\n")) {
      const h = tomlSectionOf(line);
      if (h) {
        if (cur === section && !seen && valueLine !== null) {
          out.push(valueLine);
          seen = true;
        }
        cur = h;
        out.push(line);
        continue;
      }
      if (cur === section && keyRe.test(line)) {
        if (valueLine !== null) out.push(valueLine);
        seen = true;
        continue;
      }
      out.push(line);
    }
    let result = out.join("\n");
    if (!seen) {
      if (valueLine === null) return result;
      if (cur === section) {
        result = result + (result.endsWith("\n") || result === "" ? "" : "\n") + valueLine + "\n";
      } else {
        result = result + (result.endsWith("\n") || result === "" ? "" : "\n") + "[" + section + "]\n" + valueLine + "\n";
      }
    }
    return result;
  }

  function readKnobsFromToml(text) {
    const conn = getTomlKey(text, "harness", "connectors");
    let connectors = "";
    if (conn) {
      const inner = conn.trim().replace(/^\[/, "").replace(/\]$/, "");
      connectors = inner.split(",").map((s) => s.trim().replace(/^["']|["']$/g, "")).filter(Boolean).join(", ");
    }
    return {
      freeRange: getTomlKey(text, "resources", "free_range") === "true",
      timeout: getTomlKey(text, "resources", "timeout_sec") || "",
      maxCost: getTomlKey(text, "resources", "max_cost_usd") || "",
      spawnSubs: getTomlKey(text, "harness", "spawn_subs") === "true",
      connectors,
    };
  }

  function applyKnobs(text) {
    const timeout = $("#up-timeout").value.trim();
    const maxCost = $("#up-max-cost").value.trim();
    const connectors = $("#up-connectors").value.split(",").map((s) => s.trim()).filter(Boolean);
    let out = text;
    out = setTomlKey(out, "resources", "free_range", "free_range = " + ($("#up-free-range").checked ? "true" : "false"));
    out = setTomlKey(out, "resources", "timeout_sec", timeout ? "timeout_sec = " + timeout : null);
    out = setTomlKey(out, "resources", "max_cost_usd", maxCost ? "max_cost_usd = " + maxCost : null);
    out = setTomlKey(out, "harness", "spawn_subs", "spawn_subs = " + ($("#up-spawn-subs").checked ? "true" : "false"));
    out = setTomlKey(out, "harness", "connectors", connectors.length ? "connectors = [" + connectors.map((c) => JSON.stringify(c)).join(", ") + "]" : null);
    return out;
  }

  function knobSignature() {
    return ["up-free-range", "up-timeout", "up-max-cost", "up-spawn-subs", "up-connectors"]
      .map((id) => {
        const n = $("#" + id);
        return n.type === "checkbox" ? String(n.checked) : n.value;
      })
      .join("|");
  }

  function bundlePath(pathInput, file) {
    // in-bundle path: the typed path when sane, else the file's own name
    const typed = (pathInput && pathInput.value || "").trim().replace(/^\/+/, "");
    const parts = typed.split("/").filter((p) => p && p !== "." && p !== "..");
    return parts.length ? parts.join("/") : (file.webkitRelativePath || file.name);
  }

  async function buildBundle() {
    const jobFile = $("#up-job").files[0];
    const promptFile = $("#up-prompt").files[0];
    const schemaFile = $("#up-schema").files[0];
    if (!jobFile) return { error: "job.toml: choose the egg file first." };
    if (!promptFile) return { error: "input.prompt_file: choose the prompt file." };
    if (!schemaFile) return { error: "output.schema_file: choose the schema file." };
    const files = {};
    for (const f of Array.from($("#up-attachments").files)) {
      files[f.webkitRelativePath || f.name] = await readFileB64(f);
    }
    files[bundlePath($("#up-prompt-path"), promptFile)] = await readFileB64(promptFile);
    files[bundlePath($("#up-schema-path"), schemaFile)] = await readFileB64(schemaFile);
    // spec 30: the hatchery knobs are written into the egg before candling;
    // job.toml always wins the root key
    files["job.toml"] = b64Utf8(applyKnobs(await readFileText(jobFile)));
    return { files };
  }

  function bundleSignature() {
    // names + sizes + mtimes of the chosen files + typed bundle paths + the knob fields
    const sig = [];
    for (const id of ["up-job", "up-prompt", "up-schema", "up-attachments"]) {
      for (const f of Array.from($(id).files)) sig.push(id + ":" + f.name + ":" + f.size + ":" + f.lastModified);
    }
    sig.push($("#up-prompt-path").value, $("#up-schema-path").value);
    sig.push(knobSignature());
    return sig.join("|");
  }

  function uploadErrorField(field) {
    if (field.startsWith("input.prompt_file")) return "#up-prompt-error";
    if (field.startsWith("output.schema_file")) return "#up-schema-error";
    if (field.startsWith("input.attachments")) return "#up-attachments-error";
    // spec 30: hatchery-knob fields get their own inline errors
    if (field.startsWith("resources.free_range")) return "#up-free-range-error";
    if (field.startsWith("resources.timeout_sec")) return "#up-timeout-error";
    if (field.startsWith("resources.max_cost_usd")) return "#up-max-cost-error";
    if (field.startsWith("harness.spawn_subs")) return "#up-spawn-subs-error";
    if (field.startsWith("harness.connectors")) return "#up-connectors-error";
    return "#up-job-error"; // job.toml and every other spec-section error live under the spec field
  }

  function clearUploadErrors() {
    for (const sel of ["#up-job-error", "#up-prompt-error", "#up-schema-error", "#up-attachments-error",
      "#up-free-range-error", "#up-timeout-error", "#up-max-cost-error", "#up-spawn-subs-error", "#up-connectors-error", "#upload-error"]) {
      const p = $(sel);
      p.hidden = true;
      p.textContent = "";
    }
  }

  function showUploadErrors(resp) {
    clearUploadErrors();
    if (resp.errors && resp.errors.length) {
      const buckets = {};
      for (const e of resp.errors) {
        const sel = uploadErrorField(e.field || "");
        (buckets[sel] = buckets[sel] || []).push(e.message);
      }
      for (const [sel, msgs] of Object.entries(buckets)) {
        const p = $(sel);
        p.textContent = msgs.join("\n");
        p.hidden = false;
      }
    } else {
      const p = $("#upload-error");
      p.textContent = resp.error || "egg upload failed";
      p.hidden = false;
    }
  }

  function resetUploadState() {
    uploadValid = false;
    uploadSig = "";
    uploadBundle = null;
    $("#upload-submit").textContent = "Validate";
    const res = $("#upload-result");
    res.hidden = true;
    res.textContent = "";
  }

  for (const id of ["up-job", "up-prompt", "up-schema", "up-attachments", "up-prompt-path", "up-schema-path",
    "up-free-range", "up-timeout", "up-max-cost", "up-spawn-subs", "up-connectors"]) {
    $("#" + id).addEventListener("change", () => {
      resetUploadState();
      clearUploadErrors();
      // default the bundle path to the chosen file's name
      if (id === "up-prompt" && $("#up-prompt").files[0] && !$("#up-prompt-path").value) {
        $("#up-prompt-path").value = $("#up-prompt").files[0].webkitRelativePath || $("#up-prompt").files[0].name;
      }
      if (id === "up-schema" && $("#up-schema").files[0] && !$("#up-schema-path").value) {
        $("#up-schema-path").value = $("#up-schema").files[0].webkitRelativePath || $("#up-schema").files[0].name;
      }
      // spec 30: choosing the egg initializes the hatchery-knob fields from its TOML
      if (id === "up-job" && $("#up-job").files[0]) {
        readFileText($("#up-job").files[0]).then((text) => {
          const knobs = readKnobsFromToml(text);
          $("#up-free-range").checked = knobs.freeRange;
          $("#up-timeout").value = knobs.timeout;
          $("#up-max-cost").value = knobs.maxCost;
          $("#up-spawn-subs").checked = knobs.spawnSubs;
          $("#up-connectors").value = knobs.connectors;
        }).catch(() => { /* unreadable file — the knobs stay as typed */ });
      }
    });
  }

  $("#upload-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    clearUploadErrors();
    const built = await buildBundle();
    if (built.error) {
      const p = $(uploadErrorField(built.error.split(":")[0]));
      p.textContent = built.error;
      p.hidden = false;
      return;
    }
    const sig = bundleSignature();
    const alreadyValid = uploadValid && uploadSig === sig && uploadBundle;
    if (!alreadyValid) {
      // validate (the daemon stores the bundle only when valid)
      const resp = await apiPost("/api/jobs/validate", { files: built.files });
      if (resp === null) return; /* 401 → login view shown */
      if (resp.__networkError || (resp.daemon && resp.daemon.up === false)) {
        showUploadErrors({ error: "coopd is not reachable — " + (resp.error || "") + "." });
        return;
      }
      if (!resp.ok || resp.valid !== true) {
        showUploadErrors(resp);
        resetUploadState();
        return;
      }
      uploadValid = true;
      uploadSig = sig;
      uploadBundle = built;
      $("#upload-submit").textContent = "Queue egg";
      const res = $("#upload-result");
      res.textContent = "candled — " + resp.job_id + " · sha256:" + String(resp.spec_hash).slice(0, 12) + "…";
      res.hidden = false;
      return;
    }
    // already validated and unchanged → run the stored bundle over HTTP
    const resp = await apiPost("/api/runs", { files: uploadBundle.files });
    if (resp === null) return; /* 401 → login view shown */
    if (resp.__networkError || (resp.daemon && resp.daemon.up === false)) {
      showUploadErrors({ error: "coopd is not reachable — " + (resp.error || "") + "." });
      return;
    }
    if (!resp.ok) {
      if (resp.errors) showUploadErrors(resp);
      else showUploadErrors({ error: resp.error || "queueing failed" });
      resetUploadState();
      return;
    }
    resetUploadState();
    if (location.hash !== "#/runs") location.hash = "#/runs";
    selectRun(resp.run_id);
  });

  /* --- runs view (spec 14): filterable table + run detail pane ------------- */

  function statusDotInfo(r) {
    if (r.status === "succeeded") return { cls: "dot-succeeded", label: "succeeded" };
    if (r.status === "failed") {
      return r.result_valid === false
        ? { cls: "dot-quarantined", label: "quarantined" }
        : { cls: "dot-failed", label: "failed" };
    }
    if (r.status === "cancelled") return { cls: "dot-cancelled", label: "cancelled" }; // spec 30
    if (r.status === "stopped") return { cls: "dot-stopped", label: "stopped" }; // spec 30
    if (r.status === "queued") return { cls: "dot-queued", label: "queued" };
    return { cls: "dot-running", label: r.status }; // in flight: pulsing yolk
  }

  function renderChipRow(rowSel, items, active, onPick) {
    const row = $(rowSel);
    const sig = JSON.stringify([items, active]);
    if (row.dataset.sig === sig) return;
    row.dataset.sig = sig;
    row.replaceChildren();
    const mk = (label, value) => {
      const b = el("button", "chip", label);
      b.type = "button";
      b.setAttribute("aria-pressed", String(active === value));
      b.addEventListener("click", () => onPick(value));
      row.appendChild(b);
    };
    mk("all", null);
    for (const it of items) mk(it, it);
  }

  let childrenMap = {}; // spec 30: parent run id -> [sub-chicken ids], from the unfiltered runs list

  function feedCellText(r) {
    // spec 30: measured feed against the egg's cap — cost outliers visible at a glance
    if (r.cost_usd == null) return "—";
    let text = fmtCost(r.cost_usd);
    if (r.max_cost_usd != null) text += " / cap " + fmtCost(r.max_cost_usd);
    if (r.budget_outcome === "cost_cap") text += " · cost cap";
    return text;
  }

  function lineageNote(r) {
    // spec 30: parent/children indicator from parent_run_id
    const bits = [];
    if (r.parent_run_id) bits.push("sub of " + r.parent_run_id);
    const kids = childrenMap[r.id] || [];
    if (kids.length) bits.push("parent of " + kids.length);
    return bits.join(" · ");
  }

  function renderRunsTable(runs) {
    if (!runs.length) {
      const msg = runsJobFilter || runsStatusFilter
        ? "No chickens match the active filters."
        : "No chickens yet — queue an egg above, or with tc run <job.toml>.";
      showEmpty("#runs-empty", "#runs-card", msg);
      lastRunSigs = {};
      return;
    }
    showCard("#runs-empty", "#runs-card");
    const tbody = $("#runs-tbody");
    const sig = JSON.stringify(runs.map((r) => [r.id, r.status, r.result_valid, r.finished_at, r.attested, r.auth_mode, r.parent_run_id, r.status_reason, r.cost_usd, r.max_cost_usd, r.budget_outcome, JSON.stringify(childrenMap[r.id] || null)]));
    if (tbody.dataset.sig !== sig) {
      tbody.dataset.sig = sig;
      tbody.replaceChildren();
      const seen = new Set();
      for (const r of runs) {
        seen.add(r.id);
        const rowSig = JSON.stringify([r.status, r.result_valid, r.finished_at, r.attested, r.auth_mode, r.parent_run_id, r.status_reason, r.cost_usd, r.max_cost_usd, r.budget_outcome]);
        const changed = lastRunSigs[r.id] !== undefined && lastRunSigs[r.id] !== rowSig;
        lastRunSigs[r.id] = rowSig;
        const tr = el("tr", "run-row" + (r.id === selectedRunId ? " selected" : "") + (changed ? " flash" : ""));
        tr.dataset.runId = r.id;
        const idTd = el("td");
        const openBtn = el("button", "linklike mono run-open", r.id);
        openBtn.type = "button";
        openBtn.setAttribute("aria-label", "Open chicken " + r.id);
        openBtn.addEventListener("click", () => selectRun(r.id));
        idTd.appendChild(openBtn);
        const lin = lineageNote(r);
        if (lin) idTd.appendChild(el("div", "muted lineage-note", lin));
        tr.appendChild(idTd);
        tr.appendChild(el("td", "mono job-name", r.job_name));
        const dot = statusDotInfo(r);
        const stTd = el("td");
        const st = el("span", "status-cell");
        st.appendChild(el("span", "dot " + dot.cls));
        st.appendChild(el("span", "status-label", dot.label));
        stTd.appendChild(st);
        if (r.status_reason) stTd.appendChild(el("div", "muted status-reason", r.status_reason));
        tr.appendChild(stTd);
        tr.appendChild(el("td", "mono muted", r.trigger || "—"));
        tr.appendChild(el("td", "mono muted", r.auth_mode || "—")); // spec 19: per-run auth mode
        const feedTd = el("td", "mono muted feed-cell", feedCellText(r)); // spec 30: measured feed vs cap
        if (r.budget_outcome === "cost_cap") feedTd.classList.add("cost-cap");
        tr.appendChild(feedTd);
        tr.appendChild(el("td", "mono muted", r.started_at || "—"));
        tr.appendChild(el("td", "mono muted", r.finished_at || "—"));
        // spec 30: per-row lifecycle controls — stop a live chicken, re-run a terminal one
        const actTd = el("td", "run-actions");
        const terminal = TERMINAL_RUN_STATES.includes(r.status);
        const stopBtn = el("button", "linklike run-stop", r.status === "queued" ? "Cancel" : "Stop");
        stopBtn.type = "button";
        stopBtn.disabled = terminal;
        stopBtn.setAttribute("aria-label", (r.status === "queued" ? "Cancel queued egg " : "Stop chicken ") + r.id);
        stopBtn.addEventListener("click", () => stopRun(r.id));
        actTd.appendChild(stopBtn);
        const rerunBtn = el("button", "linklike run-rerun", "Re-run");
        rerunBtn.type = "button";
        rerunBtn.disabled = !terminal;
        rerunBtn.setAttribute("aria-label", "Re-run chicken " + r.id);
        rerunBtn.addEventListener("click", () => rerunRun(r.id));
        actTd.appendChild(rerunBtn);
        tr.appendChild(actTd);
        tr.addEventListener("click", (ev) => {
          if (ev.target.closest("button")) return; // the buttons handle themselves
          selectRun(r.id);
        });
        tbody.appendChild(tr);
      }
      for (const id of Object.keys(lastRunSigs)) if (!seen.has(id)) delete lastRunSigs[id];
    }
  }

  function runActionError(msg) {
    const p = $("#run-error");
    if (!msg) {
      p.hidden = true;
      p.textContent = "";
      return;
    }
    p.textContent = msg;
    p.hidden = false;
  }

  async function postRunAction(runId, actionPath) {
    runActionError("");
    const resp = await apiPost("/api/runs/" + encodeURIComponent(runId) + actionPath, {});
    if (resp === null) return null; /* 401 → login view shown */
    if (resp.__networkError) {
      runActionError("The GUI could not reach the coop server.");
      return null;
    }
    if (resp.daemon && resp.daemon.up === false) {
      runActionError("coopd is not reachable — " + (resp.error || "") + ".");
      return null;
    }
    if (!resp.ok) {
      runActionError(resp.error || (resp.errors && resp.errors.map((e) => e.field + ": " + e.message).join("\n")) || actionPath + " refused");
      return null;
    }
    return resp;
  }

  async function stopRun(runId) {
    const resp = await postRunAction(runId, "/stop");
    if (resp) loadRuns(); // the row flips to cancelled/stopped and flashes on change
  }

  async function rerunRun(runId) {
    const resp = await postRunAction(runId, "/rerun");
    if (resp && resp.run_id) {
      loadRuns();
      selectRun(resp.run_id); // open the fresh chicken
    } else if (resp) {
      loadRuns();
    }
  }

  function selectRun(runId) {
    selectedRunId = runId;
    beakSelectedPath = null; // a different chicken → a different workspace
    $("#run-detail").hidden = false;
    $$("#runs-tbody tr").forEach((tr) => tr.classList.toggle("selected", tr.dataset.runId === runId));
    loadRuns();
  }

  function setRunsTab(name) {
    runsTab = name;
    for (const t of ["manifest", "logs", "result", "beak"]) {
      $("#tab-" + t).setAttribute("aria-selected", String(t === name));
      $("#pane-" + t).hidden = t !== name;
    }
  }

  function renderDetail(show, logs, result, ws, wsFile) {
    const detail = $("#run-detail");
    if (!show || show.__networkError || show.ok === false || (show.daemon && show.daemon.up === false)) {
      detail.hidden = true;
      selectedRunId = null;
      return;
    }
    const run = show.run;
    detail.hidden = false;
    const dot = statusDotInfo(run);
    const d = $("#detail-dot");
    d.className = "dot " + dot.cls;
    $("#detail-title").textContent = run.id;
    const authMode = run.manifest && run.manifest.auth && run.manifest.auth.mode;
    $("#detail-job").textContent = run.job_name + " · " + run.trigger + " · " + run.status + (authMode ? " · auth " + authMode : "");
    $("#tab-result").textContent = run.result && run.result.valid === false ? "Quarantine" : "Clutch";

    // spec 30: lineage — the parent run link + any sub-chickens of this chicken
    const lin = $("#detail-lineage");
    lin.replaceChildren();
    const linBits = [];
    if (run.parent_run_id) {
      const p = el("button", "linklike mono lineage-link", "sub of " + run.parent_run_id);
      p.type = "button";
      p.setAttribute("aria-label", "Open parent chicken " + run.parent_run_id);
      p.addEventListener("click", () => selectRun(run.parent_run_id));
      linBits.push(p);
    }
    const kids = childrenMap[run.id] || [];
    if (kids.length) {
      for (const kid of kids) {
        const k = el("button", "linklike mono lineage-link", "sub " + kid);
        k.type = "button";
        k.setAttribute("aria-label", "Open sub-chicken " + kid);
        k.addEventListener("click", () => selectRun(kid));
        linBits.push(k);
      }
    }
    if (linBits.length) {
      lin.hidden = false;
      for (const b of linBits) {
        lin.appendChild(b);
        lin.appendChild(document.createTextNode(" "));
      }
    } else {
      lin.hidden = true;
    }

    // spec 30: measured feed against the egg's cap — cost_cap runs stay visible
    const budgetEl = $("#detail-budget");
    const usage = run.usage || (run.manifest && run.manifest.usage) || {};
    const budget = (run.manifest && run.manifest.budget) || {};
    const cost = usage.cost_usd != null ? usage.cost_usd : null;
    const cap = budget.max_cost_usd != null ? budget.max_cost_usd : null;
    if (cost != null) {
      budgetEl.hidden = false;
      let text = "feed " + fmtCost(cost) + (cap != null ? " / cap " + fmtCost(cap) : " · no cap");
      budgetEl.textContent = text;
      budgetEl.classList.toggle("cost-cap", budget.outcome === "cost_cap");
      if (budget.outcome === "cost_cap") budgetEl.textContent = text + " · cost cap hit";
    } else {
      budgetEl.hidden = true;
      budgetEl.textContent = "";
      budgetEl.classList.remove("cost-cap");
    }

    const manifestPre = $("#manifest-pre");
    const mSig = JSON.stringify(run);
    if (manifestPre.dataset.sig !== mSig) {
      manifestPre.dataset.sig = mSig;
      manifestPre.textContent = JSON.stringify(run, null, 2);
      $("#manifest-label").textContent = "manifest · spec " + shortHash((run.manifest && run.manifest.spec_hash) || "");
    }

    if (logs && !logs.__networkError && logs.ok !== false && !(logs.daemon && logs.daemon.up === false)) {
      const logEl = $("#run-log");
      const lSig = JSON.stringify(logs.events || []);
      if (logEl.dataset.sig !== lSig) {
        const nearBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 40;
        logEl.dataset.sig = lSig;
        logEl.replaceChildren();
        for (const e of logs.events || []) {
          const line = el("div", "log-line mono");
          line.appendChild(el("span", "ts", e.ts || "—"));
          line.appendChild(el("span", "ev", "[" + (e.level || "") + "] " + (e.source || "")));
          line.appendChild(el("span", "detail", e.line || ""));
          logEl.appendChild(line);
        }
        if (nearBottom) logEl.scrollTop = logEl.scrollHeight; // follow live output only when already at the end
      }
    }

    renderResultPane(run, result);
    renderBeakPane(run, logs, ws, wsFile);
  }

  /* --- spec 29: the Beak — live workspace tree + file peek + log tail ------- */

  const BEAK_TAIL_LINES = 40;

  function beakLogLine(e) {
    const line = el("div", "log-line mono");
    line.appendChild(el("span", "ts", e.ts || "—"));
    line.appendChild(el("span", "ev", "[" + (e.level || "") + "] " + (e.source || "")));
    line.appendChild(el("span", "detail", e.line || ""));
    return line;
  }

  function isBeakPrivate(path) {
    return path.split("/")[0] === ".vibe" || path === ".tc-farmhand-settings.toml";
  }

  function renderBeakPane(run, logs, ws, wsFile) {
    const state = $("#beak-state");
    const grid = $("#beak-grid");
    const tree = $("#beak-tree");
    const pre = $("#beak-pre");
    const label = $("#beak-label");
    state.replaceChildren();
    state.hidden = true;
    const note = (text, neutral) => {
      state.hidden = false;
      state.appendChild(el("div", "quarantine-note" + (neutral ? " neutral-note" : ""), text));
    };

    // workspace tree (polled with the rest of the detail every 2 s)
    if (!ws || ws.__networkError || (ws.daemon && ws.daemon.up === false)) {
      label.textContent = "workspace unavailable";
      grid.hidden = true;
      note("coopd is not reachable — the beak cannot see the workspace right now.", true);
    } else if (ws.ok === false) {
      // terminal run (abattoir removed the workspace) or not hatched yet — the honest empty state
      label.textContent = "beak closed";
      grid.hidden = true;
      note(ws.error || "The beak is closed for this chicken.", true);
    } else {
      grid.hidden = false;
      const entries = (ws.tree && ws.tree.entries) || [];
      const sig = JSON.stringify(entries);
      if (tree.dataset.sig !== sig) {
        const hadBefore = tree.dataset.sig !== undefined;
        tree.dataset.sig = sig;
        tree.replaceChildren();
        for (const e of entries) {
          const li = el("li", "beak-entry");
          if (e.type === "dir") {
            li.appendChild(el("span", "beak-dir mono", e.path + "/"));
          } else if (e.type === "symlink") {
            li.appendChild(el("span", "beak-dir mono", e.path + " → (link)"));
          } else {
            const b = el("button", "linklike mono beak-file" + (e.path === beakSelectedPath ? " selected" : ""), e.path);
            b.type = "button";
            if (isBeakPrivate(e.path)) {
              b.disabled = true;
              b.classList.add("beak-private");
              b.title = "The chicken's private harness config — the beak never serves this file";
              li.appendChild(b);
            } else {
              b.setAttribute("aria-label", "Peek at workspace file " + e.path);
              b.addEventListener("click", () => {
                beakSelectedPath = e.path;
                loadRuns();
              });
              li.appendChild(b);
              li.appendChild(el("span", "muted beak-size", e.size_bytes + " B"));
            }
          }
          tree.appendChild(li);
        }
        if (hadBefore) flashCard(grid); // one-shot flash on change
      }
      if (ws.tree && ws.tree.truncated) note("Tree truncated at " + ws.tree.max_entries + " entries — the workspace is bigger.", true);
      label.textContent = "workspace · " + (ws.tree ? ws.tree.n_entries : 0) + " entries · " + (ws.tree ? ws.tree.total_bytes : 0) + " B" + (run.status ? " · " + run.status : "");
    }

    // single-file peek
    pre.dataset.sig = "";
    pre.textContent = "";
    if (beakSelectedPath && ws && ws.ok === true) {
      if (wsFile && wsFile.__networkError) {
        note("coopd is not reachable — the file peek failed.", true);
      } else if (wsFile && wsFile.ok === true) {
        pre.dataset.sig = JSON.stringify(wsFile);
        pre.textContent = wsFile.content;
        if (wsFile.truncated) {
          note("Truncated — showing the first " + wsFile.served_bytes + " of " + wsFile.size_bytes + " bytes (cap " + wsFile.cap_bytes + " B).", true);
        }
      } else if (wsFile) {
        note(wsFile.error || "The beak refused that file.", true);
      }
    }

    // live log tail (same runs_logs payload as the Logs tab, last lines)
    const blog = $("#beak-log");
    if (logs && !logs.__networkError && logs.ok !== false && !(logs.daemon && logs.daemon.up === false)) {
      const tail = (logs.events || []).slice(-BEAK_TAIL_LINES);
      const bSig = JSON.stringify(tail);
      if (blog.dataset.sig !== bSig) {
        blog.dataset.sig = bSig;
        blog.replaceChildren();
        for (const e of tail) blog.appendChild(beakLogLine(e));
        blog.scrollTop = blog.scrollHeight;
      }
    }
  }

  function renderResultPane(run, result) {
    const pre = $("#result-pre");
    const state = $("#result-state");
    const label = $("#result-label");
    pre.dataset.sig = "";
    pre.textContent = "";
    state.replaceChildren();
    state.hidden = true;
    if (!result || result.__networkError || (result.daemon && result.daemon.up === false)) {
      label.textContent = "clutch unavailable";
      return;
    }
    if (result.ok === true && result.output !== undefined) {
      label.textContent = "validated clutch";
      pre.dataset.sig = JSON.stringify(result.output);
      pre.textContent = JSON.stringify(result.output, null, 2);
      return;
    }
    if (result.quarantined) {
      label.textContent = "quarantined clutch";
      state.hidden = false;
      const note = el("div", "quarantine-note");
      note.textContent = "Quarantined — the clutch failed candling (schema validation). It is kept for inspection and never served as a valid clutch.";
      state.appendChild(note);
      if (result.schema_errors && result.schema_errors.length) {
        const ul = el("ul", "schema-errors mono");
        for (const e of result.schema_errors) ul.appendChild(el("li", null, e));
        state.appendChild(ul);
      }
      pre.dataset.sig = "raw:" + (result.raw_output || "");
      pre.textContent = result.raw_output || "";
      return;
    }
    if (result.expired) {
      label.textContent = "clutch";
      state.hidden = false;
      const note = el("div", "quarantine-note neutral-note");
      note.textContent = "Composted — the on-disk clutch was removed by the compost sweep; the append-only index row remains.";
      state.appendChild(note);
      return;
    }
    label.textContent = "clutch";
    state.hidden = false;
    const note = el("div", "quarantine-note neutral-note");
    note.textContent = "No clutch yet — the chicken never reached collecting (it may still be running).";
    state.appendChild(note);
  }

  async function loadRunDetail(runId) {
    const enc = encodeURIComponent(runId);
    const fetches = [
      api("/api/runs/" + enc),
      api("/api/runs/" + enc + "/logs"),
      api("/api/results/" + enc),
      api("/api/runs/" + enc + "/workspace"), // spec 29: the Beak — workspace tree
    ];
    if (beakSelectedPath) {
      fetches.push(api("/api/runs/" + enc + "/workspace?path=" + encodeURIComponent(beakSelectedPath)));
    }
    const [show, logs, result, ws, wsFile] = await Promise.all(fetches);
    if (show === null) return; /* 401 → login view shown */
    renderDetail(show, logs, result, ws, wsFile || null);
  }

  async function loadRuns() {
    const jobsData = await api("/api/jobs");
    if (jobsData === null) return; /* 401 → login view shown */
    if (!jobsData.__networkError && jobsData.ok !== false) {
      const names = (jobsData.jobs || []).map((j) => j.name).sort();
      const sig = JSON.stringify(names);
      if (jobChipSig !== sig) {
        jobChipSig = sig;
        if (runsJobFilter && !names.includes(runsJobFilter)) runsJobFilter = null;
      }
      renderChipRow("#runs-job-chips", names, runsJobFilter, (v) => {
        runsJobFilter = v;
        loadRuns();
      });
    }
    renderChipRow("#runs-status-chips", RUN_STATUSES, runsStatusFilter, (v) => {
      runsStatusFilter = v;
      loadRuns();
    });
    const qs = new URLSearchParams();
    if (runsJobFilter) qs.set("job", runsJobFilter);
    if (runsStatusFilter) qs.set("status", runsStatusFilter);
    const data = await api("/api/runs" + (qs.toString() ? "?" + qs.toString() : ""));
    if (data === null) return; /* 401 → login view shown */
    if (payloadDown(data)) {
      showEmpty("#runs-empty", "#runs-card", "Chickens unavailable — " + (data.error || "coopd is not reachable") + ".");
      return;
    }
    // spec 30: lineage (parent/children) needs the unfiltered list — reuse the
    // filtered response when no filter is active
    const allData = qs.toString() ? await api("/api/runs") : data;
    if (allData && !payloadDown(allData)) {
      const map = {};
      for (const r of allData.runs || []) {
        if (r.parent_run_id) (map[r.parent_run_id] = map[r.parent_run_id] || []).push(r.id);
      }
      childrenMap = map;
    }
    renderRunsTable(data.runs || []);
    if (selectedRunId) await loadRunDetail(selectedRunId);
  }

  $("#run-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const input = $("#run-spec-path");
    const err = $("#run-error");
    const specPath = input.value.trim();
    if (!specPath) {
      err.textContent = "Enter the in-container path of an egg (job.toml).";
      err.hidden = false;
      return;
    }
    const resp = await apiPost("/api/runs", { spec_path: specPath });
    if (resp === null) return; /* 401 → login view shown */
    if (resp.__networkError) {
      err.textContent = "The GUI could not reach the coop server.";
      err.hidden = false;
      return;
    }
    if (resp.daemon && resp.daemon.up === false) {
      err.textContent = "coopd is not reachable — " + (resp.error || "") + ".";
      err.hidden = false;
      return;
    }
    if (!resp.ok) {
      err.textContent = resp.error || "queueing failed";
      err.hidden = false;
      return;
    }
    err.hidden = true;
    input.value = "";
    selectRun(resp.run_id);
  });

  $("#detail-close").addEventListener("click", () => {
    selectedRunId = null;
    beakSelectedPath = null;
    $("#run-detail").hidden = true;
    $$("#runs-tbody tr").forEach((tr) => tr.classList.remove("selected"));
  });

  for (const t of ["manifest", "logs", "result", "beak"]) {
    $("#tab-" + t).addEventListener("click", () => setRunsTab(t));
  }

  $$(".btn-copy").forEach((b) => {
    b.addEventListener("click", async () => {
      const pre = document.getElementById(b.dataset.copyTarget);
      const text = pre ? pre.textContent : "";
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text);
      } catch (err) {
        const ta = el("textarea");
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
      }
      const old = b.textContent;
      b.textContent = "Copied";
      setTimeout(() => { b.textContent = old; }, 1200);
    });
  });

  /* --- schedules view (spec 16): table + add form (pick a spec) + remove --- */

  let schedSpecSig = ""; // signature of the spec select, for one-shot re-render
  let schedRowSig = ""; // signature of the table body, for the one-shot flash

  function schedError(msg) {
    const p = $("#sched-error");
    if (!msg) {
      p.hidden = true;
      p.textContent = "";
      return;
    }
    p.textContent = msg;
    p.hidden = false;
  }

  function renderSpecSelect(jobs) {
    const sel = $("#sched-spec");
    const options = jobs.filter((j) => j.spec_path).map((j) => ({ value: j.spec_path, label: j.name + " · " + shortHash(j.spec_hash), hash: j.spec_hash }));
    const sig = JSON.stringify(options);
    if (schedSpecSig === sig) return;
    schedSpecSig = sig;
    const previous = sel.value;
    sel.replaceChildren();
    if (!options.length) {
      const o = el("option", null, "No registered eggs with a known path");
      o.value = "";
      sel.appendChild(o);
      sel.disabled = true;
      $("#sched-submit").disabled = true;
      return;
    }
    sel.disabled = false;
    $("#sched-submit").disabled = false;
    for (const opt of options) {
      const o = el("option", null, opt.label);
      o.value = opt.value;
      o.dataset.specHash = opt.hash;
      sel.appendChild(o);
    }
    if (options.some((o) => o.value === previous)) sel.value = previous;
  }

  function renderSchedulesTable(schedules) {
    if (!schedules.length) {
      showEmpty("#schedules-empty", "#schedules-card", "The rooster has no crows yet — pick a cron-triggered egg above, or tc schedule add <job.toml>.");
      schedRowSig = "";
      return;
    }
    showCard("#schedules-empty", "#schedules-card");
    const tbody = $("#schedules-tbody");
    const card = $("#schedules-card");
    const sig = JSON.stringify(schedules.map((s) => [s.job_id, s.schedule, s.next_run, s.added_at]));
    if (tbody.dataset.sig === sig) return;
    tbody.dataset.sig = sig;
    tbody.replaceChildren();
    for (const s of schedules) {
      const tr = el("tr");
      tr.appendChild(el("td", "mono job-name", s.job_id));
      tr.appendChild(el("td", "mono", s.schedule));
      tr.appendChild(el("td", "mono", s.next_run || "—"));
      tr.appendChild(el("td", "mono muted", s.added_at || "—"));
      const actionTd = el("td");
      const rm = el("button", "linklike sched-remove", "Remove");
      rm.type = "button";
      rm.setAttribute("aria-label", "Remove schedule for " + s.job_id);
      rm.addEventListener("click", () => removeSchedule(s.job_id));
      actionTd.appendChild(rm);
      tr.appendChild(actionTd);
      tbody.appendChild(tr);
    }
    flashCard(card);
  }

  async function loadSchedules() {
    const jobsData = await api("/api/jobs");
    if (jobsData === null) return; /* 401 → login view shown */
    if (!payloadDown(jobsData)) renderSpecSelect(jobsData.jobs || []);
    const data = await api("/api/schedule");
    if (data === null) return; /* 401 → login view shown */
    if (payloadDown(data)) {
      schedError("");
      showEmpty("#schedules-empty", "#schedules-card", "Rooster unavailable — " + (data.error || "coopd is not reachable") + ".");
      return;
    }
    renderSchedulesTable(data.schedules || []);
  }

  $("#sched-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    schedError("");
    const sel = $("#sched-spec");
    const specPath = sel.value;
    if (!specPath) {
      schedError("No registered egg to pick — queue or upload a cron-triggered egg first (Eggs view).");
      return;
    }
    const opt = sel.selectedOptions[0];
    const body = { spec_path: specPath };
    if (opt && opt.dataset.specHash) body.spec_hash = opt.dataset.specHash;
    const resp = await apiPost("/api/schedule", body);
    if (resp === null) return; /* 401 → login view shown */
    if (resp.__networkError) {
      schedError("The GUI could not reach the coop server.");
      return;
    }
    if (resp.daemon && resp.daemon.up === false) {
      schedError("coopd is not reachable — " + (resp.error || "") + ".");
      return;
    }
    if (!resp.ok) {
      // manual-trigger spec, invalid cron, hash mismatch, unreadable spec — inline
      schedError(resp.error || (resp.errors && resp.errors.map((e) => e.message).join("\n")) || "crow refused");
      return;
    }
    loadSchedules();
  });

  async function removeSchedule(jobId) {
    schedError("");
    const resp = await apiDelete("/api/schedule/" + encodeURIComponent(jobId));
    if (resp === null) return; /* 401 → login view shown */
    if (resp.__networkError) {
      schedError("The GUI could not reach the coop server.");
      return;
    }
    if (resp.daemon && resp.daemon.up === false) {
      schedError("coopd is not reachable — " + (resp.error || "") + ".");
      return;
    }
    if (!resp.ok) {
      schedError(resp.error || "crow removal refused");
      return;
    }
    loadSchedules();
  }

  /* --- stats view (spec 17): per-job cards + per-job detail ------------------- */

  let statsDetailJob = null; // job whose detail card is open

  function fmtNum(v) {
    if (v == null) return "—";
    const n = Number(v);
    if (!isFinite(n)) return String(v);
    return n.toPrecision(6).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
  }

  function fmtCost(v) {
    return v == null ? "—" : "$" + fmtNum(v);
  }

  function statsTriple(mean, p50, p95) {
    return fmtNum(mean) + " · " + fmtNum(p50) + " · " + fmtNum(p95);
  }

  function renderStatsCards(stats) {
    if (!stats.length) {
      showEmpty("#stats-empty", "#stats-grid", "No feed recorded yet — let a chicken finish and its rollup lands here.");
      return;
    }
    showCard("#stats-empty", "#stats-grid");
    const grid = $("#stats-grid");
    const sig = JSON.stringify(stats);
    if (grid.dataset.sig === sig) return;
    grid.dataset.sig = sig;
    grid.replaceChildren();
    for (const s of stats) {
      const card = el("div", "card stat-card");
      const head = el("button", "job-head");
      head.type = "button";
      head.setAttribute("aria-label", "Show feed ledger for " + s.job_id);
      head.appendChild(el("span", "job-name", s.job_name || s.job_id));
      head.appendChild(el("span", "job-id mono", s.job_id));
      head.addEventListener("click", () => selectStatsJob(s.job_id));
      card.appendChild(head);
      const nums = el("div", "stat-numerals");
      const cell = (label, value, small) => {
        const n = el("div", "num");
        n.appendChild(el("span", "num-label", label));
        n.appendChild(el("span", "num-value mono" + (small ? " small" : ""), value));
        return n;
      };
      nums.appendChild(cell("chickens", fmtNum(s.n_runs)));
      nums.appendChild(cell("tokens in mean·p50·p95", statsTriple(s.tokens_in_mean, s.tokens_in_p50, s.tokens_in_p95), true));
      nums.appendChild(cell("tokens out mean·p50·p95", statsTriple(s.tokens_out_mean, s.tokens_out_p50, s.tokens_out_p95), true));
      nums.appendChild(cell("tokens total p95", fmtNum(s.tokens_p95)));
      nums.appendChild(cell("expected feed / chicken", fmtCost(s.expected_cost_usd)));
      nums.appendChild(cell("updated", s.updated_at || "—", true));
      card.appendChild(nums);
      grid.appendChild(card);
    }
    flashCard(grid);
  }

  async function selectStatsJob(jobId) {
    statsDetailJob = jobId;
    await loadStatsDetail();
  }

  async function loadStatsDetail() {
    const detail = $("#stats-detail");
    if (!statsDetailJob) {
      detail.hidden = true;
      return;
    }
    const data = await api("/api/stats/" + encodeURIComponent(statsDetailJob));
    if (data === null) return; /* 401 → login view shown */
    if (data.__networkError || (data.daemon && data.daemon.up === false)) {
      detail.hidden = true;
      return;
    }
    const s = data.stats;
    detail.hidden = false;
    $("#stats-detail-title").textContent = (s && (s.job_name || s.job_id)) || statsDetailJob;
    const rows = s
      ? [
          ["job_id", s.job_id],
          ["chickens n", fmtNum(s.n_runs)],
          ["tokens_in mean / p50 / p95", statsTriple(s.tokens_in_mean, s.tokens_in_p50, s.tokens_in_p95)],
          ["tokens_out mean / p50 / p95", statsTriple(s.tokens_out_mean, s.tokens_out_p50, s.tokens_out_p95)],
          ["tokens_total_p95", fmtNum(s.tokens_p95)],
          ["expected_cost_usd_per_run", fmtCost(s.expected_cost_usd)],
          ["updated_at", s.updated_at || "—"],
        ]
      : [["error", data.error || "no feed recorded for this egg yet"]];
    const list = $("#stats-detail-list");
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
    }
  }

  /* --- spec 30: feed vs budget card on the Feed view — per-chicken measured
         cost against the egg's cap (cost outliers visible even without a cap) */

  function renderCostCard(runs) {
    const finished = (runs || []).filter((r) => TERMINAL_RUN_STATES.includes(r.status) && r.cost_usd != null);
    const card = $("#cost-card");
    const empty = $("#cost-empty");
    if (!finished.length) {
      card.hidden = true;
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    card.hidden = false;
    const top = finished.slice().sort((a, b) => (b.cost_usd || 0) - (a.cost_usd || 0)).slice(0, 10);
    $("#cost-summary").textContent =
      finished.length + " finished chicken" + (finished.length === 1 ? "" : "s") +
      " · highest feed first — measured cost against the egg's cap.";
    const list = $("#cost-list");
    const sig = JSON.stringify(top.map((r) => [r.id, r.cost_usd, r.max_cost_usd, r.budget_outcome, r.status]));
    if (list.dataset.sig === sig) return;
    list.dataset.sig = sig;
    list.replaceChildren();
    for (const r of top) {
      const li = el("li", "cost-item");
      const openBtn = el("button", "linklike mono run-open", r.id);
      openBtn.type = "button";
      openBtn.setAttribute("aria-label", "Open chicken " + r.id);
      openBtn.addEventListener("click", () => {
        if (location.hash !== "#/runs") location.hash = "#/runs";
        selectRun(r.id);
      });
      li.appendChild(openBtn);
      li.appendChild(el("span", "muted", " " + r.status + " · "));
      const feed = el("span", "mono", fmtCost(r.cost_usd) + (r.max_cost_usd != null ? " / cap " + fmtCost(r.max_cost_usd) : " · no cap"));
      if (r.budget_outcome === "cost_cap") feed.classList.add("cost-cap");
      li.appendChild(feed);
      if (r.budget_outcome === "cost_cap") li.appendChild(el("span", "muted cost-cap", " · cost cap hit"));
      list.appendChild(li);
    }
    flashCard(card);
  }

  async function loadStats() {
    const data = await api("/api/stats");
    if (data === null) return; /* 401 → login view shown */
    if (payloadDown(data)) {
      showEmpty("#stats-empty", "#stats-grid", "Feed unavailable — " + (data.error || "coopd is not reachable") + ".");
    } else {
      renderStatsCards(data.stats || []);
    }
    // spec 30: the same cost-vs-cap picture per chicken, from the runs list
    const runsData = await api("/api/runs");
    if (runsData !== null && !payloadDown(runsData)) renderCostCard(runsData.runs || []);
    if (statsDetailJob) await loadStatsDetail();
  }

  $("#stats-detail-close").addEventListener("click", () => {
    statsDetailJob = null;
    $("#stats-detail").hidden = true;
  });

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
      showEmpty("#audit-empty", "#audit-card", "Headcount unavailable — " + (data.error || "coopd is not reachable") + ".");
      return;
    }
    const events = data.events || [];
    if (!events.length) {
      showEmpty("#audit-empty", "#audit-card", auditFilter ? "No headcount events of type '" + auditFilter + "' yet." : "The headcount is empty.");
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
      showEmpty("#settings-empty", "#settings-card", "Settings unavailable — " + (data.error || "coopd is not reachable") + ".");
      return;
    }
    const s = data.settings || {};
    const coop = s.coop || {};
    const rows = [
      ["data_dir", s.data_dir],
      ["socket_path", s.socket_path],
      ["db_path", s.db_path],
      ["audit_log_path — the headcount", s.audit_log_path],
      ["results_dir — the nest box", s.results_dir],
      ["quarantine_dir — candling quarantine", s.quarantine_dir],
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

  /* --- auth view (spec 19): providers + seat sessions + declared access scope --- */

  function authError(msg) {
    const p = $("#auth-error");
    if (!msg) {
      p.hidden = true;
      p.textContent = "";
      return;
    }
    p.textContent = msg;
    p.hidden = false;
  }

  function fmtCountdown(iso) {
    // "expires 2026-11-12T…Z · in 59m" / "expired 2026-11-11T…Z"
    if (!iso) return "—";
    const t = Date.parse(iso);
    if (!isFinite(t)) return iso;
    const diffSec = Math.round((t - Date.now()) / 1000);
    const when = iso;
    if (diffSec <= 0) return "expired " + when;
    if (diffSec < 3600) return "expires " + when + " · in " + Math.max(1, Math.floor(diffSec / 60)) + "m";
    if (diffSec < 86400) return "expires " + when + " · in " + Math.floor(diffSec / 3600) + "h " + Math.floor((diffSec % 3600) / 60) + "m";
    return "expires " + when + " · in " + Math.floor(diffSec / 86400) + "d";
  }

  function renderProviders(providers) {
    if (!providers.length) {
      showEmpty("#auth-empty", "#auth-card", "No providers configured.");
      return;
    }
    showCard("#auth-empty", "#auth-card");
    const tbody = $("#auth-providers-tbody");
    const card = $("#auth-card");
    const sig = JSON.stringify(providers);
    if (tbody.dataset.sig === sig) return;
    tbody.dataset.sig = sig;
    tbody.replaceChildren();
    for (const p of providers) {
      const tr = el("tr");
      tr.appendChild(el("td", "mono job-name", p.provider));
      const modeTd = el("td");
      modeTd.appendChild(el("span", "badge " + (p.mode === "seat" ? "badge-seat" : "badge-api-key"), p.mode || "—"));
      tr.appendChild(modeTd);
      const sessTd = el("td");
      const sess = p.session;
      if (p.mode === "seat" && sess) {
        const wrap = el("span", "status-cell");
        wrap.appendChild(el("span", "dot " + (sess.expired ? "dot-failed" : "dot-succeeded")));
        wrap.appendChild(el("span", "mono", fmtCountdown(sess.expires_at)));
        sessTd.appendChild(wrap);
      } else if (p.mode === "seat") {
        const wrap = el("span", "status-cell");
        wrap.appendChild(el("span", "dot dot-queued"));
        wrap.appendChild(el("span", "mono muted", p.session_error ? "unreadable" : "none — log in"));
        sessTd.appendChild(wrap);
      } else {
        sessTd.appendChild(el("span", "mono muted", "—"));
      }
      tr.appendChild(sessTd);
      const actionTd = el("td");
      if (p.mode === "seat") {
        const validSession = sess && !sess.expired;
        if (!validSession) {
          const login = el("button", "linklike auth-login", "Log in");
          login.type = "button";
          login.setAttribute("aria-label", "Start seat login for " + p.provider);
          login.addEventListener("click", () => startDeviceLogin(p.provider));
          actionTd.appendChild(login);
        }
        if (sess) {
          if (!validSession) actionTd.appendChild(el("span", "muted", " · "));
          const out = el("button", "linklike auth-logout", "Log out");
          out.type = "button";
          out.setAttribute("aria-label", "Log out " + p.provider);
          out.addEventListener("click", () => logoutProvider(p.provider));
          actionTd.appendChild(out);
        }
      }
      tr.appendChild(actionTd);
      tbody.appendChild(tr);
    }
    flashCard(card);
  }

  function renderAccess(access) {
    const rows = [
      ["env_allowlist" + (access.env_allowlist_enforced ? " (enforced per run)" : ""), (access.env_allowlist || []).join(", ") || "—"],
      ["base_env_keys", (access.base_env_keys || []).join(", ") || "—"],
      ["network_policy", String(access.network_policy || "—") + (access.network_enforced ? " (enforced)" : " (declared — enforcement post-MVP)")],
    ];
    const list = $("#access-list");
    const sig = JSON.stringify(rows);
    if (list.dataset.sig !== sig) {
      list.dataset.sig = sig;
      list.replaceChildren();
      for (const [k, v] of rows) {
        const row = el("div", "settings-row");
        row.appendChild(el("dt", null, k));
        row.appendChild(el("dd", null, String(v)));
        list.appendChild(row);
      }
    }
    const scope = access.scope || [];
    const table = $("#access-scope-table");
    if (!scope.length) {
      table.hidden = true;
      return;
    }
    table.hidden = false;
    const tbody = $("#access-scope-tbody");
    const sSig = JSON.stringify(scope);
    if (tbody.dataset.sig === sSig) return;
    tbody.dataset.sig = sSig;
    tbody.replaceChildren();
    for (const s of scope) {
      const tr = el("tr");
      tr.appendChild(el("td", "mono", s.kind || "—"));
      tr.appendChild(el("td", "mono muted", s.declared == null ? "none" : String(s.declared)));
      tr.appendChild(el("td", "muted", s.note || ""));
      tbody.appendChild(tr);
    }
  }

  async function loadAuth() {
    const [prov, acc] = await Promise.all([api("/api/auth/providers"), api("/api/auth/access")]);
    if (prov === null || acc === null) return; /* 401 → login view shown */
    if (payloadDown(prov)) {
      showEmpty("#auth-empty", "#auth-card", "Perch unavailable — " + (prov.error || "coopd is not reachable") + ".");
    } else {
      renderProviders(prov.providers || []);
    }
    if (!payloadDown(acc)) renderAccess(acc.access || {});
  }

  /* device login: POST /api/auth/login shows the code, then poll /api/auth/login/status */
  let devicePollTimer = null;
  let deviceProvider = null;

  function stopDevicePoll() {
    if (devicePollTimer !== null) clearInterval(devicePollTimer);
    devicePollTimer = null;
  }

  function deviceStatus(msg) {
    $("#device-status").textContent = msg;
  }

  async function pollDeviceLogin() {
    if (!deviceProvider) return;
    const resp = await apiPost("/api/auth/login/status", { provider: deviceProvider });
    if (resp === null) return; /* 401 → login view shown */
    if (resp.__networkError || (resp.daemon && resp.daemon.up === false)) {
      deviceStatus("Lost contact with the coop — still waiting.");
      return;
    }
    if (!resp.ok) {
      stopDevicePoll();
      deviceStatus(resp.error || "Login failed.");
      return;
    }
    if (resp.status === "authorized") {
      stopDevicePoll();
      deviceStatus("Authorized — perch seat claimed.");
      loadAuth();
      setTimeout(() => { $("#device-card").hidden = true; }, 1600);
      return;
    }
    if (resp.status === "expired") {
      stopDevicePoll();
      deviceStatus("The code expired — start the login again.");
      return;
    }
    deviceStatus("Waiting for authorization…");
  }

  async function startDeviceLogin(provider) {
    authError("");
    stopDevicePoll();
    const resp = await apiPost("/api/auth/login", { provider });
    if (resp === null) return; /* 401 → login view shown */
    if (resp.__networkError) {
      authError("The GUI could not reach the coop server.");
      return;
    }
    if (resp.daemon && resp.daemon.up === false) {
      authError("coopd is not reachable — " + (resp.error || "") + ".");
      return;
    }
    if (!resp.ok) {
      authError(resp.error || "login refused");
      return;
    }
    deviceProvider = provider;
    $("#device-provider").textContent = provider;
    $("#device-code").textContent = resp.user_code || "—";
    const link = $("#device-verification");
    const uri = resp.verification_uri || "";
    link.textContent = uri;
    const safeHref = httpsOnlyHref(uri);
    if (safeHref) {
      link.href = safeHref;
    } else {
      link.removeAttribute("href"); // not https → shown as text only, never linked
    }
    $("#device-expires").textContent = "code expires " + (resp.expires_at || "—");
    deviceStatus("Waiting for authorization…");
    $("#device-card").hidden = false;
    devicePollTimer = setInterval(pollDeviceLogin, 2000);
    pollDeviceLogin();
  }

  async function logoutProvider(provider) {
    authError("");
    const resp = await apiPost("/api/auth/logout", { provider });
    if (resp === null) return; /* 401 → login view shown */
    if (resp.__networkError) {
      authError("The GUI could not reach the coop server.");
      return;
    }
    if (resp.daemon && resp.daemon.up === false) {
      authError("coopd is not reachable — " + (resp.error || "") + ".");
      return;
    }
    if (!resp.ok) {
      authError(resp.error || "logout refused");
      return;
    }
    loadAuth();
  }

  $("#device-cancel").addEventListener("click", () => {
    stopDevicePoll();
    deviceProvider = null;
    $("#device-card").hidden = true;
  });

  /* restart the diagram pulses on hover (CSS restarts on a new animation-name) */
  $("#diagram-card").addEventListener("mouseenter", () => {
    $$(".flow").forEach((line) => {
      line.style.animation = "none";
      void line.getBoundingClientRect();
      line.style.animation = "";
    });
  });

  /* --- farmhand view (spec 31): transports, tools, client config, transport form --- */

  function fhError(msg) {
    const p = $("#farmhand-error");
    if (!msg) {
      p.hidden = true;
      p.textContent = "";
      return;
    }
    p.textContent = msg;
    p.hidden = false;
  }

  function renderFarmhand(info) {
    const transports = info.transports && info.transports.length ? info.transports : [info.transport];
    const rows = [
      ["transport", transports.join(" + ")],
      ["https endpoint", info.https_url ? info.https_url + (info.https_mounted ? " · served now" : " · not served — pick https or both above") : "— (coop disabled)"],
      ["bearer", info.auth_required ? "required — send Authorization: Bearer <token>" : "not required (loopback, no token configured)"],
      ["stdio command", "tc farmhand serve --transport stdio"],
    ];
    $("#farmhand-summary").textContent =
      transports.join(" + ") + " · " + (info.https_mounted ? (info.https_url || "https") + " mounted" : "https endpoint not mounted") +
      (info.auth_required ? " · bearer required" : " · open");
    const list = $("#farmhand-list");
    const card = $("#farmhand-card");
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
    // the exposed tool list — read straight off the built MCP server
    const tools = info.tools || [];
    $("#farmhand-tools-card").hidden = !tools.length;
    if (tools.length) {
      $("#farmhand-tools-count").textContent = tools.length + " tools — thin wrappers over coopd's socket ops";
      const tbody = $("#farmhand-tools-tbody");
      const tSig = JSON.stringify(tools);
      if (tbody.dataset.sig !== tSig) {
        tbody.dataset.sig = tSig;
        tbody.replaceChildren();
        for (const t of tools) {
          const tr = el("tr");
          tr.appendChild(el("td", "mono job-name", t.name));
          tr.appendChild(el("td", "muted gate-summary", t.description || ""));
          tbody.appendChild(tr);
        }
        flashCard($("#farmhand-tools-card"));
      }
    }
    // ready-to-paste client configs (the bearer header is a placeholder only)
    const cfg = info.client_config || {};
    $("#farmhand-snippet-card").hidden = false;
    $("#fh-snippet-stdio").textContent = cfg.stdio ? JSON.stringify(cfg.stdio, null, 2) : "";
    $("#fh-snippet-https").textContent = cfg.https
      ? JSON.stringify(cfg.https, null, 2)
      : "// the coop exposes no HTTPS endpoint (coop disabled)";
    // keep the transport form in sync unless the operator is editing it
    const sel = $("#fh-transport");
    if (document.activeElement !== sel && sel.value !== info.transport) sel.value = info.transport;
  }

  async function loadFarmhand() {
    const data = await api("/api/farmhand");
    if (data === null) return; /* 401 → login view shown */
    if (payloadDown(data)) {
      showEmpty("#farmhand-empty", "#farmhand-card", "Farmhand unavailable — " + (data.error || "coopd is not reachable") + ".");
      $("#farmhand-tools-card").hidden = true;
      $("#farmhand-snippet-card").hidden = true;
      return;
    }
    $("#farmhand-empty").hidden = true;
    $("#farmhand-card").hidden = false;
    renderFarmhand(data);
  }

  $("#farmhand-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    fhError("");
    const resp = await apiPost("/api/settings", { farmhand_transport: $("#fh-transport").value });
    if (resp === null) return; /* 401 → login view shown */
    if (resp.__networkError) {
      fhError("The GUI could not reach the coop server.");
      return;
    }
    if (resp.daemon && resp.daemon.up === false) {
      fhError("coopd is not reachable — " + (resp.error || "") + ".");
      return;
    }
    if (!resp.ok) {
      const errs = resp.errors || [];
      const line = errs.map((e) => (e.field || "") + ": " + (e.message || "")).join("\n");
      fhError(line || resp.error || "transport update refused");
      return;
    }
    loadFarmhand();
  });

  /* --- connectors view (spec 31): gates CRUD over /api/connectors/* --- */

  function connError(msg) {
    const p = $("#connectors-error");
    if (!msg) {
      p.hidden = true;
      p.textContent = "";
      return;
    }
    p.textContent = msg;
    p.hidden = false;
  }

  const GATE_FIELD_ERRORS = {
    name: "#gate-name-error",
    transport: "#gate-error",
    command: "#gate-command-error",
    args: "#gate-args-error",
    env: "#gate-env-error",
    url: "#gate-url-error",
    headers: "#gate-headers-error",
  };

  function setGateError(leaf, msg) {
    const sel = GATE_FIELD_ERRORS[leaf] || "#gate-error";
    const p = $(sel);
    p.textContent = msg;
    p.hidden = false;
  }

  function clearGateErrors() {
    $$("#gate-form .field-error").forEach((p) => {
      p.hidden = true;
      p.textContent = "";
    });
  }

  function parseKvLines(text) {
    const out = {};
    for (const line of String(text).split("\n")) {
      const t = line.trim();
      if (!t) continue;
      const eq = t.indexOf("=");
      if (eq <= 0) throw new Error("each line must be KEY=VALUE — got: " + t);
      out[t.slice(0, eq).trim()] = t.slice(eq + 1).trim();
    }
    return out;
  }

  function gateSummaryText(g) {
    if (g.transport === "stdio") {
      const args = (g.args || []).join(" ");
      const env = (g.env_keys || []).join(", ");
      return (g.command || "—") + (args ? " " + args : "") + (env ? "  ·  env: " + env : "");
    }
    const headers = Object.entries(g.headers || {}).map(([k, v]) => k + ": " + v).join(", ");
    return (g.url || "—") + (headers ? "  ·  headers: " + headers : "");
  }

  function toggleGateTransportFields() {
    const t = $("#gate-transport").value;
    $$("#gate-form [data-for]").forEach((f) => {
      f.hidden = f.dataset.for !== t;
    });
  }

  async function loadConnectors() {
    const data = await api("/api/connectors");
    if (data === null) return; /* 401 → login view shown */
    if (payloadDown(data)) {
      showEmpty("#connectors-empty", "#connectors-card", "Gates unavailable — " + (data.error || "coopd is not reachable") + ".");
      return;
    }
    const gates = data.connectors || [];
    if (!gates.length) {
      showEmpty("#connectors-empty", "#connectors-card", "No gates registered yet — register one above, or with tc connectors add …");
      return;
    }
    showCard("#connectors-empty", "#connectors-card");
    const tbody = $("#connectors-tbody");
    const card = $("#connectors-card");
    const sig = JSON.stringify(gates);
    if (tbody.dataset.sig !== sig) {
      tbody.dataset.sig = sig;
      tbody.replaceChildren();
      for (const g of gates) {
        const tr = el("tr");
        tr.appendChild(el("td", "mono job-name", g.name));
        const tTd = el("td");
        tTd.appendChild(el("span", "badge " + (g.transport === "https" ? "badge-seat" : "badge-api-key"), g.transport || "—"));
        tr.appendChild(tTd);
        tr.appendChild(el("td", "mono muted gate-summary", gateSummaryText(g)));
        const actionTd = el("td");
        const rm = el("button", "linklike gate-remove", "Remove");
        rm.type = "button";
        rm.setAttribute("aria-label", "Remove gate " + g.name);
        rm.addEventListener("click", () => removeGate(g.name));
        actionTd.appendChild(rm);
        tr.appendChild(actionTd);
        tbody.appendChild(tr);
      }
      flashCard(card);
    }
  }

  async function removeGate(name) {
    connError("");
    const resp = await apiPost("/api/connectors/rm", { name });
    if (resp === null) return; /* 401 → login view shown */
    if (resp.__networkError) {
      connError("The GUI could not reach the coop server.");
      return;
    }
    if (resp.daemon && resp.daemon.up === false) {
      connError("coopd is not reachable — " + (resp.error || "") + ".");
      return;
    }
    if (!resp.ok) {
      connError(resp.error || "remove refused");
      return;
    }
    loadConnectors();
  }

  $("#gate-transport").addEventListener("change", toggleGateTransportFields);

  $("#gate-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    clearGateErrors();
    const name = $("#gate-name").value.trim();
    const transport = $("#gate-transport").value;
    if (!name) {
      setGateError("name", "Name is required.");
      return;
    }
    const body = { name, transport };
    try {
      if (transport === "stdio") {
        const command = $("#gate-command").value.trim();
        if (command) body.command = command;
        const argsRaw = $("#gate-args").value.trim();
        if (argsRaw) body.args = argsRaw.split(",").map((s) => s.trim()).filter(Boolean);
        const env = parseKvLines($("#gate-env").value);
        if (Object.keys(env).length) body.env = env;
      } else {
        const url = $("#gate-url").value.trim();
        if (url) body.url = url;
        const headers = parseKvLines($("#gate-headers").value);
        if (Object.keys(headers).length) body.headers = headers;
      }
    } catch (err) {
      setGateError(transport === "stdio" ? "env" : "headers", String(err.message || err));
      return;
    }
    const resp = await apiPost("/api/connectors/add", body);
    if (resp === null) return; /* 401 → login view shown */
    if (resp.__networkError) {
      setGateError("", "The GUI could not reach the coop server.");
      return;
    }
    if (resp.daemon && resp.daemon.up === false) {
      setGateError("", "coopd is not reachable — " + (resp.error || "") + ".");
      return;
    }
    if (!resp.ok) {
      const errs = resp.errors || [];
      if (errs.length) {
        // field-named: connectors.<name>.<field> (or connectors.name)
        for (const e of errs) {
          const parts = String(e.field || "").split(".");
          const leaf = parts[parts.length - 1];
          setGateError(leaf === "<root>" ? "" : leaf, (e.field || "") + ": " + (e.message || ""));
        }
      } else {
        setGateError("", resp.error || "gate refused");
      }
      return;
    }
    $("#gate-form").reset();
    toggleGateTransportFields();
    loadConnectors();
  });

  /* --- boot --- */

  if (token) $("#signout").hidden = false;
  toggleGateTransportFields();
  if (location.hash === "" ) location.hash = "#/dashboard";
  route();
})();
