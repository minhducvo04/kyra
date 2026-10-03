# Fine-tuning the router classifier — LoRA on a small local model vs. the few-shot 3B

_Run 2026-09-05 on an Apple M5 Max (36 GB), MLX 0.32 / mlx-lm 0.31.3. Code: `src/companion/router_ft.py`,
`scripts/router_ft.py`. Numbers below are filled in by the eval step; this file is the write-up, not the raw log._

## Round 8 (2026-09-25): preserve v6 while adding simple-local routing

**Neither seed meets the activation bar; keep v6 active.** The primary candidate recovers main-suite
path accuracy and improves local routing, but regresses on simple-local and initiatives paths.
Assignment 133 changes training data and an experimental prompt only; production is unchanged.

- Preserve the exact v6 train/validation membership: 1,968 / 218 rows across all 43 categories. No old
  message is deleted, rewritten or moved between splits; keyword-bait negatives stay in their categories.
- Claude Sonnet 5 generated six batches for one `simple_local` category. Of 306 returned strings, 26
  were excluded by normalization against old data, other new rows or held-out sets. Builder review
  excluded three ambiguous/non-stable rows; 277 remain, split 250 / 27 with seed 7.
- Review only old text/Claude rows against the new policy. The teacher proposed 22 flips; the builder
  retained five ambiguous old labels and accepted 17 text/Claude-to-text/local flips (13 train, 4 valid).
  Categories: lookup bait 14, room bait 1, explanation bait 2. The full changed-row list and rejected
  proposals remain in the private experiment report; no new tool labels or category deletions.
- Final dataset: 2,218 train / 245 validation. The prompt preserves the v6 tool clause and changes only
  the text/backend policy. It must accompany any future candidate activation; weights alone do not
  encode the complete experiment configuration.
- Two fresh LoRA runs, seeds 7 and 13: Qwen2.5-1.5B-Instruct-4bit, 8 layers, batch 4, learning rate
  1e-4, masked prompt, sequence cap 512, 1,500 iterations. Full validation and checkpoints every 100
  steps. Select the lowest printed validation loss among saved checkpoints; break ties by earlier
  step, then lower seed. Freeze both checkpoints and the primary seed before candidate scoring.
- Score the deployed routing layers on nine suites, with original and precommitted policy-v2 gold.
  The committed 30-case v8 final suite is scored once per frozen model at the final evaluation stage, never for
  recipe or checkpoint selection. Read the missing simple-local fixture from its immutable original
  commit rather than changing the branch's tests. Keep data and evaluation runtime separate from
  production stores and router logs.
- Preserve and disclose inherited leakage: under the existing normalizer v6 has two matches in the
  later everyday suite and one in each confidence set (the confidence sets are exclusion-only here).
  Report everyday with and without its two inherited matches; the full suite keeps the stated bar.
  No v6 overlap with the fresh final suite. New rows are blocked against every held-out set, including
  the final suite, without sending held-out text to the teacher. The final holdout stays out of the
  model's training and validation files. A pre-score identity audit found its line 26 repeats an older
  initiatives case: report the unchanged 30-case set and the 29-new-case subset separately, without
  rewriting the fixture or changing the stated acceptance bar.
- Acceptance is fixed before scoring: no suite loses more than two percentage points of path accuracy
  versus the deployed baseline; local-case exact routing improves on both simple-local and v8-final.
  Report both seeds, including a failure. Do not select a different recipe or seed from test scores.

### Frozen selection and results

Seed 7 selects step 600 (printed full-validation loss 0.009); seed 13 selects step 1000 (0.011).
Seed 7 is primary by the predetermined validation rule. Both ran 1,500 steps; peak trainer memory
was 3.617 GB. No candidate held-out score was read before both selections were frozen.
The deployed baseline reproduces all eight prior suite scores exactly. Every model/suite pair was
scored once; original and policy-v2 gold are computed from the same predictions.

