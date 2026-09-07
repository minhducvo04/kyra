const transcript = document.getElementById("transcript");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");
const coreWrap = document.getElementById("core-wrap");
const coreLabel = document.getElementById("core-label");
const coreSub = document.getElementById("core-sub");
const statusText = document.getElementById("status-text");
const btnAuto = document.getElementById("btn-auto");
const btnClaude = document.getElementById("btn-claude");
const btnLocal = document.getElementById("btn-local");
const micBtn = document.getElementById("mic");
const micLabel = document.getElementById("mic-label");
const btnPtt = document.getElementById("btn-ptt");
const btnHandsfree = document.getElementById("btn-handsfree");
const replyAudio = document.getElementById("reply-audio");

const backendBtns = { auto: btnAuto, claude: btnClaude, local: btnLocal };

function addLine(who, text, meta) {
  const line = document.createElement("div");
  line.className = `line line-${who}`;
  const tag = document.createElement("span");
  tag.className = "line-tag";
  tag.textContent = who === "kyra" ? "KYRA" : who === "user" ? "YOU" : "SYSTEM";
  const body = document.createElement("span");
  body.className = "line-text";
  if (meta) {
    const metaSpan = document.createElement("span");
    metaSpan.className = "line-meta";
    metaSpan.textContent = meta;
    body.appendChild(metaSpan);
  }
  body.appendChild(document.createTextNode(text));
  line.append(tag, body);
  transcript.appendChild(line);
  transcript.scrollTop = transcript.scrollHeight;
  return line;
}

function setThinking(on) {
  coreWrap.classList.toggle("is-thinking", on);
  coreLabel.textContent = on ? "PROCESSING" : "STANDBY";
  coreSub.textContent = on ? "querying model…" : "awaiting input";
  sendBtn.disabled = on;
  input.disabled = on;
  micBtn.disabled = on;
}

function replyMeta(data) {
  return data.actual_backend ? `${data.actual_backend}${data.path === "tool" ? " · tool" : ""}` : "";
}

// Streams a turn from /api/chat/stream, painting tokens into one Kyra line as they
// arrive - time-to-first-token is what makes the conversation feel live. Resolves to
// the final ChatOut, or null if nothing at all came back (the caller then falls back
// to the whole-reply endpoint, so a proxy that buffers SSE can't break chat).
async function streamTurn(text) {
  const res = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: text }),
  });
  if (!res.ok || !res.body) throw new Error(`server returned ${res.status}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let line = null;
  let textNode = null;
  let final = null;
  const handle = (event, payload) => {
    if (event === "token") {
      if (!line) {
        line = addLine("kyra", "");
        textNode = line.querySelector(".line-text").lastChild;
        coreSub.textContent = "responding…";
      }
      textNode.textContent += payload;
      transcript.scrollTop = transcript.scrollHeight;
    } else if (event === "done") {
      final = payload;
      if (line) {
        textNode.textContent = payload.reply; // the authoritative text (e.g. a truncation marker)
        const meta = replyMeta(payload);
        if (meta) {
          const metaSpan = document.createElement("span");
          metaSpan.className = "line-meta";
          metaSpan.textContent = meta;
          line.querySelector(".line-text").prepend(metaSpan);
        }
      } else {
        addLine("kyra", payload.reply, replyMeta(payload));
      }
    } else if (event === "error") {
      throw new Error(payload);
    }
  };
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
      for (const l of block.split("\n")) {
        if (l.startsWith("event: ")) event = l.slice(7);
        else if (l.startsWith("data: ")) data += l.slice(6);
      }
      if (data) handle(event, JSON.parse(data));
    }
  }
  return final;
}

async function send() {
  const text = input.value.trim();
  if (!text) return;
  addLine("user", text);
  input.value = "";
  setThinking(true);
  try {
    let data = null;
    try {
      data = await streamTurn(text);
    } catch (err) {
      if (err.message.startsWith("server returned")) throw err;
      addLine("error", `stream failed — ${err.message}`);
    }
    if (!data) {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });
      if (!res.ok) throw new Error(`server returned ${res.status}`);
      data = await res.json();
      addLine("kyra", data.reply, replyMeta(data));
    }
  } catch (err) {
    addLine("error", `connection lost — ${err.message}`);
    statusText.textContent = "OFFLINE";
  } finally {
    setThinking(false);
    input.focus();
  }
}

sendBtn.addEventListener("click", send);
input.addEventListener("keydown", (e) => {
  // e.keyCode is deprecated but some automation/IME paths don't populate
  // e.key reliably - check both so a real Enter keypress never gets missed.
  if (e.key === "Enter" || e.keyCode === 13) send();
});

async function setBackend(name) {
  if (backendBtns[name].classList.contains("is-active")) return;
  Object.values(backendBtns).forEach((b) => b.classList.remove("is-loading"));
  backendBtns[name].classList.add("is-loading");
  coreSub.textContent = name === "local" ? "loading local model…" : name === "auto" ? "switching to auto…" : "switching to Claude…";
  try {
    const res = await fetch("/api/backend", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ backend: name }),
    });
    const data = await res.json();
    applyBackend(data.backend);
    addLine("system", `backend switched to ${data.backend.toUpperCase()}`);
  } catch (err) {
    addLine("error", `couldn't switch backend — ${err.message}`);
  } finally {
    backendBtns[name].classList.remove("is-loading");
    coreSub.textContent = "awaiting input";
  }
}

