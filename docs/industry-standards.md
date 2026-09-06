# Industry standards applied to Kyra — what changed, what it was before, and why the standard exists

Written 2026-09-04 during a whole-system audit. Each row is a concrete change in this repo; the "why" is the reason
the industry converged on it, which is the part worth being able to explain.

Legend: ✅ done tonight · 🟡 partially done / next step noted · ⏭ deliberately not done (with reason)

---

## 1. Correctness of the core feature: one-page resume fit

| | Before | After | Why it's the standard |
|---|---|---|---|
| ✅ **Feedback signal** | The fit loop only knew the *page count*. "2 pages" could mean 3 lines over or half a page over; the shrink prompt said "drop the single least-relevant bullet," so a big overflow never converged in 4 attempts and a 2-page PDF came back. | `latex_compile.measure_pages()` reads text lines per page from the real PDF. The loop now feeds the model *measured overflow* ("page 1 holds ~51 lines; 9 spilled") and asks for a proportional cut with an escalating margin per attempt. | **Control loops need a proportional error signal, not a binary one.** A thermostat that only knows "too hot/not too hot" oscillates; one that knows *by how much* converges. Same principle as PID control, gradient descent, and TCP congestion control. |
| ✅ **Baseline measurement** | The original resume was never compiled. A resume that already fit on one page could come back *longer* because the model had no length budget. | The original is compiled first; its page count and line capacity go into the first prompt as an explicit budget ("already fits, ~51 lines — net length must not grow"). | **Measure before you change.** Every performance/regression discipline starts from a baseline; you can't tell "made it worse" from "was always like this" without one. |
| ✅ **Best-candidate selection** | "Best" = fewest pages only. Two 2-page attempts tied; the first was kept even if the later one was much closer. | Lexicographic (pages, overflow lines). | Tie-breaking on a finer metric is basic optimization hygiene — otherwise the loop discards progress. |
| ✅ **Hard-constraint visibility** | A non-fitting result was one line in a soft "warnings" list. The user reasonably read the PDF link as "done." | `fit` / `page_count` / `overflow_lines` are first-class response fields; the UI renders a red "✗ Not one page: compiled to 2 pages — 9 lines past page 1" banner, green "✓ Fits one page" otherwise, plus a collapsible attempt-by-attempt log. | **Never let a violated hard requirement look like success.** In safety-critical UI this is called "fail loud"; in APIs it's the reason 4xx/5xx exist instead of `200 {"error": ...}`. |
| ✅ **Robust output parsing** | Only a ```` ```json ```` fence was stripped; ```` ```latex ```` was compiled as-is and failed. | Any language tag is stripped. | Parse defensively at every model boundary — LLM output is untrusted input, same as user input. |

## 2. Guardrails: structural, not prompt-only

| | Before | After | Why |
|---|---|---|---|
| ✅ **Fabrication check** | The only defense against invented facts was the prompt ("never invent"). A true-but-unauthorized memory note *did* become a fabricated resume entry once (2026-09-04). | `resume_guard.py`: after generation, every number, year, percentage, and link in the output is checked against the sources the model was given. New ones are surfaced as an explicit "fact check: verify 45, 12" warning. Deterministic, zero cost, tested. | **Defense in depth.** A prompt is a *request*; a post-condition check is a *guarantee*. Production LLM systems (and every safety review) require an out-of-model validator for anything that reaches a real-world consequence — here, a document sent to an employer. |
| 🟡 Next | — | Extend the guard to proper nouns (company/school names) via diff against the original's `\resumeSubheading` arguments. Numbers were prioritized because they're the highest-consequence class (a wrong metric/year is a reference-check failure). | |

## 3. Testing

