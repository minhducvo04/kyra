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

// --- Presence: one state for the whole interface -------------------------------
// A companion's first job is to show what it is doing (idle / listening / thinking /
// speaking / interrupted / failed - docs/plans/2026-09-07-human-interface.md, point 1).
// Before this, the core ring knew two states and the mic button five, set from a
// dozen scattered assignments. Everything now goes through setPresence(): it stamps
// <body data-presence>, so CSS owns the look, and plays a short earcon on the
// transitions a voice user cannot see (listen-start, reply-start, failure).
const PRESENCE_LABELS = {
  idle: ["STANDBY", "awaiting input"],
  listening: ["LISTENING", "go ahead"],
  thinking: ["PROCESSING", "querying model…"],
  speaking: ["SPEAKING", "click mic to interrupt"],
  interrupted: ["INTERRUPTED", "listening again"],
  failed: ["FAULT", "check the connection"],
};
let presence = "idle";
let audioCtx = null;

function earcon(kind) {
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

function setPresence(state, sub) {
  const changed = state !== presence;
  presence = state;
  document.body.dataset.presence = state;
  const [label, defaultSub] = PRESENCE_LABELS[state] || PRESENCE_LABELS.idle;
  coreLabel.textContent = label;
  coreSub.textContent = sub || defaultSub;
  coreWrap.classList.toggle("is-thinking", state === "thinking");
  if (changed && (state === "listening" || state === "speaking" || state === "failed")) earcon(state);
}

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
  // dataset.raw is the reply itself, kept apart from the meta/interrupted spans
  // that also live in .line-text - so saving and restoring never re-reads a badge
  // back in as part of what she said.
  line.dataset.who = who;
  line.dataset.raw = text;
  if (who === "kyra") line.appendChild(wrongButton(line));
  transcript.appendChild(line);
  transcript.scrollTop = transcript.scrollHeight;
  saveTranscript();
  return line;
}

// --- Marking a reply wrong ------------------------------------------------------
// The 2026 problem with a companion is correction, not recognition: the answer is
// slightly wrong and there is no way to say so. The mark saves a memory note, which
// is loaded in full into every system prompt - so she sees it on the next turn.
function wrongButton(line) {
  const btn = document.createElement("button");
  btn.className = "line-wrong-btn";
  btn.type = "button";
  btn.textContent = "\u2715";
  btn.title = "Tell her this reply was wrong";
  btn.setAttribute("aria-label", "Mark this reply as wrong");
  btn.addEventListener("click", () => markWrong(line));
  return btn;
}

function tagWrong(line) {
  line.dataset.wrong = "1";
  const btn = line.querySelector(".line-wrong-btn");
  if (btn) btn.remove();
  if (!line.querySelector(".line-wrong-tag")) {
    const tag = document.createElement("span");
    tag.className = "line-meta line-wrong-tag";
    tag.textContent = "marked wrong";
    line.querySelector(".line-text").appendChild(tag);
  }
}

async function markWrong(line) {
  try {
    await readJson(await fetch("/api/correction", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reply: line.dataset.raw || "" }),
    }));
    tagWrong(line);
    saveTranscript();
  } catch (err) {
    addLine("error", `couldn't save that correction — ${err.message}`);
  }
}

// --- Keeping the transcript across a reload -------------------------------------
// Only in this browser: the server's own history lives in the running process, so
// a restored transcript is a record of what was said, not proof she still has the
// context. It survives a page reload, which is what loses it in practice.
const TRANSCRIPT_KEY = "kyra.transcript";
const TRANSCRIPT_MAX = 200;

function saveTranscript() {
  try {
    const lines = [...transcript.querySelectorAll(".line")].slice(-TRANSCRIPT_MAX).map((l) => ({
      who: l.dataset.who || (l.className.match(/line-(\w+)/) || [])[1] || "system",
      text: l.dataset.raw ?? l.querySelector(".line-text").textContent,
      meta: (l.querySelector(".line-meta:not(.line-interrupted):not(.line-wrong-tag)") || {}).textContent || "",
      interrupted: !!l.querySelector(".line-interrupted"),
      wrong: l.dataset.wrong === "1",
    }));
    localStorage.setItem(TRANSCRIPT_KEY, JSON.stringify(lines));
  } catch (_) {
    // private window, storage disabled, quota - the conversation still works
  }
}

