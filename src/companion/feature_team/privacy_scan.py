"""Personal-data scan of a public repo's node diff (2026-10-17): AGENTS.md rule 2 as a post-condition.

Workers on the example-site were only told to grep for personal data before committing. Verify now runs this scan
on the lines a node's commit adds (``base..sha``) and fails the node on a hit. The site's own check
(tools/check_security.py) audits the built pages at rest, not a diff, so it cannot gate a commit.

Patterns: an email anchored on word boundaries whose last label is not a file extension (so ``icon@2x.png`` passes),
and a phone number only when it has separators or a leading plus (so a bare 10 to 15 digit id or timestamp passes).
Every added line is scanned, binary or not. The site publishes no email or phone (its mailto link is assembled by script), so there is
no allowlist.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ASSET_EXT = {"png", "jpg", "jpeg", "gif", "webp", "avif", "svg", "ico", "bmp", "css", "js", "mjs", "map", "json",
             "woff", "woff2", "ttf", "otf", "mp4", "webm", "pdf", "html", "txt"}
EMAIL = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.([A-Za-z]{2,})\b")
PHONE = re.compile(
    r"(?<![\w.@/+-])(?:(?:\+\d{1,3}|1)[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?![\w-])"  # US style, separated
    r"|(?i:tel:|callto:|sms:)\+?\d[\d\s().-]{6,}\d"  # a tel: link names a phone even as a bare digit run
    r"|(?<![\w.@/+-])0\d{3}[\s.-]\d{3}[\s.-]\d{3}(?![\w-])"  # Vietnamese local, 4-3-3
    r"|(?<![\w.@/+-])\+\d{7,15}(?!\w)"  # E.164 with a leading plus
    r"|(?<![\w.@/+-])\+\d{1,3}(?:[\s.-]\d{2,4}){2,4}(?![\w-])"  # international, separated
)


# NANP shape (area and exchange start 2-9, optional leading 1), so 1760000000 and 2026-10-17-01 pass
NAME_PHONE = re.compile(r"(?<!\d)1?[2-9]\d\d[-_./ ]?[2-9]\d\d[-_./ ]?\d{4}(?!\d)")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, errors="replace")


def scan_text(text: str) -> list:
    """Kinds found in ``text``: 'email' and/or 'phone', each with the matched string."""
    found = [("email", m.group(0)) for m in EMAIL.finditer(text) if m.group(1).lower() not in ASSET_EXT]
    found += [("phone", m.group(0)) for m in PHONE.finditer(text)]
    return found


def _name_hits(name: str) -> list:
    """Kinds in a file NAME. Separators (- _ . space) between digits are ignored, so call-415-555-0134.txt and
    call-4155550134.txt are phones while icon@2x.png, v10.0.19041 and long ids are not."""
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name)  # an email named file keeps its address when .txt is cut off
    kinds = {k for n in (name, stem, name.replace("_", " ")) for k, _ in scan_text(n)}
    if NAME_PHONE.search(re.sub(r"[()]", " ", name)):
        kinds.add("phone")
    return sorted(kinds)


def scan(repo: Path, base, sha: str) -> list:
    """Hits as (file number, kind, added line number) on what ``sha`` adds beyond ``base``. The file number is the
    1-based position in the sorted list of added or changed paths (deletions are not listed); a name hit has kind
    'name:email' or 'name:phone' and line 0. No path or name is ever returned: the verdict travels to events, the Board
    inbox and cloud briefs. A base that does not resolve falls back to the commit's own change set. A diff or name
    list git cannot produce is itself reported as a hit: fail closed."""
    start = None
    if base:
        found = _git(repo, "merge-base", str(base), sha)
        start = found.stdout.strip() if found.returncode == 0 else None
        if start and _git(repo, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}").stdout.strip() == start:
            start = None  # an empty range (base is the commit or already contains it): scan the commit's own change
    flags = ("-c", "core.quotePath=false", "diff", "--no-renames", "--text", "--no-ext-diff", "--no-textconv", "--no-color")
    if start:
        span = (start, sha)
    elif _git(repo, "rev-parse", "--verify", "--quiet", f"{sha}^1").returncode == 0:
        span = (f"{sha}^1", sha)  # first-parent diff: a merge's whole change, never a combined diff
    else:
        span = ("4b825dc642cb6eb9a060e54bf8d69288fbee4904", sha)  # a root commit: against the empty tree
    unreadable = [(0, "unreadable", 0)]
    listing = subprocess.run(["git", "-C", str(repo), *flags, "--name-only", "-z", "--diff-filter=d", *span],
                             capture_output=True)
    if listing.returncode != 0:
        return unreadable
    names = sorted(os.fsdecode(n) for n in listing.stdout.split(b"\0") if n)  # NUL-separated: no quoting to undo
    hits = []
    for number, name in enumerate(names, 1):
        hits += [(number, f"name:{kind}", 0) for kind in _name_hits(name)]
        # one file per diff: no header to parse; bytes, not text mode, so a bare CR is not turned into a line break
        out = subprocess.run(["git", "-C", str(repo), *flags, "-U0", *span, "--", f":(literal){name}"], capture_output=True)
        if out.returncode != 0:
            return unreadable
        in_hunk = False
        new_line = 0
        for line in out.stdout.decode("utf-8", "replace").split("\n"):
            if line.startswith("@@"):
                in_hunk = True
                m = re.search(r"\+(\d+)", line)
                new_line = int(m.group(1)) - 1 if m else 0
            elif in_hunk and line.startswith("+"):  # --text: NUL bytes are scanned too, nothing is skipped as binary
                new_line += 1
                for kind in dict.fromkeys(k for k, _ in scan_text(line[1:])):
                    hits.append((number, kind, new_line))  # never the match or the line
    return hits


def failure(hits: list) -> str:
    """Counts and positions only: '#n' is the file's place in the sorted changed paths, never its name."""
    def one(n, what, line):
        if what == "unreadable":
            return "diff unreadable"
        if what.startswith("name:"):
            return f"{what[5:]} in file name #{n}"
        return f"{what} in file #{n} line {line}"
    shown = "; ".join(one(*h) for h in hits[:5]) + ("; ..." if len(hits) > 5 else "")
    return f"privacy scan: public-repo diff adds personal data, {len(hits)} hit(s) ({shown})"