| Suite (n) | Baseline old / v2 / path | Seed 7 old / v2 / path | Seed 13 old / v2 / path |
|---|---|---|---|
| main (85) | 92.9% / 91.8% / 95.3% | 91.8% / 92.9% / 95.3% | 89.4% / 88.2% / 91.8% |
| holdout2 (22) | 95.5% / 95.5% / 100.0% | 90.9% / 90.9% / 100.0% | 95.5% / 95.5% / 100.0% |
| humidifier (16) | 100.0% / 87.5% / 100.0% | 100.0% / 87.5% / 100.0% | 100.0% / 87.5% / 100.0% |
| purifier (14) | 71.4% / 71.4% / 92.9% | 71.4% / 71.4% / 92.9% | 71.4% / 71.4% / 92.9% |
| bulb (14) | 85.7% / 85.7% / 100.0% | 78.6% / 78.6% / 100.0% | 85.7% / 85.7% / 100.0% |
| everyday (30) | 83.3% / 83.3% / 96.7% | 96.7% / 96.7% / 100.0% | 83.3% / 83.3% / 86.7% |
| simple_local (28) | 67.9% / 67.9% / 96.4% | 82.1% / 82.1% / 89.3% | 75.0% / 75.0% / 89.3% |
| initiatives (10) | 80.0% / 80.0% / 90.0% | 60.0% / 60.0% / 80.0% | 90.0% / 90.0% / 100.0% |
| v8_final (30) | 73.3% / 73.3% / 96.7% | 90.0% / 90.0% / 96.7% | 83.3% / 83.3% / 96.7% |

| Model | Simple-local local cases | V8-final local cases | Path bar | Overall bar |
|---|---|---|---|---|
| baseline | 12/20 (60.0%) | 8/15 (53.3%) | reference | reference |
| 7 | 19/20 (95.0%) | 14/15 (93.3%) | FAIL: simple_local, initiatives | FAIL |
| 13 | 16/20 (80.0%) | 11/15 (73.3%) | FAIL: main, everyday, simple_local | FAIL |

Everyday without two inherited matches (old and policy-v2 exact are identical):
- baseline: n=28, exact 82.1%, path 96.4%.
- 7: n=28, exact 96.4%, path 100.0%.
- 13: n=28, exact 82.1%, path 85.7%.

V8-final 29-new-case diagnostic (original and policy-v2 gold are identical):
- baseline: n=29, exact 72.4%, path 96.6%.
- 7: n=29, exact 89.7%, path 96.6%.
- 13: n=29, exact 82.8%, path 96.6%.

**Decision: reject both for activation.** Seed 7's path drops are 7.14 percentage points on simple-local
and 10 points on initiatives; seed 13 drops 3.53 on main, 10 on everyday, and 7.14 on simple-local.
Both improve local-case routing, but that does not waive the per-suite path limit. The final suite's
improvement is real within this measurement; it is not an activation pass or an answer-quality test.

Retaining v6's mixture avoided the prior primary candidate's large main-suite path regression.
This supports the negative-category diagnosis but does not isolate its cause: new data, labels and
training outcomes also differ. The remaining primary errors are advice/writing over-triggering tools
and indirect initiative requests failing to reach tools. Do not tune another checkpoint on these
results. A future design to discuss is preserving the existing tool/text decision and measuring a
separate text-backend decision, with fresh final evaluation data and a latency check; not built here.

Verification: local run excluding the reserved model-sandbox module reported **2 failed, 1715 passed,
3 skipped in 124.73s**. Native OCR passes in Claude's external check (7 handwriting tests), and Claude's
7 model-sandbox checks pass. The old branch's duplicate guard mishandles the policy overlay; the final
fixture also has the acknowledged repeated initiative case. Claude fixed those guard contracts on
master (PRs 67 and 69); this experiment branch was deliberately not merged mid-training. Ruff and all
three privacy guards pass. Report these qualifications rather than claiming a green full branch suite.
Data and evaluation audits verify preserved v6 membership, recorded label changes, no new training
matches to held-out sets, unchanged selection/recipe hashes and one final pass per model.

Evidence: `data/router_ft/assignment-133-candidate/` and `data/verifications/assignment-133/` in the
router-v8 worktree, with a portable candidate bundle under the main checkout's
`data/router_ft/assignment-133-candidate/`; the private handoff report is `data/private_docs/assignment-133-result.md` in the
main checkout. No production adapter is activated by this experiment.

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

## Round 5 (2026-09-08): a search tool, and a category that trained on nothing

Two tools had no coverage. `search_kyra_data` was built the same day (search slice 4), and
`set_application_resume` landed after round 4's data was generated — CLAUDE.md has been carrying "5/5
realistic phrasings already reach the tool path, but it has no held-out coverage until round 5" since then.

**Method:**

