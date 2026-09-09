# Kyra: instructions for coding agents

This is the one instruction file for every agent that works in this repository: Claude Code (which loads it through `CLAUDE.md`), OpenAI Codex (which loads `AGENTS.md` natively), and a human reading along. Keep it under 32 KB, because Codex truncates project instructions past that by default. Long-form history goes in `docs/log/`, never here.

Kyra is an AI companion: persona-driven dialogue, long-term memory over retrieval, agentic tools, local and hosted models side by side, voice in and out. It is a hands-on AI-pipeline learning project run the way a real engineering project is run: plans, tests, real-run verification, and an honest record of what did not work. The owner is Duc (Minh Duc Vo); the code and the docs call him by name.

**The repository is public** (`github.com/minhducvo04/kyra`, since 2026-09-09). Everything committed is visible to strangers. Section 2 is not optional.

## 1. Where things are

| Path | What it holds |
|---|---|
| `AGENTS.md` | This file: rules, commands, architecture, workflow. |
| `CLAUDE.md` | Claude Code entry point. Imports this file, adds Claude-only notes (skills, model table). |
| `README.md` | The public front page: what was measured, how to run it. |
| `docs/README.md` | Index of every design doc, benchmark, evaluation and plan. |
| `docs/log/` | **The engineering log**: every dated decision, bug and verification, one file per topic. Read the topic file before touching its subsystem (section 7). |
| `docs/plans/` | Dated plans, `<date>-<topic>.md`, each step written `[step] -> verify: [check]`. |
| `docs/*.md` | Design rationale, benchmarks, evaluations (`design.md`, `router-finetune.md`, `search-eval.md`, `industry-standards.md`, ...). |
| `src/companion/` | The library. One module per subsystem; `webapp.py` is the HTTP layer; `default_tools.py` assembles the tool registry all front doors share. |
| `scripts/` | One thin CLI per entry point, each over a module in `src/`. |
| `tests/` | pytest suite, plus handwritten held-out sets in `tests/data/` that the evals score against. Never train on those. |
| `web/` | The browser HUD (`index.html`, `app.js`, `style.css`, `login.html`). |
| `apple/KyraVision/` | visionOS client (SwiftUI, hand-written xcodeproj). |
| `migrations/` | Alembic revisions for the relational stores. |
| `deploy/` | Terraform for AWS (never applied) and the launchd plist for the 05:00 digest. |
| `.claude/skills/` | Vendored `SKILL.md` files (MIT, see `ATTRIBUTION.md` there). Plain markdown: any agent can read one and follow it. |
| `data/` | **Gitignored. All personal and runtime state.** See section 2. |
| `.env` | **Gitignored. The live Anthropic API key.** Never print it. |

## 2. Private data: what is here and what must never leave

The repo is public, and the owner's real job search runs through this code. `.gitignore` and a PII test are two lines of defence; you are the third.

