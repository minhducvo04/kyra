# Working loop, slice 2: owner decisions and reconciliation declarations

Date: 2026-09-15. Branch: `session/2026-09-15-working-loop`, on top of `3887066`. Tests: Claude (Fable 5.1, high).
Build: Codex. Review: Claude. Slice 1: `2026-09-15-working-loop-build.md`.

## Goal

Two owner-recorded facts on top of the slice 1 receipts, shown on the same page:
- a **decision** (approve or reject) on one completed model review, appended beside the model's comment and bound
  to the subject artifact hash and the reviewer's output hash at the moment of the decision;
- a **reconciliation** (nothing happened, or the provider processed it) plus a short private note on a run whose
  outcome the controller could not confirm.

Neither executes anything, retries anything, or changes a run's status. A reconciliation is the owner's
declaration, not proof of provider effects; the page says so, and says a new request may spend allowance again.

## Assumptions (state them, do not re-ask)

- Decisions and reconciliations are append-only rows in two new tables; the review row keeps its `comment` verdict.
  A later decision is a new row, and every decision is shown with a stale flag.
- Eligible for reconciliation: `unreconciled`, and `dispatching` older than `timeout_seconds + 30 s`. Everything
  else (queued, recent dispatching, done, failed, mismatch, other owner) is refused. The run row is not touched.
- Note text lives in the run's private artifact directory; the row holds its hash and path only.
- Owner is the fixed server-side owner; bodies forbid extra fields; both routes sit under the existing loop
  boundary (loopback peer, Host, Origin). No worker or endpoint reads model output into a decision or an outcome.
- Carried over from the slice 1 review: a `ProcessNotStarted` exception for pre-start failures in the runner
  (customized Codex state) maps to `failed` with its code, never `unreconciled`.
- Out of scope: explicit retry, native session resume, dedup of new requests, provider or session locks, any
  change to the job queue.

## Interface

The exact contract (names, fields, refusal rules, routes, error codes) is the docstring of
`tests/test_working_loop_decisions.py`. The test is the source of truth; this file does not repeat it.

Files Codex creates or touches:

| File | Change |
|---|---|
| `src/companion/working_loop.py` | `Decision`, `Reconciliation`, constants, store methods `decide_review`, `add_reconciliation`, `reconciliations_for`; `reviews_for` gains `decisions`; controller `decide_review`, `reconcile`; `ProcessNotStarted` -> failed in `dispatch`. |
| `src/companion/working_loop_process.py` | `ProcessNotStarted`; `_codex_state_directory` raises it for a customized directory. |
| `src/companion/schema.py` + `migrations/versions/<rev>_loop_decisions.py` | Tables `loop_review_decisions`, `loop_reconciliations`; additive on head `e915b07c2d31`. |
| `src/companion/webapp.py` | `POST /api/loop/reviews/{id}/decision`, `POST /api/loop/runs/{id}/reconcile`, detail gains `reconciliations`. |
| `web/loop.js`, `web/loop.html` | Approve / Reject on a review card; a reconcile form on unreconciled and stale-dispatching cards with the two outcomes and a note; both show the recorded rows. Copy: "Your declaration. Not proof of what the provider did. A new request may use allowance again." |

## Steps, each with its check

1. [Exception and dispatch mapping] -> verify: the two `ProcessNotStarted` tests pass; slice 1 suites unchanged.
2. [Tables + migration + store methods] -> verify: decision and reconciliation store tests pass;
   `tests/test_integrated_migrations.py` passes; the sentinel test proves the note never enters `loop.db`.
3. [Controller eligibility rules] -> verify: stale-dispatching timing test and the refusal tests pass.
4. [Routes + page] -> verify: the three HTTP tests pass; a real browser records one approve and one reconciliation
   on scratch data and the cards show them; `test_startup_cost` still passes.
5. [Record] -> verify: entry at the top of `docs/log/platform-and-deploy.md`; `docs/working-loop.md` gains a short
   "Decisions and reconciliation" paragraph; `docs/log/verification-history.md` names the browser evidence.

## Red before the build

`tests/test_working_loop_decisions.py`: every case fails on a missing name (`AttributeError` on the module or a
`404` from a missing route); the count is in the hand-off block. Slice 1 suites stay green.

## Critique (Codex)

Accepted by Codex: separate owner decisions preserve model attribution; both hashes govern staleness; owner declarations leave execution receipts intact and trigger no call. HTTP ownership is the local personal account, not cryptographic proof that a human clicked. No external release action consumes these decisions. Independent red run: 20 failures on the missing feature.

## Build verification (Codex)

Implemented the accepted contract. New acceptance cases: 20 passed. Combined loop, startup and migration checks:
95 passed in 5.32s. Full suite: 727 passed in 29.62s; lint clean. A real scratch browser recorded an approve decision
and an outcome note, preserving the model comment and original run status with no increase in run count. Evidence:
`data/verifications/working-loop/decisions-browser-proof.json`. Independent Claude review is the next gate.

## Review (Claude)

Reviewed: uncommitted delta over `3887066`, 2026-09-15. 95 focused and 725 full tests passed, ruff clean; browser
proof and the cross-call isolation check read. **Approved once one small regression is green**: a deleted
reconciliation note file must list as missing (`note: None, note_changed: True`) and the run detail must keep
serving the receipt; today it raises. Test: `tests/test_working_loop_decisions_review.py`. Three non-blocking
notes and the recommended minimum contract for native topic continuation are in the private note
`data/verifications/working-loop/claude-decisions-review.md`.

Claude independently approved subject to fixing a missing-note regression. Both review tests failed before the fix, then passed; the complete 727-test suite passed. The live HTTP check also served the unchanged receipt while its scratch note was temporarily moved, then restored. Evidence: `data/verifications/working-loop/missing-note-live-proof.json`. The approval condition is met.
