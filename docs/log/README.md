# Engineering log

Every dated decision, bug, reversal and verification in this project, one file per topic. The entries were moved **verbatim** out of `CLAUDE.md` on 2026-09-09: it had grown to 200 KB in 286 lines, one paragraph per line, which nobody could navigate and which Codex would truncate at 32 KB. Nothing was rewritten or dropped; entries keep their wording and dates.

How to use it: find your subsystem in the table, read that file before changing anything, and add a new entry at the **top** of it when you learn something (dated, with the *why*, and what was verified for real). `grep -rn "phrase" docs/log/` or `python3 scripts/search.py "question"` finds an entry; the log is indexed.

| File | Covers |
|---|---|
| `router.md` | The per-turn router, the classifier, fine-tune rounds 1 to 5, tool-calling distillation, when a retrain is needed. |
| `memory-and-llm.md` | Two memory layers, the Chroma duplicate-id defect, token budgets, streaming, prompt caching, the date in the system prompt. |
| `voice.md` | STT/TTS choices, barge-in, keybindings, sentence-by-sentence speech, latency measurements. |
| `web-ui.md` | The HUD: JOBS/TOOLS/SEARCH/FOCUS panels, streaming and cancel, presence, transcript, corrections, phone layout. |
| `resume.md` | The one-page LaTeX loop, the guard, Detailed mode, the fabrication bugs, token ceilings, GitHub as a source. |
| `autofill-and-boards.md` | Three autofill engines, four board sources, posting fetch and signals, mass apply, LinkedIn and Workday boundaries. |
| `outreach.md` | Drafting in Duc's voice, the LinkedIn boundary, the dash post-condition, the OUTREACH tab. |
| `digest.md` | The 05:00 page, the JSON archive, the notification, the Applications section. |
| `search.md` | Hybrid FTS5 + Chroma, RRF, the reranker result, the privacy default, the chat tool. |
| `focus-and-health.md` | Focus blocks as a blinded n=1 experiment, the browser-found defects, the two health plans. |
| `visionos.md` | The Vision Pro client: transport, orb, Today tab, the SSE bug, the launch-argument seam. |
| `platform-and-deploy.md` | Settings, errors, stores and Alembic, the job queue, the container, AWS, Render, auth, startup cost, the repo move. |
| `publishing.md` | The PII audit, the history-rewrite rehearsal, licence, README, gitignore for personal documents. |
| `tools-and-feeds.md` | Book summarisation, RSS news, the Claude Code handoff tool. |
| `rules-history.md` | The original wording of the working practices and the architecture section, with the failures each rule was written after. |
| `verification-history.md` | What was verified for real and when, newest first. Prepend after every substantive session. |
