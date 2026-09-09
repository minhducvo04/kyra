# Codex joins the project, and the repo gets navigable (2026-09-09)

Duc's ask: the repo is public now; add Codex beside Claude Code; clean everything up so it is easy to navigate (nothing deleted); write down, for Codex and for him, where instructions and data live; keep private things out of the public repo by gitignore and tell Codex that explicitly.

## 0. What was done in this session

| Step | Result |
|---|---|
| `CLAUDE.md` (200 KB, 286 lines, one paragraph per line) split | `AGENTS.md` (21.6 KB, the rulebook every agent reads) + `docs/log/` (15 topic files, every dated entry **verbatim**) + a thin `CLAUDE.md` that imports `AGENTS.md` and keeps only the Claude-specific parts. |
| Indexes | `docs/README.md` (every doc, benchmark and plan with a status) and `docs/log/README.md` (which log file covers which subsystem). |
| `README.md` | Layout and links point at `AGENTS.md` and `docs/log/`; test count re-read from pytest's summary (493). |
| `.gitignore` | Added `.env.*` (keeping `.env.example`), `*.pem`, `*.p12`, `scratchpad/`. `/data/`, `handoff/`, `.env`, Terraform state and personal-document extensions were already covered. |
| Search index | `search.py` now indexes `docs/log/*.md` and `AGENTS.md`; `tests/data/search_testset.jsonl` gained the log file for each query whose passage moved. |
| Stale notes | `ATTRIBUTION.md` no longer says the licence is open; the going-public plan carries a status line; the workflow template says AGENTS.md. |
| Privacy re-verified on the public history | 0 hits for every third-party identifier in all blobs and commit messages; 0 tracked files ever contained the owner's email; 0 `refs/pull/*`; local `master` equals `origin/master`; `test_no_third_party_pii` green. |

-> verify: every non-blank line of the old `CLAUDE.md` appears verbatim in the union of the new files, except the lines rewritten on purpose (headings, the commands list, the workflow). The check is in section 5.

## 1. How the two agents share one brain

- `AGENTS.md` is canonical. Codex reads it natively from the repo root. Claude Code reads it through `CLAUDE.md`, whose first line is `@AGENTS.md`.
- Anything both agents need goes in `AGENTS.md`. Anything only Claude Code needs (skills, model table, launch config) goes in `CLAUDE.md`. History goes in `docs/log/<topic>.md`, newest entry at the top. Plans go in `docs/plans/`.
- `AGENTS.md` stays under 32 KB: Codex's default `project_doc_max_bytes` truncates past that, silently. A rule that gets truncated is a rule nobody follows.
- Codex has no memory directory. Claude Code's auto-memory (outside the repo) is personal to it; a fact the project needs must be in `docs/log/`, or Codex never learns it.

## 2. Setting Codex up for this repo (Duc)

Codex is already installed: the ChatGPT desktop app bundles `codex-cli 0.153.4` at `/Applications/ChatGPT.app/Contents/Resources/codex`, and `~/.codex/config.toml` exists (plugins and the computer-use MCP server, nothing repo-specific). Nothing in the repo needs configuring; `AGENTS.md` is picked up on its own.

1. **Desktop app**: add `~/Projects/kyra` as a project. Or the CLI:
   ```bash
   echo 'alias codex="/Applications/ChatGPT.app/Contents/Resources/codex"' >> ~/.zshrc
   ```
   then `codex` from `~/Projects/kyra`.
2. **Sandbox and approvals.** Keep the default workspace-write sandbox and per-action approvals. Do not run Codex with full access in this repo: the working tree holds `.env` and `data/`. The test suite needs no network, so it runs inside the sandbox; real-run verification (Anthropic API, job boards, RSS) needs network, so approve it per run. If a config key name has moved, `codex --help` and the config reference are the source of truth.
3. **Optional global instructions.** Claude Code has `~/.claude/CLAUDE.md` with the global principles and the session-end line. The Codex equivalent is `~/.codex/AGENTS.md`, which it prepends to every project. If you want the same habits from both, put this in it:
   ```
   - Think before coding: state assumptions, surface tradeoffs, ask when confused instead of guessing.
   - Simplicity first, surgical changes: the minimum code for the stated problem; match existing style.
   - Goal-driven: turn "fix the bug" into "write a failing test, then make it pass."
   - Hard constraints live in code: a prompt is a request, a post-condition check is a guarantee.
   - Verify with a real run before calling anything done, and keep the proof.
   - Never print secrets. For .env diagnostics, check length and prefix only.
   - End every substantive reply with one line: Finished: <part>. Next: <part>.
   ```
