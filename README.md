# AI Companion

A personal AI companion project: persona-driven dialogue, long-term memory via RAG, agentic tools, and eventually voice + animation.

Built as a hands-on learning project for AI pipeline / LLM / agentic RAG experience, alongside prep for a software engineering interview.

## Status

Early scaffolding (Phase 0). Design doc lands in Phase 1.

## Setup (run in your own Terminal, not through Claude)

1. `cd` into this folder (drag the folder into Terminal to avoid typing the path).
2. `python3 -m venv .venv`
3. `source .venv/bin/activate`
4. `pip install -r requirements.txt`
5. Open `.env` and replace the placeholder with your real Anthropic API key.
6. `python3 scripts/smoke_test.py` — should print a one-line greeting.
   If it fails, run `python3 scripts/debug_api_key.py` instead — it prints the actual error from Anthropic's API instead of a generic message.
