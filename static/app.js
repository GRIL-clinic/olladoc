// Client-side controller for the olladoc Flask app.
//
// State model:
//   idle             — form visible, no active job
//   running_phase1   — poll /api/status, show progress panel
//   awaiting_edit    — Phase 1 done; render editable glossary panels
//   running_phase2   — poll /api/status, show progress panel
//   done             — render results panel + download links

const LANGS = [
  "Spanish", "English", "French", "German", "Italian", "Portuguese",
  "Dutch", "Russian", "Chinese", "Japanese", "Korean", "Arabic",
  "Turkish", "Polish", "Ukrainian", "Vietnamese", "Thai", "Indonesian",
  "Hindi", "Bengali", "Urdu",
];

// ---- Elements ------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const els = {
  form: $("form"),
  dropzone: $("dropzone"),
  filesInput: $("files"),
  fileList: $("fileList"),
  sourceLang: $("sourceLang"),
  targetLang: $("targetLang"),
  modelSelect: $("modelSelect"),
  modelInput: $("modelInput"),
  outputDir: $("outputDir"),
  submitBtn: $("submitBtn"),
  progress: $("progress"),
  progressTitle: $("progressTitle"),
  progressFill: $("progressFill"),
  progressText: $("progressText"),
  log: $("log"),
  edit: $("edit"),
  editList: $("editList"),
  continueBtn: $("continueBtn"),
  cancelBtn: $("cancelBtn"),
  saveBtn: $("saveBtn"),
  saveStatus: $("saveStatus"),
  results: $("results"),
  resultsBanner: $("resultsBanner"),
  resultsBody: $("resultsBody"),
  aboutBtn: $("aboutBtn"),
  aboutDialog: $("aboutDialog"),
  // Ollama controls
  ollamaStatusText: $("ollamaStatusText"),
  viewLogsLink: $("viewLogsLink"),
  startOllamaBtn: $("startOllamaBtn"),
  stopOllamaBtn: $("stopOllamaBtn"),
  logsDialog: $("logsDialog"),
  logsBody: $("logsBody"),
  logsPath: $("logsPath"),
  refreshLogsBtn: $("refreshLogsBtn"),
  // Pull model
  pullName: $("pullName"),
  pullBtn: $("pullBtn"),
  pullProgress: $("pullProgress"),
  pullLog: $("pullLog"),
  cancelPullBtn: $("cancelPullBtn"),
};

// ---- One-time setup ------------------------------------------------------
function populateLangs() {
  const fill = (sel, def) => {
    sel.innerHTML = "";
    for (const lang of LANGS) {
      const opt = document.createElement("option");
      opt.value = lang;
      opt.textContent = lang;
      if (lang === def) opt.selected = true;
      sel.appendChild(opt);
    }
  };
  fill(els.sourceLang, "Spanish");
  fill(els.targetLang, "English");
}

function setDefaultOutputDir() {
  els.outputDir.value = "./translated";
}

// ---- File input handling -------------------------------------------------
function renderFileList() {
  const files = Array.from(els.filesInput.files || []);
  els.fileList.innerHTML = "";
  for (const f of files) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = f.name;
    const size = document.createElement("span");
    size.className = "filesize";
    size.textContent = fmtBytes(f.size);
    li.appendChild(name);
    li.appendChild(size);
    els.fileList.appendChild(li);
  }
  els.submitBtn.disabled = files.length === 0;
}
function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

// Drag-and-drop wiring.
els.filesInput.addEventListener("change", renderFileList);
["dragenter", "dragover"].forEach((evt) =>
  els.dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    els.dropzone.classList.add("dragover");
  })
);
["dragleave", "dragend", "drop"].forEach((evt) =>
  els.dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    els.dropzone.classList.remove("dragover");
  })
);
els.dropzone.addEventListener("drop", (e) => {
  const dt = e.dataTransfer;
  if (dt && dt.files) {
    els.filesInput.files = dt.files;
    renderFileList();
  }
});

// ---- Submit --------------------------------------------------------------
let currentJobId = null;
let pollTimer = null;

els.form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const files = Array.from(els.filesInput.files || []);
  if (files.length === 0) return;
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  fd.append("source_lang", els.sourceLang.value);
  fd.append("target_lang", els.targetLang.value);
  const modelValue = !els.modelSelect.hidden
      ? els.modelSelect.value
      : els.modelInput.value;
  fd.append("model", modelValue);
  fd.append("output_dir", els.outputDir.value);
  fd.append("workflow", els.form.querySelector('[name=workflow]:checked').value);
  fd.append("keep_glossary",
            els.form.querySelector('[name=keep_glossary]').checked ? "true" : "false");
  fd.append("timestamp",
            els.form.querySelector('[name=timestamp]').checked ? "true" : "false");
  els.submitBtn.disabled = true;
  hideAll();
  els.progress.hidden = false;
  els.progressTitle.textContent = "Progress";
  els.log.textContent = "";
  try {
    const r = await fetch("/api/translate", { method: "POST", body: fd });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    currentJobId = data.job_id;
    startPolling();
  } catch (err) {
    showError(`Failed to start: ${err.message}`);
    els.submitBtn.disabled = false;
  }
});