function restoreTranscript() {
  let saved;
  try {
    saved = JSON.parse(localStorage.getItem(TRANSCRIPT_KEY) || "null");
  } catch (_) {
    return;
  }
  if (!Array.isArray(saved) || !saved.length) return;
  transcript.replaceChildren();
  for (const l of saved) {
    const line = addLine(l.who, l.text, l.meta || undefined);
    if (l.interrupted) markInterrupted(line);
    if (l.wrong) tagWrong(line);
  }
}

function clearTranscript() {
  try {
    localStorage.removeItem(TRANSCRIPT_KEY);
  } catch (_) { /* nothing stored to remove */ }
  transcript.replaceChildren();
  addLine("system", "transcript cleared in this browser — she still remembers the conversation");
}

function setThinking(on) {
  setPresence(on ? "thinking" : "idle", on ? undefined : handsFreeActive ? "hands-free — just start talking" : undefined);
  // While a reply streams, SEND becomes STOP and the input stays live: stopping
  // is core conversation logic, not an edge case (human-interface plan, point 3),
  // and the natural way to stop is usually to just say the next thing.
  sendBtn.querySelector("span").textContent = on ? "STOP" : "SEND";
  sendBtn.classList.toggle("is-stop", on);
  sendBtn.setAttribute("aria-label", on ? "Stop generating" : "Send");
  sendBtn.disabled = false;
  input.disabled = false;
  micBtn.disabled = on;
}

// --- Cancelling a reply in flight ---------------------------------------------
// Aborting the fetch only stops the browser *listening*; the turn keeps running
// on the server, and it would record the whole reply into history and memory -
// so Kyra would remember saying something Duc never saw. /api/chat/cancel stops
// the generation itself, and is awaited so the next turn can't race it.
let inFlight = null; // { controller } while a streamed turn is running

async function cancelTurn() {
  if (!inFlight) return false;
  const turn = inFlight;
  turn.cancelled = true;
  turn.controller.abort();
  try {
    await fetch("/api/chat/cancel", { method: "POST" });
  } catch (_) {
    // the browser side is already stopped; a failed stop only costs tokens
  }
  return true;
}

function markInterrupted(line) {
  const body = line.querySelector(".line-text");
  // A stopped turn never gets the `done` event that would set dataset.raw, so the
  // partial has to be read back off the nodes the tokens were painted into -
  // otherwise a restored transcript shows the interruption with nothing before it.
  const painted = [...body.childNodes].filter((n) => n.nodeType === Node.TEXT_NODE).map((n) => n.textContent).join("");
  if (painted) line.dataset.raw = painted;
  const mark = document.createElement("span");
  mark.className = "line-meta line-interrupted";
  mark.textContent = "interrupted";
  body.appendChild(mark);
  saveTranscript();
}

function replyMeta(data) {
  return data.actual_backend ? `${data.actual_backend}${data.path === "tool" ? " · tool" : ""}` : "";
}

