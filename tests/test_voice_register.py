"""A spoken reply has a different shape from a written one, and markdown never
reaches the synthesiser."""
import base64
import io
import json
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


# --- streaming sentences, so she can start speaking before the reply is finished ---
# Measured 2026-09-08: synthesising the first sentence costs 0.48s against 1.12s
# for the whole reply, and the reply is already streamed. Speaking sentence by
# sentence is what turns 5.3s of silence into a first word around 3.6s.


def test_take_sentences_returns_complete_ones_and_keeps_the_rest():
    from companion.voice_text import take_sentences

    done, rest = take_sentences("Hey Duc. I looked it up")
    assert done == ["Hey Duc."]
    assert rest == "I looked it up"


def test_nothing_is_emitted_until_a_sentence_actually_ends():
    from companion.voice_text import take_sentences

    # Emitting a fragment would make her speak half a thought and then pause.
    assert take_sentences("I was thinking that") == ([], "I was thinking that")


def test_several_sentences_arrive_together():
    from companion.voice_text import take_sentences

    done, rest = take_sentences("One. Two! Three? And then")
    assert done == ["One.", "Two!", "Three?"]
    assert rest == "And then"


def test_a_decimal_or_an_abbreviation_is_not_a_sentence_end():
    from companion.voice_text import take_sentences

    # "3.42" and "e.g." would otherwise chop the audio mid-thought.
    assert take_sentences("Your GPA is 3.42 and") == ([], "Your GPA is 3.42 and")
    done, rest = take_sentences("Use a tool, e.g. search. Then read")
    assert done == ["Use a tool, e.g. search."]
    assert rest == "Then read"


def test_a_finished_reply_flushes_whatever_is_left():
    from companion.voice_text import take_sentences

    # The tail rarely ends in punctuation when the model stops; it still has to be said.
    done, rest = take_sentences("All done here", final=True)
    assert done == ["All done here"] and rest == ""
    assert take_sentences("   ", final=True) == ([], "")


def _voice_stubs(monkeypatch, reply_tokens, whole_reply=None):
    """Stubs shaped like the real thing: STT returns a transcript, TTS records
    each phrase it was asked to say, and _answer streams tokens through on_token
    the way a real Claude text turn does."""
    said: list[str] = []

    def fake_answer(message, on_token=None, register=None):
        for tok in reply_tokens:
            if on_token:
                on_token(tok)
        return ChatOut(reply=whole_reply if whole_reply is not None else "".join(reply_tokens),
                       backend="claude", actual_backend="claude", path="text")

    class Tts:
        def speak(self, text):
            said.append(text)
            return [0.0] * 160, 16000

    class Stt:
        def transcribe(self, pcm):
            return "what did we decide"

    monkeypatch.setitem(sys.modules, "companion.voice", types.SimpleNamespace(
        decode_uploaded_audio=lambda f: b"pcm", encode_wav_bytes=lambda audio, rate: _wav_bytes()))
    monkeypatch.setattr(webapp, "_answer", fake_answer)
    monkeypatch.setattr(webapp, "_rt", types.SimpleNamespace(stt=Stt(), tts=Tts()))
    return said


def _sse(text):
    out = []
    for block in text.strip().split("\n\n"):
        if not block.strip():
            continue
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_voice_stream_sends_the_transcript_before_any_audio(client, monkeypatch):
    # He should see what she heard immediately, not after the whole reply is synthesised.
    _voice_stubs(monkeypatch, ["Hey Duc. ", "I looked it up."])
    with client.stream("POST", "/api/voice/stream", files={"audio": ("u.webm", b"..", "audio/webm")}) as res:
        events = _sse(res.read().decode())
    assert events[0][0] == "transcript"
    assert events[0][1]["transcript"] == "what did we decide"


