"""Runner bootstrap: probe this machine once, write one registry entry.

``bootstrap`` runs ``companion.capability_probe.probe`` and writes
``<registry_dir>/<runner_id>.json`` atomically (temp file, then replace),
so a rerun replaces only that runner's entry and a killed writer never
leaves a torn file. The entry holds the probe facts, the
``Capabilities`` mapping placement consumes, the probe time and the
repo sha. ``load_registry`` reads every entry back as
``{runner_id: Capabilities}`` for ``test_markers.place``.

A new machine or toolchain joins by running this once: the runner id
comes from the ``runner_id`` argument, else the ``KYRA_RUNNER_ID``
environment variable, else a stable hash of the hardware-ish probe
facts (OS, architecture, cores, RAM, GPU names), so the same machine
keeps the same id across reruns without anything that names it being
stored. The default registry directory is ``<data root>/runners.d``
via ``companion.claims.data_root``. Every external call is injected
(``probe_fn``, ``run``, ``env``, ``now``); standard library only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from companion.capability_probe import probe as _probe
from companion.capability_probe import run_command, to_capabilities
from companion.claims import data_root
from companion.test_markers import Capabilities

RUNNER_ID_ENV = "KYRA_RUNNER_ID"
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


def default_registry_dir() -> Path:
    return data_root() / "runners.d"


def _check_runner_id(runner_id: str) -> str:
    if not _SAFE_ID.fullmatch(runner_id) or runner_id in (".", ".."):
        raise ValueError(f"runner_id must be a plain file name, got {runner_id!r}")
    return runner_id


def stable_runner_id(facts: Mapping) -> str:
    """Stable id from hardware-ish facts only: same machine, same id."""
    basis = {
        "os": facts.get("os", ""),
        "arch": facts.get("arch", ""),
        "cores": facts.get("cores", 0),
        "ram_gb": facts.get("ram_gb", 0),
        "gpus": sorted(str(gpu.get("name", "")) for gpu in facts.get("gpus", [])),
    }
    digest = hashlib.sha256(json.dumps(basis, sort_keys=True).encode("utf-8")).hexdigest()
    return f"runner-{digest[:12]}"


def bootstrap(
    repo: str | Path = ".",
    registry_dir: str | Path | None = None,
    runner_id: str | None = None,
    *,
    probe_fn: Callable[..., dict] | None = None,
    run: Callable[[list[str]], str | None] | None = None,
    env: Mapping[str, str] | None = None,
    now: Callable[[], float] | None = None,
    **probe_kwargs,
) -> Path:
    """Probe ``repo``'s machine and write its registry entry; return its path.

    ``probe_kwargs`` go to the probe (``which``, ``loopback_check``, ...)
    when ``probe_fn`` is not given. ``now`` returns epoch seconds.
    """
    run = run_command if run is None else run
    env = os.environ if env is None else env
    now = time.time if now is None else now
    repo = Path(repo)
    registry = default_registry_dir() if registry_dir is None else Path(registry_dir)

    facts = probe_fn(repo) if probe_fn is not None else _probe(repo, run=run, env=env, **probe_kwargs)
    runner_id = _check_runner_id(runner_id or env.get(RUNNER_ID_ENV) or stable_runner_id(facts))
    stamp = float(now())
    entry = {
        "runner_id": runner_id,
        "probed_at": datetime.fromtimestamp(stamp, tz=UTC).isoformat(),
        "probe_time": stamp,
        "repo_sha": (run(["git", "-C", str(repo), "rev-parse", "HEAD"]) or "").strip(),
        "capabilities": facts["capabilities"],
        "facts": {key: value for key, value in facts.items() if key != "capabilities"},
    }
    registry.mkdir(parents=True, exist_ok=True)
    path = registry / f"{runner_id}.json"
    tmp = registry / f".{runner_id}.json.tmp"
    tmp.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_registry(registry_dir: str | Path | None = None) -> dict[str, Capabilities]:
    """Every registry entry as ``{runner_id: Capabilities}``, sorted by id.

    A missing directory is an empty registry. A malformed entry raises
    ``ValueError`` naming the file: losing a runner silently would place
    its tests nowhere instead of failing loudly.
    """
    registry = default_registry_dir() if registry_dir is None else Path(registry_dir)
    runners: dict[str, Capabilities] = {}
    if not registry.is_dir():
        return runners
    for path in sorted(registry.glob("*.json")):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            runners[path.stem] = to_capabilities(entry)
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError(f"bad registry entry {path.name}: {exc}") from exc
    return runners
