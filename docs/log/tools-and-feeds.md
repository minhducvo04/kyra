# Small tools and feeds

Book summarisation, RSS news, the Claude Code handoff tool.

Entries below were moved verbatim from `CLAUDE.md` on 2026-09-09 (original order kept, newest work is usually nearer the top of each section). Add new entries at the top of this file, dated, with the *why*.

## Entries

- Book summarization: `python3 scripts/summarize_book.py path/to/book.pdf` (or `.epub`) — reads a book you already have, saves a structured summary into the spaced-repetition learning store. Standalone script on purpose, not a conversational tool — see `docs/agentic-roadmap.md`, job #7.
- **Tech news is RSS, not scraping** (`news.py::FEEDS`) — NYT Technology, TechCrunch, Ars Technica, The Verge, Hacker News, all official/free feeds. The Verge's feed is Atom format, not RSS 2.0 - `_parse_feed()` handles both; don't assume every feed uses `<item>`.
- **Claude Code handoff is draft-only, by explicit decision** (`handoff.py::HandoffTool`) — composes a task brief and copies it to the clipboard (`pbcopy`, macOS-only) for Duc to paste into Claude Code himself. It does not execute anything or launch a session. Don't extend this to actually spawn `claude` without checking in first — see `docs/agentic-roadmap.md`, job #1.
