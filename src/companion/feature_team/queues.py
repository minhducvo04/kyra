"""Role-bound local queues. Caller roles must come from the trusted dispatcher."""

import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime

from companion.feature_team.completion import CompletionMap

ROLES = ("engineer", "intern")
LOG = logging.getLogger(__name__)


def _deny():
    LOG.warning("Feature queue access refused: invalid role or queue permission")
    raise PermissionError("queue role not permitted")


class Queues:
    """Take returns a node ID, or None. Environment variables never grant roles.

    caller_role is assigned by trusted application code, not request payloads.
    Engineer requests fall back to Intern work only after Engineer work is empty.
    """

    def __init__(self, feature: str, caller_role: str):
        if caller_role not in ROLES:
            _deny()
        self._caller_role = caller_role
        self.completion = CompletionMap(feature)
        self.folder = self.completion.path.parent
        self._check_paths()

    def _path(self, role):
        return self.folder / f"queue-{role}.jsonl"

    def _check_paths(self):
        paths = [self.completion.path, self.completion.path.with_suffix(".lock"),
                 *(self._path(role) for role in ROLES)]
        for path in paths:
            if any(p.is_symlink() for p in (path, *path.parents)):
                raise ValueError("queue paths must not contain symlinks")

    @contextmanager
    def _locked(self):
        # Share CompletionMap's lazy fcntl lock, including its atomic dispatch write.
        self._check_paths()
        with self.completion._locked():
            self._check_paths()
            yield

    def _read(self, role):
        path = self._path(role)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        try:
            rows = [json.loads(line) for line in lines]
            for row in rows:
                if (not isinstance(row, dict) or not isinstance(row.get("node"), str)
                        or type(row.get("priority")) is not int
                        or type(row.get("t")) is not int):
                    raise ValueError("invalid queue row")
            return rows
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"Invalid queue file: {path.name}") from exc

    def _write(self, role, rows):
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                             dir=self.folder, delete=False) as out:
                name = out.name
                for row in rows:
                    out.write(json.dumps(row) + "\n")
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self._path(role))
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    def enqueue(self, node: str, dag):
        item = dag.node(node)
        role = "intern" if item["intern_friendly"] else "engineer"
        priority = list(dag._nodes).index(node)
        with self._locked():
            queues = {r: self._read(r) for r in ROLES}
            state = self.completion._read()
            if state.get(node, {}).get("status") in ("done", "dispatched"):
                return
            if any(row["node"] == node for rows in queues.values() for row in rows):
                return
            queues[role].append(dict(node=node, priority=priority, t=time.time_ns()))
            self._write(role, queues[role])

    def take(self, role: str):
        if role not in ROLES or (self._caller_role == "intern" and role != "intern"):
            _deny()
        with self._locked():
            state = self.completion._read()
            for queue in (("engineer", "intern") if role == "engineer" else ("intern",)):
                rows = self._read(queue)
                active = [r for r in rows if state.get(r["node"], {}).get("status")
                          not in ("done", "dispatched")]
                if not active:
                    if rows:
                        self._write(queue, [])
                    continue
                selected = min(active, key=lambda r: (r["priority"], r["t"]))
                node = selected["node"]
                state[node] = {**state.get(node, {}), "status": "dispatched",
                               "t": datetime.now(UTC).isoformat()}
                # Persist dispatch first: a crash cannot dispatch the leftover row twice.
                self.completion._write(state)
                self._write(queue, [r for r in active if r["node"] != node])
                return node
        return None
