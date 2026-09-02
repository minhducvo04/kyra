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
from pathlib import Path

from fastapi import FastAPI, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from companion.conversation import ConversationManager
from companion.default_tools import default_tool_registry
from companion.llm import LazyBackends, build_llm
from companion.memory import ChromaMemoryStore
from companion.persona import KYRA
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
