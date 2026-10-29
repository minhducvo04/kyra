"""Two branches that add paths to packages.yaml merge without a conflict.

Landing batches split when every package edits docs/architecture/packages.yaml.
.gitattributes sets merge=pkgmap on that file (scripts/pkgmap_merge.py, the driver the train and landing runner
pass to git) so added list entries from both sides are kept. Guard tests in test_package_map.py stay unchanged.
"""
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from pkgmap_merge import GIT_CONFIG  # noqa: E402

MERGE_RULE = "docs/architecture/packages.yaml merge=pkgmap"

BASE_MAP = """version: 1
roots: [src/]
packages:
  core-loop:
    public: true
    include:
      - src/a.py
      - src/b.py
  memory-graph:
    public: true
    include:
      - src/m.py
"""


def _git_env() -> dict[str, str]:
    keep = {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"}
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k in keep}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *GIT_CONFIG,
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        env=_git_env(),
    )


def _insert_after(text: str, after: str, line: str) -> str:
    needle = after + "\n"
    assert needle in text
    return text.replace(needle, after + "\n" + line + "\n", 1)


def _write_map(repo: Path, text: str) -> None:
    path = repo / "docs" / "architecture" / "packages.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _commit(repo: Path, message: str) -> None:
    done = _git(repo, "add", ".")
    assert done.returncode == 0, done.stderr
    done = _git(repo, "commit", "-q", "-m", message)
    assert done.returncode == 0, done.stderr


def _merge_pair(tmp_path: Path, ours: str, theirs: str) -> tuple[subprocess.CompletedProcess[str], str, dict]:
    attributes = (ROOT / ".gitattributes").read_text()
    assert MERGE_RULE in attributes.splitlines()
    repo = tmp_path / "repo"
    repo.mkdir()
    started = _git(repo, "init", "-q", "-b", "main")
    assert started.returncode == 0, started.stderr
    (repo / ".gitattributes").write_text(attributes)
    _write_map(repo, BASE_MAP)
    _commit(repo, "base")
    assert _git(repo, "checkout", "-q", "-b", "ours").returncode == 0
    _write_map(repo, ours)
    _commit(repo, "ours")
    assert _git(repo, "checkout", "-q", "main").returncode == 0
    assert _git(repo, "checkout", "-q", "-b", "theirs").returncode == 0
    _write_map(repo, theirs)
    _commit(repo, "theirs")
    assert _git(repo, "checkout", "-q", "ours").returncode == 0
    merged = _git(repo, "merge", "--no-edit", "theirs")
    body = (repo / "docs" / "architecture" / "packages.yaml").read_text()
    assert "<<<<<<<" not in body
    parsed = yaml.safe_load(body)
    return merged, body, parsed


def test_same_package_different_paths_merge_without_conflict(tmp_path):
    ours = _insert_after(BASE_MAP, "      - src/b.py", "      - src/c.py")
    theirs = _insert_after(BASE_MAP, "      - src/b.py", "      - src/d.py")
    merged, body, parsed = _merge_pair(tmp_path, ours, theirs)
    assert merged.returncode == 0, merged.stdout + merged.stderr
    include = parsed["packages"]["core-loop"]["include"]
    assert "src/c.py" in include
    assert "src/d.py" in include
    assert "CONFLICT" not in merged.stdout
    assert "<<<<<<<" not in body


def test_different_packages_different_paths_merge_without_conflict(tmp_path):
    ours = _insert_after(BASE_MAP, "      - src/b.py", "      - src/c.py")
    theirs = _insert_after(BASE_MAP, "      - src/m.py", "      - src/n.py")
    merged, body, parsed = _merge_pair(tmp_path, ours, theirs)
    assert merged.returncode == 0, merged.stdout + merged.stderr
    assert "src/c.py" in parsed["packages"]["core-loop"]["include"]
    assert "src/n.py" in parsed["packages"]["memory-graph"]["include"]
    assert "CONFLICT" not in merged.stdout
    assert "<<<<<<<" not in body


def test_duplicate_same_entry_same_position_keeps_one_line(tmp_path):
    added = _insert_after(BASE_MAP, "      - src/b.py", "      - src/c.py")
    merged, body, parsed = _merge_pair(tmp_path, added, added)
    assert merged.returncode == 0, merged.stdout + merged.stderr
    include = parsed["packages"]["core-loop"]["include"]
    assert include.count("src/c.py") == 1
    assert "<<<<<<<" not in body


def test_duplicate_same_entry_different_positions_stays_valid_yaml(tmp_path):
    ours = _insert_after(BASE_MAP, "      - src/a.py", "      - src/c.py")
    theirs = _insert_after(BASE_MAP, "      - src/b.py", "      - src/c.py")
    merged, body, parsed = _merge_pair(tmp_path, ours, theirs)
    assert merged.returncode == 0, merged.stdout + merged.stderr
    include = parsed["packages"]["core-loop"]["include"]
    assert include.count("src/c.py") == 2
    assert "<<<<<<<" not in body
