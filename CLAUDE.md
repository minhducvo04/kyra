# Kyra: Claude Code entry point

@AGENTS.md

`AGENTS.md` is the single instruction file shared with Codex; everything about the project, its privacy rules, commands, architecture and workflow is there. This file holds only what is specific to Claude Code. The engineering log that code comments cite as "CLAUDE.md" moved to `docs/log/` on 2026-09-09 (verbatim, one file per topic; `docs/log/README.md` is the index).

## Model and effort

| Task | Model | Effort |
|---|---|---|
| Design, audits, hard debugging, prompt work needing real-run verification | Opus 5 | high |
| Writing tests, running verification, maintaining plans and docs | Sonnet 5 | medium |
| Trivial edits, questions answerable from one file | Sonnet 5 | low |

Lead planning, test design, independent testing, and review. Use Opus for plans and demanding reviews, and Sonnet for specified tests and routine verification. Codex is the primary code builder; contribute your own ideas alongside Codex during brainstorming. Which phases are Claude Code's and which are Codex's: `docs/agent-workflow.md`. Global principles and the session-end line (`Finished: ... Next: ... Suggested: ...`) come from `~/.claude/CLAUDE.md`; the portable source for both halves is `docs/templates/PROJECT-WORKFLOW.md`.

## Skills in use (vendored under `.claude/skills/`, see `ATTRIBUTION.md` there)

- `grill-me` / `grilling` before any non-trivial build; `interview-me` when an ask is ambiguous.
- `test-driven-development` and `code-review-and-quality` during build and review.
- `ponytail` on build sessions (minimal code); `ponytail-review` to hunt over-engineering in a diff.
- `domain-modeling` maintains `CONTEXT.md` (shared vocabulary) when terms get introduced. No `CONTEXT.md` exists yet; the skill would create it.

## Tooling

- `.claude/launch.json` defines `kyra-web-ui` for the browser preview tool (`.venv/bin/python scripts/web_ui.py --port 8420`).
- Auto-memory for this project lives under `~/.claude/projects/-Users-minhducvo-Projects-kyra/memory/`. It is personal to Claude Code; a fact the *project* needs goes in `docs/log/` so Codex sees it too.
