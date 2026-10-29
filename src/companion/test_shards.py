"""LR-3a host placement only: no transport, scheduling, or landing side effects.

The current train lands on M5; M1 is its test runner. Protected tests stay on M5
even when overflow is unavailable. WSL relaxes only the Windows allowlist;
browser and sensitive-core placement still applies. Callers retain the original
plan separately from host reports and pass both to ``verdict`` (see its schema).
"""
from __future__ import annotations

import ast
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path

from companion import headroom
from companion.runner_pool import REAL_DATA_TESTS
from companion.workflow_pr import _protected_path

WINDOWS_PASS = Path(__file__).resolve().parents[2] / 'tests/windows_pass.txt'
_TEST_PATH = re.compile(r'tests/(?:[A-Za-z0-9_-]+/)*test_[A-Za-z0-9_-]+\.py')
_HOSTS = frozenset({'pc', 'm1', 'm5'})
# Match landing_selection's sensitive-core categories, also covering the gate
# tests themselves and the host admission/placement boundaries introduced here.
_CORE_TOKENS = ('sandbox', 'worker_wall', 'permission', 'secret', 'publish',
                'public', 'export', 'credential', 'privacy',
                'redact', 'auth', 'api_key', 'guard', 'security', 'access', 'headroom')
_CORE_PREFIXES = ('test_train', 'test_landing', 'test_remote_test', 'test_test_impact',
                  'test_test_shards', 'test_runner_', 'test_commit_gate', 'test_push_check',
                  'test_settings', 'test_paths', 'test_app_tasks', 'test_deploy_local')


def _paths(files: Iterable[str]) -> list[str]:
    if isinstance(files, (str, bytes)):
        raise ValueError('expected a collection of test paths')
    result = list(files)
    for path in result:
        if (not isinstance(path, str) or not _TEST_PATH.fullmatch(path)
                or 'data' in path.casefold().split('/')):
            raise ValueError('invalid test path: expected tests/.../test_name.py')
    return result


def load_win_pass(path: Path = WINDOWS_PASS) -> set[str]:
    """Read the tracked allowlist. Unreadable means no PC work; malformed fails."""
    try:
        lines = Path(path).read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeError):
        return set()
    return set(_paths(line.split('#', 1)[0].strip() for line in lines if line.split('#', 1)[0].strip()))


def _landing_only(path: str) -> bool:
    folded = path.casefold()
    name = Path(folded).stem
    # Source-equivalent names catch protected settings/paths/app_tasks tests too.
    source = 'src/companion/' + name.removeprefix('test_') + '.py'
    return (folded in REAL_DATA_TESTS or folded == 'tests/test_package_map.py'
            or _protected_path(folded) or _protected_path(source)
            or name.startswith(_CORE_PREFIXES) or any(token in folded for token in _CORE_TOKENS))


def _browser_source(path: str) -> bool:
    """Inspect source relative to the checkout cwd, without importing tests."""
    try:
        tree = ast.parse(Path(path).read_text(encoding='utf-8'), filename=path)
    except FileNotFoundError:
        # Keep support for planning paths before they exist locally.
        return False
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise ValueError(f'cannot inspect test source: {path}') from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split('.')[0] == 'playwright' for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or '').split('.')[0] == 'playwright':
                return True
        elif isinstance(node, ast.arg) and node.arg in {'page', 'browser'}:
            return True
        elif isinstance(node, ast.Call):
            name = getattr(node.func, 'attr', getattr(node.func, 'id', None))
            if name in {'usefixtures', 'getfixturevalue'} and any(
                isinstance(arg, ast.Constant) and arg.value in ('page', 'browser')
                for arg in node.args
            ):
                return True
    return False


