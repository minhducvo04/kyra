# Learning reels: what the runs showed

Counts only. The transcript in every run so far is the fictional Northwind fixture under
`tests/data/reels/`; no real lecturer's words were sent anywhere. Newest first.

## 2026-09-16: slice A, first real Claude call (Claude Code, independent acceptance)

Setup: `MomentProposer` over `AnthropicLLM` (claude-sonnet-5), scratch `KYRA_DATA_DIR`, the SRT
fixture (12 cues, 205 s, 1 window), `max_moments=2`. Proof kept privately under
`data/verifications/2026-09-16-reels-a/` (the raw reply and the script).

| Run | Output budget | Proposed | Passed every guard | Rejected (guard) |
|---|---:|---:|---:|---|
| 1 | 4000 | cut at 2.7 KB | 0 | 1 (`truncated`); the reply was also wrapped in a code fence |
| 2 | 16000 | 2 | 2 | 0 |

Two defects the hermetic tests could not see, both fixed before commit: the model fenced the JSON
despite the prompt (parser now unwraps a fence, prompt asks for bare JSON, test added), and
adaptive thinking spent the 4000-token budget so the reply stopped mid-record (budget 16000).

Teaching-quality verdicts, the number the guards cannot give (a person reading each accepted
moment):

| Moment | Types | Would approve | Note |
|---|---|---|---|
| 15 s to 84 s, small step converges | predict, apply | yes | the transfer question restates the initial one with a different start value; a weak transfer |
| 84 s to 170 s, large step diverges | identify_wrong, apply | yes, with an edit | the three distractor hints each confirm their statement is true, so the odd one out is given away by elimination: a semantic hint leak the `hint_leaks_answer` guard cannot catch |

Both moments chose the cue boundaries the fixture offers; the second matches the span a person
would mark (the surprising result and the misconception correction). Overlap against the held-out
spans is a slice B measurement (`tests/data/reels/heldout/`, not yet written).

Cost of run 2: one call, 8.4 KB of output, on a 1,200-word transcript; token counts were not
captured by the script and are a slice B item.

oEmbed: one real registration of a public MIT OpenCourseWare lecture URL returned its title,
channel name and `EMBED_ONLY`; nothing but the oEmbed host was contacted.
