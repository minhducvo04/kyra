window.addEventListener("error", (event) => {
  reportScriptError(event.message || event.error, `${event.filename || "unknown file"}:${event.lineno || "?"}:${event.colno || "?"}`);
});
window.addEventListener("unhandledrejection", (event) => {
  reportScriptError(event.reason, "Unhandled promise rejection");
});

function scriptAssetStamp() {
  const script = document.querySelector('script[src*="/static/app.js"]');
  return script ? new URL(script.getAttribute("src"), location.href).searchParams.get("v") || "unversioned" : "unknown";
}
function reportScriptError(error, where) {
  // Independent of app state: this also runs when initialization stopped early.
  try {
    let banner = document.getElementById("script-error");
    if (!banner) {
      banner = document.createElement("div");
      banner.id = "script-error";
      banner.setAttribute("role", "alert");
      const title = document.createElement("strong");
      title.textContent = "Something went wrong in Kyra";
      const details = document.createElement("pre");
      details.textContent = `Loaded app.js: ${scriptAssetStamp()}\n`;
      const copy = document.createElement("button");
      copy.type = "button";
      copy.textContent = "Copy";
      copy.onclick = async () => {
        try {
          await navigator.clipboard.writeText(details.textContent);
          copy.textContent = "Copied";
        } catch {
          try { copy.textContent = "Copy unavailable: select the text"; } catch { /* No recursive errors. */ }
        }
      };
      const reload = document.createElement("button");
      reload.type = "button";
      reload.textContent = "Reload";
      reload.onclick = () => {
        try { location.reload(); } catch (error) { reportScriptError(error, "Reload"); }
      };
      banner.append(title, details, copy, reload);
      document.body.prepend(banner);
    }
    const details = banner.querySelector("pre");
    const message = error && error.message ? error.message : String(error);
    const source = error && error.stack ? `\n${error.stack}` : "";
    // Bound repeated failures; preserve the version line and most recent details.
    const previous = details.textContent.split("\n").slice(1).join("\n");
    details.textContent = `Loaded app.js: ${scriptAssetStamp()}\n`
      + `${previous}\n${where || "Unknown location"}: ${message}${source}`.slice(-12000);
  } catch { /* Reporting must never prevent another handler or create an error loop. */ }
}
try {
  document.getElementById("app-js-stamp").textContent = scriptAssetStamp();
} catch { /* The diagnostic label is optional if the document is incomplete. */ }

function openPanelSafely(name, opener) {
  try {
    const result = opener();
    if (result && typeof result.catch === "function") result.catch(error => reportScriptError(error, `Panel: ${name}`));
    return result;
  } catch (error) { reportScriptError(error, `Panel: ${name}`); }
}

// Existing panel routes remain available in the shared workspace shell.
const PANEL_OPENERS = {
  ideas: () => openPanelSafely("ideas", openIdeasPanel),
  devices: () => openPanelSafely("devices", openDevicesPanel),
  room: () => openPanelSafely("room", openRoomPanel),
  focus: () => openPanelSafely("focus", openFocusPanel),
  daily: () => openPanelSafely("daily", openDailyPanel),
  learning: () => openPanelSafely("learning", openLearningPanel),
  jobs: () => openPanelSafely("jobs", openJobsPanel),
  search: () => openPanelSafely("search", openSearchPanel),
  progress: () => openPanelSafely("progress", openProgressPanel),
  attention: () => openPanelSafely("attention", openAttentionPanel),
  myself: () => openPanelSafely("myself", openMyselfPanel),
  map: () => openPanelSafely("map", openMapPanel),
  console: () => openPanelSafely("console", openConsolePanel),
  tools: () => openPanelSafely("tools", openToolsPanel),
};
let panelPoll = null;
let presence = "idle";
let audioCtx = null;

// Panel outcomes remain visible after removal of the legacy chat transcript.
function addLine(who, text, meta, rest = "") {
  const notices = document.getElementById("workspace-notices");
  const line = document.createElement("p");
  line.textContent = [meta, text, rest].filter(Boolean).join(" ");
  if (who === "error") line.setAttribute("role", "alert");
  notices.hidden = false;
  notices.append(line);
  while (notices.children.length > 5) notices.firstElementChild.remove();
  return line;
}
function setPresence(state, message) {
  presence = state;
  if (message) addLine("system", message);
}
function earcon(kind) {
  if (!audioCtx || audioCtx.state !== "running") return;
  // Synthesised, not a file: nothing to load, and quiet enough to be a cue rather than a noise.
  // Skipped entirely when the user asked the OS for less motion - that preference is the
  // closest thing the browser has to "keep the interface calm".
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const notes = { listening: [[660, 0], [880, 0.09]], speaking: [[523, 0]], failed: [[220, 0], [180, 0.12]] }[kind];
    if (!notes) return;
    for (const [freq, at] of notes) {
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, audioCtx.currentTime + at);
      gain.gain.exponentialRampToValueAtTime(0.06, audioCtx.currentTime + at + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + at + 0.11);
      osc.connect(gain).connect(audioCtx.destination);
      osc.start(audioCtx.currentTime + at);
      osc.stop(audioCtx.currentTime + at + 0.12);
    }
  } catch (_) {
    // no audio context (autoplay policy, headless) - the visual state is enough
  }
}

/* ---------------- jobs panel ---------------- */

const jobsToggle = document.getElementById("jobs-toggle");
const jobsPanel = document.getElementById("jobs-panel");
const jobsClose = document.getElementById("jobs-close");

function openJobsPanel() {
  panelOpened("jobs");
  closeRoomPanel();
  jobsPanel.classList.add("is-open");
  jobsPanel.setAttribute("aria-hidden", "false");
  jobsToggle.classList.add("is-active");
  loadDraftDocPickers().catch(() => {});
}
function closeJobsPanel() {
  panelClosed("jobs");
  jobsPanel.classList.remove("is-open");
  jobsPanel.setAttribute("aria-hidden", "true");
  jobsToggle.classList.remove("is-active");
}
jobsToggle.addEventListener("click", () => {
  jobsPanel.classList.contains("is-open") ? closeJobsPanel() : PANEL_OPENERS.jobs();
});
jobsClose.addEventListener("click", closeJobsPanel);

document.querySelectorAll("#jobs-panel .jobs-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll("#jobs-panel .jobs-tab").forEach((t) => t.classList.toggle("is-active", t === tab));
    document.querySelectorAll("#jobs-panel .jobs-tab-panel").forEach((p) => {
      p.classList.toggle("is-active", p.dataset.tabPanel === tab.dataset.tab);
    });
    if (tab.dataset.tab === "tracker") loadTrackerList();
    if (tab.dataset.tab === "scout") loadScout();
    if (tab.dataset.tab === "ready") loadReady();
    if (tab.dataset.tab === "outreach") loadOutreachList();
    if (tab.dataset.tab === "profile") { loadProfile(); loadDocumentList(); }
    if (tab.dataset.tab === "draft") loadDraftDocPickers();
  });
});

/* Every API error is {error: {code, message, details}} with a real status
   code (v2). readJson turns that into a thrown Error carrying the server's
   message, so each fetch site shows the reason instead of "server returned 400". */
async function readJson(res) {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = data && data.error;
    const msg = err ? (err.details && err.details.missing_fields ? `${err.message}: ${err.details.missing_fields.join(", ")}` : err.message) : `server returned ${res.status}`;
    const e = new Error(msg); e.code = err && err.code; e.details = err && err.details; throw e;
  }
  return data;
}


/* Submit the DRAFT form as a background job and stream its progress.
   Contract (companion/jobs.py + webapp.py): POST /api/jobs/draft -> {id};
   GET /api/jobs/{id}/events is SSE with events "progress" (text line),
   "done" (JSON result, same shape as POST /api/job/draft) and "error". */
function runDraftJob(form) {
  const live = document.getElementById("draft-live");
  live.innerHTML = "";
  live.hidden = false;
  // A previous run's result must not sit under the live list while this one runs.
  draftText.textContent = "";
  document.getElementById("draft-pdf-link").hidden = true;
  document.getElementById("draft-fit-status").hidden = true;
  document.getElementById("draft-notes-wrap").hidden = true;
  document.getElementById("draft-questions-wrap").hidden = true;
  draftOutput.hidden = false;
  return new Promise(async (resolve, reject) => {
    let job;
    try {
      job = await readJson(await fetch("/api/jobs/draft", { method: "POST", body: form }));
    } catch (err) { live.hidden = true; return reject(err); }
    const es = new EventSource(`/api/jobs/${job.id}/events`);
    es.addEventListener("progress", (e) => {
      const li = document.createElement("li"); li.textContent = e.data; live.appendChild(li);
    });
    es.addEventListener("done", (e) => { es.close(); resolve(JSON.parse(e.data)); });
    es.addEventListener("error", (e) => {
      es.close();
      reject(new Error(e.data || "job failed"));
    });
    es.onerror = () => { es.close(); reject(new Error("lost connection to the job stream")); };
  });
}

/* -- draft -- */

const draftMaterialType = document.getElementById("draft-material-type");
const draftJobContext = document.getElementById("draft-job-context");
const draftBackground = document.getElementById("draft-background");
const draftResume = document.getElementById("draft-resume");
const draftStyle = document.getElementById("draft-style");
const draftGenerateBtn = document.getElementById("draft-generate");
const draftWarnings = document.getElementById("draft-warnings");
const draftOutput = document.getElementById("draft-output");
const draftText = document.getElementById("draft-text");
const draftCopy = document.getElementById("draft-copy");

/* Renders the one-page verdict returned by the resume endpoints. `fit`
   is null for non-resume material (nothing to say), true/false otherwise. */
function renderFitStatus(el, data) {
  if (data.fit === null || data.fit === undefined) { el.hidden = true; return; }
  el.classList.remove("is-ok", "is-bad");
  if (data.fit) {
    el.classList.add("is-ok");
    el.textContent = "✓ Fits one page";
  } else {
    el.classList.add("is-bad");
    const over = data.overflow_lines ? ` — ${data.overflow_lines} line(s) past page 1` : "";
    el.textContent = `✗ Not one page: compiled to ${data.page_count ?? "?"} pages${over}`;
  }
  el.hidden = false;
}

function renderNotes(wrapEl, listEl, notes) {
  listEl.innerHTML = "";
  if (!notes || !notes.length) { wrapEl.hidden = true; return; }
  notes.forEach((n) => { const li = document.createElement("li"); li.textContent = n; listEl.appendChild(li); });
  wrapEl.hidden = false;
}

draftGenerateBtn.addEventListener("click", async () => {
  draftGenerateBtn.disabled = true;
  draftGenerateBtn.textContent = "Drafting…";
  draftWarnings.hidden = true;
  draftOutput.hidden = true;
  try {
    const form = new FormData();
    form.append("material_type", draftMaterialType.value);
    form.append("job_context", draftJobContext.value);
    form.append("background_text", draftBackground.value);
    const checkedDocIds = Array.from(document.querySelectorAll("#draft-background-docs input[type=checkbox]:checked")).map((c) => c.value);
    form.append("background_document_ids", checkedDocIds.join(","));
    const styleDocSelect = document.getElementById("draft-style-doc");
    form.append("style_document_id", styleDocSelect.value);
    form.append("include_github", document.getElementById("draft-include-github").checked);
    if (draftResume.files[0]) form.append("resume", draftResume.files[0]);
    if (draftStyle.files[0]) form.append("style_sample", draftStyle.files[0]);

    let data;
    if (draftMaterialType.value === "latex_resume") {
      // v2: the one-page loop runs as a background job; progress streams in over SSE.
      data = await runDraftJob(form);
    } else {
      const res = await fetch("/api/job/draft", { method: "POST", body: form });
      data = await readJson(res);
    }

    if (data.warnings && data.warnings.length) {
      draftWarnings.textContent = data.warnings.join(" · ");
      draftWarnings.hidden = false;
    }
    draftText.textContent = data.draft;
    renderFitStatus(document.getElementById("draft-fit-status"), data);
    renderNotes(document.getElementById("draft-notes-wrap"), document.getElementById("draft-notes"), data.notes);
    renderNotes(document.getElementById("draft-questions-wrap"), document.getElementById("draft-questions"), data.questions);
    const pdfLink = document.getElementById("draft-pdf-link");
    if (data.pdf_url) {
      pdfLink.href = data.pdf_url;
      pdfLink.hidden = false;
    } else {
      pdfLink.hidden = true;
    }
    draftOutput.hidden = false;
  } catch (err) {
    draftWarnings.textContent = `draft failed — ${err.message}`;
    draftWarnings.hidden = false;
  } finally {
    draftGenerateBtn.disabled = false;
    draftGenerateBtn.textContent = "Generate draft";
  }
});

draftCopy.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(draftText.textContent);
    draftCopy.textContent = "copied!";
  } catch {
    draftCopy.textContent = "copy failed";
  } finally {
    setTimeout(() => (draftCopy.textContent = "copy"), 1500);
  }
});

/* -- resume "Detailed" mode: analyze fit -> pick & choose -> generate --
   Same job-context/background/document fields above feed both this and
   the normal Fast draft flow - only the material_type dropdown decides
   which UI block is visible and which endpoint(s) get hit. */

const fitPanel = document.getElementById("fit-panel");
const fitAnalyzeBtn = document.getElementById("fit-analyze");
const fitWarnings = document.getElementById("fit-warnings");
const fitChecklist = document.getElementById("fit-checklist");
const fitGenerateBtn = document.getElementById("fit-generate");
const fitCutSuggestions = document.getElementById("fit-cut-suggestions");
const fitCutList = document.getElementById("fit-cut-list");
const fitOutput = document.getElementById("fit-output");
const fitText = document.getElementById("fit-text");
const fitCopy = document.getElementById("fit-copy");

let currentFitBlocks = [];

function isResumeDetailed() {
  return draftMaterialType.value === "resume_detailed";
}

function updateDraftModeVisibility() {
  const detailed = isResumeDetailed();
  fitPanel.hidden = !detailed;
  draftGenerateBtn.hidden = detailed;
  if (detailed) {
    draftWarnings.hidden = true;
    draftOutput.hidden = true;
  } else {
    fitWarnings.hidden = true;
    fitChecklist.hidden = true;
    fitGenerateBtn.hidden = true;
    fitCutSuggestions.hidden = true;
    fitOutput.hidden = true;
  }
}
draftMaterialType.addEventListener("change", updateDraftModeVisibility);
updateDraftModeVisibility();

function collectDraftBackgroundFields() {
  const checkedDocIds = Array.from(document.querySelectorAll("#draft-background-docs input[type=checkbox]:checked")).map((c) => c.value);
  return {
    job_context: draftJobContext.value,
    background_text: draftBackground.value,
    background_document_ids: checkedDocIds.join(","),
  };
}

function priorityClass(priority) {
  const p = (priority || "").toLowerCase();
  return p === "high" ? "priority-high" : p === "medium" ? "priority-medium" : "priority-low";
}

function renderFitRow(block, isChild) {
  const row = document.createElement("label");
  row.className = "fit-row" + (isChild ? " is-child" : "");

  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = block.recommended_keep;
  checkbox.dataset.blockId = block.id;
  row.appendChild(checkbox);

  const body = document.createElement("div");
  body.className = "fit-row-body";

  const top = document.createElement("div");
  top.className = "fit-row-top";
  const label = document.createElement("span");
  label.className = "fit-row-label";
  label.textContent = block.label;
  top.appendChild(label);
  const badge = document.createElement("span");
  badge.className = "fit-badge " + priorityClass(block.priority);
  badge.textContent = `${block.score}% fit — ${block.priority}`;
  top.appendChild(badge);
  body.appendChild(top);

  if (block.reason) {
    const reason = document.createElement("div");
    reason.className = "fit-row-reason";
    reason.textContent = block.reason;
    body.appendChild(reason);
  }
  row.appendChild(body);
  return row;
}

function renderFitChecklist(sections, blocks) {
  fitChecklist.innerHTML = "";
  currentFitBlocks = blocks;

  // Group by section, then by entry (an "entry"-kind row plus its
  // "bullet" children share one entry label; a "detail" row with no
  // real parent - a coursework/skills line - is its own single-row group.
  const orderedSections = sections && sections.length ? sections : [...new Set(blocks.map((b) => b.section))];
  for (const sectionName of orderedSections) {
    const sectionBlocks = blocks.filter((b) => b.section === sectionName);
    if (sectionBlocks.length === 0) continue;

    const sectionEl = document.createElement("div");
    sectionEl.className = "fit-section";
    const title = document.createElement("div");
    title.className = "fit-section-title";
    title.textContent = sectionName;
    sectionEl.appendChild(title);

    const entryOrder = [];
    const byEntry = new Map();
    for (const b of sectionBlocks) {
      if (!byEntry.has(b.entry)) {
        byEntry.set(b.entry, []);
        entryOrder.push(b.entry);
      }
      byEntry.get(b.entry).push(b);
    }

    for (const entryName of entryOrder) {
      const group = byEntry.get(entryName);
      const groupEl = document.createElement("div");
      groupEl.className = "fit-entry-group";
      const parent = group.find((b) => b.kind === "entry");
      const children = group.filter((b) => b.kind !== "entry").sort((a, b) => b.score - a.score);
      if (parent) groupEl.appendChild(renderFitRow(parent, false));
      for (const child of children) groupEl.appendChild(renderFitRow(child, !!parent));
      sectionEl.appendChild(groupEl);
    }
    fitChecklist.appendChild(sectionEl);
  }
  fitChecklist.hidden = false;
  fitGenerateBtn.hidden = false;
}

fitAnalyzeBtn.addEventListener("click", async () => {
  fitAnalyzeBtn.disabled = true;
  fitAnalyzeBtn.textContent = "Analyzing…";
  fitWarnings.hidden = true;
  fitChecklist.hidden = true;
  fitGenerateBtn.hidden = true;
  fitCutSuggestions.hidden = true;
  fitOutput.hidden = true;
  try {
    const body = collectDraftBackgroundFields();
    const res = await fetch("/api/job/resume-fit/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    const data = await res.json();
    if (data.warnings && data.warnings.length) {
      fitWarnings.textContent = data.warnings.join(" · ");
      fitWarnings.hidden = false;
    }
    if (data.blocks && data.blocks.length) {
      renderFitChecklist(data.sections, data.blocks);
    }
  } catch (err) {
    fitWarnings.textContent = `analysis failed — ${err.message}`;
    fitWarnings.hidden = false;
  } finally {
    fitAnalyzeBtn.disabled = false;
    fitAnalyzeBtn.textContent = "Analyze fit";
  }
});

fitGenerateBtn.addEventListener("click", async () => {
  fitGenerateBtn.disabled = true;
  fitGenerateBtn.textContent = "Generating…";
  fitWarnings.hidden = true;
  fitCutSuggestions.hidden = true;
  fitOutput.hidden = true;
  try {
    const selections = Array.from(fitChecklist.querySelectorAll("input[type=checkbox]")).map((c) => ({
      id: c.dataset.blockId,
      keep: c.checked,
    }));
    const body = { ...collectDraftBackgroundFields(), blocks: currentFitBlocks, selections };
    const res = await fetch("/api/job/resume-fit/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    const data = await res.json();
    if (data.warnings && data.warnings.length) {
      fitWarnings.textContent = data.warnings.join(" · ");
      fitWarnings.hidden = false;
    }
    if (data.cut_suggestions && data.cut_suggestions.length) {
      fitCutList.innerHTML = "";
      for (const b of data.cut_suggestions) {
        const li = document.createElement("li");
        li.textContent = `${b.label} (${b.score}% fit)`;
        fitCutList.appendChild(li);
      }
      fitCutSuggestions.hidden = false;
    }
    fitText.textContent = data.draft || "";
    renderFitStatus(document.getElementById("fit-fit-status"), data);
    const pdfLink = document.getElementById("fit-pdf-link");
    if (data.pdf_url) {
      pdfLink.href = data.pdf_url;
      pdfLink.hidden = false;
    } else {
      pdfLink.hidden = true;
    }
    fitOutput.hidden = false;
  } catch (err) {
    fitWarnings.textContent = `generate failed — ${err.message}`;
    fitWarnings.hidden = false;
  } finally {
    fitGenerateBtn.disabled = false;
    fitGenerateBtn.textContent = "Generate resume from selection";
  }
});

fitCopy.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(fitText.textContent);
    fitCopy.textContent = "copied!";
  } catch {
    fitCopy.textContent = "copy failed";
  } finally {
    setTimeout(() => (fitCopy.textContent = "copy"), 1500);
  }
});

/* -- apply (mass apply) -- */

const applyUrls = document.getElementById("apply-urls");
const applyCoverLetter = document.getElementById("apply-cover-letter");
const applySourceUrl = document.getElementById("apply-source-url");
const applyRetailor = document.getElementById("apply-retailor");
const applyRunBtn = document.getElementById("apply-run");
const applyJobs = document.getElementById("apply-jobs");

/* One card per queued job: live progress lines, then the verdict. The worker runs the
   jobs one at a time, so cards fill in from the top. */
function applyCard(job) {
  const card = document.createElement("div");
  card.className = "apply-card";
  const head = document.createElement("div");
  head.className = "apply-card-head";
  const url = document.createElement("a");
  url.href = job.url; url.target = "_blank"; url.rel = "noopener"; url.textContent = job.url;
  const status = document.createElement("span");
  status.className = "apply-status"; status.textContent = "queued";
  head.appendChild(url); head.appendChild(status);
  const live = document.createElement("ul");
  live.className = "draft-live";
  const result = document.createElement("div");
  result.className = "apply-result"; result.hidden = true;
  card.appendChild(head); card.appendChild(live); card.appendChild(result);
  applyJobs.appendChild(card);

  const es = new EventSource(`/api/jobs/${job.id}/events`);
  // The server's terminal `event: error` shares its name with EventSource's own connection error,
  // so the native handler must not overwrite a verdict that already landed.
  let finished = false;
  es.addEventListener("progress", (e) => {
    status.textContent = "running";
    const li = document.createElement("li"); li.textContent = e.data; live.appendChild(li);
  });
  es.addEventListener("done", (e) => {
    finished = true; es.close();
    const r = JSON.parse(e.data);
    status.textContent = r.status.replace("_", " ");
    status.classList.add(r.status === "ready_to_submit" ? "is-ok" : "is-bad");
    result.innerHTML = "";
    const who = document.createElement("div");
    who.className = "apply-who";
    who.textContent = `${r.company} — ${r.role} (tracker #${r.application_id})`;
    result.appendChild(who);
    if (r.resume_pdf_path) {
      const p = document.createElement("div");
      p.textContent = `resume: ${r.resume_pdf_path.split("/").pop()} — ${r.resume_fit ? "one page" : `${r.page_count} pages`}${r.change_summary ? `; ${r.change_summary}` : ""}`;
      result.appendChild(p);
    }
    if (r.cover_letter_path) {
      const p = document.createElement("div");
      p.textContent = `cover letter: ${r.cover_letter_path.split("/").pop()} (paste it in yourself)`;
      result.appendChild(p);
    }
    if (r.autofill_summary_path) {
      const p = document.createElement("div");
      p.textContent = `form: ${r.autofill_filled} field(s) filled, ${r.autofill_skipped.length} left for you${r.autofill_skipped.length ? `: ${r.autofill_skipped.join(", ")}` : ""}`;
      result.appendChild(p);
    }
    if (r.attention.length) {
      const h = document.createElement("div"); h.className = "apply-attention-head"; h.textContent = "Needs you:";
      const ul = document.createElement("ul");
      r.attention.forEach((a) => { const li = document.createElement("li"); li.textContent = a; ul.appendChild(li); });
      result.appendChild(h); result.appendChild(ul);
    }
    if (r.questions && r.questions.length) {
      const h = document.createElement("div"); h.className = "apply-attention-head"; h.textContent = "Bullets that would be stronger with a real number:";
      const ul = document.createElement("ul");
      r.questions.forEach((q) => { const li = document.createElement("li"); li.textContent = q; ul.appendChild(li); });
      result.appendChild(h); result.appendChild(ul);
    }
    for (const warning of r.warnings || []) {
      const p = document.createElement("p");
      p.className = "jobs-warnings";
      p.textContent = warning;
      result.appendChild(p);
    }
    result.hidden = false;
    loadTrackerList();
  });
  es.addEventListener("error", (e) => {
    if (finished || !e.data) return;
    finished = true; es.close();
    status.textContent = "failed"; status.classList.add("is-bad");
    result.textContent = e.data; result.hidden = false;
  });
  es.onerror = () => { if (finished) return; es.close(); status.textContent = "lost stream"; status.classList.add("is-bad"); };
}

