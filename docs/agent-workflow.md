# Two-agent workflow: who does what, with which model

Claude Code and Codex both work in this repo. This is the division of labour, the testing assignments, and the models each side runs. `AGENTS.md` is the rulebook both read; this file is the rota. When the two disagree, `AGENTS.md` wins.

## 1. Three principles that make two agents safe

1. **One writer per branch at a time.** An agent owns a `session/<date>-<topic>` branch from the moment it starts until it posts a handoff block (section 5). The other agent reads, reviews and runs tests on that branch, but does not edit it. Two sessions once built the same slice in parallel and the merge dropped a whole entry; the diff rule in `AGENTS.md` section 5 exists because of that.
2. **The author never reviews its own work.** Whoever built a slice hands it to the other agent for review and independent verification. A spot check written by the author tests the author's assumptions.
3. **Shared state lives in git and in the plan file, never in either agent's memory.** Claude Code has an auto-memory directory; Codex has none. Anything the next session needs goes in `docs/plans/<date>-<topic>.md`, `docs/log/<topic>.md`, or the commit message.

## 2. The loop, phase by phase

| Phase | Owner | Model / effort | Produces | Handoff signal |
|---|---|---|---|---|
| **Plan** | Claude Code | Opus 5 / high (run `grill-me` first) | `docs/plans/<date>-<topic>.md` with `[step] -> verify: [check]` lines and explicit assumptions | Plan file committed on `master` or the session branch |
| **Plan critique** | Codex | default model, reasoning **high** | Comments appended to the plan under `## Critique (Codex)`: missing verify lines, hidden assumptions, cheaper alternatives | Critique section present; Claude Code answers each point inline |
| **Tests first** | Codex | default model, reasoning **medium** | Failing tests, one per `verify:` line that can be a unit test, in `tests/`; commit named `tests(red): <topic>` | Red commit on the session branch; `pytest` output pasted in the handoff block |
| **Build** | Claude Code | Sonnet 5 / medium (`ponytail` on; Opus 5 / high only when stuck) | The minimum code that turns the red tests green, matching existing style | Green commit, `ruff` clean, `pytest` count from the summary line |
| **Real-run verification** | Builder, then confirmed by the other side (section 3) | as above | Proof kept: log line, screenshot, output file, named in the commit message | Entry prepended to `docs/log/verification-history.md` |
| **Review** | The non-author (Codex reviews Claude's build; Claude reviews Codex's build) | Codex: reasoning **high**. Claude Code: Opus 5 / high with `code-review-and-quality`, `ponytail-review` | Findings as a list in the handoff block, each with file:line; blocking ones fixed by the author before merge | Reviewer writes `Reviewed: <commit>, <n> findings, <m> blocking` in the plan |
| **Record** | Builder | same model as build | Dated entry at the top of the matching `docs/log/<topic>.md` with the *why*; `docs/industry-standards.md` row if a standard changed; `data/private_docs/needs-your-input.md` for anything only Duc decides | Reviewer confirms the entry says what was verified, not what was hoped |
| **Merge and push** | Duc | | | Fast-forward or PR; nothing is pushed by an agent |

Default builder is Claude Code because its harness has the browser pane, the iOS/visionOS simulator, and the auto-memory. Codex builds instead when all three hold: the slice is fully specified with red tests already written, it is pure Python or shell (a script, a store, a parser, a CLI), and it needs no simulator. Then the roles flip for that slice and Claude Code reviews.

## 3. Testing assignments

| Kind of test | Who writes it | Who runs it, and when | Where the result goes |
|---|---|---|---|
| Hermetic unit and API tests (`pytest`, `tests/conftest.py` temp data dir) | Codex, from the plan's `verify:` lines, before the build | Both, before every commit; CI on push | Count from pytest's summary line in the commit message |
| Post-condition guards (a check in code for a hard constraint: no invented numbers, no dash, no file in a focus condition, startup imports, PII) | Claude Code during build, as the first thing built for any hard constraint | Both, as part of the suite | The guard's docstring names the failure it was written after |
| Lint (`ruff check src scripts tests`) | n/a | Both, before every commit | Commit only when clean |
| Real run: Anthropic API, LaTeX compile, RSS and board fetches, search reindex and eval | Builder | Builder first; the reviewer re-runs the cheap ones (search eval, board watch, `--dry-run` digest) to reproduce the numbers independently | Top of `docs/log/verification-history.md`; numbers in the matching `docs/*.md` eval doc |
| Real run: browser HUD (`scripts/web_ui.py`) | Claude Code (browser pane) or Codex (browser plugin), whichever built it | The other side reproduces one path end to end | Screenshot or `read_page` output named in the commit |
| Real run: visionOS client | Claude Code only (simulator tool) | Claude Code | `docs/log/visionos.md` |
| Evals with held-out sets (`router_ft.py eval`, `tool_ft.py eval`, `search.py --eval`, `focus_report.py`) | Held-out cases are handwritten by the builder and committed **before** any data is generated; the reviewer adds at least three cases the builder did not see | Builder runs; reviewer re-runs on a fresh index or a second seed | The eval doc under `docs/`, with the noise floor stated |
| Test-pollution cleanup (`data/*.db`, `data/memory_db`, `data/router.log`, `data/memory_notes/`) | n/a | Whoever ran the real run, immediately after; the reviewer checks `git status` and the store counts | Named in the verification entry |
| Private-data check before commit (`git status --porcelain`: nothing under `data/`, no `.env*` but the example, no personal-document extension) | n/a | Both, every commit | Silent when clean; a hit is a blocking review finding |

