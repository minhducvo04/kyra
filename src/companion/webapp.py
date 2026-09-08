"""Local web UI backend - thin FastAPI wrapper around ConversationManager.

Single-user, single-process, in-memory session: this runs on Duc's own
machine for Duc, not a multi-tenant service, so one global conversation is
correct, not a shortcut. The CLAUDE/LOCAL/AUTO toggle in the UI picks
between forcing a backend (manual, skips the router entirely - the "safe
button to overdrive" from the original router design conversation) and
AUTO, which hands every turn to the same TurnRouter chat.py/voice_chat.py
use - one router, three front doors.
"""
import base64
import json
import logging
import queue
import secrets
import threading
import time
from dataclasses import asdict, replace
from functools import cached_property
from pathlib import Path

from anthropic import Anthropic
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from companion import webauth
from companion.apply_pipeline import ApplyError, engine_for_url, run_apply_pipeline
from companion.config import require_api_key
from companion.conversation import ConversationManager
from companion.db import engine_for_store
from companion.default_tools import default_tool_registry
from companion.doc_text import UnsupportedDocumentType, extract_text
from companion.errors import ApiError, install_error_handlers
from companion.github_profile import extract_username as extract_github_username
from companion.github_profile import fetch_github_projects
from companion.job_applications import (
    VALID_STATUSES,
    FitBlock,
    FitSelection,
    JobApplicationStore,
    analyze_latex_resume_fit,
    draft_application_material,
    generate_latex_from_selection,
    optimize_latex_resume_one_page,
)
from companion.job_autofill import GreenhouseAutofillEngine, default_engines, resume_for_url
from companion.job_documents import JobDocumentStore
from companion.job_posting_fetch import fetch_posting as _fetch_posting
from companion.jobs import DbJobQueue, Handler, start_inline_worker
from companion.learning import LearningStore
from companion.llm import AnthropicLLM, TurnCancelled, build_llm, voice_backends
from companion.memory import ChromaMemoryStore
from companion.memory_notes import SUGGESTED_CATEGORIES, MarkdownMemoryNotesStore
from companion.news import TechNewsTool
from companion.paths import DATA_DIR, WEB_DIR
from companion.paths import GENERATED_RESUMES_DIR as RESUME_PDF_DIR
from companion.persona import KYRA
from companion.profile import load_profile, save_profile
from companion.reminders import RemindersStore
from companion.router import TurnRouter, route_and_answer_verbose
from companion.science import ScienceFactsTool
from companion.settings import get_settings
from companion.voice_text import spoken_text, take_sentences

app = FastAPI(title="Kyra")
install_error_handlers(app)

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
# Reachable without authenticating, and each for a reason: /healthz so an
# orchestrator can tell a live task from a dead one (gating it makes every
# deploy restart-loop the moment a token is set), /login and /api/login or
# there is no way to obtain a session, /static because the login page needs
# its stylesheet and neither asset is a secret.
_OPEN_PATHS = {"/healthz", "/login", "/api/login"}
# A wrong token is cheap to retry over HTTP, so slow it down per caller.
_LOGIN_MAX_FAILURES = 5
_LOGIN_WINDOW_SECONDS = 300
_login_failures: dict[str, list[float]] = {}


def _client_host(request: Request) -> str:
    """The socket peer, never a header.

    X-Forwarded-For and friends are set by whoever sent the request, so reading
    them here would let anyone claim 127.0.0.1 and skip the gate entirely. This is
    also why uvicorn is not started with --proxy-headers: that flag overwrites
    scope["client"] from exactly that header. The cost is that a proxy on the same
    host makes every caller look local, which is what KYRA_TRUST_LOOPBACK is for.
    """
    return request.client.host if request.client else ""


def _authorized(request: Request, token: str) -> bool:
    """Three ways in, and the browser can only use the third.

    Bearer is what KyraClient.swift sends and must keep working untouched. The
    cookie exists because web/app.js reaches the API from 43 fetch call sites and
    2 EventSource ones, and EventSource has no header API - so with a token set
    behind a proxy the HUD was simply dead before this.
    """
    settings = get_settings()
    if settings.trust_loopback and _client_host(request) in _LOOPBACK:
        return True
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer ") and secrets.compare_digest(header[7:], token):
        return True
    return webauth.session_valid(
        request.cookies.get(webauth.COOKIE_NAME, ""), token, settings.session_ttl_days * 86400
    )


@app.middleware("http")
async def _require_api_token(request: Request, call_next):
    """The one boundary between the internet and Kyra, once the server is exposed.

    Only enforced when KYRA_API_TOKEN is set, so an unconfigured laptop behaves
    exactly as it always did. When it is set the gate covers the whole app, not
    just /api/ - a hosted Kyra serving its HUD to strangers would be showing them
    Duc's profile, outreach contacts and memory notes, and every chat turn spends
    his key. Still not multi-user auth: one token, one tenant, no accounts.
    """
    token = get_settings().api_token
    path = request.url.path
    if not token or path in _OPEN_PATHS or path.startswith("/static/"):
        return await call_next(request)
    if _authorized(request, token):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse(
            status_code=401,
            content={"error": {"code": "unauthorized", "message": "missing or wrong API token", "details": {}}},
            headers={"WWW-Authenticate": "Bearer"},
        )
    # A browser asking for a page gets sent somewhere it can act, not a JSON 401.
    return RedirectResponse("/login", status_code=303)

# A resume or writing sample is a few hundred KB at most; a bound keeps
# a mis-dropped file (a video, a giant PDF) from being read into memory
# whole and handed to a parser.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

logger = logging.getLogger(__name__)

_claude = build_llm("claude")
_backends = voice_backends(_claude)  # + a `voice` entry when KYRA_VOICE_MODEL is set
_registry = default_tool_registry()
_router = TurnRouter(_registry)
_current_backend = "auto"  # "claude" | "local" | "auto" - auto (the router) is the default now that it exists
_cancel_current: threading.Event | None = None  # the in-flight streamed turn's stop signal, if any


class _Runtime:
    """The heavy, model-loading pieces (BGE embeddings for memory,
    faster-whisper, Kokoro) built on first use rather than at import.
    Importing this module used to load three ML models before serving a
    single request - which made the server slow to start and made the
    HTTP layer impossible to test without those models. Same
    LazyBackends idea (llm.py) applied to the rest of the runtime.
    """

    @cached_property
    def memory(self) -> ChromaMemoryStore:
        return ChromaMemoryStore()

    @cached_property
    def conversation(self) -> ConversationManager:
        return ConversationManager(persona=KYRA, memory=self.memory, llm=_claude)

    # companion.voice is imported here, not at module top: it pulls in numpy
    # and the audio stack, and the HTTP layer must import (and be testable)
    # without them - the first real CI run failed at collection on exactly
    # this (ModuleNotFoundError: numpy) with the slim install list.
    # The search index loads Chroma and the BGE embedding model, so it is built on
    # first use like everything else here - importing webapp must stay cheap.
    @cached_property
    def search_index(self):
        from companion.search import HybridSearchIndex

        return HybridSearchIndex()

    @cached_property
    def stt(self):
        from companion.voice import FasterWhisperSTT

        return FasterWhisperSTT()

    @cached_property
    def tts(self):
        from companion.voice import KokoroTTS

        return KokoroTTS()


_rt = _Runtime()

