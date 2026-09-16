# Scope reconciliation (2026-09-16, Claude Fable 5.1)

What was asked, against what exists on `session/2026-09-16-integration`. Three sources of asks: Duc's
working-loop note (preserved verbatim at the end of `2026-09-15-multi-model-working-loop.md`), the
September 13 hand-off document (on disk only), and Duc's instruction on the night of the 15th: finish the
loop for him and for Father, then the headset, then make it feel like one app. Status words: **built**
(code, tests, real run), **partial** (built with a named gap), **deferred** (a decision or access is
missing), **not started**, **decided against**.

## 1. Duc's note, item by item

| Ask | Status | Where |
|---|---|---|
| Performance and cost table from public benchmarks, with a summary column | partial | Snapshot table in `2026-09-15-multi-model-working-loop.md` section 3 with dated sources; it is benchmark arithmetic, not measured job cost. The measured side starts with the usage ledger (A01) |
| Three tiers: Life-changing, Work, Casual | partial | Tier stored per run and shown (A02). No rule binds to it yet, because only two companies are available; the rule table is written in section 2 of the plan |
| Model choice by quality then cost; a formula | decided against the formula, partial on the mechanism | The plan replaces the quotient with observed pipeline cost per accepted task; the ledger records tokens and provider-reported cost; no selection code exists |
| Human input, local breakdown, Claude plans and assigns, models work, Claude tests | partial | Assignments are records on the page (A03); the actual loop ran through `codex exec` with Claude planning, testing and reviewing. Automatic delegation is deferred to the policy decision in `needs-your-input.md` |
| Token metering by a local model | decided against | Metering is deterministic code (receipts on every run); the plan says why |
| Improvement notes and "what next" proposals after each run | not started | The initiatives queue exists for suggestions in general; nothing feeds it from loop runs |
| Cheap or local skeptic that asks questions | not started | Designed in the plan (a role without release authority); no code |
| Final checks: humanizer, watermark check and removal | partial | Humanizer pass and guards run in Father drafting (F04); document QA scans parts, metadata and facts (F02); no automatic removal and no promise about hidden watermarks, per the hand-off document |
| Summarise, organise, trim; memory palace | not started | Design in the plan (rooms over the existing notes, compaction as a proposal); no code |
| Universal memory palace for agents | not started | Same |
| Father workflow with Claude, Codex and one more company | partial | Father tenant, document QA, task cards, drafting: built on synthetic data. The third company and the dated provider terms are Duc's decisions |
| Chinese-developed models excluded | built as policy, not yet as code | Provider allowlist is two developers today; the registry with a developer field per route is designed, not built |

## 2. The hand-off document's non-negotiables for Father

| Requirement | Status |
|---|---|
| No training on his content, no casual routing, direct providers | built for training (scripts refuse the tenant); providers and router are policy flags until a Father runtime uses them |
| Voice policy | not started for Father; personal STT is local already |
| Approval before external action | built: approval is gated by QA findings; there is no delivery path at all yet |
| Deterministic QA of the produced file | built: parse every part, render pages (pandoc; Word blocked), match facts |
| Simple daily experience | built on synthetic data: five actions, no provider names on the page |
| Separate tenant, credentials, data | built for data and startup refusal; credentials and hosting are deployment work, not started |

## 3. Tonight's instruction

| Ask | Status |
|---|---|
| Finish the loop for Duc | built to the edge of the deferred decisions: ledger, tiers, readiness, assignments |
| Father | built on synthetic data through drafting; deployment, real content and the third company deferred |
| The headset app and its functionality | partial: orb, card, second opinion and a real reply verified in the simulator; taps blocked by the simulator control; physical headset blocked by the loopback rule (Duc's call) |
| Make it good like a big app | partial: one token set, one header, results first, one status vocabulary across the three pages (P02); the CONSOLE panel (P01). Still separate: the four HUD panel toggles, and the headset's own look |

## 4. What is on the integration branch

50 commits ahead of master, no private path in the diff, 845 Python tests, 31 Swift tests, ruff clean.
Everything Codex built was reviewed and committed by Claude with a real run each time; the verification
history lists them.

## 5. Discrepancies found while reconciling

1. The main checkout holds an untracked copy of `docs/plans/2026-09-15-multi-model-working-loop.md`; the
   tracked file lives on the integration branch. `git merge` in the main checkout will refuse while the
   untracked copy exists. Compare them, then delete the untracked copy before merging.
2. `.claude/worktrees/console-snapshot` is a leftover restored snapshot; delete after a glance.
3. The evening-theme test now reads `web/tokens.css`; any future palette move must update it again.
4. The Codex desktop task `Design multi-model workflow` never resumed; the queued messages from last night
   are still undelivered there. All work ran through `codex exec`.

## 6. Decisions that gate the next slices, in order of leverage

1. Merge (this branch or the four source branches).
2. Automation policy for the loop: which reversible actions may run without a click.
3. Third company and the Father provider terms with a date.
4. Loop routes on the LAN surface for the physical headset.
5. `xcode-select` in a fresh desktop-app session, so the simulator taps can be verified.