| | Before | After | Why |
|---|---|---|---|
| ✅ **Automated test suite** | None. `tests/__init__.py` was empty. Every verification was a one-off script, re-run by hand, results living only in chat history and CLAUDE.md prose. | `pytest`, 56 tests, ~5 s. Stores (SQLite/JSON/Markdown), resume format parser, LaTeX compile + error extraction, the fit loop (with real `pdflatex`), the fabrication guard, the router's decision layers, and the HTTP API end-to-end via `TestClient`. | **Regression protection is the only thing that lets you change code with confidence.** A verification that isn't automated is re-done never. Every bug in CLAUDE.md's history ("caught by actually running it") now has a test that would catch its recurrence. |
| ✅ **Hermetic tests** | Test scripts wrote to the real `data/` — 193 lines of noise ended up in the real `router.log`, real DBs needed manual cleanup after every session. | `tests/conftest.py` sets `KYRA_DATA_DIR` to a temp dir before any import; every store, log, and state file lands there and is deleted after the run. | **Tests must not share state with production or with each other.** Otherwise they're flaky, order-dependent, and dangerous — the "clean up test pollution" discipline in CLAUDE.md is a symptom of tests that weren't hermetic. |
| ✅ **Test doubles at the boundary** | Every check of LLM-dependent code cost a real API call, so it was done rarely. | `tests/fakes.py::ScriptedLLM` — canned outputs, records prompts. Tests assert on *what the code told the model* (e.g. "the shrink prompt contained the measured overflow"). | Mock the expensive/non-deterministic dependency at its interface — this is exactly what the project's `LLMBackend` ABC is for. |
| ✅ **Real-dependency tests, skipped when unavailable** | — | LaTeX-backed tests are marked `requires_latex` and skip cleanly without `pdflatex` (CI). | Test against the real thing when you can, degrade gracefully when you can't — never a silent pass. |
| ⏭ Coverage % target | — | Not measured yet. | Coverage numbers are a proxy; the bar tonight was "every documented bug class has a test." Add `pytest-cov` when the suite stabilizes. |

## 4. Configuration and environment

| | Before | After | Why |
|---|---|---|---|
| ✅ **Typed settings (2026-09-06, v2 slice 1)** | Nine call sites read `os.environ` with their own defaults; the API key was an import-time global. | `companion/settings.py`: one pydantic-settings `Settings` (`.env`-aware, cached), every knob typed with a default; `config.require_api_key()` kept as a shim. Container and laptop are configured identically. | 12-factor config: one declared schema for configuration, validated once, overridable by environment. |
| ✅ **Containerized web layer (2026-09-06, v2 slice 1)** | No image; "works on my Mac". | `Dockerfile` (python:3.12-slim + TeX Live, web-only `requirements-web.txt` - no torch/mlx/voice), `docker-compose.yml` with Postgres for slice 2, `.dockerignore`; CI builds the image and smoke-tests `/api/backend`. Docker isn't installed on the dev Mac, so CI is the build. | An image is the deployable unit; building it in CI is what proves it builds. |
| ✅ **Single source for paths** | 11 modules each computed `Path(__file__).resolve().parent.parent.parent / "data" / ...`; `memory.py` used a *cwd-relative* `"data/memory_db"` — launching from any other directory silently created an empty memory. | `companion/paths.py`: `DATA_DIR` (env-overridable via `KYRA_DATA_DIR`), `PROJECT_ROOT`, `WEB_DIR`. Every module derives from it. | **12-factor config**: environment-specific locations come from the environment. One knob relocates all state (tests, a second profile, a container volume). |
| ✅ **Dependency declaration** | `pyproject.toml` listed 9 deps; `requirements.txt` listed 18. Two sources of truth, already drifted. | `pyproject.toml` mirrors `requirements.txt` exactly; `[project.optional-dependencies] dev` + `requirements-dev.txt` for test/lint tools. `requires-python` raised to the 3.11 the code already assumed. | Drifted manifests are how "works on my machine" happens. Runtime vs. dev deps are separated so a deploy doesn't ship pytest. |
| ⏭ Lock file | — | Not added. | Would need `pip-tools`/`uv`; worth it at v2 when there's a deploy target. Noted in `v2-outline.md`. |

## 5. Observability

| | Before | After | Why |
|---|---|---|---|
| ✅ **Structured logging** | `print()` only (4 sites), no timestamps/levels/module names; the fit loop's attempts were invisible except in the HTTP response. | `logging` with a `configure_logging()` entry point (`KYRA_LOG_LEVEL` env), module loggers in `latex_compile`, `job_applications`, `webapp`. Every compile logs engine/pages/lines; every fit attempt logs its result. | You can't debug what you can't see. Levels + module names let you turn one subsystem up to DEBUG without noise from the rest, and redirect to a file/aggregator without touching call sites. |
| 🟡 Next | — | Request IDs per HTTP call; timing metrics per LLM call (tokens in/out, latency) — see v2. | |

