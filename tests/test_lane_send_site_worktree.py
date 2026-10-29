"""The Claude courier takes a worktree of an allowlisted non-Kyra repo (night 2026-10-17: the site's S0). Hermetic."""

import importlib.util
import subprocess
from pathlib import Path

from companion.feature_team import repos

REPO = Path(__file__).resolve().parents[1]


def _lane_send():
    spec = importlib.util.spec_from_file_location("lane_send_site", REPO / "scripts/lane_send.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repo(path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "--allow-empty", "-m", "base"], check=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@example.com", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"})
    return path


def test_a_site_worktree_is_allowed_and_a_stray_repo_is_not(tmp_path, monkeypatch):
    lane = _lane_send()
    site, stray = _repo(tmp_path / "site"), _repo(tmp_path / "stray")
    for repo in (site, stray):
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "w", str(repo.parent / f"{repo.name}-wt")],
                       check=True)
    real = repos.allowed()
    monkeypatch.setattr(repos, "allowed", lambda: {**real, "example-site": repos.Repo(
        "example-site", site, "main", tmp_path / "wts", "board", ("claude",))})
    assert lane._is_allowed_worktree((tmp_path / "site-wt").resolve())
    assert not lane._is_allowed_worktree((tmp_path / "stray-wt").resolve())
    assert not lane._is_allowed_worktree(site.resolve())  # the site's main checkout itself is never a worker folder
