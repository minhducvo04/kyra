# Fine-tuning the router classifier — LoRA on a small local model vs. the few-shot 3B

_Run 2026-09-05 on an Apple M5 Max (36 GB), MLX 0.32 / mlx-lm 0.31.3. Code: `src/companion/router_ft.py`,
`scripts/router_ft.py`. Numbers below are filled in by the eval step; this file is the write-up, not the raw log._

## The question

Every auto-mode turn starts with the router's classifier deciding `tool` vs `text` and, for text, `claude` vs
`local`. Today that is Llama-3.2-3B driven by `router.CLASSIFIER_PROMPT`: tool descriptions plus ~25 few-shot
examples, paid on every single turn. Can a smaller model, LoRA-tuned on the same decision, make the same call
from a ~60-token prompt (`router_ft.COMPACT_SYSTEM`), faster, and at least as accurately?

## Method

- **Held-out test set, handwritten:** `tests/data/router_testset.jsonl` — 61 cases (33 tool, 13 text/claude,
  15 text/local) in rounds 1-2, 72 from round 3 (see below) written by hand before any data was generated. It deliberately includes keyword-bait negatives
  ("remind me how quicksort works" is an explanation, not a reminder; "can you snooze for a second" is chat) and
  indirect tool phrasings ("the dentist one is done, tick it off"). It is never trained on, never used to pick a
  checkpoint, and the generator drops any synthetic message that normalizes to a test message.
- **Training data, synthetic:** one Claude call per category in `router_ft.CATEGORIES` (15 tool intents, 3
  text/claude intents, 3 text/local intents, 1 hard-negative bucket), ~45 messages each, deduped: 999 examples in
  round 1, plus 151 keyword-bait examples in two buckets added for round 2 - **1,150 generated examples in total**
  (1,035 train / 115 valid after the split). Split 90/10 train/valid, seeded. Ablation sets cap train at 300 and 100 examples with the same validation split.
- **Training:** `python -m mlx_lm.lora`, LoRA on the 4-bit instruct checkpoints, 8 layers, batch 4, lr 1e-4,
  `--mask-prompt` (loss on the label only), 600 iterations for full data (≈2.7 epochs), fewer for the ablations.
- **Eval:** every system answers the same 61 messages with `max_tokens` ≤ 40. Accuracy = path and (for text)
  backend both right; path-only = the tool/text split alone. Latency is wall-clock per message on this machine;
  prompt tokens are the chat-templated prompt length the model actually reads.
- **Baseline is production:** the few-shot classifier is run through the router's own prompt and parser, so the
  comparison is against exactly what ships, not a re-implementation.

## Baseline (measured first, before any training)

| System | Accuracy | Path-only | Mean latency | p90 | Prompt tokens |
|---|---|---|---|---|---|
| baseline few-shot (Llama-3.2-3B-Instruct-4bit) | 73.8% | 85.2% | 0.50s | 0.52s | 2051 |

The earlier 96.6% (`router-model-benchmark.md`) was on a 29-case suite of explicit phrasings. This set is
harder on purpose, and the baseline's 16 misses split three ways:

| Error | Count | Example |
|---|---|---|
| Reasoning request routed to `local` instead of `claude` | 7 | "help me think through how to structure the migration to Postgres" |
| Indirect tool phrasing missed (routed to text) | 5 | "the dentist one is done, tick it off"; "put that CAP theorem summary into my review queue" |
| Keyword bait taken literally (routed to a tool) | 4 | "can you snooze for a second, I need to think"; "add a note to the design doc that we chose SQLite" |

Prompt cost is 2,051 tokens per turn — the tool descriptions, not just the examples, are what make it long.

## Results

_(filled in by `scripts/router_ft.py eval` — see `data/router_ft/eval.json` for per-message misses)_