applyRunBtn.addEventListener("click", async () => {
  const urls = applyUrls.value.split("\n").map((u) => u.trim()).filter(Boolean);
  if (!urls.length) return;
  applyRunBtn.disabled = true;
  applyRunBtn.textContent = "Queueing…";
  try {
    const data = await readJson(await fetch("/api/jobs/apply", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        urls, cover_letter: applyCoverLetter.value, source_url: applySourceUrl.value.trim(),
        retailor: applyRetailor.checked,
      }),
    }));
    data.jobs.forEach(applyCard);
    applyUrls.value = "";
    applySourceUrl.value = "";
  } catch (err) {
    const p = document.createElement("p"); p.className = "jobs-warnings"; p.textContent = `couldn't queue — ${err.message}`;
    applyJobs.prepend(p);
  } finally {
    applyRunBtn.disabled = false;
    applyRunBtn.textContent = "Apply to all";
  }
});

/* -- prepared documents and live form review -- */
const readyList = document.getElementById("ready-list");
const readyStatus = document.getElementById("ready-status");
let readyItems = [], readySelected = 0, readyBusy = false;

function readyVisible() {
  return jobsPanel.classList.contains("is-open") &&
    !!document.querySelector('#jobs-panel [data-tab="ready"].is-active');
}
function selectReady(index) {
  readySelected = Math.max(0, Math.min(index, readyItems.length - 1));
  readyList.querySelectorAll(".ready-card").forEach((card, i) => {
    card.classList.toggle("is-selected", i === readySelected);
    card.setAttribute("aria-current", i === readySelected ? "true" : "false");
    if (i === readySelected) card.scrollIntoView({ block: "nearest" });
  });
}
async function loadReady(message = "") {
  try {
    const data = await readJson(await fetch("/api/ready"));
    readyItems = data.items.filter(item => !["applied", "skipped"].includes(item.state));
    readyStatus.textContent = message || `${data.counts.prepared} prepared · ${data.counts.ready} ready · ${data.counts.needs_input} need input`;
    readyList.replaceChildren();
    if (!readyItems.length) readyList.textContent = "No applications awaiting review.";
    for (const [index, item] of readyItems.entries()) {
      const card = document.createElement("article");
      card.className = "ready-card";
      card.tabIndex = 0;
      card.addEventListener("focus", () => selectReady(index));
      card.addEventListener("focusin", () => selectReady(index));
      const title = document.createElement("h3");
      title.textContent = `${item.company} · ${item.role}`;
      const info = document.createElement("p");
      info.textContent = `${item.state.replaceAll("_", " ")}${item.deep ? " · DEEP: deliberate review in APPLY" : ""} · ${item.fit.verdict || "unknown"} · ${item.fit.level || "unlevelled"} · years asked: ${item.fit.years_min ?? "unknown"}`;
      const matched = document.createElement("p");
      matched.textContent = `Matched: ${(item.fit.matched || []).join(", ") || "none"}`;
      const age = document.createElement("p");
      age.textContent = `${item.freshness.state} · ${item.freshness.basis} · ${item.freshness.days == null ? "age unknown" : item.freshness.days.toFixed(1) + " days"}`;
      const reasons = document.createElement("p");
      reasons.textContent = (item.fit.reasons || []).join("; ");
      card.append(title, info, matched, age, reasons);
      try {
        const url = new URL(item.url);
        if (["https:", "http:"].includes(url.protocol) && !url.username && !url.password) {
          const posting = document.createElement("a"); posting.textContent = "Review posting";
          posting.href = url.href; posting.target = "_blank"; posting.rel = "noopener noreferrer";
          card.append(posting);
        }
      } catch (_) { /* Keep malformed stored URLs inert. */ }
      for (const [kind, label, path] of [["resume", "Review resume", item.resume_path], ["letter", "Review letter (paste or attach manually)", item.cover_letter_path]]) {
        if (!path) continue;
        const link = document.createElement("a");
        link.href = `/api/ready/${encodeURIComponent(item.id)}/document/${kind}`;
        link.textContent = label;
        link.target = "_blank"; link.rel = "noopener noreferrer";
        card.append(link);
      }
      for (const question of item.questions || []) {
        const line = document.createElement("p"); line.textContent = question; card.append(line);
      }
      if (item.window_live) {
        const line = document.createElement("p");
        const count = (item.questions || []).reduce((n, q) => n + Number(q.match(/^(\d+) custom question field/i)?.[1] || 1), 0);
        line.textContent = count ? `window open, answer ${count} questions` : "window open, review and submit";
        card.append(line);
      }
      const actions = document.createElement("div"); actions.className = "ready-actions";
      const star = document.createElement("button"); star.type = "button"; star.className = "jobs-btn";
      star.textContent = item.starred ? "★ Starred" : "☆ Star";
      star.setAttribute("aria-pressed", item.starred ? "true" : "false"); star.disabled = readyBusy;
      star.title = "Star this company for 15-minute checks and deep review on the next check.";
      star.addEventListener("click", () => { readySelected = index; readyAction("star"); });
      actions.append(star);
      for (const [action, label] of [["open", "Open"], ["applied", "I submitted"], ["skip", "Skip"]]) {
        if (action === "open" && item.window_live) continue;
        if (action === "open" && item.resume_path == null && !item.deep) {
          const preparing = document.createElement("span"); preparing.textContent = "Documents preparing";
          actions.append(preparing); continue;
        }
        const button = document.createElement("button"); button.type = "button"; button.className = "jobs-btn";
        button.textContent = label; button.disabled = readyBusy || (action === "open" && item.deep);
        button.addEventListener("click", () => { readySelected = index; readyAction(action); });
        actions.append(button);
      }
      card.append(actions); readyList.append(card);
    }
    selectReady(readySelected);
  } catch (err) { readyStatus.textContent = `Could not load review queue: ${err.message}`; }
}
async function readyAction(action) {
  const item = readyItems[readySelected];
  if (!item || readyBusy || (action === "open" && (item.deep || item.resume_path == null || item.window_live))) return;
  readyBusy = true;
  readyList.querySelectorAll("button").forEach(button => { button.disabled = true; });
  try {
    const url = action === "star" ? "/api/ready/star" : `/api/ready/${encodeURIComponent(item.id)}/${action}`;
    const options = action === "star" ? { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ company: item.company, starred: !item.starred }) } : { method: "POST" };
    const result = await readJson(await fetch(url, options));
    readyBusy = false;
    await loadReady(action === "star" ? "Company star saved." : action === "applied" ?
      "Marked applied; follow-up scheduled in 7 days." : `State: ${result.state.replaceAll("_", " ")}`);
  } catch (err) {
    readyBusy = false;
    await loadReady(`Could not save: ${err.message}`);
  }
}
document.getElementById("ready-refresh").addEventListener("click", () => { if (!readyBusy) loadReady(); });
window.addEventListener("focus", () => { if (readyVisible() && !readyBusy) loadReady(); });
document.addEventListener("keydown", event => {
  const target = document.activeElement;
  if (!readyVisible() || readyBusy || event.repeat || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey ||
      target?.isContentEditable || target?.closest?.('input, textarea, select, [role="textbox"], [contenteditable]')) return;
  if (event.key === "j" || event.key === "k") {
    event.preventDefault(); selectReady(readySelected + (event.key === "j" ? 1 : -1));
  } else {
    const action = { o: "open", a: "applied", s: "skip" }[event.key];
    if (action) { event.preventDefault(); readyAction(action); }
  }
});

/* -- public hiring leads; approval is separate from applying -- */

const scoutList = document.getElementById("scout-list");
const scoutStatus = document.getElementById("scout-status");
document.getElementById("scout-refresh").addEventListener("click", () => loadScout());

async function loadScout(message = "") {
  scoutStatus.textContent = "Loading candidates…";
  try {
    const data = await readJson(await fetch("/api/scout"));
    scoutList.replaceChildren();
    scoutStatus.textContent = message || `${data.counts.proposed} proposed · ${data.counts.approved} approved · ${data.counts.rejected} rejected`;
    if (!data.candidates.length) {
      scoutList.textContent = "No candidates yet. Leads appear after a scout run.";
    }
    for (const candidate of data.candidates) {
      const card = document.createElement("article");
      card.className = "scout-card";
      const title = document.createElement("h3");
      title.textContent = candidate.company;
      const detail = document.createElement("p");
      detail.textContent = `${candidate.state} · ${candidate.source} · ${candidate.location || "Location unknown"}`;
      const why = document.createElement("p");
      why.textContent = candidate.why;
      card.append(title, detail, why);
      for (const [label, value] of [["Board", candidate.board_url], ["Website", candidate.website]]) {
        if (!value) continue;
        try {
          const url = new URL(value);
          if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) continue;
          const link = document.createElement("a");
          link.textContent = `${label}: ${url.hostname}`;
          link.href = url.href;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          card.append(link);
        } catch (_) { /* An invalid saved URL is never made clickable. */ }
      }
      if (!candidate.board_url) {
        const note = document.createElement("p");
        note.textContent = "No board yet. Approval saves the company without starting a watch.";
        card.append(note);
      }
      if (candidate.state === "proposed") {
        const actions = document.createElement("div");
        actions.className = "scout-actions";
        for (const [action, label] of [["approve", "Approve"], ["reject", "Reject"]]) {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "jobs-btn";
          button.textContent = label;
          button.addEventListener("click", async () => {
            const buttons = actions.querySelectorAll("button");
            buttons.forEach(b => { b.disabled = true; });
            try {
              const result = await readJson(await fetch(`/api/scout/${encodeURIComponent(candidate.key)}/${action}`, { method: "POST" }));
              await loadScout(result.note || "Saved");
            } catch (err) {
              scoutStatus.textContent = `Could not save: ${err.message}`;
              buttons.forEach(b => { b.disabled = false; });
            }
          });
          actions.append(button);
        }
        card.append(actions);
      }
      scoutList.append(card);
    }
  } catch (err) {
    scoutStatus.textContent = `Could not load candidates: ${err.message}`;
  }
}

/* -- tracker -- */

const trackerCompany = document.getElementById("tracker-company");
const trackerRole = document.getElementById("tracker-role");
const trackerLink = document.getElementById("tracker-link");
const trackerStatus = document.getElementById("tracker-status");
const trackerAddBtn = document.getElementById("tracker-add");
const trackerList = document.getElementById("tracker-list");

const STATUSES = ["targeting", "ready_to_submit", "needs_attention", "prepared", "applied", "referral_pending", "interviewing", "offer", "rejected", "withdrawn"];
for (const status of STATUSES) {
  const option = document.createElement("option");
  option.value = status;
  option.textContent = status.replaceAll("_", " ");
  option.selected = status === "applied";
  trackerStatus.appendChild(option);
}

async function loadTrackerList() {
  trackerList.textContent = "loading…";
  try {
    const res = await fetch("/api/job/applications");
    const data = await readJson(res);
    renderTrackerList(data.applications || []);
  } catch (err) {
    trackerList.textContent = `couldn't load — ${err.message}`;
  }
}

function renderTrackerList(apps) {
  trackerList.textContent = "";
  let draggedApp = null;
  let updating = false;
  async function updateStatus(app, status) {
    if (updating || status === app.status) return;
    updating = true;
    try {
      await readJson(await fetch("/api/job/applications/status", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: app.id, status }),
      }));
      await loadTrackerList();
    } catch (err) {
      addLine("error", `status update failed — ${err.message}`);
    } finally {
      updating = false;
    }
  }
  const columns = new Map();
  for (const status of STATUSES) {
    const column = document.createElement("section");
    column.dataset.trackerColumn = status;
    const heading = document.createElement("h3");
    heading.textContent = status.replaceAll("_", " ");
    column.appendChild(heading);
    column.addEventListener("dragover", (event) => {
      if (draggedApp && !updating) {
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
      }
    });
    column.addEventListener("drop", async (event) => {
      if (!draggedApp) return;
      event.preventDefault();
      const app = draggedApp;
      draggedApp = null;
      await updateStatus(app, status);
    });
    columns.set(status, column);
    trackerList.appendChild(column);
  }
  for (const app of apps) {
    const item = document.createElement("div");
    item.className = "jobs-tracker-item";
    item.draggable = true;
    item.dataset.appId = app.id;
    item.addEventListener("dragstart", (event) => {
      draggedApp = app;
      event.dataTransfer.setData("text/plain", app.id);
      event.dataTransfer.effectAllowed = "move";
    });
    item.addEventListener("dragend", () => { draggedApp = null; });

    const top = document.createElement("div");
    top.className = "jobs-tracker-item-top";
    const left = document.createElement("div");
    const company = document.createElement("div");
    company.className = "jobs-tracker-item-company";
    company.textContent = app.company;
    const role = document.createElement("div");
    role.className = "jobs-tracker-item-role";
    role.textContent = app.role;
    left.append(company, role);
    if (app.apply_by_at && ["targeting", "prepared", "ready_to_submit", "needs_attention"].includes(app.status)) {
      const deadline = document.createElement("div");
      deadline.className = "small dim";
      const at = new Date(app.apply_by_at);
      const hours = Math.ceil((at.getTime() - Date.now()) / 3600000);
      deadline.textContent = `Apply by ${at.toLocaleString()} · ${hours > 0 ? `${hours}h left` : "due now"} (referral window)`;
      left.appendChild(deadline);
    }
    if (app.outreach_plan) {
      const draft = document.createElement("details");
      const heading = document.createElement("summary");
      heading.textContent = "Outreach draft: humanizer review required before use";
      const text = document.createElement("p");
      text.textContent = `${app.outreach_plan.recipient}: ${app.outreach_plan.note}`;
      draft.append(heading, text);
      left.appendChild(draft);
    }

    const select = document.createElement("select");
    select.className = "jobs-tracker-status";
    select.setAttribute("aria-label", `Status for ${app.company}, ${app.role}`);
    for (const s of STATUSES) {
      const opt = document.createElement("option");
      opt.value = s;
      opt.textContent = s;
      opt.selected = s === app.status;
      select.appendChild(opt);
    }
    select.addEventListener("change", async () => {
      select.disabled = true;
      await updateStatus(app, select.value);
      select.value = app.status;
      select.disabled = false;
    });

    top.append(left, select);
    item.appendChild(top);

    const age = document.createElement("p");
    const since = Date.parse(app.status_since);
    const days = Math.max(0, Math.floor((Date.now() - since) / 86400000));
    age.textContent = Number.isFinite(days) ? `${days} days in ${app.status.replaceAll("_", " ")}` : "Status date unavailable";
    item.appendChild(age);
    if (app.notes) {
      const notes = document.createElement("p");
      notes.textContent = app.notes;
      notes.style.whiteSpace = "pre-wrap";
      item.appendChild(notes);
    }
    const history = document.createElement("details");
    const historyTitle = document.createElement("summary");
    historyTitle.textContent = "Status history";
    const timeline = document.createElement("div");
    history.append(historyTitle, timeline);
    history.addEventListener("toggle", async () => {
      if (!history.open) return;
      timeline.textContent = "Loading history…";
      try {
        const data = await readJson(await fetch(`/api/job/applications/${app.id}/history`));
        timeline.textContent = data.events.length ? "" : "No recorded history yet.";
        for (const event of data.events) {
          const line = document.createElement("p");
          line.textContent = `${new Date(event.at).toLocaleString()}: ${event.status.replaceAll("_", " ")}${event.note ? " · " + event.note : ""}`;
          timeline.appendChild(line);
        }
      } catch (err) { timeline.textContent = `History unavailable: ${err.message}`; }
    });
    item.appendChild(history);

    const follow = document.createElement("button");
    follow.type = "button";
    follow.className = "jobs-btn";
    follow.textContent = "Follow up in 7 days";
    const followNotice = document.createElement("p");
    followNotice.setAttribute("role", "status");
    follow.addEventListener("click", async () => {
      follow.disabled = true;
      try {
        const data = await readJson(await fetch(`/api/job/applications/${app.id}/follow_up`, {
          method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({days: 7}),
        }));
        followNotice.textContent = `Reminder set for ${new Date(data.reminder.due_at).toLocaleDateString()}.`;
      } catch (err) {
        followNotice.textContent = err.message;
        follow.disabled = false;
      }
    });
    item.append(follow, followNotice);

    if (app.link) {
      const link = document.createElement("a");
      link.href = app.link;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = app.link;
      link.style.color = "var(--text-faint)";
      link.style.fontSize = "0.68rem";
      item.appendChild(link);
    }

    columns.get(app.status).appendChild(item);
  }
}

trackerAddBtn.addEventListener("click", async () => {
  const company = trackerCompany.value.trim();
  const role = trackerRole.value.trim();
  if (!company || !role) return;
  trackerAddBtn.disabled = true;
  try {
    await readJson(await fetch("/api/job/applications", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ company, role, link: trackerLink.value.trim() || null, status: trackerStatus.value }),
    }));
    trackerCompany.value = "";
    trackerRole.value = "";
    trackerLink.value = "";
    await loadTrackerList();
  } catch (err) {
    addLine("error", `couldn't add application — ${err.message}`);
  } finally {
    trackerAddBtn.disabled = false;
  }
});

/* -- autofill -- */

const autofillUrl = document.getElementById("autofill-url");
const autofillRunBtn = document.getElementById("autofill-run");
const autofillOutput = document.getElementById("autofill-output");
const autofillResult = document.getElementById("autofill-result");

autofillRunBtn.addEventListener("click", async () => {
  const url = autofillUrl.value.trim();
  if (!url) return;
  autofillRunBtn.disabled = true;
  autofillRunBtn.textContent = "Filling…";
  autofillOutput.hidden = true;
  try {
    const res = await fetch("/api/job/autofill", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    autofillResult.innerHTML = "";
    let data;
    try {
      data = await readJson(res);
    } catch (err) {
      const p = document.createElement("p");
      p.style.color = "var(--danger)";
      p.textContent = err.message; // readJson already folds details.missing_fields into the message
      autofillResult.appendChild(p);
      data = null;
    }
    if (data) {
      const summary = document.createElement("p");
      summary.textContent = `filled ${data.filled.length}, skipped ${data.skipped.length}. Nothing submitted.`;
      autofillResult.appendChild(summary);
      if (data.skipped.length) {
        const list = document.createElement("ul");
        list.style.fontSize = "0.72rem";
        list.style.color = "var(--text-dim)";
        for (const s of data.skipped) {
          const li = document.createElement("li");
          li.textContent = `${s.label} — ${s.reason}`;
          list.appendChild(li);
        }
        autofillResult.appendChild(list);
      }
    }
    autofillOutput.hidden = false;
  } catch (err) {
    autofillResult.textContent = `autofill failed — ${err.message}`;
    autofillOutput.hidden = false;
  } finally {
    autofillRunBtn.disabled = false;
    autofillRunBtn.textContent = "Fill it in";
  }
});

/* -- document library (shared by draft picker + profile tab) -- */

const KIND_LABEL = { resume: "RESUME", style_sample: "STYLE", note: "NOTE" };

async function fetchDocuments() {
  const res = await fetch("/api/job/documents");
  const data = await res.json();
  return data.documents || [];
}

async function loadDraftDocPickers() {
  const picker = document.getElementById("draft-background-docs");
  const styleSelect = document.getElementById("draft-style-doc");
  const docs = await fetchDocuments().catch(() => []);

  picker.innerHTML = "";
  if (docs.length === 0) {
    picker.innerHTML = '<span class="jobs-hint">no saved documents yet — add some in PROFILE</span>';
  } else {
    for (const doc of docs) {
      const row = document.createElement("label");
      row.className = "jobs-doc-picker-item";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = doc.id;
      const badge = document.createElement("span");
      badge.className = "doc-kind-badge";
      badge.textContent = KIND_LABEL[doc.kind] || doc.kind;
      const label = document.createElement("span");
      label.textContent = doc.label;
      row.append(cb, badge, label);
      picker.appendChild(row);
    }
  }

  const prevStyleValue = styleSelect.value;
  styleSelect.innerHTML = '<option value="">(none)</option>';
  for (const doc of docs.filter((d) => d.kind === "style_sample")) {
    const opt = document.createElement("option");
    opt.value = doc.id;
    opt.textContent = doc.label;
    styleSelect.appendChild(opt);
  }
  styleSelect.value = prevStyleValue;
}

/* -- profile -- */

const PROFILE_FIELDS = [
  "first_name", "last_name", "email", "phone", "location", "country", "current_company",
  "linkedin_url", "github_url", "portfolio_url", "twitter_url", "preferred_name", "pronouns", "resume_path",
  "eeo_gender_identity", "eeo_race_ethnicity", "eeo_hispanic_latino", "eeo_veteran_status", "eeo_disability_status",
];

async function loadProfile() {
  try {
    const res = await fetch("/api/profile");
    const data = await res.json();
    for (const f of PROFILE_FIELDS) {
      const el = document.getElementById(`profile-${f}`);
      if (el) el.value = data.profile[f] || "";
    }
    document.getElementById("profile-raw").textContent = JSON.stringify(data.profile, null, 2);
  } catch (err) {
    addLine("error", `couldn't load profile — ${err.message}`);
  }
  await loadResumePicker();
}

async function loadResumePicker() {
  const picker = document.getElementById("profile-resume-picker");
  const currentPath = document.getElementById("profile-resume_path").value;
  try {
    const docs = await fetchDocuments();
    const resumesWithFile = docs.filter((d) => d.kind === "resume" && d.file_path);
    picker.innerHTML = '<option value="">Pick a resume from your document library…</option>';
    for (const doc of resumesWithFile) {
      const opt = document.createElement("option");
      opt.value = doc.file_path;
      opt.textContent = doc.label;
      opt.selected = doc.file_path === currentPath;
      picker.appendChild(opt);
    }
    if (resumesWithFile.length === 0) {
      picker.innerHTML += '<option value="" disabled>(no uploaded resume files yet — upload one below or in DRAFT)</option>';
    }
  } catch {
    /* document library not reachable - the manual text field still works */
  }
}

document.getElementById("profile-resume-picker").addEventListener("change", (e) => {
  if (e.target.value) document.getElementById("profile-resume_path").value = e.target.value;
});

const profileSaveBtn = document.getElementById("profile-save");
const profileWarnings = document.getElementById("profile-warnings");

profileSaveBtn.addEventListener("click", async () => {
  profileSaveBtn.disabled = true;
  profileSaveBtn.textContent = "Saving…";
  profileWarnings.hidden = true;
  try {
    const body = {};
    for (const f of PROFILE_FIELDS) {
      const el = document.getElementById(`profile-${f}`);
      if (el) body[f] = el.value;
    }
    const res = await fetch("/api/profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    document.getElementById("profile-raw").textContent = JSON.stringify(data.profile, null, 2);
    if (data.missing_for_autofill && data.missing_for_autofill.length) {
      profileWarnings.textContent = `still missing for autofill: ${data.missing_for_autofill.join(", ")}`;
      profileWarnings.hidden = false;
    }
  } catch (err) {
    profileWarnings.textContent = `save failed — ${err.message}`;
    profileWarnings.hidden = false;
  } finally {
    profileSaveBtn.disabled = false;
    profileSaveBtn.textContent = "Save profile";
  }
});

/* -- document library management (profile tab) -- */

async function loadDocumentList() {
  const list = document.getElementById("profile-doc-list");
  list.textContent = "loading…";
  try {
    const docs = await fetchDocuments();
    list.innerHTML = "";
    if (docs.length === 0) {
      list.textContent = "no documents yet";
      return;
    }
    for (const doc of docs) {
      const item = document.createElement("div");
      item.className = "jobs-doc-item";
      const left = document.createElement("div");
      const label = document.createElement("div");
      label.className = "jobs-doc-item-label";
      label.textContent = `${doc.label} `;
      const badge = document.createElement("span");
      badge.className = "doc-kind-badge";
      badge.textContent = KIND_LABEL[doc.kind] || doc.kind;
      label.appendChild(badge);
      const meta = document.createElement("div");
      meta.className = "jobs-doc-item-meta";
      meta.textContent = `added ${doc.added_at.slice(0, 10)} · ${doc.text.length} chars`;
      left.append(label, meta);

      const delBtn = document.createElement("button");
      delBtn.className = "jobs-doc-delete";
      delBtn.type = "button";
      delBtn.textContent = "delete";
      delBtn.addEventListener("click", async () => {
        delBtn.disabled = true;
        try {
          await fetch(`/api/job/documents/${doc.id}`, { method: "DELETE" });
          await loadDocumentList();
        } catch (err) {
          addLine("error", `couldn't delete document — ${err.message}`);
          delBtn.disabled = false;
        }
      });

      item.append(left, delBtn);
      list.appendChild(item);
    }
  } catch (err) {
    list.textContent = `couldn't load — ${err.message}`;
  }
}

const docAddBtn = document.getElementById("doc-add");
docAddBtn.addEventListener("click", async () => {
  const label = document.getElementById("doc-label").value.trim();
  const kind = document.getElementById("doc-kind").value;
  const text = document.getElementById("doc-text").value.trim();
  const fileInput = document.getElementById("doc-file");
  if (!text && !fileInput.files[0]) return;

  docAddBtn.disabled = true;
  docAddBtn.textContent = "Adding…";
  try {
    const form = new FormData();
    form.append("label", label);
    form.append("kind", kind);
    form.append("text", text);
    if (fileInput.files[0]) form.append("file", fileInput.files[0]);

    const res = await fetch("/api/job/documents", { method: "POST", body: form });
    await readJson(res); // throws with the server's message on 4xx, handled below
    document.getElementById("doc-label").value = "";
    document.getElementById("doc-text").value = "";
    fileInput.value = "";
    await loadDocumentList();
    await loadResumePicker(); // a newly-uploaded resume file should show up here immediately
  } catch (err) {
    addLine("error", `couldn't add document — ${err.message}`);
  } finally {
    docAddBtn.disabled = false;
    docAddBtn.textContent = "Add to library";
  }
});

/* ---------------- outreach (JOBS panel) ----------------
   The five outreach tools have worked through chat since 2026-09-06 but had no
   UI, the same gap the TOOLS panel closed for reminders and news. Every button
   here hits a dedicated endpoint, which runs the same tool object the chat path
   runs - so drafting still reads the linked application's status, and marking a
   contact "sent" still schedules the follow-up reminder the digest surfaces.
   Kyra never sends: the last step is always Duc pasting it himself. */

const OUTREACH_STATUSES = ["drafted", "sent", "accepted", "replied", "call_done", "referred", "no_reply"];
const outreachList = document.getElementById("outreach-list");
const outreachDueOnly = document.getElementById("outreach-due-only");

async function loadOutreachList() {
  outreachList.textContent = "loading…";
  try {
    const q = outreachDueOnly.checked ? "?due_only=true" : "";
    const data = await readJson(await fetch(`/api/outreach${q}`));
    renderOutreachList(data.contacts || []);
  } catch (err) {
    outreachList.textContent = `couldn't load — ${err.message}`;
  }
}

function outreachField(id, placeholder) {
  const el = document.createElement("input");
  el.type = "text";
  el.placeholder = placeholder;
  el.dataset.field = id;
  return el;
}

function outreachTextBlock(label, text, onCopy) {
  const wrap = document.createElement("div");
  wrap.className = "outreach-note";
  const head = document.createElement("div");
  head.className = "outreach-note-head";
  const tag = document.createElement("span");
  // The 200-character limit is enforced in code, not asked for in the prompt,
  // so showing the count is showing a real constraint rather than trivia.
  tag.textContent = `${label} · ${text.length} chars`;
  const copy = document.createElement("button");
  copy.className = "jobs-btn";
  copy.textContent = "Copy";
  copy.addEventListener("click", onCopy);
  head.append(tag, copy);
  const body = document.createElement("div");
  body.className = "outreach-note-text";
  body.textContent = text;
  wrap.append(head, body);
  return wrap;
}

function renderOutreachList(contacts) {
  outreachList.innerHTML = "";
  if (contacts.length === 0) {
    outreachList.textContent = outreachDueOnly.checked
      ? "no follow-ups are due"
      : "no outreach contacts yet";
    return;
  }
  for (const c of contacts) {
    const item = document.createElement("div");
    item.className = "jobs-tracker-item";

    const top = document.createElement("div");
    top.className = "jobs-tracker-item-top";
    const left = document.createElement("div");
    const who = document.createElement("div");
    who.className = "jobs-tracker-item-company";
    who.textContent = c.name;
    const where = document.createElement("div");
    // Scoped clamp, not on .jobs-tracker-item-role: the tracker's roles are short,
    // but a real outreach `role` often holds a paragraph of context about the
    // person, which buries every other contact in the list. Full text on hover.
    where.className = "jobs-tracker-item-role outreach-where";
    where.textContent = [c.company, c.role].filter(Boolean).join(" · ");
    where.title = where.textContent;
    left.append(who, where);
    if (c.relation) {
      const rel = document.createElement("div");
      rel.className = "jobs-tracker-item-role";
      rel.textContent = c.relation;
      left.appendChild(rel);
    }

    const select = document.createElement("select");
    select.className = "jobs-tracker-status";
    for (const st of OUTREACH_STATUSES) {
      const opt = document.createElement("option");
      opt.value = st;
      opt.textContent = st;
      opt.selected = st === c.status;
      select.appendChild(opt);
    }
    select.addEventListener("change", async () => {
      try {
        const out = await readJson(await fetch(`/api/outreach/${c.id}/status`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: select.value }),
        }));
        // "sent" is the one status with a side effect worth reporting back.
        if (out.reminder_id) addLine("system", `follow-up reminder set for ${c.name}`);
        loadOutreachList();
      } catch (err) {
        addLine("error", `status update failed — ${err.message}`);
        select.value = c.status;
      }
    });
    top.append(left, select);
    item.appendChild(top);

    if (c.profile_url) {
      const link = document.createElement("a");
      link.href = c.profile_url;
      link.target = "_blank";
      link.rel = "noopener";
      link.className = "jobs-tracker-item-link";
      link.textContent = c.profile_url;
      item.appendChild(link);
    }
    if (c.follow_up_at) {
      const due = document.createElement("div");
      due.className = "jobs-tracker-item-role";
      due.textContent = `follow up after ${c.follow_up_at.slice(0, 10)}`;
      item.appendChild(due);
    }

    const details = document.createElement("details");
    details.className = "jobs-details outreach-draft";
    const summary = document.createElement("summary");
    summary.textContent = c.note ? "Draft again" : "Draft the note";
    const context = outreachField("job_context", "The role, a line or two");
    const mutuals = outreachField("mutual_connections", "Mutual connections (people you both know)");
    // The prompt asks for one true, specific thing to build the ask around;
    // without it the note falls back to generic school-and-company framing.
    const angle = outreachField("personal_angle", "One true, specific thing about them");
    const go = document.createElement("button");
    go.className = "jobs-btn";
    go.textContent = "Draft";
    const status = document.createElement("div");
    status.className = "jobs-hint";
    go.addEventListener("click", async () => {
      go.disabled = true;
      status.textContent = "drafting — this is two real Claude calls, give it a moment…";
      try {
        const out = await readJson(await fetch(`/api/outreach/${c.id}/draft`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            job_context: context.value.trim(),
            mutual_connections: mutuals.value.trim(),
            personal_angle: angle.value.trim(),
          }),
        }));
        // A draft that failed a post-condition must not read as a clean one.
        for (const w of out.warnings || []) addLine("error", `outreach draft: ${w}`);
        loadOutreachList();
      } catch (err) {
        status.textContent = `draft failed — ${err.message}`;
      } finally {
        go.disabled = false;
      }
    });
    details.append(summary, context, mutuals, angle, go, status);
    item.appendChild(details);

    const copy = async (which) => {
      try {
        const out = await readJson(await fetch(`/api/outreach/${c.id}/copy`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ which }),
        }));
        addLine("system", out.message);
      } catch (err) {
        addLine("error", `copy failed — ${err.message}`);
      }
    };
    if (c.note) item.appendChild(outreachTextBlock("note", c.note, () => copy("note")));
    if (c.follow_up) item.appendChild(outreachTextBlock("follow-up", c.follow_up, () => copy("follow_up")));

    outreachList.appendChild(item);
  }
}