function applyBackend(name) {
  document.body.dataset.backend = name;
  document.documentElement.dataset.backend = name;
  Object.entries(backendBtns).forEach(([key, btn]) => btn.classList.toggle("is-active", key === name));
}

btnAuto.addEventListener("click", () => setBackend("auto"));
btnClaude.addEventListener("click", () => setBackend("claude"));
btnLocal.addEventListener("click", () => setBackend("local"));

/* ---------------- voice input ----------------
 * Two modes sharing one mic stream and one /api/voice upload path:
 *  - push-to-talk: click starts recording, click again stops and sends
 *  - hands-free: click starts a session; a simple energy-threshold VAD
 *    (no external library - just AnalyserNode) decides when an utterance
 *    starts/stops, same shape as VoiceActivityListener in listening.py,
 *    just re-implemented for the browser instead of a Silero model.
 * Sequential turn-taking is preserved on purpose, matching voice_chat.py:
 * the VAD loop and the mic itself are inert while a reply is being
 * fetched or spoken, so the mic never picks up Kyra's own TTS output.
 */

let voiceMode = "ptt"; // "ptt" | "handsfree"
let micStream = null;
let mediaRecorder = null;
let recordedChunks = [];
let isRecording = false;
let handsFreeActive = false;

let vadAudioCtx = null;
let vadAnalyser = null;
let vadRAF = null;
let speechRun = 0;
let silenceRun = 0;
const VAD_RMS_THRESHOLD = 0.02;
const VAD_START_FRAMES = 4; // ~4 animation frames of sustained level before we trust it's speech
const VAD_STOP_FRAMES = 35; // ~0.5s of quiet before we consider the utterance done

async function ensureMicStream() {
  if (!micStream) micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  return micStream;
}

function pickMimeType() {
  const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
  for (const c of candidates) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported(c)) return c;
  }
  return "";
}

const MIC_STATE_LABELS = {
  idle: "MIC OFF",
  recording: "● RECORDING",
  listening: "LISTENING…",
  processing: "THINKING…",
  speaking: "SPEAKING — click to stop",
};

function setMicState(state) {
  micBtn.dataset.state = state; // idle | recording | listening | processing
  micBtn.setAttribute("aria-pressed", state === "recording" || state === "listening" ? "true" : "false");
  micLabel.textContent = MIC_STATE_LABELS[state] || state;
}

function startOneRecording() {
  const stream = micStream;
  const mimeType = pickMimeType();
  recordedChunks = [];
  mediaRecorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
  mediaRecorder.ondataavailable = (e) => {
    if (e.data.size > 0) recordedChunks.push(e.data);
  };
  const stopped = new Promise((resolve) => {
    mediaRecorder.onstop = resolve;
  });
  mediaRecorder.start();
  isRecording = true;
  setMicState("recording");
  return stopped;
}

function stopOneRecording() {
  if (mediaRecorder && mediaRecorder.state !== "inactive") mediaRecorder.stop();
  isRecording = false;
}

function playReply(base64) {
  return new Promise((resolve) => {
    replyAudio.src = `data:audio/wav;base64,${base64}`;
    coreLabel.textContent = "SPEAKING";
    coreSub.textContent = "click mic to interrupt";
    setMicState("speaking");
    const done = () => {
      replyAudio.removeEventListener("ended", done);
      resolve();
    };
    replyAudio.addEventListener("ended", done);
    replyAudio.play().catch(done);
  });
}

function interruptPlayback() {
  if (!replyAudio.paused) {
    replyAudio.pause();
    replyAudio.currentTime = 0;
    addLine("system", "interrupted");
  }
}

