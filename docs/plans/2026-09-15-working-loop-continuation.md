# Working loop, slice 3: explicit native topic continuation

Date: 2026-09-15. Branch: `session/2026-09-15-working-loop`, on top of the slice 2 commit. Tests: Claude (Fable
5.1, high). Build: Codex. Review: Claude. Earlier slices: `2026-09-15-working-loop-build.md`,
`2026-09-15-working-loop-decisions.md`. Evidence that both CLIs resume a native session and recall its context:
Codex's private `codex-resume-proof.json` and `claude-resume-proof.json`.

## Goal

The owner can continue one specific completed answer in its native provider session, with the same model, effort
and isolation as the first call, and see the link between the two answers. The provider must come back on the
requested session or the continuation is a mismatch. Reviews stay fresh. Nothing continues by itself.

## Assumptions (state them, do not re-ask)

- The browser sends a parent **run id** and a prompt. Native session ids never enter through the API; a body that
  carries one is a 422. The controller reads the session from the parent's receipt.
- **One child per parent, reserved at creation by the database** (`continued_from_run_id` UNIQUE, NULLs excluded),
  never released. A failed, mismatched or unreconciled child still blocks its parent; the owner starts fresh. This
  is Codex's concurrency requirement from the slice 2 review and it replaces any lock or retry framework.
- Parent eligibility: this owner's; `done`; not a review run; a non-empty native session id in its receipt; current
  `POLICY_VERSION`; prompt and output files still hash to the receipt; the newest done run for its owner, project,
  topic and provider. The child inherits the parent's choice, project and topic.
- `POLICY_VERSION` advances, so runs recorded before the Codex isolation fix cannot be parents. They stay readable.
- Before dispatching a child the controller revalidates the parent (done, hashes, same session id, current policy);
  a changed parent fails the child with `parent_changed` and no process starts.
- After the run, the stream's session id must equal the requested one; otherwise `mismatch` / `session_mismatch`,
  output kept for inspection. The existing malformed-stream rule already covers a missing session id.
- `request_review` always creates a fresh run, even for a continued answer, and a review run cannot be a parent.
- Out of scope: automatic continuation, continuing across topics or providers, resuming unreconciled or failed
  runs, importing native chats not created by the loop, model-generated topic matching, dedup of new requests.

## Interface

The exact contract is the docstring of `tests/test_working_loop_continuation.py`. The test is the source of
truth; this file does not repeat it.

Files Codex creates or touches:

| File | Change |
|---|---|
| `src/companion/working_loop.py` | `POLICY_VERSION` bump; two record fields; `continue_run`, `can_continue`; parent revalidation and session verification in `dispatch`; `request_review` stays fresh. |
| `src/companion/working_loop_process.py` | `command_for(choice, *, resume_session_id=None)`: Claude `--resume <id>`, Codex `exec resume <id>`, every existing flag kept. |
| `src/companion/schema.py` + `migrations/versions/<rev>_loop_continuation.py` | Columns `continued_from_run_id` (UNIQUE) and `requested_session_id`; additive on head `f915b18d3e42`. |
| `src/companion/webapp.py` | `POST /api/loop/runs/{id}/continue`; detail gains `can_continue`. |
| `web/loop.js`, `web/loop.html` | "Continue this conversation" on an eligible completed answer, naming the parent on the child card; a way back to a fresh request. |

## Steps, each with its check

1. [`command_for` with `resume_session_id`] -> verify: the argv test passes for both providers; every fresh flag is
   still present in the resumed command.
2. [Columns + migration + record fields] -> verify: `tests/test_integrated_migrations.py` passes; the UNIQUE column
   makes the four-thread duplicate test produce exactly one child.
3. [`continue_run`, `can_continue`, the parent revalidation and session verification in `dispatch`] -> verify: the
   refusal matrix, newest-only, parent-changed, session-match and mismatch tests pass; slice 1 and 2 suites unchanged.
4. [Route + page] -> verify: the two HTTP tests pass; in a real browser on scratch data, for each provider: A fresh
   with a marker, B continued from A recalls the marker on the same native session id, C fresh does not know it;
   a second Continue on A is refused visibly.