document.getElementById("outreach-add").addEventListener("click", async () => {
  const value = (id) => document.getElementById(id).value.trim();
  try {
    await readJson(await fetch("/api/outreach", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: value("outreach-name"),
        company: value("outreach-company"),
        role: value("outreach-role") || null,
        profile_url: value("outreach-url") || null,
        relation: value("outreach-relation") || null,
      }),
    }));
    for (const id of ["outreach-name", "outreach-company", "outreach-role", "outreach-url", "outreach-relation"]) {
      document.getElementById(id).value = "";
    }
    loadOutreachList();
  } catch (err) {
    addLine("error", `couldn't add that contact — ${err.message}`);
  }
});

outreachDueOnly.addEventListener("change", loadOutreachList);


/* ---------------- tools panel ---------------- */

const toolsPanel = document.getElementById("tools-panel");
let toolSection = "reminders";
const toolRoutes = { reminders: "reminders", initiatives: "initiatives", news: "news", science: "science", "add-learning": "learning", memory: "memory" };
function openToolsPanel() {
  const route = Object.hasOwn(toolRoutes, location.hash.slice(1)) ? location.hash.slice(1) : "reminders";
  toolSection = toolRoutes[route];
  panelOpened("tools", route);
  toolsPanel.classList.add("is-open");
  toolsPanel.setAttribute("aria-hidden", "false");
  toolsPanel.querySelector(".jobs-panel-header span").textContent = window.KyraNavigation.label(route);
  document.querySelectorAll("[data-tools-tab-panel]").forEach(panel => {
    panel.classList.toggle("is-active", panel.dataset.toolsTabPanel === toolSection);
  });
  const loaders = { reminders: loadReminders, initiatives: loadInitiatives, memory: loadMemoryNotes };
  if (loaders[toolSection]) loaders[toolSection]();
}
function closeToolsPanel() {
  panelClosed("tools");
  toolsPanel.classList.remove("is-open");
  toolsPanel.setAttribute("aria-hidden", "true");
}
document.getElementById("tools-close").addEventListener("click", closeToolsPanel);

/* Suggestions are read from the daily snapshot. Only these explicit buttons act. */
async function loadInitiatives() {
  const list = document.getElementById("initiative-list");
  try {
    const response = await fetch("/api/initiatives");
    if (response.status === 404) {
      list.textContent = "Suggestions need an updated server.";
      return;
    }
    const data = await readJson(response);
    list.replaceChildren();
    if (!data.initiatives.length) list.textContent = "No suggestions waiting for a decision.";
    for (const proposal of data.initiatives) {
      const row = document.createElement("div");
      row.className = "jobs-tracker-item";
      const title = document.createElement("h3");
      title.textContent = proposal.title;
      const step = document.createElement("p");
      step.textContent = proposal.first_step;
      const why = document.createElement("p");
      why.textContent = `${proposal.why} About ${proposal.minutes} minutes.`;
      const evidence = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = "Evidence";
      evidence.append(summary);
      for (const source of proposal.evidence) {
        const quote = document.createElement("p");
        quote.textContent = `${source.source} (${source.when}): ${source.quote}`;
        evidence.append(quote);
      }
      const reason = document.createElement("input");
      reason.type = "text";
      reason.placeholder = "Dismiss reason (optional)";
      reason.setAttribute("aria-label", `Dismiss reason for ${proposal.title}`);
      reason.maxLength = 2000;
      const actions = document.createElement("div");
      actions.className = "jobs-reminder-actions";
      const accept = document.createElement("button");
      accept.type = "button";
      accept.textContent = proposal.status === "accepting" ? "Finish adding reminder" : "Add reminder";
      const dismiss = document.createElement("button");
      dismiss.type = "button";
      dismiss.textContent = "Dismiss";
      const error = document.createElement("p");
      error.setAttribute("role", "alert");
      async function decide(action) {
        accept.disabled = dismiss.disabled = reason.disabled = true;
        error.textContent = "";
        try {
          await readJson(await fetch(`/api/initiatives/${encodeURIComponent(proposal.id)}/${action}`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(action === "dismiss" ? { reason: reason.value.trim() || null } : {}),
          }));
          await loadInitiatives();
          await loadReminders();
        } catch (problem) {
          error.textContent = `Could not finish: ${problem.message}. You can retry.`;
        } finally {
          accept.disabled = dismiss.disabled = reason.disabled = false;
        }
      }
      accept.addEventListener("click", () => decide("accept"));
      dismiss.addEventListener("click", () => decide("dismiss"));
      actions.append(accept);
      row.append(title, step, why, evidence);
      if (proposal.status === "proposed") {
        row.append(reason);
        actions.append(dismiss);
      }
      row.append(actions, error);
      list.append(row);
    }
  } catch (problem) {
    list.textContent = `Could not load suggestions: ${problem.message}`;
  }
}
document.getElementById("initiative-refresh").addEventListener("click", loadInitiatives);

/* -- memory notes --
   The curated facts that go into every system prompt in full, and into every
   resume draft. Until now the only way to read them was to open
   data/memory_notes/*.md - the same transparency gap the PROFILE tab's "view
   raw record" closed for the applicant profile. A true-but-irrelevant note
   became a fabricated resume entry once (CLAUDE.md, 2026-09-04), which is why
   throwing one away is a real control and not a nicety. */

const memnoteList = document.getElementById("memnote-list");

async function loadMemoryNotes() {
  memnoteList.textContent = "loading…";
  try {
    const data = await readJson(await fetch("/api/memory-notes"));
    renderMemoryNotes(data.notes || []);
    // What the layer costs, since it is paid on every single turn. Flagged past
    // ~2k tokens: at that point it is the largest thing in the prompt and the
    // "small curated set" the design assumes has stopped being small.
    const weight = document.getElementById("memnote-weight");
    const heavy = data.approx_tokens > 2000;
    weight.textContent = `${(data.notes || []).length} notes · ~${data.approx_tokens} tokens on every turn`
      + (heavy ? " — worth pruning" : "");
    weight.style.color = heavy ? "var(--fit-medium)" : "";
  } catch (err) {
    memnoteList.textContent = `couldn't load — ${err.message}`;
  }
}

function renderMemoryNotes(notes) {
  memnoteList.innerHTML = "";
  if (notes.length === 0) {
    memnoteList.textContent = "she hasn't saved any durable facts yet";
    return;
  }
  let lastCategory = null;
  for (const n of notes) {
    if (n.category !== lastCategory) {
      const head = document.createElement("div");
      head.className = "memnote-category";
      head.textContent = n.category;
      memnoteList.appendChild(head);
      lastCategory = n.category;
    }
    const row = document.createElement("div");
    row.className = "memnote-row";
    const date = document.createElement("span");
    date.className = "memnote-date";
    date.textContent = n.date;
    const text = document.createElement("span");
    text.className = "memnote-text";
    text.textContent = n.text;
    const del = document.createElement("button");
    del.className = "line-wrong-btn memnote-del";
    del.type = "button";
    del.textContent = "\u2715";
    del.title = "Forget this";
    del.setAttribute("aria-label", `Forget: ${n.text}`);
    del.addEventListener("click", async () => {
      // Deleting is what she will stop knowing about him, so it asks first.
      if (!window.confirm(`Forget this?\n\n${n.text}`)) return;
      try {
        await readJson(await fetch("/api/memory-notes/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ category: n.category, text: n.text }),
        }));
        loadMemoryNotes();
      } catch (err) {
        addLine("error", `couldn't forget that — ${err.message}`);
      }
    });
    row.append(date, text, del);
    memnoteList.appendChild(row);
  }
}

document.getElementById("memnote-add").addEventListener("click", async () => {
  const category = document.getElementById("memnote-category");
  const text = document.getElementById("memnote-text");
  try {
    await readJson(await fetch("/api/memory-notes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ category: category.value.trim() || "general", note: text.value.trim() }),
    }));
    text.value = "";
    loadMemoryNotes();
  } catch (err) {
    addLine("error", `couldn't save that note — ${err.message}`);
  }
});


/* -- reminders -- */

async function loadReminders() {
  const list = document.getElementById("reminder-list");
  const showDone = document.getElementById("reminder-show-done").checked;
  list.textContent = "loading…";
  try {
    const res = await fetch(`/api/reminders?include_done=${showDone}`);
    const data = await res.json();
    renderReminders(data.reminders || []);
  } catch (err) {
    list.textContent = `couldn't load — ${err.message}`;
  }
}

function renderReminders(reminders) {
  const list = document.getElementById("reminder-list");
  list.innerHTML = "";
  if (reminders.length === 0) {
    list.textContent = "nothing here";
    return;
  }
  for (const r of reminders) {
    const item = document.createElement("div");
    item.className = "jobs-tracker-item";
    const top = document.createElement("div");
    top.className = "jobs-tracker-item-top";
    const left = document.createElement("div");
    const text = document.createElement("div");
    text.className = "jobs-tracker-item-company" + (r.done ? " jobs-reminder-done" : "");
    text.textContent = r.text;
    const due = document.createElement("div");
    due.className = "jobs-tracker-item-role";
    due.textContent = r.due_at ? new Date(r.due_at).toLocaleString() : "no due date";
    left.append(text, due);

    const actions = document.createElement("div");
    actions.className = "jobs-reminder-actions";
    if (!r.done) {
      const completeBtn = document.createElement("button");
      completeBtn.type = "button";
      completeBtn.textContent = "done";
      completeBtn.addEventListener("click", async () => {
        await fetch(`/api/reminders/${r.id}/complete`, { method: "POST" });
        loadReminders();
      });
      const snoozeBtn = document.createElement("button");
      snoozeBtn.type = "button";
      snoozeBtn.textContent = "+1 day";
      snoozeBtn.addEventListener("click", async () => {
        const base = r.due_at ? new Date(r.due_at) : new Date();
        base.setDate(base.getDate() + 1);
        await fetch(`/api/reminders/${r.id}/snooze`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ due_at: base.toISOString() }),
        });
        loadReminders();
      });
      actions.append(completeBtn, snoozeBtn);
    }

    top.append(left, actions);
    item.appendChild(top);
    list.appendChild(item);
  }
}

document.getElementById("reminder-add").addEventListener("click", async () => {
  const text = document.getElementById("reminder-text").value.trim();
  if (!text) return;
  const dueEl = document.getElementById("reminder-due");
  const due_at = dueEl.value ? new Date(dueEl.value).toISOString() : null;
  try {
    await fetch("/api/reminders", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, due_at }),
    });
    document.getElementById("reminder-text").value = "";
    dueEl.value = "";
    loadReminders();
  } catch (err) {
    addLine("error", `couldn't add reminder — ${err.message}`);
  }
});
document.getElementById("reminder-show-done").addEventListener("change", loadReminders);

/* -- news / science (same shape, share a renderer) -- */

function renderFeed(container, items, textField) {
  container.innerHTML = "";
  if (items.length === 0) {
    container.textContent = "nothing fetched yet";
    return;
  }
  for (const item of items) {
    const el = document.createElement("div");
    el.className = "jobs-feed-item";
    const source = document.createElement("div");
    source.className = "jobs-feed-item-source";
    source.textContent = item.source;
    const title = document.createElement("div");
    title.className = "jobs-feed-item-title";
    const link = document.createElement("a");
    link.href = item.link;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = item.title;
    title.appendChild(link);
    const summary = document.createElement("div");
    summary.className = "jobs-feed-item-summary";
    summary.textContent = item[textField] || "";
    el.append(source, title, summary);
    container.appendChild(el);
  }
}

document.getElementById("news-fetch").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const list = document.getElementById("news-list");
  btn.disabled = true;
  btn.textContent = "Fetching…";
  list.textContent = "";
  try {
    const res = await fetch("/api/news");
    const data = await res.json();
    renderFeed(list, data.headlines || [], "summary");
  } catch (err) {
    list.textContent = `couldn't fetch — ${err.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Fetch tech news";
  }
});

document.getElementById("science-fetch").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const list = document.getElementById("science-list");
  btn.disabled = true;
  btn.textContent = "Fetching…";
  list.textContent = "";
  try {
    const res = await fetch("/api/science");
    const data = await res.json();
    renderFeed(list, data.facts || [], "summary");
  } catch (err) {
    list.textContent = `couldn't fetch — ${err.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Fetch science facts";
  }
});

/* -- learning -- */

document.getElementById("learning-add").addEventListener("click", async () => {
  const topic = document.getElementById("learning-topic").value.trim();
  const summary = document.getElementById("learning-summary").value.trim();
  const key_takeaway = document.getElementById("learning-takeaway").value.trim();
  if (!topic || !summary || !key_takeaway) return;
  try {
    await readJson(await fetch("/api/learning", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic, summary, key_takeaway }),
    }));
    document.getElementById("learning-topic").value = "";
    document.getElementById("learning-summary").value = "";
    document.getElementById("learning-takeaway").value = "";
    addLine("system", "Learning item saved. Find due items under Learn → Review.");
  } catch (err) {
    addLine("error", `couldn't save learning item — ${err.message}`);
  }
});



/* ---- SEARCH panel -------------------------------------------------------
   One front door over everything Kyra stores. The privacy rule is the same
   one the Python side enforces: private docs are excluded unless the toggle
   is on, and the toggle is the only control here that changes what the
   local model is allowed to read. */

const searchPanel = document.getElementById("search-panel");
const searchToggle = document.getElementById("search-toggle");
const searchClose = document.getElementById("search-close");
const searchQuery = document.getElementById("search-query");
const searchStatus = document.getElementById("search-status");
const searchResults = document.getElementById("search-results");
const searchAnswerBox = document.getElementById("search-answer-box");
const searchSensitive = document.getElementById("search-sensitive");
const searchOutside = document.getElementById("search-outside");
const searchOutsideResults = document.getElementById("search-outside-results");
let outsideQuery = "", searchGeneration = 0;
let searchKind = "";

function clearOutsideSearch() {
  outsideQuery = "";
  searchOutside.hidden = true;
  searchOutsideResults.hidden = true;
  searchOutsideResults.replaceChildren();
}
searchQuery.addEventListener("input", () => { searchGeneration++; clearOutsideSearch(); });

searchOutside.addEventListener("click", async () => {
  if (!outsideQuery) return;
  const generation = searchGeneration;
  const query = outsideQuery;
  searchOutside.disabled = true;
  setSearchStatus("Searching outside with the query only…");
  try {
    const data = await readJson(await fetch("/api/search/outside", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({query, k: 5}),
    }));
    if (generation !== searchGeneration) return;
    searchOutsideResults.replaceChildren();
    const heading = document.createElement("h3");
    heading.textContent = "OUTSIDE";
    searchOutsideResults.appendChild(heading);
    for (const hit of data.results) {
      const row = document.createElement("div");
      row.className = "search-hit";
      const title = document.createElement("strong");
      title.textContent = hit.title;
      const link = document.createElement("a");
      link.textContent = hit.url;
      if (/^https?:\/\//i.test(hit.url)) { link.href = hit.url; link.target = "_blank"; link.rel = "noopener noreferrer"; }
      const snippet = document.createElement("p");
      snippet.textContent = hit.snippet;
      row.append(title, document.createElement("br"), link, snippet);
      searchOutsideResults.appendChild(row);
    }
    searchOutsideResults.hidden = false;
    setSearchStatus(`${data.results.length} outside results from DuckDuckGo`);
  } catch (err) {
    if (generation === searchGeneration) setSearchStatus(`Outside search failed: ${err.message}`, true);
  } finally { searchOutside.disabled = false; }
});

function openSearchPanel() {
  panelOpened("search");
  closeRoomPanel();
  searchPanel.classList.add("is-open");
  searchPanel.setAttribute("aria-hidden", "false");
  searchToggle.classList.add("is-active");
  searchQuery.focus();
}
function closeSearchPanel() {
  panelClosed("search");
  searchPanel.classList.remove("is-open");
  searchPanel.setAttribute("aria-hidden", "true");
  searchToggle.classList.remove("is-active");
}
searchToggle.addEventListener("click", () => {
  searchPanel.classList.contains("is-open") ? closeSearchPanel() : PANEL_OPENERS.search();
});
searchClose.addEventListener("click", closeSearchPanel);

document.querySelectorAll("#search-kinds .search-chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    searchKind = chip.dataset.kind;
    document.querySelectorAll("#search-kinds .search-chip").forEach((c) => c.classList.toggle("is-active", c === chip));
    if (searchQuery.value.trim()) runSearch();
  });
});

function setSearchStatus(text, isError) {
  searchStatus.textContent = text;
  searchStatus.classList.toggle("is-error", !!isError);
}

function searchBody() {
  return {
    query: searchQuery.value.trim(),
    kinds: searchKind ? [searchKind] : null,
    include_sensitive: searchSensitive.checked,
  };
}

function renderHits(hits) {
  searchResults.replaceChildren();
  for (const hit of hits) {
    const row = document.createElement("div");
    row.className = `search-hit${hit.sensitive ? " is-sensitive" : ""}`;
    const head = document.createElement("div");
    head.className = "search-hit-head";
    const kind = document.createElement("span");
    kind.className = "search-hit-kind";
    kind.textContent = hit.sensitive ? `${hit.kind} · private` : hit.kind;
    const path = document.createElement("span");
    path.className = "search-hit-path";
    path.textContent = hit.path;
    const score = document.createElement("span");
    score.className = "search-hit-score";
    score.textContent = hit.score.toFixed(3);
    head.append(kind, path, score);
    const snippet = document.createElement("div");
    snippet.className = "search-hit-snippet";
    snippet.textContent = hit.snippet;
    row.append(head, snippet);
    searchResults.appendChild(row);
  }
}

/* Searching never refreshes the index - only REINDEX here and the 05:00 digest
   do - so results can quietly predate this morning's edits. Say how old it is
   rather than letting a stale answer look current. */
