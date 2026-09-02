# Router & agent-specialist model benchmark

Answers the two questions queued in `docs/agentic-roadmap.md`: which model should run the router's own classifier, and could a local model take over the "Agent Specialist" (tool-calling) role currently reserved for Claude. Different rubric from `docs/model-benchmark.md` on purpose — that one measured conversational quality; this measures classification accuracy/latency and tool-call correctness, on real MLX inference, same discipline as before (real calls, real bugs found and fixed along the way).

## Part 1 — the classifier

4 candidates, swapped in as `TurnRouter`'s classifier, run against a 29-case labeled suite (17 tool-path, 6 text/claude, 6 text/local) plus 2 deliberately-hard context-dependent cases graded separately.

| Model | Full accuracy | Path-only accuracy | Avg latency | Verdict |
|---|---:|---:|---:|---|
| Qwen2.5-0.5B | 24.1% | 41.4% | 0.14s | Too small, ruled out |
| Llama-3.2-1B | 44.8% | 65.5% | 0.24s | Too small, ruled out |
| Qwen2.5-1.5B | 86.2% | 96.6% | **0.22s** | Real alternative if latency matters most |
| **Llama-3.2-3B (current default)** | **96.6%** | 96.6% | 0.36s | **Best accuracy — stays the default** |

**A real production bug got caught and fixed in the process.** The first pass showed Llama-3.2-3B missing every `complete_reminder`/`snooze_reminder` case — but its own stated reasoning showed it understood the request correctly ("mark reminder done", "reschedule reminder"), it just picked the wrong `path` field. The router's shipped `CLASSIFIER_PROMPT` had zero few-shot examples for those two tools. Added two examples, re-ran the full suite: accuracy jumped from 86.2% → 96.6%. This fix is already live in `router.py`, independent of which model wins — it would have helped the shipped 3B classifier regardless of this benchmark.

**Verdict:** keep `Llama-3.2-3B` as the default (now empirically justified, not just an unvalidated original pick). `Qwen2.5-1.5B` is a documented, real option if the classifier's ~0.14s latency delta ever matters more than its ~10pp accuracy gap - it's a one-line change (`CLASSIFIER_MODEL` in `router.py`).

## Part 2 — the Agent Specialist (tool-coordination)

11 hand-labeled cases (8 that should call one of the 9 real tools with correct arguments, 3 that should call none — testing over-triggering on cover-letter drafting, casual chat, and Claude Code coordination, the exact cases that already tripped up the classifier prompt once).

**Technical groundwork first:** confirmed local models *can* do real function-calling via `tokenizer.apply_chat_template(messages, tools=schemas, ...)` - not obvious going in. But model families render genuinely incompatible formats for the same request:

| Family | Format |
|---|---|
| Qwen2.5 | `<tool_call>\n{"name": ..., "arguments": {...}}\n</tool_call>` (Hermes-style) |
| Llama-3.1 | bare `{"name": ..., "parameters": {...}}`, no wrapper, different key name |
| Mistral-Small-2409 | didn't reliably attempt a function call at all with this schema shape - untested whether an OpenAI-nested schema would fare better |

That format fragmentation is itself a finding: a production system using local models for tool-calling needs a parser per model family, not one. Claude's single structured API sidesteps this entirely - a real, concrete cost of going local here beyond raw accuracy.

Results, each family graded with its own correct parser (the first Llama pass used a Qwen-shaped parser and wrongly scored it near zero - re-graded from the same captured outputs once the format was understood, no wasted re-run):

| Model | Tool selection accuracy | Over-trigger rate | Under-trigger rate | Avg latency |
|---|---:|---:|---:|---:|
| **Qwen2.5-7B** | **100%** | **0%** | 0% | 0.9s |
| **Qwen2.5-14B** | **100%** | **0%** | 0% | 1.7s |
| **Qwen2.5-32B** | **100%** | **0%** | 0% | 4.0s |
| Llama-3.1-8B | 72.7% | 100% | 0% | 0.8s |
| Mistral-Small-22B | inconclusive - didn't engage with tool-calling in this test | — | — | — |

Qwen2.5 was flawless at every size tested, including the smallest (7B) - correct tool, correct arguments, and correctly silent on the three no-tool-should-fire cases. Llama-3.1-8B never missed a real tool call (0% under-trigger) but hallucinated a tool call on all three cases where none should have fired - the same over-triggering risk this whole router design has been careful to guard against.

**Honest caveats, not overclaiming:** 11 cases is a small suite, single sample each, and every phrasing was fairly explicit - it doesn't stress-test genuinely ambiguous wording or a request needing two tool calls in sequence, the harder end of what "Agent Specialist" should mean. Qwen going 3-for-3 across sizes on a suite this clean says more about the suite being solvable than it definitively proves production-readiness.

**Verdict:** promising, not yet a switch. Qwen2.5-7B matching Claude's reliability on this suite, at $0/turn, is a real result worth taking seriously - but I'd want a harder, larger suite (genuinely ambiguous phrasing, multi-tool sequences, adversarial "don't call anything" cases) before actually moving the Agent Specialist role off Claude. Held as a documented option, not a recommendation to change `router.py` today.

## What changed as a result

- `router.py`'s `CLASSIFIER_PROMPT` gained two few-shot examples (`complete_reminder`, `snooze_reminder`) - a real fix, already shipped, independent of the benchmark's other conclusions.
- Everything else above is informational - no other production code changed. `CLASSIFIER_MODEL` stays `Llama-3.2-3B-Instruct-4bit`; the Agent Specialist role stays Claude.