5. [Record] -> verify: `docs/working-loop.md` gains a "Continuing an answer" paragraph that says what the link proves
   (same native session, verified by the stream) and what it does not (no dedup, no automatic continuation);
   entries at the top of `docs/log/platform-and-deploy.md` and `docs/log/verification-history.md`.

## Smaller alternative considered

Continuing only within the same HTTP request chain (no persisted link, no reservation) would be less code but
would leave the second answer's receipt unable to say which session it extended, and two clicks could resume one
session at once. The persisted link plus the UNIQUE column is the smallest shape that keeps both properties.

## Red before the build

`tests/test_working_loop_continuation.py`: every case fails on a missing name, a missing column or a missing route;
the count is in the hand-off block. Slice 1 and 2 suites stay green.

## Critique (Codex)

Accepted after one test-fixture correction: the refusal matrix currently queues a fresh run without consuming its first scripted result, so its later subject receives DispatchInterrupted and fails before testing continuation. Claude will correct the fixture order. Independent red run confirmed 13 failures.

Implementation will compare the newest done **run id**, not only `find_session`'s session string (two runs may have the same native session). Parent choice/provider/model/effort must match the current allowlist, its native identifier must be a valid UUID, and child dispatch must match the parent's exact recorded scope. The database reserves a parent once; policy/version and integrity checks run again before dispatch. Migration handles both existing older tables and metadata-created fresh tables without dropping old records.

Claude's answers:
- Fixture order: corrected. The refusal matrix now scripts `FileNotFoundError`, `DispatchInterrupted`, the codex
  subject, the Claude review and the Claude parent in consumption order, and asserts the five statuses before
  testing refusals. Red re-run: 13 failed across 11 functions; slices 1 and 2 still 83 passed.
- Newest by run id, not by session string: agreed; the contract's "find_session's answer" was the scope, the
  comparison is the newest done run's id. Two runs sharing one native session is exactly the chain case.
- Valid UUID for the native identifier: agreed; every fixture id in the test file is a valid UUID.
- Exact recorded scope at child dispatch and the second policy and integrity pass: agreed, already in the
  `parent_changed` test.
- Migration must tolerate tables created by startup before the upgrade: agreed; `tests/test_integrated_migrations.py`
  covers both orders.
- Fixture order, second note (`continuation-test-feedback.md`): that note described the file before the rewrite
  above. Verified against the in-progress build rather than by reading: the matrix passes with the five
  statuses asserted before any refusal. Two regressions added on request: a native id that is not a UUID
  (plain text, empty, shell metacharacters, a UUID with a flag appended) can never reach argv or become a parent;
  parent metadata changed after the child was created (session id, effort, model, status) fails the child with
  `parent_changed` and no process. File now 13 functions, 21 cases.

## Build verification (Codex)

Current implementation: 118 focused loop/migration/startup tests passed in 6.91s; full suite 749 passed in 28.89s.
Both providers passed A/B/C verification: fresh A saved a synthetic marker; explicit B returned the same native
session id and recalled the marker; fresh C returned a different session id and UNKNOWN. The A/B calls were submitted
in the real browser. A duplicate continuation returned 409. Evidence: `data/verifications/working-loop/continuation-live-proof.json`.
The scratch schema was compared to 0358938 before migration; a private backup was retained and all eight prior run
rows survived unchanged. Evidence: `continuation-migration-proof.json`. Independent Claude review requested.

## Review (Claude)

Reviewed: uncommitted delta over `0358938`, 2026-09-15. 118 focused and 748 full tests passed, ruff clean; live
A/B/C proof for both providers and the migration proof read. **Approved once one small regression is green**: the
dispatch-time revalidation must not re-apply the "newest run in scope" rule, or a fresh run finishing in the same
topic kills a reserved follow-up and burns the parent's slot. Test: `tests/test_working_loop_continuation_review.py`.
The review-context answer (bounded linear ancestor chain, bound by run ids and hashes), three non-blocking notes,
and the Duc-versus-local next steps are in `data/verifications/working-loop/claude-continuation-review.md`.

Claude independently approved subject to a reserved-child regression. Its test failed before the fix and passed
after moving newest-run eligibility to reservation only; immutable parent scope, hashes, policy and identity still
revalidate at dispatch. Final full suite: 749 passed in 28.89s; lint clean. The approval condition is fulfilled.
Until bounded prior-turn context is added, a continued-answer review explicitly says earlier turns were not shown.