function indexAge(indexedAt) {
  if (!indexedAt) return "index never built — press REINDEX";
  const hours = (Date.now() / 1000 - indexedAt) / 3600;
  if (hours < 1) return "index current";
  if (hours < 24) return `index ${Math.round(hours)}h old`;
  return `index ${Math.round(hours / 24)}d old — press REINDEX`;
}

async function runSearch() {
  const generation = ++searchGeneration;
  clearOutsideSearch();
  const body = searchBody();
  if (!body.query) return;
  searchAnswerBox.hidden = true;
  setSearchStatus("searching…");
  try {
    const res = await fetch("/api/search", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const data = await readJson(res);
    if (generation !== searchGeneration) return;
    renderHits(data.hits);
    if (data.indexed_at != null && data.found_inside === false) {
      outsideQuery = body.query;
      searchOutside.hidden = false;
    }
    setSearchStatus(
      `${data.count} result${data.count === 1 ? "" : "s"}`
      + `${data.include_sensitive ? " · private docs included" : ""}`
      + ` · ${indexAge(data.indexed_at)}`
    );
  } catch (err) {
    if (generation !== searchGeneration) return;
    searchResults.replaceChildren();
    setSearchStatus(`search failed — ${err.message}`, true);
  }
}

async function runSearchAnswer() {
  const body = searchBody();
  if (!body.query) return;
  setSearchStatus("thinking…");
  searchAnswerBox.hidden = false;
  searchAnswerBox.textContent = "";
  try {
    const res = await fetch("/api/search/answer", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!res.ok || !res.body) throw new Error(`server returned ${res.status}`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        let event = "message";
        let data = "";
        for (const line of block.split("\n")) {
          if (line.startsWith("event: ")) event = line.slice(7);
          else if (line.startsWith("data: ")) data += line.slice(6);
        }
        if (!data) continue;
        const payload = JSON.parse(data);
        if (event === "progress") setSearchStatus(payload);
        else if (event === "error") throw new Error(payload);
        else if (event === "done") {
          searchAnswerBox.textContent = payload.text;
          if (payload.citations.length) {
            const cites = document.createElement("span");
            cites.className = "search-answer-cites";
            cites.textContent = payload.citations.map((c) => `[${c.n}] ${c.path}`).join("  ·  ");
            searchAnswerBox.appendChild(cites);
          }
          for (const w of payload.warnings || []) {
            const warn = document.createElement("span");
            warn.className = "search-answer-warn";
            warn.textContent = `⚠ ${w}`;
            searchAnswerBox.appendChild(warn);
          }
          renderHits(payload.hits || []);
          setSearchStatus(`answered from ${payload.citations.length} source(s)`);
        }
      }
    }
  } catch (err) {
    searchAnswerBox.hidden = true;
    setSearchStatus(`answer failed — ${err.message}`, true);
  }
}

async function runSearchReindex() {
  setSearchStatus("reindexing — the index only refreshes when you ask…");
  try {
    const res = await fetch("/api/search/reindex", { method: "POST" });
    const data = await readJson(res);
    setSearchStatus(data.summary + (data.errors.length ? ` · ${data.errors.length} error(s)` : ""));
  } catch (err) {
    setSearchStatus(`reindex failed — ${err.message}`, true);
  }
}

document.getElementById("search-run").addEventListener("click", runSearch);
document.getElementById("search-answer").addEventListener("click", runSearchAnswer);
document.getElementById("search-reindex").addEventListener("click", runSearchReindex);
searchQuery.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.keyCode === 13) runSearch();
});
searchSensitive.addEventListener("change", () => {
  if (searchQuery.value.trim()) runSearch();
});

/* ---------------- focus blocks ----------------
   Plan and the evidence for every default: docs/plans/2026-09-08-attention-environment.md.

   Two things about this code are deliberate and easy to "fix" wrongly.

   1. All the audio is synthesised here from oscillators and filters. There is no
      track list and no fetch of media, because the strongest finding in the review
      was that lyrics and speech reliably hurt verbal work - so the catalogue is
      built so that a song cannot be added without rewriting this file.
   2. The gain comes from the server, not from a slider. GAIN_CEILING is mirrored
      below only as a second fence; the plan the server sends is the authority. A
      focus layer that competes with thinking is the failure mode, so louder is not
      a feature request.

   The condition is not displayed while the block runs: the server strips the name
   from the payload, and nothing here reconstructs it from the spec. */

const FOCUS_GAIN_CEILING = 0.30;   // mirrors companion/focus.py; the server value still wins
const PROBE_SECONDS = 60;
const PROBE_MIN_WAIT_MS = 1500;
const PROBE_MAX_WAIT_MS = 4500;
const PROBE_LAPSE_MS = 500;
const PROBE_ANTICIPATION_MS = 100;
// A dot nobody answers is a lapse, not a reason to wait. Without this the probe
// hangs forever on an unanswered trial - found on the first real run, where the
// end-of-block probe stalled with "0s left" and the block was therefore never
// recorded at all. PVT convention caps an unanswered trial rather than dropping it.
const PROBE_TIMEOUT_MS = 3000;

const focusToggle = document.getElementById("focus-toggle");
const focusPanel = document.getElementById("focus-panel");
const focusClose = document.getElementById("focus-close");
const focusChip = document.getElementById("focus-chip");
const focusIdleView = document.getElementById("focus-idle");
const focusRunningView = document.getElementById("focus-running");
const focusProbeView = document.getElementById("focus-probe");
const focusResultBox = document.getElementById("focus-result");
const focusRemaining = document.getElementById("focus-remaining");
const focusTaskLine = document.getElementById("focus-task-line");
const focusNextBreak = document.getElementById("focus-next-break");
const focusTarget = document.getElementById("focus-target");
const focusProbeProgress = document.getElementById("focus-probe-progress");

let focusState = null;      // { id, plan, startedAt, task }
let focusTicker = null;
let focusBreakTimers = [];
let focusRating = null;

/* -- the audio layer ---------------------------------------------------- */

const focusAudio = (() => {
  let nodes = [];
  let master = null;

  function ctx() {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    return audioCtx;
  }

  // Pink noise by Paul Kellett's filter, applied while filling one looping
  // buffer. Pink rather than white: it is the 1/f family the meta-analysed
  // studies used and it is judged less aversive at the same level.
  function pinkBuffer(ac, seconds) {
    const buf = ac.createBuffer(1, ac.sampleRate * seconds, ac.sampleRate);
    const out = buf.getChannelData(0);
    let b0 = 0, b1 = 0, b2 = 0, b3 = 0, b4 = 0, b5 = 0, b6 = 0;
    for (let i = 0; i < out.length; i++) {
      const white = Math.random() * 2 - 1;
      b0 = 0.99886 * b0 + white * 0.0555179;
      b1 = 0.99332 * b1 + white * 0.0750759;
      b2 = 0.96900 * b2 + white * 0.1538520;
      b3 = 0.86650 * b3 + white * 0.3104856;
      b4 = 0.55000 * b4 + white * 0.5329522;
      b5 = -0.7616 * b5 - white * 0.0168980;
      out[i] = (b0 + b1 + b2 + b3 + b4 + b5 + b6 + white * 0.5362) * 0.11;
      b6 = white * 0.115926;
    }
    return buf;
  }

  function build(ac, spec) {
    const made = [];
    if (spec.kind === "noise") {
      const src = ac.createBufferSource();
      src.buffer = pinkBuffer(ac, 4);
      src.loop = true;
      const lp = ac.createBiquadFilter();
      lp.type = "lowpass";
      lp.frequency.value = 5000;   // takes the hiss off the top without making it a rumble
      src.connect(lp).connect(master);
      src.start();
      made.push(src, lp);
    } else if (spec.kind === "modulated_pad") {
      // The Brain.fm mechanism in two nodes: a soft pad whose amplitude is
      // modulated at mod_hz. The pad is a root plus a fifth through a lowpass,
      // so it reads as a texture rather than as a note being held.
      const depth = ac.createGain();
      depth.gain.value = 1 - (spec.depth ?? 0.6);
      const lfo = ac.createOscillator();
      const lfoGain = ac.createGain();
      lfo.frequency.value = spec.mod_hz ?? 16;
      lfoGain.gain.value = spec.depth ?? 0.6;
      lfo.connect(lfoGain).connect(depth.gain);
      lfo.start();
      const lp = ac.createBiquadFilter();
      lp.type = "lowpass";
      lp.frequency.value = 900;
      for (const ratio of [1, 1.5]) {
        const osc = ac.createOscillator();
        osc.type = "triangle";
        osc.frequency.value = (spec.carrier_hz ?? 220) * ratio;
        const trim = ac.createGain();
        trim.gain.value = ratio === 1 ? 0.6 : 0.25;
        osc.connect(trim).connect(lp);
        osc.start();
        made.push(osc, trim);
      }
      lp.connect(depth).connect(master);
      made.push(lfo, lfoGain, lp, depth);
    } else if (spec.kind === "binaural") {
      // Two carriers a beat apart, one per ear. Needs headphones to exist at
      // all, which is why the panel asks for them on every block rather than
      // only on this one - saying it here would unblind the arm.
      const merger = ac.createChannelMerger(2);
      [spec.carrier_hz ?? 340, (spec.carrier_hz ?? 340) + (spec.beat_hz ?? 16)].forEach((f, i) => {
        const osc = ac.createOscillator();
        osc.type = "sine";
        osc.frequency.value = f;
        osc.connect(merger, 0, i);
        osc.start();
        made.push(osc);
      });
      merger.connect(master);
      made.push(merger);
    }
    return made;
  }

  return {
    async start(plan) {
      this.stop(0);
      if (!plan || plan.spec?.kind === "silence" || !plan.gain) return;
      const ac = ctx();
      if (ac.state === "suspended") await ac.resume().catch(() => {});
      master = ac.createGain();
      master.gain.setValueAtTime(0.0001, ac.currentTime);
      const target = Math.min(plan.gain, FOCUS_GAIN_CEILING);
      master.gain.exponentialRampToValueAtTime(target, ac.currentTime + (plan.fade_seconds || 3));
      master.connect(ac.destination);
      nodes = build(ac, plan.spec || {});
    },
    // Always a fade. An abrupt stop is its own startle, which is the same
    // reason the health plan's sleep sound may never just cut out.
    stop(fadeSeconds = 3) {
      if (!master) { nodes = []; return; }
      const ac = ctx();
      const dying = master, doomed = nodes;
      nodes = []; master = null;
      try {
        dying.gain.cancelScheduledValues(ac.currentTime);
        dying.gain.setValueAtTime(Math.max(dying.gain.value, 0.0001), ac.currentTime);
        dying.gain.exponentialRampToValueAtTime(0.0001, ac.currentTime + Math.max(fadeSeconds, 0.05));
      } catch (_) { /* a context that never started */ }
      setTimeout(() => {
        for (const n of doomed) { try { n.stop && n.stop(); } catch (_) {} try { n.disconnect(); } catch (_) {} }
        try { dying.disconnect(); } catch (_) {}
      }, Math.max(fadeSeconds, 0.05) * 1000 + 120);
    },
    get playing() { return master !== null; },
  };
})();

/* -- the reaction-time probe (a short PVT) ------------------------------- */

function runProbe(phase) {
  return new Promise((resolve) => {
    // Measured, not assumed: in a hidden tab this browser clamps a setTimeout
    // nested inside a setInterval to ~1000 ms, which turned a scripted 250 ms
    // response into a stored 1000 ms during verification. The probe's own maths
    // was right both times - the environment was not. So a probe that starts
    // hidden is refused out loud rather than saved as a number about Duc.
    if (document.hidden) {
      addLine("system", "Probe skipped - this tab is in the background, where timing is not measurable.");
      return resolve(null);
    }
    const rts = [];
    let falseStarts = 0, lit = false, onset = 0, waitTimer = null, endTimer = null, done = false;
    document.getElementById("focus-probe-head").textContent =
      phase === "start" ? "PROBE — BEFORE THE BLOCK" : "PROBE — AFTER THE BLOCK";
    focusProbeView.hidden = false;
    focusIdleView.hidden = true;
    focusRunningView.hidden = true;
    const deadline = performance.now() + PROBE_SECONDS * 1000;

    function paint() {
      const left = Math.max(0, Math.ceil((deadline - performance.now()) / 1000));
      focusProbeProgress.textContent = `${left}s left · ${rts.length} responses`;
    }
    function armNext() {
      if (done) return;
      lit = false;
      focusTarget.dataset.lit = "false";
      paint();
      if (performance.now() >= deadline) return finish();
      const wait = PROBE_MIN_WAIT_MS + Math.random() * (PROBE_MAX_WAIT_MS - PROBE_MIN_WAIT_MS);
      waitTimer = setTimeout(() => {
        if (done) return;
        lit = true;
        onset = performance.now();
        focusTarget.dataset.lit = "true";
      }, wait);
    }
    function respond() {
      if (done) return;
      if (!lit) {
        // Pressing before the dot is a false start, not a fast trial. Counting
        // it as a response is how a PVT flatters an impatient participant.
        falseStarts++;
        focusTarget.dataset.lit = "early";
        clearTimeout(waitTimer);
        setTimeout(armNext, 600);
        return;
      }
      const rt = performance.now() - onset;
      if (rt >= PROBE_ANTICIPATION_MS) rts.push(rt);
      else falseStarts++;
      armNext();
    }
    function onKey(e) {
      if (e.code === "Space" || e.key === " ") { e.preventDefault(); respond(); }
    }
    function finish(skipped = false) {
      if (done) return;
      done = true;
      clearTimeout(waitTimer); clearInterval(endTimer);
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("visibilitychange", onHide);
      focusTarget.removeEventListener("click", respond);
      skipBtn.removeEventListener("click", onSkip);
      focusTarget.dataset.lit = "false";
      focusProbeView.hidden = true;
      if (skipped || rts.length === 0) return resolve(null);
      const sorted = [...rts].sort((a, b) => a - b);
      const mid = Math.floor(sorted.length / 2);
      const median = sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
      resolve({
        median_ms: Math.round(median * 10) / 10,
        lapses: rts.filter((r) => r > PROBE_LAPSE_MS).length,
        trials: rts.length,
        false_starts: falseStarts,
      });
    }
    // Timers stay accurate in a background tab in this browser, but a probe run
    // while Duc is looking at something else measures nothing about him. Discarded
    // rather than saved, so a stray number cannot enter the experiment.
    function onHide() {
      if (!document.hidden) return;
      addLine("system", "Probe discarded - the tab lost focus, so the timing would not have been yours.");
      finish(true);
    }
    document.addEventListener("visibilitychange", onHide);
    const skipBtn = document.getElementById("focus-probe-skip");
    const onSkip = () => finish(true);
    skipBtn.addEventListener("click", onSkip);
    document.addEventListener("keydown", onKey);
    focusTarget.addEventListener("click", respond);
    endTimer = setInterval(() => {
      paint();
      // An unanswered dot times out as a lapse and the run moves on, so the probe
      // can never stall mid-run and can never outlive its deadline by more than
      // one timeout.
      if (lit && performance.now() - onset > PROBE_TIMEOUT_MS) {
        rts.push(PROBE_TIMEOUT_MS);
        armNext();
        return;
      }
      if (performance.now() >= deadline && !lit) finish();
    }, 250);
    armNext();
  });
}

async function sendProbe(id, phase, result) {
  if (!result) return;
  try {
    await readJson(await fetch("/api/focus/probe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, phase, median_ms: result.median_ms, lapses: result.lapses }),
    }));
  } catch (e) {
    addLine("system", `probe not saved: ${e.message}`);
  }
}

/* -- the block lifecycle ------------------------------------------------ */

function focusClearTimers() {
  clearInterval(focusTicker); focusTicker = null;
  focusBreakTimers.forEach(clearTimeout);
  focusBreakTimers = [];
}

function focusPaint() {
  if (!focusState) return;
  const elapsedMin = (Date.now() - focusState.startedAt) / 60000;
  const left = Math.max(0, focusState.plan.minutes - elapsedMin);
  const mm = String(Math.floor(left)).padStart(2, "0");
  const ss = String(Math.floor((left % 1) * 60)).padStart(2, "0");
  focusRemaining.textContent = `${mm}:${ss}`;
  focusChip.textContent = resumeFocusPlan ? `FOCUS ${mm}:${ss} · Resume sound` : `FOCUS ${mm}:${ss}`;
  const nextBreak = (focusState.plan.break_offsets || []).find((b) => b > elapsedMin);
  focusNextBreak.textContent = nextBreak
    ? `next break cue in ${Math.ceil(nextBreak - elapsedMin)} min`
    : "no more break cues this block";
  if (left <= 0) focusReachedEnd();
}

function focusReachedEnd() {
  focusClearTimers();
  setPresence("idle", "block over — rate it in FOCUS");
  addLine("system", "Focus block finished. Rate it in the FOCUS panel to record the result.");
  earcon("listening");
  focusNotify("Focus block finished", "Rate it in the FOCUS panel to record the result.");
}

// One guarded helper for both cues. A browser that denies notifications, or one
// that needs a service worker for them, falls through to the in-page cue that
// has already fired - so this can only ever add reach, never replace it.
function focusNotify(title, body) {
  if (!window.Notification || Notification.permission !== "granted") return;
  try { new Notification(title, { body, tag: "kyra-focus" }); } catch (_) { /* in-page cue stands */ }
}

function focusScheduleBreaks() {
  const elapsedMin = (Date.now() - focusState.startedAt) / 60000;
  for (const at of focusState.plan.break_offsets || []) {
    const inMs = (at - elapsedMin) * 60000;
    if (inMs <= 0) continue;
    focusBreakTimers.push(setTimeout(() => {
      // Twenty seconds of looking away is the whole intervention: the
      // micro-break meta-analysis and Apple's headset guidance agree on the
      // cadence, and neither needs Kyra to say a paragraph about it.
      earcon("listening");
      setPresence(presence, "break cue — look 20 feet away for 20 seconds");
      addLine("system", `Break cue at ${at} minutes. Look away for 20 seconds.`);
      // The break is the best-supported intervention on the whole page, and a cue
      // Duc cannot see because he is in another tab is not a cue. Notifications are
      // only requested once he has actually started a block, never on page load.
      focusNotify("Look away for 20 seconds", `${at} minutes in.`);
    }, inMs));
  }
}

function focusShowRunning(payload) {
  focusState = {
    id: payload.session.id,
    plan: payload.plan,
    startedAt: Date.parse(payload.session.started_at),
    task: payload.session.task,
  };
  focusRating = null;
  document.querySelectorAll(".focus-rate-btn").forEach((b) => b.classList.remove("is-active"));
  document.getElementById("focus-note").value = "";
  focusIdleView.hidden = true;
  focusProbeView.hidden = true;
  focusRunningView.hidden = false;
  focusResultBox.hidden = true;
  focusTaskLine.textContent = focusState.task || "(no task named)";
  focusChip.hidden = false;
  document.body.dataset.focus = "running";
  focusClearTimers();
  focusPaint();
  focusTicker = setInterval(focusPaint, 1000);
  focusScheduleBreaks();
}

function focusShowIdle(completed) {
  focusClearTimers();
  focusState = null;
  resumeFocusPlan = null;
  focusIdleView.hidden = false;
  focusRunningView.hidden = true;
  focusProbeView.hidden = true;
  focusChip.hidden = true;
  delete document.body.dataset.focus;
  if (typeof completed === "number") {
    document.getElementById("focus-start").textContent =
      completed ? `Start block (${completed} done)` : "Start block";
  }
}

function focusApplyEvening(evening) {
  // Only ever set from the server's own clock, so "evening" means Duc's
  // evening. The tokens it swaps in are strictly dimmer and warmer.
  if (evening) document.body.dataset.focusEvening = "true";
  else delete document.body.dataset.focusEvening;
}

async function focusStart() {
  const minutes = Number(document.getElementById("focus-minutes").value);
  const task = document.getElementById("focus-task").value.trim();
  const wantProbe = document.getElementById("focus-probe-on").checked;
  const startBtn = document.getElementById("focus-start");
  startBtn.disabled = true;
  focusResultBox.hidden = true;
  // Asked here rather than on load: a permission prompt makes sense the moment
  // Duc opts into being interrupted, and nowhere else.
  if (window.Notification && Notification.permission === "default") {
    Notification.requestPermission().catch(() => {});
  }
  try {
    const payload = await readJson(await fetch("/api/focus/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ minutes, task }),
    }));
    focusApplyEvening(payload.plan.evening);
    if (wantProbe) {
      const before = await runProbe("start");
      await sendProbe(payload.session.id, "start", before);
    }
    focusShowRunning(payload);
    await focusAudio.start(payload.plan);
    addLine("system", `Focus block started: ${minutes} minutes${task ? ` on ${task}` : ""}.`);
  } catch (e) {
    addLine("system", `could not start a block: ${e.message}`);
    focusShowIdle();
  } finally {
    startBtn.disabled = false;
  }
}

async function focusEnd() {
  const endBtn = document.getElementById("focus-end");
  const id = focusState?.id;
  const wantProbe = document.getElementById("focus-probe-on").checked;
  endBtn.disabled = true;
  focusAudio.stop(focusState?.plan?.fade_seconds ?? 3);
  focusClearTimers();
  try {
    if (wantProbe && id) {
      const after = await runProbe("end");
      await sendProbe(id, "end", after);
    }
    const out = await readJson(await fetch("/api/focus/end", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rating: focusRating, note: document.getElementById("focus-note").value.trim() }),
    }));
    focusShowIdle();
    const delta = out.probe_delta_ms;
    focusResultBox.hidden = false;
    focusResultBox.innerHTML = "";
    const heading = document.createElement("div");
    heading.innerHTML = `Block over. Sound condition was <strong>${out.condition}</strong>.`;
    focusResultBox.appendChild(heading);
    if (delta !== null && delta !== undefined) {
      const line = document.createElement("div");
      line.textContent = delta > 0
        ? `Reaction time ${Math.round(delta)} ms slower by the end.`
        : `Reaction time ${Math.abs(Math.round(delta))} ms faster by the end.`;
      focusResultBox.appendChild(line);
    }
    const caveat = document.createElement("div");
    caveat.className = "focus-note";
    caveat.textContent = "One block says nothing. The report needs about eight per condition.";
    focusResultBox.appendChild(caveat);
    loadFocusHistory();
  } catch (e) {
    addLine("system", `could not end the block: ${e.message}`);
    focusShowIdle();
  } finally {
    endBtn.disabled = false;
  }
}

function focusRoomRecap(sessionId) {
  const wrap = document.createElement("div");
  wrap.className = "focus-room-disclosure";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "jobs-btn jobs-btn-quiet";
  button.textContent = "Room recap";
  button.setAttribute("aria-expanded", "false");
  const recap = document.createElement("div");
  recap.id = `focus-room-${sessionId}`;
  recap.className = "focus-room-recap";
  recap.hidden = true;
  recap.setAttribute("role", "region");
  recap.setAttribute("aria-label", "Room recap");
  recap.setAttribute("aria-live", "polite");
  button.setAttribute("aria-controls", recap.id);
  wrap.append(button, recap);
  let loaded = false;
  button.addEventListener("click", async () => {
    if (button.disabled) return;
    if (loaded) {
      recap.hidden = !recap.hidden;
      button.setAttribute("aria-expanded", String(!recap.hidden));
      return;
    }
    recap.hidden = false;
    button.setAttribute("aria-expanded", "true");
    button.disabled = true;
    recap.setAttribute("aria-busy", "true");
    recap.textContent = "Loading saved room readings…";
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const data = await readJson(await fetch(`/api/focus/${sessionId}/room`, { cache: "no-store", signal: controller.signal }));
      if (!wrap.isConnected) return; // Refresh replaces the row, even when its session ID is unchanged.
      recap.replaceChildren();
      const summary = document.createElement("p");
      summary.textContent = data.history_state === "not_recorded" ? "No saved room history yet."
        : data.samples === 0 ? "No usable room readings during this block."
        : `Humidity · Min ${data.min}${data.unit} · Max ${data.max}${data.unit}`;
      const coverage = document.createElement("p");
      const count = `${data.samples} of ${data.expected_slots} five-minute slots with readings`;
      coverage.textContent = `${count}. Saved readings received during this block.`;
      const chart = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      const columns = Math.min(24, Math.max(1, data.slots.length)), rows = Math.ceil(data.slots.length / columns) || 1;
      chart.setAttribute("viewBox", `0 0 ${columns * 5} ${rows * 14}`);
      chart.style.height = `${Math.max(24, rows * 12)}px`;
      chart.setAttribute("preserveAspectRatio", "none");
      chart.setAttribute("role", "img");
      chart.setAttribute("aria-label", `${count}. Outlined marks are missing readings; each mark is one slot, left to right then down in time order.`);
      chart.classList.add("focus-room-slots");
      data.slots.forEach((slot, index) => {
        const mark = document.createElementNS(chart.namespaceURI, "rect");
        mark.setAttribute("x", String((index % columns) * 5 + 1)); mark.setAttribute("y", String(Math.floor(index / columns) * 14 + 2));
        mark.setAttribute("width", "3"); mark.setAttribute("height", "10");
        if (slot.value === null) mark.classList.add("is-missing");
        const title = document.createElementNS(chart.namespaceURI, "title");
        title.textContent = `${slot.slot}: ${slot.value === null ? "no usable reading" : `${slot.value}${data.unit}`}`;
        mark.appendChild(title); chart.appendChild(mark);
      });
      const legend = document.createElement("p");
      legend.className = "focus-note";
      legend.textContent = "Filled: reading. Outline: missing. Slots run left to right, then down; not monitoring time.";
      const note = document.createElement("p");
      note.className = "focus-note";
      note.textContent = data.note;
      recap.append(summary, coverage, chart, legend, note);
      loaded = true;
      button.textContent = "Room recap";
    } catch (error) {
      if (!wrap.isConnected) return;
      recap.textContent = error.code === "owner_only" ? "Room recap is not available on this device."
        : "Could not load saved room readings. Retry when ready.";
      button.textContent = "Retry room recap";
    } finally {
      clearTimeout(timeout);
      button.disabled = false;
      recap.setAttribute("aria-busy", "false");
    }
  });
  return wrap;
}