**What is private, and where it lives (all gitignored, all real):**
- `data/` in full: `memory_db/` (every conversation), `memory_notes/` (what Kyra durably believes about Duc), `applicant_profile.json` (name, phone, email, links), `resumes/`, `cover_letters/`, `job_descriptions/`, `job_documents/` (uploaded resumes and notes), `outreach.db` (**real people's names and LinkedIn URLs**), `job_applications.db`, `reminders.db`, `learning.db`, `focus.db`, `digests/`, `private_docs/` (job-search notes, interview prep, the publish checklist), `router.log`, model weights and fine-tune adapters.
- `.env`: the live API key and tokens. `.env.example` is the tracked template.
- `handoff/`: drafts that can carry conversation context. `.claude/worktrees/`, `.venv/`, caches.

**Rules, in the order their failures hurt:**
1. **Never copy content from `data/` or `.env` into a tracked file.** Not a name from `outreach.db`, not a line from `private_docs/`, not an employer from a job-search note, not a phone number, not resume text. If a doc must point at private material, reference the *path*. The one fictional contact for fixtures and examples is **Alex Rivera at Northwind, `linkedin.com/in/alex-example`**.
2. **Never print `.env`.** No `cat`, `diff`, `grep`, or `source` followed by `echo`. Diagnostics check length and prefix only, as `scripts/debug_api_key.py` does. Two earlier sessions leaked a real key this way; both keys had to be revoked.
3. **Never `git add -f`**, never narrow `.gitignore`, never stage anything under `data/`. Before every commit run `git status --porcelain` and confirm: nothing under `data/`, no `.env*` except `.env.example`, no file with a personal-document extension (`*.pdf`, `*.docx`, `*.tex`, `*.pages`, ...; all gitignored on purpose, and the repo tracks zero of them).
4. **Never push, never rewrite history, never change repository visibility.** Commit locally; pushing is Duc's action. The history was scrubbed and the repo recreated on 2026-09-09; a bad push cannot be undone once someone has cloned it.
5. **`tests/test_no_third_party_pii.py` is a guard, not a formality.** It scans every tracked file for identifiers of real people who once leaked into fixtures. If it fails, fix the file it names; never edit the test to pass.
6. **Tests are hermetic by construction.** `tests/conftest.py` repoints `KYRA_DATA_DIR` at a temp dir before any import, so `pytest` can never touch the real `data/`. A real run against the running app can, so either point `KYRA_DATA_DIR` at a scratch copy of `data/` or clean up afterwards (section 5, test pollution).

If a task seems to need one of these broken, stop and say so. The answer is almost always "reference the path" or "use the fictional contact".

## 3. Commands

Setup and checks:
- `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`, then `cp .env.example .env` and put a real key in it. `python3 scripts/smoke_test.py` proves the key; `scripts/debug_api_key.py` prints the API's own error if it fails.
- **Tests:** `pip install -r requirements-dev.txt` once, then `python3 -m pytest` (493 tests, about 20 s; LaTeX-backed tests skip without `pdflatex`). **Lint:** `ruff check src scripts tests`. Both run in CI (`.github/workflows/ci.yml`). Read the count off pytest's summary line, never the dots.
- One-time downloads: `scripts/setup_voice_models.py` (Kokoro), `python3 -m playwright install chromium` (autofill), a LaTeX toolchain on PATH (`pdflatex`/`xelatex`, not a pip package). `scripts/test_voice_roundtrip.py` checks voice with no microphone.

Front doors (all share `ConversationManager`, `LLMBackend`, `TurnRouter`, and `default_tool_registry()`):
- `python3 scripts/chat.py` (text), `python3 scripts/voice_chat.py` (`--mode ptt` for push-to-talk; interrupt key while she speaks), `python3 scripts/web_ui.py` (HUD at http://127.0.0.1:8420 with JOBS / TOOLS / SEARCH / FOCUS panels and voice). All take `--backend auto|claude|local`; `auto` is the router.
- In auto mode: say "focus mode" / "chill mode" / "auto mode" to set the sticky session mode; "ask claude" / "use local" overrides one turn.

Job search, digest, search:
- `scripts/watch_boards.py` checks every board in `data/job_boards/watchlist.json`; `watch_boards.py add "<Company>" <board url> "<kw1,kw2>"` adds one and checks it immediately.
- `scripts/daily_digest.py [--dry-run|--no-notify|--open|--index|--date YYYY-MM-DD|--search TERM|--rebuild|--no-reindex]`, scheduled at 05:00 by `deploy/com.kyra.daily-digest.plist`. `--dry-run` is read-only.
- `scripts/search.py --reindex` once, then `scripts/search.py "question" [-k N] [--kind resume] [--private] [--answer] [--rerank llm|cross] [--mode lexical|vector|hybrid] [--explain] [--stats] [--eval]`. Search never reindexes on its own; the digest does at 05:00.
- Autofill needs `data/applicant_profile.json` (see `profile.py::ApplicantProfile`; `resume_path` must be a real file). It fills and stops; a visible window stays open for Duc to submit.

Training, measurement, maintenance:
- Router fine-tune: `scripts/router_ft.py gen | build | train | eval` (see `docs/router-finetune.md`); activate an adapter with `KYRA_CLASSIFIER_ADAPTER` in `.env`. Tool-calling distillation: `scripts/tool_ft.py gen | trace | build | train | eval` (`docs/tool-calling-distill.md`, measured and not shipped).
- `scripts/measure_voice_latency.py`, `scripts/measure_stt.py` (`docs/voice-latency.md`); `scripts/focus_report.py`; `scripts/analyze_patterns.py`; `scripts/summarize_book.py <pdf|epub>`; `scripts/worker.py` (the job worker when `KYRA_INLINE_WORKER=false`); `scripts/migrate_memory_embeddings.py` (already run; rerun only if the embedding function changes).

How the code is wired (details in `docs/log/platform-and-deploy.md`):
- **Configuration** is one typed object, `settings.py::get_settings()` (pydantic-settings over `.env`). Add a field; never read `os.environ` in new code.
- **All on-disk state** lives under `paths.py::DATA_DIR` (`KYRA_DATA_DIR` overrides). JSON stores write through `paths.write_json()` (atomic). Readers fail loud on a corrupt file; keep them loud.
- **HTTP errors** raise `errors.ApiError(status, code, message, details)`; never `return {"error": ...}` with 200. In `web/app.js`, read responses through `readJson(res)`.
- **Stores** are SQLAlchemy Core over `schema.py`; `DATABASE_URL` unset means one SQLite file per store under `data/`, set means one shared database. Schema changes go through Alembic (`alembic revision --autogenerate`, `alembic upgrade head`); the laptop's per-store files must be stamped and upgraded by hand (log entry 2026-09-07).
- **Long tasks** run on a DB-backed `JobQueue` (`jobs.py`), inline thread on the laptop, `scripts/worker.py` in the container; progress streams over SSE.
- **Logging** via `logging.getLogger(__name__)`, never `print`, in `src/`. Scripts do `sys.path.insert(0, ".../src")` instead of an editable install; follow that in new scripts.
- **Startup must stay cheap**: importing `webapp` or building the tool registry must not import chromadb, torch, mlx, sentence-transformers, faster_whisper or kokoro (`tests/test_startup_cost.py` pins it). Construct stores lazily.

## 4. Architecture pattern: preserve this

Every subsystem is a small ABC interface + a swappable concrete backend (Strategy pattern), specifically so that "how would you extend this" always has a clean answer. Existing examples:

- `MemoryStore` → `ChromaMemoryStore` (`src/companion/memory.py`); a separate, second memory layer, `MemoryNotesStore` → `MarkdownMemoryNotesStore` (`src/companion/memory_notes.py`). They are not the same thing: Chroma logs every exchange and retrieves top-k by similarity; notes are a small curated set loaded in full on every turn. Both are on the ABC; a new implementation must supply everything on it.
- `SpeechToText` → `FasterWhisperSTT`, `TextToSpeech` → `KokoroTTS` (`src/companion/voice.py`)
- `ListenMode` → `PushToTalkListener`, `VoiceActivityListener` (`src/companion/listening.py`)
- `LLMBackend` → `AnthropicLLM`, `LocalLLM` (`src/companion/llm.py`). `ConversationManager` depends only on `LLMBackend`; `build_llm(backend)` is the factory, `LazyBackends` constructs a backend the first time it is asked for.
- `Tool` → every tool in `reminders.py`, `learning.py`, `job_applications.py`, `job_autofill.py`, `memory_notes.py`, `news.py`, `science.py`, `handoff.py`, `outreach.py`, `search.py`, `focus.py`, `apply_pipeline.py` (`src/companion/tools.py`): `name`/`description`/`input_schema`/`run()` map 1:1 onto a Claude tool-use definition, so `ToolRegistry.schemas()` goes straight into `tools=`. `default_tools.py::default_tool_registry()` is the one place they are assembled; every front door calls it (they drifted once before it existed). RSS-backed tools share `feeds.py`. `AutofillEngine` → `LabeledFormEngine` (Greenhouse) / `AshbyAutofillEngine` / `LeverAutofillEngine`; `JobBoardSource` → Greenhouse / Lever / Ashby / Workday; `OutreachChannel` → `ClipboardChannel`; `SearchIndex` → `HybridSearchIndex`; `Reranker` → cross-encoder / LLM; `JobQueue` → `DbJobQueue`; `FocusPlanner` → `ScheduledPlanner`.
- `TurnRouter` (`src/companion/router.py`): layered decisions, cheapest and most certain first: explicit override phrase > sticky session mode (`session_state.py`, file-backed, shared across front doors) > a small local classifier (a LoRA adapter on Qwen2.5-1.5B when `KYRA_CLASSIFIER_ADAPTER` is set, else few-shot Llama-3.2-3B) that decides text-vs-tool and, for text, claude-vs-local. The tool path always goes to Claude (`AnthropicLLM.respond_with_tools()`); a local model was trained for it and measured short. `route_and_answer()` is what a front door calls per turn. Every decision is logged to `data/router.log` with a reason; the classifier only ever sees the current message, never history (known limitation).
- Long, "complex-looking" turns (`_looks_complex()`) are biased toward Claude rather than decomposed; real subtask decomposition was scoped out (`docs/agentic-roadmap.md`).

When adding a new subsystem, follow the same shape: a one-or-two-method ABC, a concrete class that does the real work, callers depend only on the interface.

## 5. Hard rules (working practices)

- **Hard constraints live in code.** A prompt is a request; a post-condition check is a guarantee. The guard on resume numbers, the 200-character outreach limit, the dash check, the no-file-in-focus-conditions rule, the startup-import test, the PII test: all are checks, and the pattern is to add one rather than a sentence in a prompt.
- **Verify with a real run before calling anything done**: a real API call, a real compile, a real browser, a real device. Keep the proof (log line, screenshot, output) and name it in the commit message. Then record it at the top of `docs/log/verification-history.md`.
- **Anything that leaves this machine for a human reader goes through the humanizer pass, no exceptions** (Duc's standing rule). "Outbound" means a resume, a cover letter, a form answer, an outreach note, an email, and the surrounding text handed over with it (file header, summary, pasteable snippet). Run the real `job_applications.CRITIQUE_PROMPT` through a real call. **Never an em-dash or en-dash in outbound text, and never a spaced hyphen standing in for one.** For short final text run the pass twice and diff. The pass is not a fact-checker and adds errors of its own: re-verify every number against sources and reject any duration, scale or outcome claim the source did not contain. Check mechanically before sending: no dash characters, no invented scale, no greeting on a form field, no `llm.TRUNCATION_MARKER`.
- **Never invent a number, a name, a date or a credential in anything resume-shaped.** Extra facts may correct a detail inside an existing entry; they never create a new job, project, certification or section. That is Duc's to add by hand.
- **Boundaries set by decision, not backlog** (do not extend without asking first): autofill fills and never submits; nothing automates LinkedIn; the Claude Code handoff drafts and never spawns; Workday is never autofilled; Chromium stays out of the container image.
- **"I am going to sleep" / "work as much as you can" means do not stop at the first thing that needs Duc.** Keep going until input is genuinely required, not merely convenient. When something really does need him (a key, an account, a purchase, a recording, a device), build everything around it on its own branch named for what it waits on, leave one obvious seam, and report: what is done, what is on a prep branch, and the shortest list of things only he can do.
- **Clean up test pollution, everywhere it lands.** Real-run verification writes to `data/*.db`, `data/memory_db`, `data/router.log`, `data/memory_notes/`. Snapshot `collection.get()["ids"]` before a run and delete the set difference after; never delete by the ids you think you wrote. Or run against a scratch `KYRA_DATA_DIR`. Classifier re-test batches log to the real `data/router.log` exactly like production; check it too.
- **To check that a merge lost nothing, enumerate what disappeared; never confirm what you expected to survive.** `diff <(git show <pre-merge tip>:<file>) <file>` for every file the merge touched, and for each removed line ask whether the other side ever had it. A spot check written by the author of a change tests the author's assumptions, not the change.
- **When a tool is added, measure the current router adapter on handwritten cases for it first; retrain only if it actually fails them.** Adding categories has a budget (round 5 fixed one case and broke two). Train two seeds and read both; a single seed cannot see a 4-point effect on 72 cases. Held-out sets are handwritten and committed before any data is generated.
- **Any identifier that reaches a filename or a form gets checked against what a human would call it** (a board slug is not a company name; a document id is not a resume filename an employer should see).
- Model files and databases under `data/` are gitignored on purpose; never commit them or loosen that. Job-search and interview-prep working docs live under `data/private_docs/`, never in tracked docs.
- `git` identity for this repo is set locally, not globally; leave it repo-local. Commit messages say what was verified, not only what changed. End a commit with the co-author line your harness prescribes.
- Simplicity first, surgical changes: the minimum code that solves the stated problem, matching existing style; mention unrelated dead code, do not delete it. Think before coding: state assumptions, surface tradeoffs, stop and ask when confused instead of guessing.

## 6. Session workflow

1. **Orient**: `git status`, `git log -5`, and the log file for the subsystem (section 7). Another agent may have worked since you last looked; two sessions building the same slice at once has happened, and it is the merge rule above that made it survivable.
2. **Plan**: restate the goal, list assumptions, ask up to five clarifying questions if anything is ambiguous, write `docs/plans/<date>-<topic>.md` with `[step] -> verify: [check]` lines. Skip the file for a trivial change; never skip the restatement.
3. **Build** on a branch named `session/<date>-<topic>`: one plan section per session; tests first for any hard constraint.
4. **Verify**: a real run, not just `pytest`.
5. **Record**: a dated entry at the top of the matching `docs/log/<topic>.md` with the *why*; a row in `docs/industry-standards.md` if a standard changed; a `data/private_docs/needs-your-input.md` entry for anything only Duc can decide; a rule in this file only if it is a standing rule that every future session needs (this file is the rulebook, the log is the history).
6. **Close**: `ruff check src scripts tests` and `python3 -m pytest` green, `git status --porcelain` clean of private files, commit. Do not push.

## 7. Read the log before you touch a subsystem

| Working on | Read first |
|---|---|
| `router.py`, `router_ft.py`, `local_tools.py`, `agent_ft.py`, `patterns.py` | `docs/log/router.md` |
| `llm.py`, `memory.py`, `memory_notes.py`, `conversation.py`, `persona.py` | `docs/log/memory-and-llm.md` |
| `voice.py`, `listening.py`, `voice_text.py`, `keybindings.py`, `scripts/voice_chat.py` | `docs/log/voice.md` |
| `webapp.py`, `web/` | `docs/log/web-ui.md`, then `platform-and-deploy.md` for auth |
| `job_applications.py`, `resume_latex.py`, `resume_guard.py`, `latex_compile.py`, `doc_text.py`, `github_profile.py` | `docs/log/resume.md` |
| `job_autofill.py`, `job_boards.py`, `job_posting_fetch.py`, `posting_signals.py`, `apply_pipeline.py`, `profile.py`, `job_documents.py` | `docs/log/autofill-and-boards.md` |
| `outreach.py` | `docs/log/outreach.md` |
| `digest.py`, `scripts/daily_digest.py`, `feeds.py`, `news.py`, `science.py` | `docs/log/digest.md`, `tools-and-feeds.md` |
| `search.py` | `docs/log/search.md` |
| `focus.py` | `docs/log/focus-and-health.md` |
| `apple/` | `docs/log/visionos.md` |
| `settings.py`, `errors.py`, `db.py`, `schema.py`, `jobs.py`, `webauth.py`, `paths.py`, `Dockerfile`, `deploy/`, `render.yaml`, `migrations/` | `docs/log/platform-and-deploy.md` |
| `README.md`, `LICENSE`, `.gitignore`, anything a stranger reads | `docs/log/publishing.md` |
| "Has this ever been verified for real?" | `docs/log/verification-history.md` |

`grep -rn "phrase" docs/log/` finds an entry in a second; `python3 scripts/search.py "question"` searches them semantically (the log is indexed).

## 8. Agent-specific notes

**Claude Code.** `CLAUDE.md` imports this file and adds the model/effort table and the skill list. `.claude/launch.json` starts the web UI for the browser preview tool. The session-end line and the global principles live in `~/.claude/CLAUDE.md`. Auto-memory lives outside the repo; durable project facts still go in `docs/log/`, so Codex sees them too.

**Codex.** This file is read automatically from the repo root; there is nothing else to configure in the repo. Work from `~/Projects/kyra` with the virtualenv at `.venv/`. The test suite needs no network; real-run verification does (Anthropic API, job boards, RSS), so approve network per run rather than turning the sandbox off. To apply one of the vendored skills, read `.claude/skills/<name>/SKILL.md` and follow it. Codex has no memory directory: anything worth remembering goes in `docs/log/` or `docs/plans/`. First-session prompt and setup notes: `docs/plans/2026-09-09-codex-onboarding.md`.

**Both.** Same branch convention, same rules, same log. Never edit `AGENTS.md` and `CLAUDE.md` into disagreement: `CLAUDE.md` holds only what is Claude-specific.
