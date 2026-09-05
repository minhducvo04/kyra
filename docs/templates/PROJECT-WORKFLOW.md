# Project workflow for Claude sessions (portable — copy into any repo's CLAUDE.md)

> DRAFT 2026-09-05. Generic on purpose; project specifics belong in the repo's own CLAUDE.md sections.

## 1. Session loop
1. **Plan** — restate the goal, list assumptions, ask up to 5 clarifying questions if anything is ambiguous.
   Write the plan to `docs/plans/<date>-<topic>.md` with verifiable steps: `[step] → verify: [check]`.
2. **Build** — one plan section per session. For any hard constraint, write the test first.
3. **Verify** — a real run (real API call / real compile / real browser), not just unit tests. Keep the proof
   (log lines, screenshot, output file) and mention it in the commit message.
4. **Record** — a "key decision" bullet in CLAUDE.md (the *why*), a row in `docs/industry-standards.md` if a
   standard changed, an entry in `data/private_docs/needs-your-input.md` for anything that needs the owner.
5. **Close** — commit. End the reply with exactly:
   `Finished: <part>. Next: <part>. Suggested: <model> / <effort>.`

## 2. Principles (from Karpathy's observations; enforced, not aspirational)
- **Think before coding.** State assumptions; surface tradeoffs; stop and ask when confused instead of guessing.
- **Simplicity first.** Minimum code that solves the stated problem. No speculative abstractions or options.
- **Surgical changes.** Touch only what the request needs; match existing style; mention unrelated dead code,
  don't delete it.
- **Goal-driven.** Turn "fix the bug" into "write a failing test, then make it pass." Loop until verified.
- **Hard constraints live in code.** A prompt is a request; a post-condition check is a guarantee.

## 3. Model / effort table
| Task | Model | Effort |
|---|---|---|
| Design, architecture, audits, hard debugging, prompt work needing real-run verification | Opus 5 (Fable 5.1 if budget allows) | high |
| Implementing a specified feature, wiring, docs, refactors covered by tests | Sonnet 5 | medium |
| Trivial edits, questions answerable from one file | Sonnet 5 | low |
| Reviewing a plan or a risky diff before merge | Opus 5 | high |

Start on Opus for the plan, switch to Sonnet to build, switch back only when stuck.

## 4. Hygiene
- Never print secrets (`.env`); check length/prefix only.
- Tests are hermetic (env-overridable data dir); no test touches real user data.
- Clean generated artifacts before committing.
- `ruff check` + `pytest` green before every commit.
- Commit messages say what was verified, not just what changed.

## 5. Skills in use (edit per project)
- `mattpocock-skills`: `/grill-me` before any non-trivial build; keep `CONTEXT.md` as the shared vocabulary.
- `agent-skills` (addyosmani): `code-review-and-quality`, `test-driven-development`, `interview-me`.
- `ponytail`: on for build sessions.