def plan_shards(selected_files: Iterable[str], browser_files: Iterable[str], *,
                priority: bool, win_pass: Iterable[str] | None = None,
                m5_free: bool, wsl: bool = False) -> dict[str, list[str]]:
    """Partition files exactly once, in stable order, omitting empty hosts.

    ``selected_files`` may include browser files; their union is the test set.
    Protected files stay on the landing M5, outside overflow balancing. With M5
    free, M1 keeps ceil(remaining non-browser Mac files / 2); overflow goes to M5.
    M5 takes ceil(remaining browser files / 2). PC eligibility is explicit in
    ``win_pass`` (the tracked manifest when omitted), or non-browser WSL work.
    Priority placement ignores headroom; admission/scheduling belongs to LR-3b.
    """
    selected = set(_paths(selected_files))
    browsers = set(_paths(browser_files))
    allowed = load_win_pass() if win_pass is None else set(_paths(win_pass))
    files = selected | browsers
    if priority:
        return {'m5': sorted(files)} if files else {}
    # A caller omitting a browser from browser_files must not send it to Windows.
    browsers |= {path for path in files if 'browser' in Path(path).name.casefold()
                 or _browser_source(path)}
    protected = {path for path in files if _landing_only(path)}
    pc = {path for path in files - browsers - protected if wsl or path in allowed}
    mac = sorted(files - browsers - pc - protected)
    overflow = set(protected)
    if m5_free:
        count = len(mac) // 2
        if count:
            overflow.update(mac[-count:])
        movable_browsers = sorted(browsers - protected)
        overflow.update(movable_browsers[:(len(movable_browsers) + 1) // 2])
    shards = {'pc': sorted(pc), 'm1': sorted(files - pc - overflow), 'm5': sorted(overflow)}
    covered = [path for shard in shards.values() for path in shard]
    if len(covered) != len(files) or set(covered) != files:
        raise ValueError('shards must cover every test path exactly once')
    return {host: paths for host, paths in shards.items() if paths}


def m5_available(data_dir: Path | None = None, *, probes: headroom.Probes | None = None) -> bool:
    """Read fleet load admission and headroom once; unknown means unavailable.

    Uses the main checkout's data directory by default. Travel state is read
    without calling auto_travel (which can write state). No waits or launches.
    """
    try:
        data = headroom.main_data_dir() if data_dir is None else Path(data_dir)
        if data is None:
            return False
        fleet = json.loads((data / 'team/fleet.json').read_text(encoding='utf-8'))
        threshold = fleet['pause_new_launches_above_load1']
        if type(threshold) not in (int, float) or not math.isfinite(threshold) or threshold <= 0:
            return False
        probes = probes or headroom.Probes()
        load = probes.load()
        if type(load) not in (int, float) or not math.isfinite(load) or load < 0 or load > threshold:
            return False
        if headroom.travel_state(data)['on']:
            return False
        return headroom.headroom_refusal(replace(probes, load=lambda: load)) is None
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def verdict(results_by_host: Mapping) -> str:
    """Return 'pass' only for complete, successful evidence against the plan.

    Input: {"plan": {host: [files]}, "results":
            {host: {"exit_code": 0, "files": [accounted_for_files]}}}.
    The caller supplies its saved plan, NOT a plan reconstructed from arrivals.
    An unreachable host may be absent or None. Either fails. Reports must name
    every file exactly once, on the planned host, with no extras. This checks
    accounting, not remote report authenticity or actual transport execution.
    """
    try:
        plan, reports = results_by_host['plan'], results_by_host['results']
        if (not isinstance(plan, Mapping) or not isinstance(reports, Mapping)
                or not plan or set(plan) - _HOSTS or set(plan) != set(reports)):
            return 'fail'
        seen = set()
        for host, files in plan.items():
            expected = _paths(files)
            if not expected or len(set(expected)) != len(expected) or seen.intersection(expected):
                return 'fail'
            seen.update(expected)
            report = reports[host]
            if not isinstance(report, Mapping) or type(report.get('exit_code')) is not int or report['exit_code'] != 0:
                return 'fail'
            if sorted(_paths(report['files'])) != sorted(expected):
                return 'fail'
    except (KeyError, TypeError, ValueError):
        return 'fail'
    return 'pass'
