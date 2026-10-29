"""Persistent completion state and atomic ready-node claims."""

import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from companion.paths import DATA_DIR

COMPLETED_STATUSES = ("done", "deployed", "accepted")
STATUSES = (*COMPLETED_STATUSES, "queued", "dispatched", "failed")
_ROOT = Path(__file__).resolve().parents[3]
MAIN = _ROOT.parents[2] if _ROOT.parent.name == "worktrees" else _ROOT  # the main checkout, also from a worktree


def _relative(item, root: Path) -> str | None:
    """A repo-relative path for an artifact: absolute paths under a .claude/worktrees/<name>/ or under the main
    checkout are made relative; anything else (or a path escaping the repo) is None."""
    path = Path(item)
    if path.is_absolute():
        parts = path.parts
        for i in range(len(parts) - 2):
            if parts[i:i + 2] == (".claude", "worktrees"):
                path = Path(*parts[i + 3:]) if len(parts) > i + 3 else None
                break
        else:
            try:
                path = path.relative_to(root)
            except ValueError:
                return None
    if path is None or not path.parts or ".." in path.parts:
        return None
    return path.as_posix()


def _in_git(root: Path, rev: str, rel: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{rev}:{rel}"],
                          capture_output=True, timeout=30).returncode == 0


def unlanded(paths, root=None, refs=("origin/master", "HEAD")) -> list[str]:
    """The declared output paths that have not landed, judged by git only: a path has landed once origin/master
    has it, or the main checkout's HEAD has it after its fast-forward. File existence never counts (an untracked
    copy or the builder's worktree path would fail open), and a path that maps to no repo-relative path fails
    closed. "done" is written when verify passes in a worktree, before the train lands, so a dependent reads this,
    not the status (2026-10-16: DEMO-MEMORY was refused every tick on PKG-MAP's docs/architecture/packages.yaml
    while PKG-MAP's train was still queued)."""
    root = Path(root or MAIN)
    missing = []
    for item in paths:
        rel = _relative(item, root)
        if rel is None or not any(_in_git(root, ref, rel) for ref in refs):
            missing.append(str(item))
    return missing


class CompletionMap:
    """Use the configured data root; every operation reads current disk state."""

    def __init__(self, feature: str):
        if not isinstance(feature, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", feature):
            raise ValueError("feature must be a plain directory name")
        self.path = DATA_DIR / "team" / "features" / feature / "completion.json"
        self._read()  # Refuse corruption even before the first operation.

    @contextmanager
    def _locked(self):
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self):
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            document = json.loads(text)
            if not isinstance(document, dict) or "nodes" not in document:
                raise ValueError("missing nodes object")
            rows = document["nodes"]
            if not isinstance(rows, dict) or any(
                not isinstance(row, dict) or row.get("status") not in STATUSES
                for row in rows.values()
            ):
                raise ValueError("invalid rows")
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"Invalid completion file: {self.path}") from exc
        return rows

    def _write(self, rows):
        # E4 reads result; keep it derived from the canonical result_path.
        nodes = {}
        for node, row in rows.items():
            row = {"sha": None, "result_path": None, "by": None, **row}
            row["result"] = row["result_path"]
            nodes[node] = row
        text = json.dumps({"nodes": nodes}, indent=2, allow_nan=False) + "\n"
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                             dir=self.path.parent, delete=False) as out:
                name = out.name
                out.write(text)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.path)
        finally:
            if name is not None and os.path.exists(name):
                os.unlink(name)

    def done(self, node: str) -> bool:
        """Whether work is complete, including explicitly recorded delivery/acceptance."""
        return self._read().get(node, {}).get("status") in COMPLETED_STATUSES

    def mark(self, node: str, status: str, **fields):
        if not isinstance(node, str) or not node.strip() or status not in STATUSES:
            raise ValueError("mark requires a node ID and a valid status")
        if fields.keys() - {"sha", "result_path", "by", "t"}:
            raise ValueError("unknown completion fields")
        with self._locked():
            rows = self._read()
            old = rows.get(node, {})
            if old.get("channel") and "by" in fields and fields["by"] != old.get("by"):
                old = {**old, "channel": None}  # a new author's channel is unknown here; never keep the old one
            rows[node] = {**old, **fields, "status": status,
                          "t": fields.get("t", datetime.now(UTC).isoformat())}
            self._write(rows)

    def unlocked(self, dag) -> list[str]:
        """Claim ready IDs as queued in DAG order, once across processes/restarts.

        Failed nodes may be retried. The claim and snapshot are one locked write.
        """
        with self._locked():
            rows = self._read()
            ready = [id for id, node in dag._nodes.items()
                     if rows.get(id, {}).get("status") not in ("queued", "dispatched", *COMPLETED_STATUSES)
                     and all(rows.get(dep, {}).get("status") in COMPLETED_STATUSES for dep in node["inputs"])]
            for id in ready:
                rows[id] = {**rows.get(id, {}), "status": "queued",
                            "t": datetime.now(UTC).isoformat()}
            if ready:
                self._write(rows)
            return ready


def bottleneck(feature: str, node: str, kind: str, seconds, note: str = "") -> None:
    """Append one wait row to the feature's bottlenecks.jsonl log."""
    import math

    if kind not in ("locked", "no_worker", "load", "review", "the owner", "question"):
        raise ValueError("unknown bottleneck kind")
    if not isinstance(node, str) or not node.strip() or not isinstance(note, str):
        raise ValueError("bottleneck requires a node ID and text note")
    if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds) or seconds < 0):
        raise ValueError("bottleneck seconds must be a finite non-negative number")
    path = CompletionMap(feature).path.parent / "bottlenecks.jsonl"
    if path.is_symlink():
        raise ValueError("bottleneck log must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"t": datetime.now(UTC).isoformat(), "node": node, "kind": kind,
           "seconds": seconds, "note": note}
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(row, allow_nan=False) + "\n")
