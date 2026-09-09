"""A shared session record, so two agents hand off through a file instead of the user.

Claude Code and Codex both work in this repo, on the same machine, and the
workflow (`docs/agent-workflow.md`) hands a branch from one to the other. Until
now the hand-off was a block of text Duc copied from one chat into the other:
lossy, and a chore that scales with the number of hand-offs.

Both agents can read the same directory, so the file *is* the channel. One
thread per branch, because the branch is already the unit of work: the same
slice appends, a different slice gets its own file. Records live under
`DATA_DIR` and are therefore gitignored - a session record names branches, test
counts and sometimes what a real run wrote, none of which belongs in a public
repo. The branch is percent-encoded in the filename so the mapping is safe and
collision-free.

Append-only, like `memory_notes`: an agent adds a block and never edits an
earlier one, so the thread reads as what actually happened rather than as a
tidied summary. The mechanical half of each block (branch, head, commits ahead,
tree state) is measured here rather than typed by the agent, because a hand-off
whose facts are recalled is a hand-off the receiver has to re-check anyway.
"""
import re
import subprocess
from datetime import datetime
from pathlib import Path

from companion.paths import DATA_DIR, PROJECT_ROOT

SESSIONS_DIR = DATA_DIR / "sessions"

AGENTS = ("claude", "codex", "duc")

# What the next actor is being asked for. A free-text field here would defeat
# the point: this is the routing decision, and the receiving agent reads it to
# know whether it is being handed a critique, a test-writing job, a build or a
# review. The phases are `docs/agent-workflow.md`'s.
OPEN_FOR = ("critique", "tests", "build", "review", "duc", "nothing")


def slug(branch: str) -> str:
    """A filename that stays inside SESSIONS_DIR whatever the branch is called.

    Branch names legitimately contain slashes (`session/2026-09-09-topic`), and
    git permits enough besides that a name could otherwise climb out of the
    directory. Percent-encoding every byte outside the filename-safe set is
    injective: unlike replacing characters with underscores, two branch names
    can never map to one thread.
    """
    safe = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    return "".join(chr(byte) if byte in safe else f"%{byte:02X}" for byte in branch.encode()) or "unnamed"


def _git(*args: str, cwd: Path | None = None, strip: bool = True) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(cwd or PROJECT_ROOT),
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.strip() if strip else out.stdout


def _status_paths(cwd: Path | None = None) -> list[list[str]]:
    """Changed paths grouped by porcelain entry, including both sides of a rename."""
    records = _git("status", "--porcelain=v1", "-z", cwd=cwd, strip=False).rstrip("\0").split("\0")
    if records == [""]:
        return []
    entries = []
    i = 0
    while i < len(records):
        record = records[i]
        status = record[:2]
        paths = [record[3:]]
        if "R" in status or "C" in status:
            i += 1
            paths.append(records[i])
        entries.append(paths)
        i += 1
    return entries


def current_branch(cwd: Path | None = None) -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd) or "unknown"


def facts(cwd: Path | None = None, base: str = "master") -> dict[str, str]:
    """The half of a hand-off block that must never be typed from memory."""
    branch = current_branch(cwd)
    head = _git("rev-parse", "--short", "HEAD", cwd=cwd) or "unknown"
    subject = _git("log", "-1", "--format=%s", cwd=cwd)
    ahead = _git("rev-list", "--count", f"{base}..HEAD", cwd=cwd)
    dirty = _status_paths(cwd)
    private = [
        path for paths in dirty for path in paths
        if path.startswith(("data/", ".env")) and path != ".env.example"
    ]
    return {
        "branch": branch,
        "head": head,
        "subject": subject or "(none)",
        "ahead": ahead or "0",
        "tree": "clean" if not dirty else f"{len(dirty)} uncommitted",
        # Named in the block so a reader can see the check ran, not assume it.
        "private": "none staged or modified" if not private else "ATTENTION: " + ", ".join(private),
    }


def render(agent: str, open_for: str, body: str, *, next_up: str = "", suggest: str = "",
           now: datetime | None = None, cwd: Path | None = None) -> str:
    """One block. `body` is the message to whoever `open_for` names.

    `next_up` and `suggest` render even when empty, as "(not stated)". A missing
    hand-over is then visible in the thread instead of being something the
    reader has to notice is absent - the same reason a skipped step is reported
    rather than quietly dropped.
    """
    if agent not in AGENTS:
        raise ValueError(f"agent must be one of {AGENTS}, got {agent!r}")
    if open_for not in OPEN_FOR:
        raise ValueError(f"open_for must be one of {OPEN_FOR}, got {open_for!r}")
    f = facts(cwd)
    stamp = (now or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M")
    return "\n".join([
        f"## {agent} - {stamp}",
        "",
        f"- Branch `{f['branch']}` at `{f['head']}` ({f['subject']})",
        f"- {f['ahead']} commit(s) ahead of master, working tree {f['tree']}",
        f"- Private paths: {f['private']}",
        f"- **Open for: {open_for}**",
        f"- Next: {next_up.strip() or '(not stated)'}",
        f"- Suggested: {suggest.strip() or '(not stated)'}",
        "",
        body.strip() or "_(no notes)_",
        "",
    ])


def append(agent: str, open_for: str, body: str, *, next_up: str = "", suggest: str = "",
           branch: str | None = None, now: datetime | None = None, cwd: Path | None = None) -> Path:
    branch = branch or current_branch(cwd)
    path = SESSIONS_DIR / f"{slug(branch)}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "" if path.exists() else f"# Session thread: `{branch}`\n\nAppend-only. Newest block last. See `docs/agent-workflow.md`.\n\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(header + render(agent, open_for, body, next_up=next_up, suggest=suggest, now=now, cwd=cwd) + "\n")
    return path


def read(branch: str | None = None, cwd: Path | None = None) -> str:
    path = SESSIONS_DIR / f"{slug(branch or current_branch(cwd))}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def threads() -> list[tuple[str, str, str]]:
    """(name, last modified, the 'Open for' of the last block), newest first."""
    if not SESSIONS_DIR.exists():
        return []
    rows = []
    for p in SESSIONS_DIR.glob("*.md"):
        text = p.read_text(encoding="utf-8", errors="ignore")
        opens = re.findall(r"\*\*Open for: (\w+)\*\*", text)
        when = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        rows.append((p.stem, when, opens[-1] if opens else "?"))
    return sorted(rows, key=lambda r: r[1], reverse=True)
