// Client-side controller for the olladoc Flask app.
//
// State model:
//   idle             — form visible, no active job
//   running_phase1   — poll /api/status, show progress panel
//   awaiting_edit    — Phase 1 done; render editable glossary panels
//   running_phase2   — poll /api/status, show progress panel
//   done             — render results panel + download links

// TranslateGemma's trained-and-evaluated languages (the WMT24++ set, dialect variants collapsed), defaults first.
const LANGS = [
  "Spanish", "English",
  "Arabic", "Bengali", "Bulgarian", "Catalan", "Chinese", "Croatian", "Czech",
  "Danish", "Dutch", "Estonian", "Filipino", "Finnish", "French", "German",
  "Greek", "Gujarati", "Hebrew", "Hindi", "Hungarian", "Icelandic",
  "Indonesian", "Italian", "Japanese", "Kannada", "Korean", "Latvian",
  "Lithuanian", "Malayalam", "Marathi", "Norwegian", "Persian", "Polish",
  "Portuguese", "Punjabi", "Romanian", "Russian", "Serbian", "Slovak",
  "Slovenian", "Swahili", "Swedish", "Tamil", "Telugu", "Thai", "Turkish",
  "Ukrainian", "Urdu", "Vietnamese", "Zulu",
];

// ---- Elements ------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const els = {
  form: $("form"),
  formFields: $("formFields"),
  ollamaGateHint: $("ollamaGateHint"),
  dropzone: $("dropzone"),
  filesInput: $("files"),
  fileList: $("fileList"),
  sourceLang: $("sourceLang"),
  targetLang: $("targetLang"),
  modelSelect: $("modelSelect"),
  modelInput: $("modelInput"),
  personaSelect: $("personaSelect"),
  personaCustom: $("personaCustom"),
  baseGlossary: $("baseGlossary"),
  useBaseGlossary: $("useBaseGlossary"),
  baseGlossaryError: $("baseGlossaryError"),
  globalGlossaryHint: $("globalGlossaryHint"),
  addGlobalBtn: $("addGlobalBtn"),
  viewGlobalLink: $("viewGlobalLink"),
  conflictDialog: $("conflictDialog"),
  conflictList: $("conflictList"),
  conflictKeepBtn: $("conflictKeepBtn"),
  conflictUpdateBtn: $("conflictUpdateBtn"),
  globalDialog: $("globalDialog"),
  globalDialogPath: $("globalDialogPath"),
  globalDialogBody: $("globalDialogBody"),
  promptPreviewLink: $("promptPreviewLink"),
  promptDialog: $("promptDialog"),
  promptBody: $("promptBody"),
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
  footerAboutLink: $("footerAboutLink"),
  cancelJobBtn: $("cancelJobBtn"),
  // Tour
  tourLink: $("tourLink"),
  tourPop: $("tourPop"),
  tourPopTitle: $("tourPopTitle"),
  tourPopText: $("tourPopText"),
  tourPopCount: $("tourPopCount"),
  tourPrevBtn: $("tourPrevBtn"),
  tourNextBtn: $("tourNextBtn"),
  tourCloseBtn: $("tourCloseBtn"),
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
  modelMissing: $("modelMissing"),
  getModelBtn: $("getModelBtn"),
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
// Single-file app: dropping or browsing a new file replaces the current selection, and the list row has a remove button. selectedFiles is the source of truth (0 or 1 entries); the hidden input is kept in sync so form submission sees the same file.
let selectedFiles = [];

function syncFileInput() {
  const dt = new DataTransfer();
  for (const f of selectedFiles) dt.items.add(f);
  els.filesInput.files = dt.files;
}

function addFiles(fileList) {
  if (els.formFields.disabled) return;
  // Single-file app: the newest valid file replaces the current selection.
  for (const f of Array.from(fileList || [])) {
    if (!/\.(pdf|docx)$/i.test(f.name)) continue;
    selectedFiles = [f];
    break;
  }
  syncFileInput();
  renderFileList();
}

