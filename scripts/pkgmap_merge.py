#!/usr/bin/env python3
"""Git merge driver for docs/architecture/packages.yaml (the package map).

Most packages add one `      - path` line to the map, so two branches that add entries at the same spot conflict and
a batched train goes red (batch-074f2676, 2026-10-23). This driver runs git's normal three-way merge and resolves only
one kind of conflict: both sides inserted list entries at the same place (the base side of the hunk is empty) and
every inserted line is a `      - <path>` entry. Those resolve to ours then theirs, deduplicated. Any other conflict
(an edited or removed line, a new package header, a key) keeps its markers and the merge refuses as before.
Union merge was rejected: it would also accept two edits of one line and could put an entry under the wrong header.

Wired by .gitattributes (`merge=pkgmap`) plus GIT_CONFIG below, which train.py and landing_runner.py pass to git;
with no driver configured git falls back to its built-in merge.
Usage (git calls it): pkgmap_merge.py %O %A %B
"""
import re
import shlex
import subprocess
import sys
from pathlib import Path

ENTRY = re.compile(r"^ {6}- \S+$")
GIT_CONFIG = ("-c", "merge.pkgmap.name=package map entry merge", "-c",
              "merge.pkgmap.driver=" + " ".join(shlex.quote(part) for part in (
                  sys.executable, str(Path(__file__).resolve()), "%O", "%A", "%B")))


def git_env(env):
    """Return env plus the driver config as GIT_CONFIG_* (for git calls that take no -c, such as a bare `merge`)."""
    env = dict(env)
    pairs =[item.split("=", 1) for item in GIT_CONFIG[1::2]]
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for index, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{index}"], env[f"GIT_CONFIG_VALUE_{index}"] = key, value
    return env


def resolve(merged):
    """Resolve diff3 conflict hunks that only add map entries on both sides; return (text, conflicts left)."""
    out, hunk, left = [], None, 0
    for line in merged.splitlines(keepends=True):
        if hunk is None:
            if line.startswith("<<<<<<< "):
                hunk = {"ours": [], "base": [], "theirs": [], "raw": [line], "part": "ours"}
            else:
                out.append(line)
            continue
        hunk["raw"].append(line)
        if line.startswith("||||||| ") and hunk["part"] == "ours":
            hunk["part"] = "base"
        elif line.startswith("=======") and hunk["part"] in ("ours", "base"):
            hunk["part"] = "theirs"
        elif line.startswith(">>>>>>> ") and hunk["part"] == "theirs":
            added = hunk["ours"] + hunk["theirs"]
            if not hunk["base"] and added and all(ENTRY.match(item.rstrip("\n")) for item in added):
                out.extend(dict.fromkeys(item if item.endswith("\n") else item + "\n" for item in added))
            else:
                out.extend(hunk["raw"])
                left += 1
            hunk = None
        else:
            hunk[hunk["part"]].append(line)
    if hunk is not None:  # an unterminated hunk is not ours to judge
        out.extend(hunk["raw"])
        left += 1
    return "".join(out), left


def main(argv):
    base, ours, theirs = argv[1:4]
    merged = subprocess.run(["git", "merge-file", "-p", "--diff3", "-L", "ours", "-L", "base", "-L", "theirs",
                             ours, base, theirs], capture_output=True, text=True, check=False)
    if merged.returncode < 0 or merged.returncode > 127:
        return 2
    text, left = resolve(merged.stdout) if merged.returncode else (merged.stdout, 0)
    Path(ours).write_text(text)
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
