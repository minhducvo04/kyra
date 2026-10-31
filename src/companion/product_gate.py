"""Brownfield gate: a brief for an existing product must name its history before dispatch.

the owner 2026-10-22: the site was rebuilt from scratch three times because briefs never read the product's design-state
file, started from an old main, and skipped the keep-check. The registry (data/config/products.json, or
KYRA_PRODUCTS_FILE) maps product id -> {"repo", "state_file", "base_branch", "keep_check", "match"}. A brief whose
worktree is under a product repo, or whose text matches one of the product's patterns, is refused unless its text
contains the state_file path, the base_branch name and the keep_check command, all literally.

A missing registry file means no products are registered (nothing to check); a malformed one is a refusal.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
REQUIRED = ("repo", "state_file", "base_branch", "keep_check")


def registry_path(data_root: Path | None = None) -> Path:
    env = os.environ.get("KYRA_PRODUCTS_FILE")
    if env:
        return Path(env).expanduser()
    root = data_root or Path(os.environ.get("KYRA_DATA_DIR") or PROJECT_ROOT / "data")
    return Path(root) / "config/products.json"


def load_registry(path: Path) -> dict[str, dict]:
    """Product id -> entry. Raises ValueError on a malformed file or entry."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"product registry {path} is unreadable: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError(f"product registry {path} must be a JSON object of product id -> entry")
    for pid, entry in data.items():
        missing = [k for k in REQUIRED if not isinstance(entry, dict) or not str(entry.get(k) or "").strip()]
        if missing or not isinstance(entry.get("match", []), list):
            raise ValueError(f"product registry {path}: {pid} needs {', '.join(REQUIRED)} and a match list")
    return data


def _repo_roots(cwd: Path) -> list[Path]:
    """The folder itself plus the main checkout its git common dir points at (a worktree outside the repo tree)."""
    roots = [cwd]
    try:
        out = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                             capture_output=True, text=True, timeout=10, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out = ""
    if out:
        roots.append(Path(out).resolve().parent)
    return roots


def _touches(entry: dict, text: str, cwd: Path | None) -> bool:
    if cwd is not None and Path(cwd).is_dir():
        repo = Path(entry["repo"]).expanduser().resolve()
        if any(root == repo or root.is_relative_to(repo) for root in _repo_roots(Path(cwd).resolve())):
            return True
    lowered = text.lower()
    return any(str(p).strip() and str(p).strip().lower() in lowered for p in entry.get("match", []))


def check_brief(text: str, cwd: Path | str | None, *, path: Path | None = None) -> list[str]:
    """Refusal reasons for this brief; an empty list means it may be sent."""
    path = path or registry_path()
    try:
        registry = load_registry(path)
    except ValueError as exc:
        return [str(exc)]
    reasons = []
    for pid, entry in registry.items():
        if not _touches(entry, text, Path(cwd).expanduser() if cwd else None):
            continue
        for key, label in (("state_file", "design-state file"), ("base_branch", "base branch"),
                           ("keep_check", "keep-check command")):
            if entry[key] not in text:
                reasons.append(f"{pid}: brief touches a registered product but does not name its {label} "
                               f"{entry[key]!r} (brownfield check, lane-routing skill)")
    return reasons
