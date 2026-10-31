# Agent team

**Proof.** From a fresh install, the test command below gives **2132 passed, 23 skipped**. In those tests `Dag` refuses a cycle and names its path, a dispatcher tick parks a refused brief until its backoff ends, a dead launch is closed, its claim freed and the node requeued, and a Team reply with family or health wording is held and never sent.

**What it does.** This is Kyra's agent team, from building blocks to the loop that runs them. A feature is a graph of nodes, a picker ranks the AI coding agents that run as CLI processes ("lanes"), the dispatcher hands each ready node to a free lane, a node worker sends the brief and collects the result, and an external verifier decides whether the node is done. The Team chat is where the owner posts a request: a planner interviews the owner, splits the work and fans it out to members. Anything the team cannot settle goes to the Board inbox. A separate audit layer records what agents did, as metadata only.

**Key idea.** Each handoff passes a schema check and has a named owner. `contract` validates briefs, results and events, a file lease or a claim records who holds each piece of work, only `verify` marks a node done, and work still needs a Board review before it can land.

## How it works

- **Feature DAG.** `dag.Dag` loads nodes in file order and rejects cycles with `graphlib.TopologicalSorter`. Each node names its inputs, its tier and its acceptance lines. In strict mode `Dag` refuses an acceptance line that fuses a command with prose.
- **Picking lanes.** `picker.pick_team` ranks Engineer lanes, plus about twice as many Interns, from each lane's rollback rate and the model fit table, and keeps only lanes with a configured sender. It makes no network or model calls.
- **Dispatching.** [`dispatch.py`](../../src/companion/feature_team/dispatch.py) runs one bounded tick: selection is pure, the transport and clock are injected. A gate or approval node is never handed to a worker; it is raised once as a question. [`autopilot.py`](../../src/companion/feature_team/autopilot.py) checks capacity first (gate locks, free memory, budget pauses), and [`scripts/dispatcher.py`](../../scripts/dispatcher.py) is the loop's CLI.
- **Running a node.** [`node_worker.py`](../../src/companion/feature_team/node_worker.py) renders the brief ([`scripts/rq_brief.py`](../../scripts/rq_brief.py)), binds every path inside the lane's worktree, starts a fresh channel invocation per attempt and records the result. [`verify.py`](../../src/companion/feature_team/verify.py) checks out the result's sha in a clean scratch worktree and runs the brief's acceptance commands and the impacted tests there; it is the only code path that marks a node done.
- **Keeping it moving.** [`inspector.py`](../../src/companion/feature_team/inspector.py) checks every live run against its expected duration, in code, and frees the claims of dead workers. [`watchdog.py`](../../src/companion/feature_team/watchdog.py) reports a stalled loop or critical path to the inbox and never starts anything itself. [`fixback.py`](../../src/companion/feature_team/fixback.py) gives a failed result one fix-back run, then escalates.
- **Sending work.** [`scripts/lane_send.py`](../../scripts/lane_send.py) sends one brief to Codex, Cursor, Claude, Grok or a local worker CLI. It refuses briefs over 80 lines or 6 repo paths unless a reason is given, needs an open ownership claim per send ([`claims.py`](../../src/companion/claims.py)), and runs Grok and Muse under a per-worker `sandbox-exec` profile ([`worker_sandbox.py`](../../src/companion/worker_sandbox.py)).
- **Team chat.** [`team_chat.py`](../../src/companion/team_chat.py) keeps durable team conversations and bounded handoffs; [`team_http.py`](../../src/companion/team_http.py) is its FastAPI router. [`team_delegation.py`](../../src/companion/team_delegation.py) interviews the owner before any planning and fans the approved instruction out to members; [`team_transport.py`](../../src/companion/team_transport.py) and [`team_connected.py`](../../src/companion/team_connected.py) send to app and CLI members. [`team_replies.py`](../../src/companion/team_replies.py) labels every reply and holds private ones, and [`team_honesty.py`](../../src/companion/team_honesty.py) flags status claims the run facts do not back.
- **Locks and state.** `locks.FileLocks` keeps per-file leases in SQLite, `completion.CompletionMap` keeps completion state and makes atomic claims on ready nodes, and `runs` keeps one durable record per attempt.
- **Board inbox.** [`board_inbox.py`](../../src/companion/board_inbox.py) appends schema-checked lines to an append-only JSONL file under a file lock. Verified rows wait in a ready list ([`ready_board.py`](../../src/companion/feature_team/ready_board.py)) for a review before landing.
- **QC desk.** [`qc_sources.py`](../../src/companion/feature_team/qc_sources.py) reads local logs into one event stream, [`qc_rules.py`](../../src/companion/feature_team/qc_rules.py) names repeated failures, [`qc_incidents.py`](../../src/companion/feature_team/qc_incidents.py) turns one open episode into one Board suggestion, and [`qc_actions.py`](../../src/companion/feature_team/qc_actions.py) runs only fixed recovery commands.
- **Agent audit.** [`agent_control/`](../../src/companion/agent_control/) ingests existing logs into a hash-chained, append-only event store. It flags denied paths, scope drift, secret attempts and prompt-injection text, and scrubs every event before it is written. `redteam.py` runs dry-run attack plans from a test corpus.

