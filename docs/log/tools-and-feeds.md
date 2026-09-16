# Small tools and feeds

## 2026-09-16: shared tool-run audit

The default registry now attaches a lazy ToolRunStore so registry callers record arguments, outcome, a 300-character result summary, start time and elapsed time in private runtime state. The console receives the id from its own execution rather than querying the latest row, and an audit failure logs its exception type without exposing arguments or changing the tool's result. The idempotent Alembic revision adds tool_runs; scratch migration, successful/error runs, eight concurrent receipt matches and startup-import checks were verified. Confirmation flags cover drafting, autofill, outreach copy, initiatives, posting fetch and the two RSS tools. Proof is under `data/verifications/console/`; the supplied test mismatch and acceptance limitations are recorded in `data/private_docs/assignment-P01-result.md`.

Book summarisation, RSS news, the Claude Code handoff tool.

Entries below were moved verbatim from `CLAUDE.md` on 2026-09-09 (original order kept, newest work is usually nearer the top of each section). Add new entries at the top of this file, dated, with the *why*.

## Entries

- Book summarization: `python3 scripts/summarize_book.py path/to/book.pdf` (or `.epub`) — reads a book you already have, saves a structured summary into the spaced-repetition learning store. Standalone script on purpose, not a conversational tool — see `docs/agentic-roadmap.md`, job #7.
- **Tech news is RSS, not scraping** (`news.py::FEEDS`) — NYT Technology, TechCrunch, Ars Technica, The Verge, Hacker News, all official/free feeds. The Verge's feed is Atom format, not RSS 2.0 - `_parse_feed()` handles both; don't assume every feed uses `<item>`.
- **Claude Code handoff is draft-only, by explicit decision** (`handoff.py::HandoffTool`) — composes a task brief and copies it to the clipboard (`pbcopy`, macOS-only) for Duc to paste into Claude Code himself. It does not execute anything or launch a session. Don't extend this to actually spawn `claude` without checking in first — see `docs/agentic-roadmap.md`, job #1.