- **18 more held-out cases, written and committed before any round-5 data was generated** — 13 in the main set
  (85 total) and 5 in holdout2 (22 total): 6 `search_kyra_data`, 3 `set_application_resume`, and the bait class
  the new tool invites — lookup-shaped questions that are really general knowledge ("what's the difference
  between BM25 and TF-IDF", "how do I search for a file by name in bash") and casual uses of search/find.
- **Four new categories**: `search_kyra_data`, `set_application_resume`, `text_hard_negative_lookup`
  (claude) and `text_hard_negative_lookup_local`, 60 each. One clause added to `COMPACT_SYSTEM`.
- 1,699 train / 188 valid (round 4: 1,483 / 164), so **1,150 iters** keeps the epoch count matched to round 4's
  1,000 at its smaller size. Both seeds again, 7 and 13.

**A defect the round exposed, now fixed in code.** `parse_json_array()` returns `[]` for any reply it cannot
read, and `generate_synthetic()` logged `got=0` and carried on. On the first round-5 run, two of the four new
categories returned nothing and the run still reported success; the same command a minute later gave 60 each.
A category that silently contributes nothing is exactly how a tool ends up untrained while the run looks like
it worked — the gap round 3 existed to close, reappearing one level up in the pipeline. It now retries twice
and then raises, with a test.

**Worth knowing for the next tool.** The router decides *path and backend only* — which tool to call is
Claude's job — so a new tool that overlaps an existing one's domain (`search_kyra_data` vs
`list_job_applications` for "what jobs am I tracking") costs the router nothing, because both are the tool
path. The entire risk of adding a tool sits on the text side: the bait it invites. That is why three of the
four new categories are the tool and its two bait buckets.

### Results, on both held-out sets

| Adapter | Data | Seed | Main 85 | Path-only | Holdout2 (22) | Round-5 cases (13) |
|---|---|---|---|---|---|---|
| qwen1.5b-v4-s13 (shipped) | round 4 | 13 | 89.4% | **94.1%** | 95.5% | **11/13** |
| qwen1.5b-v5 | round 5 | 7 | **91.8%** | 92.9% | **100%** | 12/13 |
| qwen1.5b-v5-s13 | round 5 | 13 | **91.8%** | **94.1%** | 95.5% | 12/13 |

Latency and prompt size are unchanged: 0.15 s mean, 164 prompt tokens.

### The finding: the retrain bought one case, and the standing rule needs qualifying

**The adapter that had never seen either new tool already scored 11 of 13 on the round-5 cases.**
"what did we decide about X", "find that note where...", "use the Northwind resume for that application" are
close enough to phrasings it already knew that it routed them to the tool path anyway. Round 5 moved that to
12/13 — one case.

And it is a straight trade, not a gain. Both v4-s13 and v5-s13 make **exactly five path errors on 85 cases**.
v4 fails two round-5 cases and handles the advice bait; v5-s13 fixes one of those and breaks two advice cases
v4 got right ("what does a good STAR answer for Ownership look like", "what does a reposted job usually mean")
— the very cases round 4 added its advice bucket for. **Adding categories has a budget**: the new lookup-bait
bucket appears to have displaced some of what the advice bucket bought. The +2.4 points of headline accuracy
are backend labels, not paths.

The two sets also disagree, each by a single case: holdout2 prefers seed 7 (100% vs 95.5%), the main set's
path-only prefers seed 13 (94.1% vs 92.9%). One case is 4.5 points on 22 — the same noise floor round 4
measured.

**So the standing rule "a new tool means a retrain" is too strong.** The honest version: *measure the current
adapter against handwritten cases for the new tool first, and retrain only if it actually fails them.* Round 3
retrained because the new tools genuinely were mishandled; round 5 retrained because the rule said to, and the
measurement afterwards says it was not needed.

**One case no adapter gets right**, at either seed, in either round: `what does CLAUDE.md say about the resume
token budget` → text. Naming a file and asking what it says is about as clear a search request as exists, so
this is a real gap rather than an ambiguous label; it wants its own seed phrasings next round.

### Decision: not activated

`KYRA_CLASSIFIER_ADAPTER` still points at **`qwen1.5b-v4-s13`**. Path accuracy — the metric that decides
whether a tool loop runs at all, where a wrong backend still answers — is identical at 94.1%, and swapping
production config on a wash is not worth the two advice regressions. Both v5 adapters are on disk.

If Duc wants the new tools represented in the training data rather than handled by generalization (60 examples
each versus zero, which the 13 held-out cases cannot measure), the switch is one line in `.env`:

    KYRA_CLASSIFIER_ADAPTER="mlx-community/Qwen2.5-1.5B-Instruct-4bit:data/router_ft/adapters/qwen1.5b-v5-s13"

Verified through the real `TurnRouter` with that spec: **12/13 paths**, identical to the harness, same single miss.

## Round 6 (2026-09-18): two room tools, the initiatives tool, and why the last iteration is not the adapter

`humidifier_status` and `humidifier_control` shipped the same day; `suggest_initiatives` had been waiting since
2026-09-09 with two known misses. Sixteen handwritten humidifier cases were committed before any data was generated.

**Method.** Five categories, 60 each (299 kept): the three tools and two bait buckets for the text side (general
questions about humidity, air and sleep; casual talk about weather and lights). One clause added to `COMPACT_SYSTEM`.
Sources: all five earlier files plus `round6.jsonl`; 1,968 train / 218 valid; 1,330 iterations to keep the epoch count
matched to round 5; seeds 7 and 13; every other parameter as round 5.

| Adapter | Main 85 | Path-only | Holdout2 (22) | Initiatives (10) | Humidifier (16) |
|---|---|---|---|---|---|
| qwen1.5b-v4-s13 (was live) | 89.4% | 94.1% | 95.5% | 70% | 62.5% |
| v6 seed 7, final (1330) | 92.9% | 95.3% | 100% | 80% | 100% |
| v6 seed 13, final (1330) | 84.7% | 84.7% | 90.9% | 90% | 100% |
| **v6 seed 7, iter 1200 (activated)** | **92.9%** | **95.3%** | 95.5% | 80% | **100%** |
| v6 seed 13, iter 1200 | 92.9% | 95.3% | 95.5% | 70% | 100% |

All rows were measured with the new prompt clause, including the old adapter, which never saw it in training.

**The finding.** Seed 13's final weights sent 13 text messages to the tool path, every miss in the same direction.
Its validation loss was 0.005 at iteration 1200 and 0.023 at 1330; seed 7's was 0.005 and 0.008. The checkpoint was
chosen by validation loss before the held-out numbers for it were read, and there both seeds agree to the case on
the main set. A single seed would have shipped either a lucky final or an unlucky one without knowing which.
Earlier rounds used final weights; their validation curves were not checked for this and should be before the next
round draws conclusions from them.

**Two leaks closed.** The generator blocked the two original held-out files by name; it now blocks every
`tests/data/router_testset*.jsonl`. A new test that no seed phrasing equals a held-out case failed on seven existing
seeds. Generated rows equal to held-out text were already dropped, so no trained adapter saw them, but the seeds
steer what gets generated.

**Decision: activated.** `KYRA_CLASSIFIER_ADAPTER` points at `qwen1.5b-v6-c1200`. Verified through the real
`TurnRouter`: 16/16 humidifier paths, and a real spoken-style turn answered from the device.

## Round 7 (2026-09-25): simple answers local, candidate only

**Outcome: do not activate either candidate.** The validation-selected seed 7
improves simple-local from 67.9% to 85.7%, but main path accuracy falls from
95.3% to 80.0% and holdout2 path from 100% to 77.3%. Seed 13 improves simple-local
to 96.4% but still regresses on those suites. Selection remains seed 7; choosing
13 after seeing its test scores would violate the predeclared selection rule.

The policy is local for simple facts, definitions, conversions, spelling and
arithmetic; Claude for depth, advice, writing, synthesis and supplied personal
context. Requests for tools retain the tool path and its existing approvals and
outbound gate. This is a measured candidate, not an activation. The production
COMPACT_SYSTEM and adapter setting are unchanged. The candidate prompt is stored
with its adapters under the ignored router-simple-local worktree data directory;
activation would need that exact prompt as well as the selected weights.

The new handwritten 28-case suite was committed at 6a29cac before generation.
All router held-out and confidence suites are excluded by normalized message,
including from reused synthetic examples; their fingerprints are saved. No
held-out prompt was sent to the teacher or used to choose a checkpoint.
Claude's separately committed policy-v2 overlay (46d8bb8) changes three old labels
for secondary scoring; the original suites remain byte-identical.

Generation completed ten 60-example categories through the gated Claude client
before the API refused further calls for insufficient credit. The remainder is
explicitly mixed-source: Codex independently authored 25 synthesis and 25
fictional-personal-context examples and small device-intent combinations; prior
synthetic coordination examples were reused. Prior tool examples were retained;
old text categories were otherwise removed to avoid contradictory policy labels.
No account recharge, private-data upload or live-device command was performed.
The generated rows and exact generation/assembly scripts remain ignored.

After deduplication: 2,150 examples (1,451 tool/Claude, 422 text/local, 277
text/Claude). Fixed seed-7 90/10 split: 1,935 train, 215 validation; normalized
held-out overlap zero, train/validation messages disjoint. Both fresh LoRA runs
use Qwen2.5-1.5B-Instruct-4bit, 8 layers, batch 4, learning rate 1e-4, prompt
masking, length 512 and 1,300 iterations (about 2.7 epochs); seeds 7 and 13.
Validate over the entire validation set and save every 100 iterations. Select
the lowest printed validation loss among saved checkpoints; ties choose the
earlier iteration and then lower seed. Freeze selection before held-out scoring.

Evaluation uses TurnRouter.route_unlogged in isolated auto-mode state, preserving
the acknowledgement rule and complexity bias, without writing the production
router log. It measures routing decisions, not the local answer model's factual
quality. Latency excludes one warm-up; it is single-machine wall time.

Limitations: teacher labels are synthetic, the authored device combinations have
shared templates across the split, and very low validation loss is not proof of
real-world generalization. The two seeds share one data split; they quantify
training variation, not uncertainty over independent datasets.

### Frozen selection and held-out results

| Seed | Selected iteration | Printed validation loss |
|---|---:|---:|
| 7 | 1100 | 0.000 |
| 13 | 900 | 0.001 |

Printed losses have three-decimal precision; 0.000 is not proof of zero loss.
Both checkpoints were selected before candidate held-out evaluation; overall
selection is seed 7. Every table cell below is original exact / policy-v2 exact /
path accuracy. The policy overlay changes only main and humidifier labels.

| Suite (n) | Baseline old / v2 / path | Seed 7 old / v2 / path | Seed 13 old / v2 / path |
|---|---|---|---|
| main (85) | 92.9% / 91.8% / 95.3% | 70.6% / 71.8% / 80.0% | 77.6% / 77.6% / 82.4% |
| holdout2 (22) | 95.5% / 95.5% / 100.0% | 59.1% / 59.1% / 77.3% | 81.8% / 81.8% / 86.4% |
| humidifier (16) | 100.0% / 87.5% / 100.0% | 75.0% / 87.5% / 93.8% | 75.0% / 87.5% / 93.8% |
| purifier (14) | 71.4% / 71.4% / 92.9% | 100.0% / 100.0% / 100.0% | 92.9% / 92.9% / 100.0% |
| bulb (14) | 85.7% / 85.7% / 100.0% | 85.7% / 85.7% / 92.9% | 92.9% / 92.9% / 100.0% |
| everyday (30) | 83.3% / 83.3% / 96.7% | 93.3% / 93.3% / 96.7% | 96.7% / 96.7% / 96.7% |
| simple_local (28) | 67.9% / 67.9% / 96.4% | 85.7% / 85.7% / 89.3% | 96.4% / 96.4% / 96.4% |
| initiatives (10) | 80.0% / 80.0% / 90.0% | 90.0% / 90.0% / 100.0% | 80.0% / 80.0% / 90.0% |

Mean routing latency: about 0.186-0.191 s for seed 7 and 0.187-0.195 s for seed
13 on suites other than everyday; everyday averages about 0.137-0.138 s because
acknowledgements bypass the classifier. Baseline is about 0.195-0.197 s, everyday
0.142 s. This small difference is not a deployment justification. Per-suite p90,
per-case errors and full curves are in the private verification bundle.

**Codex's mistake and next experiment:** removing every old text category except
coordination discarded valuable unchanged casual and keyword-bait negatives,
not just examples whose backend label conflicted with the new policy. Broad
text-to-tool regressions are consistent with that loss of coverage, but this
mixed-source/prompt/data change does not isolate causality. A follow-up should
preserve unaffected independent examples, adjudicate only genuinely conflicting
policy labels, and have Claude write a fresh holdout before another experiment.
Do not copy these test misses into training or repeatedly tune against them.
The new policy also needs an answer-quality check on the local answer model;
this work measures the classifier only.

Verification: 1715 passed, 2 skipped in 53.46s; Claude separately ran the seven
reserved sandbox tests (0.70s). Ruff and privacy guards pass. No adapter or
production prompt activated, no held-out files edited, no push. Persistent
private bundle: `data/router_ft/assignment-129-candidate/` in the main checkout;
it includes selected weights, candidate prompt, data, hashes, scripts, loss
curves, baseline/candidate evaluations and the precommitted overlay. Working
proof also remains at `data/verifications/assignment-129/` in the
router-simple-local worktree. The bundle's selection.json points to its durable
copies; it is not a runtime configuration.