def test_each_sentence_is_synthesised_and_sent_as_it_completes(client, monkeypatch):
    said = _voice_stubs(monkeypatch, ["Hey Duc. ", "I looked ", "it up. ", "Nothing there."])
    with client.stream("POST", "/api/voice/stream", files={"audio": ("u.webm", b"..", "audio/webm")}) as res:
        events = _sse(res.read().decode())

    audio = [payload for kind, payload in events if kind == "audio"]
    assert [a["text"] for a in audio] == ["Hey Duc.", "I looked it up.", "Nothing there."]
    assert said == ["Hey Duc.", "I looked it up.", "Nothing there."], "one synthesis call per sentence"
    assert base64.b64decode(audio[0]["b64"])[:4] == b"RIFF"
    assert events[-1][0] == "done" and events[-1][1]["reply"].startswith("Hey Duc.")


def test_markdown_never_reaches_the_synthesiser_per_sentence(client, monkeypatch):
    said = _voice_stubs(monkeypatch, ["**Sure.** ", "Run `pytest` now."])
    with client.stream("POST", "/api/voice/stream", files={"audio": ("u.webm", b"..", "audio/webm")}) as res:
        res.read()
    assert said == ["Sure.", "Run pytest now."]


def test_a_turn_that_cannot_stream_still_speaks_the_whole_reply(client, monkeypatch):
    # Tool turns have no on_token at all; the audio must not silently vanish.
    said = _voice_stubs(monkeypatch, [], whole_reply="I added that reminder for you.")
    with client.stream("POST", "/api/voice/stream", files={"audio": ("u.webm", b"..", "audio/webm")}) as res:
        events = _sse(res.read().decode())
    assert said == ["I added that reminder for you."]
    assert [k for k, _ in events] == ["transcript", "audio", "done"]


def test_silence_in_gets_no_audio_out(client, monkeypatch):
    said = _voice_stubs(monkeypatch, ["ignored"])

    class Deaf:
        def transcribe(self, pcm):
            return "   "

    monkeypatch.setattr(webapp._rt, "stt", Deaf())
    with client.stream("POST", "/api/voice/stream", files={"audio": ("u.webm", b"..", "audio/webm")}) as res:
        events = _sse(res.read().decode())
    assert [k for k, _ in events] == ["transcript", "done"]
    assert said == []


# --- speaking text that is already written -------------------------------------
# The visionOS client types a turn through /api/chat/stream and then needs to
# hear the reply. /api/voice/stream takes audio in, so it cannot serve that; this
# is the same sentence-by-sentence synthesis with the STT and the turn removed.


def test_speak_streams_one_audio_chunk_per_sentence(client, monkeypatch):
    said = []

    class Tts:
        def speak(self, text):
            said.append(text)
            return [0.0] * 160, 16000

    monkeypatch.setitem(sys.modules, "companion.voice", types.SimpleNamespace(
        encode_wav_bytes=lambda audio, rate: _wav_bytes()))
    monkeypatch.setattr(webapp, "_rt", types.SimpleNamespace(tts=Tts()))

    with client.stream("POST", "/api/speak", json={"text": "**Sure.** Run `pytest` now. Then read it."}) as res:
        assert res.status_code == 200
        events = _sse(res.read().decode())

    audio = [p for kind, p in events if kind == "audio"]
    # Markdown is stripped per sentence, exactly as on the voice path.
    assert said == ["Sure.", "Run pytest now.", "Then read it."]
    assert [a["text"] for a in audio] == said
    assert base64.b64decode(audio[0]["b64"])[:4] == b"RIFF"
    assert events[-1][0] == "done"


def test_speaking_nothing_is_rejected_rather_than_answered_with_silence(client):
    assert client.post("/api/speak", json={"text": "   "}).status_code == 400


def test_text_with_no_sayable_content_still_ends_cleanly(client, monkeypatch):
    # A reply that is only a code fence has nothing a voice can say; the caller
    # needs `done`, not a stream that stops without explanation.
    said = []

    class Tts:
        def speak(self, text):
            said.append(text)
            return [0.0] * 160, 16000

    monkeypatch.setitem(sys.modules, "companion.voice", types.SimpleNamespace(
        encode_wav_bytes=lambda audio, rate: _wav_bytes()))
    monkeypatch.setattr(webapp, "_rt", types.SimpleNamespace(tts=Tts()))
    with client.stream("POST", "/api/speak", json={"text": "```\ncode only\n```"}) as res:
        events = _sse(res.read().decode())
    assert said == []
    assert [k for k, _ in events] == ["done"]