function removeFile(idx) {
  selectedFiles.splice(idx, 1);
  syncFileInput();
  renderFileList();
}

function renderFileList() {
  els.fileList.innerHTML = "";
  selectedFiles.forEach((f, i) => {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = f.name;
    const size = document.createElement("span");
    size.className = "filesize";
    size.textContent = fmtBytes(f.size);
    const rm = document.createElement("button");
    rm.type = "button";
    rm.className = "file-remove";
    rm.title = "Remove";
    rm.textContent = "✕";
    rm.addEventListener("click", (e) => {
      e.preventDefault();
      removeFile(i);
    });
    li.appendChild(name);
    li.appendChild(size);
    li.appendChild(rm);
    els.fileList.appendChild(li);
  });
  els.submitBtn.disabled = selectedFiles.length === 0;
}
function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

// Drag-and-drop wiring.
els.filesInput.addEventListener("change", () => addFiles(els.filesInput.files));
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
  if (e.dataTransfer && e.dataTransfer.files) {
    addFiles(e.dataTransfer.files);
  }
});

// Clicking the grayed-out form (the disabled fieldset is click-transparent, so the form catches it) explains the gate and points at the Ollama bar.
els.form.addEventListener("click", () => {
  if (!els.formFields.disabled) return;
  const bar = document.querySelector(".ollama-bar");
  bar.classList.remove("attn");
  void bar.offsetWidth;   // restart the animation on repeat clicks
  bar.classList.add("attn");
});

// ---- Submit --------------------------------------------------------------
let currentJobId = null;
let pollTimer = null;

els.form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const files = Array.from(els.filesInput.files || []);
  if (files.length === 0) return;
  if (els.useBaseGlossary.checked && !els.baseGlossary.files.length) {
    $("secAdvanced").open = true;
    els.baseGlossary.hidden = false;
    els.baseGlossaryError.hidden = false;
    els.baseGlossary.scrollIntoView({ block: "center", behavior: "instant" });
    return;
  }
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
  fd.append("domain", els.personaSelect.value === "__custom__"
            ? els.personaCustom.value.trim()
            : els.personaSelect.value);
  // The keep-glossary checkbox is currently commented out of the form; when absent, the glossary is always kept (backend default).
  const keepGlossaryEl = els.form.querySelector('[name=keep_glossary]');
  fd.append("keep_glossary", keepGlossaryEl && !keepGlossaryEl.checked ? "false" : "true");
  fd.append("timestamp",
            els.form.querySelector('[name=timestamp]').checked ? "true" : "false");
  fd.append("debug_dump",
            els.form.querySelector('[name=debug_dump]').checked ? "true" : "false");
  fd.append("use_global_glossary",
            els.form.querySelector('[name=use_global_glossary]').checked ? "true" : "false");
  if (els.useBaseGlossary.checked && els.baseGlossary.files.length) {
    fd.append("base_glossary", els.baseGlossary.files[0]);
  }
  els.submitBtn.disabled = true;
  hideAll();
  resetCancelJobBtn();
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
    els.progressTitle.textContent = "Progress · Phase 1: building glossary";
  } else if (s.status === "running_phase2") {
    els.progressTitle.textContent = "Progress · Phase 2: translating";
  } else if (s.status === "running") {
    els.progressTitle.textContent = "Progress · building glossary + translating";
  } else if (s.status === "awaiting_edit") {
    stopPolling();
    renderEditUI(s);
  } else if (s.status === "done") {
    stopPolling();
    renderResults(s);
  } else if (s.status === "cancelled") {
    stopPolling();
    renderCancelled(s);
  }
}

