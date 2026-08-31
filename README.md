# AI Companion — Kyra

A personal AI companion project: persona-driven dialogue, long-term memory via RAG, agentic tools, and eventually voice + animation.

Kyra's personality is warm and protective (Black Widow looking out for the team) crossed with sharp and resourceful (Hermione Granger) — see `docs/design.md` for the full design.

Built as a hands-on learning project for AI pipeline / LLM / agentic RAG experience, alongside prep for a software engineering interview.

## Status

Phase 1 done: persona, long-term memory (Chroma-backed `MemoryStore`), and the conversation orchestrator are implemented and have been verified end-to-end (multi-turn chat + memory persisting and getting recalled across sessions). Next up: you trying `scripts/chat.py` yourself, then Phase 2 (agentic tools).

## Setup (run in your own Terminal, not through Claude)

1. `cd` into this folder (drag the folder into Terminal to avoid typing the path).
2. `python3 -m venv .venv`
3. `source .venv/bin/activate`
4. `pip install -r requirements.txt`
5. Open `.env` and replace the placeholder with your real Anthropic API key.
6. `python3 scripts/smoke_test.py` — should print a one-line greeting.
   If it fails, run `python3 scripts/debug_api_key.py` instead — it prints the actual error from Anthropic's API instead of a generic message.
7. `python3 scripts/chat.py` — chat with Kyra. First run downloads a small embedding model (for memory search) — needs network and takes a few seconds, one time only. She'll remember things across runs (stored in `data/memory_db`, gitignored).
