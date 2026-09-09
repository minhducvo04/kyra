# Kyra

A personal AI companion, built to learn the whole AI pipeline by shipping one: persona-driven
dialogue, long-term memory over retrieval, agentic tools, local and hosted models side by side,
and voice in and out.

It runs on a laptop. Text chat, a voice loop, a browser HUD, and a visionOS client all talk to the
same conversation core. Design rationale is in [docs/design.md](docs/design.md).

## What is worth looking at

Each of these was measured rather than assumed, and the write-ups keep the numbers that did not
flatter the decision.

| Piece | Where | The short version |
|---|---|---|
| Per-turn router | [src/companion/router.py](src/companion/router.py) | Decides tool vs text, and local vs hosted, on every turn. A fine-tuned 1.5B classifier replaced a few-shot 3B prompt: 91.7% against 73.8%, on 122 prompt tokens instead of 2,051. [docs/router-finetune.md](docs/router-finetune.md) |
| Hybrid search | [src/companion/search.py](src/companion/search.py) | SQLite FTS5 and Chroma over the same chunks, fused by reciprocal rank. Vector-only scored 69.2% recall against lexical 88.5%, because embeddings miss identifiers. [docs/search-eval.md](docs/search-eval.md) |
| One-page resume fitting | [src/companion/job_applications.py](src/companion/job_applications.py) | A real compile, measure, iterate loop. It shells out to LaTeX, reads the page count back from the PDF, and feeds the measured overflow into the next pass, because a model editing LaTeX cannot know what it renders to. |
| Tool-calling distillation | [docs/tool-calling-distill.md](docs/tool-calling-distill.md) | Trained a local 7B to take over tool calls, measured 80.0% against the teacher's 90.0%, and did not ship it. The negative result is the point. |
| Two memory layers | [memory.py](src/companion/memory.py), [memory_notes.py](src/companion/memory_notes.py) | Retrieval over every exchange, plus a small curated set of facts loaded in full on every turn. Top-k retrieval misses a durable fact on an off-topic turn, so the two layers do different jobs. |
| Voice latency | [docs/voice-latency.md](docs/voice-latency.md) | Measured end to end, then shortened by speaking the first sentence while the rest is still being written. First word at 3.61s against 4.45s. |

Everything follows one shape on purpose: a small interface, a swappable concrete backend. Memory,
speech, listening, models, tools, job boards, search and job queues are each an abstract base class
with a real implementation behind it, so "how would you extend this" always has an answer that is
not a rewrite. [CLAUDE.md](CLAUDE.md) is the engineering log, and it records the bugs and the
reversals as carefully as the wins.

## Running it

Tested on macOS with Apple silicon. The local model and voice paths use MLX and want that hardware.
Text chat against the hosted model works anywhere Python does.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then put a real Anthropic API key in it
python3 scripts/smoke_test.py
```

The install includes the voice stack, so it pulls a few hundred megabytes once. If the smoke test
fails, `python3 scripts/debug_api_key.py` prints the API's own error instead of a generic one.

Then pick a front door:

```bash
python3 scripts/chat.py      # text
python3 scripts/web_ui.py    # browser HUD at http://127.0.0.1:8420
python3 scripts/voice_chat.py
```

The first run downloads a small embedding model for memory search. Conversations persist in
`data/memory_db`, which is gitignored along with the rest of `data/`.

### Voice

```bash
python3 scripts/setup_voice_models.py     # one time, ~350MB of Kokoro TTS
python3 scripts/test_voice_roundtrip.py   # no microphone needed
python3 scripts/voice_chat.py             # hands-free; --mode ptt for push-to-talk
```

Speech to text is faster-whisper, speech out is Kokoro, both local. No per-turn cost, and audio
never leaves the machine. The first roundtrip also pulls the Whisper model, about 500MB.

macOS asks for microphone permission the first time. If nothing is picked up and no prompt appeared,
check System Settings, then Privacy and Security, then Microphone. On a `PortAudio library not
found` error, `brew install portaudio`.

## Tests and lint

```bash
pip install -r requirements-dev.txt
python3 -m pytest          # 493 tests, about 20 seconds
ruff check src scripts tests
```

The suite is hermetic. `tests/conftest.py` repoints the data directory at a temp path before any
import, so a test run can never touch real conversations, resumes or databases. Tests that need a
LaTeX toolchain skip cleanly when one is absent. Both commands run in CI.

## Layout

```
src/companion/   the library: models, memory, voice, router, tools, search, job pipeline
scripts/         one thin CLI per entry point, each over a module in src/
web/             the browser HUD
apple/           visionOS client (SwiftUI, hand-written xcodeproj)
docs/            design, benchmarks, evaluations, and dated plans
deploy/          Terraform for the container shape on AWS, never applied
tests/           493 tests plus the handwritten held-out sets the evals score against
```

## Boundaries that are deliberate

Some things are missing by decision rather than by backlog, and the reasons are in
[CLAUDE.md](CLAUDE.md):

- Job application autofill fills a form and stops. A human reviews and submits.
- Nothing automates LinkedIn. Its terms forbid it, so outreach drafting hands over a clipboard.
- Voice is local and open source, not a cloud speech API.
- Generated resume text runs through a check that flags any number, link or proper noun absent from
  the sources the model was given.

## License

MIT, see [LICENSE](LICENSE). Vendored skill files under `.claude/skills/` keep their own upstream
notices; see the attribution file there.