// ---- Cancel a running translation ---------------------------------------
els.cancelJobBtn.addEventListener("click", async () => {
  if (!currentJobId) return;
  els.cancelJobBtn.disabled = true;
  els.cancelJobBtn.textContent = "Cancelling…";
  try {
    await fetch(`/api/cancel_job/${currentJobId}`, { method: "POST" });
  } catch (err) { /* polling will surface the state either way */ }
});
function resetCancelJobBtn() {
  els.cancelJobBtn.disabled = false;
  els.cancelJobBtn.textContent = "Cancel";
}

function renderCancelled(s) {
  els.progress.hidden = true;
  els.edit.hidden = true;
  els.results.hidden = false;
  els.resultsBanner.innerHTML = "";
  els.resultsBody.innerHTML = "";
  const banner = document.createElement("div");
  banner.className = "banner warning";
  banner.textContent = s.saved_paths.length
    ? `Cancelled. ${s.saved_paths.length} file(s) finished before cancelling were saved.`
    : "Cancelled. No files were completed.";
  els.resultsBanner.appendChild(banner);
  if (s.saved_paths.length) {
    const ul = document.createElement("ul");
    ul.className = "saved-list";
    for (const path of s.saved_paths) {
      const li = document.createElement("li");
      li.textContent = path;
      ul.appendChild(li);
    }
    els.resultsBody.appendChild(ul);
  }
  const done = document.createElement("button");
  done.className = "btn-secondary reset-btn";
  done.textContent = "Back to the form";
  done.addEventListener("click", resetToForm);
  els.resultsBody.appendChild(done);
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
    summary.textContent = `${p.name} · ${p.glossary_path.split(/[\\/]/).pop()}`;
    wrap.appendChild(summary);
    const ta = document.createElement("textarea");
    ta.dataset.idx = String(i);
    ta.textContent = "Loading…";
    wrap.appendChild(ta);
    const issuesDiv = document.createElement("div");
    issuesDiv.className = "gedit-issues";
    issuesDiv.hidden = true;
    wrap.appendChild(issuesDiv);
    ta.addEventListener("input", () => scheduleValidate(ta, issuesDiv));
    els.editList.appendChild(wrap);
    try {
      const r = await fetch(`/api/glossary/${currentJobId}/${i}`);
      const data = await r.json();
      ta.value = data.content || "";
      if (data.notes && data.notes.length) {
        const nd = document.createElement("div");
        nd.className = "review-notes";
        const heading = document.createElement("strong");
        heading.textContent = "Worth a look before you continue:";
        nd.appendChild(heading);
        const ul = document.createElement("ul");
        for (const n of data.notes) {
          const li = document.createElement("li");
          li.textContent = n;
          ul.appendChild(li);
        }
        nd.appendChild(ul);
        wrap.insertBefore(nd, ta);
      }
      validateTA(ta, issuesDiv);
    } catch (err) {
      ta.value = `[error loading glossary: ${err.message}]`;
    }
  }
}