// Streams a turn from /api/chat/stream, painting tokens into one Kyra line as they
// arrive - time-to-first-token is what makes the conversation feel live. Resolves to
// the final ChatOut, or null if nothing at all came back (the caller then falls back
// to the whole-reply endpoint, so a proxy that buffers SSE can't break chat).
async function streamTurn(text) {
  const turn = { controller: new AbortController(), cancelled: false };
  inFlight = turn;
  const res = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: text }),
    signal: turn.controller.signal,
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
        setPresence("thinking", "responding…");
      }
      textNode.textContent += payload;
      transcript.scrollTop = transcript.scrollHeight;
    } else if (event === "done") {
      final = payload;
      if (line) {
        textNode.textContent = payload.reply; // the authoritative text (e.g. a truncation marker)
        line.dataset.raw = payload.reply;
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
  try {
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
  } catch (err) {
    // An abort is the user stopping her, not a failure: keep whatever she had
    // already said and label it, so the transcript matches what he heard.
    if (!turn.cancelled) throw err;
    if (line) markInterrupted(line);
    else addLine("system", "interrupted");
    return { cancelled: true };
  } finally {
    if (inFlight === turn) inFlight = null;
  }
  return final;
}

/* Speak her typed replies too, not just spoken turns. A voice turn already comes
   back as audio; a typed one had no way to be heard, which made the voice half of
   her only reachable through the microphone. Uses /api/speak and the same
   sentence queue, so the first sentence starts while the rest is still being
   synthesised. Off by default - she should not start talking unasked. */
const btnSpeak = document.getElementById("btn-speak");
let speakReplies = localStorage.getItem("kyra.speak") === "1";
// The synthesis stream outlives the audio it produced: stopping playback without
// stopping this keeps feeding the queue, and she starts talking again a moment
// after being cut off. Caught by toggling SPEAK off mid-reply.
let speakAbort = null;

function applySpeakToggle() {
  btnSpeak.classList.toggle("is-active", speakReplies);
  btnSpeak.setAttribute("aria-pressed", speakReplies ? "true" : "false");
}
applySpeakToggle();

btnSpeak.addEventListener("click", () => {
  speakReplies = !speakReplies;
  localStorage.setItem("kyra.speak", speakReplies ? "1" : "0");
  applySpeakToggle();
  if (!speakReplies) interruptPlayback();
});

async function speakReply(text) {
  if (!speakReplies || !text) return;
  speakAbort = new AbortController();
  const mine = speakAbort;
  try {
    const res = await fetch("/api/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
      signal: mine.signal,
    });
    if (!res.ok || !res.body) return;
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const chunk = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        let event = "message";
        let data = "";
        for (const l of chunk.split("\n")) {
          if (l.startsWith("event: ")) event = l.slice(7);
          else if (l.startsWith("data: ")) data += l.slice(6);
        }
        // Re-checked per chunk, not once at the top: she may have been stopped
        // since this stream started.
        if (event === "audio" && data && speakReplies && mine === speakAbort) {
          enqueueAudio(JSON.parse(data).b64);
        }
      }
    }
  } catch (_) {
    // Failing to speak is not failing the turn - the reply is already on screen.
  }
}

async function send() {
  const text = input.value.trim();
  if (!text) return;
  await cancelTurn(); // saying the next thing stops the current reply
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
    if (data && data.cancelled) {
      setThinking(false);
      setPresence("interrupted");
      input.focus();
      return;
    }
    if (data && data.reply) speakReply(data.reply);
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
    setPresence("failed");
    sendBtn.disabled = false;
    input.disabled = false;
    micBtn.disabled = false;
    input.focus();
    return;
  }
  setThinking(false);
  saveTranscript();
  focusSync();
  input.focus();
}

sendBtn.addEventListener("click", () => {
  if (inFlight) cancelTurn();
  else send();
});
document.addEventListener("keydown", (e) => {
  // Escape, not the space bar the plan first sketched: the input stays enabled
  // while she replies, so a space there is a space.
  if (e.key === "Escape" && inFlight) cancelTurn();
});
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
  micBtn.dataset.state = state; // idle | recording | listening | processing | speaking
  micBtn.setAttribute("aria-pressed", state === "recording" || state === "listening" ? "true" : "false");
  micLabel.textContent = MIC_STATE_LABELS[state] || state;
  if (state === "recording") setPresence("listening", "recording — click mic to stop");
  else if (state === "listening") setPresence("listening", "hands-free — just start talking");
  else if (state === "processing") setPresence("thinking", "transcribing…");
  else if (state === "speaking") setPresence("speaking");
  else if (presence !== "failed") setPresence("idle");
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
    setMicState("speaking");
    const done = () => {
      replyAudio.removeEventListener("ended", done);
      resolve();
    };
    replyAudio.addEventListener("ended", done);
    replyAudio.play().catch(done);
  });
}

