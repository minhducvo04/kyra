# Landing gate

**Proof.** From a fresh install, the first test command below gives **504 passed** and the test system command **237 passed**. The gate tests run against real temporary Git repositories: the gate lands a tested fast-forward, and it refuses a merge that is not a fast-forward, a master that moved while the suite ran, and a privacy guard that failed or was skipped.

**What it does.** Takes reviewed agent branches from a queue, merges them in batches called trains, runs the tests each change affects plus the privacy guards and ruff, and pushes to master only as a fast-forward. A keeper process watches master and holds the queue while master is red.

**Key idea.** A queue line pins each branch to the sha that was reviewed; if the branch has moved, the line is refused. One script, `workflow_land.py`, is the only code that pushes, and it refuses unless every check passed on a clean worktree and master did not move during the run.

## How it works

- **Queue.** [`scripts/landing_runner.py`](../../scripts/landing_runner.py) reads one train per line (`<name> <kind> <branch>@<sha>...`), runs one train at a time while the one-minute load is under 12, and never pushes itself. [`landing/queue.py`](../../src/companion/landing/queue.py) keeps an append-only merge queue where a candidate joins only with a review PASS on its own sha, and groups file-disjoint candidates into trains.
- **Trains.** [`scripts/train.py`](../../scripts/train.py) merges the branches in order into one worktree, runs the privacy guards and ruff on changed Python files, then calls the gate with the impacted tests. Full-suite entries batch together, up to six.
- **Areas.** [`landing_areas.py`](../../src/companion/landing_areas.py) puts each file in an area (loop, web, apple, docs, core). A train that mixes web and non-web files is refused. [`gate_lock.py`](../../src/companion/gate_lock.py) holds one lock per area, so trains in different areas can land side by side.
- **Gate.** [`scripts/workflow_land.py`](../../scripts/workflow_land.py) refuses unless the merge is a fast-forward of freshly fetched master, no protected paths changed, ruff and pytest pass in a clean worktree, the privacy guards pass with none skipped, and master did not move while the suite ran. It then writes a receipt and pushes without force.
- **Shards.** [`test_shards.py`](../../src/companion/test_shards.py) splits test files across three machines, keeping protected and landing tests on the main one. [`scripts/remote_test.py`](../../scripts/remote_test.py) ships a `git archive` over SSH (never the data directory or `.env`) and runs pytest there under a lock. [`train_pool.py`](../../src/companion/train_pool.py) can spread non-browser files over a runner pool and checks that each file had exactly one accepted outcome.
- **Failures.** A red batch is split in halves. Transient refusals (load, a held runner, master moved) keep their queue line and retry with a back-off from 5 to 30 minutes, posting to the Board on the third retry.
- **Master keeper.** [`scripts/master_keeper.py`](../../scripts/master_keeper.py) runs the full suite on each new master sha. If master is red it confirms the failing tests, holds the queue, dispatches a fix and races a revert. When master is green again it clears the hold. It alerts after 45 minutes without landing progress.

## Test system

The suite runs on every machine in a runner registry at once, and each machine says what it can run instead of the code naming machines.