// ---- Glossary format validation ------------------------------------------
// Live feedback while editing (debounced), plus a confirm gate on Continue: error-level lines are silently dropped by the parser, so the user should know before Phase 2 runs without them.
const validateTimers = new Map();
function scheduleValidate(ta, issuesDiv) {
  clearTimeout(validateTimers.get(ta));
  validateTimers.set(ta, setTimeout(() => validateTA(ta, issuesDiv), 600));
}
async function validateTA(ta, issuesDiv) {
  try {
    const r = await fetch("/api/glossary/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: ta.value }),
    });
    const data = await r.json();
    renderIssues(issuesDiv, data.issues || []);
    return data.issues || [];
  } catch (err) {
    return [];
  }
}
function renderIssues(div, issues) {
  div.innerHTML = "";
  div.hidden = !issues.length;
  for (const it of issues) {
    const row = document.createElement("div");
    row.className = it.level === "error" ? "issue issue-error" : "issue issue-warning";
    row.textContent = `${it.level === "error" ? "✖" : "⚠"} Line ${it.line}: ${it.message}`;
    div.appendChild(row);
  }
}
async function countGlossaryErrors() {
  let errors = 0;
  for (const ta of els.editList.querySelectorAll("textarea")) {
    const issuesDiv = ta.parentElement.querySelector(".gedit-issues");
    const issues = await validateTA(ta, issuesDiv);
    errors += issues.filter((i) => i.level === "error").length;
  }
  return errors;
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

// Append reviewed terms to the personal global glossary. Only offered here, on the review screen, so everything in the global file has passed human review.
els.addGlobalBtn.addEventListener("click", async () => {
  els.addGlobalBtn.disabled = true;
  els.saveStatus.textContent = "Adding to global glossary…";
  try {
    const content = Array.from(els.editList.querySelectorAll("textarea"))
      .map((ta) => ta.value).join("\n");
    const r = await fetch("/api/global_glossary/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
    const data = await r.json();
    const parts = [];
    if (data.added) parts.push(`Added ${data.added} new term(s).`);
    if (data.unchanged && data.unchanged.length) {
      parts.push(`Already in your global glossary: ${data.unchanged.join(", ")}.`);
    }
    if (data.conflicts && data.conflicts.length) {
      // Past human judgment (the global) vs present human judgment (this review): only the user knows which is right, per term, so ask on the spot with a checkbox each.
      const chosen = await resolveConflicts(data.conflicts);
      if (chosen.length) {
        const r2 = await fetch("/api/global_glossary/add", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content, overwrite: chosen }),
        });
        const d2 = await r2.json();
        if (d2.updated && d2.updated.length) {
          parts.push(`Updated in the global glossary: ${d2.updated.join(", ")}.`);
        }
      }
      const kept = data.conflicts.map((x) => x.term).filter((t) => !chosen.includes(t));
      if (kept.length) parts.push(`Kept the global version for: ${kept.join(", ")}.`);
    }
    els.saveStatus.textContent = parts.join(" ") || "Nothing to add.";
  } catch (err) {
    els.saveStatus.textContent = `Failed to update global glossary: ${err.message}`;
  } finally {
    els.addGlobalBtn.disabled = false;
  }
});

// Checking the base-glossary box reveals the file input and opens the picker right away; unchecking clears the selection.
els.useBaseGlossary.addEventListener("change", () => {
  els.baseGlossaryError.hidden = true;
  if (els.useBaseGlossary.checked) {
    els.baseGlossary.hidden = false;
    els.baseGlossary.click();
  } else {
    els.baseGlossary.value = "";
    els.baseGlossary.hidden = true;
  }
});
els.baseGlossary.addEventListener("change", () => {
  if (els.baseGlossary.files.length) els.baseGlossaryError.hidden = true;
});

els.viewGlobalLink.addEventListener("click", async (e) => {
  e.preventDefault();
  els.globalDialogBody.textContent = "Loading…";
  els.globalDialogPath.textContent = "";
  els.globalDialog.showModal();
  try {
    const r = await fetch("/api/global_glossary");
    const g = await r.json();
    if (!g.exists) {
      els.globalDialogPath.textContent = `Will be created at ${g.path}`;
      els.globalDialogBody.textContent = "No global glossary yet. Run a two-phase translation and use the Add new terms to global glossary button on the review screen to start one.";
      return;
    }
    els.globalDialogPath.textContent = `${g.entries} term(s) · ${g.path}`;
    els.globalDialogBody.textContent = g.content;
  } catch (err) {
    els.globalDialogBody.textContent = `Failed to load: ${err.message}`;
  }
});