# Job application panel - hits the same underlying stores/tools as the
# chat path (job_applications.py, job_autofill.py) directly rather than
# round-tripping through the router/classifier, since a dedicated UI
# already knows exactly which action it wants (no intent classification
# needed for a button labeled "Add").
_job_store = JobApplicationStore()
# Dedicated instance at the larger tool-calling token budget - same
# reasoning as default_tools.py::default_tool_registry(draft_backend=...):
# a draft plus a critique pass needs more room than plain chat's default.
_draft_llm = AnthropicLLM(Anthropic(api_key=require_api_key()), max_tokens=2500)
# A full resume rewrite is a genuinely long document, not a paragraph -
# a 3000-token budget silently truncated mid-document in testing, and
# 6000 still hit the same wall for real (2026-09-04, Duc's actual LaTeX
# resume against a real job posting - "cut off before finishing" on
# repeat attempts). Same root cause each time: Sonnet 5's adaptive
# thinking eats into max_tokens unpredictably (see llm.py), and every
# iteration of the one-page fit loop has to return a COMPLETE document,
# not a diff, so thinking + a full multi-KB LaTeX/resume output can
# together exceed a budget that looked generous in isolated testing.
# Raising the ceiling costs nothing unless actually used (max_tokens is
# a cap, not a reservation) - 16000 leaves enormous headroom over any
# real resume's output size. Separate instance/budget from _draft_llm,
# which stays sized for cover letters/bullets (already verified clean
# at 2500 - much shorter output, doesn't need this).
# Every fit-loop iteration must return a COMPLETE document, not a diff, and Sonnet's
# adaptive thinking is charged against the same ceiling. The base resume has grown to
# ~4.5k tokens, and 16000 started failing on it with the truncation marker (2026-09-07,
# tailoring for Composio) - the same way 6000 failed once the resume passed ~4k. This is
# a ceiling, not a reservation: raising it costs nothing unless it is used.
# 40000 (2026-09-07): 32000 was already past the SDK's non-streaming limit (~21k tokens), so
# respond() streams now and the ceiling is free to sit well above any observed output (the
# largest real fit-loop reply so far was ~13.8k output tokens on ~10k of input).
_resume_llm = AnthropicLLM(Anthropic(api_key=require_api_key()), max_tokens=40000)
_autofill_engine = GreenhouseAutofillEngine()
# apply_pipeline routes by the posting URL's ATS; add a Lever/Ashby engine here when one exists.
# One engine per supported ATS, keyed the way job_posting_fetch parses a posting URL.
# Ashby joined on 2026-09-07 (built against real Composio and Netic forms), which is
# where most of the new-grad postings Duc tracks actually live.
_autofill_engines = default_engines()
_job_documents = JobDocumentStore()
_memory_notes = MarkdownMemoryNotesStore()

# TOOLS panel - reminders/news/science/learning were already built and
# working through chat, just never given their own UI surface. Same
# direct-endpoint pattern as the JOBS panel: a button already knows what
# it wants, no need to round-trip through the classifier.
_reminders_store = RemindersStore()
_learning_store = LearningStore()
_news_tool = TechNewsTool()
_science_tool = ScienceFactsTool()


class ChatIn(BaseModel):
    message: str


class ChatOut(BaseModel):
    reply: str
    backend: str  # the mode ("claude"/"local"/"auto"); in auto mode, actual_backend says what the router picked
    actual_backend: str | None = None
    path: str | None = None  # "text" | "tool", auto mode only
    reason: str | None = None  # the router's reason, auto mode only


class VoiceOut(ChatOut):
    transcript: str
    reply_audio_b64: str  # WAV bytes, base64 - one JSON response carries transcript + reply + audio together
    reply_audio_rate: int


class BackendIn(BaseModel):
    backend: str


class CorrectionIn(BaseModel):
    reply: str


class LoginIn(BaseModel):
    token: str


