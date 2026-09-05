# AI Companion — Kyra

A personal AI companion project: persona-driven dialogue, long-term memory via RAG, agentic tools, voice, and eventually animation.

Kyra's personality is warm and protective (Black Widow looking out for the team) crossed with sharp and resourceful (Hermione Granger) — see `docs/design.md` for the full design.

Built as a hands-on learning project for AI pipeline / LLM / agentic RAG experience, alongside prep for a software engineering interview.

## Status

Phase 1 done and verified end-to-end: persona, long-term memory (Chroma-backed `MemoryStore`), and the conversation orchestrator (multi-turn chat + memory persisting and getting recalled across sessions).

Voice I/O is built and verified with a real microphone (2026-09-02) - local, open-source speech-to-text (faster-whisper) and text-to-speech (Kokoro), plus hands-free/push-to-talk listening (`scripts/voice_chat.py`).

Web UI (`scripts/web_ui.py`) with a per-turn router (AUTO/CLAUDE/LOCAL), a JOBS panel (drafting, one-page LaTeX resume fitting with a real compile loop, tracker, Greenhouse autofill) and a TOOLS panel (reminders, news, science, spaced-repetition learning).

Quality bar (2026-09-04): `pytest` suite (56 tests, hermetic - never touches your real `data/`), `ruff` lint, CI workflow, structured logging, a deterministic fact-check guard on every generated resume. See `docs/industry-standards.md` for what changed and why, `docs/v2-outline.md` for where it goes next.

Tests: `pip install -r requirements-dev.txt && python3 -m pytest`

## Setup (run in your own Terminal, not through Claude)

1. `cd` into this folder (drag the folder into Terminal to avoid typing the path).
2. `python3 -m venv .venv`
3. `source .venv/bin/activate`
4. `pip install -r requirements.txt` — this now includes the voice stack (faster-whisper, kokoro-onnx, silero-vad, sounddevice, torch), so it'll take a few minutes and download a few hundred MB. That's a one-time cost.
5. Open `.env` and replace the placeholder with your real Anthropic API key.
6. `python3 scripts/smoke_test.py` — should print a one-line greeting.
   If it fails, run `python3 scripts/debug_api_key.py` instead — it prints the actual error from Anthropic's API instead of a generic message.
7. `python3 scripts/chat.py` — chat with Kyra over text. First run downloads a small embedding model (for memory search) — needs network and takes a few seconds, one time only. She'll remember things across runs (stored in `data/memory_db`, gitignored).

## Voice setup (talk to Kyra out loud)

1. `python3 scripts/setup_voice_models.py` — one-time download of Kokoro's TTS model files (~350MB, into `data/voice_models`, gitignored).
2. `python3 scripts/test_voice_roundtrip.py` — sanity check with **no microphone needed**: Kyra speaks a line, then transcribes her own recording back, so you can see both models are working (and hear how she sounds, if your speakers are on) before trying the live mic. First run also downloads faster-whisper's model (~500MB for the default "small" size).
3. `python3 scripts/voice_chat.py` — the real thing. Defaults to hands-free (just start talking); add `--mode ptt` for push-to-talk (press Enter, speak, press Enter again) instead.

**First time you run it, macOS will pop up a microphone permission prompt for Terminal** — allow it, or Kyra won't hear anything. If you don't see the prompt and nothing's being picked up, check System Settings → Privacy & Security → Microphone and make sure Terminal (or your terminal app) is allowed.

`sounddevice` normally bundles the audio library it needs on macOS, so no extra install should be required. If you see a `PortAudio library not found` error, run `brew install portaudio` and try again.

Everything here runs locally and is free - no API cost, audio never leaves your machine. See the "Voice I/O" section of `docs/design.md` for how it's built and why.
