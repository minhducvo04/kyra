---
name: test-impact
description: >
  Run the tests a change can affect while iterating, and the full suite only at commit and CI
  boundaries. Use before every pytest run during a build or a fix, and whenever the user mentions
  "impacted tests", "test impact", "TIA", or CI time. Never a substitute for the full suite before
  a commit.
license: MIT
---

# Test impact

From Anthropic's test impact analysis (2026): a listener records every test result, a selector picks
tests for a change from history and relevance, the full run stays the safety net.

Kyra's version is small because the full suite takes about thirty seconds:

1. Every `pytest` run appends one line per test to `data/test_history.jsonl` (outcome, duration,
   git head). The file is private runtime state, never committed.
2. `scripts/impacted_tests.py [--run] [paths...]` lists the test modules a change can affect:
   modules that import a changed source module (transitively), tests whose name shares the
   module's stem, tests that read a changed fixture under `tests/data/`, and any test that failed
   in the last ten recorded runs. With no paths it reads `git diff --name-only` against the merge
   base. `--run` executes that set.
3. While iterating: `scripts/impacted_tests.py --run`. Before a commit and in CI: the full suite.
   The commit message quotes the full suite's summary line, never the impacted set's.
4. A selector that returns nothing for a change means "unknown", and unknown runs the full suite.
5. Flaky or recently failing tests are always selected, so a fix is seen sooner, not later.