**Stand-ins.** The private app's web backend, job queue, speech model, tool registry, contact store, local task catalogue, local privacy screen model and release policy are not here. Where the team code reaches them it uses a small stand-in: the speech route answers 501, the honesty check needs a queue passed in, no `local:*` channel is allowed, the screen reports itself off, memory packs are empty, and [`health_words.py`](../../src/companion/health_words.py) is a short English word list. Every worker also gets the rulebook at the repository root, `AGENTS.md`, which this package does not ship: add your own, and the 12 node-worker tests that need it run instead of skipping.

## Main files

| File | Purpose |
|---|---|
| [`feature_team/dag.py`](../../src/companion/feature_team/dag.py) | Loads and validates the feature DAG |
| [`feature_team/picker.py`](../../src/companion/feature_team/picker.py) | Ranks the lane roster |
| [`feature_team/dispatch.py`](../../src/companion/feature_team/dispatch.py) | One dispatcher tick: ready nodes to free channels |
| [`feature_team/node_worker.py`](../../src/companion/feature_team/node_worker.py) | Runs one node attempt and records the result |
| [`feature_team/verify.py`](../../src/companion/feature_team/verify.py) | External checks; the only path that marks a node done |
| [`feature_team/inspector.py`](../../src/companion/feature_team/inspector.py) | Finds dead or overdue runs and frees their claims |
| [`feature_team/watchdog.py`](../../src/companion/feature_team/watchdog.py) | Reports stalls to the inbox |
| [`feature_team/contract.py`](../../src/companion/feature_team/contract.py) | Brief, result and event schemas |
| [`feature_team/locks.py`](../../src/companion/feature_team/locks.py) | Per-file leases in SQLite |
| [`team_chat.py`](../../src/companion/team_chat.py) | Team conversations and handoffs |
| [`team_delegation.py`](../../src/companion/team_delegation.py) | Interview, plan approval and fan-out |
| [`scripts/lane_send.py`](../../scripts/lane_send.py) | Sends work to agent CLIs |
| [`board_inbox.py`](../../src/companion/board_inbox.py) | Locked, validated Board inbox appends |
| [`agent_control/store.py`](../../src/companion/agent_control/store.py) | Hash-chained audit store |

## Tests

From the repository root, after the install in the [top README](../../README.md#tests):

```sh
python -m pytest -q -n 2 tests/test_feature_team_*.py tests/test_lane_send*.py tests/test_claims.py tests/test_qc_*.py tests/test_agent_control_*.py tests/test_worker_sandbox.py tests/test_runner_exec*.py tests/test_team_*.py tests/test_dispatch*.py tests/test_inspector*.py tests/test_launch_silent.py tests/test_loop_*.py tests/test_zz_review_*.py
```
