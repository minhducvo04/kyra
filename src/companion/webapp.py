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
from dataclasses import asdict
from pathlib import Path

from anthropic import Anthropic
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from companion.config import require_api_key
from companion.conversation import ConversationManager
from companion.default_tools import default_tool_registry
from companion.doc_text import UnsupportedDocumentType, extract_text
from companion.job_applications import VALID_STATUSES, JobApplicationStore, draft_application_material
from companion.job_autofill import GreenhouseAutofillEngine
from companion.job_documents import JobDocumentStore
from companion.llm import AnthropicLLM, LazyBackends, build_llm
from companion.memory import ChromaMemoryStore
from companion.memory_notes import MarkdownMemoryNotesStore
from companion.persona import KYRA
from companion.profile import load_profile, save_profile
from companion.router import TurnRouter, route_and_answer_verbose
from companion.voice import FasterWhisperSTT, KokoroTTS, decode_uploaded_audio, encode_wav_bytes

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"

app = FastAPI(title="Kyra")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

_memory = ChromaMemoryStore()
_claude = build_llm("claude")
_conversation = ConversationManager(persona=KYRA, memory=_memory, llm=_claude)
_backends = LazyBackends(claude=_claude)
_registry = default_tool_registry()
_router = TurnRouter(_registry)
_current_backend = "auto"  # "claude" | "local" | "auto" - auto (the router) is the default now that it exists
_stt = FasterWhisperSTT()
_tts = KokoroTTS()

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
_autofill_engine = GreenhouseAutofillEngine()
_job_documents = JobDocumentStore()
_memory_notes = MarkdownMemoryNotesStore()


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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/backend")
def get_backend() -> dict:
    return {"backend": _current_backend}


@app.post("/api/backend")
def set_backend(body: BackendIn) -> dict:
    global _current_backend
    name = body.backend
    if name not in ("claude", "local", "auto"):
        return {"error": f"unknown backend {name!r}"}
    if name in ("claude", "local"):
        _conversation.llm = _backends[name]  # first switch to local loads the model - can take a while
    _current_backend = name
    return {"backend": _current_backend}


def _answer(message: str) -> ChatOut:
    """Shared by /api/chat and /api/voice - text in, routed reply out. The
    only thing voice adds on top is transcribing in and synthesizing out.
    """
    if _current_backend != "auto":
        reply = _conversation.handle_turn(message)
        return ChatOut(reply=reply, backend=_current_backend)

    reply, decision = route_and_answer_verbose(message, _conversation, _router, _backends, _registry)
    if decision is None:  # a mode-switch command ("focus mode" etc.), not a routed turn
        return ChatOut(reply=reply, backend="auto")
    return ChatOut(
        reply=reply, backend="auto", actual_backend=decision.backend, path=decision.path, reason=decision.reason
    )


@app.post("/api/chat", response_model=ChatOut)
def chat(body: ChatIn) -> ChatOut:
    return _answer(body.message)


@app.post("/api/voice", response_model=VoiceOut)
def voice(audio: UploadFile) -> VoiceOut:
    """One mic recording in, one spoken reply out - push-to-talk and
    hands-free in the web UI both hit this same endpoint per utterance;
    the difference is only how the browser decides when to call it.
    """
    pcm = decode_uploaded_audio(audio.file)
    transcript = _stt.transcribe(pcm).strip()
    if not transcript:
        return VoiceOut(
            reply="", backend=_current_backend, transcript="", reply_audio_b64="", reply_audio_rate=0
        )

    chat_out = _answer(transcript)
    reply_audio, rate = _tts.speak(chat_out.reply)
    wav_bytes = encode_wav_bytes(reply_audio, rate)

    return VoiceOut(
        **chat_out.model_dump(),
        transcript=transcript,
        reply_audio_b64=base64.b64encode(wav_bytes).decode("ascii"),
        reply_audio_rate=rate,
    )


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