// ---- Polling -------------------------------------------------------------
function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(pollOnce, 1000);
  pollOnce();
}
function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

async function pollOnce() {
  if (!currentJobId) return;
  try {
    const r = await fetch(`/api/status/${currentJobId}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const s = await r.json();
    applyStatus(s);
  } catch (err) {
    console.warn("poll failed", err);
  }
}

function applyStatus(s) {
  // Progress + log — always update these while a job is running.
  const pct = s.total_count ? (s.done_count / s.total_count) * 100 : 0;
  els.progressFill.style.width = `${pct}%`;
  els.progressText.textContent = `${s.done_count} / ${s.total_count}`;
  els.log.textContent = s.log.join("\n");
  // Scroll the log tail into view.
  els.log.scrollTop = els.log.scrollHeight;

  if (s.status === "running_phase1") {
    els.progressTitle.textContent = "Progress — Phase 1: building glossary";
  } else if (s.status === "running_phase2") {
    els.progressTitle.textContent = "Progress — Phase 2: translating";
  } else if (s.status === "running") {
    els.progressTitle.textContent = "Progress — building glossary + translating";
  } else if (s.status === "awaiting_edit") {
    stopPolling();
    renderEditUI(s);
  } else if (s.status === "done") {
    stopPolling();
    renderResults(s);
  }
}

// ---- Edit UI -------------------------------------------------------------
async function renderEditUI(s) {
  els.progress.hidden = true;
  els.edit.hidden = false;
  els.editList.innerHTML = "";
  for (let i = 0; i < s.payloads.length; i++) {
    const p = s.payloads[i];
    if (!p.glossary_path) continue;
    const wrap = document.createElement("details");
    wrap.className = "gedit";
    wrap.open = s.payloads.length === 1;
    const summary = document.createElement("summary");
    summary.className = "gedit-title";
    summary.textContent = `${p.name} — ${p.glossary_path.split(/[\\/]/).pop()}`;
    wrap.appendChild(summary);
    const ta = document.createElement("textarea");
    ta.dataset.idx = String(i);
    ta.textContent = "Loading…";
    wrap.appendChild(ta);
    els.editList.appendChild(wrap);
    try {
      const r = await fetch(`/api/glossary/${currentJobId}/${i}`);
      const data = await r.json();
      ta.value = data.content || "";
    } catch (err) {
      ta.value = `[error loading glossary: ${err.message}]`;
    }
  }
}

async function saveAllEdits() {
  const textareas = els.editList.querySelectorAll("textarea");
  for (const ta of textareas) {
    const idx = ta.dataset.idx;
    const r = await fetch(`/api/glossary/${currentJobId}/${idx}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: ta.value }),
    });
    if (!r.ok) throw new Error(`save failed for file ${idx}`);
  }
}

els.saveBtn.addEventListener("click", async () => {
  els.saveBtn.disabled = true;
  els.saveStatus.textContent = "Saving…";
  try {
    await saveAllEdits();
    els.saveStatus.textContent = "Saved.";
    setTimeout(() => { els.saveStatus.textContent = ""; }, 3000);
  } catch (err) {
    els.saveStatus.textContent = `Save failed: ${err.message}`;
  } finally {
    els.saveBtn.disabled = false;
  }
});

els.continueBtn.addEventListener("click", async () => {
  els.continueBtn.disabled = true;
  els.cancelBtn.disabled = true;
  els.saveBtn.disabled = true;
  els.saveStatus.textContent = "Saving…";
  try {
    await saveAllEdits();
  } catch (err) {
    els.saveStatus.textContent = `Save failed: ${err.message}`;
    els.continueBtn.disabled = false;
    els.cancelBtn.disabled = false;
    els.saveBtn.disabled = false;
    return;
  }
  els.saveStatus.textContent = "";
  // Kick off Phase 2.
  const r = await fetch(`/api/continue/${currentJobId}`, { method: "POST" });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    showError(`Continue failed: ${err.error || r.statusText}`);
    els.continueBtn.disabled = false;
    els.cancelBtn.disabled = false;
    els.saveBtn.disabled = false;
    return;
  }
  els.edit.hidden = true;
  els.progress.hidden = false;
  els.progressTitle.textContent = "Progress — Phase 2: translating";
  els.log.textContent = "";
  startPolling();
});