async function loadFocusHistory() {
  const list = document.getElementById("focus-history-list");
  list.textContent = "loading…";
  try {
    const data = await readJson(await fetch("/api/focus/history"));
    list.textContent = "";
    if (!data.sessions.length) { list.textContent = "no finished blocks yet"; return; }
    for (const s of data.sessions) {
      const row = document.createElement("div");
      row.className = "jobs-tracker-item";
      const when = new Date(s.started_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
      const delta = (s.probe_end_ms != null && s.probe_start_ms != null)
        ? `${s.probe_end_ms - s.probe_start_ms > 0 ? "+" : ""}${Math.round(s.probe_end_ms - s.probe_start_ms)} ms`
        : "no probe";
      const head = document.createElement("div");
      head.innerHTML = `<strong>${s.condition}</strong> · ${s.planned_minutes} min · ${delta}`;
      const sub = document.createElement("div");
      sub.className = "focus-note";
      sub.textContent = `${when}${s.task ? ` · ${s.task}` : ""}${s.rating ? ` · rated ${s.rating}/5` : ""}${s.note ? ` · ${s.note}` : ""}`;
      row.append(head, sub);
      if (Number.isSafeInteger(s.id) && s.id > 0 && s.ended_at && !s.abandoned) row.appendChild(focusRoomRecap(s.id));
      list.appendChild(row);
    }
  } catch (e) {
    list.textContent = `could not load history: ${e.message}`;
  }
}

document.getElementById("focus-about").innerHTML = `
  <p>What this does, and why each part is here. Full review and sources:
  <code>docs/plans/2026-09-08-attention-environment.md</code>.</p>
  <h4>The defaults are removals</h4>
  <ul>
    <li>No lyrics and no speech, ever. Every sound is synthesised here, so there is nowhere to put a track.</li>
    <li>The volume comes from the server and is capped. Louder is not a setting.</li>
    <li>Sound always fades in and out.</li>
    <li>A break cue at least every 25 minutes, for the eyes and for vigour.</li>
    <li>After 21:00 the interface only gets dimmer and warmer, never brighter.</li>
  </ul>
  <h4>The additions are an experiment</h4>
  <ul>
    <li>Four sound conditions, assigned in a balanced shuffled order, hidden until each block ends.</li>
    <li>A 60-second reaction-time probe at both ends, plus your own rating.</li>
    <li><code>scripts/focus_report.py</code> reads them back per condition, with the noise floor stated.</li>
  </ul>
  <p class="focus-note">Nothing here claims a benefit. Broadband noise helps listeners with attention difficulties
  and measurably hurts everyone else, so the population result cannot answer this for you. The report can.</p>
`;

/* -- wiring -------------------------------------------------------------- */

function openFocusPanel() {
  panelOpened("focus");
  closeRoomPanel();
  focusPanel.classList.add("is-open");
  focusPanel.setAttribute("aria-hidden", "false");
  focusToggle.classList.add("is-active");
  if (!focusState) focusRestore();
  if (!panelPoll) panelPoll = setInterval(focusSync, 5 * 60 * 1000);
}
function closeFocusPanel() {
  panelClosed("focus");

  focusPanel.classList.remove("is-open");
  focusPanel.setAttribute("aria-hidden", "true");
  focusToggle.classList.remove("is-active");
}
focusToggle.addEventListener("click", () => {
  focusPanel.classList.contains("is-open") ? closeFocusPanel() : PANEL_OPENERS.focus();
});
focusClose.addEventListener("click", closeFocusPanel);
let resumeFocusPlan = null;
function offerFocusSound(plan) {
  resumeFocusPlan = plan;
  focusChip.textContent = "Focus · Resume sound";
}
focusChip.addEventListener("click", async () => {
  PANEL_OPENERS.focus();
  if (resumeFocusPlan) {
    const plan = resumeFocusPlan;
    resumeFocusPlan = null;
    await focusAudio.start(plan);
  }
});

document.querySelectorAll("#focus-panel .jobs-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll("#focus-panel .jobs-tab").forEach((t) => t.classList.toggle("is-active", t === tab));
    document.querySelectorAll("#focus-panel .jobs-tab-panel").forEach((p) => {
      p.classList.toggle("is-active", p.dataset.focusTabPanel === tab.dataset.focusTab);
    });
    if (tab.dataset.focusTab === "history") loadFocusHistory();
  });
});

document.getElementById("focus-start").addEventListener("click", focusStart);
document.getElementById("focus-end").addEventListener("click", focusEnd);
document.getElementById("focus-history-refresh").addEventListener("click", loadFocusHistory);
document.querySelectorAll(".focus-rate-btn").forEach((b) => {
  b.addEventListener("click", () => {
    focusRating = Number(b.dataset.rating);
    document.querySelectorAll(".focus-rate-btn").forEach((o) => o.classList.toggle("is-active", o === b));
  });
});

// A block can also be started or ended by talking to Kyra - "start a 50 minute
// block" goes through start_focus_block on the tool path, which the browser has
// no other way to learn about. Without this the two front doors disagree: the
// server has Duc in a block while the HUD sits idle with no sound, no clock and
// no break cue. One cheap GET after each turn, and the server stays the source
// of truth about whether he is working.
async function focusSync() {
  try {
    const data = await readJson(await fetch("/api/focus/active"));

    focusApplyEvening(data.evening ?? data.plan?.evening);
    const runningHere = focusState !== null;
    if (data.running && !runningHere) {
      focusShowRunning(data);
      // A restored block waits for an explicit click before starting sound.
      offerFocusSound(data.plan);
    } else if (!data.running && runningHere) {
      focusAudio.stop(3);
      focusShowIdle(data.completed_blocks);
    }
  } catch (_) {
    // server unreachable - leave the browser as it is rather than guessing
  }
}

// Resume after a reload: the block is on the server, so the tab is not the
// source of truth about whether Duc is working. Audio does not restart on its
// own (an autoplay policy would block it silently); the chip offers it back.
async function focusRestore() {
  try {
    const data = await readJson(await fetch("/api/focus/active"));

    focusApplyEvening(data.evening ?? data.plan?.evening);
    if (!data.running) { focusShowIdle(data.completed_blocks); return; }
    focusShowRunning(data);
    offerFocusSound(data.plan);
  } catch (_) {
    // server not reachable yet - the panel still opens, the block is still there
  }
}

// Wind-down must arrive on its own: without this the evening theme only applied
// when Duc happened to send a turn, so an evening spent reading would stay on the
// daytime palette - which is the one thing the evening rule exists to prevent. The
// hour lives in companion/focus.py and is reported by /api/focus/active, so this
// asks rather than duplicating the constant. Slow on purpose; it is a theme, not a
// countdown, and the same call keeps the block state in step with the other front
// doors for free.

/* ---------------- console: registry, hand-offs and run receipts ---------------- */
const consolePanel = document.getElementById("console-panel");
const consoleToggle = document.getElementById("console-toggle");
const consoleTabs = [...consolePanel.querySelectorAll("[data-console-tab]")];
let consoleToolsLoaded = false;
let consoleThreadRequest = 0;

function consoleNode(tag, text = "", className = "") {
  const node = document.createElement(tag);
  node.textContent = text;
  node.className = className;
  return node;
}
function consoleButton(text, onClick) {
  const button = consoleNode("button", text, "jobs-btn");
  button.type = "button";
  button.addEventListener("click", onClick);
  return button;
}
function closeConsolePanel() {
  panelClosed("console");
  consolePanel.classList.remove("is-open");
  consolePanel.setAttribute("aria-hidden", "true");
  consolePanel.inert = true;
  consoleToggle.classList.remove("is-active");
  consoleToggle.setAttribute("aria-expanded", "false");
  consoleToggle.focus();
}
function loadConsoleTab(name) {
  if (name === "tools" && !consoleToolsLoaded) loadConsoleTools();
  if (name === "agents") loadConsoleAgents();
  if (name === "runs") loadConsoleRuns();
  if (name === "privacy") loadConsolePrivacy();
}
function openConsolePanel() {
  panelOpened("console");
  closeJobsPanel(); closeToolsPanel(); closeSearchPanel(); closeFocusPanel();
  consolePanel.inert = false;
  consolePanel.classList.add("is-open");
  consolePanel.setAttribute("aria-hidden", "false");
  consoleToggle.classList.add("is-active");
  consoleToggle.setAttribute("aria-expanded", "true");
  const active = consoleTabs.find((tab) => tab.classList.contains("is-active"));
  active.focus();
  loadConsoleTab(active.dataset.consoleTab);
}
consoleToggle.addEventListener("click", () => {
  consolePanel.classList.contains("is-open") ? closeConsolePanel() : PANEL_OPENERS.console();
});
document.getElementById("console-close").addEventListener("click", closeConsolePanel);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && consolePanel.classList.contains("is-open")) closeConsolePanel();
});
consoleTabs.forEach((tab, index) => {
  tab.addEventListener("click", () => {
    consoleTabs.forEach((other) => {
      const active = other === tab;
      other.classList.toggle("is-active", active);
      other.setAttribute("aria-selected", String(active));
      other.tabIndex = active ? 0 : -1;
    });
    consolePanel.querySelectorAll("[data-console-tab-panel]").forEach((panel) => {
      panel.classList.toggle("is-active", panel.dataset.consoleTabPanel === tab.dataset.consoleTab);
    });
    loadConsoleTab(tab.dataset.consoleTab);
  });
  tab.addEventListener("keydown", (event) => {
    const offsets = { ArrowRight: 1, ArrowLeft: -1, Home: -index, End: consoleTabs.length - 1 - index };
    if (!(event.key in offsets)) return;
    event.preventDefault();
    const next = consoleTabs[(index + offsets[event.key] + consoleTabs.length) % consoleTabs.length];
    next.click(); next.focus();
  });
});

function consoleOpenPanel(destination) {
  const [panel, tab] = destination.split("/");
  if (!Object.hasOwn(PANEL_OPENERS, panel)) return;
  closeConsolePanel();
  PANEL_OPENERS[panel]();
  const attribute = panel === "jobs" ? "data-tab" : `data-${panel}-tab`;
  const target = document.querySelector(`#${panel}-panel [${attribute}="${tab}"]`);
  if (target) { target.click(); target.focus(); }
  else document.getElementById(`${panel}-close`).focus();
}

function consoleRunForm(tool) {
  const form = consoleNode("form", "", "console-run-form");
  form.hidden = true;
  const fields = [];
  for (const [name, schema] of Object.entries(tool.input_schema.properties || {})) {
    const required = (tool.input_schema.required || []).includes(name);
    const label = consoleNode("label", "", "jobs-field");
    label.append(consoleNode("span", `${name}${required ? " *" : " (optional)"}`));
    let field;
    if (schema.enum) {
      field = document.createElement("select");
      field.append(new Option("Choose…", ""));
      schema.enum.forEach((value) => field.append(new Option(String(value), String(value))));
    } else if (schema.type === "boolean") {
      field = document.createElement("input");
      field.type = "checkbox";
    } else if (schema.type === "integer" || schema.type === "number") {
      field = document.createElement("input");
      field.type = "number";
      field.step = schema.type === "integer" ? "1" : "any";
      if (schema.minimum !== undefined) field.min = schema.minimum;
      if (schema.maximum !== undefined) field.max = schema.maximum;
    } else if (/_text$|_notes$/.test(name) || /text/i.test(schema.description || "")) {
      field = document.createElement("textarea");
      field.rows = 3;
    } else {
      field = document.createElement("input");
      field.type = "text";
    }
    field.name = name;
    // A required boolean may legitimately be false; HTML required would force true.
    field.required = required && field.type !== "checkbox";
    if (schema.default !== undefined) {
      if (field.type === "checkbox") field.checked = schema.default;
      else field.value = schema.default;
    }
    label.append(field);
    if (schema.description) label.append(consoleNode("small", schema.description));
    form.append(label);
    fields.push({ name, schema, field });
  }
  if (tool.needs_confirmation) form.append(consoleNode("p", "This tool needs your confirmation before it runs.", "jobs-hint"));
  const submit = consoleNode("button", "RUN", "jobs-btn");
  submit.type = "submit";
  const result = consoleNode("pre");
  result.setAttribute("aria-live", "polite");
  form.append(submit, result);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submit.disabled) return;
    if (tool.needs_confirmation && !confirm(`Run ${tool.name}? This may use model tokens, contact an external service or open a browser.`)) return;
    const input = {};
    for (const { name, schema, field } of fields) {
      if (field.type === "checkbox") input[name] = field.checked;
      else if (field.value !== "") input[name] = ["integer", "number"].includes(schema.type) ? Number(field.value) : field.value;
    }
    submit.disabled = true;
    result.textContent = "Running…";
    try {
      const receipt = await readJson(await fetch(`/api/tools/${encodeURIComponent(tool.name)}/run`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input, confirmed: tool.needs_confirmation }),
      }));
      result.textContent = JSON.stringify(receipt, null, 2);
    } catch (error) { result.textContent = error.message; }
    finally { submit.disabled = false; }
  });
  return form;
}

async function loadConsoleTools() {
  const list = document.getElementById("console-tool-list");
  list.textContent = "Loading tools…";
  try {
    const { tools } = await readJson(await fetch("/api/tools"));
    list.replaceChildren();
    let group;
    for (const tool of tools.sort((a, b) => a.group.localeCompare(b.group) || a.name.localeCompare(b.name))) {
      if (group !== tool.group) {
        group = tool.group;
        list.append(consoleNode("h3", group));
      }
      const row = consoleNode("article", "", "console-card");
      row.dataset.tool = tool.name;
      row.append(consoleNode("h4", tool.name), consoleNode("p", tool.description));
      const actions = consoleNode("div", "", "console-actions");
      if (tool.panel) actions.append(consoleButton("OPEN", () => consoleOpenPanel(tool.panel)));
      const form = consoleRunForm(tool);
      form.id = `console-form-${tool.name}`;
      const run = consoleButton("RUN", () => {
        form.hidden = !form.hidden;
        run.setAttribute("aria-expanded", String(!form.hidden));
        if (!form.hidden) form.querySelector("input, textarea, select, button").focus();
      });
      run.setAttribute("aria-expanded", "false");
      run.setAttribute("aria-controls", form.id);
      actions.append(run);
      row.append(actions, form);
      list.append(row);
    }
    consoleToolsLoaded = true;
  } catch (error) {
    list.textContent = error.message;
    list.append(consoleButton("RETRY", loadConsoleTools));
  }
}

async function loadConsoleAgents() {
  const list = document.getElementById("console-thread-list");
  const repo = document.getElementById("console-repo");
  const text = document.getElementById("console-thread-text");
  const refresh = document.getElementById("console-agents-refresh");
  if (refresh.disabled) return;
  refresh.disabled = true;
  list.textContent = "Loading threads…";
  repo.textContent = "";
  text.textContent = "";
  ++consoleThreadRequest;
  try {
    const data = await readJson(await fetch("/api/agents"));
    repo.textContent = `${data.repo.branch} · ${data.repo.head} · ${data.repo.tree}\n${data.repo.subject}`;
    list.replaceChildren();
    if (!data.threads.length) list.textContent = "No session threads yet.";
    for (const thread of data.threads) {
      const card = consoleButton("", async () => {
        const request = ++consoleThreadRequest;
        text.textContent = "Loading thread…";
        try {
          const body = await readJson(await fetch(`/api/agents/${encodeURIComponent(thread.slug)}`));
          if (request === consoleThreadRequest) text.textContent = body.text;
        } catch (error) { if (request === consoleThreadRequest) text.textContent = error.message; }
      });
      card.className = "jobs-btn console-card console-thread";
      card.append(consoleNode("strong", thread.slug), consoleNode("span", `${thread.agent || "Unknown agent"} · ${thread.stamp || thread.modified}`));
      const badge = consoleNode("span", `Open for: ${thread.open_for || "not stated"}`, "console-badge");
      badge.dataset.openFor = thread.open_for || "nothing";
      card.append(badge, consoleNode("span", `Next: ${thread.next || "not stated"}`), consoleNode("span", `Suggested: ${thread.suggested || "not stated"}`));
      list.append(card);
    }
  } catch (error) { list.textContent = error.message; }
  finally { refresh.disabled = false; }
}

async function loadConsoleRuns() {
  const runs = document.getElementById("console-run-list");
  const jobs = document.getElementById("console-job-list");
  const refresh = document.getElementById("console-runs-refresh");
  if (refresh.disabled) return;
  refresh.disabled = true;
  runs.textContent = jobs.textContent = "Loading…";
  await Promise.all([
    (async () => {
      try {
        const data = await readJson(await fetch("/api/tools/runs"));
        runs.replaceChildren();
        if (!data.runs.length) runs.textContent = "No tool runs yet.";
        for (const run of data.runs) {
          const row = consoleNode("article", "", "console-card");
          const status = run.ok ? "done" : "failed";
          const heading = consoleNode("strong", `${run.tool} · `);
          heading.append(consoleNode("span", status, `status-badge status-${status}`));
          row.append(heading, consoleNode("p", run.started_at), consoleNode("pre", run.error || run.summary));
          runs.append(row);
        }
      } catch (error) { runs.textContent = error.message; }
    })(),
    (async () => {
      try {
        const data = await readJson(await fetch("/api/jobs"));
        jobs.replaceChildren();
        if (!data.jobs.length) jobs.textContent = "No background jobs yet.";
        for (const job of data.jobs) {
          const row = consoleNode("article", "", "console-card");
          const heading = consoleNode("strong", `#${job.id} ${job.kind} · `);
          heading.append(consoleNode("span", job.status, `status-badge status-${job.status}`));
          row.append(heading, consoleNode("p", `Created: ${job.created_at}\nFinished: ${job.finished_at || "pending"}`));
          if (job.error) row.append(consoleNode("pre", job.error));
          jobs.append(row);
        }
      } catch (error) { jobs.textContent = error.message; }
    })(),
  ]);
  refresh.disabled = false;
}
let consolePrivacyRequest = 0;

function notesLabelRow(file, suggestion) {
  const row = consoleNode("article", "", "console-card");
  row.append(consoleNode("strong", file.category));
  row.append(consoleNode("p", `${file.notes} notes · ${file.bytes} bytes`));
  const columns = consoleNode("div", "", "privacy-label-columns");
  columns.append(consoleNode("p", `Current label: T${file.tier}${file.classes.length ? " / " + file.classes.join(", ") : ""}`));
  const suggested = consoleNode("div");
  suggested.append(consoleNode("strong", "Suggested"));
  if (!suggestion) {
    suggested.append(consoleNode("p", "No suggestion yet."));
  } else {
    suggested.append(consoleNode("p", suggestion.error ? `Could not label: ${suggestion.error}` :
      suggestion.suggested_classes.join(", ") || "T1 ordinary (no sensitive classes)"));
    suggested.append(consoleNode("p", [
      ...suggestion.added.map((name) => `+ ${name}`), ...suggestion.removed.map((name) => `− ${name}`),
    ].join(" · ")));
    const stale = suggestion.stale || suggestion.suggestion_sha256 !== file.sha256;
    if (stale) suggested.append(consoleNode("p", "Notes changed; suggest again.", "privacy-stale"));
    const apply = consoleNode("button", "Apply", "jobs-btn");
    apply.type = "button";
    apply.disabled = stale || Boolean(suggestion.error);
    apply.addEventListener("click", async () => {
      apply.disabled = true;
      try {
        const response = await fetch(`/api/notes/labels/${encodeURIComponent(file.category)}`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tier: suggestion.suggested_classes.length ? 2 : 1,
            classes: suggestion.suggested_classes, sha256: file.sha256 }),
        });
        await readJson(response);
        await loadConsolePrivacy();
      } catch (error) {
        suggested.append(consoleNode("p", error.message, "privacy-stale"));
      } finally { apply.disabled = stale || Boolean(suggestion.error); }
    });
    suggested.append(apply);
  }
  columns.append(suggested);
  row.append(columns);
  if (file.stale) row.append(consoleNode("span", "stale · review needed", "privacy-stale"));
  if (file.reviewed_at) row.append(consoleNode("p", `Last reviewed: ${file.reviewed_at}`, "console-badge"));

  const form = consoleNode("form", "", "console-run-form privacy-label-form");
  const tierLabel = consoleNode("label", "Tier", "jobs-field");
  const tier = consoleNode("select");
  for (const [value, title] of [[1, "T1 ordinary"], [2, "T2 sensitive"]]) {
    const option = consoleNode("option", title);
    option.value = String(value);
    tier.append(option);
  }
  tier.value = file.tier === 2 ? "2" : "1";
  tierLabel.append(tier);
  const classes = consoleNode("fieldset", "", "privacy-classes");
  classes.append(consoleNode("legend", "Sensitive classes (choose at least one)"));
  const choices = [];
  for (const name of ["conversation", "job_search", "third_party", "health", "ledger", "intake", "activity", "busy"]) {
    const label = consoleNode("label");
    const checkbox = consoleNode("input");
    checkbox.type = "checkbox";
    checkbox.value = name;
    checkbox.checked = file.classes.includes(name);
    label.append(checkbox, consoleNode("span", name));
    classes.append(label);
    choices.push(checkbox);
  }
  function updateClasses() {
    classes.hidden = tier.value !== "2";
    classes.disabled = classes.hidden;
  }
  tier.addEventListener("change", updateClasses);
  updateClasses();
  const save = consoleNode("button", "SAVE LABEL", "jobs-btn");
  save.type = "submit";
  const status = consoleNode("p", "", "console-badge");
  status.setAttribute("role", "status");
  form.append(tierLabel, classes, save, status);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const selected = tier.value === "2" ? choices.filter((c) => c.checked).map((c) => c.value) : [];
    if (tier.value === "2" && !selected.length) {
      status.textContent = "Choose at least one sensitive class.";
      return;
    }
    save.disabled = true;
    status.textContent = "Saving…";
    try {
      const response = await fetch(`/api/notes/labels/${encodeURIComponent(file.category)}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tier: Number(tier.value), classes: selected, sha256: file.sha256 }),
      });
      if (response.status === 409) {
        await loadConsolePrivacy();
        document.getElementById("console-privacy-files").prepend(
          consoleNode("p", "Notes changed. Review the current file before saving its label.", "privacy-stale"),
        );
        return;
      }
      await readJson(response);
      await loadConsolePrivacy();
    } catch (error) {
      status.textContent = error.message;
    } finally {
      save.disabled = false;
    }
  });
  row.append(form);
  return row;
}

async function loadConsolePrivacy() {
  const request = ++consolePrivacyRequest;
  const files = document.getElementById("console-privacy-files");
  const summary = document.getElementById("console-privacy-report");
  files.textContent = "Loading labels…";
  summary.textContent = "Loading report…";
  await Promise.all([
    (async () => {
      try {
        const [body, shadow] = await Promise.all([
          fetch("/api/notes/labels").then(readJson), fetch("/api/notes/shadow").then(readJson),
        ]);
        if (request !== consolePrivacyRequest) return;
        const suggestions = new Map(shadow.rows.map((row) => [row.category, row]));
        files.replaceChildren(...body.files.map((file) => notesLabelRow(file, suggestions.get(file.category))));
        if (!body.files.length) files.textContent = "No saved notes files.";
      } catch (error) {
        if (request === consolePrivacyRequest) files.textContent = error.message;
      }
    })(),
    (async () => {
      try {
        const body = await readJson(await fetch("/api/outbound/report?days=30"));
        if (request !== consolePrivacyRequest) return;
        summary.replaceChildren(consoleNode("p", `would refuse ${body.would_refuse} of ${body.requests} requests`));
        const counts = consoleNode("ul");
        for (const [name, count] of Object.entries(body.by_class)) counts.append(consoleNode("li", `${name}: ${count}`));
        summary.append(counts);
      } catch (error) {
        if (request === consolePrivacyRequest) summary.textContent = error.message;
      }
    })(),
  ]);
}

document.getElementById("console-privacy-suggest").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const status = document.getElementById("console-privacy-suggest-status");
  button.disabled = true;
  status.textContent = "Queuing local suggestions…";
  try {
    const job = await readJson(await fetch("/api/notes/shadow/run", { method: "POST" }));
    status.textContent = `Local suggestions queued (#${job.job_id}). Current labels are unchanged.`;
    const stream = new EventSource(`/api/jobs/${job.job_id}/events`);
    stream.addEventListener("done", async () => {
      stream.close(); button.disabled = false;
      status.textContent = "Finished. Review each suggestion or error below; labels are unchanged.";
      await loadConsolePrivacy();
    });
    stream.addEventListener("error", () => {
      stream.close(); button.disabled = false;
      status.textContent = "Could not follow the job. Check CONSOLE > RUNS, then refresh labels.";
    });
  } catch (error) { status.textContent = error.message; button.disabled = false; }
});

