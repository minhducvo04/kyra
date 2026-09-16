# Learning reels

## 2026-09-16: slice A reviewed, two real-run defects fixed, first real proposals accepted

Claude Code reviewed the build below (the working tree Codex left unstaged; Claude committed it).
The two failing fixtures were Claude's own: `_approved_moment` reused one span on one source,
which the duplicate guard the same file demands refuses; each call now takes a distinct end
second. The real run then found what hermetic tests cannot: Claude wrapped the JSON in a code
fence (rejected as `json`) and the 4000-token budget was spent partly by adaptive thinking, so
the reply stopped mid-record (rejected as `truncated`, the guard doing its job). Fixes: the
parser unwraps a fence before `json.loads` (content untouched, raw file keeps the fence, test
added), the prompt asks for bare JSON, the CLI backend budget is 16000. Second run: 2 proposed,
2 passed every guard, 0 rejected. Numbers and the per-moment approval verdicts are in
`docs/reels-eval.md`. The 73 local failures Codex saw come from a `KYRA_API_TOKEN` now present in
the developer's `.env` that `tests/conftest.py` does not pin; flagged as its own task, not this
branch's. The three fixes above are Claude's code; Codex reviews them at the next hand-off.

## 2026-09-16: slice A built, acceptance awaits fixture corrections and live review

Implemented the module and synchronous CLI from `docs/plans/2026-09-13-learning-reels.md`.
YouTube registration fetches metadata through a redirect-refusing oEmbed opener. Playback is an
embed URL; this module does not fetch source media. Supplied transcripts and their SHA-256 are
stored with the source. The source adapter is the extension point for later source types.

Proposals retain raw output before parsing, cap model input by time and words, and enforce named
guards: `window_bounds`, `window_length`, `evidence_span`, `options_distinct`, `one_correct`,
`hint_leaks_answer`, `dash`, `transfer_type`, `fact_evidence`, `json`, and `truncated`. The manual
path keeps the selected bounds. Evidence is checked against whole cues touching the selected
interval, including a cue ending exactly at its start; the fixtures require that preceding
context. This is cue-level provenance, not word-level alignment. A single cue longer than fifteen
minutes requires finer supplied timestamps. Plain text carries no time bounds. The firewall view
contains facts and example constraints but drops evidence quotes and the source timestamp.

These guards establish structure, not truth, originality, pedagogical quality, or semantic hint
safety. A matching quote does not establish entailment; a marked answer does not establish that
only one option is valid. Human approval remains necessary. Live generation quality has not been
measured in this build.

Moment bodies are immutable in storage; rejection permits a new record on the same source span.
Learner state and attempts commit together. Delayed review stays open after a wrong answer until
correct or revealed. Only a correct first try advances the interval ladder; a completed review
with an error schedules one day later. Hint exposure and daily XP use UTC days. Review-opening
and completion flags retain the denominator for weekly delayed-recall accuracy. Learner state is
JSON in the composite-key table; events have queryable kind, time, scoring and review flags.

Migration `e3a8c219d740` adds four tables after `d210a93e7b61`. Existing startup-created tables are
preserved. Scratch verification exercised upgrade, schema comparison against runtime metadata,
downgrade/upgrade, and startup-before-migration with a saved source and transcript. No production
database was migrated.

Verification:

- Focused tests: **2 failed, 76 passed in 2.84s**. Both failures are conflicting fixtures in
  `test_mastery_needs_all_four_conditions` and
  `test_progress_reports_both_weeks_counts_and_never_a_bare_percentage`: they create another
  approved moment on the identical source/span, which the dedicated duplicate test requires
  refusing. The duplicate guard is retained; repository tests were not edited. A separate
  diagnostic ran those two scenarios with a distinct source per moment and both passed.
- Full suite with `KYRA_API_TOKEN=''` scoped to the test process:
  **2 failed, 713 passed, 1 skipped in 67.33s (0:01:07)**. The same two failures remain.
  The initial unisolated run inherited the local token setting and produced unrelated 401s:
  **75 failed, 633 passed, 1 skipped, 7 errors in 64.99s (0:01:04)**.
- `ruff check src scripts tests`: **All checks passed!**
- Separate-process CLI checks passed for list, show, approve, quick quiz, learn quiz, due review,
  progress, and reject using fictional fixture sources in scratch databases.
- Additional scratch checks passed for UTC rewards across offsets/midnight, a review retry across
  midnight, persistent learner state, multiline dash guards, empty manual proposal rejection,
  continuation after an invalid window, and the 2,500-word input cap on long paragraphs.

Local proof: `/private/tmp/reels-a-focused.log`, `/private/tmp/reels-a-pytest.log`,
`/private/tmp/reels-a-pytest-isolated.log`, `/private/tmp/reels-a-runtime.log`, and
`/private/tmp/reels-a-edge-cases.log`. Scratch databases were removed. Live oEmbed registration,
Claude proposal/mark calls, and human teaching-quality acceptance remain for the lead reviewer.