async function sendVoiceBlob(blob) {
  if (blob.size === 0) {
    setMicState(handsFreeActive ? "listening" : "idle");
    return;
  }
  setMicState("processing");
  coreWrap.classList.add("is-thinking");
  coreLabel.textContent = "PROCESSING";
  coreSub.textContent = "transcribing…";
  input.disabled = true;
  sendBtn.disabled = true;
  try {
    const form = new FormData();
    form.append("audio", blob, "utterance.webm");
    const res = await fetch("/api/voice", { method: "POST", body: form });
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    const data = await res.json();
    if (!data.transcript) {
      coreSub.textContent = "didn't catch that";
      return;
    }
    addLine("user", data.transcript);
    const meta = data.actual_backend ? `${data.actual_backend}${data.path === "tool" ? " · tool" : ""}` : "";
    addLine("kyra", data.reply, meta);
    if (data.reply_audio_b64) await playReply(data.reply_audio_b64);
  } catch (err) {
    addLine("error", `voice turn failed — ${err.message}`);
  } finally {
    coreWrap.classList.remove("is-thinking");
    coreLabel.textContent = "STANDBY";
    coreSub.textContent = handsFreeActive ? "hands-free — just start talking" : "awaiting input";
    input.disabled = false;
    sendBtn.disabled = false;
    setMicState(handsFreeActive ? "listening" : "idle");
  }
}

async function handlePttClick() {
  if (isRecording) {
    stopOneRecording();
    return;
  }
  try {
    await ensureMicStream();
    const stopped = startOneRecording();
    stopped.then(() => {
      const blob = new Blob(recordedChunks, { type: mediaRecorder.mimeType || "audio/webm" });
      sendVoiceBlob(blob);
    });
  } catch (err) {
    addLine("error", `microphone error — ${err.message}`);
    setMicState("idle");
  }
}

function computeRms(analyser) {
  const data = new Uint8Array(analyser.fftSize);
  analyser.getByteTimeDomainData(data);
  let sumSq = 0;
  for (let i = 0; i < data.length; i++) {
    const v = (data[i] - 128) / 128;
    sumSq += v * v;
  }
  return Math.sqrt(sumSq / data.length);
}

function vadLoop() {
  if (!handsFreeActive) return;
  // Inert while she's speaking or a turn is being processed - the same
  // sequential discipline voice_chat.py uses, so the mic never hears her.
  if (!replyAudio.paused || micBtn.dataset.state === "processing") {
    vadRAF = requestAnimationFrame(vadLoop);
    return;
  }
  const rms = computeRms(vadAnalyser);
  if (!isRecording) {
    if (rms > VAD_RMS_THRESHOLD) {
      speechRun++;
      if (speechRun >= VAD_START_FRAMES) {
        speechRun = 0;
        beginHandsFreeUtterance();
      }
    } else {
      speechRun = 0;
    }
  } else if (rms < VAD_RMS_THRESHOLD) {
    silenceRun++;
    if (silenceRun >= VAD_STOP_FRAMES) {
      silenceRun = 0;
      stopOneRecording();
    }
  } else {
    silenceRun = 0;
  }
  vadRAF = requestAnimationFrame(vadLoop);
}

function beginHandsFreeUtterance() {
  const stopped = startOneRecording();
  stopped.then(() => {
    const blob = new Blob(recordedChunks, { type: mediaRecorder.mimeType || "audio/webm" });
    sendVoiceBlob(blob);
  });
}

async function startHandsFree() {
  try {
    const stream = await ensureMicStream();
    vadAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const source = vadAudioCtx.createMediaStreamSource(stream);
    vadAnalyser = vadAudioCtx.createAnalyser();
    vadAnalyser.fftSize = 1024;
    source.connect(vadAnalyser);
    handsFreeActive = true;
    speechRun = 0;
    silenceRun = 0;
    setMicState("listening");
    coreLabel.textContent = "LISTENING";
    coreSub.textContent = "hands-free — just start talking";
    vadLoop();
  } catch (err) {
    addLine("error", `microphone error — ${err.message}`);
  }
}

function stopHandsFree() {
  handsFreeActive = false;
  if (vadRAF) cancelAnimationFrame(vadRAF);
  if (isRecording) stopOneRecording();
  if (vadAudioCtx) {
    vadAudioCtx.close();
    vadAudioCtx = null;
  }
  setMicState("idle");
  coreLabel.textContent = "STANDBY";
  coreSub.textContent = "awaiting input";
}

