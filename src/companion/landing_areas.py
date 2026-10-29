"""Landing areas: which train a changed file belongs to, so the agent loop lands without the browser phase.

the owner, 2026-10-15 ~18:00: "split the project into independent pieces, that way the train can go independently and
the long and easy to flag error browser not hinder the priority ones". Areas: loop, web, apple, docs, core.

The browser rule comes first and fails toward web: any file for which test_impact.browser_tests selects even one
browser test (including every path it is unsure about) is web, wherever it sits. A branch's areas are the union of
its files' areas; a branch with any web file is a web branch. That one-step rule scopes browser runs and groups
trains. Skipping the browser phase is a separate, stricter decision (test_impact.browser_reach: the transitive
import closure of the browser files and the server; review 68): a non-web train whose files a page can execute
runs the 20 smoke nodes, and scripts/workflow_land.py re-checks browser_reach on the tested tree before any skip.
"""
from fnmatch import fnmatch
from pathlib import Path

AREAS = ("loop", "web", "apple", "docs", "core")

_LOOP_STEMS = ("dispatcher", "dispatch", "feature_team", "lane_send", "train", "remote_test", "watchdog",
               "autopilot", "node_worker", "verify", "courier", "budget", "low_power", "rq_brief", "push_check",
               "done_md", "commit_gate", "gate_lock", "browser_lock", "train_land", "test_impact", "landing_areas")
_TRAIN_LOOKALIKES = ("tests/test_train_adapter.py",)  # LoRA training, not the landing train

LOOP = ("src/companion/feature_team/*",
        *(f"scripts/{stem}.py" for stem in _LOOP_STEMS), "scripts/train_decide_head.py", "scripts/low_power_job.sh",
        *(f"src/companion/{stem}.py" for stem in _LOOP_STEMS),
        *(f"tests/test_{stem}.py" for stem in _LOOP_STEMS), *(f"tests/test_{stem}_*.py" for stem in _LOOP_STEMS))


def area(path, *, root, browser_tests=None):
    """The one area of a changed path. ``browser_tests`` is test_impact.browser_tests (injectable for tests)."""
    if browser_tests is None:
        from companion.test_impact import browser_tests
    if browser_tests([path], root=Path(root)):
        return "web"
    if path.startswith("apple/"):
        return "apple"
    if path.startswith("docs/") or path.endswith(".md"):
        return "docs"
    if path not in _TRAIN_LOOKALIKES and any(fnmatch(path, pattern) for pattern in LOOP):
        return "loop"
    return "core"


def areas(paths, *, root, browser_tests=None):
    """The union of the paths' areas; a set holding web means a web branch. No paths: core."""
    found = {area(path, root=root, browser_tests=browser_tests) for path in paths}
    return found or {"core"}


def is_web(found):
    return "web" in found


def train_kind(per_branch, allow_mixed=False):
    """(web?, areas) for a train from {branch: areas}. Refuses (ValueError) a train mixing web and non-web branches
    unless allow_mixed, so a browser-risky branch never rides a priority train by accident."""
    web = [branch for branch, found in per_branch.items() if is_web(found)]
    other = [branch for branch, found in per_branch.items() if not is_web(found)]
    if web and other and not allow_mixed:
        raise ValueError(f"train mixes web branches ({', '.join(web)}) with non-web ones ({', '.join(other)}); "
                         "split them into two trains or pass --allow-mixed")
    return bool(web), set().union(*per_branch.values())
