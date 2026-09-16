"""Static test selection and private pytest outcome history."""
import ast
import json
import os
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


def _imports(source: str, *, package: str) -> set[str]:
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
    return {
        ".".join(parts[:i])
        for name in names if name == "companion" or name.startswith("companion.")
        for parts in [name.split(".")]
        for i in range(1, len(parts) + 1)
    }


def impacted(changed: list[str], *, root: Path, history: Path | None = None) -> set[str] | None:
    if not changed:
        return set()
    paths = [Path(path) for path in changed]
    shared = {"src/companion/schema.py", "tests/conftest.py", "tests/fakes.py", "pyproject.toml"}
    if any(path.as_posix() in shared or path.match("requirements*.txt") for path in paths):
        return None

    selected = recent_failures(history if history is not None else root / HISTORY_PATH.name)
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
        elif name.startswith("tests/data/") and len(path.parts) > 3:
            marker = "/".join(path.parts[1:3])
            selected.update(test for test, source in sources.items() if marker in source)
        elif name.startswith("tests/test_") and path.suffix == ".py":
            if (root / path).is_file():
                selected.add(name)
            else:
                return None
        elif not name.startswith(("web/", "docs/", "deploy/")):
            return None

    while True:
        dependents = {name for name, imports in graph.items() if imports & affected}
        if dependents <= affected:
            break
        affected.update(dependents)
    return selected | (affected & sources.keys())


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