// Shows the conflict dialog and resolves with the terms the user ticked for updating. Closing the dialog any other way (Escape, Keep button) resolves with none.
function resolveConflicts(conflicts) {
  return new Promise((resolve) => {
    els.conflictList.innerHTML = "";
    conflicts.forEach((c, idx) => {
      const wrap = document.createElement("div");
      wrap.className = "conflict-item";
      const title = document.createElement("div");
      title.className = "conflict-term";
      title.textContent = c.term;
      wrap.appendChild(title);
      // One radio pair per term: the global's definition (default, so nothing changes without an active choice) vs this review's.
      [["global", c.kept, "global glossary"], ["review", c.offered, "this review"]].forEach(([val, definition, tag]) => {
        const row = document.createElement("label");
        row.className = "radio conflict-option";
        const rb = document.createElement("input");
        rb.type = "radio";
        rb.name = `conflict_${idx}`;
        rb.value = val;
        rb.dataset.term = c.term;
        if (val === "global") rb.checked = true;
        const span = document.createElement("span");
        span.innerHTML = `${escapeHtml(definition)} <span class="conflict-tag">(${tag})</span>`;
        row.appendChild(rb);
        row.appendChild(span);
        wrap.appendChild(row);
      });
      els.conflictList.appendChild(wrap);
    });
    let settled = false;
    const settle = (val) => { if (!settled) { settled = true; resolve(val); } };
    const collect = () =>
      Array.from(els.conflictList.querySelectorAll('input[value="review"]:checked')).map((i) => i.dataset.term);
    const onUpdate = () => { settle(collect()); els.conflictDialog.close(); };
    const onKeep = () => { settle([]); els.conflictDialog.close(); };
    const onClose = () => {
      settle([]);
      els.conflictUpdateBtn.removeEventListener("click", onUpdate);
      els.conflictKeepBtn.removeEventListener("click", onKeep);
      els.conflictDialog.removeEventListener("close", onClose);
    };
    els.conflictUpdateBtn.addEventListener("click", onUpdate);
    els.conflictKeepBtn.addEventListener("click", onKeep);
    els.conflictDialog.addEventListener("close", onClose);
    els.conflictDialog.showModal();
  });
}