@app.get("/")
def index() -> HTMLResponse:
    """Serves index.html with each static asset's real file mtime
    appended as a cache-busting query string (e.g. app.js?v=1725...) -
    a real bug caught 2026-09-04 while building the resume "Detailed"
    mode: a browser (including the automated test browser used to
    verify this UI) can keep serving a stale cached app.js/style.css
    after an edit even across a hard reload, since StaticFiles' ETag/
    Last-Modified validation doesn't guarantee a re-fetch. A manually
    bumped version string would work but silently goes stale the next
    time someone edits these files and forgets to bump it - reading the
    real mtime here means it can't go stale, no discipline required.
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for asset in ("app.js", "style.css"):
        mtime = int((WEB_DIR / asset).stat().st_mtime)
        html = html.replace(f'/static/{asset}"', f'/static/{asset}?v={mtime}"')
    return HTMLResponse(html)


@app.get("/healthz")
def healthz() -> dict:
    """Liveness for a container or load balancer, deliberately touching nothing.

    It must answer while Postgres, Chroma and the model are all unavailable,
    otherwise a slow dependency reads as a dead process and the orchestrator
    restarts a server that was fine. Ungated for the same reason: an authenticated
    healthcheck fails closed the moment a token is set, and the deploy
    restart-loops with no obvious cause.
    """
    return {"status": "ok"}


@app.get("/login")
def login_page() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "login.html").read_text(encoding="utf-8"))


def _login_throttled(host: str) -> bool:
    """Per-caller, so a stranger guessing cannot lock Duc out of his own server."""
    now = time.time()
    recent = [t for t in _login_failures.get(host, []) if now - t < _LOGIN_WINDOW_SECONDS]
    _login_failures[host] = recent
    return len(recent) >= _LOGIN_MAX_FAILURES


@app.post("/api/login")
def login(body: LoginIn, request: Request) -> JSONResponse:
    """Exchange the token for a session cookie a browser can actually carry."""
    token = get_settings().api_token
    if not token:
        raise ApiError(400, "no_token_configured", "this server has no KYRA_API_TOKEN set")
    host = _client_host(request)
    if _login_throttled(host):
        raise ApiError(429, "too_many_attempts", "too many failed attempts, wait a few minutes")
    if not secrets.compare_digest(body.token, token):
        _login_failures.setdefault(host, []).append(time.time())
        raise ApiError(401, "unauthorized", "wrong token")
    _login_failures.pop(host, None)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        webauth.COOKIE_NAME,
        webauth.issue_session(token),
        max_age=get_settings().session_ttl_days * 86400,
        httponly=True,
        samesite="lax",
        # Over plain http the flag would stop the cookie ever coming back. Worst case
        # of trusting the proxy header here is a login that visibly fails, not a leak.
        secure=request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "") == "https",
        path="/",
    )
    return response


@app.post("/api/logout")
def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(webauth.COOKIE_NAME, path="/")
    return response


@app.get("/api/backend")
def get_backend() -> dict:
    return {"backend": _current_backend}


@app.post("/api/backend")
def set_backend(body: BackendIn) -> dict:
    global _current_backend
    name = body.backend
    if name not in ("claude", "local", "auto"):
        raise ApiError(400, "unknown_backend", f"unknown backend {name!r}", {"allowed": ["claude", "local", "auto"]})
    if name in ("claude", "local"):
        _rt.conversation.llm = _backends[name]  # first switch to local loads the model - can take a while
    _current_backend = name
    return {"backend": _current_backend}


def _answer(message: str, on_token=None, register: str | None = None) -> ChatOut:
    """Shared by /api/chat, /api/chat/stream and /api/voice - text in, routed
    reply out. The only thing voice adds on top is transcribing in and
    synthesizing out; the only thing streaming adds is on_token, which
    receives each text delta when the backend that answers can stream (Claude
    text turns) and nothing otherwise (tool turns, the local model).
    """
    if _current_backend != "auto":
        reply = _rt.conversation.handle_turn(message, on_token=on_token, register=register)
        return ChatOut(reply=reply, backend=_current_backend)

    reply, decision = route_and_answer_verbose(
        message, _rt.conversation, _router, _backends, _registry, on_token=on_token, register=register
    )
    if decision is None:  # a mode-switch command ("focus mode" etc.), not a routed turn
        return ChatOut(reply=reply, backend="auto")
    return ChatOut(
        reply=reply, backend="auto", actual_backend=decision.backend, path=decision.path, reason=decision.reason
    )


@app.post("/api/chat", response_model=ChatOut)
def chat(body: ChatIn) -> ChatOut:
    return _answer(body.message)


@app.post("/api/chat/stream")
def chat_stream(body: ChatIn) -> StreamingResponse:
    """The same turn as /api/chat, as server-sent events: `token` events with
    each text delta as it arrives, then one `done` event carrying the full
    ChatOut (or `error`). Time-to-first-token is what a conversation feels
    like (docs/plans/2026-09-07-human-interface.md, point 2); the whole-reply
    endpoint stays for callers that do not care.

    POST, not GET with a query string, so the message never lands in an
    access log. The turn runs on a worker thread and tokens cross to the
    response through a queue; a turn that cannot stream simply yields `done`.
    """
    q: queue.Queue = queue.Queue()
    done = object()

    # Each turn owns its own stop signal and _cancel_current just points at the
    # newest one, so a stop pressed a moment late sets an Event nobody is
    # reading rather than killing whatever turn started next.
    global _cancel_current
    cancel = threading.Event()
    _cancel_current = cancel

    def on_token(delta: str) -> None:
        if cancel.is_set():
            raise TurnCancelled()
        q.put(("token", delta))

    def run() -> None:
        global _cancel_current
        try:
            out = _answer(body.message, on_token=on_token)
            q.put(("done", out.model_dump()))
        except Exception as e:  # noqa: BLE001 - surfaced to the client as an error event, never swallowed
            logger.exception("chat stream failed")
            q.put(("error", f"{type(e).__name__}: {e}"))
        finally:
            if _cancel_current is cancel:
                _cancel_current = None
            q.put(done)

    threading.Thread(target=run, name="kyra-chat-stream", daemon=True).start()

    def events():
        while True:
            item = q.get()
            if item is done:
                return
            kind, payload = item
            yield f"event: {kind}\ndata: {json.dumps(payload)}\n\n"

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@app.post("/api/chat/cancel")
def chat_cancel() -> dict:
    """Stop the reply that is being generated right now.

    The browser aborting its fetch only stops it *listening*: the turn keeps
    running here, and ConversationManager._record_turn would then file the
    whole reply into history and memory - leaving Kyra remembering saying
    something Duc never saw, and quoting it back next turn. This is the half
    that actually stops the model, via the on_token callback the streaming
    turn is already calling per delta (see llm.TurnCancelled).

    Only a streamed text turn can be stopped. A tool turn has no on_token and
    runs to completion, which is what you want once a tool has begun doing
    something real; the browser stops showing it either way.
    """
    ev = _cancel_current
    if ev is None:
        return {"cancelled": False}
    ev.set()
    return {"cancelled": True}


@app.post("/api/correction")
def correction(body: CorrectionIn) -> dict:
    """Duc marking one Kyra line as wrong.

    The 2026 problem with a companion is correction, not recognition
    (docs/plans/2026-09-07-human-interface.md, point 5): the interface answer
    is a transcript he can point at, and this is the data answer. It lands in
    memory notes rather than the vector store on purpose - notes are loaded in
    full into every system prompt, so a correction is in front of her on the
    next turn instead of waiting to be semantically similar to something.
    """
    text = " ".join(body.reply.split())
    if not text:
        raise ApiError(400, "empty_reply", "there is no reply text to mark wrong")
    excerpt = text if len(text) <= 120 else text[:120].rstrip() + "\u2026"
    _memory_notes.add("corrections", f"Duc marked this reply as wrong: {excerpt}")
    return {"saved": True, "category": "corrections"}


# Measured against the real API on 2026-09-08: 6,797 characters of real system
# prompt came to 2,386 tokens. Re-measure if the notes' shape changes a lot.
CHARS_PER_TOKEN = 2.85


class MemoryNoteIn(BaseModel):
    category: str = "general"
    note: str


class MemoryNoteRef(BaseModel):
    category: str
    text: str


@app.get("/api/memory-notes")
def list_memory_notes() -> dict:
    """What Kyra durably believes about Duc.

    These are not the conversation log - they are the small curated set that
    goes into *every* system prompt in full, and into every resume draft. Until
    now the only way to read them was to open data/memory_notes/*.md, which is
    the same transparency gap the PROFILE tab's "view raw record" closed for the
    applicant profile. A true-but-irrelevant note has already become a
    fabricated resume entry once (CLAUDE.md, 2026-09-04), so seeing them, and
    being able to throw one away, is a real control rather than a nicety.
    """
    notes = _memory_notes.list_notes()
    # How heavy the layer has become. It is rendered in full into every system
    # prompt, and was measured at about 65% of one (docs/voice-latency.md); the
    # design rests on the set staying small, and nothing said when it had stopped
    # being small.
    #
    # Characters are exact. The token figure is an estimate, but not the usual
    # chars/4 rule: a real prompt measured against the API was 6,797 characters
    # for 2,386 tokens, so this content runs about 2.85 characters per token -
    # dated bullets with markdown are denser than the prose that rule assumes,
    # and chars/4 understated it by a third. Estimated rather than counted
    # because a count_tokens call per page load is a network round trip to
    # answer a question that only needs a sense of scale.
    rendered = _memory_notes.render()
    chars = 0 if rendered == "(no saved notes yet)" else len(rendered)
    return {
        "notes": [asdict(n) for n in notes],
        "categories": sorted({n.category for n in notes}) or SUGGESTED_CATEGORIES,
        "rendered_chars": chars,
        "approx_tokens": round(chars / CHARS_PER_TOKEN),
    }


@app.post("/api/memory-notes")
def add_memory_note(body: MemoryNoteIn) -> dict:
    try:
        _memory_notes.add(body.category, body.note)
    except ValueError as e:
        raise ApiError(400, "empty_note", str(e)) from e
    return {"saved": True}


@app.post("/api/memory-notes/delete")
def delete_memory_note(body: MemoryNoteRef) -> dict:
    # Human-initiated removal of one line; nothing here resolves contradictions
    # on its own, which is the property memory_notes.py's docstring protects.
    if not _memory_notes.delete(body.category, body.text):
        raise ApiError(404, "note_not_found", "no note with that text in that category")
    return {"deleted": True}


def _audio_event(sentence: str, encode_wav_bytes) -> tuple[str, dict] | None:
    """One sentence -> one `audio` SSE event, or None when there is nothing a
    voice can say (a code fence strips to nothing). Shared by /api/voice/stream
    and /api/speak so the two cannot drift: both strip markdown per sentence
    before the synthesiser ever sees it, which is the guarantee voice_text.py
    exists to make."""
    speech = spoken_text(sentence)
    if not speech:
        return None
    samples, rate = _rt.tts.speak(speech)
    wav = encode_wav_bytes(samples, rate)
    return ("audio", {"b64": base64.b64encode(wav).decode(), "rate": rate, "text": speech})


class SpeakIn(BaseModel):
    text: str


@app.post("/api/speak")
def speak(body: SpeakIn) -> StreamingResponse:
    """Say text that is already written, one sentence at a time.

    /api/voice/stream takes a microphone recording and runs a whole turn; a
    client that typed its turn through /api/chat/stream has the reply already
    and only needs a voice for it. Same events (`audio` per sentence, then
    `done`) so a client reuses one player for both, and same per-sentence
    synthesis so the first words arrive while the rest is still being made.

    Synchronous rather than threaded, unlike the voice path: there is no model
    call to overlap with, only synthesis, and Kokoro is fast enough that the
    first sentence is out in about half a second.
    """
    from companion.voice import encode_wav_bytes

    text = body.text.strip()
    if not text:
        raise ApiError(400, "empty_text", "there is nothing to say")

    def events():
        sentences, _ = take_sentences(text, final=True)
        for sentence in sentences:
            event = _audio_event(sentence, encode_wav_bytes)
            if event:
                yield f"event: {event[0]}\ndata: {json.dumps(event[1])}\n\n"
        yield f"event: done\ndata: {json.dumps({'sentences': len(sentences)})}\n\n"

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@app.post("/api/voice/stream")
def voice_stream(audio: UploadFile) -> StreamingResponse:
    """One utterance in, her reply out sentence by sentence as SSE.

    The point is time-to-first-word. /api/voice waits for the whole reply, then
    synthesises the whole thing, then sends one WAV - measured at 5.3s of silence
    on 2026-09-08 (docs/voice-latency.md). The reply is already streamed, and
    synthesising just the first sentence costs 0.48s against 1.12s for all of it,
    so speaking each sentence as it finishes puts the first word near 3.6s.

    Events: `transcript` (what she heard, immediately), then one `audio` per
    sentence, then `done` with the written reply. Each audio chunk is a complete
    WAV rather than a slice of one stream, so the browser can just play them in
    order without MediaSource. A turn that cannot stream - a tool turn has no
    on_token - falls back to synthesising the whole reply at the end, so the
    audio is never silently dropped.
    """
    from companion.voice import decode_uploaded_audio, encode_wav_bytes

    pcm = decode_uploaded_audio(audio.file)
    transcript = _rt.stt.transcribe(pcm).strip()

    q: queue.Queue = queue.Queue()
    finished = object()

    def say(sentence: str) -> None:
        """Synthesise one sentence and hand it to the browser."""
        event = _audio_event(sentence, encode_wav_bytes)
        if event:
            q.put(event)

    def run() -> None:
        try:
            buffer = ""
            spoken_any = False

            def on_token(delta: str) -> None:
                nonlocal buffer, spoken_any
                buffer += delta
                sentences, buffer = take_sentences(buffer)
                for sentence in sentences:
                    say(sentence)
                    spoken_any = True

            out = _answer(transcript, on_token=on_token, register="voice")
            tail, _ = take_sentences(buffer, final=True)
            for sentence in tail:
                say(sentence)
                spoken_any = True
            if not spoken_any:
                # Nothing streamed (a tool turn, or the local model): say it all now.
                say(out.reply)
            q.put(("done", out.model_dump()))
        except Exception as e:  # noqa: BLE001 - surfaced as an error event, never swallowed
            logger.exception("voice stream failed")
            q.put(("error", f"{type(e).__name__}: {e}"))
        finally:
            q.put(finished)

    def events():
        yield f"event: transcript\ndata: {json.dumps({'transcript': transcript})}\n\n"
        if not transcript:
            # Nothing was said; do not spend a turn on silence.
            yield f"event: done\ndata: {json.dumps(ChatOut(reply='', backend=_current_backend).model_dump())}\n\n"
            return
        threading.Thread(target=run, name="kyra-voice-stream", daemon=True).start()
        while True:
            item = q.get()
            if item is finished:
                return
            kind, payload = item
            yield f"event: {kind}\ndata: {json.dumps(payload)}\n\n"

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@app.post("/api/voice", response_model=VoiceOut)
def voice(audio: UploadFile) -> VoiceOut:
    """One mic recording in, one spoken reply out - push-to-talk and
    hands-free in the web UI both hit this same endpoint per utterance;
    the difference is only how the browser decides when to call it.
    """
    from companion.voice import decode_uploaded_audio, encode_wav_bytes

    pcm = decode_uploaded_audio(audio.file)
    transcript = _rt.stt.transcribe(pcm).strip()
    if not transcript:
        return VoiceOut(
            reply="", backend=_current_backend, transcript="", reply_audio_b64="", reply_audio_rate=0
        )

    # Spoken register: the prompt asks for a spoken shape, spoken_text() guarantees no
    # markdown reaches the synthesiser. The transcript still shows the reply as written.
    chat_out = _answer(transcript, register="voice")
    reply_audio, rate = _rt.tts.speak(spoken_text(chat_out.reply))
    wav_bytes = encode_wav_bytes(reply_audio, rate)

    return VoiceOut(
        **chat_out.model_dump(),
        transcript=transcript,
        reply_audio_b64=base64.b64encode(wav_bytes).decode("ascii"),
        reply_audio_rate=rate,
    )


# ---------------- Search panel ----------------
# One front door over everything Kyra stores (docs, digests, resumes, stores,
# conversation log). Sensitivity is a default argument rather than a prompt:
# nothing under data/private_docs/ is returned or shown to the model unless the
# caller explicitly asks, and the request model defaults that to False.


class SearchIn(BaseModel):
    query: str
    k: int = 8
    kinds: list[str] | None = None
    include_sensitive: bool = False
    mode: str = "hybrid"


def _hit_out(hit) -> dict:
    c = hit.chunk
    return {
        "chunk_id": c.chunk_id, "path": c.path, "kind": c.kind, "title": c.title,
        "index": c.index, "sensitive": c.sensitive, "score": round(hit.score, 5),
        "lexical_rank": hit.lexical_rank, "vector_rank": hit.vector_rank,
        "snippet": c.text[:400],
    }


@app.post("/api/search")
def search_endpoint(body: SearchIn) -> dict:
    query = body.query.strip()
    if not query:
        raise ApiError(400, "empty_query", "give me something to search for")
    if body.mode not in ("hybrid", "lexical", "vector"):
        raise ApiError(400, "bad_mode", f"unknown mode {body.mode!r}", {"allowed": ["hybrid", "lexical", "vector"]})
    hits = _rt.search_index.search(
        query, k=max(1, min(body.k, 50)), kinds=body.kinds or None,
        include_sensitive=body.include_sensitive, mode=body.mode,
    )
    # When the index was last built. Searching never refreshes it (only an
    # explicit reindex and the 05:00 digest do), so results can silently predate
    # this morning's edits - the panel says so rather than letting an old answer
    # look current. The chat tool carries the same date for the same reason.
    return {"query": query, "count": len(hits), "include_sensitive": body.include_sensitive,
            "indexed_at": _rt.search_index.last_indexed(),
            "hits": [_hit_out(h) for h in hits]}


@app.post("/api/search/answer")
def search_answer(body: SearchIn) -> StreamingResponse:
    """Retrieval plus a cited answer from the LOCAL model, as SSE - the same
    event shape as /api/chat/stream. It is a stream because the local model
    takes seconds and the panel should say what it is doing meanwhile; the
    answer itself arrives whole in `done`, since answer() returns citations
    and warnings together with the text."""
    query = body.query.strip()
    if not query:
        raise ApiError(400, "empty_query", "give me something to search for")

    q: queue.Queue = queue.Queue()
    finished = object()

    def run() -> None:
        try:
            from companion.search import answer as search_answer_fn

            q.put(("progress", "searching the index…"))
            index = _rt.search_index
            # The local model is fetched here, not above: LazyBackends builds a 14B
            # MLX model on first access, and that must not happen until an answer is
            # genuinely being written (it also makes this endpoint testable without it).
            q.put(("progress", "asking the local model to write it up…"))
            result = search_answer_fn(
                index, query, k=max(1, min(body.k, 20)), llm=_backends["local"],
                include_sensitive=body.include_sensitive, kinds=body.kinds or None,
            )
            q.put(("done", {
                "query": query,
                "text": result.text,
                "warnings": result.warnings,
                "citations": [{"n": i + 1, "path": c.path, "title": c.title, "kind": c.kind}
                              for i, c in enumerate(result.citations)],
                "hits": [_hit_out(h) for h in result.hits],
            }))
        except Exception as e:  # noqa: BLE001 - surfaced to the client, never swallowed
            logger.exception("search answer failed")
            q.put(("error", f"{type(e).__name__}: {e}"))
        finally:
            q.put(finished)

    threading.Thread(target=run, name="kyra-search-answer", daemon=True).start()

    def events():
        while True:
            item = q.get()
            if item is finished:
                return
            kind, payload = item
            yield f"event: {kind}\ndata: {json.dumps(payload)}\n\n"

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@app.post("/api/search/reindex")
def search_reindex() -> dict:
    """Incremental by content hash, so a refresh after a few edits is quick;
    a first build is not. The index never refreshes itself on search, which is
    why this button exists at all."""
    stats = _rt.search_index.index()
    return {"added": stats.added, "updated": stats.updated, "unchanged": stats.unchanged,
            "deleted": stats.deleted, "chunks": stats.chunks, "errors": stats.errors,
            "summary": str(stats)}


# ---------------- Job Application panel ----------------
# Direct endpoints, not routed through the classifier - a dedicated UI
# button already knows exactly which action it wants, so there's no
# intent to classify. Same underlying stores/tools chat uses either way
# (job_applications.py, job_autofill.py, profile.py) - no logic forked.


class DraftOut(BaseModel):
    draft: str
    background_chars: int
    style_chars: int
    warnings: list[str] = []
    pdf_url: str | None = None  # set for material_type == "latex_resume"
    questions: list[str] = []  # bullets that would benefit from a real number - Duc answers, Kyra never guesses
    # One-page fit status for the LaTeX path - None for non-resume
    # material. Surfaced as its own field (not buried in warnings) so
    # the UI can show an unmissable pass/fail, since "it came back as 2
    # pages" was a real complaint that a warnings line didn't prevent.
    fit: bool | None = None
    page_count: int | None = None
    overflow_lines: int | None = None
    notes: list[str] = []


# --- Resume "Detailed" mode (analyze -> pick -> generate) - a JSON-body
# request/response shape rather than the file-upload Form(...) pattern
# the rest of JOBS uses, since this flow has no file upload of its own
# (it works from a document library selection, same as Fast mode without
# a one-off upload) and passes structured block data back and forth
# between two calls instead. FitBlockIO mirrors job_applications.py's
# FitBlock dataclass field-for-field - kept as a separate pydantic model
# rather than reusing the dataclass directly so FastAPI's OpenAPI schema
# stays accurate without coupling the API shape to the dataclass's exact
# definition.
class FitBlockIO(BaseModel):
    id: str
    section: str
    entry: str
    kind: str
    label: str
    score: int
    priority: str
    reason: str
    recommended_keep: bool


class ResumeFitAnalyzeIn(BaseModel):
    job_context: str = ""
    background_text: str = ""
    background_document_ids: str = ""


class ResumeFitAnalyzeOut(BaseModel):
    sections: list[str] = []
    blocks: list[FitBlockIO] = []
    warnings: list[str] = []


class FitSelectionIn(BaseModel):
    id: str
    keep: bool


class ResumeFitGenerateIn(BaseModel):
    job_context: str = ""
    background_text: str = ""
    background_document_ids: str = ""
    blocks: list[FitBlockIO]
    selections: list[FitSelectionIn]


class ResumeFitGenerateOut(BaseModel):
    draft: str = ""
    pdf_url: str | None = None
    fit: bool = False
    page_count: int | None = None
    overflow_lines: int | None = None
    cut_suggestions: list[FitBlockIO] = []
    notes: list[str] = []
    warnings: list[str] = []


def _get_or_add_document(
    label: str, kind: str, text: str, source_filename: str | None = None, file_bytes: bytes | None = None
):
    """Avoids creating a duplicate library entry when the exact same
    content was already saved - caught for real when the same resume
    file was dropped into both the "Resume" and "Style sample file"
    quick-upload slots in one draft request, silently creating two
    redundant entries (one per slot) for the same document. Matching on
    exact text, regardless of kind - if the content's already there as
    a resume, re-saving it as a style sample too is (almost certainly)
    not what was intended.
    """
    for existing in _job_documents.list():
        if existing.text == text:
            return existing, False  # False = reused, not newly created
    return (
        _job_documents.add(label=label, kind=kind, text=text, source_filename=source_filename, file_bytes=file_bytes),
        True,
    )


def _extract_upload(upload: UploadFile | None) -> tuple[str, bytes | None, str | None]:
    """Returns (text, raw_bytes, warning). raw_bytes is what actually gets
    saved to disk for a resume (see JobDocumentStore.add(file_bytes=...)) -
    job_autofill.py needs a real file to attach to a browser file input,
    extracted text alone can't be uploaded to a form. A warning (not an
    exception) on an unsupported/unreadable file, so one bad upload
    doesn't 500 the whole request - the draft can still proceed on
    whatever else was given.
    """
    if upload is None or not upload.filename:
        return "", None, None
    try:
        data = upload.file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            return "", None, f"{upload.filename}: file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB - not read"
        return extract_text(upload.filename, data), data, None
    except UnsupportedDocumentType as e:
        return "", None, f"{upload.filename}: {e}"
    except Exception as e:
        return "", None, f"{upload.filename}: couldn't read file ({e})"


def _fetch_github_context() -> tuple[str, list[str]]:
    """Pulls real public repo data from Duc's GitHub, per his profile's
    github_url - never invented, and only fetched when explicitly asked
    for on a given draft (a checkbox, not automatic every time), since
    it's a real network call. Returns (text, warnings); text is "" if
    nothing usable came back, with a warning explaining why.
    """
    profile = load_profile()
    if not profile.github_url:
        return "", ["GitHub inclusion requested but no github_url is set in PROFILE"]
    username = extract_github_username(profile.github_url)
    if not username:
        return "", [f"couldn't parse a GitHub username out of {profile.github_url!r}"]
    text, warnings = fetch_github_projects(username)
    if text:
        return f"[Duc's real public GitHub projects, from github.com/{username} - use if genuinely relevant, don't force it]\n{text}", warnings
    return "", warnings


def _pick_resume_source(
    background_text: str, background_document_ids: str, resume: UploadFile | None
) -> tuple[str, str, list[str]]:
    """Shared by the full-resume and LaTeX-resume paths - exactly one
    clean source of truth for the resume itself, in priority order: a
    one-off upload (also saved to the library, same dedup as the
    paragraph-drafting path), then a single selected resume-kind
    document, then pasted text.

    Also gathers "extra facts" alongside it: Memory Notes (durable facts
    Kyra already knows, e.g. "graduated June 2026") plus any selected
    non-resume library documents (free-text notes) - real gap found
    2026-09-04, Duc asked whether he could update resume generation "by
    words" (tell Kyra something new) and neither of these paths looked
    at Memory Notes or note documents at all, unlike the plain
    paragraph-drafting flow which already does. Returns
    (resume_text, extra_facts_text, warnings).
    """
    warnings: list[str] = []
    extra_parts: list[str] = []

    notes_block = _memory_notes.render()
    if notes_block and notes_block != "(no saved notes yet)":
        extra_parts.append(f"[What Kyra already knows about Duc]\n{notes_block}")

    doc_ids = [i.strip() for i in background_document_ids.split(",") if i.strip()]
    picked = _job_documents.get_many(doc_ids)
    for doc in picked:
        if doc.kind != "resume":
            extra_parts.append(f"[{doc.label}]\n{doc.text}")

    def _with_extra(resume_text: str) -> tuple[str, str, list[str]]:
        if background_text.strip():
            extra_parts.append(background_text.strip())
        return resume_text, "\n\n".join(extra_parts), warnings

    resume_text, resume_bytes, warn = _extract_upload(resume)
    if warn:
        warnings.append(warn)
    if resume_text:
        saved, is_new = _get_or_add_document(
            label=resume.filename, kind="resume", text=resume_text, source_filename=resume.filename, file_bytes=resume_bytes
        )
        if is_new:
            warnings.append(f"saved '{saved.label}' to your document library (PROFILE tab → Document Library) for reuse")
        return _with_extra(resume_text)

    resumes = [d for d in picked if d.kind == "resume"]
    if resumes:
        if len(resumes) > 1:
            warnings.append(f"multiple resumes selected - using '{resumes[0].label}', the others were ignored")
        return _with_extra(resumes[0].text)
    if background_text.strip():
        # background_text is the resume source itself here (nothing else
        # was given) - don't also double it into extra_parts.
        return background_text.strip(), "\n\n".join(extra_parts), warnings
    return "", "\n\n".join(extra_parts), warnings


def _save_generated_pdf(pdf_bytes: bytes) -> str:
    """Writes already-rendered PDF bytes to the same generated-resumes
    directory both LaTeX paths serve through the one
    /api/job/resume-pdf/{filename} endpoint. Returns the filename
    (not the full path) for building that URL.
    """
    import uuid

    RESUME_PDF_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex[:12]}.pdf"
    (RESUME_PDF_DIR / filename).write_bytes(pdf_bytes)
    return filename


def _latex_resume_draft(
    job_context: str, background_text: str, background_document_ids: str, resume: UploadFile | None,
    include_github: bool = False,
) -> DraftOut:
    """Edits Duc's own LaTeX source directly, then actually compiles it
    (latex_compile.py) and iterates until it fits exactly one real page
    - see job_applications.py::optimize_latex_resume_one_page for why
    this needs a real compile-measure-iterate loop rather than a single
    blind rewrite. Editing the source directly (rather than going
    through a generic text->HTML->PDF renderer) also sidesteps a real
    text-extraction fidelity issue found testing on Duc's actual PDF
    (dropped underscores, spurious spaces around ordinal superscripts) -
    there's nothing to extract when editing the source. Returns both the
    edited LaTeX (for Duc to keep/tweak) and a compiled PDF preview, so
    he can see the real result before trusting it.
    """
    latex_text, extra_facts, warnings = _pick_resume_source(background_text, background_document_ids, resume)

    if not latex_text:
        return DraftOut(
            draft="", background_chars=0, style_chars=0,
            warnings=warnings + ["nothing to optimize - upload your .tex file, pick one from your document library, or paste it in Extra background"],
        )
    if "\\begin{document}" not in latex_text and "\\documentclass" not in latex_text:
        warnings.append("this doesn't look like LaTeX source (no \\documentclass/\\begin{document} found) - results may be off")

    if include_github:
        github_text, github_warnings = _fetch_github_context()
        warnings.extend(github_warnings)
        if github_text:
            extra_facts = f"{extra_facts}\n\n{github_text}" if extra_facts else github_text

    try:
        result = optimize_latex_resume_one_page(_resume_llm, latex_text, job_context, extra_facts)
    except ValueError as e:
        return DraftOut(draft="", background_chars=len(latex_text), style_chars=0, warnings=warnings + [str(e)])

    warnings.extend(result.guard_warnings)
    if not result.fit:
        warnings.append(
            f"NOT one page: the best attempt compiled to {result.page_count} page(s)"
            + (f" with {result.overflow_lines} line(s) over" if result.overflow_lines else "")
            + " - use Resume — Detailed to pick what to cut, or try again"
        )

    pdf_url = None
    if result.pdf_bytes:
        pdf_url = f"/api/job/resume-pdf/{_save_generated_pdf(result.pdf_bytes)}"

    return DraftOut(
        draft=result.latex, background_chars=len(latex_text), style_chars=0, warnings=warnings, pdf_url=pdf_url,
        fit=result.fit, page_count=result.page_count, overflow_lines=result.overflow_lines, notes=result.notes,
        questions=result.questions,
    )


@app.get("/api/job/resume-pdf/{filename}")
def get_resume_pdf(filename: str):
    # filename comes straight from the URL path - reject anything that
    # isn't a bare generated-uuid.pdf name before touching the filesystem,
    # so this can't be used to read arbitrary files (e.g. "../../.env").
    if "/" in filename or "\\" in filename or ".." in filename or not filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="invalid filename")
    path = RESUME_PDF_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="application/pdf", filename="optimized_resume.pdf")


# ---------------- background jobs (v2 slice 3) ----------------
# The one-page resume loop runs 1-2 minutes with several model calls. As a
# background job it no longer sits inside an HTTP request: the client gets
# an id back at once and streams progress lines over SSE while the worker
# (a thread here; scripts/worker.py in the container) does the work.
_queue = DbJobQueue(engine_for_store(DATA_DIR / "kyra.db"))


def _run_latex_resume_job(payload: dict, on_progress) -> dict:
    """Job handler: same inputs and output shape as the synchronous
    latex_resume draft, plus live progress. A one-off upload was already
    saved to the document library by the enqueue endpoint, so the payload
    only carries ids and text."""
    latex_text, extra_facts, warnings = _pick_resume_source(
        payload.get("background_text", ""), payload.get("background_document_ids", ""), None
    )
    if not latex_text:
        raise ApiError(400, "no_resume_source", "nothing to optimize - pick a resume from your document library or paste it")
    if payload.get("include_github"):
        github_text, github_warnings = _fetch_github_context()
        warnings.extend(github_warnings)
        if github_text:
            extra_facts = f"{extra_facts}\n\n{github_text}" if extra_facts else github_text
    result = optimize_latex_resume_one_page(
        _resume_llm, latex_text, payload.get("job_context", ""), extra_facts, on_progress=on_progress
    )
    warnings.extend(result.guard_warnings)
    if not result.fit:
        warnings.append(
            f"NOT one page: the best attempt compiled to {result.page_count} page(s)"
            + (f" with {result.overflow_lines} line(s) over" if result.overflow_lines else "")
            + " - use Resume — Detailed to pick what to cut, or try again"
        )
    pdf_url = f"/api/job/resume-pdf/{_save_generated_pdf(result.pdf_bytes)}" if result.pdf_bytes else None
    return DraftOut(
        draft=result.latex, background_chars=len(latex_text), style_chars=0, warnings=warnings, pdf_url=pdf_url,
        fit=result.fit, page_count=result.page_count, overflow_lines=result.overflow_lines, notes=result.notes,
        questions=result.questions,
    ).model_dump()


def _apply_base_latex() -> str:
    """The .tex every mass-apply resume is tailored from (Settings.resume_base_tex). A missing file is a
    configuration error worth a clear message, not a silent fallback to something else."""
    path = Path(get_settings().resume_base_tex)
    if not path.is_absolute():
        path = DATA_DIR / path
    if not path.is_file():
        raise ApiError(409, "no_base_resume", f"base resume LaTeX not found: {path} - set KYRA_RESUME_BASE_TEX")
    return path.read_text(encoding="utf-8")


def _run_apply_job(payload: dict, on_progress) -> dict:
    """Job handler: one posting URL through apply_pipeline.run_apply_pipeline. Extra facts are the
    same Memory Notes block every resume path already reads (via _pick_resume_source with no
    resume of its own)."""
    _, extra_facts, _ = _pick_resume_source("", "", None)
    try:
        result = run_apply_pipeline(
            payload["url"], store=_job_store, resume_llm=_resume_llm, draft_llm=_draft_llm,
            base_latex=_apply_base_latex(), profile=load_profile(), engines=_autofill_engines, extra_facts=extra_facts,
            posting_text=payload.get("posting_text", ""), company=payload.get("company", ""), role=payload.get("role", ""),
            cover_letter=payload.get("cover_letter", "auto"), fetch=_fetch_posting, on_progress=on_progress,
        )
    except ApplyError as e:
        raise ApiError(400, "apply_failed", str(e)) from e
    return asdict(result)


HANDLERS: dict[str, Handler] = {"latex_resume": _run_latex_resume_job, "apply": _run_apply_job}


@app.on_event("startup")
def _start_worker() -> None:
    if get_settings().inline_worker:
        start_inline_worker(_queue, HANDLERS)
        logger.info("inline job worker started")


@app.on_event("startup")
def _warm_up_router() -> None:
    """Pay the classifier's load time before anyone is waiting on it.

    Measured 2026-09-08: the first auto-mode turn spends ~44s constructing the
    LoRA-adapted 1.5B, against ~5s for the whole warm voice loop. On a background
    thread so the server still serves immediately, and best-effort: a failed
    warm-up must never stop the app starting, since the classifier would load on
    demand anyway.
    """
    if not get_settings().warm_up_router:
        return

    def run() -> None:
        try:
            t0 = time.perf_counter()
            _router.warm()
            logger.info("router classifier warm in %.1fs", time.perf_counter() - t0)
        except Exception:  # noqa: BLE001 - the on-demand path still works
            logger.warning("router warm-up failed; it will load on the first turn", exc_info=True)

    threading.Thread(target=run, name="kyra-router-warmup", daemon=True).start()


@app.post("/api/jobs/draft")
def enqueue_draft_job(
    material_type: str = Form(...),
    job_context: str = Form(""),
    background_text: str = Form(""),
    background_document_ids: str = Form(""),
    resume: UploadFile | None = File(None),
    include_github: bool = Form(False),
) -> dict:
    """Same form as POST /api/job/draft, but returns a job id immediately.
    Only the long-running material type is a job; the others stay synchronous."""
    if material_type != "latex_resume":
        raise ApiError(400, "not_a_job", f"material_type {material_type!r} runs synchronously via /api/job/draft")
    doc_ids = [i.strip() for i in background_document_ids.split(",") if i.strip()]
    resume_text, resume_bytes, warn = _extract_upload(resume)
    if warn:
        raise ApiError(400, "unreadable_file", warn)
    if resume_text:
        saved, _ = _get_or_add_document(
            label=resume.filename, kind="resume", text=resume_text, source_filename=resume.filename, file_bytes=resume_bytes
        )
        doc_ids.insert(0, saved.id)
    job_id = _queue.enqueue("latex_resume", {
        "job_context": job_context, "background_text": background_text,
        "background_document_ids": ",".join(doc_ids), "include_github": include_github,
    })
    return {"id": job_id, "kind": "latex_resume", "status": "queued"}


class ApplyIn(BaseModel):
    urls: list[str]
    cover_letter: str = "auto"  # auto (only when the posting mentions one) | always | never
    posting_text: str = ""  # for a single non-board URL (LinkedIn, a company site) - pasted text
    company: str = ""
    role: str = ""


@app.post("/api/jobs/apply")
def enqueue_apply_jobs(body: ApplyIn) -> dict:
    """Mass apply: one queued job per posting URL. The worker runs them in order, each one
    opening its own filled browser window; nothing is submitted (apply_pipeline.py)."""
    urls = [u.strip() for u in body.urls if u.strip()]
    if not urls:
        raise ApiError(400, "no_urls", "paste at least one posting URL")
    if body.cover_letter not in ("auto", "always", "never"):
        raise ApiError(400, "bad_cover_letter", "cover_letter must be auto, always or never")
    if body.posting_text and len(urls) > 1:
        raise ApiError(400, "text_needs_one_url", "pasted posting text applies to exactly one URL")
    _apply_base_latex()  # fail now, not inside every queued job
    ids = [
        _queue.enqueue("apply", {
            "url": u, "cover_letter": body.cover_letter, "posting_text": body.posting_text,
            "company": body.company, "role": body.role,
        })
        for u in urls
    ]
    return {"jobs": [{"id": i, "url": u, "kind": "apply", "status": "queued"} for i, u in zip(ids, urls, strict=True)]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int) -> dict:
    job = _queue.get(job_id)
    if job is None:
        raise ApiError(404, "not_found", f"no job {job_id}")
    return asdict(job)


@app.get("/api/jobs/{job_id}/events")
def job_events(job_id: int):
    """Server-sent events: every new progress line as it lands, then one
    terminal 'done' (with the full result) or 'error' event. Polls the
    queue table - cheap at this volume, and it works on SQLite and Postgres
    alike; a pub/sub channel is a later optimization, not a correctness fix."""
    if _queue.get(job_id) is None:
        raise ApiError(404, "not_found", f"no job {job_id}")

    def stream():
        sent = 0
        while True:
            job = _queue.get(job_id)
            for line in job.progress[sent:]:
                yield f"event: progress\ndata: {line}\n\n"
            sent = len(job.progress)
            if job.status == "done":
                yield f"event: done\ndata: {json.dumps(job.result)}\n\n"
                return
            if job.status == "failed":
                yield f"event: error\ndata: {(job.error or 'job failed').splitlines()[0]}\n\n"
                return
            yield ": keepalive\n\n"
            time.sleep(0.7)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/job/resume-fit/analyze", response_model=ResumeFitAnalyzeOut)
def resume_fit_analyze(body: ResumeFitAnalyzeIn) -> ResumeFitAnalyzeOut:
    """First half of Detailed mode: rates every real bullet/entry/skill
    line against the job context, without editing anything - no file
    upload here, works from the document library the same way Fast mode
    does when nothing new is uploaded (_pick_resume_source(..., resume=None)).
    """
    latex_text, extra_facts, warnings = _pick_resume_source(body.background_text, body.background_document_ids, None)
    if not latex_text:
        return ResumeFitAnalyzeOut(
            warnings=warnings + ["nothing to analyze - pick a resume from your document library or paste it in Extra background"]
        )
    if "\\begin{document}" not in latex_text and "\\documentclass" not in latex_text:
        warnings.append("this doesn't look like LaTeX source (no \\documentclass/\\begin{document} found) - results may be off")

    try:
        analysis = analyze_latex_resume_fit(_resume_llm, latex_text, body.job_context)
    except ValueError as e:
        return ResumeFitAnalyzeOut(warnings=warnings + [str(e)])

    blocks_out = [FitBlockIO(**asdict(b)) for b in analysis.blocks]
    return ResumeFitAnalyzeOut(sections=analysis.sections, blocks=blocks_out, warnings=warnings)


@app.post("/api/job/resume-fit/generate", response_model=ResumeFitGenerateOut)
def resume_fit_generate(body: ResumeFitGenerateIn) -> ResumeFitGenerateOut:
    """Second half of Detailed mode: edits the LaTeX to match exactly
    what Duc checked/unchecked (the full block list + selections are
    echoed back from the frontend's analyze-step state, keeping this
    endpoint stateless like the rest of webapp.py), compiles it for
    real, and never cuts more than what was explicitly marked CUT - see
    generate_latex_from_selection()'s docstring for why overflow is
    reported (cut_suggestions), not auto-trimmed.
    """
    latex_text, extra_facts, warnings = _pick_resume_source(body.background_text, body.background_document_ids, None)
    if not latex_text:
        return ResumeFitGenerateOut(
            warnings=warnings + ["nothing to generate from - pick a resume from your document library or paste it in Extra background"]
        )

    blocks = [FitBlock(**b.model_dump()) for b in body.blocks]
    selections = [FitSelection(id=s.id, keep=s.keep) for s in body.selections]

    try:
        result = generate_latex_from_selection(_resume_llm, latex_text, blocks, selections, body.job_context, extra_facts)
    except ValueError as e:
        return ResumeFitGenerateOut(warnings=warnings + [str(e)])

    pdf_url = None
    if result.pdf_bytes:
        pdf_url = f"/api/job/resume-pdf/{_save_generated_pdf(result.pdf_bytes)}"

    warnings.extend(result.notes)
    if not result.fit and not result.cut_suggestions:
        warnings.append("didn't compile even after a fix attempt - see the draft text below")
    elif not result.fit:
        warnings.append("your selection doesn't fit one page yet - see the suggested items to cut next, lowest fit first")

    cut_out = [FitBlockIO(**asdict(b)) for b in result.cut_suggestions]
    return ResumeFitGenerateOut(
        draft=result.latex, pdf_url=pdf_url, fit=result.fit, page_count=result.page_count,
        overflow_lines=result.overflow_lines, cut_suggestions=cut_out, notes=result.notes,
        warnings=warnings + result.guard_warnings,
    )


@app.post("/api/job/draft", response_model=DraftOut)
def job_draft(
    material_type: str = Form(...),
    job_context: str = Form(""),
    background_text: str = Form(""),
    background_document_ids: str = Form(""),  # comma-separated JobDocument ids
    style_document_id: str = Form(""),
    resume: UploadFile | None = File(None),
    style_sample: UploadFile | None = File(None),
    include_github: bool = Form(False),
) -> DraftOut:
    """Pulls background from, in order: anything Kyra already durably
    knows about Duc (memory_notes.py - so a draft benefits from facts
    saved in any normal conversation, not just this panel), any
    documents picked from the library, typed-in text, and a one-off
    upload (which also gets saved into the library so it's there next
    time - the old flow lost every upload the moment the request ended).

    material_type == "latex_resume" is a different path from the
    paragraph-drafting flow: one clean LaTeX source as ground truth,
    compiled for real, returned with a PDF preview.
    """
    if material_type == "latex_resume":
        return _latex_resume_draft(
            job_context, background_text, background_document_ids, resume, include_github
        )

    warnings: list[str] = []

    background_parts = []
    notes_block = _memory_notes.render()
    if notes_block and notes_block != "(no saved notes yet)":
        background_parts.append(f"[What Kyra already knows about Duc]\n{notes_block}")

    if include_github:
        github_text, github_warnings = _fetch_github_context()
        warnings.extend(github_warnings)
        if github_text:
            background_parts.append(github_text)

    doc_ids = [i.strip() for i in background_document_ids.split(",") if i.strip()]
    for doc in _job_documents.get_many(doc_ids):
        background_parts.append(f"[{doc.label}]\n{doc.text}")

    if background_text.strip():
        background_parts.append(background_text.strip())

    resume_text, resume_bytes, warn = _extract_upload(resume)
    if warn:
        warnings.append(warn)
    if resume_text:
        saved, is_new = _get_or_add_document(
            label=resume.filename, kind="resume", text=resume_text, source_filename=resume.filename, file_bytes=resume_bytes
        )
        background_parts.append(f"[{saved.label}]\n{resume_text}")
        if is_new:
            warnings.append(f"saved '{saved.label}' to your document library (PROFILE tab → Document Library) for reuse")
        else:
            warnings.append(f"'{saved.label}' matches something already in your document library - reused it, not duplicated")

    background = "\n\n".join(background_parts)
    if not background:
        warnings.append("no background given - draft will be generic. Paste some background, pick a saved document, or upload a resume.")

    style_text = ""
    if style_document_id:
        docs = _job_documents.get_many([style_document_id])
        if docs:
            style_text = docs[0].text
        else:
            warnings.append(f"style document {style_document_id!r} not found")
    style_upload_text, style_upload_bytes, warn = _extract_upload(style_sample)
    if warn:
        warnings.append(warn)
    if style_upload_text:
        saved, is_new = _get_or_add_document(
            label=style_sample.filename, kind="style_sample", text=style_upload_text,
            source_filename=style_sample.filename, file_bytes=style_upload_bytes,
        )
        style_text = style_text or style_upload_text
        if is_new:
            warnings.append(f"saved '{saved.label}' to your document library (PROFILE tab → Document Library) for reuse")
        else:
            warnings.append(f"'{saved.label}' matches something already in your document library - reused it, not duplicated")

    draft = draft_application_material(
        _draft_llm, material_type=material_type, job_context=job_context, background=background, style_sample=style_text
    )
    return DraftOut(draft=draft, background_chars=len(background), style_chars=len(style_text), warnings=warnings)


# ---- document library ----


class JobDocumentOut(BaseModel):
    id: str
    label: str
    kind: str
    text: str
    source_filename: str | None
    added_at: str
    file_path: str | None = None


@app.get("/api/job/documents")
def list_job_documents() -> dict:
    return {"documents": [asdict(d) for d in _job_documents.list()]}


@app.post("/api/job/documents")
def add_job_document(
    label: str = Form(""),
    kind: str = Form(...),
    file: UploadFile | None = File(None),
    text: str = Form(""),
) -> dict:
    """Add a document either from an uploaded file or pasted text
    directly - covers both "upload another resume" and "here's a
    written summary of a past project" in one endpoint.
    """
    body_text = text.strip()
    filename = None
    file_bytes = None
    if file is not None and file.filename:
        filename = file.filename
        extracted, file_bytes, warn = _extract_upload(file)
        if warn:
            raise ApiError(400, "unreadable_file", warn)
        body_text = extracted
    if not body_text:
        raise ApiError(400, "empty_document", "no text or file content given")
    try:
        doc, is_new = _get_or_add_document(label=label, kind=kind, text=body_text, source_filename=filename, file_bytes=file_bytes)
    except ValueError as e:
        raise ApiError(400, "invalid_document", str(e)) from e
    result = asdict(doc)
    result["reused_existing"] = not is_new
    return result


@app.delete("/api/job/documents/{doc_id}")
def delete_job_document(doc_id: str) -> dict:
    ok = _job_documents.delete(doc_id)
    return {"deleted": ok}


# ---- applicant profile ----


class ProfileIn(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    country: str | None = None
    linkedin_url: str | None = None
    github_url: str | None = None
    portfolio_url: str | None = None
    twitter_url: str | None = None
    current_company: str | None = None
    preferred_name: str | None = None
    pronouns: str | None = None
    resume_path: str | None = None
    eeo_gender_identity: str | None = None
    eeo_race_ethnicity: str | None = None
    eeo_hispanic_latino: str | None = None
    eeo_veteran_status: str | None = None
    eeo_disability_status: str | None = None


@app.get("/api/profile")
def get_profile() -> dict:
    """Transparency endpoint - the full current profile record, exactly
    as it'll be used for autofill. Nothing hidden.
    """
    profile = load_profile()
    return {"profile": asdict(profile), "missing_for_autofill": profile.is_ready_for_autofill()}


@app.post("/api/profile")
def update_profile(body: ProfileIn) -> dict:
    """Partial update - only fields actually sent get changed, so the
    UI can save one field at a time without clobbering the rest.
    """
    profile = load_profile()
    for field_name, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(profile, field_name, value)
    save_profile(profile)
    return {"profile": asdict(profile), "missing_for_autofill": profile.is_ready_for_autofill()}


class JobApplicationOut(BaseModel):
    id: int
    company: str
    role: str
    link: str | None
    status: str
    notes: str | None
    created_at: str
    updated_at: str


@app.get("/api/job/applications")
def list_job_applications(status: str | None = None) -> dict:
    return {"applications": [asdict(a) for a in _job_store.list(status)]}


class AddJobApplicationIn(BaseModel):
    company: str
    role: str
    link: str | None = None
    notes: str | None = None


@app.post("/api/job/applications")
def add_job_application(body: AddJobApplicationIn) -> dict:
    return asdict(_job_store.add(body.company, body.role, body.link, body.notes))


class UpdateJobApplicationStatusIn(BaseModel):
    id: int
    status: str
    notes: str | None = None


@app.post("/api/job/applications/status")
def update_job_application_status(body: UpdateJobApplicationStatusIn) -> dict:
    if body.status not in VALID_STATUSES:
        raise ApiError(400, "invalid_status", f"status must be one of {sorted(VALID_STATUSES)}", {"allowed": sorted(VALID_STATUSES)})
    result = _job_store.update_status(body.id, body.status, body.notes)
    if not result:
        raise ApiError(404, "not_found", f"no application with id {body.id}")
    return asdict(result)


class AutofillIn(BaseModel):
    url: str


@app.post("/api/job/autofill")
def job_autofill(body: AutofillIn) -> dict:
    profile = load_profile()
    missing = profile.is_ready_for_autofill()
    if missing:
        raise ApiError(409, "profile_incomplete", "profile isn't ready for autofill", {"missing_fields": missing})
    # Route by the posting's ATS instead of assuming Greenhouse: the Autofill tab
    # is where Duc pastes any posting URL, and most of the ones he tracks are Ashby.
    engine, ats = engine_for_url(body.url, _autofill_engines)
    if engine is None:
        raise ApiError(
            400, "unsupported_ats", f"no autofill engine for {ats} postings yet - fill this one by hand",
            {"supported": sorted(_autofill_engines)},
        )
    # The company-tailored resume, when the tracker has one for this posting.
    tailored, why = resume_for_url(body.url, _job_store.list())
    if tailored and Path(tailored).is_file():
        profile = replace(profile, resume_path=tailored)
    try:
        report = engine.fill(body.url, profile)
    except Exception as e:
        raise ApiError(502, "autofill_failed", f"autofill failed: {e}") from e
    return {
        "url": body.url,
        "filled": [asdict(f) for f in report.filled],
        "skipped": [asdict(s) for s in report.skipped],
        "resume_attached": Path(profile.resume_path).name,
        "resume_choice": why,
        "summary_path": report.summary_path,
    }


# ---------------- TOOLS panel ----------------
# reminders/news/science/learning - built earlier, only ever reachable
# through typed/spoken chat until now. Same direct-endpoint pattern as
# JOBS: a UI button already knows what it wants.


# ---- outreach (JOBS panel, Outreach tab) ----
# These go through the same tool objects the chat path uses rather than calling
# OutreachStore directly, because the logic that matters lives in the tools:
# drafting reads the linked application's status so a note can never claim Duc
# applied when he is only targeting, and marking a contact `sent` schedules the
# follow-up reminder that the digest then surfaces. Two front doors, one
# implementation - the same reason default_tool_registry() exists, after the
# three front doors really did drift apart once.


def _outreach(tool: str, **kwargs) -> dict:
    """Run an outreach tool and turn its error convention into an HTTP one.

    A tool returns {"error": ...} because that is what a model reads; an
    endpoint must not answer 200 with an error body (CLAUDE.md), so a missing
    contact becomes 404 and everything else a 400 with the tool's own message.
    """
    out = _registry.run(tool, **kwargs)
    if isinstance(out, dict) and "error" in out:
        message = out["error"]
        status = 404 if "no outreach contact with id" in message else 400
        raise ApiError(status, "outreach_invalid", message)
    return out


class OutreachAddIn(BaseModel):
    name: str
    company: str
    role: str | None = None
    profile_url: str | None = None
    relation: str | None = None
    application_id: int | None = None


class OutreachDraftIn(BaseModel):
    job_context: str = ""
    mutual_connections: str = ""
    personal_angle: str = ""


class OutreachCopyIn(BaseModel):
    which: str = "note"
    open_profile: bool = False


class OutreachStatusIn(BaseModel):
    status: str


@app.get("/api/outreach")
def list_outreach(status: str | None = None, company: str | None = None, due_only: bool = False) -> dict:
    return _outreach("list_outreach", company=company or None, status=status or None, due_only=due_only)


@app.post("/api/outreach")
def add_outreach(body: OutreachAddIn) -> dict:
    # The add tool returns the contact's fields flat while the status tool wraps
    # them; wrap here so every outreach response has the same shape. (`name` can
    # be passed as a keyword because ToolRegistry.run takes the tool name
    # positional-only, which exists for exactly this tool.)
    try:
        return {"contact": _outreach("add_outreach_contact", **body.model_dump())}
    except ValueError as e:  # the store rejects a blank name or company
        raise ApiError(400, "outreach_invalid", str(e)) from e


@app.post("/api/outreach/{contact_id}/draft")
def draft_outreach(contact_id: int, body: OutreachDraftIn) -> dict:
    """Slow on purpose: a draft plus the humanizer critique pass is two or
    three real Claude calls. Synchronous, unlike the resume fit loop - that
    one runs for minutes and needs the job queue; this is tens of seconds."""
    return _outreach("draft_outreach_note", id=contact_id, **body.model_dump())


@app.post("/api/outreach/{contact_id}/copy")
def copy_outreach(contact_id: int, body: OutreachCopyIn) -> dict:
    return _outreach("copy_outreach_note", id=contact_id, **body.model_dump())


@app.post("/api/outreach/{contact_id}/status")
def set_outreach_status(contact_id: int, body: OutreachStatusIn) -> dict:
    return _outreach("update_outreach_status", id=contact_id, status=body.status)


@app.get("/api/reminders")
def list_reminders(include_done: bool = False) -> dict:
    return {"reminders": [asdict(r) for r in _reminders_store.list(include_done)]}


class AddReminderIn(BaseModel):
    text: str
    due_at: str | None = None


@app.post("/api/reminders")
def add_reminder(body: AddReminderIn) -> dict:
    return asdict(_reminders_store.add(body.text, body.due_at))


@app.post("/api/reminders/{reminder_id}/complete")
def complete_reminder(reminder_id: int) -> dict:
    return {"ok": _reminders_store.complete(reminder_id)}


class SnoozeReminderIn(BaseModel):
    due_at: str


@app.post("/api/reminders/{reminder_id}/snooze")
def snooze_reminder(reminder_id: int, body: SnoozeReminderIn) -> dict:
    return {"ok": _reminders_store.snooze(reminder_id, body.due_at)}


@app.get("/api/news")
def get_news(per_source: int = 3) -> dict:
    return _news_tool.run(per_source=per_source)


@app.get("/api/science")
def get_science(per_source: int = 2) -> dict:
    return _science_tool.run(per_source=per_source)


@app.get("/api/learning/due")
def learning_due() -> dict:
    return {"due": [asdict(i) for i in _learning_store.due()]}


class AddLearningItemIn(BaseModel):
    topic: str
    summary: str
    key_takeaway: str


@app.post("/api/learning")
def add_learning_item(body: AddLearningItemIn) -> dict:
    return asdict(_learning_store.add(body.topic, body.summary, body.key_takeaway))


class MarkReviewedIn(BaseModel):
    remembered: bool


@app.post("/api/learning/{item_id}/review")
def mark_learning_reviewed(item_id: int, body: MarkReviewedIn) -> dict:
    result = _learning_store.mark_reviewed(item_id, body.remembered)
    if not result:
        raise ApiError(404, "not_found", f"no learning item with id {item_id}")
    return result