document.getElementById("console-privacy-refresh").addEventListener("click", loadConsolePrivacy);
document.getElementById("console-agents-refresh").addEventListener("click", loadConsoleAgents);
document.getElementById("console-runs-refresh").addEventListener("click", loadConsoleRuns);

/* ---------------- feature blueprint and memory map ---------------- */
const mapPanel = document.getElementById("map-panel");
const mapToggle = document.getElementById("map-toggle");
const mapTabs = [...mapPanel.querySelectorAll("[data-map-tab]")];
let mapRequest = 0;
const memoryMap = document.getElementById("memory-map");
const mapSummary = document.getElementById("memory-map-summary");
const mapDetail = document.getElementById("memory-map-detail");

function closeMapPanel() {
  panelClosed("map");
  mapPanel.classList.remove("is-open", "is-wide");
  mapPanel.setAttribute("aria-hidden", "true");
  mapPanel.inert = true;
  mapToggle.classList.remove("is-active");
  mapToggle.setAttribute("aria-expanded", "false");
}
function selectMapTab(tab) {
  mapPanel.classList.toggle("is-wide", ["system", "overview"].includes(tab.dataset.mapTab));
  mapTabs.forEach((other) => {
    const active = other === tab;
    other.classList.toggle("is-active", active);
    other.setAttribute("aria-selected", String(active));
    other.tabIndex = active ? 0 : -1;
  });
  mapPanel.querySelectorAll("[data-map-tab-panel]").forEach((panel) => {
    panel.classList.toggle("is-active", panel.dataset.mapTabPanel === tab.dataset.mapTab);
  });
  if (tab.dataset.mapTab === "overview") loadAtlasOverview();
  if (tab.dataset.mapTab === "features") loadFeatureMap();
  if (tab.dataset.mapTab === "memory") loadMemoryMap();
  if (tab.dataset.mapTab === "system") loadSystemMap();
}
function openMapPanel() {
  panelOpened("map");
  closeJobsPanel(); closeToolsPanel(); closeSearchPanel(); closeFocusPanel(); closeConsolePanel();
  mapPanel.inert = false;
  mapPanel.classList.add("is-open");
  mapPanel.setAttribute("aria-hidden", "false");
  mapToggle.classList.add("is-active");
  mapToggle.setAttribute("aria-expanded", "true");
  const active = mapTabs.find((tab) => tab.classList.contains("is-active"));
  active.focus();
  selectMapTab(active);
}
mapToggle.addEventListener("click", () => {
  mapPanel.classList.contains("is-open") ? closeMapPanel() : PANEL_OPENERS.map();
});
document.getElementById("map-close").addEventListener("click", () => { closeMapPanel(); mapToggle.focus(); });

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && mapPanel.classList.contains("is-open")) {
    closeMapPanel(); mapToggle.focus();
  }
});
mapTabs.forEach((tab, index) => {
  tab.addEventListener("click", () => selectMapTab(tab));
  tab.addEventListener("keydown", (event) => {
    const offsets = { ArrowRight: 1, ArrowLeft: -1, Home: -index, End: mapTabs.length - 1 - index };
    if (!(event.key in offsets)) return;
    event.preventDefault();
    const next = mapTabs[(index + offsets[event.key] + mapTabs.length) % mapTabs.length];
    next.click(); next.focus();
  });
});

function mapSvg(tag, attrs, text = "") {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  node.textContent = text;
  return node;
}

function showFeatureSlices(feature) {
  const details = document.getElementById("map-slices");
  const heading = document.createElement("h3");
  heading.textContent = `${feature.name} · ${feature.percent}% · ${feature.status}`;
  const list = document.createElement("ul");
  for (const slice of feature.slices) {
    const item = document.createElement("li");
    item.textContent = `${slice.id}: ${slice.name} · ${slice.status}`;
    list.append(item);
  }
  details.replaceChildren(heading, list);
  details.focus();
}

async function loadFeatureMap() {
  const request = ++mapRequest;
  const svg = document.getElementById("feature-map");
  const summary = document.getElementById("map-summary");
  svg.replaceChildren();
  document.getElementById("map-slices").replaceChildren();
  summary.textContent = "Loading features…";
  try {
    const data = await readJson(await fetch("/api/features"));
    if (request !== mapRequest) return;
    summary.textContent = `${data.overall}% overall · Select a feature for its slices.`;
    svg.setAttribute("viewBox", `0 0 340 ${Math.max(1, data.features.length) * 88 + 8}`);
    const positions = new Map(data.features.map((feature, index) => [feature.id, 8 + index * 88]));
    // Draw dependency wires first so the feature cards remain in front.
    for (const feature of data.features) {
      for (const dependency of feature.depends_on) {
        const from = positions.get(dependency) + 34;
        const to = positions.get(feature.id) + 34;
        const wire = mapSvg("path", { d: `M 40 ${from} H 16 V ${to} H 40`, class: "map-wire" });
        wire.append(mapSvg("title", {}, `${feature.name} depends on ${dependency}`));
        svg.append(wire);
      }
    }
    for (const feature of data.features) {
      const node = mapSvg("g", {
        transform: `translate(40 ${positions.get(feature.id)})`,
        "data-feature": feature.id, "data-percent": feature.percent,
        role: "button", tabindex: "0", "aria-controls": "map-slices",
        "aria-label": `${feature.name}, ${feature.percent}%, ${feature.status}. Show slices`,
      });
      node.append(
        mapSvg("rect", { width: 292, height: 68, rx: 4, class: "map-card" }),
        mapSvg("text", { x: 12, y: 23 }, feature.name),
        mapSvg("text", { x: 12, y: 43, class: "map-status" }, `${feature.percent}% · ${feature.status}`),
        mapSvg("rect", { x: 12, y: 53, width: 268, height: 5, class: "map-track" }),
        mapSvg("rect", { x: 12, y: 53, width: 268 * feature.percent / 100, height: 5, class: "map-progress" }),
      );
      node.addEventListener("click", () => showFeatureSlices(feature));
      node.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        showFeatureSlices(feature);
      });
      svg.append(node);
    }
  } catch (error) {
    if (request !== mapRequest) return;
    summary.textContent = `${error.message} Close and reopen MAP to retry.`;
  }
}

let systemMapRequest = 0;
async function loadSystemMap() {
  const request = ++systemMapRequest;
  const svg = document.getElementById("system-map");
  const summary = document.getElementById("system-map-summary");
  const details = document.getElementById("system-map-detail");
  svg.replaceChildren(); details.replaceChildren();
  summary.textContent = "Loading system map…";
  try {
    const data = await readJson(await fetch("/api/system-map"));
    if (request !== systemMapRequest) return;
    const positions = new Map();
    const counts = data.lanes.map((lane, column) => {
      const entries = data.nodes.filter((node) => node.lane === lane);
      svg.append(mapSvg("text", { x: column * 280 + 16, y: 24 }, lane.toUpperCase()));
      entries.forEach((node, row) => positions.set(node.id, { x: column * 280 + 16, y: row * 80 + 42 }));
      return entries.length;
    });
    svg.setAttribute("viewBox", `0 0 1120 ${Math.max(1, ...counts) * 80 + 50}`);
    for (const edge of data.edges) {
      const from = positions.get(edge.from), to = positions.get(edge.to);
      const rightward = from.x < to.x;
      const x1 = from.x + (rightward ? 244 : 0), x2 = to.x + (rightward ? 0 : 244);
      const wire = mapSvg("path", {
        d: `M ${x1} ${from.y + 30} C ${(x1 + x2) / 2} ${from.y + 30}, ${(x1 + x2) / 2} ${to.y + 30}, ${x2} ${to.y + 30}`,
        class: "map-wire",
      });
      wire.append(mapSvg("title", {}, `${edge.from} → ${edge.to}`));
      svg.append(wire);
    }
    for (const item of data.nodes) {
      const pos = positions.get(item.id);
      const node = mapSvg("g", {
        transform: `translate(${pos.x} ${pos.y})`, "data-node": item.id, "data-state": item.state,
        role: "button", tabindex: "0", "aria-controls": "system-map-detail",
        "aria-label": `${item.label}, ${item.state}. Show details`,
      });
      node.append(
        mapSvg("rect", { width: 244, height: 62, rx: 4, class: "map-card" }),
        mapSvg("text", { x: 10, y: 24 }, item.label.length > 26 ? `${item.label.slice(0, 25)}…` : item.label),
        mapSvg("text", { x: 10, y: 46, class: "map-status" }, item.state),
        mapSvg("title", {}, item.label),
      );
      const show = () => {
        const heading = document.createElement("h3");
        heading.textContent = `${item.label} · ${item.state}`;
        details.replaceChildren(heading);
        for (const text of [item.note || "No note recorded.", `Evidence: ${item.evidence || "Not recorded"}`, `Verified on: ${item.verified_on || "Not recorded"}`]) {
          const p = document.createElement("p"); p.textContent = text; details.append(p);
        }
        details.focus();
      };
      node.addEventListener("click", show);
      node.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault(); show();
      });
      svg.append(node);
    }
    summary.textContent = `${data.nodes.length} nodes · ${data.edges.length} connections · Select a node for evidence.`;
  } catch (error) {
    if (request === systemMapRequest) summary.textContent = `${error.message} Use Refresh to retry.`;
  }
}
document.getElementById("system-map-refresh").addEventListener("click", loadSystemMap);

function memoryMapNode(tag, attributes, text = "") {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attributes).forEach(([name, value]) => node.setAttribute(name, value));
  node.textContent = text;
  return node;
}
function drawMemoryMap(data) {
  memoryMap.replaceChildren();
  const rooms = new Map(data.rooms.map((room, i) => [room.name, { ...room, x: 90, y: 60 + i * 120 }]));
  const threads = new Map(data.thread_nodes.map((thread, i) => [thread.name, { ...thread, x: 280, y: 60 + i * 60 }]));
  memoryMap.setAttribute("viewBox", `0 0 360 ${Math.max(180, rooms.size * 120, threads.size * 60 + 60)}`);
  data.links.forEach((link) => {
    const room = rooms.get(link.room), thread = threads.get(link.thread);
    if (room && thread) memoryMap.append(memoryMapNode("line", {
      x1: room.x, y1: room.y, x2: thread.x, y2: thread.y, class: "memory-map-link",
    }));
  });
  function drawNode(item, radius, label, age, detail) {
    const node = memoryMapNode("g", { role: "button", tabindex: 0, "aria-label": label, "data-age": age });
    node.append(memoryMapNode("circle", { cx: item.x, cy: item.y, r: radius }));
    node.append(memoryMapNode("text", { x: item.x, y: item.y + radius + 16, "text-anchor": "middle" }, label));
    node.addEventListener("click", () => { mapDetail.textContent = detail; });
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); mapDetail.textContent = detail; }
    });
    memoryMap.append(node);
  }
  const largest = Math.max(1, ...data.rooms.map((room) => room.count));
  rooms.forEach((room) => drawNode(room, 12 + 28 * Math.sqrt(room.count / largest), room.name,
    room.age_days === null ? "unknown" : room.age_days >= 30 ? "stale" : "fresh",
    `${room.count} notes · Last updated: ${room.last_date ?? "unknown"} · Age: ${room.age_days ?? "unknown"} days · ${data.links.filter((link) => link.room === room.name).length} linked threads`));
  let index = 0;
  threads.forEach((thread) => drawNode(thread, 7, `Thread ${++index}`, "thread",
    `Last updated: ${thread.last_date} · ${data.links.filter((link) => link.thread === thread.name).length} linked rooms`));
}
let memoryMapRequest = 0;
async function loadMemoryMap() {
  const request = ++memoryMapRequest;
  mapSummary.textContent = "Loading memory map…";
  mapDetail.textContent = "";
  memoryMap.replaceChildren();
  try {
    const data = await readJson(await fetch("/api/memory/map"));
    if (request !== memoryMapRequest) return;
    drawMemoryMap(data);
    const assignments = Object.entries(data.assignments).map(([status, count]) => `${status}: ${count}`).join(", ");
    mapSummary.textContent = `${data.rooms.length} rooms · ${data.threads.count} threads · Exchanges: ${data.exchanges ?? "not opened"} · Assignments: ${assignments || "0"}`;
    mapDetail.textContent = data.rooms.length || data.threads.count ? "Select a node for counts and dates." : "No memory notes or session threads yet.";
  } catch (error) {
    if (request === memoryMapRequest) mapSummary.textContent = `Could not load memory map: ${error.message}`;
  }
}
document.getElementById("memory-map-refresh").addEventListener("click", loadMemoryMap);


/* Room is read-only; device controls live in DEVICES. */
const roomToggle = document.getElementById("room-toggle");
const roomPanel = document.getElementById("room-panel");
const roomRefresh = document.getElementById("room-refresh");
const roomStatus = document.getElementById("room-status");
let roomCloud = null;
let roomBusy = false;

function roomPaint() {
  document.getElementById("room-humidity").textContent =
    `Humidity: ${roomCloud?.humidity == null ? "unavailable" : roomCloud.humidity + "%"} · Target: ${roomCloud?.target_humidity == null ? "unavailable" : roomCloud.target_humidity + "%"}`;
  roomRefresh.disabled = roomBusy;
}

function roomPaintHistory(history, target) {
  const svg = document.getElementById("room-history");
  const caption = document.getElementById("room-history-caption");
  svg.replaceChildren();
  svg.toggleAttribute("hidden", history.points.length === 0);
  if (!history.points.length) {
    caption.textContent = "No history yet. The sampler writes a point every five minutes.";
    return;
  }
  const summary = history.summary;
  caption.textContent = `24 h: min ${summary.min ?? "unavailable"}% · max ${summary.max ?? "unavailable"}% · ${summary.samples} samples · ${summary.unavailable} gaps`;
  const values = history.points.filter(p => p.quality === "ok" && Number.isFinite(p.value)).map(p => p.value);
  const targetValue = target.summary.latest;
  if (Number.isFinite(targetValue)) values.push(targetValue);
  const low = values.length ? Math.min(...values) - 5 : 0;
  const high = values.length ? Math.max(...values) + 5 : 100;
  const end = Date.now(), start = end - 24 * 60 * 60 * 1000;
  const x = slot => 4 + 352 * Math.max(0, Math.min(1, (Date.parse(slot) - start) / (end - start)));
  const y = value => 92 - 88 * (value - low) / (high - low);
  const append = (name, attrs) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    svg.append(node);
  };
  if (Number.isFinite(targetValue)) {
    append("line", { x1: 4, x2: 356, y1: y(targetValue), y2: y(targetValue), class: "room-history-target" });
  }
  let path = "", connected = false, previous = null;
  for (const point of history.points) {
    const timestamp = Date.parse(point.slot);
    if (point.quality !== "ok" || !Number.isFinite(point.value) || !Number.isFinite(timestamp)) {
      connected = false;
      continue;
    }
    if (previous !== null && timestamp - previous > 5 * 60 * 1000) connected = false;
    path += `${connected ? "L" : "M"}${x(point.slot)},${y(point.value)} `;
    append("circle", { cx: x(point.slot), cy: y(point.value), r: 2 });
    connected = true;
    previous = timestamp;
  }
  if (path) append("path", { d: path });
}

async function roomLoadHistory() {
  try {
    const [history, target] = await Promise.all(["humidity", "target_humidity"].map(async metric =>
      readJson(await fetch(`/api/room/history?metric=${metric}&hours=24`))));
    roomPaintHistory(history, target);
  } catch (error) {
    const svg = document.getElementById("room-history");
    svg.replaceChildren();
    svg.setAttribute("hidden", "");
    document.getElementById("room-history-caption").textContent = `History unavailable: ${error.message}`;
  }
}

async function roomRequest() {
  if (roomBusy) return;
  roomBusy = true;
  roomPaint();
  roomStatus.textContent = "Refreshing…";
  const historyRequest = roomLoadHistory();
  try {
    const res = await fetch("/api/humidifier");
    // readJson retains the server's error code; remember HTTP 404 for older servers too.
    const missing = res.status === 404;
    let data;
    try { data = await readJson(res); }
    catch (error) {
      if (missing) {
        roomCloud = null;
        error.message = "Humidifier is not configured on this server.";
      }
      throw error;
    }
    roomCloud = data.status;
    roomStatus.textContent = "Status refreshed.";
  } catch (error) {
    roomStatus.textContent = error.message;
  } finally {
    await historyRequest;
    roomBusy = false;
    roomPaint();
  }
}

function closeRoomPanel() {
  panelClosed("room");
  roomPanel.classList.remove("is-open");
  roomPanel.setAttribute("aria-hidden", "true");
  roomPanel.inert = true;
  roomToggle.classList.remove("is-active");
  roomToggle.setAttribute("aria-expanded", "false");
}
function openRoomPanel() {
  panelOpened("room");
  closeJobsPanel(); closeToolsPanel(); closeSearchPanel(); closeFocusPanel(); closeConsolePanel(); closeMapPanel();
  roomPanel.inert = false;
  roomPanel.classList.add("is-open");
  roomPanel.setAttribute("aria-hidden", "false");
  roomToggle.classList.add("is-active");
  roomToggle.setAttribute("aria-expanded", "true");
  document.getElementById("room-close").focus();
  roomRequest();
}
roomToggle.addEventListener("click", () => {
  roomPanel.classList.contains("is-open") ? closeRoomPanel() : PANEL_OPENERS.room();
});
document.getElementById("room-close").addEventListener("click", () => { closeRoomPanel(); roomToggle.focus(); });

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && roomPanel.classList.contains("is-open")) {
    closeRoomPanel(); roomToggle.focus();
  }
});
roomRefresh.addEventListener("click", () => roomRequest());


/* Device writes are deliberate taps; status refreshes never resend them. */
const devicesPanel = document.getElementById("devices-panel");
const devicesToggle = document.getElementById("devices-toggle");
const devicesStatus = document.getElementById("devices-status");
let devicesCards = [];
let devicesBusy = false;
const devicesPending = new Map();

