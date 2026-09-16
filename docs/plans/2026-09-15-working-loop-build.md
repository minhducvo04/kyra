# Working loop, first slice: build plan

Date: 2026-09-15. Branch: `session/2026-09-15-working-loop`. Tests: Claude (Fable 5.1, high). Build: Codex. Review: Claude.
Design source: `docs/plans/2026-09-15-multi-model-working-loop.md`, section 10 "immediate objective". Process-boundary
facts (real CLI probes, flags, paths): Codex's private notes under `data/verifications/working-loop/`.

## Goal

One observable personal two-provider loop: a controller dispatches the installed Claude Code and Codex CLIs as
bounded text producers with every agent tool disabled, persists a content-free execution record per run with
input/output hashes and a private artifact, refuses anything outside a static allowlist, and shows each run on a
small loopback-only page. Reviews bind to the artifact hash. The existing chat handoff stays draft-only.

## Assumptions (state them, do not re-ask)

- Personal Mac only. One fixed owner (`PERSONAL_OWNER`) set server-side; the owner column exists so every query is
  scoped now and a second tenant later is a data change, not a code change. No multi-user security claim.
- Executable paths are `Settings` fields with the vendor-app defaults from the adapter notes. A browser request
  chooses only a choice key, a prompt, a topic and a project.
- Dispatch runs on the existing `jobs` queue (kind `loop_dispatch`, payload `{"run_id"}`), so no new queue and
  the request returns immediately. The job handler resolves the controller through the cached factory at run time.
- Statuses: `queued`, `dispatching`, `done`, `failed`, `unreconciled`, `mismatch`. "Unreconciled" means the
  process started and the outcome is unknown; nothing automatic ever moves it or retries it.
- A Claude run whose stream lacks a model string is `done` with `served_model=None`: company evidence (allowlist
  entry plus the configured CLI) and model evidence are recorded separately. A served model that differs from
  the requested one is `mismatch`, kept for inspection, never `done`.
- No tiers, memory, pricing, resume, or third provider in this slice.

## Interface

The exact contract is the docstring of `tests/test_working_loop.py` (module names, dataclass fields, store and
controller methods, HTTP routes and error codes). This file does not repeat it; the test is the source of truth.

Files Codex creates or touches:

| File | Change |
|---|---|
| `src/companion/working_loop.py` | New: allowlist, records, `DbLoopStore`, `LoopController`, stream parsers, `SubprocessRunner`, `command_for`. |
| `src/companion/schema.py` | New tables `loop_runs`, `loop_reviews` (owner column on both; JSON text for usage). |
| `migrations/versions/<rev>_working_loop.py` | Additive revision on head `d210a93e7b61`. `tests/test_integrated_migrations.py` compares metadata to the migrated head, so a schema change without a revision fails there. |
| `src/companion/settings.py` | `claude_cli_path`, `codex_cli_path`, `loop_timeout_seconds` (no `os.environ` reads). |
| `src/companion/webapp.py` | `_loop_controller()` cached factory, `HANDLERS["loop_dispatch"]`, `GET /loop`, the three `/api/loop/runs` routes, loopback + Origin dependency. |
| `web/loop.html` (+ small JS/CSS inline or in a new file) | The run view: list, detail with prompt/output, reviews with stale flag. Do not depend on unfinished console/reels work. |

## Steps, each with its check

1. [Add `working_loop.py` with allowlist, `command_for`, records, exceptions] -> verify: the four policy/command
   tests pass; `ruff` clean.
2. [Add tables + Alembic revision; `DbLoopStore` with private artifact files under `DATA_DIR/working_loop/<run_id>/`]
   -> verify: owner-scoping, review and topic-lookup tests pass; `tests/test_integrated_migrations.py` still passes;
   the sentinel test proves the DB bytes never contain prompt or output.
3. [`LoopController.request/dispatch` with both stream parsers] -> verify: all dispatch and failure-honesty tests
   pass, including "dispatching persisted before the process starts" and "unreconciled is never retried".
4. [`SubprocessRunner`: stdin prompt, process group, `timeout_seconds`, bounded stdout/stderr, wraps
   `TimeoutExpired`/kill into `DispatchInterrupted`, lets `FileNotFoundError` through] -> verify: one real
   dispatch per provider from `scripts/` or a REPL against a scratch `KYRA_DATA_DIR`, with the record and
   artifact saved as evidence under `data/verifications/working-loop/`; served model for Claude equals the
   requested model; Codex record shows `served_model=None`.
5. [HTTP routes, loopback + Origin gate, `web/loop.html`] -> verify: the six HTTP tests pass; a real browser
   (Claude's preview tool or Codex's steps) shows one run from queued to done with its output; `test_startup_cost`
   still passes (no heavy import at module top).
6. [Record] -> verify: entry at the top of `docs/log/platform-and-deploy.md` (why the loop lives on the jobs
   queue, why loopback-only) and `docs/log/verification-history.md` (the real dispatches, counts from pytest's
   summary line); hand-off block via `scripts/session_log.py --open-for review`.

