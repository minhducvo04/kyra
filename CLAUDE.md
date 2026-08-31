# Companion project (Kyra) — Claude Code instructions

An AI companion with persona-driven dialogue, long-term memory (RAG), and voice I/O. Built as a hands-on AI-pipeline learning project, alongside prep for a final-round interview — the project *is* the interview practice (planning, LLD, implementation, debugging, done for real).

Full design rationale: `docs/design.md`. Setup/usage: `README.md`. Don't duplicate those here — this file is for things you wouldn't get just by reading the code.

## Commands

- Setup: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
- Text chat: `python3 scripts/chat.py`
- Voice chat: `python3 scripts/voice_chat.py` (`--mode ptt` for push-to-talk; default is hands-free)
- Voice model download (one-time): `python3 scripts/setup_voice_models.py`
- Voice sanity check, no mic needed: `python3 scripts/test_voice_roundtrip.py`
- API key sanity check: `python3 scripts/smoke_test.py`, or `python3 scripts/debug_api_key.py` for a detailed error if it fails
- No test suite yet (no pytest). Verification so far has been targeted scripts like the ones above. If you add real tests, use pytest under `tests/`.
- Scripts import via `sys.path.insert(0, ".../src")` rather than an editable install — keeps setup to plain `pip install -r requirements.txt`, no build step. Follow the same pattern in new scripts.

## Architecture pattern — preserve this

Every subsystem is a small ABC interface + a swappable concrete backend (Strategy pattern), specifically because this project doubles as interview practice — "how would you extend this" should always have a clean answer. Existing examples:

- `MemoryStore` → `ChromaMemoryStore` (`src/companion/memory.py`)
- `SpeechToText` → `FasterWhisperSTT`, `TextToSpeech` → `KokoroTTS` (`src/companion/voice.py`)
- `ListenMode` → `PushToTalkListener`, `VoiceActivityListener` (`src/companion/listening.py`)

When adding a new subsystem (agentic tools are next up), follow the same shape: a one-or-two-method ABC, a concrete class that does the real work, callers depend only on the interface.

## Key decisions worth knowing

- **Voice is local/open-source on purpose** (faster-whisper + Kokoro + Silero VAD), not a cloud API like ElevenLabs/OpenAI/Deepgram. Chosen for $0/turn (this gets tested a lot), privacy, and because running real local inference is more of the point of this project than calling someone else's endpoint. The interface boundary makes a cloud backend a future drop-in class, not a rewrite, if a polished demo ever wants one.
- **Voice turn-taking is strictly sequential** (listen, then speak, then listen again) — not concurrent. This avoids Kyra hearing her own TTS output through the mic, without needing real echo cancellation. `voice_chat.py`'s loop only calls `listener.listen()` again after `sd.wait()` returns. "Barge-in" (interrupting her mid-reply) is a known future upgrade, not a bug.
- **Silero VAD requires exactly 512-sample chunks at 16kHz** (`VoiceActivityListener.CHUNK_SAMPLES` in `listening.py`) — it raises "Input audio chunk is too short" on anything else. Don't change this constant without re-verifying against the installed silero-vad version.
- **Sample rate contract**: `SpeechToText.transcribe()` always expects mono float32 audio at 16kHz (`companion.voice.SAMPLE_RATE`). Kokoro's TTS output is 24kHz — resample before feeding it back into STT (see `test_voice_roundtrip.py`'s `resample()` for the pattern used so far).

## Verified vs. not (as of 2026-08-31 — update this section as things get confirmed)

Built and verified end-to-end: text chat (`chat.py` — persona + memory + multi-turn, including memory correctly recalled across a simulated new session), `KokoroTTS` (real synthesis, correct shape/dtype/rate), `VoiceActivityListener`'s VAD scoring (tested on synthetic audio chunks).

Built but **not yet run by anyone with real hardware**: `scripts/voice_chat.py` end-to-end, live `faster-whisper` transcription, anything touching an actual microphone or speaker, push-to-talk mode. This is the immediate next step — if you're picking this up, that's probably why.

## Working practices

- **Never `cat`, `diff`, or otherwise print the full contents of `.env`.** It holds a live Anthropic API key. Two earlier sessions accidentally leaked a real key into a conversation this way (both keys were revoked after). For diagnostics, check only length/prefix/whitespace, like `scripts/debug_api_key.py` does.
- Model files and local databases (`data/`, including `data/voice_models/`) are gitignored on purpose — don't commit them, don't remove them from `.gitignore`.
- `git` identity for this repo is set locally (not global) to Minh Duc Vo / a private address — that's deliberate, leave it repo-local.
- This project also has job-search working docs under data/private_docs/ (gitignored).