function deviceValue(kind, state, key) {
  if (kind === "humidifier" && key === "night_light") return state.night_light?.on;
  if (kind === "humidifier" && key === "night_light_brightness") return state.night_light?.brightness;
  return state[key];
}
function deviceAccept(kind, state) {
  const pending = devicesPending.get(kind);
  if (!pending || !state || state.online === false) return;
  for (const [key, value] of Object.entries(pending.changes)) {
    if (deviceValue(kind, state, key) === value) delete pending.changes[key];
  }
  if (!Object.keys(pending.changes).length) devicesPending.delete(kind);
}
function paintDevices() {
  const list = document.getElementById("devices-cards");
  list.replaceChildren();
  document.getElementById("devices-refresh").disabled = devicesBusy;
  if (!devicesCards.length) list.append(consoleNode("p", "No devices configured.", "jobs-hint"));
  for (const card of devicesCards) {
    const node = consoleNode("article", "", "console-card device-card");
    const state = card.state || {};
    const pending = devicesPending.get(card.kind);
    node.append(consoleNode("h3", card.kind === "purifier" ? "Air purifier · Core200S" : card.kind === "bulb" ? "Kasa colour bulb · KL125" : "Humidifier"));
    if (pending) node.append(consoleNode("p", Date.now() - pending.at < 180000
      ? "Change sent; awaiting matching status. Refresh to check."
      : "Still unconfirmed. Refresh status before making another change.", "jobs-status"));
    if (card.available && card.fresh === false) node.append(consoleNode("p",
      `No live read yet; showing status from ${Math.round(card.age_s)} s ago. Refresh to check.`, "jobs-status"));
    if (!card.available) {
      node.append(consoleNode("p", card.error || "Unavailable", "jobs-status"));
      list.append(node); continue;
    }
    if (card.kind === "bulb") {
      paintBulbControls(node, state);
      list.append(node); continue;
    }
    if (card.kind === "purifier") {
      const timer = state.timer;
      node.append(consoleNode("p", `Filter life: ${state.filter_life ?? "unavailable"}${state.filter_life == null ? "" : "%"}`));
      node.append(consoleNode("p", timer ? `Timer: ${timer.remaining_seconds} seconds remaining (${timer.action})`
        : "Timer: no active timer reported", "jobs-hint"));
      node.append(consoleNode("p", "No air-quality sensor. Mode and fan changes turn the purifier on; fan selects manual mode.", "jobs-hint"));
    }
    const fields = card.kind === "purifier" ? [
      ["power", "Power", ["on", "off"]], ["mode", "Mode", ["manual", "sleep"]],
      ["fan_level", "Fan level", [1, 2, 3]], ["display", "Display", [true, false]],
      ["night_light", "Night light", ["on", "off"]], ["child_lock", "Child lock", [true, false]],
    ] : [["display", "Display", [true, false]], ["night_light", "Night light", [true, false]],
      ["night_light_brightness", "Night light brightness (turns light on)", null]];
    for (const [key, title, choices] of fields) {
      const label = consoleNode("label", title, "device-control");
      const value = pending && Object.hasOwn(pending.changes, key) ? pending.changes[key] : deviceValue(card.kind, state, key);
      const input = document.createElement(choices ? "select" : "input");
      input.setAttribute("aria-label", title);
      if (choices) {
        if (!choices.includes(value)) {
          const unknown = consoleNode("option", "Unknown"); unknown.value = ""; unknown.disabled = true;
          input.append(unknown);
        }
        choices.forEach((choice, i) => {
          const option = consoleNode("option", choice === true ? "On" : choice === false ? "Off" : String(choice));
          option.value = String(i); input.append(option);
        });
        input.value = choices.includes(value) ? String(choices.indexOf(value)) : "";
      } else {
        input.type = "range"; input.min = "40"; input.max = "100"; input.step = "1";
        input.value = String(value ?? 40);
        label.append(consoleNode("span", ` ${value ?? "unknown"}%`));
      }
      input.disabled = devicesBusy;
      input.addEventListener("change", () => deviceWrite(card.kind, { [key]: choices ? choices[Number(input.value)] : Number(input.value) }));
      label.append(input); node.append(label);
    }
    list.append(node);
  }
}
function paintBulbControls(node, state) {
  node.append(consoleNode("p", `Power: ${state.power} · Brightness: ${state.brightness}%`));
  node.append(consoleNode("p", state.color_temp != null ? `White: ${state.color_temp} K`
    : `Colour: hue ${state.hue ?? "unknown"}°, saturation ${state.saturation ?? "unknown"}%`));
  node.append(consoleNode("p", "Brightness, white temperature and colour turn the bulb on. Changes stay on your local network.", "jobs-hint"));
  for (const power of ["on", "off"]) {
    const button = consoleNode("button", power === "on" ? "Turn on" : "Turn off", "jobs-btn");
    button.disabled = devicesBusy;
    button.addEventListener("click", () => deviceWrite("bulb", { power })); node.append(button);
  }
  function number(title, min, max, value) {
    const label = consoleNode("label", title, "device-control");
    const input = document.createElement("input"); input.type = "number";
    input.min = String(min); input.max = String(max); input.step = "1"; input.value = String(value);
    input.setAttribute("aria-label", title); input.disabled = devicesBusy;
    label.append(input); node.append(label); return input;
  }
  function apply(title, fields, changes) {
    const button = consoleNode("button", title, "jobs-btn"); button.disabled = devicesBusy;
    button.addEventListener("click", () => {
      if (fields.every(input => input.value !== "" && input.reportValidity())) deviceWrite("bulb", changes());
    }); node.append(button);
  }
  const brightness = number("Brightness (%)", 1, 100, state.brightness || 1);
  apply("Set brightness", [brightness], () => ({ brightness: Number(brightness.value) }));
  const temperature = number("White temperature (K)", 2500, 6500, state.color_temp ?? 3000);
  apply("Set white", [temperature], () => ({ color_temp: Number(temperature.value) }));
  const hue = number("Hue (degrees)", 0, 360, state.hue ?? 0);
  const saturation = number("Saturation (%)", 0, 100, state.saturation ?? 100);
  apply("Set colour", [hue, saturation], () => ({ hue: Number(hue.value), saturation: Number(saturation.value) }));
}
async function loadDevices() {
  if (devicesBusy) return;
  devicesBusy = true; paintDevices(); devicesStatus.textContent = "Refreshing…";
  try {
    const data = await readJson(await fetch("/api/devices"));
    devicesCards = data.devices;
    devicesCards.forEach(card => { if (card.fresh !== false) deviceAccept(card.kind, card.state); });
    devicesStatus.textContent = "Status refreshed.";
  } catch (error) {
    devicesCards = [];
    devicesStatus.textContent = `Could not refresh: ${error.message}`;
  } finally { devicesBusy = false; paintDevices(); }
}
async function deviceWrite(kind, changes) {
  if (devicesBusy) return;
  devicesBusy = true; paintDevices(); devicesStatus.textContent = "Sending change…";
  function remember(applied) {
    if (!applied || !Object.keys(applied).length) return;
    const previous = { ...(devicesPending.get(kind)?.changes || {}) };
    if (kind === "bulb" && Object.hasOwn(applied, "hue")) delete previous.color_temp;
    if (kind === "bulb" && Object.hasOwn(applied, "color_temp")) { delete previous.hue; delete previous.saturation; }
    devicesPending.set(kind, { changes: { ...previous, ...applied }, at: Date.now() });
  }
  try {
    const data = await readJson(await fetch(`/api/devices/${kind}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(changes),
    }));
    remember(data.applied);
    const card = devicesCards.find(item => item.kind === kind);
    if (card) { card.state = data.status; card.available = true; card.error = null; }
    deviceAccept(kind, data.status);
    devicesStatus.textContent = data.confirmed ? "Change confirmed by status." : "Change accepted; refresh to check status.";
  } catch (error) {
    remember(error.details?.applied);
    devicesStatus.textContent = `${error.message} Refresh status before retrying.`;
  } finally { devicesBusy = false; paintDevices(); }
}
function closeDevicesPanel() {
  panelClosed("devices");
  devicesPanel.classList.remove("is-open"); devicesPanel.setAttribute("aria-hidden", "true"); devicesPanel.inert = true;
  devicesToggle.classList.remove("is-active"); devicesToggle.setAttribute("aria-expanded", "false");
}
function openDevicesPanel() {
  closeOtherPanels("devices"); panelOpened("devices");
  devicesPanel.inert = false; devicesPanel.classList.add("is-open"); devicesPanel.setAttribute("aria-hidden", "false");
  devicesToggle.classList.add("is-active"); devicesToggle.setAttribute("aria-expanded", "true");
  document.getElementById("devices-close").focus(); loadDevices();
}
devicesToggle.addEventListener("click", () => devicesPanel.classList.contains("is-open") ? closeDevicesPanel() : PANEL_OPENERS.devices());
document.getElementById("devices-close").addEventListener("click", () => { closeDevicesPanel(); devicesToggle.focus(); });
document.getElementById("devices-refresh").addEventListener("click", loadDevices);
document.getElementById("room-devices").addEventListener("click", event => { event.preventDefault(); PANEL_OPENERS.devices(); });
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && devicesPanel.classList.contains("is-open")) { closeDevicesPanel(); devicesToggle.focus(); }
});

// Pending actions stay outside the saved transcript: their state belongs to the server.
let actionsRequest = 0;
async function loadPendingActions() {
  const request = ++actionsRequest;
  const list = document.getElementById("pending-actions");
  try {
    const { actions } = await readJson(await fetch("/api/actions"));
    if (request !== actionsRequest) return;
    list.replaceChildren();
    for (const action of actions) {
      const card = consoleNode("article", "", "console-card");
      card.append(consoleNode("strong", action.tool));
      card.append(consoleNode("pre", JSON.stringify(action.arguments, null, 2)));
      const controls = consoleNode("div", "", "jobs-inline");
      for (const [decision, label] of [["approve", "Approve"], ["deny", "Deny"]]) {
        const button = consoleNode("button", label, "jobs-btn");
        button.type = "button";
        button.addEventListener("click", async () => {
          controls.querySelectorAll("button").forEach((b) => { b.disabled = true; });
          try {
            const { result } = await readJson(await fetch(`/api/actions/${encodeURIComponent(action.id)}/${decision}`, {
              method: "POST",
            }));
            let reply = decision === "deny" ? "Cancelled." : "Done.";
            if (result && typeof result === "object") {
              if (Object.hasOwn(result, "error")) reply = `That did not work: ${result.error}`;
              else if (Object.hasOwn(result, "note")) reply = `Done. ${result.note}`;
            }
            addLine("kyra", reply);
          } catch (error) {
            addLine("error", error.message);
          } finally {
            await loadPendingActions();
            controls.querySelectorAll("button").forEach((b) => { b.disabled = false; });
          }
        });
        controls.append(button);
      }
      card.append(controls);
      list.append(card);
    }
  } catch (error) {
    if (request === actionsRequest) list.textContent = `Could not load pending actions: ${error.message}`;
  }
}

/* Saved Team metadata enriches existing rows by key; it never adds work. */
function teamRun(snapshot, key) {
  const run = snapshot?.runs?.find(item => item.key === key);
  return run && /^[a-f0-9]{32}$/.test(run.run_id) && key === `team:${run.run_id}` ? run : null;
}
function appendTeamStatus(card, run) {
  card.classList.add("team-status-card");
  if (!run) {
    card.append(consoleNode("p", "Saved Team details unavailable. Use Refresh to try again.", "jobs-hint"));
    return;
  }
  const labels = {queued: "Getting ready", planning: "Plan requested", building: "Build requested",
    reviewing: "Review requested", stopping: "Stop requested; worker completion unconfirmed",
    needs_input: "Your input is needed", uncertain: "Needs a check on the Mac",
    completed: "Work complete", stopped: "Stopped", failed: "Something needs attention"};
  card.append(consoleNode("p", `Saved status: ${labels[run.status] || "Status unavailable"}`));
  const identity = consoleNode("p", `Team · run ${run.run_id.slice(0, 8)}`, "jobs-hint");
  identity.setAttribute("aria-label", `Team run ${run.run_id}`);
  card.append(identity);
  for (const label of [run.reason_label, run.controller_label, run.claim_label]) {
    if (label) card.append(consoleNode("p", label, "jobs-hint"));
  }
  if (typeof run.room_id === "string" && /^[a-f0-9]{32}$/.test(run.room_id)) {
    const link = consoleNode("a", "Open in Team", "jobs-btn team-status-link");
    link.href = `/team#${encodeURIComponent(run.room_id)}`;
    card.append(link);
  } else {
    card.append(consoleNode("p", "Room link unavailable.", "jobs-hint"));
  }
}
function savedTime(value) {
  const date = new Date(value * 1000);
  return Number.isFinite(value) && Number.isFinite(date.getTime()) ? date.toLocaleString() : "unknown";
}
function renderTeamNotices(name, snapshot) {
  let target = document.getElementById(`${name}-team-notices`);
  if (!target) {
    target = consoleNode("div", "", "team-status-notices");
    target.id = `${name}-team-notices`;
    document.getElementById(`${name}-status`).after(target);
  }
  target.replaceChildren();
  const seen = new Set();
  for (const notice of snapshot?.notices || []) {
    if (!seen.has(notice.code)) {
      seen.add(notice.code);
      target.append(consoleNode("p", notice.text, "team-status-notice"));
    }
  }
  if (snapshot?.runs?.length || seen.size) {
    target.append(consoleNode("p", `Saved Team snapshot · read ${savedTime(snapshot.read_at)}`, "jobs-hint"));
    target.append(consoleNode("p", `Source saves: Team ${savedTime(snapshot.team_saved_at)} · Controller ${savedTime(snapshot.controller_saved_at)} · Claims ${savedTime(snapshot.claims_saved_at)}`, "jobs-hint"));
    target.append(consoleNode("p", "Sources were read separately; this is not live worker status.", "jobs-hint"));
  }
  target.hidden = !target.childElementCount;
}
function markEarlierSnapshot(name, reason) {
  const status = document.getElementById(`${name}-status`);
  status.textContent = status.dataset.readAt
    ? `Earlier snapshot · ${reason} · read ${status.dataset.readAt}`
    : `No saved snapshot · ${reason}`;
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) return;
  attentionRequest++; progressRequest++;
  for (const name of ["attention", "progress"]) markEarlierSnapshot(name, "page was hidden; use Refresh");
});

/* ---------------- attention: five cards, explicit decisions ---------------- */
const attentionPanel = document.getElementById("attention-panel");
const attentionToggle = document.getElementById("attention-toggle");
const attentionStatus = document.getElementById("attention-status");
let attentionRequest = 0;
let reflectionRequest = 0;

function closeAttentionPanel() {
  if (attentionPanel.classList.contains("is-open")) {
    attentionRequest++;
    markEarlierSnapshot("attention", "panel closed; use Refresh");
  }
  panelClosed("attention");
  attentionPanel.classList.remove("is-open");
  attentionPanel.setAttribute("aria-hidden", "true");
  attentionPanel.inert = true;
  attentionToggle.classList.remove("is-active");
  attentionToggle.setAttribute("aria-expanded", "false");
}
function attentionAction(card) {
  if (card.source === "/team") return; // Team navigation needs a matched, validated room link.
  if (card.source === "/api/reflections") {
    document.getElementById("reflection-feed").scrollIntoView({ block: "nearest" });
    return;
  }
  closeAttentionPanel();
  if (card.source === "/api/reminders") {
    navigateWorkspace("reminders");
  } else if (card.source === "/api/actions") {
    PANEL_OPENERS.attention();
    const list = document.getElementById("pending-actions");
    list.tabIndex = -1;
    list.scrollIntoView({ block: "nearest" }); list.focus();
  } else if (["/api/outbound/report", "/api/tools/runs", "/api/jobs"].includes(card.source)) {
    consoleToggle.click();
    const name = card.source === "/api/outbound/report" ? "privacy" : "runs";
    const tab = consoleTabs.find((item) => item.dataset.consoleTab === name);
    tab.click(); tab.focus();
  } else {
    window.location.assign("/loop");
  }
}
function attentionAge(seconds) {
  if (seconds === null || !Number.isFinite(seconds)) return "Source age unknown";
  if (seconds < 60) return "Source less than a minute old";
  if (seconds < 3600) return `Source ${Math.floor(seconds / 60)} minutes old`;
  if (seconds < 86400) return `Source ${Math.floor(seconds / 3600)} hours old`;
  return `Source ${Math.floor(seconds / 86400)} days old`;
}
function attentionCard(card, snapshot) {
  const node = consoleNode("article", "", "console-card attention-card");
  node.dataset.severity = String(card.severity);
  const run = card.source === "/team" ? teamRun(snapshot, card.key) : null;
  node.append(consoleNode("h3", run?.title ?? card.title));
  if (card.source === "/team") appendTeamStatus(node, run);
  else {
    node.append(consoleNode("p", card.why_now));
    node.append(consoleNode("p", `${card.source} · ${attentionAge(card.source_age_s)}`, "jobs-hint"));
    if (card.due_at) node.append(consoleNode("p", `Due: ${new Date(card.due_at).toLocaleString()}`, "jobs-hint"));
    node.append(consoleButton(card.action, () => attentionAction(card)));
  }
  const form = consoleNode("form", "", "attention-snooze");
  const label = consoleNode("label", "Snooze until");
  const input = document.createElement("input");
  input.type = "datetime-local"; input.required = true;
  const later = new Date(Date.now() + 3600000);
  input.value = new Date(later.getTime() - later.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  label.append(input);
  const button = consoleNode("button", "Snooze", "jobs-btn");
  button.type = "submit";
  form.append(label, button);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const until = new Date(input.value);
    if (!Number.isFinite(until.getTime()) || until.getTime() <= Date.now()) {
      input.setCustomValidity("Choose a future time."); input.reportValidity(); return;
    }
    button.disabled = true;
    try {
      await readJson(await fetch("/api/attention/snooze", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: card.key, until: until.toISOString() }),
      }));
      await loadAttention();
    } catch (error) { attentionStatus.textContent = `Could not snooze: ${error.message}`; }
    finally { button.disabled = false; }
  });
  input.addEventListener("input", () => input.setCustomValidity(""));
  node.append(form);
  return node;
}
async function loadAttention() {
  loadReflections();
  const request = ++attentionRequest;
  markEarlierSnapshot("attention", "refreshing");
  try {
    const data = await readJson(await fetch("/api/attention"));
    if (request !== attentionRequest || document.hidden) return;
    attentionToggle.textContent = `ATTENTION ${data.critical}`;
    attentionToggle.setAttribute("aria-label", `Attention: ${data.critical} critical items`);
    const cards = document.getElementById("attention-cards");
    cards.replaceChildren(...data.cards.slice(0, 5).map(card => attentionCard(card, data.team)));
    renderTeamNotices("attention", data.team);
    attentionStatus.dataset.readAt = new Date().toLocaleString();
    attentionStatus.textContent = `${data.critical} critical · ${data.snoozed} snoozed` +
      (data.cards.length ? "" : " · No open cards in the sources checked.") + ` · read ${attentionStatus.dataset.readAt}`;
    const grouped = document.getElementById("attention-grouped");
    grouped.replaceChildren();
    const names = { warning: "warnings", time: "time-sensitive items", input: "questions for you" };
    for (const [kind, count] of Object.entries(data.grouped)) {
      grouped.append(consoleNode("p", `${count} more ${names[kind] || kind}`));
    }
    if (Object.keys(data.grouped).length) {
      grouped.append(consoleNode("p", "Review or snooze the cards above to see the next items.", "jobs-hint"));
    }
  } catch (error) {
    if (request !== attentionRequest) return;
    attentionToggle.textContent = "ATTENTION ?";
    attentionToggle.setAttribute("aria-label", "Attention unavailable");
    attentionStatus.textContent = `Attention unavailable: ${error.message}`;
    if (attentionStatus.dataset.readAt) markEarlierSnapshot("attention", "refresh failed; use Refresh");
  }
}
function openAttentionPanel() {
  panelOpened("attention");
  closeJobsPanel(); closeToolsPanel(); closeSearchPanel(); closeFocusPanel();
  closeConsolePanel(); closeMapPanel(); closeRoomPanel();
  attentionPanel.inert = false;
  attentionPanel.classList.add("is-open");
  attentionPanel.setAttribute("aria-hidden", "false");
  attentionToggle.classList.add("is-active");
  attentionToggle.setAttribute("aria-expanded", "true");
  document.getElementById("attention-close").focus();
  loadAttention();
  loadPendingActions();
}
attentionToggle.addEventListener("click", () => {
  attentionPanel.classList.contains("is-open") ? closeAttentionPanel() : PANEL_OPENERS.attention();
});
document.getElementById("attention-close").addEventListener("click", () => {
  closeAttentionPanel(); attentionToggle.focus();
});
document.getElementById("attention-refresh").addEventListener("click", loadAttention);

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && attentionPanel.classList.contains("is-open")) {
    closeAttentionPanel(); attentionToggle.focus();
  }
});
// Refresh explicitly or on opening, so a background refresh never replaces a snooze being edited.

/* ---------------- progress: stages and counts, never grades ---------------- */
const progressPanel = document.getElementById("progress-panel");
const progressToggle = document.getElementById("progress-toggle");
let progressRequest = 0;

function closeProgressPanel() {
  if (progressPanel.classList.contains("is-open")) {
    progressRequest++;
    markEarlierSnapshot("progress", "panel closed; use Refresh");
  }
  panelClosed("progress");
  progressPanel.classList.remove("is-open");
  progressPanel.setAttribute("aria-hidden", "true");
  progressPanel.inert = true;
  progressToggle.classList.remove("is-active");
  progressToggle.setAttribute("aria-expanded", "false");
}
function progressLead(lead, snapshot) {
  const card = consoleNode("article", "", "console-card progress-lead");
  if (lead.kind === "team") {
    const run = teamRun(snapshot, lead.key);
    card.append(consoleNode("h3", run?.title ?? lead.title));
    appendTeamStatus(card, run);
    return card;
  }
  card.append(consoleNode("h3", lead.title), consoleNode("p", `${lead.kind} · ${lead.step}`));
  const hasPercent = Number.isFinite(lead.percent);
  const label = hasPercent && lead.steps_total !== null
    ? `${lead.steps_done}/${lead.steps_total} steps complete (${lead.percent}%)`
    : hasPercent && lead.kind === "focus" ? `${lead.percent}% of planned time elapsed` : lead.step;
  card.append(consoleNode("p", label, "jobs-hint"));
  // Unknown scope gets an empty, unscaled track, never a made-up percentage.
  const bar = consoleNode("div", "", "progress-bar");
  if (hasPercent) {
    bar.setAttribute("role", "progressbar");
    bar.setAttribute("aria-label", `${lead.title}: ${label}`);
    bar.setAttribute("aria-valuemin", "0");
    bar.setAttribute("aria-valuemax", "100");
    bar.setAttribute("aria-valuenow", String(lead.percent));
    const fill = consoleNode("span", "", "progress-fill");
    fill.style.width = `${Math.max(0, Math.min(100, lead.percent))}%`;
    bar.append(fill);
  } else {
    bar.classList.add("progress-unknown");
    bar.setAttribute("aria-hidden", "true");
  }
  card.append(bar);
  if (lead.stalled_days !== null) {
    card.dataset.stalled = "true";
    card.append(consoleNode("p", `Stalled: ${lead.stalled_days} days in ${lead.step}.`, "progress-stalled"));
  }
  if (lead.next) card.append(consoleNode("p", `Next: ${lead.next}`));
  if (lead.since) card.append(consoleNode("p", `Since ${new Date(lead.since).toLocaleString()}`, "jobs-hint"));
  return card;
}
async function loadProgress() {
  const request = ++progressRequest;
  const status = document.getElementById("progress-status");
  status.textContent = "Reading progress…";
  markEarlierSnapshot("progress", "refreshing");
  try {
    const data = await readJson(await fetch("/api/progress"));
    if (request !== progressRequest || document.hidden) return;
    document.getElementById("progress-leads").replaceChildren(...data.leads.map(lead => progressLead(lead, data.team)));
    renderTeamNotices("progress", data.team);
    const counts = [
      [data.today.focus_minutes, "focus minutes"],
      [data.today.assignments_moved, "assignments moved"],
      [data.today.applications_touched, "application events"],
      [data.today.learning_reviews, "learning reviews due"],
    ];
    document.getElementById("progress-today").replaceChildren(...counts.map(([value, label]) => {
      const cell = consoleNode("div", "", "progress-count");
      cell.append(consoleNode("strong", String(value)), consoleNode("span", label));
      return cell;
    }));
    document.getElementById("progress-line").textContent = data.today.line;
    status.dataset.readAt = new Date().toLocaleString();
    status.textContent = (data.leads.length ? `${data.leads.length} saved leads` : "No current leads.") + ` · read ${status.dataset.readAt}`;
  } catch (error) {
    if (request !== progressRequest) return;
    status.textContent = `Progress unavailable: ${error.message}`;
    if (status.dataset.readAt) markEarlierSnapshot("progress", "refresh failed; use Refresh");
  }
}
function openProgressPanel() {
  panelOpened("progress");
  closeAttentionPanel(); closeJobsPanel(); closeToolsPanel(); closeSearchPanel();
  closeFocusPanel(); closeConsolePanel(); closeMapPanel(); closeRoomPanel();
  progressPanel.inert = false;
  progressPanel.classList.add("is-open");
  progressPanel.setAttribute("aria-hidden", "false");
  progressToggle.classList.add("is-active");
  progressToggle.setAttribute("aria-expanded", "true");
  document.getElementById("progress-close").focus();
  loadProgress();
}
progressToggle.addEventListener("click", () => {
  progressPanel.classList.contains("is-open") ? closeProgressPanel() : PANEL_OPENERS.progress();
});
document.getElementById("progress-close").addEventListener("click", () => {
  closeProgressPanel(); progressToggle.focus();
});
document.getElementById("progress-refresh").addEventListener("click", loadProgress);

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && progressPanel.classList.contains("is-open")) {
    closeProgressPanel(); progressToggle.focus();
  }
});

/* ---------------- learning: recall first, then reveal and review ---------------- */
const learningPanel = document.getElementById("learning-panel");
const learningToggle = document.getElementById("learning-toggle");
let learningTabRequest = 0;
let learningSummaryRequest = 0;

async function loadLearningSummary() {
  const request = ++learningSummaryRequest;
  try {
    const data = await readJson(await fetch("/api/learning/summary"));
    if (request !== learningSummaryRequest) return;
    const due = data.due_items + data.due_reels;
    learningToggle.textContent = `LEARNING ${due}`;
    learningToggle.setAttribute("aria-label", `Learning: ${due} reviews due`);
    document.getElementById("learning-streak").textContent = `${data.streak_days}-day streak · ` +
      (data.reviewed_today ? "Successful review today" : "No successful review recorded today");
    document.getElementById("learning-status").textContent = `${due} reviews due` +
      (data.next_review_at ? ` · Next scheduled: ${new Date(data.next_review_at).toLocaleString()}` : "");
  } catch (error) {
    if (request !== learningSummaryRequest) return;
    learningToggle.textContent = "LEARNING ?";
    document.getElementById("learning-streak").textContent = "";
    document.getElementById("learning-status").textContent = `Learning summary unavailable: ${error.message}`;
  }
}
function learningItemCard(item) {
  const card = consoleNode("article", "", "console-card learning-card");
  card.append(consoleNode("h3", item.topic));
  const answer = consoleNode("div", "", "learning-answer");
  answer.hidden = true;
  answer.append(consoleNode("p", item.key_takeaway));
  if (item.summary) answer.append(consoleNode("p", item.summary, "jobs-hint"));
  const actions = consoleNode("div", "", "learning-actions");
  const status = consoleNode("p", "", "jobs-hint");
  status.setAttribute("role", "status");
  for (const [label, remembered] of [["Remembered", true], ["Forgot", false]]) {
    const button = consoleNode("button", label, "jobs-btn");
    button.type = "button";
    button.addEventListener("click", async () => {
      const buttons = [...actions.querySelectorAll("button")];
      buttons.forEach(b => { b.disabled = true; });
      status.textContent = "Saving review…";
      try {
        const result = await readJson(await fetch(`/api/learning/${item.id}/review`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ remembered, expected_next_review_at: item.next_review_at }),
        }));
        const days = Math.max(0, Math.ceil((new Date(result.next_review_at).getTime() - Date.now()) / 86400000));
        status.textContent = `Saved; next in ${days} ${days === 1 ? "day" : "days"}.`;
        status.tabIndex = -1; status.focus();
        await loadLearningSummary();
      } catch (error) {
        status.textContent = `Review not confirmed: ${error.message}. Refresh before trying again.`;
        if (error.code === "stale_review") {
          await loadLearningTab();
          document.getElementById("learning-items").prepend(status);
        }
      }
    });
    actions.append(button);
  }
  answer.append(actions);
  const show = consoleNode("button", "Show", "jobs-btn");
  show.type = "button";
  show.setAttribute("aria-expanded", "false");
  show.addEventListener("click", () => {
    answer.hidden = !answer.hidden;
    show.textContent = answer.hidden ? "Show" : "Hide";
    show.setAttribute("aria-expanded", String(!answer.hidden));
  });
  card.append(show, answer, status);
  return card;
}
function learningReelCard(reel) {
  const card = consoleNode("article", "", "console-card learning-card");
  card.append(consoleNode("h3", reel.learning_objective), consoleNode("p", reel.source_title, "jobs-hint"));
  let url;
  try { url = new URL(reel.embed_url); } catch { /* Malformed sources are never embedded. */ }
  if (url && url.protocol === "https:" && url.hostname === "www.youtube-nocookie.com" &&
      /^\/embed\/[A-Za-z0-9_-]{11}$/.test(url.pathname) && !url.username && !url.password && !url.port) {
    const frame = document.createElement("iframe");

    frame.title = reel.source_title || reel.learning_objective;
    frame.loading = "lazy";
    frame.referrerPolicy = "strict-origin-when-cross-origin";
    frame.allow = "encrypted-media; picture-in-picture; fullscreen";
    frame.setAttribute("sandbox", "allow-scripts allow-same-origin allow-presentation");
    const play = consoleNode("button", "Play clip", "jobs-btn");
    play.type = "button";
    play.addEventListener("click", () => { frame.src = url.href; play.replaceWith(frame); }, { once: true });
    card.append(play);
  } else if (url && url.protocol === "https:" && !url.username && !url.password) {
    const link = consoleNode("a", "Open source", "jobs-hint");
    link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer";
    card.append(link);
  } else {
    card.append(consoleNode("p", "Clip unavailable.", "jobs-hint"));
  }
  card.append(consoleNode("p", reel.question.stem));
  const options = consoleNode("div", "", "learning-options");
  const feedback = consoleNode("p", "", "learning-feedback");
  feedback.setAttribute("role", "status");
  for (const option of reel.question.options) {
    const button = consoleNode("button", option.text, "jobs-btn");
    button.type = "button";
    button.addEventListener("click", async () => {
      const buttons = [...options.querySelectorAll("button")];
      buttons.forEach(b => { b.disabled = true; });
      feedback.textContent = "Checking answer…";
      try {
        const result = await readJson(await fetch(`/api/reels/${reel.moment_id}/answer`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ kind: reel.kind, chosen: option.text }),
        }));
        feedback.textContent = result.message;
        if (!result.correct) buttons.forEach(b => { b.disabled = false; });
        await loadLearningSummary();
      } catch (error) {
        feedback.textContent = `Answer not confirmed: ${error.message}. Refresh before trying again.`;
      }
    });
    options.append(button);
  }
  card.append(options, feedback);
  return card;
}
async function loadLearningTab() {
  lessonView().load();
  const request = ++learningTabRequest;
  loadLearningSummary();
  await Promise.all([
    ["/api/learning/due", "learning-items", learningItemCard, "No items due now."],
    ["/api/reels/due", "learning-reels", learningReelCard, "No reel reviews due now."],
  ].map(async ([url, id, render, empty]) => {
    const list = document.getElementById(id);
    list.textContent = "Loading…";
    try {
      const data = await readJson(await fetch(url));
      if (request !== learningTabRequest) return;
      list.replaceChildren(...data.due.map(render));
      if (!data.due.length) list.textContent = empty;
    } catch (error) {
      if (request !== learningTabRequest) return;
      list.textContent = `Could not load: ${error.message}`;
    }
  }));
}
function closeLearningPanel() {
  panelClosed("learning");
  learningPanel.classList.remove("is-open");
  learningPanel.setAttribute("aria-hidden", "true");
  learningPanel.inert = true;
  learningToggle.classList.remove("is-active");
  learningToggle.setAttribute("aria-expanded", "false");
  ++learningTabRequest;
  // Removing frames stops a playing clip when the panel closes.
  document.getElementById("learning-reels").replaceChildren();
}
function openLearningPanel() {
  panelOpened("learning");
  closeAttentionPanel(); closeProgressPanel(); closeJobsPanel(); closeToolsPanel(); closeSearchPanel();
  closeFocusPanel(); closeConsolePanel(); closeMapPanel(); closeRoomPanel();
  learningPanel.inert = false;
  learningPanel.classList.add("is-open");
  learningPanel.setAttribute("aria-hidden", "false");
  learningToggle.classList.add("is-active");
  learningToggle.setAttribute("aria-expanded", "true");
  document.getElementById("learning-close").focus();
  loadLearningTab();
}
learningToggle.addEventListener("click", () => {
  learningPanel.classList.contains("is-open") ? closeLearningPanel() : PANEL_OPENERS.learning();
});
document.getElementById("learning-close").addEventListener("click", () => {
  closeLearningPanel(); learningToggle.focus();
});
document.getElementById("learning-tab-refresh").addEventListener("click", loadLearningTab);

