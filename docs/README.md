# Docs index

Start with `../AGENTS.md` (rules, commands, architecture) and `../README.md` (the public front page). This folder is the rationale and the record.

## Design and reference

| File | What it is |
|---|---|
| `design.md` | The v1 design: persona, memory over retrieval, the conversation core. Where the project started. |
| `agentic-roadmap.md` | Duc's prioritised use cases, routed against the model benchmark and answered. The router's design history. |
| `v2-outline.md` | The industry-shaped v2 outline (services, stores, queue, deploy) that Phase 1 was built from. |
| `agent-workflow.md` | Two-agent rota (and `scripts/session_log.py`, the shared hand-off thread): which of Claude Code and Codex owns each phase, testing assignments, handoff block, models per side. |
| `industry-standards.md` | Every standard applied to the code: what changed, what it was before, why the standard exists. |
| `templates/PROJECT-WORKFLOW.md` | The portable session workflow; this repo's copy is `AGENTS.md` sections 5 and 6, original wording in `log/rules-history.md`. |

## Measurements (numbers first, decisions second)

| File | Question it answers |
|---|---|
| `model-benchmark.md` | Which local model, and how far behind Claude is it? |
| `router-model-benchmark.md` | Which small model classifies a turn best, and could a local model be the tool caller? |
| `router-finetune.md` | Can a LoRA on a 1.5B model replace the few-shot 3B classifier? (Yes: 91.7% vs 73.8% on 122 tokens instead of 2,051.) Rounds 1 to 5. |
| `tool-calling-distill.md` | Can a local 7B take over tool calling? (No: 80.0% vs the teacher's 90.0%. Not shipped.) |
| `search-eval.md` | Lexical vs vector vs hybrid, and which reranker. (Hybrid; the local LLM reranker won.) |
| `voice-latency.md` | Where the seconds go in a spoken turn, and which model for STT. |

## Engineering log

`log/` holds every dated decision, bug and verification, one file per topic, moved verbatim out of the old 200 KB `CLAUDE.md` on 2026-09-09. `log/README.md` is the index. Read the topic file before touching its subsystem.

## Plans (`plans/`, dated, `[step] -> verify: [check]`)

| Plan | Status |
|---|---|
| `plans/2026-09-06-outreach-assist.md` | Slices 1 and 2 built. |
| `plans/2026-09-06-overnight-notes.md` | Built (posting signals, target posting, invented-number fix). |
| `plans/2026-09-06-tool-calling-distill.md` | Measured; result in `tool-calling-distill.md`. |
| `plans/2026-09-06-v2-phase1.md` | Slices 1 to 4 built; Terraform validated, never applied. |
| `plans/2026-09-07-health-companion.md` | Plan only; band decided (Garmin vivosmart 5). |
| `plans/2026-09-07-human-interface.md` | Streaming, cancel, presence, transcript, phone layout built. |
| `plans/2026-09-07-mass-apply.md` | Slices 1 to 3 built (Greenhouse, Ashby, Lever, LinkedIn hand-off, Workday fetch). |
| `plans/2026-09-07-semantic-search.md` | Slices 1 to 4 built. |
| `plans/2026-09-07-visionos.md` | Client builds and talks to the Mac; presence orb and Today tab built. |
| `plans/2026-09-08-ambient-assistant.md` | Concept and research only. |
| `plans/2026-09-08-attention-environment.md` | Slices 0 to 4 built (focus blocks); 5 to 7 open. |
| `plans/2026-09-08-going-public.md` | Auth boundary, `/healthz`, `render.yaml` built; repo public since 2026-09-09; no deploy yet. |
| `plans/2026-09-09-destructive-guard.md` | Planned. Snapshot first, then one guard hook script shared by both harnesses. |
| `plans/2026-09-09-codex-onboarding.md` | This restructure, and how Codex joins the project. |