| System | Accuracy | Path-only | Mean latency | p90 | Prompt tokens |
|---|---|---|---|---|---|
| baseline few-shot (Llama-3.2-3B-Instruct-4bit) | 73.8% | 85.2% | 0.58s | 0.61s | 2051 |
| LoRA qwen1.5b-full (Qwen2.5-1.5B-Instruct-4bit) | 86.9% | 86.9% | 0.15s | 0.15s | 122 |
| LoRA qwen0.5b-full (Qwen2.5-0.5B-Instruct-4bit) | 83.6% | 83.6% | 0.11s | 0.11s | 122 |
| LoRA qwen1.5b-n300 (Qwen2.5-1.5B-Instruct-4bit) | 78.7% | 80.3% | 0.15s | 0.15s | 122 |
| LoRA qwen1.5b-n100 (Qwen2.5-1.5B-Instruct-4bit) | 80.3% | 88.5% | 0.15s | 0.15s | 122 |
| LoRA qwen1.5b-hn (Qwen2.5-1.5B-Instruct-4bit) | 88.5% | 93.4% | 0.15s | 0.15s | 122 |
| LoRA qwen0.5b-hn (Qwen2.5-0.5B-Instruct-4bit) | 86.9% | 90.2% | 0.11s | 0.11s | 122 |
| zero-shot compact (Qwen2.5-1.5B-Instruct-4bit) | 27.9% | 45.9% | 0.12s | 0.13s | 122 |

## Reading the numbers

- **The adapter is the effect, not the prompt or the model.** The same Qwen2.5-1.5B with the same compact
  prompt and no adapter scores 27.9% (zero-shot control row). Everything above that is learned.
- **Best system: LoRA on 1.5B with hard negatives — 88.5% / 93.4% path-only at 0.15 s and 122 tokens**, versus
  the production few-shot 3B at 73.8% / 85.2%, 0.58 s and 2,051 tokens: +14.7 points, ~4x faster, ~17x fewer prompt
  tokens per turn.
- **The data fix mattered more than model size.** Round 1 (999 synthetic examples, 68% tool) gave 86.9% with 7 of
  its 8 misses being keyword-bait negatives routed to a tool. Adding 151 bait examples in two new buckets
  (`text_hard_negative_claude`, `text_hard_negative_local`) took path-only accuracy from 86.9% to 93.4%. The 0.5B
  model got the same treatment and went 83.6% -> 86.9%.
- **Data size:** 100 examples -> 80.3%, 300 -> 78.7%, 900 -> 86.9%, 1,035 with bait -> 88.5%. The 100-example run
  has the highest path-only of round 1 (88.5%) but hasn't learned the claude/local split; 300 sits in a dip,
  which with a single seed is within noise of 100.
- **What's left (7 misses on the winner):** one indirect tool request ("save what you just explained about Raft so
  I review it next week" -> text), two bait cases still routed to a tool ("write a cover letter about octopuses
  for fun", "I applied a patch to the router yesterday"), one keyword-bait explanation routed to a tool ("add a
  note to the design doc..."), and three local-vs-claude calls on bait ("is science news usually reliable") that a
  second labeler could reasonably score the other way. The residual is concentrated on the genuinely ambiguous
  tail of the set, not on real tool requests.
- **Caveats, stated plainly:** 61 test cases means one case is 1.6 points, so differences under ~3 points are not
  meaningful; single training seed; latencies are wall-clock on one machine with other processes running;
  the synthetic data was written by Claude, so its phrasing distribution is Claude's, not Duc's - the held-out set
  being handwritten is what keeps the headline number honest.
- **Decision:** activated `qwen1.5b-hn` in production via `KYRA_CLASSIFIER_ADAPTER`. The few-shot path stays as
  the fallback (unset the variable). Next improvements, in order: a second seed for error bars, real usage turns
  from `router.log` folded into training once there are enough, and a small labeled set of the ambiguous tail
  with two labelers to settle what "correct" means there.

## What this changes in Kyra

Set `KYRA_CLASSIFIER_ADAPTER="<repo>:<adapter dir>"` and `TurnRouter` loads that model with the adapter and
uses `COMPACT_SYSTEM`; unset, it runs the few-shot path. Same `RoutingDecision`, same log fields, same
`route_and_answer()` — the classifier is a config value behind the existing interface, which is the point of
having the interface.

## Round 3 (2026-09-07): seven new tools, same adapter recipe

Between round 2 and this run, seven tools landed in `default_tools.py` (outreach assist: `add_outreach_contact`,
`draft_outreach_note`, `copy_outreach_note`, `update_outreach_status`, `list_outreach`; posting signals:
`analyze_job_posting`, `target_job_posting`). The production adapter had never seen them. Observed in real use:
"what's a good salary range for a new grad SWE in NYC?" went to the tool path.