## 6. HTTP API hygiene

| | Before | After | Why |
|---|---|---|---|
| ✅ **Status codes on the file endpoint** | `/api/job/resume-pdf/{name}` returned `200 {"error": "invalid filename"}`. A browser opening the link saw JSON with a success code. | `HTTPException(400)` for a bad name (now also rejects `..`), `404` when missing. | Status codes are the contract clients, proxies, and monitors rely on. A 200 error is invisible to every one of them. |
| ✅ **Error envelope (2026-09-06, v2 slice 1)** | 7 endpoints returned `{"error": ...}` with HTTP 200; the JS checked `data.error` on success responses. | `companion/errors.py`: `ApiError(status, code, message, details)` + handlers render one shape, `{"error": {"code", "message", "details"}}`, with real 400/404/409/502; `HTTPException` renders the same envelope. `app.js` has one `readJson()` that throws the server's message. | Status codes are the contract clients, proxies, and monitors key on; one envelope means one error path in every client. |
| ✅ **Upload bound** | `upload.file.read()` — unbounded; a mis-dropped 2 GB file would be read into memory and handed to a parser. | `MAX_UPLOAD_BYTES` (10 MB), read `n+1` bytes and reject over-size with a clear message. | Bound every input. Unbounded reads are a denial-of-service vector and an OOM waiting to happen. |
| ✅ **Path traversal** | Already guarded (`/`, `\`, suffix). | Also rejects `..` explicitly. | Belt and suspenders on anything that touches the filesystem from URL input. |

## 7. Startup and testability of the web layer

| | Before | After | Why |
|---|---|---|---|
| ✅ **Lazy heavy dependencies** | Importing `webapp` loaded BGE embeddings, faster-whisper, and Kokoro *before serving a request* — slow startup, and impossible to test the HTTP layer without three ML models. | `_Runtime` with `cached_property` for memory/conversation/STT/TTS: built on first use. Import went from model-loading to 0.7 s. | Construct expensive things lazily and inject dependencies; it's the same reason `LazyBackends` already existed for the local LLM. Testable code and fast startup are the same property. |
| ⏭ Full app factory (`create_app(deps)`) | — | Not done — module-level globals + monkeypatch is sufficient for tests today. | Proper DI is a v2 item (needed the moment there's a second process or a worker). |

## 8. Code quality automation

| | Before | After | Why |
|---|---|---|---|
| ✅ **Linter** | None configured. | `ruff` (`E,F,I,B,UP`) in `pyproject.toml`; the run found one real bug class (`zip()` without `strict`, which silently truncates on length mismatch) and a missing `raise ... from`. All clean. | A linter is the cheapest reviewer you'll ever have; the `B` (bugbear) rules catch actual bugs, not style. |
| ✅ **CI** | None. | `.github/workflows/ci.yml`: lint + tests on every push/PR, LaTeX tests skip in CI. | The suite only protects you if it runs without anyone remembering to run it. (No remote yet — see `needs-your-input.md`.) |
| ⏭ Type checking (`mypy`/`pyright`) | — | Not added tonight. The codebase is annotated; a strict pass is a couple of hours of cleanup. | v2. |

## 9. Documentation and decision records

| | Before | After | Why |
|---|---|---|---|
| ✅ | Decisions lived in CLAUDE.md prose (good) but the *standards* were implicit. | This file, plus `v2-outline.md` and `needs-your-input.md`. | Architecture Decision Records: write down *why*, so the next person (or you in three months) doesn't undo a deliberate choice. CLAUDE.md already did this well; this file makes the standards themselves explicit. |

---

## Verification of tonight's work (what was actually run)

- `pytest`: 56 passed, ~5 s, including real `pdflatex` compiles.
- `ruff check src scripts tests`: clean.
- Real end-to-end: Duc's actual LaTeX resume + a realistic SDE posting through the rewritten Fast loop with a real Claude call — result recorded in the private prep notes and CLAUDE.md.
- Test PDFs generated during verification were deleted; the one pre-existing `data/generated_resumes/078cb7e0bc4a.pdf` (the 2-page output from the old `full_resume` HTML path, 2026-09-04 00:36) is the artifact that prompted this audit and was left in place as evidence.