4. **Prove it loaded.** Open Codex in the repo and ask: "What must never be committed here, and where do private files live?" The answer should name `data/`, `.env`, `handoff/`, the fictional contact, and the PII test. If it does not, `AGENTS.md` was not read; check the working directory.

-> verify: done 2026-09-09 with the bundled CLI, `codex exec --sandbox read-only`, asked to answer from project instructions alone: it named `data/`, Alex Rivera and `tests/test_no_third_party_pii.py`. `AGENTS.md` is loaded from the repo root with no configuration.

## 3. First prompt to paste into Codex

```
You are working in ~/Projects/kyra, a public GitHub repository. Read AGENTS.md in full before doing
anything; it is the rulebook for every agent here (Claude Code and you). Then read docs/README.md and
docs/log/README.md so you know where the design docs and the engineering log are.

Non-negotiable: this repo is public and the owner's real job search runs through it. Everything under
data/ and the .env file is private and gitignored (conversations, resumes, the applicant profile with
phone and email, outreach.db with real people's names, private_docs/). Never copy any of that into a
tracked file, never print .env, never git add -f, never push, never rewrite history, never change the
repo's visibility. The only contact for examples is the fictional Alex Rivera at Northwind.
tests/test_no_third_party_pii.py enforces part of this; the rest is you.

When I give you a task: orient first (git status, git log -5, and the docs/log topic file for the
subsystem, per AGENTS.md section 7), state your assumptions, work on a branch named
session/<date>-<topic>, write the test first for any hard constraint, verify with a real run where one
applies, then record what you learned at the top of the matching docs/log/<topic>.md and prepend a
dated entry to docs/log/verification-history.md. Finish with `ruff check src scripts tests` and
`python3 -m pytest` green, and `git status --porcelain` showing nothing under data/. Commit locally
with a message that says what was verified; I push.

Start by telling me, in a few lines, what you understood: where private data lives, which files you
would read before touching the router, and three things you would never do in this repo.
```

## 4. What only Duc can do

1. **Merge and push** after reading the diff. The work is on `session/2026-09-09-codex-and-cleanup`, branched from `master`'s tip, so it fast-forwards: `git checkout master && git merge --ff-only session/2026-09-09-codex-and-cleanup && git push origin master`. Nothing was pushed from this session.
2. **Decide about the commit author email.** 196 of the 199 public commits carry your Gmail address as the git author (the other 3 use the GitHub noreply address). It appears in no file content. GitHub shows author emails to anyone who clones. Options: leave it; or set the repo-local identity to the noreply address for future commits (`git config user.email "<id>+minhducvo04@users.noreply.github.com"`). A rewrite of the past ones would change every hash again and is not worth it.
3. **The two disclosure judgment calls** from the publish checklist §7 are still yours: the one company name that appears across tracked files, and how much job-search detail the log reveals. With the split, the second one is concentrated in `docs/log/autofill-and-boards.md` and `docs/log/resume.md`, so the decision is now about two files rather than one 200 KB wall.
4. **Delete the three orphaned worktree directories** under `.claude/worktrees/` (7.5 MB, branches fully pushed to the archive, nothing else in them).
5. Whether Codex gets the global `~/.codex/AGENTS.md` from section 2.

## 5. Verification of this restructure

- Lost-lines check: for every non-blank line of the pre-split `CLAUDE.md` (`git show HEAD:CLAUDE.md`), assert it appears verbatim in `AGENTS.md`, `CLAUDE.md` or some `docs/log/*.md`; print the exceptions and confirm each is a heading or a deliberate rewrite. -> verify: the exception list is exactly the section headings, the two intro lines, and the session-workflow block (all rewritten in `AGENTS.md`); the architecture and working-practices blocks are kept verbatim in `docs/log/rules-history.md` beside their condensed forms.
- `python3 -m pytest` and `ruff check src scripts tests` green. -> verify: 493 passed.
- `wc -c AGENTS.md` under 32,768. -> verify: 21,631.
- `git status --porcelain` shows no path under `data/`, no `.env`, no personal-document extension. -> verify: run before the commit.
- `python3 scripts/search.py --reindex` then `--eval`: the eight moved passages are found in their log files. -> verify: done; lexical unchanged at 88.5%, vector up to 73.1%, hybrid down one query to 76.9% because the split made one test case stricter (recorded in `docs/search-eval.md` and `docs/log/search.md`, deliberately not tuned).
