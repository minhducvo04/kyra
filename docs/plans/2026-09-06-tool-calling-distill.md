# Distilling the Agent Specialist into a local model (plan, 2026-09-06)

**Status: COMPLETE 2026-09-06 - measured, verdict is do-not-activate. See `docs/tool-calling-distill.md`.**

**Goal.** Today every `tool` turn goes to Claude (`AnthropicLLM.respond_with_tools`). Measure whether a local
model, LoRA-tuned on Claude's own tool-calling traces, can make the same calls with the same arguments on a
handwritten held-out suite - and switch it in behind the existing interface only if it clears a stated bar.

**Decisions taken (Duc, 2026-09-06):** build this next; casual-but-real questions label `local` for the router's
next round (recorded in `needs-your-input.md` #10). Assumptions I'm making, stated so they can be overruled:
- Student: `mlx-community/Qwen2.5-7B-Instruct-4bit` (already cached; flawless on the old 11-case suite), with
  1.5B/3B as latency ablations if the 7B clears the bar.
- Teacher: production Sonnet 5 through the *real* `respond_with_tools` loop against a scripted registry, so the
  traces are exactly what ships. Tool schemas get `cache_control` so the schema block (measured: 2,020 tokens) is cached -
  a production saving too, not just a data-gen one.
- Bar to activate: ≥ 95% of the teacher's suite accuracy, and 0 over-triggers on the no-tool cases. Below that
  it stays a documented option, like the 2026-09-02 benchmark verdict.
- Spend: ~500 teacher traces ≈ $3-5 with caching. Pilot 40 first, check quality, then the full run.

## Steps
1. **Held-out suite, handwritten first** `tests/data/tool_testset.jsonl` (~70 cases: every tool explicit +
   indirect, ~12 keyword-bait no-tool, ~10 find-then-act multi-step with scripted list results, ~5 two-tool
   chains, ~5 under-specified asks that should get a question, not a call). Fixed "today" so date arithmetic is
   checkable. -> verify: `test_tool_ft.py::test_suite_is_well_formed`, never generated, never trained on.
2. **Harness** `companion/agent_ft.py`: `FakeRegistry` (real schemas, scripted results, records calls, raises on
   missing required args like the real one), argument matchers, per-case scoring (call sequence, args,
   over/under-trigger), `evaluate()` for anything with `respond_with_tools`. -> verify: unit tests with a
   scripted backend.
3. **Local tool-calling backend** `companion/local_tools.py::LocalToolLLM(LocalLLM)` - Qwen's native
   `<tool_call>` format via the tokenizer's chat template, same `respond_with_tools` signature as Claude's.
   -> verify: parser tests; a real zero-shot run on the suite (this is the baseline).
4. **Teacher traces** `scripts/tool_ft.py gen` (messages per category, leakage-scrubbed against the suite) and
   `trace` (Claude + FakeRegistry, varied "today"). Traces whose calls contradict their category are dropped and
   counted. -> verify: pilot of 40, eyeball 10, then full.
5. **Build + train** one mlx-lm row per assistant turn (mask-prompt trains only the last turn, so multi-step
   traces are expanded), `tools` field in each row. LoRA on the 7B. -> verify: loss falls; adapter loads.
6. **Eval** teacher vs zero-shot 7B vs LoRA 7B (vs 1.5B/3B if warranted) on the same suite: accuracy, sequence
   accuracy, over-trigger rate, latency. -> verify: `data/tool_ft/eval.json` + `docs/tool-calling-distill.md`.
7. **Activate only if the bar is met**: `KYRA_TOOL_BACKEND=local` + `KYRA_TOOL_ADAPTER` in Settings; the router
   picks `backends["local_tools"]`; Claude stays the default. -> verify: a real chat turn adds a reminder locally.

## Not in scope
Training the conversational reply quality (the local reply after a tool result is scored only for non-emptiness);
persona SFT; DPO (needs a rating UI first).
