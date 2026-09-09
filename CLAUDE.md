# Kyra: Claude Code entry point

@AGENTS.md

`AGENTS.md` is the single instruction file shared with Codex; everything about the project, its privacy rules, commands, architecture and workflow is there. This file holds only what is specific to Claude Code. The engineering log that code comments cite as "CLAUDE.md" moved to `docs/log/` on 2026-09-09 (verbatim, one file per topic; `docs/log/README.md` is the index).

## Model and effort

| Task | Model | Effort |
|---|---|---|
| Design, audits, hard debugging, prompt work needing real-run verification | Opus 5 | high |
| Implementing a specified feature, wiring, docs, refactors covered by tests | Sonnet 5 | medium |
| Trivial edits, questions answerable from one file | Sonnet 5 | low |

Start on Opus for the plan, switch to Sonnet to build, switch back only when stuck. Global principles and the session-end line (`Finished: ... Next: ... Suggested: ...`) come from `~/.claude/CLAUDE.md`; the portable source for both halves is `docs/templates/PROJECT-WORKFLOW.md`.

## Skills in use (vendored under `.claude/skills/`, see `ATTRIBUTION.md` there)

- `grill-me` / `grilling` before any non-trivial build; `interview-me` when an ask is ambiguous.
- `test-driven-development` and `code-review-and-quality` during build and review.
- `ponytail` on build sessions (minimal code); `ponytail-review` to hunt over-engineering in a diff.
- `domain-modeling` maintains `CONTEXT.md` (shared vocabulary) when terms get introduced.

## Tooling

- `.claude/launch.json` defines `kyra-web-ui` for the browser preview tool (`.venv/bin/python scripts/web_ui.py --port 8420`).
- Auto-memory for this project lives under `~/.claude/projects/-Users-minhducvo-Projects-kyra/memory/`. It is personal to Claude Code; a fact the *project* needs goes in `docs/log/` so Codex sees it too.
