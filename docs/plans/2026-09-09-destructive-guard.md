# Stopping an accidental deletion of the data or the repo (deferred plan, 2026-09-09)

Duc's ask: a hook so that neither agent, nor he, accidentally deletes the databases or the repository.

Owner split under the roles set today: this plan and the failing tests are Claude Code's; the implementation is Codex's.

**Status:** snapshot slice implemented, independently approved, and live backup/restore
and 04:30 launchd kickstart verified on 2026-09-10. Merge and switching the installed
job from the reviewed worktree to the main entry point are pending. Hook and filesystem-lock slices remain
deferred. No hook is installed in Claude Code or Codex. The shared instructions and
skills setup is complete without hooks. If the hook slice resumes, verify each harness against its
current official documentation before assuming one event schema or one response format works in
both; binary inspection alone is not a supported integration contract.

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

The intended design is one repository guard script with separate harness registrations. That is a
design constraint, not a verified capability: no registration exists today, and the earlier claim
of a shared event schema came from binary inspection rather than supported documentation. Treat
the two integrations independently until a real allowed and blocked command has been verified in
each harness.

## 2. Slice 1: a snapshot of the 5 MB that cannot be regenerated

- [x] `scripts/snapshot_data.py`, logic in `src/companion/snapshot.py`. Copies retained data into `~/kyra-snapshots/<date-time>/`, excluding top-level regenerable directories: `bench_hf`, `local_llm_models`, `router_ft`, `search_index`, `generated_resumes`, `voice_models`, and `tool_ft`. Unchanged ordinary files share hardlinks with the previous snapshot; databases always get a fresh backup.
      -> verify: two consecutive snapshots of unchanged data occupy one copy on disk (`du -sh` of the second is near zero), and both restore identically.
- [x] SQLite files are recognized by their header and copied with Python's `sqlite3.Connection.backup()`, never a plain file copy, so committed WAL rows are included. Database sidecars are omitted.
      -> verify: snapshot a store while a writer holds it open, then open the copy and read the row count.
- [x] Keep the last 30, delete older only after publishing a completed copy. Refuse to publish or rotate when the captured live set is smaller than half the newest snapshot, since that may mean the live set was already lost and the snapshots are now the only copy. This corrects the reversed comparison in the original plan, following Claude's regression and handoff.
      -> verify: a test where the live data is emptied asserts rotation refuses and says why.
- [x] `--restore <snapshot> --into <new-directory>` writes into a **new** directory and never over existing live data, printing an `rsync -avn` dry-run command for manual inspection. Restored files are independent copies, not hardlinks to snapshots.
      -> verify: restoring into a temp dir leaves the live `data/` byte-identical.
- [x] launchd plist beside `deploy/com.kyra.daily-digest.plist`, at 04:30 so it precedes the 05:00 digest.
      -> verify: `launchctl kickstart -k` produces a real snapshot rather than waiting for tomorrow.

Implementation verification (2026-09-10): 11 snapshot tests pass, including Claude's
seven and four failure/destination checks. Full snapshot-worktree suite: 548 passed,
1 existing optional tokenizer skip; ruff and plist syntax clean. Two live snapshots
captured 339 files. The first used 44 MB; the second added 1.2 MB for eight fresh
SQLite copies and shared 331 unchanged files. All eight databases passed integrity
checks. Both temporary restores matched every captured file's SHA-256 and used
independent inodes; temporary restores were removed. Evidence lives at
`data/verifications/2026-09-10-snapshot/`. A root lock prevents overlapping runs;
partial copies are hidden until complete, and symlinks are refused. This is a
per-database consistent backup, not a transaction across stores or Chroma's SQLite
and separate index files. Same-disk storage still does not cover disk failure.

Review follow-up: independent review reproduced changed bytes retaining the same
size/mtime, restore destinations nested within live data or saved snapshots, and
macOS `uchg` flags preventing later hardlinks/rotation. Each received a failing
regression before its fix. Hardlink reuse now also requires equal SHA-256; the CLI
refuses restores under live data, restore refuses all snapshot storage, and copied
files have BSD flags cleared without changing live-file protection. Fifteen
focused tests pass, including a real macOS protected-file snapshot/restore/rotation
check. A repeated live run after content verification captured 341 files, shared
333, freshly backed up eight databases, and restored every file identically twice.
Evidence: `data/verifications/2026-09-10-snapshot/real-run-after-review.json`.

Reviewed (independent Codex reviewer, 2026-09-10): implementation diff over
`b360a99`, approve, zero remaining blockers. Independently reran the 15 snapshot
tests, lint, plist validation, preserved-metadata content reproduction, restore
isolation, and macOS immutable-file rotation/restore. Nonblocking suggestions:
preserve harmless BSD flags instead of clearing all flags on copies, and assert
the plist's existing `Umask=63` in its test. Both are deferred; backup content and
live-file protection are verified.

Final premerge suite after all review fixes: **552 passed, 1 existing optional
tokenizer skip in 66.67s**, ruff clean. This worktree does not yet contain the
eleven wake-up tests already merged on master.

Scheduled-job verification: installed `com.kyra.snapshot` with `Umask=63`,
kickstarted it, observed exit 0, and restored its 345 captured files identically;
all eight SQLite copies passed integrity checks. Evidence:
`data/verifications/2026-09-10-snapshot/launchd-worktree.json`. Another agent owns
an in-progress initiatives merge in the shared checkout, so the installed job
temporarily uses this reviewed worktree with `KYRA_DATA_DIR` set to the main data
directory. After merging, install the tracked plist unchanged and repeat kickstart
to verify the main entry point. No other agent's merge was altered.

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
