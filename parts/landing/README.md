# Landing gate

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

## Tests

```sh
PYTHONPATH=src python -m pytest -q tests/test_landing_*.py tests/test_workflow_land.py tests/test_master_keeper.py tests/test_test_shards.py tests/test_train_pool.py tests/test_gate_lock.py tests/test_remote_test.py tests/test_train*.py
```
