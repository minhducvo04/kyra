# Repair the learning-reels CI failure

PR #5 includes the planned red contract tests but no `companion.reels` module. The Python job exits
at collection; the separate working-loop PR is green. Build the library contract already specified
in `2026-09-13-learning-reels.md`, not a CI skip or a placeholder module.

## Scope

- Implement transcript parsing, metadata-only YouTube adapters, guarded moment proposals, and persistent
  attempts, mastery, XP and progress using existing Pydantic and SQLAlchemy dependencies.
- Add the four planned relational tables and an additive Alembic migration.
- Keep media embedding, rights checks, source evidence and duplicate-span checks intact.
- Leave the CLI, HUD, media generation and public release work for their own slices.

## Steps

1. Reproduce the missing-module failure -> verify: run the existing reels test module before edits.
2. Implement the existing library contract -> verify: reels, boundary and startup tests pass.
3. Correct only contradictory fixtures -> verify: required evidence is inside the marked span; independent
   mastery scenarios use different spans; duplicate rejection and evidence rejection assertions remain.
4. Add the schema migration -> verify: existing migration tests compare metadata and preserve old rows.
5. Exercise one real generated proposal on fictional scratch data -> verify: raw response, guard result,
   persisted moment and learning transitions recorded privately.
6. Independent Claude review and final checks -> verify: blocking findings addressed, lint and full Python
   suite pass; commit locally. Do not push or merge without Duc's separate instruction.

## Independent contract clarification

Claude confirmed that the original valid proposal's start at 32 seconds excludes both required-fact
quotes in the 15-to-32-second cue. Its start is now 15 seconds. Claude also confirmed that the mastery
and progress helpers reused a live source span despite a separate test forbidding duplicates. These
scenarios now vary only their end timestamp. No test was skipped, deleted or weakened.

The first incorrect delayed answer leaves the current due review open for a hinted retry. A correct
answer or the second incorrect answer closes it and schedules the next review, matching the existing tests.

## Verification and review

Completed the six steps above. Final Python suite: **717 passed, 1 existing optional tokenizer skip**;
lint clean. Claude approved with no blocking findings. Two optional parser findings were reproduced
and fixed with two focused regression cases. The real Fable proposal and scratch database round trip
passed after prompt clarifications; malformed responses stayed rejected. Details and limitations are
recorded at the top of `docs/log/verification-history.md`.