document.addEventListener("keydown", event => {
  if (event.key === "Escape" && learningPanel.classList.contains("is-open")) {
    closeLearningPanel(); learningToggle.focus();
  }
});


/* Local reflections: explicit feedback and unsent drafts only. */
async function loadReflections() {
  const request = ++reflectionRequest;
  const status = document.getElementById("reflection-status");
  const list = document.getElementById("reflection-cards");
  try {
    const data = await readJson(await fetch("/api/reflections"));
    if (request !== reflectionRequest) return;
    list.replaceChildren();
    status.textContent = data.cards.length ? `${data.day} · suggestions to review` : "No reflections yet. The nightly run has not produced cards for today.";
    for (const card of data.cards.slice(0, 3)) {
      const node = consoleNode("article", "", "console-card reflection-card");
      node.append(consoleNode("h4", `${card.kind}: ${card.observation}`));
      node.append(consoleNode("p", card.suggestion));
      node.append(consoleNode("p", `Evidence: ${card.evidence}`, "jobs-hint"));
      node.append(consoleNode("p", `Check afterwards: ${card.outcome_check}`, "jobs-hint"));
      const feedback = consoleNode("p", card.vote ? `Your vote: ${card.vote.replaceAll("_", " ")}` : "", "jobs-hint");
      feedback.setAttribute("role", "status");
      const actions = consoleNode("div", "", "reflection-actions");
      for (const [value, label] of [["useful", "Useful"], ["not_useful", "Not useful"], ["do_it", "Do it: draft only"]]) {
        const button = consoleButton(label, async () => {
          const buttons = [...actions.querySelectorAll("button")];
          buttons.forEach(b => { b.disabled = true; });
          try {
            const result = await readJson(await fetch(`/api/reflections/${encodeURIComponent(card.key)}/vote`, {
              method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ vote: value }),
            }));
            feedback.textContent = result.assignment_id ? `LOOP draft ${result.assignment_id} saved. Nothing sent.` : `Saved: ${label}`;
          } catch (error) { feedback.textContent = `Could not save: ${error.message}`; }
          finally { buttons.forEach(b => { b.disabled = false; }); }
        });
        actions.append(button);
      }
      node.append(actions, feedback);
      list.append(node);
    }
  } catch (error) {
    if (request !== reflectionRequest) return;
    list.replaceChildren();
    status.textContent = `Reflections unavailable: ${error.message}`;
  }
}


/* Daily: existing sources only, fetched on opening or explicit refresh. */
const dailyPanel = document.getElementById("daily-panel");
const dailyToggle = document.getElementById("daily-toggle");
let dailyRequest = 0;
function dailyWhen(value, allDay = false) {
  if (allDay) return `${String(value).slice(0, 10)} · All day`;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Time unavailable" : date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}
function dailyCard(row, kind) {
  const card = consoleNode("article", "", "console-card daily-card");
  if (kind === "prepare") {
    card.append(consoleNode("h3", row.note), consoleNode("p", dailyWhen(row.when, row.all_day), "jobs-hint"));
  } else if (kind === "reminder") {
    card.append(consoleNode("h3", row.text), consoleNode("p", `Due ${dailyWhen(row.due_at)}`, "jobs-hint"));
  } else if (kind === "mail") {
    card.append(consoleNode("h3", row.subject), consoleNode("p", row.sender, "jobs-hint"));
  } else if (kind === "news") {
    let link;
    try {
      const url = new URL(row.link || row.url);
      if (["https:", "http:"].includes(url.protocol)) {
        link = consoleNode("a", row.title);
        link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer";
      }
    } catch { /* A malformed feed link stays plain text. */ }
    card.append(link || consoleNode("h3", row.title));
    if (row.source) card.append(consoleNode("p", row.source, "jobs-hint"));
  } else {
    card.append(consoleNode("h3", row.title));
    card.append(consoleNode("p", dailyWhen(row.start, row.all_day), "jobs-hint"));
    if (row.location) card.append(consoleNode("p", row.location));
    if (row.calendar) card.append(consoleNode("p", row.calendar, "jobs-hint"));
  }
  return card;
}
async function loadDaily() {
  const request = ++dailyRequest;
  const status = document.getElementById("daily-status");
  const sections = document.getElementById("daily-sections");
  status.textContent = "Reading daily sources…";
  sections.replaceChildren();
  try {
    const data = await readJson(await fetch("/api/daily"));
    if (request !== dailyRequest) return;
    status.textContent = `${data.date}` + (data.unavailable.length ? ` · Unavailable: ${data.unavailable.join(", ")}` : "");
    for (const [key, title, kind, source] of [
      ["prepare", "Prepare", "prepare", "calendar"],
      ["today", "Today", "event", "calendar"],
      ["tomorrow", "Tomorrow", "event", "calendar"],
      ["later", "Later · next seven days", "event", "calendar"],
      ["due_reminders", "Due reminders", "reminder", "reminders"],
      ["mail", "Mail needing a reply", "mail", "mail"],
    ]) {
      const section = consoleNode("section", "", "daily-section");
      section.append(consoleNode("h2", title));
      section.append(...data[key].map(row => dailyCard(row, kind)));
      if (!data[key].length) section.append(consoleNode("p", data.unavailable.includes(source) ? "Source unavailable." : "No items supplied by this source.", "jobs-hint"));
      sections.append(section);
    }
    const digest = consoleNode("section", "", "daily-section");
    digest.append(consoleNode("h2", "Latest digest"));
    digest.append(consoleNode("p", data.digest_day ? `Digest: ${data.digest_day}` : "No digest available.", "jobs-hint"));
    if (data.digest_path) digest.append(consoleNode("p", `Open locally: ${data.digest_path}`, "daily-path"));
    sections.append(digest);
    const headlines = consoleNode("section", "", "daily-section");
    headlines.append(consoleNode("h2", "Headlines"));
    headlines.append(...data.news.slice(0, 5).map(row => dailyCard(row, "news")));
    if (!data.news.length) headlines.append(consoleNode("p", data.unavailable.includes("news") ? "News unavailable." : "No headlines supplied.", "jobs-hint"));
    sections.append(headlines);
  } catch (error) {
    if (request !== dailyRequest) return;
    sections.replaceChildren();
    status.textContent = `Daily unavailable: ${error.message}`;
  }
}
function closeDailyPanel() {
  panelClosed("daily");
  ++dailyRequest;
  dailyPanel.classList.remove("is-open");
  dailyPanel.setAttribute("aria-hidden", "true"); dailyPanel.inert = true;
  dailyToggle.classList.remove("is-active"); dailyToggle.setAttribute("aria-expanded", "false");
}
function openDailyPanel() {
  panelOpened("daily");
  closeAttentionPanel(); closeProgressPanel(); closeLearningPanel(); closeJobsPanel(); closeToolsPanel();
  closeSearchPanel(); closeFocusPanel(); closeConsolePanel(); closeMapPanel(); closeRoomPanel();
  dailyPanel.inert = false; dailyPanel.classList.add("is-open"); dailyPanel.setAttribute("aria-hidden", "false");
  dailyToggle.classList.add("is-active"); dailyToggle.setAttribute("aria-expanded", "true");
  document.getElementById("daily-close").focus();
  loadDaily();
}
dailyToggle.addEventListener("click", () => {
  dailyPanel.classList.contains("is-open") ? closeDailyPanel() : PANEL_OPENERS.daily();
});
document.getElementById("daily-close").addEventListener("click", () => { closeDailyPanel(); dailyToggle.focus(); });
document.getElementById("daily-refresh").addEventListener("click", loadDaily);

document.addEventListener("keydown", event => {
  if (event.key === "Escape" && dailyPanel.classList.contains("is-open")) { closeDailyPanel(); dailyToggle.focus(); }
});

/* Health readings stay in this local panel, never the conversation. */
const myselfPanel = document.getElementById("myself-panel");
const myselfToggle = document.getElementById("myself-toggle");
let myselfRequest = 0;
function clearMyself() {
  for (const id of ["myself-last", "myself-sleep", "myself-heart", "myself-sleep-caption", "myself-heart-caption"]) {
    document.getElementById(id).replaceChildren();
  }
}
async function loadMyself() {
  const request = ++myselfRequest;
  const status = document.getElementById("myself-status");
  clearMyself(); status.textContent = "Reading local export…";
  try {
    const data = await readJson(await fetch("/api/myself", { cache: "no-store" }));
    if (request !== myselfRequest) return;
    status.textContent = data.note || `${data.export_day_source === "file_modified" ? "File modified" : "Exported"}: ${data.export_day} · Watch: ${data.watch || "not identified"}`;
    const last = data.nights.at(-1);
    document.getElementById("myself-last").textContent = last
      ? `${last.date}: ${(last.asleep_minutes / 60).toFixed(1)} h asleep · ${last.in_bed_minutes ? `${(last.in_bed_minutes / 60).toFixed(1)} h in bed` : "No in-bed interval recorded"} · Bed ${last.bed} · Wake ${last.wake}`
      : "No recorded nights in the last 30 days.";
    myselfLine("myself-sleep", "myself-sleep-caption", data.nights.map(n => ({ date: n.date, value: n.asleep_minutes / 60 })), "hours asleep");
    myselfLine("myself-heart", "myself-heart-caption", data.resting_hr.map(r => ({ date: r.date, value: r.bpm })), "bpm · daily median");
  } catch {
    if (request !== myselfRequest) return;
    clearMyself(); status.textContent = "Health export unavailable. Check the local export and try again.";
  }
}
function myselfLine(id, captionId, rows, unit) {
  const svg = document.getElementById(id), caption = document.getElementById(captionId);
  svg.replaceChildren();
  const now = new Date(), end = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
  const day = 86400000, start = end - 29 * day;
  const points = rows.map(row => ({ ...row, stamp: Date.parse(`${row.date}T00:00:00Z`) }))
    .filter(row => Number.isFinite(row.stamp) && Number.isFinite(row.value) && row.stamp >= start && row.stamp <= end)
    .sort((a, b) => a.stamp - b.stamp);
  svg.toggleAttribute("hidden", !points.length);
  if (!points.length) { caption.textContent = "No records in this 30-day window."; return; }
  const values = points.map(p => p.value), low = Math.max(0, Math.min(...values) - 1), high = Math.max(...values) + 1;
  const x = stamp => 36 + 310 * (stamp - start) / (end - start);
  const y = value => 112 - 100 * (value - low) / (high - low);
  function append(tag, attributes, text) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    if (text !== undefined) node.textContent = text;
    svg.append(node);
    return node;
  }
  append("text", { x: 2, y: 16 }, high.toFixed(1));
  append("text", { x: 2, y: 112 }, low.toFixed(1));
  append("text", { x: 36, y: 136 }, new Date(start).toISOString().slice(5, 10));
  append("text", { x: 346, y: 136, "text-anchor": "end" }, new Date(end).toISOString().slice(5, 10));
  let path = "", previous = null;
  for (const point of points) {
    path += `${previous !== null && point.stamp - previous === day ? "L" : "M"}${x(point.stamp)},${y(point.value)} `;
    const dot = append("circle", { cx: x(point.stamp), cy: y(point.value), r: 3 });
    const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
    title.textContent = `${point.date}: ${point.value.toFixed(1)} ${unit}`; dot.append(title);
    previous = point.stamp;
  }
  append("path", { d: path });
  caption.textContent = `${points.length} recorded days · ${unit}. Breaks in the line mean missing records.`;
}
function closeMyselfPanel() {
  panelClosed("myself");
  ++myselfRequest;
  clearMyself(); document.getElementById("myself-status").textContent = "";
  myselfPanel.classList.remove("is-open"); myselfPanel.inert = true;
  myselfPanel.setAttribute("aria-hidden", "true");
  myselfToggle.classList.remove("is-active"); myselfToggle.setAttribute("aria-expanded", "false");
}
function openMyselfPanel() {
  panelOpened("myself");
  closeAttentionPanel(); closeProgressPanel(); closeLearningPanel(); closeDailyPanel(); closeJobsPanel(); closeToolsPanel();
  closeSearchPanel(); closeFocusPanel(); closeConsolePanel(); closeMapPanel(); closeRoomPanel();
  myselfPanel.inert = false; myselfPanel.classList.add("is-open"); myselfPanel.setAttribute("aria-hidden", "false");
  myselfToggle.classList.add("is-active"); myselfToggle.setAttribute("aria-expanded", "true");
  document.getElementById("myself-close").focus(); loadMyself();
}
myselfToggle.addEventListener("click", () => {
  myselfPanel.classList.contains("is-open") ? closeMyselfPanel() : PANEL_OPENERS.myself();
});
document.getElementById("myself-close").addEventListener("click", () => { closeMyselfPanel(); myselfToggle.focus(); });
document.getElementById("myself-refresh").addEventListener("click", loadMyself);

document.addEventListener("keydown", event => {
  if (event.key === "Escape" && myselfPanel.classList.contains("is-open")) { closeMyselfPanel(); myselfToggle.focus(); }
});

/* Idea Lab renderers stay independent; this file owns navigation and HTTP errors. */
const ideaDeskRoot = document.getElementById("idea-desk");
const ideasPanel = document.getElementById("ideas-panel");
const ideasToggle = document.getElementById("ideas-toggle");
let ideasRenderer, lessonsRenderer, atlasRenderer;
const labRequest = async (url, options) => {
  const response = await fetch(url, options);
  try { return await readJson(response); }
  catch (error) { error.status = response.status; throw error; }
};
let prototypeIdea = null, prototypeReceipt = null;
const prototypeFields = ["problem", "hypothesis", "experiment"];
function showPrototype(seed) {
  PANEL_OPENERS.ideas();
  if (prototypeReceipt) {
    document.getElementById("prototype-seed").textContent = "Resolve the previous save first; its outcome is unknown. Retry uses the same receipt.";
  } else {
    prototypeIdea = seed?.id ? seed : null;
    prototypeFields.forEach(key => { document.getElementById(`prototype-${key}`).value = seed?.[key] || ""; });
    document.getElementById("prototype-seed").textContent = "Edit the experiment below. The isolated picker receives no Kyra data; copy your candidate names into it.";
  }
  document.getElementById("prototype-sample").scrollIntoView({behavior: "auto", block: "start"});
}
document.getElementById("prototype-save").addEventListener("click", async () => {
  const button = document.getElementById("prototype-save"), status = document.getElementById("prototype-seed");
  const fields = Object.fromEntries(prototypeFields.map(key => [key, document.getElementById(`prototype-${key}`).value.trim()]));
  if (!fields.hypothesis) { status.textContent = "Write a hypothesis first."; return; }
  if (!prototypeReceipt) prototypeReceipt = prototypeIdea ? {url:`/api/ideas/${encodeURIComponent(prototypeIdea.id)}`, method:"PATCH", body:fields} : {
    url:"/api/ideas", method:"POST", body:{...fields,title:"Experiment Picker idea",excerpt:"",concept_id:"weighted-decisions",request_id:crypto.randomUUID()}
  };
  button.disabled = true; prototypeFields.forEach(key => { document.getElementById(`prototype-${key}`).disabled=true; });
  let confirmed = false;
  try {
    prototypeIdea = await labRequest(prototypeReceipt.url, {method:prototypeReceipt.method,headers:{"Content-Type":"application/json"},body:JSON.stringify(prototypeReceipt.body)});
    prototypeReceipt = null; confirmed = true; status.textContent = "Saved locally. Try the picker, then update the saved idea with what happened."; ideasView().load();
  } catch(error) {
    if (error.status >=400 && error.status <500) prototypeReceipt=null;
    status.textContent = `Save not confirmed: ${error.message}. ${prototypeReceipt ? "Retry keeps the same receipt and content." : "Correct the fields and try again."}`;
  } finally {
    button.disabled=false; button.textContent = confirmed ? "Update experiment idea" : prototypeReceipt ? "Retry experiment save" : "Save experiment idea";
    prototypeFields.forEach(key => { document.getElementById(`prototype-${key}`).disabled=Boolean(prototypeReceipt); });
  }
});
function lessonView() {
  if (!lessonsRenderer) lessonsRenderer = window.KyraLessons.mount(document.getElementById("learning-lessons"), {
    request: labRequest, api: {lessons: "/api/lessons", learning: "/api/learning"}, onPrototype: showPrototype
  });
  return lessonsRenderer;
}
function ideasView() {
  if (!ideasRenderer) ideasRenderer = window.KyraIdeas.mount(ideaDeskRoot, {
    request: labRequest, api: {desk: "/api/ideas/desk", ideas: "/api/ideas"},
    onLearn: async (id) => { PANEL_OPENERS.learning(); await lessonView().open(id); document.querySelector("[data-lesson]")?.scrollIntoView({block:"start"}); },
    onPrototype: showPrototype
  });
  return ideasRenderer;
}
function openIdeasPanel() {
  closeOtherPanels("ideas"); panelOpened("ideas");
  ideasPanel.inert = false; ideasPanel.classList.add("is-open");
  ideasPanel.setAttribute("aria-hidden", "false"); ideasToggle.setAttribute("aria-expanded", "true");
  ideasToggle.classList.add("is-active"); document.getElementById("ideas-close").focus();
  ideasView().load();
}
function closeIdeasPanel() {
  panelClosed("ideas"); ideasPanel.classList.remove("is-open"); ideasPanel.inert = true;
  ideasPanel.setAttribute("aria-hidden", "true"); ideasToggle.setAttribute("aria-expanded", "false"); ideasToggle.classList.remove("is-active");
}
document.getElementById("idea-try").addEventListener("click", () => document.getElementById("prototype-sample").scrollIntoView({block:"start"}));
ideasToggle.addEventListener("click", () => ideasPanel.classList.contains("is-open") ? closeIdeasPanel() : PANEL_OPENERS.ideas());
document.getElementById("ideas-close").addEventListener("click", () => { closeIdeasPanel(); ideasToggle.focus(); });
document.addEventListener("keydown", event => { if (event.key === "Escape" && ideasPanel.classList.contains("is-open")) { closeIdeasPanel(); ideasToggle.focus(); } });
// Date labels belong to each data-desk-item. Live feeds without source dates remain undated.
ideaDeskRoot.addEventListener("focusin", event => { const card = event.target.closest("[data-desk-item]"); if (card) card.classList.add("was-focused"); });
function loadAtlasOverview() {
  if (!atlasRenderer) atlasRenderer = window.KyraAtlas.mount(document.getElementById("map-overview"), {
    request: labRequest, api: "/api/atlas", onOpen: node => { const name = node.panel.replace(/-panel$/, ""); if (PANEL_OPENERS[name]) PANEL_OPENERS[name](); }
  });
  atlasRenderer.load();
}
document.getElementById("map-overview").addEventListener("keydown", event => {
  if (event.key === "Escape" && event.target.closest("[data-atlas-node]")) document.getElementById("map-tab-overview").focus();
});

/* One URL per panel; no other panel is opened or fetched by hash routing. */
function closeOtherPanels(keep) {
  const closers = {
    ideas: closeIdeasPanel,
    devices: closeDevicesPanel,
    room: closeRoomPanel,
    focus: closeFocusPanel,
    daily: closeDailyPanel,
    learning: closeLearningPanel,
    jobs: closeJobsPanel,
    search: closeSearchPanel,
    progress: closeProgressPanel,
    attention: closeAttentionPanel,
    myself: closeMyselfPanel,
    map: closeMapPanel,
    console: closeConsolePanel,
    tools: closeToolsPanel,
  };
  for (const [name, close] of Object.entries(closers)) {
    if (name !== keep && document.getElementById(`${name}-panel`).classList.contains("is-open")) close();
  }
}
let routingPanel = false;
function panelOpened(name, route = name) {
  if (route === "tools") route = "reminders";
  if (!routingPanel && location.hash !== `#${route}`) history.pushState(null, "", `/workspace#${route}`);
  routingPanel = true;
  closeOtherPanels(name);
  routingPanel = false;
  const label = window.KyraNavigation.label(route);
  document.title = `KYRA · ${label}`;
  document.querySelector(".app-page").textContent = label;
  window.KyraNavigation.update(route);
}
function panelClosed(name) {
  if (routingPanel) return;
  const current = location.hash.slice(1);
  const panel = Object.hasOwn(toolRoutes, current) ? "tools" : current;
  if (panel === name) {
    // Defer until the close handler has finished changing visibility.
    queueMicrotask(() => { if (!document.querySelector(".jobs-panel.is-open")) navigateWorkspace("daily"); });
  }
}
function navigateWorkspace(name) {
  document.querySelector("#companion-navigation details").open = false;
  if (location.hash !== `#${name}`) history.pushState(null, "", `/workspace#${name}`);
  routePanel();
}
function routePanel() {
  let name = location.hash.slice(1);
  if (name === "tools") name = "reminders";
  if (!Object.hasOwn(PANEL_OPENERS, name) && !Object.hasOwn(toolRoutes, name)) name = "daily";
  if (location.hash !== `#${name}`) history.replaceState(null, "", `/workspace#${name}`);
  const panel = Object.hasOwn(toolRoutes, name) ? "tools" : name;
  try {
    routingPanel = true;
    closeOtherPanels(panel);
    routingPanel = false;
    if ((panel === "tools" && toolSection !== toolRoutes[name]) || !document.getElementById(`${panel}-panel`).classList.contains("is-open")) PANEL_OPENERS[panel]();
    else window.KyraNavigation.update(name);
  } catch (error) { routingPanel = false; reportScriptError(error, `Panel: ${name}`); }
}
document.addEventListener("click", event => {
  const link = event.target.closest('a[href^="/workspace#"]');
  if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  navigateWorkspace(link.hash.slice(1));
  if (link.hasAttribute("data-memory-map")) document.querySelector('[data-map-tab="memory"]').click();
}, true);
window.addEventListener("hashchange", routePanel);
window.addEventListener("popstate", routePanel);
routePanel();
