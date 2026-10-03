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
from datetime import UTC, datetime, timedelta
from functools import cache, cached_property
from ipaddress import ip_address
from pathlib import Path
from typing import Literal
from urllib.parse import unquote
from uuid import UUID

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

from companion import session_log, webauth
from companion.apply_pipeline import ApplyError, engine_for_url, fill_with_receipt, run_apply_pipeline
from companion.approvals import ApprovalError, ApprovalNotFound, ApprovalRequired
from companion.atlas import load_atlas
from companion.brief import Reply, TextStream, check, short_name
from companion.checkpoints import CheckpointConflict, CheckpointDraft, CheckpointStore, DbCheckpointStore
from companion.config import require_api_key
from companion.conversation import ConversationManager
from companion.db import engine_for_store
from companion.default_tools import default_tool_registry
from companion.doc_text import UnsupportedDocumentType, extract_text
from companion.errors import ApiError, install_error_handlers
from companion.features import feature_map, load_features
from companion.focus import (
    FocusBlockRunning,
    FocusPlan,
    FocusStore,
    NoActiveFocusBlock,
    ScheduledPlanner,
    theme_for,
)
from companion.github_profile import extract_username as extract_github_username
from companion.github_profile import fetch_github_projects
from companion.ideas import IdeaDraft, IdeaRequestConflict, IdeaStore, build_desk, load_desk, resolve_draft
from companion.input_labels import default_input_labeller
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
from companion.learning import LearningRequestConflict, LearningStore, StaleReview
from companion.lessons import check_answer, list_lessons
from companion.llm import AnthropicLLM, TurnCancelled, build_anthropic_client, build_llm, voice_backends
from companion.memory import ChromaMemoryStore
from companion.memory_map import build_map
from companion.memory_notes import SUGGESTED_CATEGORIES, MarkdownMemoryNotesStore, NotesVersionConflict
from companion.news import TechNewsTool
from companion.outbound import OutboundAudit, ReleaseRefused, default_gate
from companion.outbound import report as outbound_report
from companion.paths import DATA_DIR, WEB_DIR, write_json
from companion.paths import GENERATED_RESUMES_DIR as RESUME_PDF_DIR
from companion.persona import KYRA
from companion.privacy import PrivacyClass, Tier
from companion.profile import load_profile, save_profile
from companion.provider import AuditUnavailable, current_release_label, release_label, run_in_scope
from companion.reminders import RemindersStore
from companion.router import TurnRouter, approval_reply, handle_mode_command, route_and_answer_verbose
from companion.science import ScienceFactsTool
from companion.session_state import get_mode
from companion.settings import get_settings
from companion.space import DeviceReader, GestureLedger, GestureRefused, RoomRegistry, Thing, identity_of, map_gesture
from companion.system_map import LANES, load_system_map
from companion.voice_text import SpokenBudget, spoken_reply, spoken_text, take_sentences

_input_labeller = default_input_labeller()

app = FastAPI(title="Kyra")
install_error_handlers(app)

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def serve_args(host: str, port: int, tls_cert: str, tls_key: str, *, settings) -> dict:
    """Validate the listener policy before uvicorn binds; never resolve names or read keys."""
    from companion.working_loop import PolicyRefused

    try:
        loopback = ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    if not loopback and not settings.api_token.strip():
        raise PolicyRefused("token_required")
    if bool(tls_cert) != bool(tls_key):
        raise PolicyRefused("tls_pair_required")
    if not loopback and not tls_cert and not settings.tls_terminated_upstream:
        raise PolicyRefused("tls_required")
    args = {"host": host, "port": port, "log_level": settings.log_level.lower(),
            "proxy_headers": False}  # Authorization uses the socket peer, never forwarded headers.
    if tls_cert:
        args.update(ssl_certfile=tls_cert, ssl_keyfile=tls_key)
    return args


# Reachable without authenticating, and each for a reason: /healthz so an
# orchestrator can tell a live task from a dead one (gating it makes every
# deploy restart-loop the moment a token is set), /login and /api/login or
# there is no way to obtain a session, /static because the login page needs
# its stylesheet and neither asset is a secret.
_OPEN_PATHS = {"/healthz", "/login", "/api/login"}
# These endpoints authenticate their own device/bridge credentials, never the owner token.
_CAST_CREDENTIAL_PATHS = {"/api/cast/session", "/api/cast/bridge/redeem", "/api/cast/bridge/ticket",
                          "/api/cast/bridge/verify", "/api/cast/bridge/check", "/api/cast/bridge/close",
                          "/api/cast/bridge/health", "/api/cast/bridge/release"}
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
    settings = get_settings()
    token = settings.api_token
    path = request.url.path
    if (path.rstrip("/") == "/busy" or path == "/api/busy" or path.startswith("/api/busy/")
            or path in {"/static/busy.html", "/static/busy.js", "/static/busy.css"}
            or path.startswith("/busy/") or path.startswith("/static/busy-")):
        if settings.tenant != "busy":
            return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": "Not found"}})
    if (not token or path in _OPEN_PATHS or path.startswith("/static/")
            or (request.method == "POST" and path in _CAST_CREDENTIAL_PATHS)
            or (request.method == "GET" and (path == "/api/cast/bridge/opens" or path.startswith("/cast/artifacts/")))):
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
        return ConversationManager(persona=KYRA, memory=self.memory, llm=_claude, gate=default_gate())

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
_draft_llm = AnthropicLLM(build_anthropic_client(api_key=require_api_key()), max_tokens=2500)
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
# ~4.5k tokens, and 16000 started failing on it with the truncation marker,
# the same way 6000 failed once the resume passed ~4k. This is
# a ceiling, not a reservation: raising it costs nothing unless it is used.
# 40000 (2026-09-07): 32000 was already past the SDK's non-streaming limit (~21k tokens), so
# respond() streams now and the ceiling is free to sit well above any observed output (the
# largest real fit-loop reply so far was ~13.8k output tokens on ~10k of input).
_resume_llm = AnthropicLLM(build_anthropic_client(api_key=require_api_key()), max_tokens=40000)
_autofill_engine = GreenhouseAutofillEngine()
# apply_pipeline routes by the posting URL's ATS; add a Lever/Ashby engine here when one exists.
# One engine per supported ATS, keyed the way job_posting_fetch parses a posting URL.
# Ashby uses the same registry after verification against rendered forms.
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
    rest: str = ""
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


def _busy_store():
    from companion.busy import BusyTaskStore, WorkflowStore

    settings = get_settings()
    if settings.tenant != "busy":
        raise ApiError(404, "not_found", "Not found")
    root = settings.data_dir.resolve()
    return BusyTaskStore(root, WorkflowStore(root))


class BusyTaskIn(BaseModel):
    slug: str
    facts: dict[str, str]


class BusyFactsIn(BaseModel):
    facts: dict[str, str]


class BusyDecisionIn(BaseModel):
    decision: str
    note: str = ""


@app.get("/busy")
def busy_page() -> FileResponse:
    if get_settings().tenant != "busy":
        raise ApiError(404, "not_found", "Not found")
    return FileResponse(WEB_DIR / "busy.html")


@app.get("/busy/office")
@app.get("/busy/assistant")
def busy_office_page() -> FileResponse:
    """One Busy shell with tabs; /busy/assistant opens it on the Trợ lý tab (busy-office.js reads the path)."""
    if get_settings().tenant != "busy":
        raise ApiError(404, "not_found", "Not found")
    return FileResponse(WEB_DIR / "busy-office.html")


@app.get("/api/busy/workflows")
def busy_workflows() -> dict:
    return {"workflows": [{"slug": v.slug, "version": v.version, "title": v.title,
                           "required_facts": v.required_facts} for v in _busy_store().workflows.list()]}


@app.get("/api/busy/tasks")
def busy_tasks_list() -> dict:
    return {"tasks": [asdict(task) for task in _busy_store().list()]}


@app.post("/api/busy/tasks")
def busy_start_task(body: BusyTaskIn) -> dict:
    store = _busy_store()
    try:
        return asdict(store.start_task(body.slug, body.facts))
    except ValueError as exc:
        raise ApiError(400, "invalid_workflow", str(exc)) from exc


@app.get("/api/busy/tasks/{task_id}")
def busy_task(task_id: int) -> dict:
    store = _busy_store()
    task = store.get(task_id)
    if task is None:
        raise ApiError(404, "not_found", "Task not found")
    version = store.workflows.get(task.slug, task.version)
    return {**asdict(task), "required_facts": version.required_facts if version else list(task.facts)}


@app.put("/api/busy/tasks/{task_id}/facts")
def busy_update_facts(task_id: int, body: BusyFactsIn) -> dict:
    store = _busy_store()
    try:
        return asdict(store.update_facts(task_id, body.facts))
    except KeyError as exc:
        raise ApiError(404, "not_found", "Task not found") from exc
    except ValueError as exc:
        raise ApiError(409, "task_changed", str(exc)) from exc


@app.post("/api/busy/tasks/{task_id}/decision")
def busy_decide(task_id: int, body: BusyDecisionIn) -> dict:
    from companion.busy import ApprovalRefused

    store = _busy_store()
    try:
        return asdict(store.decide(task_id, body.decision, body.note))
    except ApprovalRefused as exc:
        raise ApiError(409, "approval_refused", str(exc)) from exc
    except KeyError as exc:
        raise ApiError(404, "not_found", "Task not found") from exc
    except ValueError as exc:
        raise ApiError(409, "invalid_decision", str(exc)) from exc


@app.get("/api/busy/tasks/{task_id}/pages/{page_number}")
def busy_page_image(task_id: int, page_number: int) -> FileResponse:
    store = _busy_store()
    task = store.get(task_id)
    if task is None or not 1 <= page_number <= len(task.report["rendered_pages"]):
        raise ApiError(404, "not_found", "Page not found")
    path = Path(task.report["rendered_pages"][page_number - 1]).resolve()
    if not path.is_relative_to(store.out_dir.resolve()) or not path.is_file():
        raise ApiError(404, "not_found", "Page not found")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})


_busy_assistant_instance = None


def _busy_assistant():
    """One Assistant per data dir (it holds the lazily built chat client); tests swap it out."""
    global _busy_assistant_instance
    from companion.busy_assistant import Assistant

    settings = get_settings()
    if settings.tenant != "busy":
        raise ApiError(404, "not_found", "Not found")
    root = settings.data_dir.resolve()
    if _busy_assistant_instance is None or _busy_assistant_instance.data_dir != root:
        _busy_assistant_instance = Assistant(root, office=_busy_office)
    return _busy_assistant_instance


def _assistant_call(fn, *args):
    from companion.busy_assistant import AssistantRefused
    from companion.llm import ProviderUnavailable

    try:
        return fn(*args)
    except AssistantRefused as exc:
        raise ApiError(400, "refused", str(exc)) from exc
    except ProviderUnavailable as exc:
        raise ApiError(503, "model_unavailable", "Trợ lý tạm thời không trả lời được, bác thử lại sau") from exc


class BusyAssistantChatIn(BaseModel):
    message: str
    history: list[dict] = []
    path: str | None = None


class BusyAssistantApplyIn(BaseModel):
    id: str


class BusyAssistantFeedbackIn(BaseModel):
    text: str


@app.get("/api/busy/assistant/files")
def busy_assistant_files(path: str = "") -> dict:
    assistant = _busy_assistant()
    return _assistant_call(assistant.folder.list, path)


@app.get("/api/busy/assistant/file")
def busy_assistant_file(path: str) -> dict:
    assistant = _busy_assistant()
    return _assistant_call(assistant.folder.preview, path, True)


@app.get("/api/busy/assistant/sheet")
def busy_assistant_sheet(path: str, sheet: str, start: int = 1) -> dict:
    """More rows of one sheet for the grid (100 at a time)."""
    from companion.busy_assistant import xlsx_grid

    assistant = _busy_assistant()
    return _assistant_call(lambda: xlsx_grid(assistant.folder.resolve(path), sheet, max(1, start))[0])


class BusyCellsIn(BaseModel):
    path: str
    sheet: str
    changes: list[dict]
    overwrite_formulas: bool = False


class BusySheetIn(BaseModel):
    path: str
    op: str
    sheet: str | None = None
    name: str | None = None
    date: str | None = None
    check: bool = False


@app.post("/api/busy/assistant/cells")
def busy_assistant_cells(body: BusyCellsIn) -> dict:
    assistant = _busy_assistant()
    return _assistant_call(assistant.edit_cells, body.path, body.sheet, body.changes, body.overwrite_formulas)


@app.post("/api/busy/assistant/sheets")
def busy_assistant_sheets(body: BusySheetIn) -> dict:
    assistant = _busy_assistant()
    return _assistant_call(lambda: assistant.sheet_action(None if body.check else _busy_office(), body.path, body.op,
                                                          body.sheet, body.name, body.date, body.check))


@app.post("/api/busy/assistant/chat")
def busy_assistant_chat(body: BusyAssistantChatIn) -> dict:
    assistant = _busy_assistant()
    return _assistant_call(assistant.chat, body.message, body.history, body.path)


@app.post("/api/busy/assistant/apply")
def busy_assistant_apply(body: BusyAssistantApplyIn) -> dict:
    assistant = _busy_assistant()
    return _assistant_call(lambda: assistant.apply(body.id, office=_busy_office))


@app.post("/api/busy/assistant/feedback")
def busy_assistant_feedback(body: BusyAssistantFeedbackIn) -> dict:
    return _assistant_call(_busy_assistant().feedback, body.text)


@app.get("/api/busy/messages")
def busy_messages() -> dict:
    """What the Busy user sent to Duc (help requests and Góp ý) with chưa đọc / đã đọc."""
    from companion import busy_inbox

    return {"items": busy_inbox.messages(_busy_assistant().data_dir, 50)}


@app.post("/api/busy/assistant/daily-draft")
def busy_assistant_daily_draft() -> dict:
    return _busy_assistant().daily_draft()


def _daily_report():
    from companion.busy_daily_report import DailyReport

    settings = get_settings()
    if settings.tenant != "busy":
        raise ApiError(404, "not_found", "Not found")
    return DailyReport(settings.data_dir.resolve())


class DailyReportPrepareIn(BaseModel):
    date: str | None = None


class DailyReportSentIn(BaseModel):
    date: str
    sent: bool
    template: str | None = None


def _report_day(text: str | None):
    from datetime import date

    try:
        return date.fromisoformat(text) if text else datetime.now().date()
    except ValueError as exc:
        raise ApiError(400, "invalid_date", "Ngày không hợp lệ") from exc


@app.get("/api/busy/daily-report")
def busy_daily_report_status() -> dict:
    report = _daily_report()
    try:
        return {"configured": True, **report.status(datetime.now())}
    except FileNotFoundError:
        return {"configured": False, "config_path": str(report.config_path)}