- **Data:** 7 new tool categories plus 2 bait buckets (`text_hard_negative_outreach` for salary/LinkedIn/repost
  *advice* questions, `text_hard_negative_outreach_local` for casual mentions of recruiters/referrals/targets),
  45 each -> 408 new synthetic examples (`data/router_ft/new_tools.jsonl`). Combined with the round 1-2 data:
  1,403 train / 155 valid. `COMPACT_SYSTEM` gained one clause naming posting signals and outreach (122 -> 145
  prompt tokens).
- **Test set:** 11 handwritten cases appended (7 tool, one per new tool; 3 text/claude bait incl. the salary
  question; 1 text/local bait) -> 72 cases. Written before the data was generated, never trained on.
- **Training:** `qwen1.5b-v3`, same recipe (Qwen2.5-1.5B-4bit, 8 layers, batch 4, lr 1e-4), 950 iters ≈ 2.7 epochs.

| System | Prompt | Accuracy (72) | Path-only | Old 61 subset | New 11 subset | Prompt tokens |
|---|---|---|---|---|---|---|
| LoRA qwen1.5b-hn (production before) | round-2 prompt | 87.5% | 91.7% | 88.5% | 81.8% | 123 |
| LoRA qwen1.5b-hn | round-3 prompt | 91.7% | 93.1% | 91.8% | 90.9% | 145 |
| **LoRA qwen1.5b-v3** | round-3 prompt | **93.1%** | **93.1%** | 91.8% | **100%** | 145 |

Reading it:

- The 88.5% from round 2 reproduces exactly under its own prompt, so the harness is deterministic and the rows
  are comparable.
- The old adapter already generalized to most new-tool phrasings (it learned "job-search request -> tool"), but
  it missed `analyze_job_posting` and took the salary question as a tool call. v3 gets all 11 new cases and sends
  the salary question to text.
- **On the old 61, v3 and hn tie at 91.8% but not on the same cases.** v3 fixed two ("save what you just explained
  about Raft..." -> tool, "can you snooze for a second" -> local) and regressed two: "note for later: I hate early
  morning meetings" -> text (a real `save_memory_note` miss) and "what does a good STAR answer for Ownership look
  like" -> tool (job-search-adjacent advice over-triggering, the same failure class as the salary question). The
  three persistent misses ("cover letter about octopuses", "applied a patch", "add a note to the design doc") are
  unchanged since round 2.
- Through the real `TurnRouter` (env-driven, no harness): salary question -> text, "target this posting <url>" ->
  tool, "read the warning signs..." -> tool, "draft a connection note for Alex" -> tool, "how do I write a good
  LinkedIn note in general" -> text. Still wrong: "what does a reposted job mean" -> tool (the held-out phrasing
  "what does a reposted job **usually** mean" is right), so the repost-bait boundary is thin.
- One case is 1.4 points on 72, so 93.1 vs 91.7 is within noise; the decision rests on the new-tool coverage
  (11/11 vs 10/11) and the fixed over-trigger, not the headline.
- **Decision:** `qwen1.5b-v3` activated via `KYRA_CLASSIFIER_ADAPTER`. `qwen1.5b-hn` stays on disk as the
  rollback. Next: a second seed, and a bait bucket for job-search *advice* questions in general (STAR answers,
  interview prep, salary) since that class now shows up on both sides of the test set.

## Round 4 (2026-09-07): a bucket for career advice, and what a second seed says about round 3