- **Markers.** [`test_markers.py`](../../src/companion/test_markers.py) defines six needs: macOS, real data, git history, network loopback, GPU and timing sensitive. A test file with no marker is hermetic and may run anywhere; an unknown marker is refused.
- **Probe and registry.** [`capability_probe.py`](../../src/companion/capability_probe.py) checks what a machine has (OS, cores, RAM, GPU, tools, git history, loopback, current load). [`scripts/runner_bootstrap.py`](../../scripts/runner_bootstrap.py) writes that as one registry entry per machine, and [`runner_bootstrap.py`](../../src/companion/runner_bootstrap.py) is the only loader the planners use.
- **Placement.** [`test_placement.py`](../../src/companion/test_placement.py) puts each file on a runner whose probed facts cover its markers and balances runners by measured seconds per worker. Timing sensitive files are scheduled, not refused: they go last, one worker, preferring a calm runner. Real data files never leave the machine that holds the data ([`runner_pool.py`](../../src/companion/runner_pool.py) checks this after planning).
- **Fast track.** [`scripts/test_fasttrack.py`](../../scripts/test_fasttrack.py) runs the plan on all runners in parallel, at most 350 files per call, re-plans when a runner is unreachable, runs local batches one after another under the machine's memory budget, and prints one table. [`scripts/test_plan.py`](../../scripts/test_plan.py) shows the plan without running it.
- **Master health.** [`master_health.py`](../../src/companion/master_health.py) keeps one record per runner and commit; [`scripts/master_health.py`](../../scripts/master_health.py) prints green, red, missing or stale for each runner.
- **Trains.** [`train_placement.py`](../../src/companion/train_placement.py) picks a runner for each train from the registry and its slot limits, and [`train_phase.py`](../../src/companion/train_phase.py) runs a train's test phase across runners, moving the files of a runner that dies to the others.
- **Memory caps.** Every pytest session caps itself: [`pytest_caps.py`](../../src/companion/pytest_caps.py), loaded from `tests/conftest.py`, watches its own process tree with [`resource_cap.py`](../../src/companion/resource_cap.py) and kills it at a memory or time limit, even where `ps` is denied. [`mac_guard.py`](../../src/companion/mac_guard.py) admits or defers new work by memory pressure and fails closed when it cannot read it, and [`local_test_supervisor.py`](../../src/companion/local_test_supervisor.py) runs a local test worker under a fixed physical memory ceiling and cleans up its whole process group. [`scripts/mac_hard_kill.py`](../../scripts/mac_hard_kill.py) is the last resort: a launchd agent outside every sandbox that stops any process over 12 GB, or over 4 GB and growing more than 1 GB in 10 seconds.

## Main files

| File | Purpose |
|---|---|
| [`scripts/landing_runner.py`](../../scripts/landing_runner.py) | Queue loop, batching, bisection, retries |
| [`scripts/train.py`](../../scripts/train.py) | Merges a train, runs guards and ruff, calls the gate |
| [`scripts/workflow_land.py`](../../scripts/workflow_land.py) | The merge gate; the only code that pushes |
| [`scripts/master_keeper.py`](../../scripts/master_keeper.py) | Master health, queue hold, fix and revert race |
| [`src/companion/gate_lock.py`](../../src/companion/gate_lock.py) | Gate locks by kind and area |
| [`src/companion/landing_areas.py`](../../src/companion/landing_areas.py) | File areas and train kind |
| [`src/companion/test_shards.py`](../../src/companion/test_shards.py) | Places test files on machines |
| [`src/companion/train_pool.py`](../../src/companion/train_pool.py) | Optional runner pool phase |
| [`scripts/remote_test.py`](../../scripts/remote_test.py) | Runs pytest on another machine over SSH |
| [`src/companion/landing/queue.py`](../../src/companion/landing/queue.py) | Review-gated merge queue |
| [`src/companion/test_markers.py`](../../src/companion/test_markers.py) | Test needs markers |
| [`src/companion/test_placement.py`](../../src/companion/test_placement.py) | Places test files by needs and probed facts |
| [`scripts/test_fasttrack.py`](../../scripts/test_fasttrack.py) | Runs the suite on every registry runner |
| [`src/companion/pytest_caps.py`](../../src/companion/pytest_caps.py) | Memory and time cap inside each pytest session |
| [`scripts/mac_hard_kill.py`](../../scripts/mac_hard_kill.py) | Last resort memory kill switch |

## Tests

From the repository root, after the install in the [top README](../../README.md#tests):

```sh
PYTHONPATH=src python -m pytest -q tests/test_landing_*.py tests/test_workflow_land.py tests/test_master_keeper.py tests/test_test_shards.py tests/test_train_pool.py tests/test_gate_lock.py tests/test_remote_test.py tests/test_train*.py
```

The test system:

```sh
PYTHONPATH=src python -m pytest -q tests/test_test_markers.py tests/test_test_placement.py tests/test_capability_probe.py tests/test_runner_bootstrap.py tests/test_runner_pool.py tests/test_runner_probe.py tests/test_master_health.py tests/test_test_fasttrack.py tests/test_resource_cap.py tests/test_pytest_caps.py tests/test_mac_guard*.py tests/test_mac_hard_kill.py tests/test_local_test_supervisor.py
```
