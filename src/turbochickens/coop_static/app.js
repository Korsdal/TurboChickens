/* TurboChickens coop SPA — dependency-free, hash-routed, polling every 2 s.
   The token lives in sessionStorage only and goes out as Authorization: Bearer.
   No cookies. All data comes from /api/* (thin mirrors of the daemon socket ops). */
(() => {
  "use strict";

  const TOKEN_KEY = "tc.coop.token";
  const POLL_MS = 2000;
  const VIEWS = ["dashboard", "runs", "jobs", "schedules", "stats", "audit", "settings"];
  /* views with a live /api loader (schedules/stats land in specs 16/17) */
  const VIEW_LOADERS = { dashboard: loadStatus, runs: loadRuns, jobs: loadJobs, audit: loadAudit, settings: loadSettings };

  const RUN_STATUSES = ["queued", "provisioning", "running", "collecting", "finalizing", "succeeded", "failed"];

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  let token = sessionStorage.getItem(TOKEN_KEY);
  let pollTimer = null;
  let lastStats = {};
  let auditFilter = null; // active event-type filter chip, null = all
  let runsJobFilter = null; // active runs job filter chip, null = all
  let runsStatusFilter = null; // active runs status filter chip, null = all
  let selectedRunId = null; // open run detail pane
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
    if (!jobFile) return { error: "job.toml: choose the job spec file first." };
    if (!promptFile) return { error: "input.prompt_file: choose the prompt file." };
    if (!schemaFile) return { error: "output.schema_file: choose the schema file." };
    const files = {};
    for (const f of Array.from($("#up-attachments").files)) {
      files[f.webkitRelativePath || f.name] = await readFileB64(f);
    }
    files[bundlePath($("#up-prompt-path"), promptFile)] = await readFileB64(promptFile);
    files[bundlePath($("#up-schema-path"), schemaFile)] = await readFileB64(schemaFile);
    files["job.toml"] = await readFileB64(jobFile); // job.toml always wins the root key
    return { files };
  }

  function bundleSignature() {
    // names + sizes + mtimes of the chosen files + typed bundle paths
    const sig = [];
    for (const id of ["up-job", "up-prompt", "up-schema", "up-attachments"]) {
      for (const f of Array.from($(id).files)) sig.push(id + ":" + f.name + ":" + f.size + ":" + f.lastModified);
    }
    sig.push($("#up-prompt-path").value, $("#up-schema-path").value);
    return sig.join("|");
  }

  function uploadErrorField(field) {
    if (field.startsWith("input.prompt_file")) return "#up-prompt-error";
    if (field.startsWith("output.schema_file")) return "#up-schema-error";
    if (field.startsWith("input.attachments")) return "#up-attachments-error";
    return "#up-job-error"; // job.toml and every spec-section error live under the spec field
  }

  function clearUploadErrors() {
    for (const sel of ["#up-job-error", "#up-prompt-error", "#up-schema-error", "#up-attachments-error", "#upload-error"]) {
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
      p.textContent = resp.error || "upload failed";
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

  for (const id of ["up-job", "up-prompt", "up-schema", "up-attachments", "up-prompt-path", "up-schema-path"]) {
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
        showUploadErrors({ error: "The supervisor is not reachable — " + (resp.error || "") + "." });
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
      $("#upload-submit").textContent = "Run job";
      const res = $("#upload-result");
      res.textContent = "valid — " + resp.job_id + " · sha256:" + String(resp.spec_hash).slice(0, 12) + "…";
      res.hidden = false;
      return;
    }
    // already validated and unchanged → run the stored bundle over HTTP
    const resp = await apiPost("/api/runs", { files: uploadBundle.files });
    if (resp === null) return; /* 401 → login view shown */
    if (resp.__networkError || (resp.daemon && resp.daemon.up === false)) {
      showUploadErrors({ error: "The supervisor is not reachable — " + (resp.error || "") + "." });
      return;
    }
    if (!resp.ok) {
      if (resp.errors) showUploadErrors(resp);
      else showUploadErrors({ error: resp.error || "enqueue failed" });
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

  function renderRunsTable(runs) {
    if (!runs.length) {
      const msg = runsJobFilter || runsStatusFilter
        ? "No runs match the active filters."
        : "No runs yet — run a job above, or with tc run <job.toml>.";
      showEmpty("#runs-empty", "#runs-card", msg);
      lastRunSigs = {};
      return;
    }
    showCard("#runs-empty", "#runs-card");
    const tbody = $("#runs-tbody");
    const sig = JSON.stringify(runs.map((r) => [r.id, r.status, r.result_valid, r.finished_at, r.attested]));
    if (tbody.dataset.sig !== sig) {
      tbody.dataset.sig = sig;
      tbody.replaceChildren();
      const seen = new Set();
      for (const r of runs) {
        seen.add(r.id);
        const rowSig = JSON.stringify([r.status, r.result_valid, r.finished_at, r.attested]);
        const changed = lastRunSigs[r.id] !== undefined && lastRunSigs[r.id] !== rowSig;
        lastRunSigs[r.id] = rowSig;
        const tr = el("tr", "run-row" + (r.id === selectedRunId ? " selected" : "") + (changed ? " flash" : ""));
        tr.dataset.runId = r.id;
        const idTd = el("td");
        const openBtn = el("button", "linklike mono run-open", r.id);
        openBtn.type = "button";
        openBtn.setAttribute("aria-label", "Open run " + r.id);
        openBtn.addEventListener("click", () => selectRun(r.id));
        idTd.appendChild(openBtn);
        tr.appendChild(idTd);
        tr.appendChild(el("td", "mono job-name", r.job_name));
        const dot = statusDotInfo(r);
        const stTd = el("td");
        const st = el("span", "status-cell");
        st.appendChild(el("span", "dot " + dot.cls));
        st.appendChild(el("span", "status-label", dot.label));
        stTd.appendChild(st);
        tr.appendChild(stTd);
        tr.appendChild(el("td", "mono muted", r.trigger || "—"));
        tr.appendChild(el("td", "mono muted", r.started_at || "—"));
        tr.appendChild(el("td", "mono muted", r.finished_at || "—"));
        tr.addEventListener("click", (ev) => {
          if (ev.target.closest("button")) return; // the open button handles itself
          selectRun(r.id);
        });
        tbody.appendChild(tr);
      }
      for (const id of Object.keys(lastRunSigs)) if (!seen.has(id)) delete lastRunSigs[id];
    }
  }

  function selectRun(runId) {
    selectedRunId = runId;
    $("#run-detail").hidden = false;
    $$("#runs-tbody tr").forEach((tr) => tr.classList.toggle("selected", tr.dataset.runId === runId));
    loadRuns();
  }

  function setRunsTab(name) {
    runsTab = name;
    for (const t of ["manifest", "logs", "result"]) {
      $("#tab-" + t).setAttribute("aria-selected", String(t === name));
      $("#pane-" + t).hidden = t !== name;
    }
  }

  function renderDetail(show, logs, result) {
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
    $("#detail-job").textContent = run.job_name + " · " + run.trigger + " · " + run.status;
    $("#tab-result").textContent = run.result && run.result.valid === false ? "Quarantine" : "Result";

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
      label.textContent = "result unavailable";
      return;
    }
    if (result.ok === true && result.output !== undefined) {
      label.textContent = "validated result";
      pre.dataset.sig = JSON.stringify(result.output);
      pre.textContent = JSON.stringify(result.output, null, 2);
      return;
    }
    if (result.quarantined) {
      label.textContent = "quarantined raw output";
      state.hidden = false;
      const note = el("div", "quarantine-note");
      note.textContent = "Quarantined — the output failed schema validation. It is kept for inspection and never served as a valid result.";
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
      label.textContent = "result";
      state.hidden = false;
      const note = el("div", "quarantine-note neutral-note");
      note.textContent = "Expired — the on-disk artifacts were removed by the retention sweep; the append-only index row remains.";
      state.appendChild(note);
      return;
    }
    label.textContent = "result";
    state.hidden = false;
    const note = el("div", "quarantine-note neutral-note");
    note.textContent = "No result yet — the run never reached collecting (it may still be running).";
    state.appendChild(note);
  }

  async function loadRunDetail(runId) {
    const enc = encodeURIComponent(runId);
    const [show, logs, result] = await Promise.all([
      api("/api/runs/" + enc),
      api("/api/runs/" + enc + "/logs"),
      api("/api/results/" + enc),
    ]);
    if (show === null) return; /* 401 → login view shown */
    renderDetail(show, logs, result);
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
      showEmpty("#runs-empty", "#runs-card", "Runs unavailable — " + (data.error || "the supervisor is not reachable") + ".");
      return;
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
      err.textContent = "Enter the in-container path of a job spec (job.toml).";
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
      err.textContent = "The supervisor is not reachable — " + (resp.error || "") + ".";
      err.hidden = false;
      return;
    }
    if (!resp.ok) {
      err.textContent = resp.error || "enqueue failed";
      err.hidden = false;
      return;
    }
    err.hidden = true;
    input.value = "";
    selectRun(resp.run_id);
  });

  $("#detail-close").addEventListener("click", () => {
    selectedRunId = null;
    $("#run-detail").hidden = true;
    $$("#runs-tbody tr").forEach((tr) => tr.classList.remove("selected"));
  });

  for (const t of ["manifest", "logs", "result"]) {
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
