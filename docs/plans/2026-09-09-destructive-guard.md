# Stopping an accidental deletion of the data or the repo (plan, 2026-09-09)

Duc's ask: a hook so that neither agent, nor he, accidentally deletes the databases or the repository.

Owner split under the roles set today: this plan and the failing tests are Claude Code's; the implementation is Codex's.

## 0. What is actually at risk, measured

The irreplaceable state is small and has **no backup at all**:

| Holds | Size |
|---|---|
| `data/memory_db` every conversation since 2026-08-31 | 940K |
| `data/resumes` the base `.tex` every tailored resume is built from | 2.4M |
| `data/private_docs` job-search notes, interview prep, the publish checklist | 880K |
| `data/cover_letters`, `data/digests`, `data/job_documents`, `data/memory_notes` | ~430K |
| six SQLite stores: applications, reminders, learning, outreach, focus, kyra | ~280K |

`~/kyra-fullbackup-2026-09-09.git` and `~/kyra-backup.git` are mirrors of the **repository**, and `data/` is gitignored, so neither contains a single byte of the table above. Everything at risk fits in about **5 MB**, which is the fact that shapes this plan: a snapshot is cheap enough to take often, and it survives a mistake a hook cannot see.

**The most dangerous ordinary-looking command in this repo is `git clean -xdf`.** It removes ignored files, and `git clean -nxd` lists `data/` and `.env` today. It reads like tidying. It is a total loss. Also live: `rm -rf` with a wrong variable or a trailing slash, `sqlite3 ... "DROP TABLE"`, `alembic downgrade`, a `chromadb` collection delete, `git reset --hard` over uncommitted work, and `git push --force` or `branch -D` against the public repo.

## 1. Two layers, and the honest ranking

1. **A snapshot survives a mistake.** It protects against both agents, against Duc, against a bad script, and against a disk problem. Nothing else on this page does that.
2. **A hook blocks a mistake before it happens.** It only sees tool calls made through a harness that has it installed, and it can be bypassed (`--dangerously-bypass-hook-trust` on one side, a disabled hook on the other). It is a seatbelt, not a vault.

So **slice 1 is the snapshot and it ships first**, even though the ask was for a hook. A hook with no backup behind it is one clever `bash -c` away from being worth nothing.

Both harnesses make the hook layer genuinely shared: Codex and Claude Code use the **same hook event names** (`PreToolUse`, `PostToolUse`, `SessionStart`, `Stop`, ...) and the **same wire format**, a JSON event on stdin and a reply carrying `hookSpecificOutput.permissionDecision` of `allow`, `deny` or `ask`. Verified by inspecting the Codex binary's own hook schema. So **one script serves both**, registered twice.

## 2. Slice 1: a snapshot of the 5 MB that cannot be regenerated

- [ ] `scripts/snapshot_data.py`, logic in `src/companion/snapshot.py`. Copies the irreplaceable set (the table in section 0, never `bench_hf`, `local_llm_models`, `router_ft/adapters`, `search_index`, all of which are regenerable and are 170 GB together) into `~/kyra-snapshots/<date-time>/`, hardlinking unchanged files against the previous snapshot so a daily copy costs kilobytes.
      -> verify: two consecutive snapshots of unchanged data occupy one copy on disk (`du -sh` of the second is near zero), and both restore identically.
- [ ] SQLite files are copied with `.backup` through `sqlite3`, never `cp`, because a plain copy of a database being written is a corrupt database.
      -> verify: snapshot a store while a writer holds it open, then open the copy and read the row count.
- [ ] Keep the last 30, delete older. Refuse to delete when the newest snapshot is smaller than half the live set, since that means the live set was already lost and the snapshots are now the only copy.
      -> verify: a test where the live data is emptied asserts rotation refuses and says why.
- [ ] `--restore <snapshot>` writes into a **new** directory and never over `data/`, printing the `rsync`-style command to move it into place. A restore that overwrites is the same failure it exists to fix.
      -> verify: restoring into a temp dir leaves the live `data/` byte-identical.
- [ ] launchd plist beside `deploy/com.kyra.daily-digest.plist`, at 04:30 so it precedes the 05:00 digest.
      -> verify: `launchctl kickstart -k` produces a real snapshot rather than waiting for tomorrow.

## 3. Slice 2: one guard script, two registrations

- [ ] `scripts/guard_destructive.py`: reads the hook JSON on stdin, inspects `tool_name` and `tool_input`, prints a decision. Exits 0 always; the decision is in the JSON, so a crash in the guard can never block ordinary work.
      -> verify: malformed stdin, an empty event and an unknown tool each yield `allow`.
- [ ] **Deny** only what is unambiguous and unrecoverable: `git clean` with `-x` or `-X`; `rm -rf` (or `rm -r`) whose target resolves inside `data/`, the repo root, or `$HOME`; `sqlite3` against a file in `data/` with `DROP`, `DELETE FROM` or `.recover`; `git push --force`/`--force-with-lease` and `git branch -D` against `master` or `origin`; any write to `.env`; `chflags nouchg` or `chmod` on `data/`.
      -> verify: one test per pattern, each asserting `deny` and a message naming the safe alternative.
- [ ] **Ask**, not deny, for the recoverable-but-costly: `git reset --hard`, `git checkout -- .`, `alembic downgrade`, `rm -rf` outside those roots.
      -> verify: each returns `ask`.
- [ ] **Allow everything else, and prove it does.** A guard that fires on safe work gets switched off, and then protects nothing. This repo already records the same lesson about a status that cries wolf.
      -> verify: a corpus of at least 40 real commands taken from this session's own history all return `allow`, including `rm -rf` on a `tmp_path`, `git clean -nxd` (the dry run), and every `pytest`, `ruff` and `git` command used here.
- [ ] The deny message says what to do instead, naming `scripts/snapshot_data.py` and the snapshot directory. A block with no exit is a block someone disables.
      -> verify: every deny message contains an alternative.
- [ ] Register in `.claude/settings.json` under `hooks.PreToolUse` with a `Bash` matcher, and in Codex's hooks file for the same event. One script path, two entries.
      -> verify: a real blocked command in each harness, and a real allowed one, with the transcript kept.

## 4. Slice 3: the layer that needs no software

- [ ] `chflags uchg` on `data/resumes/*.tex` and `data/private_docs/*.md`. These change rarely and are the costliest to lose. macOS then refuses deletion regardless of which agent, which harness, or which typo.
      -> verify: `rm -f` on a locked file fails while the repo still works end to end; document the `nouchg` command to edit one.
- [ ] Record in `docs/log/platform-and-deploy.md` that `data/` has no history and the snapshot is its only one.

## 5. Explicitly not doing

- No `alias rm=rm -i`. It trains a reflex that fails on every other machine and does not touch agent tool calls.
- No blocking of `git commit`, `git merge` or branch creation. Recoverable through the reflog, and blocking them makes the guard hostile.
- No attempt to guard against a deliberate bypass. Both harnesses can disable hooks by design; that is what slice 1 is for.

## 6. Open for Duc, not for an agent

- Where snapshots live. `~/kyra-snapshots` is on the same disk, which does not survive a disk failure. iCloud, an external drive or a private remote is his call, and `data/private_docs` plus `applicant_profile.json` mean anything off-machine is a privacy decision, not just a storage one.