els.cancelBtn.addEventListener("click", async () => {
  if (currentJobId) {
    await fetch(`/api/cancel/${currentJobId}`, { method: "POST" });
  }
  resetToForm();
});

// ---- Results -------------------------------------------------------------
function renderResults(s) {
  els.progress.hidden = true;
  els.edit.hidden = true;
  els.results.hidden = false;
  els.resultsBanner.innerHTML = "";
  els.resultsBody.innerHTML = "";
  const ok = s.total_count - s.failures.length;
  const banner = document.createElement("div");
  if (s.failures.length) {
    banner.className = "banner warning";
    banner.textContent =
      `${ok}/${s.total_count} succeeded — ${s.failures.length} failed.`;
  } else {
    banner.className = "banner success";
    banner.textContent =
      `Done — ${ok} file(s), ${s.totals.blocks} blocks, ` +
      `${s.totals.chars} chars, ${s.outputs.length} output file(s).`;
  }
  els.resultsBanner.appendChild(banner);

  if (s.failures.length) {
    const ul = document.createElement("ul");
    for (const [name, err] of s.failures) {
      const li = document.createElement("li");
      li.innerHTML = `<strong>${escapeHtml(name)}</strong> — ${escapeHtml(err)}`;
      ul.appendChild(li);
    }
    els.resultsBody.appendChild(ul);
  }

  if (s.saved_paths.length) {
    const h = document.createElement("p");
    h.textContent = "Saved:";
    els.resultsBody.appendChild(h);
    const ul = document.createElement("ul");
    ul.className = "saved-list";
    for (const path of s.saved_paths) {
      const li = document.createElement("li");
      li.textContent = path;
      ul.appendChild(li);
    }
    els.resultsBody.appendChild(ul);
  }
  if (s.outputs.length) {
    const h = document.createElement("p");
    h.textContent = "Download:";
    els.resultsBody.appendChild(h);
    for (const name of s.outputs) {
      const a = document.createElement("a");
      a.href = `/api/download/${currentJobId}/${encodeURIComponent(name)}`;
      a.textContent = name;
      a.className = "download-link";
      a.setAttribute("download", "");
      els.resultsBody.appendChild(a);
    }
  }

  const done = document.createElement("button");
  done.className = "btn-secondary reset-btn";
  done.textContent = "Start another translation";
  done.addEventListener("click", resetToForm);
  els.resultsBody.appendChild(done);
}

