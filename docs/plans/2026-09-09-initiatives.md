# Initiatives: Kyra proposes, with receipts, and never acts (plan, 2026-09-09, revised the same night)

Claude Code's plan; Codex critiqued the first version and four of its blockers reshaped this one (see the log
entry in `docs/log/digest.md` when the slice lands). The gap it closes: Kyra delivers a digest, reminders and a
board watch every morning, and `patterns.py` counts repeated tool use, but nothing in Kyra proposes a next step
with reasons and waits for a yes. Read `docs/log/digest.md` and `docs/agentic-roadmap.md` ("Usage-pattern
observation") first.

## Boundary, set by decision

An initiative is proposed, never executed. Accepting one (section 2) creates a reminder and nothing else. No
initiative ever opens a pull request, runs a script, sends a message, or changes a file. This joins the existing
list (autofill never submits, handoff never spawns) and is enforced in code, not in a prompt.

What the evidence check proves, stated narrowly: the cited records exist in the bundle the model was shown. It
does not prove their content supports the idea; that is why every initiative carries its evidence lines for the
owner to read. The destructive-verb list is a filter, not the boundary.

## Section 1, build first: `suggest_initiatives`, on demand, no persistence

Small enough to build in one session and use for a week before anything is stored or scheduled.

- `src/companion/initiatives.py`:
  - `Evidence(id, source, quote, when)`: one line of fact with where it came from.
  - `InitiativeSource` (ABC, one method `collect() -> list[Evidence]`) with four concrete sources, all live and
    all on master: overdue and due-today reminders (`RemindersStore`), memory notes in the `projects` category
    (`MarkdownMemoryNotesStore`), `patterns.find_patterns` hits (counting only, no observation call), and the
    last ten commits of the repository (`subprocess`, read only; an empty list outside a git repository).
  - `Initiative(title, why, first_step, minutes, evidence_ids, status="proposed")`.
  - `propose(evidence, llm) -> list[Initiative]`: one `LLMBackend.respond()` call over the whole bundle; the
    prompt shows every evidence id and quote and demands ids per item; a reply that does not parse returns `[]`
    and logs at WARNING.
  - `guard(initiatives, evidence) -> list[Initiative]`: the post-condition. Drops any initiative that cites no
    evidence id, cites an id not in the bundle, or whose `first_step` contains a destructive verb (delete, push,
    send, submit, pay, rm, force; whole words, case-insensitive). Logs every drop with the reason. Caps at three.
    Pure: a new list, inputs untouched.
  - `SuggestInitiativesTool(sources, llm)` on the `Tool` ABC, name `suggest_initiatives`. `run()` collects,
    abstains with `{"initiatives": [], "abstained": true, "reason": "no evidence"}` and no model call on an empty
    bundle, otherwise proposes, guards, and returns JSON-serialisable initiatives each carrying its evidence
    lines (source, quote, when). One model call per run. No cache, no store, no digest, no UI.
- `default_tools.py` registers it with the real stores and the shared Claude backend; the stores are constructed
  lazily so the registry still builds without heavy imports.
- Router: already measured on ten handwritten cases (`tests/data/router_testset_initiatives.jsonl`): 70 percent,
  80 percent path-only, three of five asks already reach the tool path. No retrain; see `docs/log/router.md`.

Steps:
1. `tests/test_initiatives.py` is written and red (guard, propose, tool) -> verify: it fails at import before the
   module exists and passes after; `ruff check` clean.
2. `Evidence`, `InitiativeSource`, the four sources -> verify: each returns an empty list on an empty scratch
   `KYRA_DATA_DIR` and the expected lines on a seeded one; `tests/test_startup_cost.py` covers
   `companion.initiatives` and stays green.
3. `propose()` and `guard()` -> verify: the unit tests pass; one real call over a seeded bundle returns at most
   three items all surviving the guard, raw reply kept in the verification log.
4. The tool and the registry -> verify: `default_tool_registry()` builds with no heavy import; the schema passes
   through `ToolRegistry.schemas()`; a real tool-path turn in `chat.py` against a scratch `KYRA_DATA_DIR` seeded
   with two reminders and a project note lists initiatives with their evidence, and an empty scratch store makes
   it abstain. Record both at the top of `docs/log/digest.md` and in `docs/log/verification-history.md`.

Out of scope for section 1, deliberately: any write, any schedule, any second model call, any UI.

## Section 2, after section 1 has been used: daily run, store, digest section, Today tab

Not implementation-ready yet; the contracts below answer the blockers Codex found, and
`tests/test_initiatives_daily.py` holds the tests, red until this section starts.

- `run_daily(day, *, llm, sources, cache_dir, write=True)`: cached at `<cache_dir>/<day>.json`; same day is served
  from the file with no model call; a new day with an unchanged bundle hash makes no call and returns the previous
  list; a changed bundle makes one call. `write=False` is what `daily_digest.py --dry-run` uses: it reads a cache
  if one exists and never generates, because dry-run is read-only by contract.
- Sources added here: the branch hand-off threads (`session_log.read`), which is not on master yet; it arrives when the shared-agent branch merges.
  The "previous day's digest actions" source is dropped: the archive is stale the moment a reminder is completed,
  and the live reminders source already covers it.
- `InitiativeStore` over `schema.py` (new table `initiatives`, Alembic revision) with `status`, `reason` (for
  dismissals, because a dismissal alone is ambiguous) and `reminder_id`. **One completion authority:** an accepted
  initiative is represented by its reminder from then on; the digest section shows proposed items only and counts
  nothing twice. **Idempotent accept across two stores:** `accept()` first looks for a reminder already carrying
  this initiative's id, creates one only if none exists, then flips status; a crash between the two writes leaves a
  state a retry completes without a duplicate.
- Digest: a "Kyra suggests" section in `DigestData` (JSON round trip included).
- Two endpoints in `webapp.py` (`POST /api/initiatives/<id>/accept`, `.../dismiss` with optional `reason`) using
  `ApiError` on a bad id; a third row type in the visionOS Today tab and the web HUD showing title, first step and
  evidence lines.
- The "say nothing" class stays out until there are weeks of labels with reasons.

Steps for section 2 are written when section 1 has been used for a week and the tool's output has been read.
