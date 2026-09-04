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
from companion.github_profile import extract_username as extract_github_username
from companion.github_profile import fetch_github_projects
from companion.job_applications import (
    VALID_STATUSES,
    JobApplicationStore,
    draft_application_material,
    optimize_full_resume,
    optimize_latex_resume_one_page,
)
from companion.job_autofill import GreenhouseAutofillEngine
from companion.job_documents import JobDocumentStore
from companion.learning import LearningStore
from companion.llm import AnthropicLLM, LazyBackends, build_llm
from companion.memory import ChromaMemoryStore
from companion.memory_notes import MarkdownMemoryNotesStore
from companion.news import TechNewsTool
from companion.persona import KYRA
from companion.profile import load_profile, save_profile
from companion.reminders import RemindersStore
from companion.resume_format import ResumeFormatError
from companion.resume_pdf import OUTPUT_DIR as RESUME_PDF_DIR
from companion.resume_pdf import render_pdf
from companion.router import TurnRouter, route_and_answer_verbose
from companion.science import ScienceFactsTool
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
_resume_llm = AnthropicLLM(Anthropic(api_key=require_api_key()), max_tokens=16000)
_autofill_engine = GreenhouseAutofillEngine()
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
    pdf_url: str | None = None  # set for material_type in {"full_resume", "latex_resume"}


def _resume_doc_preview(doc) -> str:
    """Flattens a ResumeDoc into readable plain text for the UI's text
    preview - the PDF is the real deliverable, this is just so the
    existing output box shows something meaningful instead of nothing.
    """
    lines = [doc.name]
    if doc.contact:
        lines.append(doc.contact)
    for section in doc.sections:
        lines.append(f"\n{section.title.upper()}")
        for entry in section.entries:
            header = " | ".join(p for p in (entry.heading, entry.date) if p)
            if header:
                lines.append(header)
            if entry.subheading:
                lines.append(entry.subheading)
            for bullet in entry.bullets:
                lines.append(f"- {bullet}")
    return "\n".join(lines)


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
        data = upload.file.read()
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
    directory render_pdf() uses, so both resume paths serve through the
    one /api/job/resume-pdf/{filename} endpoint. Returns the filename
    (not the full path) for building that URL.
    """
    import uuid

    RESUME_PDF_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex[:12]}.pdf"
    (RESUME_PDF_DIR / filename).write_bytes(pdf_bytes)
    return filename


def _full_resume_draft(
    job_context: str, background_text: str, background_document_ids: str, resume: UploadFile | None,
    include_github: bool = False,
) -> DraftOut:
    resume_text, extra_facts, warnings = _pick_resume_source(background_text, background_document_ids, resume)

    if not resume_text:
        return DraftOut(
            draft="", background_chars=0, style_chars=0,
            warnings=warnings + ["nothing to optimize - upload a resume, pick one from your document library, or paste its text in Extra background"],
        )

    if include_github:
        github_text, github_warnings = _fetch_github_context()
        warnings.extend(github_warnings)
        if github_text:
            extra_facts = f"{extra_facts}\n\n{github_text}" if extra_facts else github_text

    try:
        doc = optimize_full_resume(_resume_llm, resume_text, job_context, extra_facts)
    except ResumeFormatError as e:
        return DraftOut(draft="", background_chars=len(resume_text), style_chars=0, warnings=warnings + [str(e)])

    pdf_path = render_pdf(doc)
    pdf_filename = Path(pdf_path).name
    preview = _resume_doc_preview(doc)
    return DraftOut(
        draft=preview, background_chars=len(resume_text), style_chars=0,
        warnings=warnings, pdf_url=f"/api/job/resume-pdf/{pdf_filename}",
    )


def _latex_resume_draft(
    job_context: str, background_text: str, background_document_ids: str, resume: UploadFile | None,
    include_github: bool = False,
) -> DraftOut:
    """Edits Duc's own LaTeX source directly, then actually compiles it
    (latex_compile.py) and iterates until it fits exactly one real page
    - see job_applications.py::optimize_latex_resume_one_page for why
    this needs a real compile-measure-iterate loop rather than a single
    blind rewrite. Editing the source directly (rather than going
    through resume_format.py/resume_pdf.py) also sidesteps a real
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

    warnings.extend(result.notes)
    if not result.fit:
        warnings.append(
            "couldn't automatically confirm a one-page fit - review the compiled PDF and adjust further yourself if needed"
        )

    pdf_url = None
    if result.pdf_bytes:
        pdf_url = f"/api/job/resume-pdf/{_save_generated_pdf(result.pdf_bytes)}"

    return DraftOut(
        draft=result.latex, background_chars=len(latex_text), style_chars=0, warnings=warnings, pdf_url=pdf_url,
    )


@app.get("/api/job/resume-pdf/{filename}")
def get_resume_pdf(filename: str):
    # filename comes straight from the URL path - reject anything that
    # isn't a bare generated-uuid.pdf name before touching the filesystem,
    # so this can't be used to read arbitrary files (e.g. "../../.env").
    if "/" in filename or "\\" in filename or not filename.endswith(".pdf"):
        return {"error": "invalid filename"}
    path = RESUME_PDF_DIR / filename
    if not path.is_file():
        return {"error": "not found"}
    return FileResponse(path, media_type="application/pdf", filename="optimized_resume.pdf")


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

    material_type == "full_resume" is a genuinely different path, not
    a variant of the paragraph-drafting flow above: it needs exactly
    one clean original resume as ground truth, not several sources
    blended together, and it returns a rendered PDF rather than text
    for the output box to display.
    """
    if material_type == "full_resume":
        return _full_resume_draft(
            job_context, background_text, background_document_ids, resume, include_github
        )
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
            return {"error": warn}
        body_text = extracted
    if not body_text:
        return {"error": "no text or file content given"}
    try:
        doc, is_new = _get_or_add_document(label=label, kind=kind, text=body_text, source_filename=filename, file_bytes=file_bytes)
    except ValueError as e:
        return {"error": str(e)}
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


# ---------------- TOOLS panel ----------------
# reminders/news/science/learning - built earlier, only ever reachable
# through typed/spoken chat until now. Same direct-endpoint pattern as
# JOBS: a UI button already knows what it wants.


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
    return result if result else {"error": f"no learning item with id {item_id}"}
