# Design: Kyra — AI Companion (v1)

## Overview

Kyra is a persistent AI companion: warm and protective (think Black Widow looking after the team) with a sharp, resourceful, "I did the reading" energy (think Hermione Granger). She remembers things about Duc across conversations, not just within one session — that long-term memory is the RAG core of this project.

This doc covers v1 scope: **text-only, persona + long-term memory**. Tools, voice, and animation are backlog items layered on top once v1 works end to end.

## Requirements

**Functional**
- Hold an in-character conversation as Kyra.
- Remember things across sessions — not just the current chat window, but weeks later.
- (Backlog, not v1) Use tools; speak; show expression via an avatar.

**Non-functional**
- Extensible: swapping the memory backend or adding a tool shouldn't require touching the core conversation loop.
- Inspectable: possible to see *which* memories got retrieved for a given reply — good practice, and useful for debugging "why did she say that."
- Cheap to run for a hobby project: local vector store, no paid infra required for v1.

## Concepts (the RAG part, explained once, used everywhere below)

- **Embedding**: text turned into a list of numbers (a vector) such that similar meanings land as nearby vectors. "I love hiking" and "I enjoy trails" end up close together even without sharing words.
- **Vector store**: a database built to answer "which stored vectors are closest to this new vector?" quickly. That's the trick behind memory retrieval.
- **Retrieval**: before Kyra answers, we embed the user's message, ask the vector store for the closest-matching stored memories, and hand those to the LLM as extra context. That's RAG — Retrieval-Augmented Generation.

## Architecture

Five small pieces, each behind a narrow interface so any one can be swapped later without touching the others:

```
Persona              -- who Kyra is (name, traits, tone) -> builds the system prompt
MemoryRecord         -- one stored memory (text, metadata, similarity score)
MemoryStore          -- interface: add() / retrieve()  ->  ChromaMemoryStore is the v1 implementation
Message              -- one turn in the current session (role, content)
ConversationManager  -- orchestrates a turn: retrieve -> build prompt -> call LLM -> store -> return
```

### `Persona`
Holds Kyra's character definition, turns it into a system prompt. Kept as data (a traits list + a few example lines) rather than hardcoded prose, so tone can be tuned without touching logic.

### `MemoryStore` (interface) → `ChromaMemoryStore`
```python
class MemoryStore(ABC):
    def add(self, text: str, metadata: dict) -> None: ...
    def retrieve(self, query: str, k: int = 5) -> list[MemoryRecord]: ...
```
`ChromaMemoryStore` is the concrete v1 backend, using Chroma's built-in local embedding model — no extra API calls or keys just to store/search memories. Swapping to a hosted vector DB later only touches this one class.

### `ConversationManager`
The orchestrator. One method, `handle_turn(user_input)`:
1. Retrieve relevant memories for `user_input`.
2. Build the full prompt: persona system prompt + retrieved memories + recent session history + the new message.
3. Call the LLM (Claude).
4. Store this exchange as a new memory.
5. Return the reply.

This is the class every future agentic tool plugs into later (Feature Backlog #1) — the tool-decision step slots in between steps 2 and 3.

## Why this shape (the LLD angle)

`MemoryStore` being an abstract interface rather than "just import chromadb everywhere" is the actual pattern interview test for with "how would you extend this design." Same reasoning will apply to `Tool` later — an interface now means adding a new tool or memory backend is additive, not a rewrite.

## Out of scope for v1

Tools, voice, animation, multi-agent orchestration, evals — see Feature Backlog in the Prep Roadmap.

## Voice I/O (Feature Backlog #2)

Kyra can now listen and speak, not just read and write. Same architectural trick as memory: two more interfaces, each with a swappable local/open-source backend.

**`SpeechToText`** (`src/companion/voice.py`) - one method, `transcribe(audio) -> str`. Backend: `FasterWhisperSTT`, a CTranslate2-optimized reimplementation of OpenAI's Whisper, running entirely on-device.

**`TextToSpeech`** (`src/companion/voice.py`) - one method, `speak(text) -> (audio, sample_rate)`. Backend: `KokoroTTS`, an 82M-parameter open-source model (Apache 2.0) small enough to run comfortably on a laptop CPU.

**`ListenMode`** (`src/companion/listening.py`) - one method, `listen() -> audio`, blocks until a full spoken utterance is captured. Two backends:
- `PushToTalkListener`: press Enter, speak, press Enter again.
- `VoiceActivityListener`: hands-free - Silero VAD scores each ~32ms chunk of live mic audio as speech or silence, auto-starting the capture when it hears you and auto-stopping after a pause. Selected as the default per Duc's "hands free while working" preference; push-to-talk stays available via `--mode ptt`.

**Why local models instead of a cloud API (ElevenLabs/OpenAI/Deepgram)?** Cost and iteration speed, mainly: this project gets tested a lot, and $0 per turn means never watching a meter while iterating. It's also strictly more of the hands-on AI-pipeline experience the project is for - running real inference locally, not just calling someone else's endpoint. The interface boundary makes this a reversible choice: an `ElevenLabsTTS(TextToSpeech)` backend later, if a polished demo wants maximum expressiveness, is a new class, not a rewrite.

**Why sequential turn-taking, not always-listening-while-she-talks?** Simpler, and it sidesteps Kyra hearing and responding to her own voice through the speakers (the classic voice-assistant echo problem) without needing real echo cancellation. `voice_chat.py`'s loop only calls `listener.listen()` again after her reply is done playing *or interrupted*. Full concurrent listen-while-speaking (true voice-triggered barge-in) still isn't implemented, and still needs echo cancellation to work reliably - that stays a Backlog item.

**Barge-in, v1 (keypress, not voice).** `speak_interruptibly()` in `voice_chat.py` plays her reply via `sd.play()` and polls `sys.stdin` with a short `select.select()` timeout instead of blocking on `sd.wait()`; pressing Enter calls `sd.stop()` and moves straight to listening for the next turn. Deliberately not voice-triggered: the mic would also pick up her own TTS output through the speakers, and without real echo cancellation that risks her cutting herself off. Keypress sidesteps the whole problem and reuses the same "press Enter" vocabulary push-to-talk already has.

**What's verified vs. not (as of this writing):** `KokoroTTS` was tested end-to-end - model loads, synthesizes real audio, correct shape/dtype/sample rate. `VoiceActivityListener`'s VAD scoring was tested on synthetic audio chunks (correctly scores silence/noise low). `FasterWhisperSTT` is API-verified (exact method signature confirmed) but its live transcription wasn't run in dev, since the STT model download was network-blocked in the sandbox it was built in. Nothing involving an actual microphone or speaker has been tested by anyone yet - that needs a real machine with real audio hardware, which is Duc's Mac.