@app.post("/api/busy/daily-report/prepare")
def busy_daily_report_prepare(body: DailyReportPrepareIn) -> dict:
    from companion.busy_daily_report import DailyReportRefused

    try:
        return _daily_report().prepare(_report_day(body.date))
    except DailyReportRefused as exc:
        raise ApiError(409, "refused", str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise ApiError(400, "not_configured", f"Cấu hình chưa đúng: {exc}") from exc


@app.post("/api/busy/daily-report/drafts")
def busy_daily_report_drafts(body: DailyReportPrepareIn) -> dict:
    from companion.busy_daily_report import DailyReportRefused

    try:
        return _daily_report().drafts(_report_day(body.date))
    except DailyReportRefused as exc:
        raise ApiError(409, "refused", str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise ApiError(400, "not_configured", f"Cấu hình chưa đúng: {exc}") from exc


@app.post("/api/busy/daily-report/sent")
def busy_daily_report_sent(body: DailyReportSentIn) -> dict:
    try:
        return _daily_report().mark_sent(_report_day(body.date), body.sent, body.template)
    except (FileNotFoundError, ValueError) as exc:
        raise ApiError(400, "not_configured", f"Cấu hình chưa đúng: {exc}") from exc


_busy_office_instance = None
_busy_scheduler = None


def _busy_office():
    """One Office per data dir (contract-v2); tests swap it out."""
    global _busy_office_instance
    from companion.busy_office import Office

    settings = get_settings()
    if settings.tenant != "busy":
        raise ApiError(404, "not_found", "Not found")
    root = settings.data_dir.resolve()
    if _busy_office_instance is None or _busy_office_instance.data_dir != root:
        _busy_office_instance = Office(root)
    return _busy_office_instance


def _office_call(fn, *args, **kwargs):
    from companion.busy_office import OfficeRefused
    from companion.llm import ProviderUnavailable

    try:
        return fn(*args, **kwargs)
    except OfficeRefused as exc:
        raise ApiError(409, "refused", str(exc)) from exc
    except ProviderUnavailable as exc:
        raise ApiError(503, "model_unavailable", "Trợ lý tạm thời không trả lời được, bác thử lại sau") from exc


def _office_now(text: str | None) -> datetime | None:
    """A test/demo override of "today" (YYYY-MM-DD, noon); None means now."""
    if not text:
        return None
    return datetime.combine(_report_day(text), datetime.min.time()).replace(hour=12)


@app.on_event("startup")
def _start_busy_scheduler() -> None:
    """Busy tenant only, and only where background workers run (tests set KYRA_INLINE_WORKER=false)."""
    global _busy_scheduler
    settings = get_settings()
    if settings.tenant != "busy" or not settings.inline_worker or _busy_scheduler is not None:
        return
    from companion.busy_office import Scheduler

    _busy_scheduler = Scheduler(_busy_office()).start()
    logger.info("busy office scheduler started")


class BusyDailyIn(BaseModel):
    date: str | None = None


class BusyDraftIn(BaseModel):
    refresh_attachment: bool = False


class BusyAutoIn(BaseModel):
    enabled: bool
    time: str


class BusyLogIn(BaseModel):
    text: str
    at: str | None = None


class BusyUndoIn(BaseModel):
    entry_id: str


class BusyQuickIn(BaseModel):
    message: str
    history: list[dict] = []


@app.get("/api/busy/daily/status")
def busy_daily_status(date: str | None = None) -> dict:
    return _office_call(_busy_office().status, _office_now(date))


@app.post("/api/busy/daily/prepare")
def busy_daily_prepare(body: BusyDailyIn | None = None) -> dict:
    return _office_call(_busy_office().prepare, _office_now(body.date if body else None))


@app.post("/api/busy/daily/draft")
def busy_daily_draft(body: BusyDraftIn | None = None) -> dict:
    return _office_call(_busy_office().draft, bool(body and body.refresh_attachment))


@app.post("/api/busy/daily/sent")
def busy_daily_sent() -> dict:
    return _office_call(_busy_office().mark_sent)


@app.put("/api/busy/daily/auto")
def busy_daily_auto(body: BusyAutoIn) -> dict:
    return _office_call(_busy_office().set_auto, body.enabled, body.time)


@app.get("/api/busy/log")
def busy_log(limit: int = 100) -> dict:
    return _busy_office().log(limit)


@app.post("/api/busy/log")
def busy_log_add(body: BusyLogIn) -> dict:
    return _office_call(_busy_office().add_note, body.text, body.at)


@app.get("/api/busy/reminders")
def busy_reminders() -> list[dict]:
    return _busy_office().reminders()


@app.get("/api/busy/reminders/due")
def busy_reminders_due() -> list[dict]:
    return _busy_office().due()


@app.post("/api/busy/reminders")
def busy_reminder_save(body: dict) -> dict:
    return _office_call(_busy_office().save_reminder, body)


@app.delete("/api/busy/reminders/{rid}")
def busy_reminder_delete(rid: str) -> dict:
    return _office_call(_busy_office().delete_reminder, rid)


@app.delete("/api/busy/reminders")
def busy_reminder_delete_query(id: str) -> dict:
    return _office_call(_busy_office().delete_reminder, id)


@app.post("/api/busy/undo")
def busy_undo(body: BusyUndoIn) -> dict:
    return _office_call(_busy_office().undo, body.entry_id)


@app.post("/api/busy/quick")
def busy_quick(body: BusyQuickIn) -> dict:
    return _office_call(_busy_office().quick, body.message, body.history)


# --- Duc's read-only inbox of Busy messages (personal tenant) ----------------------------------------------------------

_busy_inbox_poller = None


def _personal_dir() -> Path:
    settings = get_settings()
    if settings.tenant != "personal":
        raise ApiError(404, "not_found", "Not found")
    return settings.data_dir.resolve()


class BusyInboxReadIn(BaseModel):
    ids: list[str] = []
    all: bool = False


class BusyInboxDoneIn(BaseModel):
    uid: str


@app.get("/api/busy-inbox")
def busy_inbox_list() -> dict:
    from companion import busy_inbox

    return busy_inbox.inbox(_personal_dir())


@app.post("/api/busy-inbox/read")
def busy_inbox_read(body: BusyInboxReadIn) -> dict:
    from companion import busy_inbox

    return busy_inbox.mark_inbox(_personal_dir(), None if body.all else body.ids)


@app.post("/api/busy-inbox/done")
def busy_inbox_done(body: BusyInboxDoneIn) -> dict:
    """"Đã xử lý": marks the Busy item read and done (stops the daily nudge) and its bell notification read."""
    from companion import busy_inbox

    personal = _personal_dir()
    out = busy_inbox.mark_inbox(personal, [body.uid], done=True)
    notification = busy_inbox.notification_of(personal, body.uid)
    if notification:
        from companion.team_chat import TeamStore
        from companion.team_threads import Store

        Store(TeamStore().root).mark_read([notification])
    return out


@app.on_event("startup")
def _start_busy_inbox_poller() -> None:
    """Personal tenant only, where background workers run; it reads Busy data dirs named in busy_inbox.json."""
    global _busy_inbox_poller
    settings = get_settings()
    if settings.tenant != "personal" or not settings.inline_worker or _busy_inbox_poller is not None:
        return
    from companion.busy_inbox import Poller

    _busy_inbox_poller = Poller(settings.data_dir.resolve()).start()


_phone_link = None


@app.on_event("startup")
def _start_phone_link() -> None:
    """Busy and personal tenants, only where background workers run and only when <data_dir>/phone_link.json is
    complete (scripts/phone_link.py pair); otherwise the phone link stays off."""
    global _phone_link
    settings = get_settings()
    if not settings.inline_worker or _phone_link is not None:
        return
    from companion import phone_link

    office = _busy_office() if settings.tenant == "busy" else None
    _phone_link = phone_link.start_if_configured(settings.tenant, settings.data_dir.resolve(), office)
    if _phone_link is not None:
        logger.info("phone link started")


# ---- Kyra Văn phòng: first-run setup (busy_setup) and the Windows updater (busy_update) ----

@app.get("/busy/setup")
def busy_setup_page() -> HTMLResponse:
    if get_settings().tenant != "busy":
        raise ApiError(404, "not_found", "Not found")
    html = (WEB_DIR / "busy-setup.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace('</main>', '<p><a href="/busy/report">Gửi báo lỗi cho Duc</a></p></main>'))


# Diagnostics stays local until the user opens and sends the mail draft.
@app.get("/busy/report")
def busy_report_page() -> FileResponse:
    if get_settings().tenant != "busy":
        raise ApiError(404, "not_found", "Not found")
    return FileResponse(WEB_DIR / "busy-report.html")


def _busy_report_request(request: Request) -> None:
    origin = request.headers.get("origin")
    if ((origin and origin != str(request.base_url).rstrip("/"))
            or request.headers.get("content-type", "").split(";")[0] != "application/json"):
        raise ApiError(403, "report_origin", "Anh mở trang báo lỗi trong Kyra nhé.")


def _busy_report_context():
    office = _busy_office()
    from companion import busy_report

    try:
        cfg = office.config()
    except (OSError, ValueError, TypeError, AttributeError):
        cfg = {}
    folder = cfg.get("workbook_folder")
    reports = Path(folder).expanduser() if isinstance(folder, str) and folder else None
    return office.data_dir, busy_report.desktop_folder(), reports, cfg


@app.post("/api/busy/report/preview")
def busy_report_preview(request: Request) -> dict:
    from companion import busy_report

    _busy_report_request(request)
    root, desktop, reports, _ = _busy_report_context()
    try:
        return busy_report.prepare(root, desktop, reports)
    except (OSError, busy_report.ReportRefused):
        raise ApiError(409, "report_unavailable", "Em chưa tạo được gói an toàn. Anh kiểm tra Cài đặt nhé.") from None


class BusyReportDraftIn(BaseModel):
    name: str
    sha256: str
    receipt: str


@app.post("/api/busy/report/draft")
def busy_report_draft(body: BusyReportDraftIn, request: Request) -> dict:
    from companion import busy_mail, busy_report

    _busy_report_request(request)
    _, desktop, reports, cfg = _busy_report_context()
    try:
        if not busy_report._allowed(desktop, reports):
            raise busy_report.ReportRefused()
        return busy_report.draft(desktop, body.name, body.sha256, str(cfg.get("support_address") or ""),
                                 receipt=body.receipt)
    except (OSError, busy_report.ReportRefused, busy_mail.MailRefused):
        raise ApiError(409, "draft_unavailable", "Em chưa mở được thư nháp. Anh xem trước lại nhé.") from None


@app.middleware("http")
async def _busy_report_exceptions(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as exc:
        if get_settings().tenant == "busy":
            from companion import busy_report

            try:
                # Fall back even when setup or Desktop resolution is the original failure.
                root, reports = get_settings().data_dir, None
                try:
                    root, _, reports, _ = _busy_report_context()
                except Exception:
                    pass
                if busy_report._allowed(root / "busy" / "incidents.jsonl", reports):
                    busy_report.record_exception(root, exc)
            except Exception:
                pass  # Reporting must not replace the original server failure.
        raise


class BusySetupIn(BaseModel):
    folder: str = ""
    subject: str | None = None
    to: list[str] | None = None
    cc: list[str] | None = None
    body: str | None = None
    date_format: str | None = None
    times: dict[str, str] = {}


class BusySampleIn(BaseModel):
    text: str = Field(max_length=50_000)


class BusyFolderIn(BaseModel):
    path: str


class BusyPhoneIn(BaseModel):
    relay: str = ""


def _setup_call(fn, *args):
    from companion.busy_setup import SetupRefused

    try:
        return fn(*args)
    except SetupRefused as exc:
        raise ApiError(400, "refused", str(exc)) from exc


def _app_defaults() -> dict:
    try:
        return json.loads((Path(__file__).resolve().parent.parent.parent / "defaults.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@app.get("/api/busy/setup")
def busy_setup_status() -> dict:
    from companion import busy_setup, phone_link

    office = _busy_office()
    out = busy_setup.status(office.data_dir, office)
    cfg = phone_link.load_config(office.data_dir)
    out["phone"] = {"paired": bool(cfg and cfg.get("phone_pub")), "pending": bool(cfg and not cfg.get("phone_pub")),
                    "repair": bool(cfg) and phone_link.needs_repair(office.data_dir),
                    "relay":(cfg or {}).get("relay") or _app_defaults().get("phone_relay", "")}
    return out


@app.post("/api/busy/setup/parse")
def busy_setup_parse(body: BusySampleIn) -> dict:
    from companion.busy_setup import parse_sample

    _busy_office()  # tenant gate
    return parse_sample(body.text)


@app.post("/api/busy/setup")
def busy_setup_save(body: BusySetupIn) -> dict:
    from companion import busy_setup

    office = _busy_office()
    return _setup_call(busy_setup.save, office.data_dir, office, body.model_dump(exclude_none=True))


@app.post("/api/busy/setup/pick-folder")
def busy_setup_pick_folder() -> dict:
    from companion.busy_setup import pick_folder

    _busy_office()
    return {"path": pick_folder()}


@app.post("/api/busy/setup/open-folder")
def busy_setup_open_folder(body: BusyFolderIn) -> dict:
    from companion.busy_setup import open_folder

    _busy_office()
    _setup_call(open_folder, body.path)
    return {"ok": True}


@app.post("/api/busy/setup/phone")
def busy_setup_phone(body: BusyPhoneIn) -> dict:
    from companion import phone_link

    office = _busy_office()
    relay = body.relay.strip() or _app_defaults().get("phone_relay", "")
    if not relay:
        raise ApiError(400, "refused", "Chưa có địa chỉ máy chủ điện thoại (relay)")
    try:
        info = phone_link.pair_start(office.data_dir, relay, "office")
    except phone_link.LinkRefused as exc:
        raise ApiError(400, "refused", str(exc)) from exc
    except OSError as exc:
        raise ApiError(503, "relay_unavailable", "Chưa kết nối được máy chủ điện thoại") from exc
    _start_phone_link()  # the link confirms the phone's key once it scans the code
    return info


def _busy_updater():
    from companion import busy_update

    office = _busy_office()
    return busy_update.from_env(office.data_dir)


def _update_call(fn):
    from companion.busy_update import UpdateRefused

    try:
        return fn()
    except UpdateRefused as exc:
        raise ApiError(409, "refused", str(exc)) from exc


@app.get("/api/busy/update/status")
def busy_update_status() -> dict:
    updater = _busy_updater()
    return updater.status() if updater else {"installed": False, "available": None}


@app.post("/api/busy/update/check")
def busy_update_check() -> dict:
    updater = _busy_updater()
    if updater is None:
        raise ApiError(409, "refused", "Bản chạy thử này không tự cập nhật")
    return _update_call(updater.check)


@app.post("/api/busy/update/apply")
def busy_update_apply() -> dict:
    updater = _busy_updater()
    if updater is None:
        raise ApiError(409, "refused", "Bản chạy thử này không tự cập nhật")
    return _update_call(updater.apply)


class BusyRollbackIn(BaseModel):
    reason: str = Field("", max_length=1000)


@app.post("/api/busy/update/rollback")
def busy_update_rollback(body: BusyRollbackIn) -> dict:
    updater = _busy_updater()
    if updater is None:
        raise ApiError(409, "refused", "Bản chạy thử này không có phiên bản cũ để quay lại")
    return _update_call(lambda: updater.roll_back(body.reason))


# ---- Kyra Văn phòng: known-good data snapshots and restore (busy_snapshot) ----

@app.get("/busy/restore")
def busy_restore_page() -> FileResponse:
    if get_settings().tenant != "busy":
        raise ApiError(404, "not_found", "Not found")
    return FileResponse(WEB_DIR / "busy-restore.html")


def _snapshot_call(fn, *args) -> dict:
    from companion import busy_snapshot

    office = _busy_office()
    try:
        pending = fn(office.data_dir, *args)
    except busy_snapshot.SnapshotMissing as exc:
        raise ApiError(404, "not_found", str(exc)) from exc
    except busy_snapshot.SnapshotRefused as exc:
        raise ApiError(409, "refused", str(exc)) from exc
    return {"pending": pending, "restarting": busy_snapshot.restart_server()}


@app.get("/api/busy/snapshots")
def busy_snapshots() -> dict:
    from companion import busy_snapshot

    try:
        return busy_snapshot.status(_busy_office().data_dir)
    except busy_snapshot.SnapshotRefused as exc:
        raise ApiError(409, "refused", str(exc)) from exc


@app.post("/api/busy/snapshots/undo")
def busy_snapshot_undo() -> dict:
    from companion import busy_snapshot

    return _snapshot_call(busy_snapshot.request_undo)


@app.post("/api/busy/snapshots/{snapshot_id}/restore")
def busy_snapshot_restore(snapshot_id: str) -> dict:
    from companion import busy_snapshot

    return _snapshot_call(busy_snapshot.request_restore, snapshot_id)


@app.get("/api/busy/daily-report/files/{day}/{kind}")
def busy_daily_report_file(day: str, kind: str, template: str | None = None) -> FileResponse:
    report = _daily_report()
    try:
        path = report.prepared_file(_report_day(day), kind, template).resolve()
    except KeyError as exc:
        raise ApiError(404, "not_found", "File not found") from exc
    if not path.is_file():
        raise ApiError(404, "not_found", "File not found")
    media = "message/rfc822" if kind == "eml" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return FileResponse(path, media_type=media, filename=path.name, headers={"Cache-Control": "no-store"})


@app.get("/")
def index() -> HTMLResponse:
    settings = get_settings()
    if settings.tenant == "busy":  # the busy tenant never lands on personal Kyra pages
        target = "/busy/office" if (WEB_DIR / "busy-office.html").is_file() else "/busy/assistant"
        return RedirectResponse(target, status_code=307)
    if settings.team_primary and settings.owner_machine and settings.tenant == 'personal':
        from companion.team_http import page
        return page()
    return workspace()


@app.get("/start")
def start_guide() -> HTMLResponse:
    """Static, generic usage guidance; no file lookup, model or state access."""
    return HTMLResponse((WEB_DIR / "start.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


@app.get("/workspace")
def workspace() -> HTMLResponse:
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
    for asset in ("app.js", "style.css", "atlas.js", "atlas.css", "ideas.js", "lessons.js"):
        mtime = (WEB_DIR / asset).stat().st_mtime_ns
        html = html.replace(f'/static/{asset}"', f'/static/{asset}?v={mtime}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/features")
def features() -> dict:
    return feature_map(load_features())


@app.get("/api/system-map")
def system_map() -> dict:
    try:
        return {**load_system_map(), "lanes": LANES}
    except FileNotFoundError as exc:
        raise ApiError(404, "system_map_missing", "System map is unavailable.") from exc


@app.get("/atlas")
def atlas_page() -> HTMLResponse:
    html = (WEB_DIR / "atlas.html").read_text(encoding="utf-8")
    for asset in ("atlas.js", "atlas.css", "atlas-page.js"):
        html = html.replace(f'/static/{asset}"', f'/static/{asset}?v={(WEB_DIR / asset).stat().st_mtime_ns}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/atlas")
def atlas_data() -> dict:
    try:
        return load_atlas()
    except (FileNotFoundError, ValueError) as exc:
        raise ApiError(404, "atlas_missing", "The project map is unavailable.") from exc


# Constructors remain cheap; no file, model or network access at startup.
_idea_store = IdeaStore()


class SaveIdeaRequest(IdeaDraft):
    request_id: UUID


class EditIdeaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypothesis: str | None = Field(default=None, min_length=1, max_length=500)
    problem: str | None = None
    experiment: str | None = None
    status: Literal["open", "trying", "kept", "dropped"] | None = None


@app.get("/api/ideas/desk")
def idea_desk(live: int = Query(default=0, ge=0, le=1)) -> dict:
    return build_desk(load_desk(), headlines=(lambda: TechNewsTool().run(per_source=2)) if live else None)


@app.get("/api/ideas")
def saved_ideas() -> dict:
    return {"ideas": [idea.model_dump(mode="json") for idea in _idea_store.list()]}


@app.post("/api/ideas")
def save_idea(body: SaveIdeaRequest) -> dict:
    try:
        draft = resolve_draft(IdeaDraft(**body.model_dump(exclude={"request_id"})))
    except ValueError as exc:
        raise ApiError(422, "invalid_idea", "The curated source is not in the catalogue.") from exc
    try:
        return _idea_store.save(draft, request_id=body.request_id).model_dump(mode="json")
    except IdeaRequestConflict as exc:
        raise ApiError(409, "request_id_conflict", "This save receipt was already used for a different idea.") from exc



@app.patch("/api/ideas/{idea_id}")
def edit_idea(idea_id: str, body: EditIdeaRequest) -> dict:
    try:
        idea = _idea_store.update(idea_id, **body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise ApiError(422, "invalid_idea", "The idea changes are invalid.") from exc
    if idea is None:
        raise ApiError(404, "not_found", "Idea not found.")
    return idea.model_dump(mode="json")


def _lesson_by_id(lesson_id: str):
    lesson = next((lesson for lesson in list_lessons() if lesson.id == lesson_id), None)
    if lesson is None:
        raise ApiError(404, "not_found", "Lesson not found.")
    return lesson


@app.get("/api/lessons")
def lessons_index() -> dict:
    return {"lessons": [{"id": lesson.id, "title": lesson.title, "goal": lesson.goal} for lesson in list_lessons()]}


@app.get("/api/lessons/{lesson_id}")
def lesson_detail(lesson_id: str) -> dict:
    return _lesson_by_id(lesson_id).model_dump(mode="json")


class LessonAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_id: str
    option: StrictInt


@app.post("/api/lessons/{lesson_id}/answer")
def lesson_answer(lesson_id: str, body: LessonAnswerRequest) -> dict:
    lesson = _lesson_by_id(lesson_id)
    if not any(question.id == body.question_id for question in lesson.questions):
        raise ApiError(404, "not_found", "Question not found.")
    try:
        return check_answer(lesson, body.question_id, body.option)
    except ValueError as exc:
        raise ApiError(422, "invalid_answer", "Choose one of this question's answers.") from exc


@app.get("/healthz")
def healthz() -> dict:
    """Liveness plus local asset stamps, independent of runtime subsystems.

    It must answer while Postgres, Chroma and the model are all unavailable,
    otherwise a slow dependency reads as a dead process and the orchestrator
    restarts a server that was fine. Ungated for the same reason: an authenticated
    healthcheck fails closed the moment a token is set, and the deploy
    restart-loops with no obvious cause.
    """
    return {"status": "ok", "ok": True,
            "assets": {asset: (WEB_DIR / asset).stat().st_mtime_ns
                       for asset in ("app.js", "style.css")}}


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
    input_label = _input_labeller.label(message)
    if on_token is not None and register != "voice":
        on_token = TextStream(on_token, mode=get_mode())

    def finish(reply, **metadata):
        if register == "voice":
            return ChatOut(reply=reply, **metadata)
        rendered = Reply(reply, mode=get_mode())
        return ChatOut(reply=str(rendered), rest=rendered.rest, **metadata)

    if _current_backend != "auto":
        reply = handle_mode_command(message)
        if reply is None:
            reply = approval_reply(message, _rt.conversation, _registry, input_label=input_label)
        if reply is None:
            reply = _rt.conversation.handle_turn(
                message, on_token=on_token, register=register, input_label=input_label,
            )
        return finish(reply, backend=_current_backend)

    reply, decision = route_and_answer_verbose(
        message, _rt.conversation, _router, _backends, _registry, on_token=on_token, register=register,
        input_label=input_label,
    )
    if decision is None:  # a mode-switch command ("focus mode" etc.), not a routed turn
        return finish(reply, backend="auto")
    return finish(
        reply=reply, backend="auto",
        actual_backend=("local (kept on this Mac)" if decision.error == "ReleaseRefused" else "local (Claude unreachable)")
        if decision.fallback_from else decision.backend,
        path=decision.path, reason=decision.reason,
    )


@app.post("/api/chat", response_model=ChatOut)
def chat(body: ChatIn) -> ChatOut:
    try:
        return _answer(body.message)
    except ReleaseRefused as exc:
        raise ApiError(403, "release_refused", str(exc)) from exc
    except AuditUnavailable as exc:
        raise ApiError(503, "audit_unavailable", str(exc)) from exc


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
        if delta:
            q.put(("token", delta))

    def run() -> None:
        global _cancel_current
        try:
            out = _answer(body.message, on_token=on_token)
            q.put(("done", out.model_dump(exclude={"rest"} if not out.rest else None)))
        except (ReleaseRefused, AuditUnavailable) as exc:
            code = "audit_unavailable" if isinstance(exc, AuditUnavailable) else "release_refused"
            q.put(("refused", {"code": code, "message": str(exc), "retry": False}))
        except Exception as e:  # noqa: BLE001 - surfaced to the client as an error event, never swallowed
            logger.exception("chat stream failed")
            q.put(("error", f"{type(e).__name__}: {e}"))
        finally:
            if _cancel_current is cancel:
                _cancel_current = None
            q.put(done)

    threading.Thread(target=run_in_scope(run), name="kyra-chat-stream", daemon=True).start()

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


@app.get("/api/memory/map")
def memory_map() -> dict:
    # Reading the cached property's dictionary must never open Chroma.
    memory = vars(_rt).get("memory")
    threads = session_log.threads()
    controller = _loop_controller()
    result = build_map(
        notes=_memory_notes.list_notes(), threads=threads,
        assignments=controller.store.list_assignments(owner=controller.owner),
        exchanges=memory._collection.count() if memory is not None else None,
    )
    # Include unlinked threads for drawing, without reading any thread body into the response.
    result["thread_nodes"] = [{"name": name, "last_date": modified} for name, modified, _ in threads]
    return result


@app.get("/api/handwriting")
def handwriting_summary() -> dict:
    from companion.handwriting import summary

    return summary()


def _needs_input_path() -> Path:
    return DATA_DIR / "private_docs" / "needs-your-input.md"


def _job_heartbeats() -> dict:
    from companion.attention import job_heartbeats
    return job_heartbeats()


def _snoozes_store() -> Path:
    from companion.attention import SNOOZES
    return SNOOZES


@app.get("/api/calendar/upcoming")
def calendar_upcoming(days: int = Query(default=7, ge=1, le=365)) -> dict:
    from companion.calendar_events import CalendarAccessDenied, upcoming

    if not get_settings().calendar_enabled:
        raise ApiError(409, "calendar_disabled", "Calendar access is disabled in Kyra settings.")
    try:
        return {"events": [asdict(event) for event in upcoming(days)]}
    except CalendarAccessDenied as exc:
        raise ApiError(409, "calendar_access_denied",
                       "Allow calendar access in System Settings > Privacy & Security > Calendars.") from exc
    except Exception as exc:
        raise ApiError(503, "calendar_unavailable", "Could not read the local calendar.") from exc


@app.get("/api/mail/needs-reply")
def mail_needs_reply() -> dict:
    from companion.mail import GmailReader, MailNotAuthorised, needs_reply

    settings = get_settings()
    if not settings.gmail_credentials:
        raise ApiError(409, "mail_disabled", "Mail is disabled until KYRA_GMAIL_CREDENTIALS is configured.")
    message = "Run scripts/mail_authorise.py with the owner to authorise Gmail read-only access."
    if not settings.gmail_token.expanduser().is_file():
        raise ApiError(409, "mail_not_authorised", message)
    try:
        threads = GmailReader(settings.gmail_credentials, settings.gmail_token).list_threads()
        return {"threads": [asdict(thread) for thread in needs_reply(threads, now=datetime.now(UTC))]}
    except MailNotAuthorised as exc:
        raise ApiError(409, "mail_not_authorised", message) from exc
    except Exception as exc:
        raise ApiError(503, "mail_unavailable", "Could not read Gmail metadata.") from exc


def _health_dir() -> Path:
    return DATA_DIR / "health"


@app.get("/api/myself")
def myself_summary():
    from companion import health_auto
    from companion.health_export import MissingExport, export_info, find_export, nights, resting_heart_rate

    source = None
    try:
        folder = get_settings().health_auto_dir.strip()
        files = health_auto.find_files(folder) if folder else []
        now = datetime.now().astimezone()
        if files:
            source = "auto"
            body = health_auto.merge(files)
            first = (now.date() - timedelta(days=29)).isoformat()
            for kind in ("nights", "resting_hr"):
                body[kind] = [row for row in body[kind] if first <= row["date"] <= now.date().isoformat()]
            body.update(source=source, watch=None,
                        export_day=datetime.fromtimestamp(files[-1].stat().st_mtime).date().isoformat(),
                        export_day_source="file_modified", resting_hr_method="last_row",
                        note="Health Auto Export: latest row per date. Resting-rate format awaits a real-file check.")
        else:
            path = find_export(_health_dir())
            source = "export"
            body = {"nights": nights(path, now=now), "resting_hr": resting_heart_rate(path, now=now),
                    **export_info(path), "source": source, "resting_hr_method": "median"}
    except health_auto.Unaggregated:
        body = {"nights": [], "resting_hr": [], "export_day": None, "watch": None, "source": "auto",
                "note": "Turn on Summarize Data in Health Auto Export"}
    except MissingExport:
        body = {"nights": [], "resting_hr": [], "export_day": None, "watch": None, "source": None,
                "note": "No Health export yet; export from the Health app to data/health/export.zip"}
    except Exception:
        # No exception text, record values, audit entry or provider scope crosses this boundary.
        return JSONResponse(status_code=422, content={"source": source, "error": {"code": "health_export_unreadable",
                            "message": "Could not read the Health export. Check the selected export format.",
                            "details": {}}}, headers={"Cache-Control": "no-store"})
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


def _daily_news() -> list[dict]:
    result = _news_tool.run(per_source=1)
    if result.get("errors"):
        raise RuntimeError("News feeds unavailable")
    return result["headlines"][:5]


def _latest_digest_day() -> str | None:
    days = []
    for path in (DATA_DIR / "digests").glob("*"):
        if not path.is_file() or path.suffix not in {".html", ".md", ".json"}:
            continue
        try:
            day = datetime.strptime(path.stem, "%Y-%m-%d").date().isoformat()
        except ValueError:
            continue
        if day == path.stem:
            days.append(day)
    return max(days, default=None)


@app.get("/api/daily")
def daily_summary() -> dict:
    from companion.daily import timeline

    now = datetime.now().astimezone()
    inputs = dict(events=[], reminders=[], mail=[], news=[], digest_day=None)
    unavailable = []

    def read(name, field, loader):
        try:
            value = loader()
            # A malformed source must not prevent the other sources from displaying.
            timeline(now=now, **(inputs | {field: value}))
            inputs[field] = value
        except Exception:
            unavailable.append(name)

    settings = get_settings()
    if settings.calendar_enabled:
        read("calendar", "events", lambda: calendar_upcoming(7)["events"])
    read("reminders", "reminders", lambda: [asdict(r) for r in _reminders_store.list()])
    if settings.gmail_credentials:
        read("mail", "mail", lambda: mail_needs_reply()["threads"])
    read("news", "news", _daily_news)
    read("digest", "digest_day", _latest_digest_day)
    out = timeline(now=now, **inputs)
    out["unavailable"] = unavailable
    out["digest_path"] = None
    if out["digest_day"]:
        try:
            for suffix in (".html", ".md", ".json"):
                path = DATA_DIR / "digests" / (out["digest_day"] + suffix)
                if path.is_file():
                    out["digest_path"] = str(path)
                    break
        except OSError:
            unavailable.append("digest")
            out["digest_day"] = None
    return out


def _team_status_snapshot(now):
    from companion.team_chat import TeamStore, empty_status_snapshot

    settings = get_settings()
    if settings.tenant != 'personal' or not settings.owner_machine:
        return empty_status_snapshot(now=now.timestamp())
    return TeamStore().status_snapshot(now=now.timestamp())


@app.get("/api/attention")
def attention_summary() -> dict:
    from companion import app_dispatch, attention, reflections

    unavailable = []

    def read(source, loader, fallback):
        try:
            return loader()
        except Exception:
            unavailable.append(attention.Card(
                "warning", f"source:{source}", "Attention source unavailable",
                "Could not read this source; its status is unknown.", source, None,
                "Check the source with your agent", severity=2,
            ))
            return fallback

    def runs():
        controller = _loop_controller()
        return [asdict(row) for row in controller.store.list_runs(owner=controller.owner)]

    path = _needs_input_path()
    now = datetime.now(UTC)
    team = _team_status_snapshot(now)
    cards = attention.collect(
        now=now, team_runs=team["runs"],
        outbound=read("/api/outbound/report", lambda: outbound_report(OutboundAudit()), {}),
        tool_runs=read("/api/tools/runs", lambda: [asdict(r) for r in _registry.audit.list(limit=500)], []),
        jobs=read("/api/jobs", lambda: [asdict(j) for j in _queue.list(limit=500)], []),
        deliveries=read("/api/loop/deliveries", lambda: app_dispatch.load(_deliveries_store()), []),
        loop_runs=read("/api/loop/runs", runs, []),
        reminders=read("/api/reminders", lambda: [asdict(r) for r in _reminders_store.list()], []),
        approvals=read("/api/actions", lambda: pending_actions()["actions"], []),
        heartbeats=read("launchd", _job_heartbeats, {}),
        reflections=read("/api/reflections", lambda: [asdict(c) for c in reflections.load_cards(_today())], []),
        needs_input=read("needs-your-input.md", lambda: path.read_text() if path.exists() else "", ""),
        events=read("/api/calendar/upcoming", lambda: calendar_upcoming(1)["events"], [])
        if get_settings().calendar_enabled else [],
        mail=read("/api/mail/needs-reply", lambda: mail_needs_reply()["threads"], [])
        if get_settings().gmail_credentials else [],
    )
    result = attention.present(cards + unavailable, now=now, snoozes=attention.load_snoozes(_snoozes_store()))
    return {**result, "team": team}


def _today():
    return datetime.now(UTC).date().isoformat()


@app.get("/api/reflections")
def reflections_summary() -> dict:
    from companion import reflections as rf
    day = _today()
    votes = rf.load_votes()
    return {"day": day, "cards": [dict(asdict(c), vote=votes.get(c.key, {}).get("vote"))
                                   for c in rf.load_cards(day)], "votes": len(votes)}


class ReflectionVoteIn(BaseModel):
    model_config = {"extra": "forbid"}
    vote: Literal["useful", "not_useful", "do_it"]


@app.post("/api/reflections/{key}/vote")
def reflection_vote(key: str, body: ReflectionVoteIn) -> dict:
    from companion import reflections as rf
    card = next((c for c in rf.load_cards(_today()) if c.key == key), None)
    if card is None:
        raise ApiError(404, "reflection_not_found", "This reflection is no longer in today's feed.")
    result = {"ok": True}
    if body.vote == "do_it":
        if get_settings().database_url:
            raise ApiError(409, "local_store_required", "Reflection drafts require the local LOOP store.")
        controller = _loop_controller()
        # Same lock across processes. A retry finds the existing unsent assignment.
        with rf.writing():
            code = f"reflection-{card.key}"
            assignment = next((a for a in controller.store.list_assignments(owner=controller.owner)
                               if a.code == code), None)
            if assignment is None:
                assignment = controller.store.create_assignment(
                    owner=controller.owner, code=code, title=card.observation,
                    goal=f"Review this unverified reflection before planning work.\n{card.suggestion}\nEvidence: {card.evidence}",
                    allowed_files=[], acceptance=[card.outcome_check])
            result["assignment_id"] = assignment.id
    rf.vote(key, body.vote, datetime.now(UTC))
    return result


@app.get("/api/progress")
def progress_summary() -> dict:
    from sqlalchemy import select

    from companion import progress
    from companion.initiative_digest import existing_store
    from companion.schema import initiatives as initiative_table

    now = _utcnow().astimezone()
    team = _team_status_snapshot(now)
    controller = _loop_controller()
    assignments = [asdict(row) for row in controller.store.list_assignments(owner=controller.owner)]
    applications = [asdict(row) for row in _job_store.list()]
    active = _focus_store.active()
    store = existing_store()
    initiatives = []
    if store is not None:
        # list() is the proposal inbox and intentionally excludes accepted items.
        with store._engine.connect() as conn:
            rows = conn.execute(select(initiative_table).where(
                initiative_table.c.status.in_(['proposed', 'accepting', 'accepted']))).all()
        initiatives = [store._decode(row) for row in rows]
    return {
        "team": team,
        "leads": [asdict(lead) for lead in progress.leads(
            now=now, assignments=assignments, focus_active=asdict(active) if active else None,
            applications=applications, initiatives=initiatives, team_runs=team["runs"],
        )],
        "today": progress.today(
            now=now, focus_sessions=[asdict(row) for row in _focus_store.list(limit=None)],
            assignment_events=[row['updated_at'] for row in assignments],
            application_events=[event.at for row in applications for event in _job_store.history(row['id'])],
            learning_reviews=len(_learning_store.due()),
        ),
    }


class AttentionSnoozeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=200)
    until: str

    @field_validator("until")
    @classmethod
    def iso_time(cls, value: str) -> str:
        datetime.fromisoformat(value)
        return value


@app.post("/api/attention/snooze")
def attention_snooze(body: AttentionSnoozeIn) -> dict:
    from companion.attention import snooze
    return {"snoozed": snooze(body.key, body.until, _snoozes_store())}


def _notes_store() -> MarkdownMemoryNotesStore:
    return _memory_notes


class NotesLabelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tier: StrictInt
    classes: list[PrivacyClass]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


@app.get("/api/notes/shadow")
def notes_shadow() -> dict:
    from companion.shadow_labels import SUGGESTIONS_FILE, diff

    store = _notes_store()
    path = store._dir / SUGGESTIONS_FILE
    suggestions = json.loads(path.read_text()) if path.exists() else {}
    return {"rows": diff(store, suggestions)}


@app.post("/api/notes/shadow/run")
def run_notes_shadow() -> dict:
    return {"job_id": _queue.enqueue("shadow_labels", {})}


def _run_shadow_labels_job(payload: dict, on_progress) -> dict:
    from companion.shadow_labels import sandboxed_labeller, suggest

    on_progress("Suggesting labels locally; current labels remain unchanged.")
    result = suggest(_notes_store()._dir, labeller=sandboxed_labeller(
        get_settings().shadow_label_model_dir, DATA_DIR / "shadow_labels",
    ))
    return {"files": len(result), "errors": sum("error" in item for item in result.values())}


@app.get("/api/notes/labels")
def notes_labels() -> dict:
    return {"files": [
        {"category": block.category, "tier": int(block.label[0]),
         "classes": sorted(c.value for c in block.label[1]), "stale": block.stale,
         "notes": block.notes, "bytes": block.bytes, "reviewed_at": block.reviewed_at, "sha256": block.sha256}
        for block in _notes_store().labelled_blocks() if block.category
    ]}


@app.post("/api/notes/labels/{category}")
def set_notes_label(category: str, body: NotesLabelIn) -> dict:
    try:
        _notes_store().set_label(category, Tier(body.tier), frozenset(body.classes), sha256=body.sha256)
    except NotesVersionConflict as exc:
        raise ApiError(409, "notes_version_conflict", str(exc)) from None
    except KeyError:
        raise ApiError(404, "notes_category_not_found", "No such notes category") from None
    except ValueError as exc:
        raise ApiError(400, "invalid_notes_label", str(exc)) from None
    return {"ok": True}


@app.get("/api/outbound/report")
def outbound_summary(days: int = Query(30, ge=1, le=3650)) -> dict:
    return outbound_report(OutboundAudit(), days=days)


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
        speech, _ = spoken_reply(text)
        sentences, _ = take_sentences(speech, final=True)
        for sentence in sentences:
            event = _audio_event(sentence, encode_wav_bytes)
            if event:
                yield f"event: {event[0]}\ndata: {json.dumps(event[1])}\n\n"
        yield f"event: done\ndata: {json.dumps({'sentences': len(sentences)})}\n\n"

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


def _transcribe_team_upload(audio: UploadFile) -> str:
    from companion.team_audio import decode_recording
    return _rt.stt.transcribe(decode_recording(audio.file)).strip()


def _transcribe_upload(audio: UploadFile) -> str:
    from companion.voice import decode_uploaded_audio

    return _rt.stt.transcribe(decode_uploaded_audio(audio.file)).strip()


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
    from companion.voice import encode_wav_bytes

    transcript = _transcribe_upload(audio)

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
            budget = SpokenBudget()

            def admit(sentence: str) -> None:
                if speech := budget.admit(sentence):
                    say(speech)

            def on_token(delta: str) -> None:
                nonlocal buffer, spoken_any
                buffer += delta
                sentences, buffer = take_sentences(buffer)
                for sentence in sentences:
                    admit(sentence)
                    spoken_any = True

            out = _answer(transcript, on_token=on_token, register="voice")
            tail, _ = take_sentences(buffer, final=True)
            for sentence in tail:
                admit(sentence)
                spoken_any = True
            if not spoken_any:
                # Nothing streamed (a tool turn, or the local model).
                sentences, _ = take_sentences(spoken_text(out.reply), final=True)
                for sentence in sentences:
                    admit(sentence)
            if cue := budget.closing():
                say(cue)
            q.put(("done", out.model_dump()))
        except (ReleaseRefused, AuditUnavailable) as exc:
            code = "audit_unavailable" if isinstance(exc, AuditUnavailable) else "release_refused"
            q.put(("refused", {"code": code, "message": str(exc), "retry": False}))
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
        threading.Thread(target=run_in_scope(run), name="kyra-voice-stream", daemon=True).start()
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
    from companion.voice import encode_wav_bytes

    transcript = _transcribe_upload(audio)
    if not transcript:
        return VoiceOut(
            reply="", backend=_current_backend, transcript="", reply_audio_b64="", reply_audio_rate=0
        )

    # Bound only the audio; the transcript keeps the full written reply.
    try:
        chat_out = _answer(transcript, register="voice")
    except ReleaseRefused as exc:
        raise ApiError(403, "release_refused", str(exc)) from exc
    except AuditUnavailable as exc:
        raise ApiError(503, "audit_unavailable", str(exc)) from exc
    speech, _ = spoken_reply(chat_out.reply)
    reply_audio, rate = _rt.tts.speak(speech)
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
    from companion.search import matches_lookup_query

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
            "found_inside": any(matches_lookup_query(hit, query) for hit in hits),
            "indexed_at": _rt.search_index.last_indexed(),
            "hits": [_hit_out(h) for h in hits]}


def _outside_search():
    from companion.web_search import DdgsSearch

    return DdgsSearch()


class OutsideSearchIn(BaseModel):
    query: str
    k: int = Field(default=5, ge=1, le=10)


@app.post("/api/search/outside")
def search_outside(body: OutsideSearchIn) -> dict:
    from companion.web_search import OutsideRefused

    query = body.query.strip()
    if not query:
        raise ApiError(400, "empty_query", "give me something to search for")
    try:
        with release_label(*_input_labeller.label(query)):
            hits = _outside_search().search(query, body.k)
    except OutsideRefused as exc:
        raise ApiError(409, "outside_refused", str(exc)) from None
    return {"results": [asdict(hit) for hit in hits]}


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

    threading.Thread(target=run_in_scope(run), name="kyra-search-answer", daemon=True).start()

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


def _draft_notes(warnings: list[str]) -> str:
    if get_settings().outbound_gate != "enforce":
        return _memory_notes.render()
    text, left_out = _memory_notes.render_releasable(default_gate().policy)
    if left_out:
        files = ", ".join(f"{category} ({', '.join(classes)})" for category, classes in left_out)
        warnings.append(f"left out {len(left_out)} notes file(s) that may not leave this Mac: {files}")
    return text


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

    notes_block = _draft_notes(warnings)
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


@release_label(Tier.T2, frozenset({PrivacyClass.job_search}))
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


@release_label(Tier.T2, frozenset({PrivacyClass.job_search}))
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


@release_label(Tier.T2, frozenset({PrivacyClass.job_search}))
def _run_apply_job(payload: dict, on_progress) -> dict:
    """Job handler: one posting URL through apply_pipeline.run_apply_pipeline. Extra facts are the
    same Memory Notes block every resume path already reads (via _pick_resume_source with no
    resume of its own)."""
    _, extra_facts, warnings = _pick_resume_source("", "", None)
    try:
        result = run_apply_pipeline(
            payload["url"], store=_job_store, resume_llm=_resume_llm, draft_llm=_draft_llm,
            base_latex=_apply_base_latex(), profile=load_profile(), engines=_autofill_engines, extra_facts=extra_facts,
            posting_text=payload.get("posting_text", ""), company=payload.get("company", ""), role=payload.get("role", ""),
            source_url=payload.get("source_url", ""), cover_letter=payload.get("cover_letter", "auto"),
            retailor=payload.get("retailor", False), prepare_only=payload.get("prepare_only", False),
            fetch=_fetch_posting, on_progress=on_progress,
            record_fill=_record_fill,
        )
    except ApplyError as e:
        raise ApiError(400, "apply_failed", str(e)) from e
    out = asdict(result)
    if warnings:
        out["warnings"] = warnings
    return out


def _run_prepare_job(payload: dict, on_progress) -> dict:
    store = _ready_store()
    item = store.get(payload["ready_id"])
    if item["state"] in {"skipped", "applied"} or item["deep"]:
        return {"skipped": True}
    try:
        result = _run_apply_job({**payload, "prepare_only": True}, on_progress)
        current = store.get(item["id"])
        if current["state"] not in {"skipped", "applied"}:
            state = "prepared" if result["status"] == "prepared" and result.get("resume_pdf_path") else "needs_input"
            store.mark(item["id"], state, resume_path=result.get("resume_pdf_path"),
                       cover_letter_path=result.get("cover_letter_path"), questions=result.get("attention") or [])
        return result
    except Exception:
        if store.get(item["id"])["state"] not in {"skipped", "applied"}:
            store.mark(item["id"], "needs_input", questions=["Preparation failed. Review the preparation job before retrying."])
        raise


HANDLERS: dict[str, Handler] = {"latex_resume": _run_latex_resume_job, "apply": _run_apply_job,
                                "shadow_labels": _run_shadow_labels_job, "prepare": _run_prepare_job}


def _run_team_honesty_job(payload: dict, on_progress) -> dict:
    from companion import team_honesty  # lazy: startup stays cheap
    return team_honesty.job(payload, on_progress)


HANDLERS["team_honesty"] = _run_team_honesty_job


@app.on_event("startup")
def _start_worker() -> None:
    if get_settings().tenant != "busy" and get_settings().inline_worker:
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
    if get_settings().tenant == "busy" or not get_settings().warm_up_router:
        return

    def run() -> None:
        try:
            t0 = time.perf_counter()
            _router.warm()
            logger.info("router classifier warm in %.1fs", time.perf_counter() - t0)
        except Exception:  # noqa: BLE001 - the on-demand path still works
            logger.warning("router warm-up failed; it will load on the first turn", exc_info=True)

    threading.Thread(target=run_in_scope(run), name="kyra-router-warmup", daemon=True).start()


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
    }, label=(Tier.T2, frozenset({PrivacyClass.job_search})))
    return {"id": job_id, "kind": "latex_resume", "status": "queued"}


class ApplyIn(BaseModel):
    urls: list[str]
    cover_letter: str = "auto"  # auto (only when the posting mentions one) | always | never
    posting_text: str = ""  # for a single non-board URL (LinkedIn, a company site) - pasted text
    company: str = ""
    role: str = ""
    source_url: str = ""  # where the one posting was found (a LinkedIn listing); recorded, never read
    # An application that already has a tailored resume reuses it (apply_pipeline). Set this
    # to spend a fresh multi-minute loop anyway - after the base .tex has changed, say.
    retailor: bool = False


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
    if body.source_url.strip() and len(urls) > 1:
        raise ApiError(400, "source_needs_one_url", "a source (LinkedIn) link belongs to exactly one posting URL")
    _apply_base_latex()  # fail now, not inside every queued job
    ids = [
        _queue.enqueue("apply", {
            "url": u, "cover_letter": body.cover_letter, "posting_text": body.posting_text,
            "company": body.company, "role": body.role, "source_url": body.source_url.strip(),
            "retailor": body.retailor,
        }, label=(Tier.T2, frozenset({PrivacyClass.job_search})))
        for u in urls
    ]
    return {"jobs": [{"id": i, "url": u, "kind": "apply", "status": "queued"} for i, u in zip(ids, urls, strict=True)]}


# Console routes share the registry and the existing authentication middleware.
CONSOLE_PANELS = {
    **dict.fromkeys(("add_reminder", "list_reminders", "complete_reminder", "snooze_reminder"), "tools/reminders"),
    **dict.fromkeys(("save_learning_item", "due_learning_reviews", "mark_learning_reviewed"), "tools/learning"),
    **dict.fromkeys(("add_job_application", "list_job_applications", "update_job_application_status",
                     "set_application_resume", "target_job_posting"), "jobs/tracker"),
    **dict.fromkeys(("add_outreach_contact", "draft_outreach_note", "copy_outreach_note",
                     "update_outreach_status", "list_outreach"), "jobs/outreach"),
    **dict.fromkeys(("start_focus_block", "end_focus_block", "focus_status"), "focus/block"),
    "draft_application_material": "jobs/draft",
    "autofill_job_application": "jobs/autofill",
    "analyze_job_posting": "jobs/apply",
    "save_memory_note": "tools/memory",
    "suggest_initiatives": "tools/initiatives",
    "tech_news": "tools/news",
    "science_facts": "tools/science",
    "search_kyra_data": "search/search",
}


def _approve_action(action_id: str) -> tuple:
    try:
        _registry.approvals.approve(action_id, session="local")
        return _registry.run_approved(action_id, session="local")
    except ApprovalNotFound as exc:
        raise ApiError(404, "action_not_found", str(exc)) from exc
    except ApprovalError as exc:
        raise ApiError(409, "action_conflict", str(exc)) from exc


def _run_tap(name: str, arguments: dict) -> tuple:
    """A deliberate control tap approves precisely the normalized action."""
    try:
        return _registry.dispatch(name, arguments, session="local", tainted=True)
    except ApprovalRequired as exc:
        return _approve_action(exc.pending.id)


@app.get("/api/actions")
def pending_actions() -> dict:
    return {"actions": [
        {"id": action.id, "tool": action.tool, "arguments": action.arguments,
         "created_at": action.created_at, "expires_at": action.expires_at}
        for action in _registry.approvals.pending("local")
    ]}


@app.post("/api/actions/{action_id}/approve")
def approve_action(action_id: str) -> dict:
    result, run_id = _approve_action(action_id)
    return {"result": result, "run_id": run_id}


@app.post("/api/actions/{action_id}/deny")
def deny_action(action_id: str) -> dict:
    try:
        _registry.approvals.deny(action_id, session="local")
    except ApprovalNotFound as exc:
        raise ApiError(404, "action_not_found", str(exc)) from exc
    except ApprovalError as exc:
        raise ApiError(409, "action_conflict", str(exc)) from exc
    return {"result": "Cancelled."}


class HumidifierIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display: StrictBool | None = None
    night_light: StrictBool | None = None
    night_light_brightness: StrictInt | None = None


@cache
def _env_history():
    from companion.home import EnvHistory

    return EnvHistory()


def _utcnow() -> datetime:
    return datetime.now(UTC)


@app.get("/api/room/history")
def room_history(
    metric: Literal["humidity", "target_humidity", "mist_level", "power", "night_light_brightness"] = "humidity",
    hours: int = Query(default=24, ge=1, le=168),
) -> dict:
    from companion.home import HumidifierDriver

    now = _utcnow()
    rows = [row for row in _env_history().series("humidifier.room", metric, now - timedelta(hours=hours))
            if row.slot <= now]
    values = [row.value for row in rows if row.quality == "ok" and row.value is not None]
    return {
        "entity": "humidifier.room", "metric": metric, "unit": HumidifierDriver.metrics[metric],
        "points": [{"slot": row.slot.astimezone(UTC).isoformat(),
                    "value": row.value if row.quality == "ok" else None, "quality": row.quality} for row in rows],
        "summary": {"min": min(values) if values else None, "max": max(values) if values else None,
                    "latest": values[-1] if values else None, "samples": len(values),
                    "unavailable": sum(row.quality == "unavailable" for row in rows)},
    }


@app.get("/api/humidifier")
def humidifier_status() -> dict:
    if "humidifier_status" not in _registry:
        raise ApiError(404, "humidifier_not_configured", "Humidifier is not configured on this server.")
    result = _registry.run("humidifier_status")
    if "error" in result:
        raise ApiError(502, "humidifier_error", str(result["error"]))
    return {"status": result}


@app.post("/api/humidifier")
@app.post("/api/devices/humidifier")
def humidifier_control(body: HumidifierIn) -> dict:
    if "humidifier_control" not in _registry:
        raise ApiError(404, "humidifier_not_configured", "Humidifier is not configured on this server.")
    result, run_id = _run_tap("humidifier_control", body.model_dump(exclude_none=True))
    if "error" in result:
        raise ApiError(400, "humidifier_error", str(result["error"]))
    return {**result, "run_id": run_id}


DEVICES_STALE_S = 30.0


@app.get("/api/devices")
def devices_status() -> dict:
    """Read every configured device at once within the reader's budget (a cold VeSync login or an unreachable
    bulb used to stall the whole call). A slow device keeps its last card for DEVICES_STALE_S, marked
    fresh False, then shows as timed out until a read lands."""
    cards = []
    for card in _space_reader.read_many(("humidifier", "purifier", "bulb")):
        if not card["fresh"] and card["age_s"] is not None and card["age_s"] > DEVICES_STALE_S:
            card = {**card, "available": False, "state": None,
                    "error": f"Device read timed out; the last status is {card['age_s']:.0f} s old."}
        cards.append(card)
    return {"devices": cards}


_space_registry = RoomRegistry()
_space_ledger = GestureLedger()
_space_reader = DeviceReader(_registry)
SPACE_EVENT_INTERVAL_S = 3.0


@app.get("/api/space/things")
def space_things() -> dict:
    return {"things": [thing.model_dump() for thing in _space_registry.things()]}


class SpaceThingIn(Thing):
    @model_validator(mode="before")
    @classmethod
    def server_identity(cls, value):
        if isinstance(value, dict) and "identity" in value:
            raise ValueError("identity is set by the server")
        return value


@app.put("/api/space/things/{thing_id}")
def space_place(thing_id: str, body: SpaceThingIn) -> dict:
    if thing_id != body.id:
        raise ApiError(400, "thing_id_mismatch", "Path and thing id must match.")
    if body.kind == "device":
        card = _space_reader.read(body.device)
        body.identity = identity_of(card["state"]) if card["fresh"] else None
    return {"thing": _space_registry.place(body).model_dump(), "bound": body.identity is not None}


@app.get("/api/space/things/{thing_id}/state")
def space_state(thing_id: str) -> dict:
    thing = _space_registry.get(thing_id)
    if thing is None:
        raise ApiError(404, "thing_not_found", "Thing was not found.")
    if thing.kind != "device":
        raise ApiError(400, "not_a_device", "This thing is not a device.")
    return {"thing": thing.id, "identity": thing.identity, "card": _space_reader.read(thing.device)}


@app.delete("/api/space/things/{thing_id}")
def space_remove(thing_id: str) -> dict:
    if not _space_registry.remove(thing_id):
        raise ApiError(404, "thing_not_found", "Thing was not found.")
    return {"removed": True}


class GestureIn(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    thing: str
    gesture: str
    gesture_id: str
    at: float
    value: float | dict | None = None
    revision: str
    expect: dict | None = None

    @field_validator("gesture_id")
    @classmethod
    def valid_id(cls, value):
        return str(UUID(value))


@app.post("/api/space/gesture")
def space_gesture(body: GestureIn) -> dict:
    try:
        _space_ledger.admit(body.gesture_id, body.at, time.time())
    except GestureRefused as exc:
        raise ApiError(400, "gesture_refused", str(exc)) from exc
    thing = _space_registry.get(body.thing)
    if thing is None:
        raise ApiError(404, "thing_not_found", "Thing was not found.")
    if body.revision != thing.anchor.placed_at:
        raise ApiError(409, "binding_changed", "The thing was placed again; refresh before acting.")
    try:
        tool, arguments = map_gesture(thing, body.gesture, body.value)
    except GestureRefused as exc:
        raise ApiError(400, "gesture_refused", str(exc)) from exc
    if body.expect is not None and body.expect != arguments:
        raise ApiError(400, "gesture_mismatch", "The gesture differs from its displayed action.")
    if tool not in _registry:
        raise ApiError(404, "device_not_configured", "Device is not configured on this server.")
    if thing.identity is None:
        raise ApiError(409, "binding_unconfirmed", "Place the thing again to bind its device.")
    card = _space_reader.read(thing.device)
    if not card["fresh"] or card["state"] is None:
        raise ApiError(503, "device_unreachable", "A fresh device status is unavailable.")
    if identity_of(card["state"]) != thing.identity:
        raise ApiError(409, "identity_mismatch", "The live device differs from the placed device.")
    try:
        _space_ledger.check_freshness(body.at, time.time())
    except GestureRefused as exc:
        raise ApiError(400, "gesture_refused", str(exc)) from exc
    result, run_id = _run_tap(tool, arguments)
    if "error" in result:
        raise ApiError(400, "gesture_failed", str(result["error"]), details=result)
    return {**result, "thing": thing.id, "gesture": body.gesture, "tool": tool,
            "arguments": arguments, "run_id": run_id}


@app.get("/api/space/events")
def space_events(limit: int = Query(default=0, ge=0)) -> StreamingResponse:
    def cards():
        return {"devices": _space_reader.read_many(("humidifier", "purifier", "bulb"))}

    snapshot = {**space_things(), **cards()}

    def stream():
        yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
        count = 1
        while not limit or count < limit:
            time.sleep(SPACE_EVENT_INTERVAL_S)
            yield f"event: devices\ndata: {json.dumps(cards())}\n\n"
            count += 1

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class PurifierIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    power: Literal["on", "off"] | None = None
    mode: Literal["manual", "sleep"] | None = None
    fan_level: StrictInt | None = Field(default=None, ge=1, le=3)
    display: StrictBool | None = None
    night_light: Literal["on", "off"] | None = None
    child_lock: StrictBool | None = None


@app.post("/api/devices/purifier")
def purifier_control(body: PurifierIn) -> dict:
    if "purifier_control" not in _registry:
        raise ApiError(404, "purifier_not_configured", "Purifier is not configured on this server.")
    result, run_id = _run_tap("purifier_control", body.model_dump(exclude_none=True))
    if "error" in result:
        raise ApiError(400, "purifier_error", str(result["error"]), details=result)
    return {**result, "run_id": run_id}


class BulbIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    power: Literal["on", "off"] | None = None
    brightness: StrictInt | None = Field(default=None, ge=1, le=100)
    color_temp: StrictInt | None = Field(default=None, ge=2500, le=6500)
    hue: StrictInt | None = Field(default=None, ge=0, le=360)
    saturation: StrictInt | None = Field(default=None, ge=0, le=100)


@app.post("/api/devices/bulb")
def bulb_control(body: BulbIn) -> dict:
    if "bulb_control" not in _registry:
        raise ApiError(404, "bulb_not_configured", "Bulb is not configured on this server.")
    result, run_id = _run_tap("bulb_control", body.model_dump(exclude_none=True))
    if "error" in result:
        raise ApiError(400, "bulb_error", str(result["error"]), details=result)
    return {**result, "run_id": run_id}


class ToolRunIn(BaseModel):
    input: dict = Field(default_factory=dict)
    confirmed: StrictBool = False


@app.get("/api/tools")
def console_tools() -> dict:
    return {"tools": [
        {**tool.to_schema(), "needs_confirmation": tool.needs_confirmation or (
            tool.side_effect and tool.name not in _registry.preapproved),
         "group": type(tool).__module__.rsplit(".", 1)[-1], "panel": CONSOLE_PANELS.get(tool.name)}
        for tool in _registry
    ]}


@app.post("/api/tools/{name}/run")
def console_run_tool(name: str, body: ToolRunIn) -> dict:
    tool = next((tool for tool in _registry if tool.name == name), None)
    if tool is None:
        raise ApiError(404, "unknown_tool", f"Unknown tool: {name}")
    gated = tool.side_effect and name not in _registry.preapproved
    if (tool.needs_confirmation or gated) and not body.confirmed:
        raise ApiError(409, "confirmation_required", f"Confirm before running {name}.", {"tool": name})
    try:
        if gated:
            result, run_id = _run_tap(name, body.input)
        else:
            result, run_id = _registry.run_with_receipt(name, **body.input)
    except TypeError as exc:
        raise ApiError(400, "tool_input_invalid", str(exc)) from exc
    if isinstance(result, dict) and "error" in result:
        raise ApiError(400, "tool_error", str(result["error"]))
    return {"tool": name, "result": result, "run_id": run_id}


@app.get("/api/tools/runs")
def console_tool_runs(limit: int = Query(50, ge=0, le=500)) -> dict:
    return {"runs": [asdict(run) for run in _registry.audit.list(limit=limit)]}


@app.get("/api/jobs")
def console_jobs(limit: int = Query(20, ge=0, le=500)) -> dict:
    fields = ("id", "kind", "status", "created_at", "started_at", "finished_at", "error")
    return {"jobs": [{key: getattr(job, key) for key in fields} for job in _queue.list(limit=limit)]}


def _console_thread_path(slug: str) -> Path:
    # threads() supplies encoded filenames. Decode once, then use the same safe
    # encoder as the writer; a raw traversal can never become a filesystem path.
    safe = session_log.slug(unquote(slug))
    path = session_log.SESSIONS_DIR / f"{safe}.md"
    if not path.resolve().is_relative_to(session_log.SESSIONS_DIR.resolve()) or not path.is_file():
        raise ApiError(404, "not_found", "Session thread not found.")
    return path


@app.get("/api/agents")
def console_agents() -> dict:
    rows = []
    for slug, modified, _ in session_log.threads():
        path = _console_thread_path(slug)
        rows.append((path.stat().st_mtime_ns, {
            "slug": slug, "modified": modified,
            **session_log.summarize(path.read_text(encoding="utf-8")),
        }))
    return {"repo": session_log.facts(), "threads": [row for _, row in sorted(rows, key=lambda r: r[0], reverse=True)]}


@app.get("/api/agents/{slug:path}")
def console_agent(slug: str) -> dict:
    path = _console_thread_path(slug)
    return {"slug": path.stem, "text": path.read_text(encoding="utf-8")}


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
@release_label(Tier.T2, frozenset({PrivacyClass.job_search}))
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
@release_label(Tier.T2, frozenset({PrivacyClass.job_search}))
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
@release_label(Tier.T2, frozenset({PrivacyClass.job_search}))
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
    notes_block = _draft_notes(warnings)
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


@cache
def _ready_store():
    from companion.ready import ReadyStore
    return ReadyStore()


def _ready_engines():
    return _autofill_engines


_ready_action_lock = threading.RLock()


def _starred_path():
    return DATA_DIR / "job_boards" / "starred.json"


def _read_starred():
    path = _starred_path()
    names = json.loads(path.read_text()) if path.exists() else []
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError("starred.json must be a list of company names")
    return names


class ReadyStarIn(BaseModel):
    company: str = Field(min_length=1, max_length=200)
    starred: StrictBool


@app.post("/api/ready/star")
def star_ready(body: ReadyStarIn) -> dict:
    from companion.ready import ReadyStore
    company = body.company.strip()
    if not company:
        raise ApiError(400, "invalid_company", "A company name is required")
    with ReadyStore(_starred_path()).locked():
        names = _read_starred()
        if body.starred:
            if company.casefold() not in {name.casefold() for name in names}:
                names.append(company)
        else:
            names = [name for name in names if name.casefold() != company.casefold()]
        write_json(_starred_path(), names)
    return {"starred": names}


def _enqueue_prepare(application_id, url):
    store = _ready_store()
    item = next(row for row in store.list() if row["application_id"] == application_id and row["url"] == url)
    return store.enqueue_preparation(item["id"], _queue)[0]


def _ready_item(item_id):
    try:
        return _ready_store().get(item_id)
    except KeyError:
        raise ApiError(404, "not_found", "Review item not found") from None


def _ready_engine(item):
    from companion.scout import board_url
    if not board_url(item["url"]):
        return None
    return engine_for_url(item["url"], _ready_engines())[0]


def _ready_is_live(item):
    if not item.get("window"):
        return False
    # The owning engine's registry identifies a live window, independently of URL parsing.
    for engine in _ready_engines().values():
        try:
            if engine.is_live(item["window"]):
                return True
        except Exception:
            continue
    return False


@app.get("/api/ready")
def list_ready() -> dict:
    from companion.ready import STATES
    with _ready_action_lock:
        starred = _read_starred()
        star_keys = {name.casefold() for name in starred}
        store = _ready_store()
        items = []
        for item in store.list():
            live = item["state"] in {"ready", "needs_input"} and _ready_is_live(item)
            if not live and item["window"]:
                was_ready = item["state"] == "ready"
                store.mark_window_closed(item["window"])
                app_row = next((row for row in _job_store.list() if row.id == item["application_id"]), None)
                if was_ready and app_row and app_row.status == "ready_to_submit":
                    _job_store.update_status(app_row.id, "prepared")
                item = store.get(item["id"])
            items.append({**item, "window_live": bool(live), "starred": item["company"].casefold() in star_keys})
    return {"items": items, "starred": starred,
            "counts": {state: sum(i["state"] == state for i in items) for state in STATES}}


@app.post("/api/ready/{item_id}/open")
def open_ready(item_id: str) -> dict:
    with _ready_action_lock:
        try:
            item = _open_ready(item_id)
        except ApiError as exc:
            # Preserve the standard error envelope/status and include this endpoint's liveness field.
            item = next((row for row in _ready_store().list() if row["id"] == item_id), None)
            error = {"code": exc.code, "message": exc.message}
            if exc.details:
                error["details"] = exc.details
            return JSONResponse(status_code=exc.status_code, content={
                "error": error, "window_live": _ready_is_live(item) if item else False})
        return {**item, "window_live": _ready_is_live(item)}


def _open_ready(item_id: str) -> dict:
    with _ready_action_lock:
        item = _ready_item(item_id)
        store = _ready_store()
        if item["state"] in {"applied", "skipped"} or item["deep"]:
            raise ApiError(409, "not_in_fast_lane", "Review this item deliberately in APPLY")
        if item["resume_path"] is None:
            _enqueue_prepare(item["application_id"], item["url"])
            raise ApiError(409, "not_prepared", "Documents are not prepared yet; preparing now.")
        if item.get("job_id"):
            job = _queue.get(item["job_id"])
            if job and job.status in {"queued", "running"}:
                raise ApiError(409, "preparing", "Documents are still being prepared")
        if _ready_is_live(item):
            return {**item, "window_live": True}
        engine = _ready_engine(item)
        if engine is None or not item["resume_path"]:
            return store.mark(item_id, "needs_input", questions=["A supported form and prepared resume are required."])
        try:
            report = fill_with_receipt(engine, item["url"], replace(load_profile(), resume_path=item["resume_path"]),
                                       via="ready_lane", record_fill=_record_fill)
        except Exception:
            return store.mark(item_id, "needs_input", questions=["Fill failed. Review the form and profile before retrying."])
        questions = [f"{field.label}: {field.reason}" for field in report.skipped if field.required]
        if not report.filled:
            questions.append("No fields were filled; inspect the form manually.")
        window = getattr(report, "window", None)
        if questions:
            _job_store.update_status(item["application_id"], "needs_attention")
            return store.mark(item_id, "needs_input", questions=questions, window=window)
        if not window or not _ready_is_live({**item, "window": window}):
            _job_store.update_status(item["application_id"], "prepared")
            return store.mark(item_id, "prepared", questions=["no live window"])
        store.mark(item_id, "prepared", questions=[])
        _job_store.update_status(item["application_id"], "ready_to_submit")
        return store.mark_ready(item_id, window)


@app.post("/api/ready/{item_id}/applied")
def applied_ready(item_id: str) -> dict:
    from companion.job_applications import FollowUpExists, schedule_follow_up
    with _ready_action_lock:
        item = _ready_item(item_id)
        if item["state"] == "skipped":
            raise ApiError(409, "skipped", "This item was skipped")
        if not any(row.id == item["application_id"] for row in _job_store.list()):
            raise ApiError(404, "not_found", "Application not found")
        # His explicit confirmation remains valid after a submitted page closes.
        if item["state"] != "applied":
            _job_store.update_status(item["application_id"], "applied")
            try:
                schedule_follow_up(_job_store, _reminders_store, item["application_id"], 7)
            except FollowUpExists:
                pass
        return _ready_store().mark(item_id, "applied")


@app.post("/api/ready/{item_id}/skip")
def skip_ready(item_id: str) -> dict:
    with _ready_action_lock:
        item = _ready_item(item_id)
        if item["state"] == "applied":
            raise ApiError(409, "applied", "This item was already applied")
        return _ready_store().mark(item_id, "skipped")


@app.get("/api/ready/{item_id}/document/{kind}")
def ready_document(item_id: str, kind: str):
    item = _ready_item(item_id)
    field = {"resume": "resume_path", "letter": "cover_letter_path"}.get(kind)
    if not field or not item.get(field):
        raise ApiError(404, "not_found", "Document not available")
    path = Path(item[field]).resolve()
    if not path.is_relative_to(DATA_DIR.resolve()) or not path.is_file():
        raise ApiError(404, "not_found", "Document not available in local data")
    return FileResponse(path, filename=path.name, headers={"Cache-Control": "no-store"})


def _scout_store():
    from companion.scout import CandidateStore
    return CandidateStore()


def _scout_watchlist():
    from companion.job_boards import WATCHLIST_PATH
    return WATCHLIST_PATH


@app.get("/api/scout")
def list_scout_candidates() -> dict:
    from companion.scout import STATES
    store = _scout_store()
    candidates = sorted(store.load(), key=lambda c: STATES.index(c.state))
    return {"candidates": [asdict(c) for c in candidates],
            "counts": {state: sum(c.state == state for c in candidates) for state in STATES}}


@app.post("/api/scout/{key}/approve")
def approve_scout_candidate(key: str) -> dict:
    from companion.scout import watch
    store = _scout_store()
    candidate = next((c for c in store.load() if c.key == key), None)
    if candidate is None:
        raise ApiError(404, "not_found", "Candidate not found")
    if candidate.board_url:
        try:
            # The authenticated click authorizes this one candidate; failures remain retryable.
            watch(replace(candidate, state="approved"), _scout_watchlist())
        except ValueError as exc:
            raise ApiError(400, "invalid_board", str(exc)) from None
    store.set_state(key, "approved")
    return {"watching": bool(candidate.board_url),
            "note": "Board added to watchlist" if candidate.board_url else "no board yet"}


@app.post("/api/scout/{key}/reject")
def reject_scout_candidate(key: str) -> dict:
    try:
        candidate = _scout_store().set_state(key, "rejected")
    except KeyError:
        raise ApiError(404, "not_found", "Candidate not found") from None
    return {"candidate": asdict(candidate), "note": "Rejected; existing board watches are unchanged"}


class AddJobApplicationIn(BaseModel):
    company: str
    role: str
    link: str | None = None
    notes: str | None = None
    status: str = "applied"


@app.post("/api/job/applications")
def add_job_application(body: AddJobApplicationIn) -> dict:
    if body.status not in VALID_STATUSES:
        raise ApiError(400, "invalid_status", f"status must be one of {sorted(VALID_STATUSES)}")
    return asdict(_job_store.add(body.company, body.role, body.link, body.notes, status=body.status))


@app.get("/api/job/applications/{app_id}/history")
def job_application_history(app_id: int) -> dict:
    if not any(application.id == app_id for application in _job_store.list()):
        raise ApiError(404, "not_found", f"no application with id {app_id}")
    return {"events": [asdict(event) for event in _job_store.history(app_id)]}


class JobFollowUpIn(BaseModel):
    days: int = Field(gt=0, strict=True)


@app.post("/api/job/applications/{app_id}/follow_up")
def job_application_follow_up(app_id: int, body: JobFollowUpIn) -> dict:
    from companion.job_applications import FollowUpExists, schedule_follow_up

    try:
        reminder = schedule_follow_up(_job_store, _reminders_store, app_id, body.days)
    except LookupError as exc:
        raise ApiError(404, "not_found", str(exc)) from None
    except FollowUpExists as exc:
        raise ApiError(409, "follow_up_exists", str(exc)) from None
    except ValueError as exc:
        raise ApiError(400, "invalid_days", str(exc)) from None
    return {"reminder": asdict(reminder)}


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


def _record_fill(*args, **kwargs) -> None:
    if _registry.audit is not None:
        _registry.audit.record(*args, **kwargs)


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
        report = fill_with_receipt(engine, body.url, profile, via="jobs_panel", record_fill=_record_fill)
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
    if tool == "copy_outreach_note":
        # The dedicated Copy button is approval for this exact action, like ROOM.
        out, _ = _run_tap(tool, kwargs)
    else:
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


# Focus blocks (plan: docs/plans/2026-09-08-attention-environment.md).
#
# These go straight to the store and the planner rather than through _registry.run,
# unlike the outreach tab: there, the logic that matters lives in the tools (a draft
# reads the application's status). Here the logic lives in FocusStore and
# FocusPlanner, and the tools are thin wrappers over the same two objects - so both
# front doors already share one implementation. The stores also share one engine
# (db.engine_for_store caches per URL) and hold no in-memory state, so the tool path
# and the HTTP path cannot disagree about what is running.
#
# The one thing the HTTP layer has that the tool path does not is the synthesis spec:
# the browser cannot make a sound without it. That is why Duc is blind by convention
# and not by construction, and why the panel does not name the arm until the block ends.
_focus_store = FocusStore()
_focus_planner = ScheduledPlanner()


class FocusStartIn(BaseModel):
    minutes: int = 50
    task: str = ""


class FocusEndIn(BaseModel):
    rating: int | None = None
    note: str = ""


class FocusProbeIn(BaseModel):
    id: int
    phase: str
    median_ms: float
    lapses: int


def _plan_for(session) -> FocusPlan:
    """Rebuild the plan for a running block, so a browser reload resumes the same
    sound rather than silently switching arm mid-block."""
    return _focus_planner.plan(
        now=datetime.now().astimezone(),
        completed=0,
        minutes=session.planned_minutes,
        condition=session.condition,
    )


def _focus_payload(session) -> dict:
    """The blind, made structural rather than left to the panel's manners.

    The synthesis spec has to travel - the browser is what makes the sound - but
    the arm's *name* does not, so it is stripped from both the plan and the
    session while the block is running and returned only by /api/focus/end. A
    determined look at the spec still says "noise" or "binaural", so this is a
    real blind against reading the UI and a weak one against reading devtools;
    focus_report.py states that caveat next to its numbers.
    """
    plan = _plan_for(session)
    started = datetime.fromisoformat(session.started_at)
    elapsed = (datetime.now(UTC) - started).total_seconds() / 60
    running = session.ended_at is None
    plan_out, session_out = plan.to_dict(), asdict(session)
    if running:
        plan_out.pop("condition", None)
        session_out.pop("condition", None)
    return {
        "running": running,
        "session": session_out,
        "plan": plan_out,
        "elapsed_minutes": round(elapsed, 2),
        "remaining_minutes": round(max(session.planned_minutes - elapsed, 0), 2),
    }


@app.post("/api/focus/start")
def focus_start(body: FocusStartIn) -> dict:
    try:
        plan = _focus_planner.plan(
            now=datetime.now().astimezone(),
            completed=_focus_store.completed_count(),
            minutes=body.minutes,
        )
        session = _focus_store.start(condition=plan.condition, minutes=plan.minutes, task=body.task)
    except FocusBlockRunning as e:
        raise ApiError(409, "focus_running", str(e)) from e
    except ValueError as e:
        raise ApiError(400, "focus_invalid", str(e)) from e
    return _focus_payload(session)


@app.post("/api/focus/end")
def focus_end(body: FocusEndIn) -> dict:
    session = _focus_store.active()
    if session is None:
        raise ApiError(404, "focus_not_running", "no focus block is running")
    try:
        ended = _focus_store.end(session.id, rating=body.rating, note=body.note)
    except ValueError as e:
        raise ApiError(400, "focus_invalid", str(e)) from e
    except NoActiveFocusBlock as e:
        raise ApiError(404, "focus_not_running", str(e)) from e
    # Unblinding happens here and only here.
    return {"session": asdict(ended), "condition": ended.condition, "probe_delta_ms": ended.probe_delta_ms}


@app.get("/api/focus/active")
def focus_active() -> dict:
    session = _focus_store.active()
    if session is None:
        return {
            "running": False,
            "completed_blocks": _focus_store.completed_count(),
            "evening": theme_for(datetime.now().astimezone()).evening,
        }
    return _focus_payload(session) | {"completed_blocks": _focus_store.completed_count()}


@app.post("/api/focus/probe")
def focus_probe(body: FocusProbeIn) -> dict:
    try:
        session = _focus_store.record_probe(
            body.id, phase=body.phase, median_ms=body.median_ms, lapses=body.lapses
        )
    except ValueError as e:
        raise ApiError(400, "focus_invalid", str(e)) from e
    except KeyError as e:
        raise ApiError(404, "not_found", f"no focus block with id {body.id}") from e
    return {"session": asdict(session)}


@app.middleware("http")
async def _focus_room_no_store(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/focus/") and request.url.path.endswith("/room"):
        if response.status_code == 422:
            response = JSONResponse(
                {"error": {"code": "focus_invalid", "message": "Choose a finished Focus block."}},
                status_code=422,
            )
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/focus/{session_id}/room")
def focus_room_recap(session_id: int) -> dict:
    from companion.db import is_local_personal
    from companion.focus_room import interval, summarize

    if not 0 < session_id < 2 ** 63:
        raise ApiError(422, "focus_invalid", "Choose a finished Focus block.")
    settings = get_settings()
    if (settings.tenant != "personal" or not settings.owner_machine or settings.database_url
            or not is_local_personal(_focus_store._engine, settings)):
        raise ApiError(403, "owner_only", "Room recaps are available on the owner's local Kyra.")
    try:
        session = _focus_store.get(session_id)
    except KeyError:
        raise ApiError(404, "focus_missing", "Focus block not found.") from None
    except Exception:
        raise ApiError(503, "focus_history_unavailable", "Saved Focus history is unavailable.") from None
    if session.ended_at is None or session.abandoned:
        raise ApiError(409, "focus_not_finished", "Choose a finished Focus block.")
    try:
        start, end = interval(datetime.fromisoformat(session.started_at), datetime.fromisoformat(session.ended_at))
    except (ValueError, TypeError, OverflowError):
        raise ApiError(409, "focus_interval_invalid", "This block has no supported finished interval.") from None
    try:
        rows = _env_history().window("humidifier.room", "humidity", start, end)
    except FileNotFoundError:
        return summarize(start, end, []) | {"history_state": "not_recorded"}
    except Exception:
        raise ApiError(503, "room_history_unavailable", "Saved room history is unavailable. Try again later.") from None
    result = summarize(start, end, rows)
    return result | {"history_state": "recorded" if result["samples"] else "empty"}


@app.get("/api/focus/history")
def focus_history(limit: int = 50) -> dict:
    return {"sessions": [asdict(s) for s in _focus_store.list(limit=limit)]}


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


def _checkpoint_access(request: Request) -> None:
    settings = get_settings()
    if not settings.api_token and not (
        settings.trust_loopback and _client_host(request) in {"127.0.0.1", "::1"}
    ):
        raise ApiError(503, "setup_required", "Configure KYRA_API_TOKEN on the Mac before accessing checkpoints remotely.")


_checkpoint_init_lock = threading.Lock()


@cache
def _checkpoint_store() -> CheckpointStore:
    # functools.cache can execute concurrent misses. Serialize schema creation
    # on the single-process Mac server; deployed schemas are migrated before serving.
    with _checkpoint_init_lock:
        return DbCheckpointStore()


@app.get("/api/checkpoints", dependencies=[Depends(_checkpoint_access)])
def list_checkpoints() -> dict:
    return {"checkpoints": [asdict(item) for item in _checkpoint_store().list()]}


@app.post("/api/checkpoints/{checkpoint_id}", dependencies=[Depends(_checkpoint_access)])
def save_checkpoint(checkpoint_id: UUID, body: CheckpointDraft) -> dict:
    try:
        return asdict(_checkpoint_store().save(checkpoint_id, body))
    except CheckpointConflict as exc:
        raise ApiError(409, "revision_conflict", str(exc)) from exc


def _reels_store():
    from sqlalchemy import inspect

    from companion.db import _engine_for, normalize_db_url
    from companion.reels import ReelsStore

    url = get_settings().database_url
    path = DATA_DIR / "reels.db"
    if not url and not path.exists():
        return None
    engine = _engine_for(normalize_db_url(url) if url else f"sqlite:///{path}")
    if not inspect(engine).has_table("reel_moments"):
        engine.dispose()
        return None
    return ReelsStore(engine=engine)


@app.get("/api/reels/due")
def reels_due() -> dict:
    from companion.reels import RightsState, embed_url

    store = _reels_store()
    due = []
    if store is None:
        return {"due": due}
    for moment in store.due("duc", at=_utcnow()):
        source = store.get_source(moment.source_id)
        if source is None or source.rights_state == RightsState.REJECTED:
            continue
        question = moment.questions["initial"]
        due.append({
            "moment_id": moment.id, "kind": "delayed", "learning_objective": moment.learning_objective,
            "embed_url": embed_url(source, moment.start_s, moment.end_s) or source.url,
            "source_title": source.title,
            "question": {"stem": question.stem, "options": [{"text": option.text} for option in question.options]},
        })
    return {"due": due}


class ReelAnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["initial", "transfer", "delayed"]
    chosen: str


@app.post("/api/reels/{moment_id}/answer")
def answer_reel(moment_id: int, body: ReelAnswerIn) -> dict:
    from companion.reels import NotApprovedError, NotDueError, RightsError

    store = _reels_store()
    if store is None or store.get_moment(moment_id) is None:
        raise ApiError(404, "not_found", "Unknown moment")
    try:
        return store.record_attempt("duc", moment_id, body.kind, body.chosen, at=_utcnow()).model_dump()
    except NotApprovedError as exc:
        raise ApiError(403, "not_approved", str(exc)) from exc
    except RightsError as exc:
        raise ApiError(403, "rights_refused", str(exc)) from exc
    except NotDueError as exc:
        raise ApiError(409, "not_due", str(exc)) from exc
    except ValueError as exc:
        raise ApiError(400, "invalid_choice", str(exc)) from exc


@app.get("/api/learning/due")
def learning_due() -> dict:
    return {"due": [asdict(i) for i in _learning_store.due()]}


@app.get("/api/learning/summary")
def learning_summary() -> dict:
    store = _reels_store()
    return {**_learning_store.summary(), "due_reels": len(store.due("duc", at=_utcnow())) if store else 0}


class AddLearningItemIn(BaseModel):
    topic: str
    summary: str
    key_takeaway: str
    request_id: UUID | None = None


@app.post("/api/learning")
def add_learning_item(body: AddLearningItemIn) -> dict:
    try:
        return asdict(_learning_store.add(body.topic, body.summary, body.key_takeaway, request_id=body.request_id))
    except LearningRequestConflict as exc:
        raise ApiError(409, "request_id_conflict", str(exc)) from exc


class MarkReviewedIn(BaseModel):
    remembered: bool
    expected_next_review_at: str | None = None


@app.post("/api/learning/{item_id}/review")
def mark_learning_reviewed(item_id: int, body: MarkReviewedIn) -> dict:
    try:
        result = _learning_store.mark_reviewed(item_id, body.remembered,
                                               expected_next_review_at=body.expected_next_review_at)
    except StaleReview as exc:
        raise ApiError(409, "stale_review", str(exc), {"next_review_at": exc.next_review_at}) from exc
    if not result:
        raise ApiError(404, "not_found", f"no learning item with id {item_id}")
    return result


# Lazy factory: listing an empty installation must not create a store.
def initiative_store():
    from companion.initiative_store import DbInitiativeStore
    return DbInitiativeStore()


@app.get('/api/initiatives')
def list_initiatives():
    from companion.initiative_digest import existing_store
    store = existing_store()
    return {'initiatives': store.list() if store is not None else []}


class DismissInitiativeIn(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


@app.post('/api/initiatives/{initiative_id}/accept')
def accept_initiative(initiative_id: str):
    try:
        return initiative_store().accept(initiative_id)
    except KeyError:
        raise ApiError(404, 'not_found', 'Initiative not found') from None
    except ValueError as exc:
        raise ApiError(409, 'initiative_conflict', str(exc)) from None


@app.post('/api/initiatives/{initiative_id}/dismiss')
def dismiss_initiative(initiative_id: str, body: DismissInitiativeIn):
    try:
        return initiative_store().dismiss(initiative_id, body.reason)
    except KeyError:
        raise ApiError(404, 'not_found', 'Initiative not found') from None
    except ValueError as exc:
        raise ApiError(409, 'initiative_conflict', str(exc)) from None


# Working loop stays personal and loopback-only, independently of the optional LAN token.
@app.middleware("http")
async def _loop_boundary(request: Request, call_next):
    if request.url.path.rstrip("/") in {"/loop", "/api/progress", "/api/reflections", "/api/myself", "/api/attention", "/api/attention/snooze", "/api/scout", "/api/ready"} or request.url.path.startswith(("/api/loop/", "/api/reflections/", "/api/scout/", "/api/ready/")):
        error = None
        if _client_host(request) not in {"127.0.0.1", "::1"}:
            error = "loopback_only"
        elif request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            error = "bad_host"
        elif request.headers.get("origin") is not None:
            # Compare scheme/host/port to stop another local page from spending subscriptions.
            from urllib.parse import urlsplit
            origin = urlsplit(request.headers["origin"])
            if (origin.scheme, origin.netloc) != (request.url.scheme, request.url.netloc):
                error = "bad_origin"
        if error:
            return JSONResponse(status_code=403, content={"error": {"code": error,
                                "message": "Open this page directly on this Mac.", "details": {}}})
    return await call_next(request)


@cache
def _loop_controller():
    from companion.working_loop import PERSONAL_OWNER, DbLoopStore, LoopController, SubprocessRunner
    return LoopController(DbLoopStore(), SubprocessRunner(), owner=PERSONAL_OWNER)


class LoopRunIn(BaseModel):
    tier: str = "work"
    model_config = {"extra": "forbid"}
    choice: str
    prompt: str = Field(min_length=1, max_length=65536)
    topic: str = Field(min_length=1, max_length=160)
    project: str = Field(default="kyra", min_length=1, max_length=160)


@app.get("/loop")
def loop_page() -> HTMLResponse:
    html = (WEB_DIR / "loop.html").read_text(encoding="utf-8")
    for asset in ("loop.js", "loop.css"):
        html = html.replace(f'/static/{asset}"', f'/static/{asset}?v={(WEB_DIR / asset).stat().st_mtime_ns}"')
    return HTMLResponse(html)


@app.get("/model3d")
def model3d_page() -> HTMLResponse:
    """The 3D viewer; its jobs API lives in the model3d router."""
    html = (WEB_DIR / "model3d.html").read_text(encoding="utf-8")
    for asset in ("model3d.js", "model3d.css"):
        html = html.replace(f'/static/{asset}"', f'/static/{asset}?v={(WEB_DIR / asset).stat().st_mtime_ns}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


class AssignmentIn(BaseModel):
    model_config = {"extra": "forbid"}
    code: str
    title: str
    goal: str
    allowed_files: list[str]
    acceptance: list[str]
    tier: str = "work"


class AssignmentAdvanceIn(BaseModel):
    model_config = {"extra": "forbid"}
    status: str
    builder_run_id: int | None = None
    result_sha256: str | None = None
    commit_hash: str | None = None


@app.get("/api/loop/assignments")
def loop_assignments() -> dict:
    controller = _loop_controller()
    return {"assignments": [asdict(a) for a in controller.store.list_assignments(owner=controller.owner)],
            "headless_enabled": get_settings().loop_headless}


@app.post("/api/loop/assignments")
def loop_create_assignment(body: AssignmentIn) -> dict:
    from companion.working_loop import PolicyRefused
    controller = _loop_controller()
    try:
        assignment = controller.store.create_assignment(owner=controller.owner, **body.model_dump())
    except PolicyRefused as exc:
        raise ApiError(400, "policy_refused", str(exc)) from None
    return {"assignment": asdict(assignment)}


@app.post("/api/loop/assignments/{assignment_id}/advance")
def loop_advance_assignment(assignment_id: int, body: AssignmentAdvanceIn) -> dict:
    from companion.working_loop import PolicyRefused
    controller = _loop_controller()
    try:
        assignment = controller.store.advance_assignment(assignment_id, owner=controller.owner, **body.model_dump())
    except PolicyRefused as exc:
        raise ApiError(400, "policy_refused", str(exc)) from None
    except LookupError:
        raise ApiError(404, "not_found", "Assignment not found") from None
    return {"assignment": asdict(assignment)}


@app.get("/api/loop/usage")
def loop_usage() -> dict:
    controller = _loop_controller()
    return {"rows": [{**row, "model_label": short_name(row["developer"])}
                     for row in controller.store.usage_ledger(owner=controller.owner)]}


def _loop_run_record(record, artifact=None) -> dict:
    result = {**asdict(record), "model_label": short_name(record.developer)}
    if record.status == "done" and artifact and artifact.get("output"):
        result["brief"] = asdict(check(artifact["output"]))
    return result


def _loop_dispatcher():
    from companion.dispatch import Dispatcher
    controller = _loop_controller()
    return Dispatcher(controller.store, controller.runner, owner=controller.owner,
                      repo_root=WEB_DIR.parent, worktrees_dir=DATA_DIR / "working_loop" / "worktrees")


def _handoff_tasks():
    from companion import app_tasks
    return (app_tasks.discover_codex(Path.home() / ".codex/sessions")
            + app_tasks.discover_claude(Path.home() / ".claude/sessions"))


def _handoff_runner():
    import subprocess
    return subprocess.run


def _deliveries_store():
    return DATA_DIR / "dispatch/deliveries.json"


def _pins_store():
    return DATA_DIR / "dispatch/destinations.json"


def _destination_tasks():
    root = _loop_dispatcher().repo_root.resolve()
    return [task for task in _handoff_tasks() if Path(task.cwd).expanduser().resolve() == root]


class DestinationPinIn(BaseModel):
    model_config = {"extra": "forbid"}
    app: Literal["codex", "claude"]
    id: str = Field(min_length=1)


@app.get("/api/loop/destinations")
def loop_destinations() -> dict:
    from companion.app_dispatch import load_pins

    pins = load_pins(_pins_store())
    return {"destinations": [{"app": task.app, "id": task.id, "name": task.name,
                              "idle": task.idle, "pinned": pins[task.app] == task.id,
                              "updated_at": task.updated_at}
                             for task in sorted(_destination_tasks(), key=lambda task: task.updated_at, reverse=True)]}


@app.post("/api/loop/destinations/pin")
def pin_loop_destination(body: DestinationPinIn) -> dict:
    from companion.app_dispatch import save_pin

    if not any(task.app == body.app and task.id == body.id for task in _destination_tasks()):
        raise ApiError(404, "destination_not_found", "No such visible conversation in this repository")
    return {"pins": save_pin(body.app, body.id, _pins_store())}


class AssignmentHandoffIn(BaseModel):
    model_config = {"extra": "forbid"}
    app: Literal["codex", "claude"]
    kind: str


@app.post("/api/loop/assignments/{assignment_id}/handoff")
def loop_handoff_assignment(assignment_id: int, body: AssignmentHandoffIn) -> dict:
    from companion.app_dispatch import DuplicateDelivery, StaleResult, load_pins
    from companion.working_loop import PolicyRefused
    try:
        return _loop_dispatcher().handoff(assignment_id, app=body.app, kind=body.kind,
                                         tasks=_handoff_tasks(), store_path=_deliveries_store(), runner=_handoff_runner(),
                                         pins=load_pins(_pins_store()))
    except StaleResult:
        raise ApiError(409, "stale_result", "A previous result exists; review and move it before sending again.") from None
    except DuplicateDelivery:
        raise ApiError(409, "duplicate_delivery", "This assignment already has an open or uncertain delivery.") from None
    except PolicyRefused as exc:
        raise ApiError(409, str(exc), str(exc)) from None
    except LookupError:
        raise ApiError(404, "not_found", "Assignment not found") from None


@app.get("/api/loop/deliveries")
def loop_deliveries() -> dict:
    from companion import app_dispatch
    def read_rollout(path, offset):
        try:
            with Path(path).open("rb") as stream:
                stream.seek(offset)
                return stream.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            return []  # A closed/rotated rollout must not erase the last observation.

    statuses = {task.id: ("idle" if task.idle else "busy")
                for task in _handoff_tasks() if task.app == "claude" and task.idle is not None}
    return {"deliveries": app_dispatch.observe(
        _deliveries_store(), read_rollout=read_rollout, claude_status=statuses.get,
        exists=lambda p: Path(p).is_file() and not Path(p).is_symlink(),
    )}


def _assignment_stage(assignment_id, stage):
    from companion.working_loop import PolicyRefused
    dispatcher = _loop_dispatcher()
    try:
        run = getattr(dispatcher, stage)(assignment_id)
    except PolicyRefused as exc:
        if str(exc) == "headless_disabled":
            raise ApiError(409, "headless_disabled", "Use Hand off to work in the visible conversation.") from None
        raise ApiError(400, "policy_refused", str(exc)) from None
    except LookupError:
        raise ApiError(404, "not_found", "Assignment not found") from None
    return {"assignment": asdict(dispatcher.store.get_assignment(assignment_id, owner=dispatcher.owner)),
            **_enqueue_loop(run)}


@app.post("/api/loop/assignments/{assignment_id}/plan")
def loop_plan_assignment(assignment_id: int) -> dict:
    return _assignment_stage(assignment_id, "plan")


class AssignmentBuildIn(BaseModel):
    model_config = {"extra": "forbid"}
    confirmed: StrictBool


@app.post("/api/loop/assignments/{assignment_id}/build")
def loop_build_assignment(assignment_id: int, body: AssignmentBuildIn) -> dict:
    from companion.working_loop import PolicyRefused
    dispatcher = _loop_dispatcher()
    try:
        assignment, _, _ = dispatcher.check_build(assignment_id, confirmed=body.confirmed)
    except PolicyRefused as exc:
        if str(exc) == "headless_disabled":
            raise ApiError(409, "headless_disabled", "Use Hand off to work in the visible conversation.") from None
        if str(exc) == "confirmation_required":
            raise ApiError(409, "confirmation_required", str(exc)) from None
        raise ApiError(400, "policy_refused", str(exc)) from None
    except LookupError:
        raise ApiError(404, "not_found", "Assignment not found") from None
    job_id = _queue.enqueue("loop_build", {"assignment_id": assignment_id, "confirmed": body.confirmed},
                            label=current_release_label())
    return {"assignment": asdict(assignment), "receipt": None, "job_id": job_id}


@app.post("/api/loop/assignments/{assignment_id}/review")
def loop_review_assignment(assignment_id: int) -> dict:
    return _assignment_stage(assignment_id, "review")


def _run_loop_build_job(payload, on_progress):
    dispatcher = _loop_dispatcher()
    receipt = dispatcher.build(payload["assignment_id"], confirmed=payload["confirmed"])
    on_progress(f"Build exited with code {receipt.returncode}")
    return {"assignment": asdict(dispatcher.store.get_assignment(receipt.assignment_id, owner=dispatcher.owner)),
            "receipt": asdict(receipt)}


HANDLERS["loop_build"] = _run_loop_build_job


@app.get("/api/loop/runs")
def loop_runs(topic: str | None = None) -> dict:
    controller = _loop_controller()
    return {"runs": [_loop_run_record(r, controller.store.read_artifact(r.id, owner=controller.owner)
                                      if r.status == "done" else None)
                     for r in controller.store.list_runs(owner=controller.owner, topic=topic)]}


@app.get("/api/loop/runs/{run_id}")
def loop_run(run_id: int) -> dict:
    controller = _loop_controller()
    record = controller.store.get_run(run_id, owner=controller.owner)
    if not record:
        raise ApiError(404, "not_found", "Run not found")
    artifact = controller.store.read_artifact(run_id, owner=controller.owner)
    return {"run": _loop_run_record(record, artifact), "artifact": artifact,
            "reviews": controller.store.reviews_for(run_id, owner=controller.owner),
            "readiness": controller.readiness(record),
            "reconciliations": controller.store.reconciliations_for(run_id, owner=controller.owner),
            "can_reconcile": controller.can_reconcile(record), "can_continue": controller.can_continue(record)}


def _enqueue_loop(record) -> dict:
    # The queue stores only an id, never prompts or model output.
    _queue.enqueue("loop_dispatch", {"run_id": record.id}, label=current_release_label())
    return {"run": _loop_run_record(record)}


@app.post("/api/loop/runs")
def loop_create(body: LoopRunIn) -> dict:
    from companion.working_loop import PolicyRefused
    try:
        record = _loop_controller().request(project=body.project, topic=body.topic,
                                            choice_key=body.choice, prompt=body.prompt, tier=body.tier)
    except PolicyRefused as exc:
        raise ApiError(400, "policy_refused", str(exc)) from None
    return _enqueue_loop(record)


@app.post("/api/loop/runs/{run_id}/review")
def loop_request_review(run_id: int) -> dict:
    from companion.working_loop import PolicyRefused
    try:
        record = _loop_controller().request_review(run_id)
    except PolicyRefused as exc:
        raise ApiError(409, "review_refused", str(exc)) from None
    return _enqueue_loop(record)


def _run_loop_job(payload, on_progress):
    from companion.working_loop import ReviewRefused
    controller = _loop_controller()
    record = controller.dispatch(payload["run_id"])
    if record.status == "done" and record.review_subject_id:
        try:
            controller.store.add_review(owner=controller.owner, subject_run_id=record.review_subject_id,
                reviewer_run_id=record.id, verdict="comment", artifact_sha256=record.review_subject_sha256)
        except ReviewRefused:
            pass  # A changed artifact never receives a fresh review label.
    on_progress(record.status)
    return {"run_id": record.id, "status": record.status}


HANDLERS["loop_dispatch"] = _run_loop_job


class LoopDecisionIn(BaseModel):
    model_config = {"extra": "forbid"}
    decision: Literal["approve", "reject"]


class LoopContinueIn(BaseModel):
    model_config = {"extra": "forbid"}
    prompt: str = Field(min_length=1, max_length=65536)


@app.post("/api/loop/runs/{run_id}/continue")
def loop_continue(run_id: int, body: LoopContinueIn) -> dict:
    from companion.working_loop import PolicyRefused
    controller = _loop_controller()
    if not controller.store.get_run(run_id, owner=controller.owner):
        raise ApiError(404, "not_found", "Run not found")
    try:
        record = controller.continue_run(run_id, prompt=body.prompt)
    except PolicyRefused as exc:
        raise ApiError(409, "continuation_refused", str(exc)) from None
    return _enqueue_loop(record)


class LoopReconciliationIn(BaseModel):
    model_config = {"extra": "forbid"}
    outcome: Literal["nothing_happened", "provider_processed"]
    note: str = Field(min_length=1, max_length=2000)


@app.post("/api/loop/reviews/{review_id}/decision")
def loop_decide_review(review_id: int, body: LoopDecisionIn) -> dict:
    from companion.working_loop import ReviewRefused
    controller = _loop_controller()
    if not controller.store.get_review(review_id, owner=controller.owner):
        raise ApiError(404, "not_found", "Review not found")
    try:
        decision = controller.decide_review(review_id, decision=body.decision)
    except ReviewRefused as exc:
        raise ApiError(409, "decision_refused", str(exc)) from None
    return {"decision": asdict(decision)}


@app.post("/api/loop/runs/{run_id}/reconcile")
def loop_reconcile(run_id: int, body: LoopReconciliationIn) -> dict:
    from companion.working_loop import PolicyRefused
    controller = _loop_controller()
    if not controller.store.get_run(run_id, owner=controller.owner):
        raise ApiError(404, "not_found", "Run not found")
    try:
        result = controller.reconcile(run_id, outcome=body.outcome, note=body.note)
    except PolicyRefused as exc:
        raise ApiError(409, "reconcile_refused", str(exc)) from None
    return {"reconciliation": asdict(result)}

# Cast pixels stay native. These routes only authorize local viewing.
def _cast_store():
    from companion.cast_sessions import CastSessionStore
    return CastSessionStore(DATA_DIR / "cast" / "devices.json")


def _cast_bridge_secret_path():
    return DATA_DIR / "cast" / "bridge-secret"


def _cast_local(request: Request):
    if _client_host(request) not in _LOOPBACK:
        raise ApiError(403, "cast_auth", "Cast approval requires this Mac")
    origin = request.headers.get("origin")
    if (request.url.hostname not in _LOOPBACK
            or (origin and origin != str(request.base_url).rstrip("/"))):
        raise ApiError(403, "cast_auth", "Cast authorization refused")


def _cast_bridge(request: Request):
    from companion.cast_sessions import bridge_secret
    _cast_local(request)
    expected = bridge_secret(_cast_bridge_secret_path())
    if not secrets.compare_digest(request.headers.get("x-kyra-bridge", ""), expected):
        raise ApiError(401, "cast_auth", "Cast authorization refused")


async def _cast_body(request: Request, fields: dict, optional: frozenset = frozenset()):
    # Do not let validation errors reflect credential values back to a caller.
    size, chunks = 0, []
    async for chunk in request.stream():
        size += len(chunk)
        if size > 4096:
            raise ApiError(400, "cast_auth", "Invalid cast request")
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks))
        if not isinstance(body, dict) or not set(fields) <= set(body) or set(body) - set(fields) - optional:
            raise ValueError()
        for key, kind in fields.items():
            if type(body[key]) is not kind or (kind is str and not 0 < len(body[key]) <= 1024):
                raise ValueError()
        return body
    except (ValueError, TypeError):
        raise ApiError(400, "cast_auth", "Invalid cast request") from None


async def _cast_call(method, **kwargs):
    from starlette.concurrency import run_in_threadpool

    from companion.cast_sessions import CastAuthError, bridge_secret
    try:
        await run_in_threadpool(bridge_secret, _cast_bridge_secret_path())
        return await run_in_threadpool(method, **kwargs)
    except CastAuthError:
        raise ApiError(401, "cast_auth", "Cast authorization refused") from None


@app.post("/api/cast/enroll")
async def cast_enroll(request: Request):
    return await _cast_call(_cast_store().begin_enrollment, **await _cast_body(request, {"name": str}), now=time.time())


@app.post("/api/cast/enroll/{enrollment_id}/approve")
async def cast_approve(enrollment_id: str, request: Request):
    _cast_local(request)
    return await _cast_call(_cast_store().approve, enrollment_id=enrollment_id,
                            **await _cast_body(request, {"code": str}), now=time.time())


@app.get("/api/cast/devices")
async def cast_devices(request: Request):
    _cast_local(request)
    return {"devices": await _cast_call(_cast_store().devices)}


@app.post("/api/cast/devices/{device_id}/revoke")
async def cast_revoke(device_id: str, request: Request):
    _cast_local(request)
    await _cast_call(_cast_store().revoke, device_id=device_id)
    return {"revoked": True}


@app.post("/api/cast/session")
async def cast_session(request: Request):
    cap = await _cast_call(_cast_store().issue_capability,
                          **await _cast_body(request, {"device_id": str, "secret": str}), now=time.time())
    return {"capability": cap}


@app.post("/api/cast/bridge/redeem")
async def cast_redeem(request: Request):
    _cast_bridge(request)
    return await _cast_call(_cast_store().redeem_capability,
                           **await _cast_body(request, {"capability": str}), now=time.time())


@app.post("/api/cast/bridge/ticket")
async def cast_ticket(request: Request):
    _cast_bridge(request)
    ticket = await _cast_call(_cast_store().issue_ticket,
                             **await _cast_body(request, {"session_id": str, "stream_id": str, "epoch": int}), now=time.time())
    return {"ticket": ticket}


@app.post("/api/cast/bridge/verify")
async def cast_verify(request: Request):
    _cast_bridge(request)
    device_id = await _cast_call(_cast_store().verify_ticket,
                                **await _cast_body(request, {"ticket": str, "stream_id": str, "epoch": int}), now=time.time())
    return {"device_id": device_id}


@app.post("/api/cast/bridge/check")
async def cast_check(request: Request):
    _cast_bridge(request)
    return await _cast_call(_cast_store().check_session,
                           **await _cast_body(request, {"session_id": str}), now=time.time())


@app.post("/api/cast/bridge/close")
async def cast_close(request: Request):
    _cast_bridge(request)
    await _cast_call(_cast_store().close_session, **await _cast_body(request, {"session_id": str}))
    return {"closed": True}


@app.post("/api/cast/bridge/health")
async def cast_bridge_health(request: Request):
    _cast_bridge(request)
    return {"ok": True}


@app.post("/api/cast/enroll/{enrollment_id}/claim")
async def cast_claim(enrollment_id: str, request: Request):
    return await _cast_call(_cast_store().claim, enrollment_id=enrollment_id,
                           **await _cast_body(request, {"claim": str}), now=time.time())


@app.get("/api/cast/pending")
async def cast_pending(request: Request):
    _cast_local(request)
    return {"pending": await _cast_call(_cast_store().pending, now=time.time())}


@app.get("/cast-devices")
async def cast_devices_page(request: Request):
    _cast_local(request)
    return FileResponse(WEB_DIR / "cast-devices.html", headers={"Cache-Control": "no-store"})


@app.on_event("startup")
def _cast_startup():
    from companion.cast_sessions import bridge_secret
    bridge_secret(_cast_bridge_secret_path())


@app.middleware("http")
async def _cast_no_cache(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/cast/") or request.url.path == "/cast-devices" or request.url.path.startswith("/cast/artifacts/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _cast_artifacts():
    from companion.cast_artifacts import ArtifactStore
    return ArtifactStore(DATA_DIR / "cast" / "artifacts")


def _cast_breakdown_model():
    from companion.cast_artifacts import breakdown_model
    return breakdown_model()


def _cast_artifact_owner(request: Request):
    token = get_settings().api_token
    if not token or not _authorized(request, token):
        raise ApiError(401, "cast_auth", "Owner authentication required")
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise ApiError(403, "cast_auth", "Cross-origin artifact action refused")


@app.post("/api/cast/artifacts/approve")
async def cast_artifact_approve(request: Request):
    _cast_artifact_owner(request)
    from companion.cast_artifacts import declaration_label
    body = await _cast_body(request, {"freeze_id": str, "digest": str}, frozenset({"source", "declaration"}))
    label = declaration_label(body.pop("source", None), body.pop("declaration", None))
    approval = await _cast_call(_cast_artifacts().approve, **body, session_id="owner", now=time.time(), label=label)
    return {"approval": approval}


@app.post("/api/cast/bridge/release")
async def cast_artifact_release(request: Request):
    from companion.cast_artifacts import MAX_PNG, GraphError
    _cast_bridge(request)
    if request.headers.get("content-type", "").lower() != "image/png":
        raise ApiError(415, "cast_image", "A raw PNG is required")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_PNG:
            raise ApiError(413, "cast_image", "Image exceeds 4 MiB")
        data.extend(chunk)
    try:
        artifact = await _cast_call(_cast_artifacts().release,
            approval=request.headers.get("x-kyra-approval", ""), png=bytes(data),
            freeze_id=request.headers.get("x-kyra-freeze", ""),
            model=_cast_breakdown_model(), now=time.time())
    except GraphError:
        raise ApiError(422, "cast_graph", "The model did not return a valid graph; approval consumed") from None
    return {"artifact_id": artifact["id"]}


def _cast_artifact_get(ident):
    from companion.cast_artifacts import GraphError
    try:
        return _cast_artifacts().get(ident)
    except (GraphError, FileNotFoundError):
        raise ApiError(404, "cast_artifact", "Artifact unavailable") from None


@app.get("/api/cast/artifacts/{artifact_id}")
def cast_artifact_get(artifact_id: str, request: Request):
    _cast_artifact_owner(request)
    return _cast_artifact_get(artifact_id)


@app.post("/api/cast/artifacts/{artifact_id}/open-on-mac")
def cast_artifact_open(artifact_id: str, request: Request):
    _cast_artifact_owner(request)
    _cast_artifact_get(artifact_id)
    _cast_artifacts().request_open(artifact_id)
    return {"queued": True}


@app.get("/api/cast/bridge/opens")
def cast_artifact_opens(request: Request):
    _cast_bridge(request)
    return {"ids": _cast_artifacts().pending_opens()}


@app.get("/cast/artifacts/{artifact_id}")
def cast_artifact_viewer(artifact_id: str, request: Request):
    from fastapi.responses import HTMLResponse
    _cast_local(request)
    graph = json.dumps(_cast_artifact_get(artifact_id)["graph"], ensure_ascii=True).replace("<", "\\u003c")
    page = (WEB_DIR / "cast-artifact.html").read_text().replace("__GRAPH_JSON__", graph)
    return HTMLResponse(page, headers={"Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; base-uri 'none'; frame-ancestors 'none'"})


# Routes are entirely inside the existing protected /api/loop/ prefix.
from companion.workflow_http import router as workflow_router  # noqa: E402

app.include_router(workflow_router)

from companion.team_http import install as install_team  # noqa: E402

install_team(app)

from companion.team_agent_threads import router as agent_threads_router  # noqa: E402

app.include_router(agent_threads_router)

# Kyra 3D jobs: same app-wide token gate as every other /api/ route.
from companion.model3d_http import router as model3d_router  # noqa: E402

app.include_router(model3d_router)