/* Her reply arrives one synthesised sentence at a time, so playback is a queue
   rather than a single clip: the first sentence starts while the rest is still
   being written and spoken (docs/voice-latency.md - 0.48s to synthesise one
   sentence against 1.12s for a whole reply). Each chunk is its own complete WAV,
   which is why this can be an <audio> element and not MediaSource. */
let audioQueue = [];
let queuePlaying = false;

function speaking() {
  return queuePlaying || !replyAudio.paused;
}

function enqueueAudio(b64) {
  audioQueue.push(b64);
  if (!queuePlaying) playQueue();
}

async function playQueue() {
  queuePlaying = true;
  setMicState("speaking");
  while (audioQueue.length && queuePlaying) {
    const b64 = audioQueue.shift();
    await new Promise((resolve) => {
      replyAudio.src = `data:audio/wav;base64,${b64}`;
      const done = () => {
        replyAudio.removeEventListener("ended", done);
        resolve();
      };
      replyAudio.addEventListener("ended", done);
      replyAudio.play().catch(done);
    });
  }
  queuePlaying = false;
}

function interruptPlayback() {
  // Stop the synthesis too, not just the sound, or the queue refills behind it.
  if (speakAbort) {
    speakAbort.abort();
    speakAbort = null;
  }
  if (!speaking()) return;
  // Drop what has not been said yet as well as what is playing, or she would
  // carry on with the next sentence a moment after being cut off.
  queuePlaying = false;
  audioQueue = [];
  replyAudio.pause();
  replyAudio.currentTime = 0;
  addLine("system", "interrupted");
  setPresence("interrupted");
}

/* Reads /api/voice/stream: the transcript first (so he sees what she heard
   before she has said anything), then one audio chunk per sentence, then the
   written reply. Returns "no-speech" when nothing was said, false when the
   stream could not be used at all so the caller can fall back. */
async function streamVoiceTurn(form) {
  let sawAudio = false;
  try {
    const res = await fetch("/api/voice/stream", { method: "POST", body: form });
    if (!res.ok || !res.body) return false;
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let heard = null;
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
        if (!data) continue;
        const payload = JSON.parse(data);
        if (event === "transcript") {
          heard = payload.transcript;
          if (heard) addLine("user", heard);
        } else if (event === "audio") {
          sawAudio = true;
          enqueueAudio(payload.b64);
        } else if (event === "done") {
          if (!heard) return "no-speech";
          if (payload.reply) addLine("kyra", payload.reply, replyMeta(payload));
        } else if (event === "error") {
          throw new Error(payload);
        }
      }
    }
    // Let her finish saying it before the mic goes live again.
    while (speaking()) await new Promise((r) => setTimeout(r, 120));
    return true;
  } catch (err) {
    if (sawAudio) throw err; // already speaking - a retry would talk over her
    return false;
  }
}