micBtn.addEventListener("click", async () => {
  if (!replyAudio.paused) {
    interruptPlayback();
    return;
  }
  if (voiceMode === "ptt") {
    await handlePttClick();
  } else if (handsFreeActive) {
    stopHandsFree();
  } else {
    await startHandsFree();
  }
});

function setVoiceMode(mode) {
  if (mode === voiceMode) return;
  if (handsFreeActive) stopHandsFree();
  if (isRecording) stopOneRecording();
  voiceMode = mode;
  btnPtt.classList.toggle("is-active", mode === "ptt");
  btnHandsfree.classList.toggle("is-active", mode === "handsfree");
}

btnPtt.addEventListener("click", () => setVoiceMode("ptt"));
btnHandsfree.addEventListener("click", () => setVoiceMode("handsfree"));

(async function init() {
  try {
    const res = await fetch("/api/backend");
    const data = await res.json();
    applyBackend(data.backend);
  } catch {
    /* server not reachable yet on first paint - defaults stay as rendered */
  }
  input.focus();
  loadDraftDocPickers().catch(() => {}); // Draft tab is open by default - populate its doc picker up front
})();

/* ---------------- jobs panel ---------------- */

const jobsToggle = document.getElementById("jobs-toggle");
const jobsPanel = document.getElementById("jobs-panel");
const jobsClose = document.getElementById("jobs-close");

function openJobsPanel() {
  jobsPanel.classList.add("is-open");
  jobsPanel.setAttribute("aria-hidden", "false");
  jobsToggle.classList.add("is-active");
}
function closeJobsPanel() {
  jobsPanel.classList.remove("is-open");
  jobsPanel.setAttribute("aria-hidden", "true");
  jobsToggle.classList.remove("is-active");
}
jobsToggle.addEventListener("click", () => {
  jobsPanel.classList.contains("is-open") ? closeJobsPanel() : openJobsPanel();
});
jobsClose.addEventListener("click", closeJobsPanel);

document.querySelectorAll(".jobs-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".jobs-tab").forEach((t) => t.classList.toggle("is-active", t === tab));
    document.querySelectorAll(".jobs-tab-panel").forEach((p) => {
      p.classList.toggle("is-active", p.dataset.tabPanel === tab.dataset.tab);
    });
    if (tab.dataset.tab === "tracker") loadTrackerList();
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
      body: JSON.stringify({ urls, cover_letter: applyCoverLetter.value }),
    }));
    data.jobs.forEach(applyCard);
    applyUrls.value = "";
  } catch (err) {
    const p = document.createElement("p"); p.className = "jobs-warnings"; p.textContent = `couldn't queue — ${err.message}`;
    applyJobs.prepend(p);
  } finally {
    applyRunBtn.disabled = false;
    applyRunBtn.textContent = "Apply to all";
  }
});

/* -- tracker -- */

const trackerCompany = document.getElementById("tracker-company");
const trackerRole = document.getElementById("tracker-role");
const trackerLink = document.getElementById("tracker-link");
const trackerAddBtn = document.getElementById("tracker-add");
const trackerList = document.getElementById("tracker-list");

const STATUSES = ["targeting", "ready_to_submit", "needs_attention", "applied", "referral_pending", "interviewing", "offer", "rejected", "withdrawn"];

async function loadTrackerList() {
  trackerList.textContent = "loading…";
  try {
    const res = await fetch("/api/job/applications");
    const data = await res.json();
    renderTrackerList(data.applications || []);
  } catch (err) {
    trackerList.textContent = `couldn't load — ${err.message}`;
  }
}

function renderTrackerList(apps) {
  trackerList.innerHTML = "";
  if (apps.length === 0) {
    trackerList.textContent = "no applications tracked yet";
    return;
  }
  for (const app of apps) {
    const item = document.createElement("div");
    item.className = "jobs-tracker-item";

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

    const select = document.createElement("select");
    select.className = "jobs-tracker-status";
    for (const s of STATUSES) {
      const opt = document.createElement("option");
      opt.value = s;
      opt.textContent = s;
      opt.selected = s === app.status;
      select.appendChild(opt);
    }
    select.addEventListener("change", async () => {
      try {
        await fetch("/api/job/applications/status", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: app.id, status: select.value }),
        });
      } catch (err) {
        addLine("error", `status update failed — ${err.message}`);
      }
    });

    top.append(left, select);
    item.appendChild(top);

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

    trackerList.appendChild(item);
  }
}

