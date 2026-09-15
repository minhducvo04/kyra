# Personal working loop

Kyra's `/loop` page makes separate calls through the locally installed Codex and Claude Code CLIs. A user can
ask one model for an answer and ask the other to review that exact answer. This first slice produces text and
review comments. It cannot edit repositories or run tools from the page.

## Use it

Start Kyra with the normal `scripts/web_ui.py` entry point and open **LOOP** in the header. Use a loopback URL
such as `http://127.0.0.1:8420/loop` on the same Mac. Both official CLIs must already be signed in. The app
finds installed binaries on PATH or in their macOS vendor application locations. Server-side overrides are
`KYRA_CLAUDE_CLI_PATH`, `KYRA_CODEX_CLI_PATH`, and `KYRA_LOOP_TIMEOUT_SECONDS` (600 by default, maximum 1800).

1. Choose an existing topic, or name a new one.
2. Choose Codex Astra High or Claude Fable 5.1 High and enter the request.
3. Read the contribution when it completes. Expand **Request sent** or **Execution receipt** for evidence.
4. Select **Ask Claude to review** or **Ask Codex to review**. The other company receives the original request,
   exact answer and its SHA-256 hash. A completed review appears in the topic and is attached as a comment.

The topic list groups the latest 100 saved contributions. Older receipts remain in the database. Each dispatch starts a fresh CLI session; automatic native session
resume and automatic inclusion of older topic messages are not implemented. The store has an owner/project/topic/provider
lookup for future controlled resume. Be explicit about necessary context in each new request.

## What the receipts prove

- The controller selected an exact approved provider/model/effort and launched its configured CLI. Model prose
  cannot create another run or a review receipt. Only OpenAI and Anthropic are approved in this slice.
- Claude's served-model field comes from its actual assistant event. Codex's current JSON stream does not report
  the served model, so it remains unknown. Requested configuration is not relabeled as observed identity.
- Usage comes from terminal protocol events. Claude's auxiliary model usage remains separately reported; it does
  not count as another independent reviewer. Subscription cost is not treated as zero. Provider `costUSD`, where
  supplied, is a list-price estimate, not an invoice or marginal subscription charge.
- Both provider sessions must complete, be from different developers and match the review's bound artifact hash.
  An unrelated completed run cannot review an artifact it never received. Editing an artifact makes old reviews stale.
- Reviews are comments. The software never turns a model's confident prose into a human approval or guarantee.

Full private reasoning is not available. Ask for explanations, assumptions and evidence in the response. The page
shows those responses without claiming they are the model's internal thought process.

## Failure and data handling

A conditional database update claims each queued run once before execution. Model tools, plugins and external tool
servers are disabled; prompts travel on standard input in an empty scratch directory. A process has a timeout and
an output limit, and cleanup terminates its process group. The child environment retains OS account identity for
macOS Keychain sign-in, but excludes inherited API keys and alternative provider endpoints. Codex uses a dedicated private state directory at
`data/working_loop/codex-state/`; its `auth.json` is a symlink to the existing `~/.codex/auth.json`, not a copy.
The normal Codex app's configuration and global AGENTS.md are not loaded. The installed CLI creates its own bundled
system skills and native session files there. Those sessions are inspected through Kyra's receipts and private
state directory; they are not claimed to appear automatically in the main Codex app sidebar.

In the verified Codex version, an advertised code-mode entry point can still be attempted, but its host is disabled
and returns an error before executing code. A live tool-demand probe and native trace confirmed that behavior.
This is capability isolation, not a promise that every tool name disappears from the model's context.

`failed` means an explicit failure was observed. `mismatch` means a reported served model differed from the requested
one. `unreconciled` means execution began but completion could not be confirmed. None is retried automatically. A
hard server termination can leave a run in `dispatching`; inspect that receipt's start time and native session before
making any new request. Restart does not claim an existing `dispatching` run again.

`data/loop.db` stores metadata; `data/working_loop/<id>/` holds private prompts and answers. They follow `KYRA_DATA_DIR`
and are gitignored. The UI is personal and loopback-only even when the wider Kyra app has a LAN token. It rejects
foreign Origins and non-loopback Host names. Do not forward this endpoint through a local reverse proxy. This is
not a multi-user privacy boundary and does not protect files from another process running as the same OS user.

The existing job queue carries only the run id. Raw CLI output is parsed in memory rather than copied into the jobs
log. Nothing from this workflow enters the existing companion memory or local classifier.

## Build and review

See [the build plan](plans/2026-09-15-working-loop-build.md) and [the broader design](plans/2026-09-15-multi-model-working-loop.md).
Claude Fable High wrote the acceptance suite; Codex reviewed that contract and implemented it; Claude independently
reviews implementation and adds regression tests. Private real-run evidence and the native Claude conversation
reference live under `data/verifications/working-loop/` in the isolated working-loop worktree.

Third-company integration, automatic planning/execution stages, comparative cost routing, shared memory and Father's
interface remain later slices. None is represented as active by this page.