async function refreshGlobalGlossaryHint() {
  try {
    const r = await fetch("/api/global_glossary");
    const g = await r.json();
    els.globalGlossaryHint.textContent = g.exists
      ? `Your global glossary has ${g.entries} term(s), stored at ${g.path}.`
      : "Your global glossary is a personal term list that grows as you approve terms on the review screen.";
  } catch (err) { /* keep the default hint */ }
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
  let leavingToPhase2 = false;
  try {
    const nErrors = await countGlossaryErrors();
    if (nErrors && !confirm(`${nErrors} glossary line(s) have format problems (marked ✖ below) and will be IGNORED during translation. Continue anyway?`)) {
      return;
    }
    els.saveStatus.textContent = "Saving…";
    await saveAllEdits();
    els.saveStatus.textContent = "";
    const r = await fetch(`/api/continue/${currentJobId}`, { method: "POST" });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      showError(`Continue failed: ${err.error || r.statusText}`);
      return;
    }
    leavingToPhase2 = true;
    els.edit.hidden = true;
    resetCancelJobBtn();
    els.progress.hidden = false;
    els.progressTitle.textContent = "Progress · Phase 2: translating";
    els.log.textContent = "";
    startPolling();
  } catch (err) {
    els.saveStatus.textContent = `Continue failed: ${err.message}`;
  } finally {
    if (!leavingToPhase2) {
      els.continueBtn.disabled = false;
      els.cancelBtn.disabled = false;
      els.saveBtn.disabled = false;
    }
  }
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
      `${ok}/${s.total_count} succeeded, ${s.failures.length} failed.`;
  } else {
    banner.className = "banner success";
    banner.textContent =
      `Done: ${ok} file(s), ${s.totals.blocks} blocks, ` +
      `${s.totals.chars} chars, ${s.outputs.length} output file(s).`;
  }
  els.resultsBanner.appendChild(banner);

  if (s.failures.length) {
    const ul = document.createElement("ul");
    for (const [name, err] of s.failures) {
      const li = document.createElement("li");
      li.innerHTML = `<strong>${escapeHtml(name)}</strong>: ${escapeHtml(err)}`;
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

// ---- Persona picker ------------------------------------------------------
els.personaSelect.addEventListener("change", () => {
  els.personaCustom.hidden = els.personaSelect.value !== "__custom__";
  if (!els.personaCustom.hidden) els.personaCustom.focus();
});

// ---- Prompt preview ------------------------------------------------------
els.promptPreviewLink.addEventListener("click", async (e) => {
  e.preventDefault();
  const domain = els.personaSelect.value === "__custom__"
    ? els.personaCustom.value.trim()
    : els.personaSelect.value;
  const model = !els.modelSelect.hidden ? els.modelSelect.value : els.modelInput.value;
  const params = new URLSearchParams({
    domain, model,
    source_lang: els.sourceLang.value,
    target_lang: els.targetLang.value,
  });
  els.promptBody.textContent = "Loading…";
  els.promptDialog.showModal();
  try {
    const r = await fetch(`/api/prompt/preview?${params}`);
    const data = await r.json();
    els.promptBody.textContent = data.prompt || "(no prompt)";
  } catch (err) {
    els.promptBody.textContent = `Failed to load: ${err.message}`;
  }
});

// ---- About dialog --------------------------------------------------------
els.aboutBtn.addEventListener("click", () => els.aboutDialog.showModal());
els.footerAboutLink.addEventListener("click", (e) => {
  e.preventDefault();
  els.aboutDialog.showModal();
});

// ---- Guided tour ---------------------------------------------------------
// Each step points at a live element; the popover is repositioned on scroll/resize so it tracks its target.
const TOUR_STEPS = [
  { target: () => document.querySelector(".ollama-bar"), title: "Ollama", text: "Ollama is a local AI engine that lets you run large language models directly on your own computer. It must be running for olladoc to translate. If the status here is not green, click Start. The 🔍 opens Ollama's logs if you need to troubleshoot." },
  { target: () => $("secDocument"), title: "Add a document", text: "Drop a PDF or Word file here, or click Browse files." },
  { target: () => $("secLanguages"), title: "Languages", text: "Pick the document's language and the language to translate into. Spanish and English are the most tested pair." },
  { target: () => $("secModel"), title: "Model", text: () => els.modelMissing.hidden
      ? "This is already set up for you. Change it only if you want to experiment with other models."
      : "The default model is not installed yet. Click Download translategemma to get it (a one-time download of a few GB)." },
  { target: () => $("secWorkflow"), title: "Workflow", text: "Pick how to run: straight through, or with a pause to review the glossary before translating. Two-phase is worth it when exact terminology matters." },
  { target: () => $("secAdvanced"), title: "Advanced options", text: "Optional settings live here: the translator persona, a preview of the translation prompt, and how output files are saved." },
  { target: () => $("secOutput"), title: "Output folder", text: "Finished files are saved here and also appear below as download links when the translation completes." },
  { target: () => els.submitBtn, title: "Translate", text: "Click Translate to start. Progress appears below, and you can cancel while it runs." },
];
let tourIdx = -1;

function tourShow(i) {
  const el = TOUR_STEPS[i].target();
  if (!el) { tourEnd(); return; }
  document.querySelectorAll(".tour-highlight").forEach((n) => n.classList.remove("tour-highlight"));
  el.classList.add("tour-highlight");
  tourIdx = i;
  els.tourPopTitle.textContent = TOUR_STEPS[i].title;
  const stepText = TOUR_STEPS[i].text;
  els.tourPopText.textContent = typeof stepText === "function" ? stepText() : stepText;
  els.tourPopCount.textContent = `${i + 1} / ${TOUR_STEPS.length}`;
  els.tourPrevBtn.disabled = i === 0;
  els.tourNextBtn.textContent = i === TOUR_STEPS.length - 1 ? "Done" : "Next";
  els.tourPop.hidden = false;
  el.scrollIntoView({ block: "center", behavior: "instant" });
  tourPosition();
}
function tourPosition() {
  if (tourIdx < 0) return;
  const el = TOUR_STEPS[tourIdx].target();
  if (!el) return;
  const rect = el.getBoundingClientRect();
  const pop = els.tourPop;
  let top = rect.bottom + 10;
  if (top + pop.offsetHeight > window.innerHeight - 10) {
    top = Math.max(10, rect.top - pop.offsetHeight - 10);
  }
  const left = Math.min(Math.max(10, rect.left), window.innerWidth - pop.offsetWidth - 10);
  pop.style.top = `${top}px`;
  pop.style.left = `${left}px`;
}
function tourEnd() {
  tourIdx = -1;
  els.tourPop.hidden = true;
  document.querySelectorAll(".tour-highlight").forEach((n) => n.classList.remove("tour-highlight"));
}
els.tourLink.addEventListener("click", (e) => { e.preventDefault(); tourShow(0); });
els.tourNextBtn.addEventListener("click", () => {
  if (tourIdx >= TOUR_STEPS.length - 1) tourEnd();
  else tourShow(tourIdx + 1);
});
els.tourPrevBtn.addEventListener("click", () => { if (tourIdx > 0) tourShow(tourIdx - 1); });
els.tourCloseBtn.addEventListener("click", tourEnd);
window.addEventListener("scroll", tourPosition, { passive: true });
window.addEventListener("resize", tourPosition);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && tourIdx >= 0) tourEnd(); });

