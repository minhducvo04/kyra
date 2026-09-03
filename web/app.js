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

async function send() {
  const text = input.value.trim();
  if (!text) return;
  addLine("user", text);
  input.value = "";
  setThinking(true);
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    const data = await res.json();
    const meta = data.actual_backend
      ? `${data.actual_backend}${data.path === "tool" ? " · tool" : ""}`
      : "";
    addLine("kyra", data.reply, meta);
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
  });
});

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
    if (draftResume.files[0]) form.append("resume", draftResume.files[0]);
    if (draftStyle.files[0]) form.append("style_sample", draftStyle.files[0]);

    const res = await fetch("/api/job/draft", { method: "POST", body: form });
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    const data = await res.json();

    if (data.warnings && data.warnings.length) {
      draftWarnings.textContent = data.warnings.join(" · ");
      draftWarnings.hidden = false;
    }
    draftText.textContent = data.draft;
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

/* -- tracker -- */

const trackerCompany = document.getElementById("tracker-company");
const trackerRole = document.getElementById("tracker-role");
const trackerLink = document.getElementById("tracker-link");
const trackerAddBtn = document.getElementById("tracker-add");
const trackerList = document.getElementById("tracker-list");

const STATUSES = ["applied", "interviewing", "offer", "rejected", "withdrawn"];

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
    const data = await res.json();
    autofillResult.innerHTML = "";
    if (data.error) {
      const p = document.createElement("p");
      p.style.color = "var(--danger)";
      p.textContent = data.missing_fields ? `${data.error}: ${data.missing_fields.join(", ")}` : data.error;
      autofillResult.appendChild(p);
    } else {
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