// ---- Reset ---------------------------------------------------------------
function hideAll() {
  els.progress.hidden = true;
  els.edit.hidden = true;
  els.results.hidden = true;
}
function resetToForm() {
  stopPolling();
  // Server-side cleanup: drops the finished job from JOBS and frees its temp dir.
  if (currentJobId) {
    fetch(`/api/cancel/${currentJobId}`, { method: "POST" }).catch(() => {});
  }
  currentJobId = null;
  hideAll();
  els.submitBtn.disabled = els.filesInput.files.length === 0;
}
function showError(msg) {
  hideAll();
  els.results.hidden = false;
  const b = document.createElement("div");
  b.className = "banner error";
  b.textContent = msg;
  els.resultsBanner.innerHTML = "";
  els.resultsBanner.appendChild(b);
  els.resultsBody.innerHTML = "";
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---- About dialog --------------------------------------------------------
els.aboutBtn.addEventListener("click", () => els.aboutDialog.showModal());

// ---- Ollama status + start/stop + model picker ---------------------------
// Refreshes the status pill, enables/disables Start/Stop, and repopulates the model dropdown from /api/ollama/models.
// Called on load, after Start/Stop, and every 5s while the page is open.
async function refreshOllamaStatus() {
  let status;
  try {
    const r = await fetch("/api/ollama/status");
    status = await r.json();
  } catch (err) {
    status = { running: false, version: null, managed: false };
  }
  if (status.running) {
    els.ollamaStatusText.textContent = `running${status.version ? ` (v${status.version})` : ""}`;
    els.ollamaStatusText.className = "status-ok";
  } else {
    els.ollamaStatusText.textContent = "not running";
    els.ollamaStatusText.className = "status-bad";
  }
  els.startOllamaBtn.disabled = status.running;
  // Only allow Stop for a process we started ourselves.
  els.stopOllamaBtn.disabled = !(status.running && status.managed);
  els.stopOllamaBtn.title = els.stopOllamaBtn.disabled && status.running
    ? "Ollama wasn't started by this app — use the menubar app to stop it."
    : "";
  els.pullBtn.disabled = !status.running || !els.pullName.value.trim();
  await refreshModelPicker(status.running);
}

async function refreshModelPicker(running) {
  if (!running) {
    els.modelSelect.hidden = true;
    els.modelInput.hidden = false;
    els.modelSelect.name = "";
    els.modelInput.name = "model";
    return;
  }
  let models = [];
  try {
    const r = await fetch("/api/ollama/models");
    const data = await r.json();
    models = data.models || [];
  } catch (err) { /* keep fallback */ }
  if (!models.length) {
    els.modelSelect.hidden = true;
    els.modelInput.hidden = false;
    els.modelSelect.name = "";
    els.modelInput.name = "model";
    return;
  }
  const prev = els.modelSelect.value || els.modelInput.value || "translategemma";
  els.modelSelect.innerHTML = "";
  for (const m of models) {
    const opt = document.createElement("option");
    opt.value = m.name;
    opt.textContent = `${m.name}  (${m.params}, ${m.size_gb.toFixed(1)} GB)`;
    els.modelSelect.appendChild(opt);
  }
  if ([...els.modelSelect.options].some((o) => o.value === prev)) {
    els.modelSelect.value = prev;
  }
  els.modelSelect.hidden = false;
  els.modelInput.hidden = true;
  els.modelSelect.name = "model";
  els.modelInput.name = "";
}

els.startOllamaBtn.addEventListener("click", async () => {
  els.startOllamaBtn.disabled = true;
  els.ollamaStatusText.textContent = "starting…";
  const r = await fetch("/api/ollama/start", { method: "POST" });
  const data = await r.json().catch(() => ({}));
  if (!data.ok && data.error) alert(data.error);
  await refreshOllamaStatus();
});

els.stopOllamaBtn.addEventListener("click", async () => {
  els.stopOllamaBtn.disabled = true;
  els.ollamaStatusText.textContent = "stopping…";
  const r = await fetch("/api/ollama/stop", { method: "POST" });
  const data = await r.json().catch(() => ({}));
  if (!data.ok && data.error) alert(data.error);
  await refreshOllamaStatus();
});

// ---- Logs dialog ---------------------------------------------------------
els.viewLogsLink.addEventListener("click", async (e) => {
  e.preventDefault();
  await loadOllamaLogs();
  els.logsDialog.showModal();
});
els.refreshLogsBtn.addEventListener("click", loadOllamaLogs);
async function loadOllamaLogs() {
  els.logsBody.textContent = "Loading…";
  try {
    const r = await fetch("/api/ollama/log?n=500");
    const data = await r.json();
    els.logsPath.textContent = data.path ? `Log file: ${data.path}` : "No log file found.";
    els.logsBody.textContent = data.log || "(empty)";
    els.logsBody.scrollTop = els.logsBody.scrollHeight;
  } catch (err) {
    els.logsBody.textContent = `Failed to load logs: ${err.message}`;
  }
}

// ---- Pull a new model ----------------------------------------------------
let pullPollTimer = null;
els.pullName.addEventListener("input", () => {
  els.pullBtn.disabled = !els.pullName.value.trim();
});
els.pullBtn.addEventListener("click", async () => {
  const name = els.pullName.value.trim();
  if (!name) return;
  els.pullBtn.disabled = true;
  const r = await fetch("/api/ollama/pull", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model: name }),
  });
  const data = await r.json().catch(() => ({}));
  if (data.already_installed) {
    alert(`${name} is already installed.`);
    els.pullBtn.disabled = false;
    return;
  }
  if (!data.ok) {
    alert(data.error || "Failed to start pull");
    els.pullBtn.disabled = false;
    return;
  }
  els.pullProgress.hidden = false;
  els.pullLog.textContent = "Starting…";
  if (pullPollTimer) clearInterval(pullPollTimer);
  pullPollTimer = setInterval(pollPull, 1000);
});
els.cancelPullBtn.addEventListener("click", async () => {
  await fetch("/api/ollama/pull/cancel", { method: "POST" });
});
async function pollPull() {
  try {
    const r = await fetch("/api/ollama/pull/status");
    const data = await r.json();
    els.pullLog.textContent = data.log || "";
    if (data.status === "done" || data.status === "error"
        || data.status === "cancelled") {
      clearInterval(pullPollTimer);
      pullPollTimer = null;
      els.pullBtn.disabled = !els.pullName.value.trim();
      if (data.status === "done") {
        // Refresh model list so the freshly-pulled model shows up.
        await refreshModelPicker(true);
      }
    }
  } catch (err) { /* transient */ }
}

// ---- Init ----------------------------------------------------------------
populateLangs();
setDefaultOutputDir();
refreshOllamaStatus();
// Refresh status every 5s so the pill stays current if Ollama goes up/down.
setInterval(refreshOllamaStatus, 5000);
