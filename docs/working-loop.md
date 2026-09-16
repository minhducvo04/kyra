# Personal working loop

Kyra's `/loop` page makes separate calls through the locally installed Codex and Claude Code CLIs. A user can
ask one model for an answer and ask the other to review that exact answer. The personal loop produces text and
review comments, with separate owner decisions. It cannot edit repositories or run tools from the page.

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

The topic list groups the latest 100 saved contributions. Older receipts remain in the database. An ordinary request
starts a fresh conversation. Choosing a topic groups contributions; it does not silently send that topic's history.

## Continuing an answer

Choose **Continue this conversation** on an eligible completed answer. The form names that contribution and keeps
its topic and model fixed. **Send follow-up** resumes its recorded native session; **Start a fresh conversation
instead** returns to a new session. The answer card shows its parent and says when the provider's returned session
id matches the requested one. The receipt keeps both requested and returned ids. The CLI restores that session's
context, which may increase input usage.

Only the newest completed run for that owner/project/topic/provider can be continued. It must be a regular answer,
use the current policy/model/effort, have a valid native identifier, and still match its saved prompt and output
hashes. Older-policy receipts remain readable but cannot continue. One child is reserved per parent in the database,
including under concurrent requests. A failed or uncertain follow-up keeps that reservation; start a fresh conversation
instead of retrying the same parent. A verified child can itself be continued. A session mismatch is shown explicitly
and its answer remains available for inspection.

Reviews start fresh and cannot be resumed. A review of a continued answer includes up to eight prior turns,
oldest first, read from this loop's saved artifacts. Whole oldest turns are removed if needed to fit the prompt
limit; the request and page say when earlier turns were omitted. Missing or changed inspected context or a changed subject request refuses the
review. The receipt records each included turn's id and input/output hashes. A change to any included prompt or
answer stops a queued review and marks a completed review and its owner decisions stale. Omitted context is not
verified; the reviewer is told to flag missing information. Legacy reviews with no recorded context stay labeled. This feature does not import unrelated native app chats or choose a
conversation automatically. It verifies continuity for conversations created through this loop. Do not also resume
these managed sessions outside Kyra while using them here; the controller cannot reserve another application's calls.

Upgrading an earlier loop build requires migrating its existing `loop.db` before starting this version. Back up and
verify its recorded schema first. Startup creates missing tables but does not add columns to old tables. The tested
scratch upgrade matched the slice 2 schema, stamped its previously unversioned database at `f915b18d3e42`, then
upgraded to `a916c29e4f53` with all eight prior receipts preserved. The later context migration
`b916d30f5a64` preserved all fourteen receipts present before that upgrade. Do not stamp a different database by assumption.

## What the receipts prove

The **USAGE** section totals all saved runs for the local owner by provider, developer, model and effort;
**Refresh** reloads the totals. It uses the served model when reported, otherwise the requested model.
Token totals cover known receipts only, with runs without usage counted separately. The only cost shown
is provider-reported USD from saved per-model receipts; no pricing or subscription charge is calculated.

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

## Decisions and reconciliation

A completed, current review has **Approve** and **Reject** controls. These append your decision beside the model's
comment, bound to both answers' hashes. Changing either answer makes the decision stale. A model saying "approved"
does not create a decision. These controls record the local owner's declaration; they do not authenticate a human,
release an artifact, execute work or send another provider request.

For an uncertain run, use **Record what happened** after inspecting its native session. Choose **Nothing happened**
or **Provider processed it** and add a short private note. This is your declaration, not proof of provider effects.
An unreconciled run is eligible immediately; a stuck dispatch becomes eligible after its timeout plus 30 seconds.
The original status and receipt stay unchanged. The page shows the notes and their timestamps. A new request may
use subscription allowance again; the old run cannot be dispatched again.

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

`data/loop.db` stores metadata; `data/working_loop/<id>/` holds private prompts, answers and reconciliation notes. They follow `KYRA_DATA_DIR`
and are gitignored. The UI is personal and loopback-only even when the wider Kyra app has a LAN token. It rejects
foreign Origins and non-loopback Host names. Do not forward this endpoint through a local reverse proxy. This is
not a multi-user privacy boundary and does not protect files from another process running as the same OS user.

The existing job queue carries only the run id. Raw CLI output is parsed in memory rather than copied into the jobs
log. Nothing from this workflow enters the existing companion memory or local classifier.

## Build and review

See [the build plan](plans/2026-09-15-working-loop-build.md), [owner decisions](plans/2026-09-15-working-loop-decisions.md) and [the broader design](plans/2026-09-15-multi-model-working-loop.md).
Claude Fable High wrote the acceptance suite; Codex reviewed that contract and implemented it; Claude independently
reviews implementation and adds regression tests. Private real-run evidence and the native Claude conversation
reference live under `data/verifications/working-loop/` in the isolated working-loop worktree.

Third-company integration, automatic planning/execution stages, comparative cost routing, shared memory and Father's
interface remain later slices. None is represented as active by this page.