def _extract_upload(upload: UploadFile | None) -> tuple[str, str | None]:
    """Returns (text, warning). A warning (not an exception) on an
    unsupported/unreadable file, so one bad upload doesn't 500 the whole
    request - the draft can still proceed on whatever else was given.
    """
    if upload is None or not upload.filename:
        return "", None
    try:
        data = upload.file.read()
        return extract_text(upload.filename, data), None
    except UnsupportedDocumentType as e:
        return "", f"{upload.filename}: {e}"
    except Exception as e:
        return "", f"{upload.filename}: couldn't read file ({e})"


@app.post("/api/job/draft", response_model=DraftOut)
def job_draft(
    material_type: str = Form(...),
    job_context: str = Form(""),
    background_text: str = Form(""),
    background_document_ids: str = Form(""),  # comma-separated JobDocument ids
    style_document_id: str = Form(""),
    resume: UploadFile | None = File(None),
    style_sample: UploadFile | None = File(None),
) -> DraftOut:
    """Pulls background from, in order: anything Kyra already durably
    knows about Duc (memory_notes.py - so a draft benefits from facts
    saved in any normal conversation, not just this panel), any
    documents picked from the library, typed-in text, and a one-off
    upload (which also gets saved into the library so it's there next
    time - the old flow lost every upload the moment the request ended).
    """
    warnings: list[str] = []

    background_parts = []
    notes_block = _memory_notes.render()
    if notes_block and notes_block != "(no saved notes yet)":
        background_parts.append(f"[What Kyra already knows about Duc]\n{notes_block}")

    doc_ids = [i.strip() for i in background_document_ids.split(",") if i.strip()]
    for doc in _job_documents.get_many(doc_ids):
        background_parts.append(f"[{doc.label}]\n{doc.text}")

    if background_text.strip():
        background_parts.append(background_text.strip())

    resume_text, warn = _extract_upload(resume)
    if warn:
        warnings.append(warn)
    if resume_text:
        saved = _job_documents.add(label=resume.filename, kind="resume", text=resume_text, source_filename=resume.filename)
        background_parts.append(f"[{saved.label}]\n{resume_text}")
        warnings.append(f"saved '{saved.label}' to your document library for reuse")

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
    style_upload_text, warn = _extract_upload(style_sample)
    if warn:
        warnings.append(warn)
    if style_upload_text:
        saved = _job_documents.add(
            label=style_sample.filename, kind="style_sample", text=style_upload_text, source_filename=style_sample.filename
        )
        style_text = style_text or style_upload_text
        warnings.append(f"saved '{saved.label}' to your document library for reuse")

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
    if file is not None and file.filename:
        filename = file.filename
        extracted, warn = _extract_upload(file)
        if warn:
            return {"error": warn}
        body_text = extracted
    if not body_text:
        return {"error": "no text or file content given"}
    try:
        doc = _job_documents.add(label=label, kind=kind, text=body_text, source_filename=filename)
    except ValueError as e:
        return {"error": str(e)}
    return asdict(doc)


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
        return {"error": f"status must be one of {sorted(VALID_STATUSES)}"}
    result = _job_store.update_status(body.id, body.status, body.notes)
    return asdict(result) if result else {"error": f"no application with id {body.id}"}


class AutofillIn(BaseModel):
    url: str


@app.post("/api/job/autofill")
def job_autofill(body: AutofillIn) -> dict:
    profile = load_profile()
    missing = profile.is_ready_for_autofill()
    if missing:
        return {"error": "profile isn't ready for autofill", "missing_fields": missing}
    try:
        report = _autofill_engine.fill(body.url, profile)
    except Exception as e:
        return {"error": f"autofill failed: {e}"}
    return {
        "url": body.url,
        "filled": [asdict(f) for f in report.filled],
        "skipped": [asdict(s) for s in report.skipped],
        "summary_path": report.summary_path,
    }