async function sendVoiceBlob(blob) {
  if (blob.size === 0) {
    setMicState(handsFreeActive ? "listening" : "idle");
    return;
  }
  await cancelTurn(); // speaking to her supersedes a text reply still streaming
  setMicState("processing");
  input.disabled = true;
  sendBtn.disabled = true;
  try {
    const form = new FormData();
    form.append("audio", blob, "utterance.webm");
    const streamed = await streamVoiceTurn(form);
    if (streamed === "no-speech") {
      coreSub.textContent = "didn't catch that";
      return;
    }
    if (streamed === false) {
      // A proxy that buffers SSE, or an older server: take the whole-reply path.
      const res = await fetch("/api/voice", { method: "POST", body: form });
      if (!res.ok) throw new Error(`server returned ${res.status}`);
      const data = await res.json();
      if (!data.transcript) {
        coreSub.textContent = "didn't catch that";
        return;
      }
      addLine("user", data.transcript);
      addLine("kyra", data.reply, replyMeta(data));
      if (data.reply_audio_b64) await playReply(data.reply_audio_b64);
    }
  } catch (err) {
    addLine("error", `voice turn failed — ${err.message}`);
    setPresence("failed");
  } finally {
    input.disabled = false;
    sendBtn.disabled = false;
    setMicState(handsFreeActive ? "listening" : "idle");
    focusSync();
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
    setPresence("failed", "microphone unavailable");
    micBtn.dataset.state = "idle";
    micLabel.textContent = MIC_STATE_LABELS.idle;
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
  if (speaking() || micBtn.dataset.state === "processing") {
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
  if (speaking()) {
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

document.getElementById("transcript-clear").addEventListener("click", clearTranscript);

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
  restoreTranscript();
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

const toolsToggle = document.getElementById("tools-toggle");
const toolsPanel = document.getElementById("tools-panel");
const toolsClose = document.getElementById("tools-close");

function openToolsPanel() {
  toolsPanel.classList.add("is-open");
  toolsPanel.setAttribute("aria-hidden", "false");
  toolsToggle.classList.add("is-active");
  const activeTab = document.querySelector("#tools-panel .jobs-tab.is-active");
  const loaders = { reminders: loadReminders, initiatives: loadInitiatives, learning: loadLearningDue, memory: loadMemoryNotes };
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
    const loaders = { reminders: loadReminders, initiatives: loadInitiatives, learning: loadLearningDue, memory: loadMemoryNotes };
    if (loaders[tab.dataset.toolsTab]) loaders[tab.dataset.toolsTab]();
  });
});

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
let searchKind = "";

function openSearchPanel() {
  searchPanel.classList.add("is-open");
  searchPanel.setAttribute("aria-hidden", "false");
  searchToggle.classList.add("is-active");
  searchQuery.focus();
}
function closeSearchPanel() {
  searchPanel.classList.remove("is-open");
  searchPanel.setAttribute("aria-hidden", "true");
  searchToggle.classList.remove("is-active");
}
searchToggle.addEventListener("click", () => {
  searchPanel.classList.contains("is-open") ? closeSearchPanel() : openSearchPanel();
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
  const body = searchBody();
  if (!body.query) return;
  searchAnswerBox.hidden = true;
  setSearchStatus("searching…");
  try {
    const res = await fetch("/api/search", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const data = await readJson(res);
    renderHits(data.hits);
    setSearchStatus(
      `${data.count} result${data.count === 1 ? "" : "s"}`
      + `${data.include_sensitive ? " · private docs included" : ""}`
      + ` · ${indexAge(data.indexed_at)}`
    );
  } catch (err) {
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
  focusChip.textContent = `FOCUS ${mm}:${ss}`;
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
  focusPanel.classList.add("is-open");
  focusPanel.setAttribute("aria-hidden", "false");
  focusToggle.classList.add("is-active");
}
function closeFocusPanel() {
  focusPanel.classList.remove("is-open");
  focusPanel.setAttribute("aria-hidden", "true");
  focusToggle.classList.remove("is-active");
}
focusToggle.addEventListener("click", () => {
  focusPanel.classList.contains("is-open") ? closeFocusPanel() : openFocusPanel();
});
focusClose.addEventListener("click", closeFocusPanel);
focusChip.addEventListener("click", openFocusPanel);

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
      // The turn itself was the user gesture, so starting audio here is allowed.
      await focusAudio.start(data.plan);
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
    focusChip.textContent = "FOCUS — click to resume sound";
    const resume = async () => {
      focusChip.removeEventListener("click", resume);
      await focusAudio.start(data.plan);
    };
    focusChip.addEventListener("click", resume);
  } catch (_) {
    // server not reachable yet - the panel still opens, the block is still there
  }
}
focusRestore();

// Wind-down must arrive on its own: without this the evening theme only applied
// when Duc happened to send a turn, so an evening spent reading would stay on the
// daytime palette - which is the one thing the evening rule exists to prevent. The
// hour lives in companion/focus.py and is reported by /api/focus/active, so this
// asks rather than duplicating the constant. Slow on purpose; it is a theme, not a
// countdown, and the same call keeps the block state in step with the other front
// doors for free.
setInterval(focusSync, 5 * 60 * 1000);