## Baseline before this slice

`ruff check src scripts tests`: clean. `.venv/bin/python -m pytest`: 644 passed in 24.08s.
`tests/test_working_loop.py` alone: every case fails on `ModuleNotFoundError: companion.working_loop` (count in the
hand-off block).

## Out of scope, by decision

Automating repo edits or shell commands from the page; any tool enabled in either CLI; retrying an unreconciled
run; a third provider; tiers; cost ledger; memory; native session resume (item 3 of the roadmap) beyond the
read-only `find_session` lookup; changing `handoff_to_claude_code`.

## Critique (Codex) and answers (Claude)

Codex's critique (private file `data/verifications/working-loop/codex-test-critique.md`, 2026-09-15) and how the
tests changed:

- **Blocking: `add_review` accepted any completed other-provider run plus the author's hash.** Accepted. The
  contract now has `LoopController.request_review(subject_run_id)`: it picks the other provider's allowlist
  choice, puts the subject's prompt and output verbatim into the reviewer's stdin, and records
  `review_subject_id` and `review_subject_sha256` on the reviewer run at creation. `dispatch` rechecks the
  subject's on-disk hash before starting the process and fails with `subject_changed` if it moved. `add_review`
  requires that binding, both runs done, same owner, different developers, and the current on-disk hash. New
  tests: request_review in both directions with the artifact visible in stdin; an unrelated completed reviewer
  is refused; a subject edited before the reviewer runs fails with zero processes.
- **Verdicts are chosen by a human ("approve" / "reject" / "comment"), never derived from prose.** Accepted and
  written into the contract. No verdict-JSON framework. The page may offer a button that records `comment` and
  shows the reviewer's actual answer; no HTTP test for that in this slice.
- **Concurrent dispatch.** Accepted: two threads contend for one queued run; exactly one process starts, the
  loser raises `PolicyRefused`. The claim is a conditional UPDATE, the same shape `DbJobQueue.claim` uses.
- **A fabricated result string must not become done.** Accepted: a terminal event without assistant text, a
  result from another session id, a second Codex thread identity, or a foreign protocol are all `failed` with
  `malformed_stream` / `conflicting_metadata`. Only "expected protocol seen, no terminal event" stays
  `unreconciled`. Output text comes from assistant text blocks / `agent_message`, never `result.result`.
- **Missing served model stays allowed** (done with `served_model=None`), as agreed.
- **Static Python allowlist, no YAML.** Already the case.

Red after these changes: every case in `tests/test_working_loop.py` fails on `ModuleNotFoundError`; the count is in
the hand-off block.

## Review (Claude)

Reviewed: uncommitted build over `976b8d1`, 2026-09-15. 12 findings, 1 blocking. Full suite 701 passed, ruff clean;
12 review regressions added in `tests/test_working_loop_review.py`. The blocker: Duc's global Codex instructions
(`~/.codex/AGENTS.md`) reached the isolated Codex call in live run 1 (the answer ends with the session-end line the
prompt never asked for). Details and the fix path are in the private note
`data/verifications/working-loop/claude-build-review.md`.

### Re-check (Claude, same day)

B1 closed with a dedicated Codex state directory (auth referenced by symlink, no global instructions), proven by a
live tool-demand call that ran nothing and a repeated prompt with no footer, plus the native trace. Findings 1, 2
and 4 fixed red-then-green in `tests/test_working_loop_fixes.py`. Full suite 705 passed, ruff clean. **Approved for
this limited personal slice**; two non-blocking notes carried into the next slice (private note). Commit is
Codex's; push is Duc's.

## Next slice (Claude's recommendation, needs no Duc input)

Smallest useful slice: **human verdicts and reconciliation**. Two endpoints and two buttons on the existing page,
same store and HTTP patterns, about ten tests.

1. [`POST /api/loop/runs/{id}/reviews/{review_id}/verdict` with `approve` or `reject`, recorded by the owner,
   refused when the review is stale] -> verify: the verdict binds the same artifact hash; editing the artifact makes
   it stale; nothing in the reviewer's prose can set it.
2. [`POST /api/loop/runs/{id}/reconcile` with `nothing_happened` or `provider_processed` plus a short note, allowed
   only for `unreconciled` and stuck `dispatching` runs] -> verify: the record keeps its original status and gains
   a reconciliation entry; only a reconciled run can be re-requested, as a new run, never a re-dispatch; nothing
   is automatic.
3. [Cross-call isolation check for the managed Codex state] -> verify: a unique marker in call A is unknown to
   call B (the CLI writes `memories_*.sqlite` there; `features.memories=false` is passed but not yet proven).

Why this first: today an unreconciled run is a dead end, and the "reviews are comments, the human approves" loop
has no human step yet. After it, roadmap item 3: native session resume per topic through the private Codex state
directory and Claude `--resume`, which needs a Codex probe of the resume flags before tests are written.
