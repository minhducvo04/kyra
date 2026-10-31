"""Per-agent lessons log: what went wrong, the rule that now prevents it, and the check that catches it.

One markdown file per agent, data/team/lessons/<agent>.md, one line per lesson:
    - 2026-10-12 | RQ75 | mistake | rule | check
A mistake already logged keeps its one lesson line; each later occurrence appends an occurrence line instead,
    + 2026-10-14 | RQ80 | mistake
which read() skips (it reads only "- " lines), so briefs show each lesson once while weekly() counts every occurrence.
The Board adds a line when a fix is routed back, a review is FIX-FIRST or the nightly reverts a merge. rq_brief.py puts the
last five lines of an agent's file into that agent's next brief. A lesson names packages and rules only: never private
text (health or family wording, a path, an address), which add() refuses. Agent names are the lane names in lane-routing,
as slugs ("Claude lane 0" is claude-lane-0).
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta
from pathlib import Path

from companion import health_words
from companion.paths import DATA_DIR

FIELDS = ("date", "package", "mistake", "rule", "check")
MAX_FIELD = 200
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
_PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,40}")
_PRIVATE = re.compile(r"/Users/|<user-home>/|\.env\b|@[\w-]+\.[a-z]{2,}|\b\d{3}[-. ]\d{3}[-. ]\d{4}\b|\bsk-[A-Za-z0-9]{8,}")


def default_root() -> Path:
    return Path(DATA_DIR) / "team" / "lessons"


def slug(name: str) -> str:
    """'Claude lane 0' -> 'claude-lane-0'. ValueError when nothing safe is left."""
    out = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    if not _SLUG.fullmatch(out):
        raise ValueError("agent name must be a short lane name")
    return out


def _path(agent: str, root: Path | None) -> Path:
    return (Path(root) if root is not None else default_root()) / f"{slug(agent)}.md"


def _clean(field: str, value) -> str:
    text = " ".join(str(value).split())
    if not text or len(text) > MAX_FIELD or "|" in text:
        raise ValueError(f"{field} must be 1 to {MAX_FIELD} characters and contain no '|'")
    if _PRIVATE.search(text) or health_words.is_health(text):
        raise ValueError(f"{field} must name packages and rules only, never private text")
    return text



def normalized(value: str) -> str:
    """Compare wording without case, spacing or terminal punctuation."""
    value = " ".join(value.casefold().split())
    while value and (value[-1].isspace() or unicodedata.category(value[-1]).startswith("P")):
        value = value[:-1]
    return value


def add(agent: str, package: str, mistake: str, rule: str, check: str, *, on: date | None = None, root: Path | None = None) -> Path:
    """Append a new mistake, or an occurrence line when the mistake is already a lesson (once per package, so a replay
    adds nothing). ValueError for a bad agent, a bad field or private text; nothing is written then."""
    if not _PACKAGE.fullmatch(str(package)):
        raise ValueError("package must look like RQ75")
    mistake = _clean("mistake", mistake)
    day = (on or date.today()).isoformat()
    line = "- " + " | ".join([day, str(package), mistake, _clean("rule", rule), _clean("check", check)]) + "\n"
    path = _path(agent, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    import fcntl  # Keep module imports portable; serialize the search and append.

    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            key = normalized(mistake)
            seen = [r for r in read(agent, root=root) + occurrences(agent, root=root) if normalized(r["mistake"]) == key]
            if seen:
                line = "+ " + " | ".join([day, str(package), mistake]) + "\n"
            if not any(r["package"] == str(package) for r in seen):
                with path.open("a", encoding="utf-8") as f:
                    f.write(line)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return path


def read(agent: str, *, root: Path | None = None) -> list[dict]:
    """Every well-formed lesson of one agent, oldest first. A missing file is an empty list; bad lines are skipped."""
    path = _path(agent, root)
    if not path.is_file():
        return []
    rows = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.startswith("- "):
            continue
        parts = [p.strip() for p in raw[2:].split(" | ")]
        if len(parts) != len(FIELDS):
            continue
        row = dict(zip(FIELDS, parts, strict=True))
        try:
            row["day"] = date.fromisoformat(row["date"])
        except ValueError:
            continue
        rows.append(row)
    return rows


def occurrences(agent: str, *, root: Path | None = None) -> list[dict]:
    """Every well-formed repeat ('+ date | package | mistake') of one agent, oldest first."""
    path = _path(agent, root)
    if not path.is_file():
        return []
    rows = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = [p.strip() for p in raw[2:].split(" | ")] if raw.startswith("+ ") else []
        if len(parts) != 3:
            continue
        try:
            rows.append({"date": parts[0], "package": parts[1], "mistake": parts[2], "day": date.fromisoformat(parts[0])})
        except ValueError:
            continue
    return rows


def recent(agent: str, n: int = 5, *, root: Path | None = None) -> list[dict]:
    return read(agent, root=root)[-n:]


def brief_section(agent: str, n: int = 5, *, root: Path | None = None) -> str:
    """'## Lessons (last 5)' for a brief, or '' when the agent has no lessons."""
    rows = recent(agent, n, root=root)
    if not rows:
        return ""
    lines = [f"- {r['date']} {r['package']}: {r['mistake']}. Rule: {r['rule']}. Check: {r['check']}." for r in rows]
    return f"## Lessons (last {n})\n" + "\n".join(lines) + "\n"


def agents(*, root: Path | None = None) -> list[str]:
    base = Path(root) if root is not None else default_root()
    return sorted(p.stem for p in base.glob("*.md")) if base.is_dir() else []


def weekly(*, today: date | None = None, root: Path | None = None) -> list[dict]:
    """Per agent and rule, how many times it happened in the last 7 days: lesson lines plus the occurrence lines of their
    mistakes (an occurrence counts under its lesson's rule). A rule logged twice or more needs RQ86."""
    end = today or date.today()
    start = end - timedelta(days=6)
    rows = []
    for agent in agents(root=root):
        counts: dict[str, list[dict]] = {}
        lessons = read(agent, root=root)
        rule_of: dict[str, str] = {}
        for r in lessons:
            rule_of.setdefault(normalized(r["mistake"]), r["rule"])
        repeats = [dict(o, rule=rule_of[normalized(o["mistake"])]) for o in occurrences(agent, root=root)
                   if normalized(o["mistake"]) in rule_of]
        for r in lessons + repeats:
            if start <= r["day"] <= end:
                counts.setdefault(r["rule"], []).append(r)
        for rule, hits in counts.items():
            rows.append({"agent": agent, "rule": rule, "count": len(hits), "packages": sorted({h["package"] for h in hits}),
                         "repeat": len(hits) >= 2})
    return sorted(rows, key=lambda r: (-r["count"], r["agent"], r["rule"]))


def weekly_table(rows: list[dict]) -> str:
    """The markdown table for docs/model-fit.md."""
    if not rows:
        return "No lessons logged in the last 7 days.\n"
    out = ["| agent | rule | times (7 days) | packages | next |", "|---|---|---:|---|---|"]
    for r in rows:
        out.append(f"| {r['agent']} | {r['rule']} | {r['count']} | {', '.join(r['packages'])} | "
                   f"{'hand to RQ86' if r['repeat'] else 'logged'} |")
    return "\n".join(out) + "\n"