Two rules that apply to every row: read the count off pytest's **summary line**, never the dots (a session once reported 1 to 4 high from the dots); and a test that has quietly stopped testing its own name is worse than no test, so a reviewer's first question about any new test is "what change would make this fail?".

## 4. Models per side

**Claude Code**

| Task | Model | Effort |
|---|---|---|
| Plan, design, audits, hard debugging, prompt work needing real-run verification, review of a risky diff | Opus 5 | high |
| Implementing a specified slice, wiring, docs, refactors covered by tests | Sonnet 5 | medium |
| Trivial edits, questions answerable from one file | Sonnet 5 | low |

**Codex** (the configured default model; `gpt-6-astra` at the time of writing, visible in the banner of every `codex exec` run)

| Task | Reasoning effort |
|---|---|
| Plan critique, code review, security review of anything touching auth, autofill or the API key | high |
| Writing red tests from a plan, building a fully specified pure-Python slice, re-running evals | medium |
| Trivial edits, one-file questions | low |

Set effort with `/model` in the Codex TUI, or per profile in `~/.codex/config.toml`; if a key has moved, `codex --help` is the source of truth. Keep the default workspace-write sandbox and approve network per run; never full access in this repo.

## 5. The handoff, as a file rather than a copy-paste

Both agents run on the same machine, so a directory is the channel and nothing needs copying between two chats. `data/sessions/<branch>.md` is the thread: one file per branch, because the branch is already the unit of work. It is append-only and gitignored, since a session record names test counts and what a real run wrote.

Starting work, whichever agent you are:

```bash
python3 scripts/session_log.py            # this branch's thread, oldest block first
python3 scripts/session_log.py --list     # every thread, newest first, with what each is open for
```

Finishing, before you stop:

```bash
python3 scripts/session_log.py --write --agent claude --open-for review \
    --next "wire the engine into apply_pipeline" --suggest "Sonnet 5 / medium" <<'EOF'
Done: steps 1 to 3 of the plan.
Verified: pytest 494 passed, ruff clean, one real Lever form filled headless with fake data.
Not done: step 4, waiting on a real posting URL.
Pollution: hermetic only.
To Codex: the required-field union lives in `_is_required`; reading one board's marking alone
calls every field on the other two optional.
EOF
```

`--agent` is one of claude, codex, duc. `--open-for` is one of critique, tests, build, review, duc, nothing, and it is validated, because a typo there misroutes the next session silently. The branch, head commit, count of commits ahead of master, working-tree state and the private-path check are measured from git and written into the block for you. Do not type them: a hand-off whose facts are recalled is one the receiver has to re-check anyway, which is the copying problem again in a smaller form.

**Every block ends with three things, and none of them is optional in practice.** `--next` is the one line saying what comes next, so the receiver does not re-derive the plan. `--suggest` is the model and effort for that next piece, in the vocabulary of whoever picks it up: Opus 5 or Sonnet 5 with an effort for Claude Code, a reasoning level for Codex, and say which agent when it is not obvious. Both render as `(not stated)` when left out, so a missing hand-over is visible in the thread rather than being something a reader has to notice is absent. The third is the body itself, which **is** the message to whoever `--open-for` names: write it to them, not about them, and open a line with `To Codex:` or `To Claude:` for anything they specifically need to know, such as a trap you hit, a decision you made that they might undo, or a question you want answered. When the block is for Duc rather than the other agent, set `--open-for duc` and address him.

The receiving agent's first action is to read the thread, then `git checkout <branch> && git log -3 && python3 -m pytest`, and its first line back is whether those match the last block.

## 6. A slice through the loop

1. Duc asks for a Lever engine for the digest's `needs_attention` reasons.
2. Claude Code (Opus 5 / high) grills the ask, writes `docs/plans/2026-09-10-lever-reasons.md` with four steps and their `verify:` lines, commits it.
3. Codex (high) appends a critique: step 3 has no verify line for the empty-profile case. Claude Code adds it.
4. Codex (medium) writes three failing tests on `session/2026-09-10-lever-reasons`, commits `tests(red): lever reasons`, posts a handoff block: `Open for Claude Code: build`.
5. Claude Code (Sonnet 5 / medium) makes them green, runs a real Lever form headless with placeholder data, records the proof, prepends the verification entry, posts a handoff: `Open for Codex: review`.
6. Codex (high) reviews, finds one blocking issue (a required question silently skipped), Claude Code fixes it, Codex re-reviews and writes `Reviewed: <sha>, 2 findings, 0 blocking`.
7. Claude Code writes the `docs/log/autofill-and-boards.md` entry. Duc merges and pushes.
