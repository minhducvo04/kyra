# On-demand initiatives: build and verification

Build section 1 of the accompanying initiatives plan, using the guard and tool tests written on branch
`session/2026-09-09-initiatives` at `8d8a58e`. Section 2 is deferred. Its intentionally failing daily tests
are not part of this build branch. No persistence, scheduling, acceptance endpoint, UI, or deployment.

Assumptions and implementation choices:

- Collect current due and overdue reminders, project notes, repeated tool reasons, and recent git subjects.
  Empty user stores do not imply empty repository history: git input is explicit in empty-evidence checks.
- New sources construct their stores only on collection and do not create missing files merely to read them.
  Existing shared registry stores retain their current initialization behavior.
- Reminder dates use the machine's local day, including later today and overdue entries, excluding completed
  and undated reminders. Invalid stored data remains an error rather than a misleading empty result.
- Parse model output strictly, reject truncation, allow abstention, and filter unknown citations. Evidence
  content is data. No model-produced command or path is executed.
- A suggestion result ends the tool loop and is rendered directly with its evidence. Sibling tool requests
  in that response are not executed. This prevents proposed actions from becoming follow-up tool calls.
  The model budget is one proposal call per invocation; tool selection is the existing outer model call.
- Existing router cases were written and measured before registration. No adapter or training changes.

1. Copy the independently authored section-1 tests; add source and malformed-output checks -> verify: the
   focused tests fail before the new module exists, with the output retained privately.
2. Implement sources, parser, guard, and tool; register the tool through the shared backend -> verify: focused
   tests and startup checks pass, sources preserve scratch state, and results remain JSON serializable.
3. Test the advisory tool-loop boundary before changing the harness -> verify: a malicious follow-up or sibling
   tool call performs no action and the user still receives suggestions with their evidence.
4. Exercise real hosted inference with fictional scratch sources through the chat tool path, plus empty-store
   abstention -> verify: retain raw replies, evidence, call counts, and before/after state checks privately.
5. Run lint, the full suite, and the PII guard; record the exact results -> verify: local commit contains only
   product code, tests, and engineering documentation. Hand the commit to the other agent for review.

## Build result

Section 1 is implemented. Complete fenced JSON is accepted after a real model response reproduced a parser
failure; malformed, mixed-prose, and truncated responses still abstain. Git is optional when the executable
is absent. The seeded and empty real CLI runs passed, and the final suite passed 543 tests with clean lint.
The tokenizer-dependent check used the existing model cache in offline mode. Details and limitations are in
`docs/log/verification-history.md`. Independent review remains pending; no persistence or UI work was added.