trackerAddBtn.addEventListener("click", async () => {
  const company = trackerCompany.value.trim();
  const role = trackerRole.value.trim();
  if (!company || !role) return;
  trackerAddBtn.disabled = true;
  try {
    await fetch("/api/job/applications", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ company, role, link: trackerLink.value.trim() || null }),
    });
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

/* ---------------- tools panel ---------------- */

const toolsToggle = document.getElementById("tools-toggle");
const toolsPanel = document.getElementById("tools-panel");
const toolsClose = document.getElementById("tools-close");

function openToolsPanel() {
  toolsPanel.classList.add("is-open");
  toolsPanel.setAttribute("aria-hidden", "false");
  toolsToggle.classList.add("is-active");
  const activeTab = document.querySelector("#tools-panel .jobs-tab.is-active");
  const loaders = { reminders: loadReminders, learning: loadLearningDue };
  if (activeTab && loaders[activeTab.dataset.toolsTab]) loaders[activeTab.dataset.toolsTab]();
}
function closeToolsPanel() {
  toolsPanel.classList.remove("is-open");
  toolsPanel.setAttribute("aria-hidden", "true");
  toolsToggle.classList.remove("is-active");
}
toolsToggle.addEventListener("click", () => {
  toolsPanel.classList.contains("is-open") ? closeToolsPanel() : openToolsPanel();
});
toolsClose.addEventListener("click", closeToolsPanel);

document.querySelectorAll("#tools-panel .jobs-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll("#tools-panel .jobs-tab").forEach((t) => t.classList.toggle("is-active", t === tab));
    document.querySelectorAll("#tools-panel .jobs-tab-panel").forEach((p) => {
      p.classList.toggle("is-active", p.dataset.toolsTabPanel === tab.dataset.toolsTab);
    });
    const loaders = { reminders: loadReminders, learning: loadLearningDue };
    if (loaders[tab.dataset.toolsTab]) loaders[tab.dataset.toolsTab]();
  });
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

async function loadLearningDue() {
  const list = document.getElementById("learning-due-list");
  list.textContent = "loading…";
  try {
    const res = await fetch("/api/learning/due");
    const data = await res.json();
    renderLearningDue(data.due || []);
  } catch (err) {
    list.textContent = `couldn't load — ${err.message}`;
  }
}

function renderLearningDue(items) {
  const list = document.getElementById("learning-due-list");
  list.innerHTML = "";
  if (items.length === 0) {
    list.textContent = "nothing due right now";
    return;
  }
  for (const item of items) {
    const el = document.createElement("div");
    el.className = "jobs-tracker-item";
    const top = document.createElement("div");
    top.className = "jobs-tracker-item-top";
    const left = document.createElement("div");
    const topic = document.createElement("div");
    topic.className = "jobs-tracker-item-company";
    topic.textContent = item.topic;
    const takeaway = document.createElement("div");
    takeaway.className = "jobs-tracker-item-role";
    takeaway.textContent = item.key_takeaway;
    left.append(topic, takeaway);

    const actions = document.createElement("div");
    actions.className = "jobs-reminder-actions";
    const yesBtn = document.createElement("button");
    yesBtn.type = "button";
    yesBtn.textContent = "remembered";
    yesBtn.addEventListener("click", async () => {
      await fetch(`/api/learning/${item.id}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ remembered: true }),
      });
      loadLearningDue();
    });
    const noBtn = document.createElement("button");
    noBtn.type = "button";
    noBtn.textContent = "forgot";
    noBtn.addEventListener("click", async () => {
      await fetch(`/api/learning/${item.id}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ remembered: false }),
      });
      loadLearningDue();
    });
    actions.append(yesBtn, noBtn);

    top.append(left, actions);
    const summary = document.createElement("div");
    summary.className = "jobs-feed-item-summary";
    summary.textContent = item.summary;
    el.append(top, summary);
    list.appendChild(el);
  }
}

document.getElementById("learning-add").addEventListener("click", async () => {
  const topic = document.getElementById("learning-topic").value.trim();
  const summary = document.getElementById("learning-summary").value.trim();
  const key_takeaway = document.getElementById("learning-takeaway").value.trim();
  if (!topic || !summary || !key_takeaway) return;
  try {
    await fetch("/api/learning", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic, summary, key_takeaway }),
    });
    document.getElementById("learning-topic").value = "";
    document.getElementById("learning-summary").value = "";
    document.getElementById("learning-takeaway").value = "";
    loadLearningDue();
  } catch (err) {
    addLine("error", `couldn't save learning item — ${err.message}`);
  }
});

document.getElementById("learning-refresh").addEventListener("click", loadLearningDue);
