# Agent team

**Proof.** From a fresh install, the test command below gives **881 passed, 2 skipped**. In those tests `Dag` refuses a cycle and names its path. The audit store scrubs secrets before it writes, and `verify_chain` reports the first tampered line and catches a torn or deleted last line.

**What it does.** These are the building blocks of Kyra's agent team. A feature is a graph of nodes, a picker ranks the AI coding agents that run as CLI processes ("lanes"), a courier sends each lane its brief, and anything the team cannot settle goes to the Board inbox. A separate audit layer records what agents did, as metadata only.

**Key idea.** Each handoff passes a schema check and has a named owner. `contract` validates briefs, results and events, a file lease or a claim records who holds each piece of work, and work still needs a Board review before it can land.

This package has the components and their tests. The dispatcher loop that drives them in the private application, with its verifier, inspector, watchdog and fixback modules, is not included.

## How it works

- **Feature DAG.** `dag.Dag` loads nodes in file order and rejects cycles with `graphlib.TopologicalSorter`. Each node names its inputs, its tier and its acceptance lines. In strict mode `Dag` refuses an acceptance line that fuses a command with prose.
- **Picking lanes.** `picker.pick_team` ranks Engineer lanes, plus about twice as many Interns, from each lane's rollback rate and the model fit table, and keeps only lanes with a configured sender. It makes no network or model calls.
- **Sending work.** [`scripts/lane_send.py`](../../scripts/lane_send.py) sends one brief to Codex, Cursor, Claude, Grok or a local worker CLI. It refuses briefs over 80 lines or 6 repo paths unless a reason is given, needs an open ownership claim per send ([`claims.py`](../../src/companion/claims.py)), and runs Grok and Muse under a per-worker `sandbox-exec` profile ([`worker_sandbox.py`](../../src/companion/worker_sandbox.py)).
- **Locks and state.** `locks.FileLocks` keeps per-file leases in SQLite, `completion.CompletionMap` keeps completion state and makes atomic claims on ready nodes, and `contract` validates the brief, result and event shapes.
- **Board inbox.** [`board_inbox.py`](../../src/companion/board_inbox.py) appends schema-checked lines to an append-only JSONL file under a file lock. Verified rows wait in a ready list ([`ready_board.py`](../../src/companion/feature_team/ready_board.py)) for a review before landing.
- **QC desk.** [`qc_sources.py`](../../src/companion/feature_team/qc_sources.py) reads local logs into one event stream, [`qc_rules.py`](../../src/companion/feature_team/qc_rules.py) names repeated failures, [`qc_incidents.py`](../../src/companion/feature_team/qc_incidents.py) turns one open episode into one Board suggestion, and [`qc_actions.py`](../../src/companion/feature_team/qc_actions.py) runs only fixed recovery commands.
- **Agent audit.** [`agent_control/`](../../src/companion/agent_control/) ingests existing logs into a hash-chained, append-only event store. It flags denied paths, scope drift, secret attempts and prompt-injection text, and scrubs every event before it is written. `redteam.py` runs dry-run attack plans from a test corpus.

## Main files

| File | Purpose |
|---|---|
| [`feature_team/dag.py`](../../src/companion/feature_team/dag.py) | Loads and validates the feature DAG |
| [`feature_team/picker.py`](../../src/companion/feature_team/picker.py) | Ranks the lane roster |
| [`feature_team/contract.py`](../../src/companion/feature_team/contract.py) | Brief, result and event schemas |
| [`feature_team/locks.py`](../../src/companion/feature_team/locks.py) | Per-file leases in SQLite |
| [`feature_team/completion.py`](../../src/companion/feature_team/completion.py) | Completion state and ready-node claims |
| [`scripts/lane_send.py`](../../scripts/lane_send.py) | Sends work to agent CLIs |
| [`board_inbox.py`](../../src/companion/board_inbox.py) | Locked, validated Board inbox appends |
| [`agent_control/store.py`](../../src/companion/agent_control/store.py) | Hash-chained audit store |

## Tests

From the repository root, after the install in the [top README](../../README.md#tests):

```sh
python -m pytest -q tests/test_feature_team_*.py tests/test_lane_send*.py tests/test_claims.py tests/test_qc_*.py tests/test_agent_control_*.py tests/test_worker_sandbox.py tests/test_runner_exec*.py
```