// ---- Ollama status + start/stop + model picker ---------------------------
// Refreshes the status pill, enables/disables Start/Stop, and repopulates the model dropdown from /api/ollama/models.
// Called on load, after Start/Stop, and every 5s while the page is open.
// Tracks whether Ollama changed state outside the app (desktop app, terminal), so the status bar can say so instead of looking buggy.
let ollamaWasRunning = null;
let ollamaStoppedExternally = false;
let appIsChangingOllama = false;

async function refreshOllamaStatus() {
  let status;
  try {
    const r = await fetch("/api/ollama/status");
    status = await r.json();
  } catch (err) {
    status = { running: false, version: null, managed: false };
  }
  if (!status.running && ollamaWasRunning === true && !appIsChangingOllama) {
    ollamaStoppedExternally = true;
  }
  if (status.running) ollamaStoppedExternally = false;
  ollamaWasRunning = status.running;
  if (status.running) {
    const external = status.managed ? "" : " · started outside the app";
    els.ollamaStatusText.textContent = `running${status.version ? ` (v${status.version})` : ""}${external}`;
    els.ollamaStatusText.className = "status-ok";
  } else {
    const external = ollamaStoppedExternally ? " · stopped outside the app" : "";
    els.ollamaStatusText.textContent = `not running${external}`;
    els.ollamaStatusText.className = "status-bad";
  }
  // Gate the whole form until Ollama is running; the hint explains the gray-out.
  els.formFields.disabled = !status.running;
  els.ollamaGateHint.hidden = status.running;
  els.startOllamaBtn.disabled = status.running;
  // Only allow Stop for a process we started ourselves.
  els.stopOllamaBtn.disabled = !(status.running && status.managed);
  els.stopOllamaBtn.title = els.stopOllamaBtn.disabled && status.running
    ? "Ollama was started outside this app, so stop it from where it was started (desktop app or terminal)."
    : "";
  els.pullBtn.disabled = !status.running || !els.pullName.value.trim();
  await refreshModelPicker(status.running);
}

async function refreshModelPicker(running) {
  if (!running) {
    els.modelMissing.hidden = true;   // can't know what's installed without Ollama
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
  // First-run helper: show the download banner while the default model isn't installed. Any translategemma variant (tags, size suffixes, hf.co imports) counts.
  const hasDefault = models.some((m) => m.name.toLowerCase().includes("translategemma"));
  els.modelMissing.hidden = hasDefault;
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
    const isDefault = m.name.toLowerCase().includes("translategemma");
    opt.textContent = `${m.name}  (${isDefault ? "default · " : ""}${m.params}, ${m.size_gb.toFixed(1)} GB)`;
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
  appIsChangingOllama = true;
  els.startOllamaBtn.disabled = true;
  els.ollamaStatusText.textContent = "starting…";
  const r = await fetch("/api/ollama/start", { method: "POST" });
  const data = await r.json().catch(() => ({}));
  if (!data.ok && data.error) alert(data.error);
  await refreshOllamaStatus();
  appIsChangingOllama = false;
});

