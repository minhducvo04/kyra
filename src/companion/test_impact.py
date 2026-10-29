"""Static test selection and private pytest outcome history."""
import ast
import functools
import json
import os
import re
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HISTORY_PATH = PROJECT_ROOT / ".pytest_history.jsonl"


def append_run(path: Path, *, head: str, results: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for result in results:
            stream.write(json.dumps({"head": head, **result}) + "\n")


def recent_failures(path: Path, *, runs: int = 10) -> set[str]:
    if runs <= 0 or not path.exists():
        return set()
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    heads = list(dict.fromkeys(record["head"] for record in reversed(records)))[:runs]
    return {
        record["nodeid"].split("::", 1)[0]
        for record in records
        if record["head"] in heads and record["outcome"] == "failed"
    }


# Keyed by the source text itself, so an edited file is parsed again and an unchanged one never is: impacted() reads
# ~1,000 test files per call and commit_gate, train and build_test_select_set call it several times per process
# (~4 s of ast.parse per call before, test diet D3 2026-10-15). Bounded to about two checkouts' worth of files.
@functools.lru_cache(maxsize=4096)
def _imports(source: str, *, package: str) -> frozenset[str]:
    """Include imports inside functions/guards, without executing any source."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[:len(parts) - node.level + 1] + ([base] if base else []))
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    # Importing a child also executes its package's __init__.py.
    return frozenset(
        ".".join(parts[:i])
        for name in names if name == "companion" or name.startswith("companion.")
        for parts in [name.split(".")]
        for i in range(1, len(parts) + 1)
    )


def impacted(changed: list[str], *, root: Path, history: Path | None = None,
             include_history: bool = True) -> set[str] | None:
    if not changed:
        return set()
    paths = [Path(path) for path in changed]
    shared = {"src/companion/schema.py", "tests/conftest.py", "tests/fakes.py", "pyproject.toml"}
    if any(path.as_posix() in shared or path.match("requirements*.txt") for path in paths):
        return None

    selected = recent_failures(history if history is not None else root / HISTORY_PATH.name) if include_history else set()
    sources = {}
    graph = {}
    try:
        for path in (root / "src" / "companion").rglob("*.py"):
            parts = path.relative_to(root / "src").with_suffix("").parts
            module = ".".join(parts[:-1] if path.stem == "__init__" else parts)
            package = module if path.stem == "__init__" else module.rpartition(".")[0]
            graph[module] = _imports(path.read_text(encoding="utf-8"), package=package)
        for path in (root / "tests").glob("test_*.py"):
            name = path.relative_to(root).as_posix()
            sources[name] = path.read_text(encoding="utf-8")
            graph[name] = _imports(sources[name], package="tests")
    except (OSError, SyntaxError, UnicodeError):
        return None

    affected = set()
    for path in paths:
        name = path.as_posix()
        if name.startswith("src/companion/") and path.suffix == ".py":
            parts = path.with_suffix("").parts[1:]
            affected.add(".".join(parts[:-1] if path.stem == "__init__" else parts))
            selected.update(test for test in sources if Path(test).match(f"test_{path.stem}*.py"))
        elif (name.startswith("src/companion/") and path.suffix == ".json"
              and len(path.parts) == 4):
            marker = path.parts[2]
            selected.update(test for test, source in sources.items() if marker in source)
        elif name.startswith("scripts/") and path.suffix == ".py" and path.parent.as_posix() == "scripts":
            stem = path.stem
            matched = {
                test for test, source in sources.items()
                if f"scripts/{stem}" in source or f"scripts.{stem}" in source
                or f"from scripts import {stem}" in source
            }
            if len(matched) == 0:
                return None
            selected.update(matched)
        elif name.startswith("tests/data/") and len(path.parts) > 3:
            marker = "/".join(path.parts[1:3])
            selected.update(test for test, source in sources.items() if marker in source)
        elif name.startswith("tests/test_") and path.suffix == ".py":
            if (root / path).is_file():
                selected.add(name)
            else:
                return None
        elif not name.startswith(("web/", "docs/", "deploy/", "apple/")):
            return None

    while True:
        dependents = {name for name, imports in graph.items() if imports & affected}
        if dependents <= affected:
            break
        affected.update(dependents)
    return {name for name in selected | (affected & sources.keys()) if (root / name).is_file()}


LANDING_GUARDS = ('tests/test_no_third_party_pii.py', 'tests/test_no_personal_files.py',
                  'tests/test_no_target_companies.py')


def landing_selection(changed: list[str], *, root: Path, full: bool = False) -> dict:
    """Fail-safe gate plan for the merged tree, also used to decide queue batch eligibility.

    Check each mapping without failure history: a mapped neighbour or an old failure
    must not hide an unmapped file. Documentation and Apple sources are intentional
    no-test mappings (Swift has its own gate).
    """
    from companion.workflow_pr import _protected_path

    reasons = {}
    full_reasons = ['FULL=1'] if full else []
    gate_names = {'train.py', 'landing_runner.py', 'remote_test.py', 'test_impact.py'}
    for path in changed:
        name = Path(path).name
        if (_protected_path(path) or name in gate_names
                or any(token in path for token in ('worker_sandbox', 'permission', 'secret',
                                                   'publish', 'public-manifest', 'public_export',
                                                   'export_scan', 'scan_export', 'credential', 'privacy',
                                                   'redact', 'worker_auth', 'webauth', 'api_key'))
                or name.startswith('.env')
                or path in LANDING_GUARDS or 'guard' in name):
            full_reasons.append(f'sensitive core: {path}')

    def add(test, reason):
        reasons.setdefault(test, []).append(reason)

    if not full_reasons:
        try:
            selected = impacted(changed, root=root)
            if selected is None:
                full_reasons.append('unknown mapping: ' + ', '.join(changed))
            else:
                for path in changed:
                    own = impacted([path], root=root, include_history=False)
                    if own is None or (not own and not path.startswith(('docs/', 'apple/'))):
                        full_reasons.append(f'unknown mapping: {path}')
                for test in sorted(selected):
                    add(test, 'impacted')
        except (OSError, ValueError, SyntaxError, UnicodeError) as exc:
            full_reasons.append(f'unknown mapping: {type(exc).__name__}')
    for test in LANDING_GUARDS:
        add(test, 'guard')
    add('tests/test_package_map.py', 'always-run package map')
    try:
        manifest = root / 'tests/preflight.txt'
        for line in manifest.read_text().splitlines():
            test = line.split('#', 1)[0].strip()
            if test:
                add(test, 'always-run smoke')
        # Keep the existing browser smoke boundary for changes visible to a page.
        if not full_reasons and browser_reach(changed, root=root):
            for line in (root / 'tests/browser_smoke.txt').read_text().splitlines():
                test = line.split('#', 1)[0].strip()
                if test:
                    add(test, 'browser smoke')
    except OSError:
        full_reasons.append('unknown mapping: missing smoke manifest')
    # A whole file subsumes its smoke nodes; do not collect those tests twice.
    tests = sorted(test for test in reasons if '::' not in test or test.split('::')[0] not in reasons)
    return {'full': full_reasons, 'tests': tests, 'reasons': reasons}


class HistoryPlugin:
    """Record call-phase reports independently of KYRA_DATA_DIR isolation."""

    def __init__(self):
        self.results = []

    def pytest_runtest_logreport(self, report):
        if report.when == "call":
            self.results.append({
                "nodeid": report.nodeid,
                "outcome": report.outcome,
                "duration": report.duration,
            })

    def pytest_sessionfinish(self, session, exitstatus):
        try:
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT,
                                  capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            head = "unknown"  # outside a checkout the history is still worth keeping
        append_run(Path(os.environ.get("KYRA_TEST_HISTORY", HISTORY_PATH)), head=head, results=self.results)


_ROUTE_MARK = re.compile(r'APIRouter\(|@(?:app|router)\.(?:get|post|put|patch|delete|websocket)\(|'
                         r'add_api_route|include_router|\.mount\(')
# Kinds that no page can observe: prose, Apple clients, launchd/deploy files, agent config.
_NO_PAGE = ('docs/', 'apple/', 'deploy/', '.claude/', '.agents/')


def _test_imports(source: str) -> set[str]:
    """Names like tests.test_team_lead_http that a browser file pulls in as a fixture."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names if alias.name.startswith('tests.'))
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            if node.module == 'tests':
                names.update(f'tests.{alias.name}' for alias in node.names)
            elif node.module.startswith('tests.'):
                names.add(node.module)
    return names


def _served_by_routes(root: Path) -> set[str]:
    """Modules that webapp.py or any route-declaring module imports directly (lazy imports too)."""
    served = set()
    for path in (root / 'src' / 'companion').rglob('*.py'):
        text = path.read_text(encoding='utf-8')
        if path.name == 'webapp.py' or _ROUTE_MARK.search(text):
            parts = path.relative_to(root / 'src').with_suffix('').parts
            module = '.'.join(parts[:-1] if path.stem == '__init__' else parts)
            package = module if path.stem == '__init__' else module.rpartition('.')[0]
            served |= _imports(text, package=package)
    return served


_SCRIPT_NAME = re.compile(r'\b(\w+)\.py\b|\bscripts\.(\w+)|\bscript\(\s*[\'"](\w+)[\'"]')
_MODULE_STRING = re.compile(r'[\'"](companion(?:\.\w+)+)[\'"]')


def _test_fixture_closure(root: Path, names: set[str], memo: dict) -> set[str]:
    """tests.* modules reached from ``names`` through further tests.* imports (review 68b D1c: 65 browser files load
    a fixture module that imports another test module). ``memo`` caches each module's own tests.* imports."""
    reached, todo = set(), list(names)
    while todo:
        name = todo.pop()
        if name in reached:
            continue
        reached.add(name)
        if name not in memo:
            source = root / 'tests' / f"{name.split('.', 1)[1].replace('.', '/')}.py"
            memo[name] = _test_imports(source.read_text(encoding='utf-8')) if source.is_file() else set()
        todo.extend(memo[name])
    return reached


def browser_closure(root: Path) -> set[str]:
    """What a browser page can execute, followed transitively and failing toward the page: companion module names,
    'scripts/<x>.py' paths and tests.* fixture modules. The start is every browser file (and the test modules it
    loads, at any depth), webapp.py and every route module. Edges are companion imports, a quoted 'companion.x'
    string (python -m companion.x), and any existing script a source names (subprocess or spec_from_file_location
    by path, scripts.x, dispatch.script('x')). browser_tests follows one step only, which is right for scoping a
    browser run and wrong for skipping one (review 68: 17 feature_team modules reach pages through webapp.py;
    review 68b: power_routes runs scripts/low_power.py, local_lane runs -m companion.worker_loop)."""
    scripts = {path.stem for path in (root / 'scripts').glob('*.py')}

    def edges(text, package):
        found = set(_imports(text, package=package)) | set(_MODULE_STRING.findall(text))  # _imports is cached frozen
        for groups in _SCRIPT_NAME.findall(text):
            stem = next(group for group in groups if group)
            if stem in scripts:
                found.add(f'scripts/{stem}.py')
        return found

    graph, start = {}, set()
    for path in (root / 'src' / 'companion').rglob('*.py'):
        text = path.read_text(encoding='utf-8')
        parts = path.relative_to(root / 'src').with_suffix('').parts
        module = '.'.join(parts[:-1] if path.stem == '__init__' else parts)
        package = module if path.stem == '__init__' else module.rpartition('.')[0]
        graph[module] = edges(text, package)
        if path.name == 'webapp.py' or _ROUTE_MARK.search(text):
            start.add(module)
    for stem in scripts:
        graph[f'scripts/{stem}.py'] = edges((root / 'scripts' / f'{stem}.py').read_text(encoding='utf-8'), '')
    for path in (root / 'tests').glob('test_*.py'):
        text = path.read_text(encoding='utf-8')
        graph[f'tests.{path.stem}'] = edges(text, 'tests') | _test_imports(text)
        if path.name.endswith('_browser.py'):
            start.add(f'tests.{path.stem}')
    reached, todo = set(), list(start)
    while todo:
        name = todo.pop()
        if name not in reached:
            reached.add(name)
            todo.extend(graph.get(name, ()))
    return reached


def browser_reach(changed: list[str], *, root: Path) -> list[str]:
    """The changed paths a browser page could observe: browser_tests' one-step rule (with its fail-toward-full
    cases) or a module, script or test module in browser_closure. Empty means a browser phase may be skipped."""
    try:
        closure = browser_closure(root)
    except (OSError, SyntaxError, UnicodeError):
        return list(changed)
    hits = []
    for name in changed:
        path = Path(name)
        node = None
        if name.startswith('src/companion/') and path.suffix == '.py':
            parts = path.with_suffix('').parts[1:]
            node = '.'.join(parts[:-1] if parts[-1] == '__init__' else parts)
        elif name.startswith('scripts/') and path.suffix == '.py' and path.parent.as_posix() == 'scripts':
            node = name
        elif path.parent.as_posix() == 'tests' and path.match('test_*.py'):
            node = f'tests.{path.stem}'
        if node in closure or browser_tests([name], root=root):
            hits.append(name)
    return hits


def browser_tests(changed: list[str], *, root: Path) -> list[str]:
    """Browser files that can observe the change, read statically; anything unsure runs them all.

    The whole set runs for a web asset, the app shell (webapp.py, a module that declares
    routes), page data under src/companion that is not Python, a browser test or a shared
    fixture, and any path kind not named below. A backend module selects only the browser
    files that import it or name it, plus the page whose name it starts with (jobs_http.py:
    test_jobs*_browser.py). Import reach through the app is not followed: through webapp.py it
    reached most pages, so a loop-only change ran 32 browser files (train48, 2026-10-15).
    One step is followed: a module that webapp.py or a route module imports directly is
    served to pages, so it runs the whole set too.
    Any module inside a sub-package that holds a route module (today src/companion/cad/)
    runs the whole set, since that package's routes reach all of it.
    """
    candidates = {p.relative_to(root).as_posix() for p in (root / 'tests').glob('test_*_browser.py')}
    full = sorted(candidates)
    selected = set()
    try:
        sources = {test: (root / test).read_text(encoding='utf-8') for test in candidates}
        imports = {test: _imports(source, package='tests') for test, source in sources.items()}
        memo = {}
        fixtures = {test: _test_fixture_closure(root, _test_imports(source), memo) for test, source in sources.items()}
    except (OSError, SyntaxError, UnicodeError):
        return full
    for name in changed:
        path = Path(name)
        if name.startswith(_NO_PAGE) or (path.suffix == '.md' and '/' not in name):
            continue
        if name.startswith('web/') or name == 'pyproject.toml' or path.match('requirements*.txt'):
            return full
        if name.startswith('tests/'):
            if path.parent.as_posix() != 'tests' or not path.match('test_*.py') or name.endswith('_browser.py'):
                return full  # conftest, fakes, data, runners and the browser files themselves
            module = f'tests.{path.stem}'
            selected.update(test for test in candidates if module in fixtures[test])
            continue
        if name.startswith('scripts/') and path.suffix in ('.py', '.sh'):
            selected.update(test for test, source in sources.items()
                            if name in source or f'scripts.{path.stem}' in source)
            continue
        if not (name.startswith('src/companion/') and path.suffix == '.py'):
            return full  # unknown kind, or page data such as features.yaml
        try:
            text = (root / name).read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            return full  # deleted or unreadable: cannot tell what it served
        if path.name == 'webapp.py' or path.name == 'route_exposure.py' or _ROUTE_MARK.search(text):
            return full
        parts = path.with_suffix('').parts[1:]
        module = '.'.join(parts[:-1] if parts[-1] == '__init__' else parts)
        try:
            served = _served_by_routes(root)
            package = root / 'src' / 'companion' / path.parts[2] if len(path.parts) > 3 else None
            # A sub-package with its own routes (cad/http.py) drives its kernel two steps in and
            # starts workers by string (-m companion.cad.import_worker): review 60, 2026-10-15.
            if package and any(_ROUTE_MARK.search(p.read_text(encoding='utf-8')) for p in package.rglob('*.py')):
                return full
        except (OSError, SyntaxError, UnicodeError):
            return full
        if module in served:
            return full  # webapp.py or a route module calls it directly
        importers = {test for test in candidates if module in imports[test]}
        if len(importers) > 3:
            return full  # widely shared by pages
        page = (path.parent.name if path.stem == '__init__' else path.stem).split('_')[0]
        selected.update(importers)
        # One step through scripts too: page_check.py imports browser_lock, and its browser file names the script,
        # not the module (review 84, 2026-10-15). The scripts rule above then picks the file by the script's name.
        try:
            scripts = [p for p in (root / 'scripts').glob('*.py')
                       if module in _imports(p.read_text(encoding='utf-8'), package='scripts')]
        except (OSError, SyntaxError, UnicodeError):
            return full
        selected.update(test for test, source in sources.items() for script in scripts
                        if f'scripts/{script.name}' in source or f'scripts.{script.stem}' in source)
        selected.update(test for test, source in sources.items()
                        if Path(test).name.startswith(f'test_{page}') or module in source
                        or name.removeprefix('src/') in source)
    return sorted(selected)
