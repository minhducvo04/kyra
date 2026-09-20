"""Copy only explicitly selected commit blobs into an empty export directory."""
import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


class ExportError(ValueError):
    """The candidate does not meet the export constraints."""


@dataclass(frozen=True)
class Entry:
    path: str
    sha256: str | None = None


def parse_manifest(text: str) -> list[Entry]:
    entries, seen = [], set()
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if len(fields) not in (1, 2):
            raise ExportError(f"Line {number}: expected path and optional sha256")
        path = fields[0]
        parts = path.split("/")
        lower = path.lower()
        private = ("data", "handoff", ".claude", ".agents", ".git", ".codex")
        if (any(p in ("", ".", "..") for p in parts)
                or any(c in path for c in "*?[]\\:")
                or any(ord(c) < 32 for c in path)
                or parts[0].lower() in private
                or any(p.lower() == ".git" for p in parts)
                or lower in ("agents.md", "claude.md", "start-here.md")
                or lower.startswith(("docs/plans/", "docs/log/"))
                or any(p.lower().startswith(".env") for p in parts) and path != ".env.example"):
            raise ExportError(f"Line {number}: forbidden or nonliteral path")
        if lower in seen:
            raise ExportError(f"Line {number}: duplicate path")
        digest = fields[1].lower() if len(fields) == 2 else None
        if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ExportError(f"Line {number}: invalid sha256")
        if (lower.startswith("docs/") or lower.endswith(".md")) and digest is None:
            raise ExportError(f"Line {number}: document needs approved sha256")
        seen.add(lower)
        entries.append(Entry(path, digest))
    return entries


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True)
    if result.returncode:
        raise ExportError("Cannot read the requested commit or blob")
    return result.stdout


def build_export(repo: Path, commit: str, entries: list[Entry], dest: Path) -> list[str]:
    """Validate all selected blobs before writing; never copy working-tree content or Git history."""
    repo, dest = Path(repo), Path(dest)
    entries = parse_manifest("\n".join(e.path + (" " + e.sha256 if e.sha256 else "") for e in entries))
    if dest.is_symlink() or dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise ExportError("Destination must be an empty directory, not a symlink")
    revision = _git(repo, "rev-parse", "--verify", "--end-of-options", commit + "^{commit}").decode().strip()
    tree = {}
    for record in _git(repo, "ls-tree", "-rz", "--full-tree", revision).split(b"\0"):
        if record:
            info, name = record.split(b"\t", 1)
            tree[name.decode()] = info.decode().split()
    blobs = []
    for entry in entries:
        mode, kind, oid = tree.get(entry.path, ("", "", ""))
        if mode not in ("100644", "100755") or kind != "blob":
            raise ExportError(f"Not a regular committed file: {entry.path}")
        content = _git(repo, "cat-file", "blob", oid)
        if entry.sha256 and hashlib.sha256(content).hexdigest() != entry.sha256:
            raise ExportError(f"Approved content changed: {entry.path}")
        blobs.append((entry.path, mode, content))
    dest.mkdir(parents=True, exist_ok=True)
    for name, mode, content in blobs:
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as output:
            output.write(content)
        target.chmod(int(mode, 8) & 0o777)
    return [e.path for e in entries]