els.stopOllamaBtn.addEventListener("click", async () => {
  appIsChangingOllama = true;
  els.stopOllamaBtn.disabled = true;
  els.ollamaStatusText.textContent = "stopping…";
  const r = await fetch("/api/ollama/stop", { method: "POST" });
  const data = await r.json().catch(() => ({}));
  if (!data.ok && data.error) alert(data.error);
  await refreshOllamaStatus();
  appIsChangingOllama = false;
});

// ---- Logs dialog ---------------------------------------------------------
els.viewLogsLink.addEventListener("click", async (e) => {
  e.preventDefault();
  await loadOllamaLogs();
  els.logsDialog.showModal();
});
els.refreshLogsBtn.addEventListener("click", loadOllamaLogs);
function fmtAge(seconds) {
  if (seconds < 90) return "just now";
  if (seconds < 5400) return `${Math.round(seconds / 60)} minutes ago`;
  if (seconds < 129600) return `${Math.round(seconds / 3600)} hours ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

async function loadOllamaLogs() {
  els.logsBody.textContent = "Loading…";
  try {
    const r = await fetch("/api/ollama/log?n=500");
    const data = await r.json();
    if (!data.path) {
      els.logsPath.textContent = "No log file found.";
      els.logsBody.textContent = "If Ollama was started in a terminal, its logs appear in that terminal window, not here. To see Ollama logs in the app, stop that Ollama and start it with the Start button above instead.";
      return;
    }
    const age = data.modified != null ? ` (last updated ${fmtAge(data.modified)})` : "";
    els.logsPath.textContent = `Log file: ${data.path}${age}`;
    // A log that hasn't been touched in over an hour while Ollama runs is probably from an earlier session, e.g. when Ollama was started in a terminal instead.
    const stale = data.modified != null && data.modified > 3600 && ollamaWasRunning;
    els.logsBody.textContent = (stale
      ? "NOTE: this log file has not been updated recently. The running Ollama was likely started elsewhere (e.g. a terminal), and its logs are not available here.\n\n"
      : "") + (data.log || "(empty)");
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
// "Download translategemma" banner button: reuse the normal pull flow with the name filled in.
els.getModelBtn.addEventListener("click", () => {
  els.getModelBtn.disabled = true;
  $("pullExpander").open = true;
  els.pullName.value = "translategemma";
  els.pullBtn.disabled = false;
  els.pullBtn.click();
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
  resetCancelPullBtn();
  if (pullPollTimer) clearInterval(pullPollTimer);
  pullPollTimer = setInterval(pollPull, 1000);
});
els.cancelPullBtn.addEventListener("click", async () => {
  els.cancelPullBtn.disabled = true;
  els.cancelPullBtn.textContent = "Cancelling…";
  await fetch("/api/ollama/pull/cancel", { method: "POST" });
});
function resetCancelPullBtn() {
  els.cancelPullBtn.hidden = false;
  els.cancelPullBtn.disabled = false;
  els.cancelPullBtn.textContent = "Cancel";
}
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
      resetCancelPullBtn();
      els.cancelPullBtn.hidden = true;   // nothing left to cancel
      els.getModelBtn.disabled = false;
      if (data.status === "done") {
        els.pullLog.textContent = "Model installed and ready to use.";
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
refreshGlobalGlossaryHint();
// Refresh status every 5s so the pill stays current if Ollama goes up/down.
setInterval(refreshOllamaStatus, 5000);
