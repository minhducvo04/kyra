"""Which git repository a feature's nodes are built in (2026-10-15: the example-site DAG's 13 nodes build files of
the site repo, and the loop only made Kyra worktrees, so a seat would have built site files inside Kyra).

A feature's queue entry (data/team/feature-queue.json) or its DAG (top-level "repo") may name a repository: a name
from ALLOWED below or an absolute path under <user-home>/Projects that is one of the ALLOWED paths. Nothing else is accepted,
so a queue edit cannot point a worker at an arbitrary folder. Without a name the feature is Kyra's.

Each repo says where its run worktrees go, which ref a node's work starts from, which channels can work there, and
who lands it. Kyra lands through the landing trains. A non-Kyra repo is "board": its node is done at verify_pass and
landing (and any deploy) is the Board's, never the loop's; AGENTS.md rule 1 still applies, so nothing of the site is
pushed or deployed without the owner yes.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from companion.feature_team.dag import live_path

KYRA = Path(__file__).resolve().parents[3]
PROJECTS = Path.home() / "Projects"


@dataclass(frozen=True)
class Repo:
    name: str
    path: Path
    base: str  # the ref a node's work starts from when it has no input commit
    worktrees: Path  # where its run worktrees go
    landing: str  # "trains" (Kyra's landing trains) or "board" (done at verify_pass; the Board lands)
    channels: tuple[str, ...]  # channel kinds whose courier can work in this repo; empty means every one
    public: bool = False  # its content is published: verify scans a node's diff for emails and phones (privacy_scan)

    @property
    def is_kyra(self):
        return self.name == "kyra"


def _kyra_main():
    """The main checkout that owns this file's checkout (a worktree's .git file points at it)."""
    git = KYRA / ".git"
    if git.is_file():
        text = git.read_text().strip()
        if text.startswith("gitdir:"):
            gitdir = Path(text.split(":", 1)[1].strip())
            return gitdir.parents[2] if gitdir.parent.name == "worktrees" else KYRA
    return KYRA


def allowed():
    """The allowlist, kept in code. The site's couriers: the Claude CLI and grok. Muse and codex-exec accept
    worktrees of the Kyra repo only. A grok site folder must still be a worktree directly under <user-home>/kyra-grok."""
    site = PROJECTS / "example-site"
    return {
        "kyra": Repo("kyra", KYRA, "origin/master", KYRA / ".claude/worktrees", "trains", ()),
        "example-site": Repo("example-site", site, "main", PROJECTS / "example-site-worktrees", "board",
                               ("claude", "grok"), public=True),
    }


def resolve(value):
    """A Repo from a name or an absolute path; None means Kyra. Anything not on the allowlist is refused."""
    table = allowed()
    if value in (None, "", "kyra"):
        return table["kyra"]
    if not isinstance(value, str):
        raise ValueError(f"repo: {value!r} is not a repository name or path")
    if value in table:
        return table[value]
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"repo: {value!r} is neither an allowed name ({', '.join(table)}) nor an absolute path")
    path = path.resolve()
    if not path.is_relative_to(PROJECTS.resolve()):
        raise ValueError(f"repo: {value} is not under {PROJECTS}")
    for repo in table.values():
        if path == repo.path.resolve() or (repo.is_kyra and path == _kyra_main().resolve()):
            return repo
    raise ValueError(f"repo: {value} is not on the allowlist ({', '.join(str(r.path) for r in table.values())})")


def for_feature(data, feature):
    """The feature's repo: its queue entry's "repo", else its DAG's top-level "repo", else Kyra."""
    data = Path(data)
    try:
        entries = json.loads((data / "team/feature-queue.json").read_text()).get("features", [])
    except (OSError, ValueError, AttributeError):
        entries = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") == feature and entry.get("repo"):
            return resolve(entry["repo"])
    try:
        dag = json.loads(live_path(data / "team/feature-teams", feature).read_text())
    except (OSError, ValueError):
        dag = {}
    return resolve(dag.get("repo") if isinstance(dag, dict) else None)


def for_path(path):
    """The allowlisted Repo a checkout path belongs to (Kyra for anything else, as before this module)."""
    path = Path(path).resolve()
    for repo in allowed().values():
        if not repo.is_kyra and path == repo.path.resolve():
            return repo
    return allowed()["kyra"]


def for_worktree(path):
    """The non-Kyra Repo whose run worktrees directory holds ``path``, else None (a Kyra worktree or anything else)."""
    path = Path(path).resolve()
    for repo in allowed().values():
        if not repo.is_kyra and path.is_relative_to(repo.worktrees.resolve()):
            return repo
    return None


def unlanded(data, feature, paths):
    """completion.unlanded judged in the feature's own repo: a non-Kyra repo (the site) has landed a path once its
    base ref (main) holds it; Kyra keeps origin/master or the main checkout's HEAD (2026-10-17: example-site S2 was
    refused every tick on S0's docs/site-survey.md, which was on the site's main but never in Kyra's master)."""
    from companion.feature_team import completion

    try:
        repo = for_feature(data, feature)
    except ValueError:  # a repo off the allowlist: nothing lands, and one bad feature never stops the tick
        return [str(item) for item in paths]
    if repo.is_kyra:
        return completion.unlanded(paths)
    return completion.unlanded(paths, repo.path, refs=(repo.base,))