Round 3 shipped on a 1.4-point margin. Two things were owed: a bait bucket for job-search *advice* (round 3
fixed the salary question but still sent "what does a good STAR answer for Ownership look like" and "what does a
reposted job mean" to the tool path), and a second training seed to say whether any of these margins are real.

**Method changes, all in code:**

- `tests/data/router_testset_holdout2.jsonl` — a **second held-out set, 17 cases**, written and committed
  *before* any round-4 data was generated: 8 advice bait (interview detail, negotiating, resume length, cover
  letters, follow-up etiquette, reposts, when to network, equity), 2 casual bait, and 7 real tool requests in the
  same domain so an over-corrected model cannot score well by never calling a tool. The generator now blocks both
  sets; a test asserts the two sets share no message.
- One new category, `text_hard_negative_jobsearch_advice` (45 examples), and wider `save_memory_note` seeds to
  cover the bare-imperative shape ("note for later: ...", "jot this down: ...") that v3 was missing.
- `--seed` on `train`, `--testset`/`--out` on `eval`, and cross-file dedupe in `_load_synth` (each `gen` run only
  deduped within itself). 1,483 train / 164 valid.

### Two seeds of each dataset, both held-out sets

| Adapter | Data | Seed | Main 72 | Path-only | Holdout2 (17) |
|---|---|---|---|---|---|
| qwen1.5b-v3 | round 3 | 7 | 93.1% | 93.1% | 100% |
| qwen1.5b-v3-s13 | round 3 | 13 | 88.9% | 93.1% | 82.4% |
| qwen1.5b-v4 | round 4 | 7 | 91.7% | 91.7% | 100% |
| **qwen1.5b-v4-s13** | round 4 | 13 | **91.7%** | **95.8%** | **100%** |
| qwen1.5b-hn (round 2) | round 2 | 7 | 87.5%* | 91.7%* | 88.2% |

\* under the round-3 prompt, from the table above.

### What the seed says about round 3 — a correction

**Changing only the training seed moved the same dataset by 4.2 points on the main set and 17.6 points on
holdout2.** Round 3's headline (v3 93.1% vs the old adapter's 91.7% under the same prompt) is therefore noise:
its second seed scores 88.9%, below the adapter it replaced. Round 3's own caveat said one case is 1.4 points and
differences under ~3 points are not meaningful; the seed study says the real noise floor on this setup is larger
than that, roughly ±4 points on 72 cases. Nothing about round 3's *tool coverage* changes — 11/11 new-tool cases
is a capability the old adapter did not have, and that was the actual reason to ship it. The accuracy margin was
not.

### What round 4 actually bought: stability, not points

- **v4 scores identically at both seeds** (91.7 / 91.7 main, 100 / 100 holdout2) where v3 swings. On the mean of
  two seeds v4 is 91.7 vs v3's 91.0 on the main set and 100 vs 91.2 on holdout2. The gain is that the answer
  stops depending on the seed.
- **Both target cases are fixed at both seeds:** the STAR question routes to text, and "note for later: I hate
  early morning meetings" routes to the memory-note tool (v3 missed it at both seeds — the widened seeds, not the
  new bucket, fixed that one).
- **The repost case is still not solved.** "what does a reposted job usually mean" is wrong on v4 seed 7 and
  right on seed 13; it was right on v3 seed 7 and wrong on v3 seed 13. Four runs, two each way: this case is a
  coin flip, not a fixed failure. It happens to be right in the shipped adapter.
- **Holdout2 says the bucket generalizes**, on cases that never informed the data: 17/17 at both v4 seeds,
  including all 7 real tool requests, so the advice bucket did not teach it to under-call tools.

### Decision, and the part that inflates the number

Shipped **`qwen1.5b-v4-s13`** via `KYRA_CLASSIFIER_ADAPTER`. The tie-break was stated before looking at the
misses: the two v4 seeds have identical accuracy and identical holdout2, so take the higher **path-only** score,
since a wrong path calls or skips a tool loop while a wrong backend still answers. Said plainly: **choosing
between two seeds by their held-out score is checkpoint selection, and the 95.8% path-only it produces is
optimistic** — the honest summary of round 4 is "91.7% ±0 across seeds, 100% on the second held-out set."
`qwen1.5b-v3` and `qwen1.5b-hn` stay on disk as rollbacks.

Remaining misses on the shipped adapter, all long-standing: three path errors ("what's the news with my sister
lately", "I applied a patch to the router yesterday", "add a note to the design doc that we chose SQLite" — the
last two unfixed since round 2) and three backend-only calls on casual bait that a second labeler could score
either way. Verified through the real `TurnRouter`: 13/13 paths correct, including every case that was wrong in
production before this round.

Next, in order: a third seed would tighten the error bars further, but the cheaper win is that the main set is
now the *only* thing every round is tuned against — the honest move for round 5 is another independent set, or
folding real `router.log` turns in once enough have accumulated.
