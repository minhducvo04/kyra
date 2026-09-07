"""A spoken reply has a different shape from a written one, and markdown never
reaches the synthesiser."""
import base64
import io
import sys
import types
import wave

import pytest
from fastapi.testclient import TestClient

from companion import webapp
from companion.conversation import ConversationManager
from companion.persona import KYRA
from companion.voice_text import SPOKEN_REGISTER, spoken_text
from companion.webapp import ChatOut
from tests.fakes import ScriptedLLM


def test_spoken_text_removes_what_cannot_be_said():
    reply = (
        "## Plan\n\n1. Run **tests** first\n- then `ruff`\n\n```python\nprint('x')\n```\n"
        "See [the docs](https://example.com/docs) or https://example.com/raw for *more*."
    )
    assert spoken_text(reply) == "Plan Run tests first then ruff See the docs or a link for more."
    assert spoken_text("plain sentence.") == "plain sentence."
    assert spoken_text("***both***") == "both"
    assert "*" not in spoken_text("**a** and __b__ and _c_")


def _bare_manager(llm):
    class Memory:
        def retrieve(self, *_a, **_k):
            return []

        def add(self, *_a, **_k):
            pass

    class Notes:
        def render(self):
            return ""

    cm = ConversationManager.__new__(ConversationManager)
    cm.llm, cm.memory, cm.memory_notes, cm.history, cm.persona = llm, Memory(), Notes(), [], KYRA
    return cm


def test_voice_register_adds_exactly_one_line_to_the_system_prompt():
    llm = ScriptedLLM(["ok", "ok"])
    cm = _bare_manager(llm)
    cm.handle_turn("hi")
    cm.handle_turn("hi", register="voice")
    written, spoken = (c["system"] for c in llm.calls)
    assert SPOKEN_REGISTER not in written and spoken.endswith(SPOKEN_REGISTER)
    assert spoken.replace("\n\n" + SPOKEN_REGISTER, "") == written  # nothing else changed


@pytest.fixture
def client():
    with TestClient(webapp.app) as c:
        yield c


def _wav_bytes() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 1600)
    return buf.getvalue()


def test_voice_endpoint_asks_for_the_spoken_register_and_synthesises_clean_text(client, monkeypatch):
    seen = {}

    def fake_answer(message, on_token=None, register=None):
        seen["register"] = register
        return ChatOut(reply="**Sure.** Try `pytest` - see [docs](https://x.y).", backend="claude")

    class Tts:
        def speak(self, text):
            seen["spoken"] = text
            return [0.0] * 160, 16000

    class Stt:
        def transcribe(self, pcm):
            return "what should I run"

    # companion.voice pulls in the audio stack; the handler imports it lazily, so stub the module
    fake_voice = types.SimpleNamespace(
        decode_uploaded_audio=lambda f: b"pcm", encode_wav_bytes=lambda audio, rate: _wav_bytes()
    )
    monkeypatch.setitem(sys.modules, "companion.voice", fake_voice)
    monkeypatch.setattr(webapp, "_answer", fake_answer)
    monkeypatch.setattr(webapp, "_rt", types.SimpleNamespace(stt=Stt(), tts=Tts()))

    res = client.post("/api/voice", files={"audio": ("u.webm", b"...", "audio/webm")})
    assert res.status_code == 200, res.text
    body = res.json()
    assert seen["register"] == "voice"
    assert seen["spoken"] == "Sure. Try pytest - see docs."  # what the voice says
    assert body["reply"] == "**Sure.** Try `pytest` - see [docs](https://x.y)."  # what the transcript shows
    assert base64.b64decode(body["reply_audio_b64"])[:4] == b"RIFF"
