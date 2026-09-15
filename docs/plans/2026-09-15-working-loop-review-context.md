# Working loop, slice 4: bounded prior-turn context for reviews

Date: 2026-09-15. Branch: `session/2026-09-15-working-loop`, on top of `787ef85`. Tests: Claude (Fable 5.1, high).
Build: Codex. Review: Claude. Earlier slices: `2026-09-15-working-loop-build.md`, `-decisions.md`, `-continuation.md`.

## Goal

A review of a continued answer shows the reviewer that answer's earlier turns, read from the loop's own hashed
artifacts, bounded in depth and bytes, with any omission stated in the prompt and recorded on the receipt. The
review is bound to exactly what the reviewer read, so a changed earlier turn invalidates the evidence the same way
a changed subject does. The reviewer still runs fresh, on the other provider, and the quoted turns are data.

## Assumptions (state them, do not re-ask)

- Context is the linear ancestor chain (one child per parent, so a list). At most `REVIEW_CONTEXT_MAX_TURNS = 8`
  ancestors are read, newest first; reaching the bound with a parent still above it sets `omitted` without reading
  that parent. The walk cannot exceed the bound, so a cycle cannot loop; a cycle or a missing or foreign parent is
  refused outright.
- The byte bound is the existing `MAX_PROMPT_BYTES`. Whole turns are dropped oldest first; a turn is never cut
  inside. If nothing fits, the review request is refused before any call.
- Every visited ancestor must hash to its receipt; missing, edited or foreign-owner artifacts fail closed with no
  run and no process.
- The reviewer record carries `review_context` (one nullable JSON column): included run ids with their input and
  output hashes, plus the omission flag. Codex's critique is taken: run ids alone cannot detect change, so hashes
  are stored. Before dispatch, when attaching, when deciding, and in the stale flag, every included turn is
  rechecked.
- Legacy reviews (before this slice) read back with `review_context` None and keep their slice 2 rules.
- No new route. The interim "earlier turns were not shown" sentence is removed; omission is stated only when true.
- The "replacement link" (a fresh request naming the reconciled run it supersedes) is deferred: it needs an API
  field and an eligibility rule, so it is not behaviour-free and does not belong in this scope.

## Interface

The exact contract is the docstring of `tests/test_working_loop_review_context.py`. The test is the source of
truth; this file does not repeat it.

Files Codex creates or touches:

| File | Change |
|---|---|
| `src/companion/working_loop.py` | `REVIEW_CONTEXT_MAX_TURNS`; `review_context` on the record and in `create_run`; the bounded walk and prompt builder in `request_review`; `context_changed` in `dispatch`; context in `add_review`, `decide_review`, `reviews_for`. |
| `src/companion/schema.py` + `migrations/versions/<rev>_loop_review_context.py` | Nullable JSON text column `review_context` on `loop_runs`; additive on head `a916c29e4f53`, idempotent for startup-created tables. |
| `web/loop.js` | On a review card: "Reviewer saw N earlier turns" and "more were omitted" when flagged. |
| `docs/working-loop.md` | Short paragraph under reviews: what the reviewer sees, the two bounds, the fail-closed rule. |

## Steps, each with its check

1. [Column, migration, record field] -> verify: the migration test and `tests/test_integrated_migrations.py` pass;
   legacy review rows read back with `review_context` None.
2. [Bounded walk and prompt builder in `request_review`] -> verify: the content, depth, byte and fail-closed tests
   pass; the depth test proves nothing beyond the bound is read (its artifacts are corrupted on purpose).
3. [Context checks at dispatch, attach, decide and stale] -> verify: `context_changed` and the stale test pass;
   slice 2 and 3 suites unchanged.
4. [Detail JSON and page] -> verify: the HTTP test passes; a real browser on scratch data shows a review of a
   continued answer with "saw 1 earlier turn" and, after editing the ancestor artifact file, the stale label.
5. [Record] -> verify: `docs/working-loop.md` paragraph; entries at the top of `docs/log/platform-and-deploy.md`
   and `docs/log/verification-history.md` naming the browser evidence.

## Red before the build

`tests/test_working_loop_review_context.py`: every case fails on the missing field, the missing constant, the old
prompt shape or the missing column; the count is in the hand-off block. Earlier suites stay green.

## Critique (Codex)

Accepted; independent red verification found 13 failures and one already-passing oversized-subject guard. Fixed eight-turn traversal, serialized-byte limit, complete scoped ancestors and both input/output hashes are required. Context-free legacy records remain readable. Replacement links stay separate.

## Build verification (Codex)

All 14 acceptance cases pass; focused loop/migration/startup suite 133 passed in 6.13s; full suite 763 passed in 25.71s, lint clean. Real browser review15 read parent10 while reviewing continued answer11, used a fresh Codex session, and matched the synthetic marker. An included prompt edit made both review and owner decision stale and a new decision returned 409; the file was restored. Evidence: `data/verifications/working-loop/review-context-live-proof.json`. The migration preserved all fourteen earlier rows. Independent Claude review is pending.

## Review (Claude)

Reviewed: uncommitted delta over `787ef85`, 2026-09-15. 133 focused and 763 full tests passed, ruff clean; live proof
(Codex reviewer matched the marker against the earlier turn; stale on ancestor edit; 409 on a new decision; current
after restore) and the 14-row migration proof read. **Approved once three regressions are green**
(`tests/test_working_loop_review_context_review.py`): the subject's own request must be bound after a review is
requested (today only its output is), and a lineage cycle closing exactly at the depth bound must be refused rather
than labelled omitted. Both were raised by Codex in the review request and confirmed in the diff. Details in
`data/verifications/working-loop/claude-review-context-review.md`.

## Independent review resolved

Claude approved conditionally on three regressions, all first observed red. The subject's own request hash is now
checked before dispatch, attaching reviews and owner decisions and in stale status. A repeated next id at the exact
depth limit is refused without reading another ancestor. All three regressions pass. Final full suite: 766 passed
in 27.45s; lint clean. Live subject-request edit and restore verified stale review/decision plus 409 refusal in
`data/verifications/working-loop/subject-request-live-proof.json`. The approval conditions are fulfilled.
