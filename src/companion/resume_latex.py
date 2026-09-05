"""Deterministic helpers around a model's LaTeX resume edit.

Two real failures motivated this module (2026-09-05):

1. The Fast one-page path returned Duc's resume with every `%`-commented
   line deleted - his stash of alternate bullets and two archived projects.
   Had he pasted the result over his source, he'd have lost them. The
   prompt now says "leave comment lines alone," but `restore_comments()`
   guarantees it: any comment line from the original that is missing from
   the edit is put back after the nearest surviving anchor line.

2. The same path returned the resume otherwise unchanged and reported
   success ("fits one page"), because the original already fit and the
   loop had no objective beyond fitting. `content_diff()` measures what
   actually changed at the bullet/heading level so the caller can refuse
   to call an unchanged document "tailored."
"""
import re
from dataclasses import dataclass, field

# \resumeItem{...} (the resume template) or a plain \item ... (article-class itemize)
_BULLET_RE = re.compile(r"\\resumeItem\{(.*)\}\s*$|^\s*\\item\s+(.*?)\s*$")
_HEADING_RE = re.compile(r"\\resume(?:Sub|Project)[Hh]eading\s*\{(.*)$")


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("%")


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())


def restore_comments(original: str, edited: str) -> tuple[str, int]:
    """Re-insert any comment-only line from `original` that `edited`
    dropped, placing each after the nearest preceding non-comment line
    of the original that still exists in `edited` (or, if no anchor
    survives, before \\end{document}). Returns (text, restored_count).
    Idempotent: a comment already present is left where it is.
    """
    orig_lines = original.splitlines()
    out_lines = edited.splitlines()
    present = {_norm(line) for line in out_lines if _is_comment(line)}
    anchors = {_norm(line): i for i, line in enumerate(out_lines) if not _is_comment(line) and line.strip()}

    restored = 0
    last_anchor: str | None = None
    # walk the original; remember the most recent non-comment line as the anchor
    pending: list[tuple[str | None, str]] = []
    for line in orig_lines:
        if _is_comment(line):
            if _norm(line) not in present:
                pending.append((last_anchor, line))
        elif line.strip():
            last_anchor = _norm(line)

    # insert from the bottom up so earlier indices stay valid
    inserts: dict[int, list[str]] = {}
    tail: list[str] = []
    for anchor, line in pending:
        idx = anchors.get(anchor) if anchor is not None else None
        if idx is None:
            tail.append(line)
        else:
            inserts.setdefault(idx, []).append(line)
        restored += 1
    result: list[str] = []
    for i, line in enumerate(out_lines):
        result.append(line)
        if i in inserts:
            result.extend(inserts[i])
    if tail:
        end = next((i for i, line in enumerate(result) if line.strip() == r"\end{document}"), len(result))
        result[end:end] = tail
    return "\n".join(result) + ("\n" if edited.endswith("\n") else ""), restored


def _active_items(text: str) -> tuple[list[str], list[str]]:
    bullets, heads = [], []
    for line in text.splitlines():
        if _is_comment(line):
            continue
        m = _BULLET_RE.search(line)
        if m:
            bullets.append(_norm(m.group(1) or m.group(2) or ""))
            continue
        h = _HEADING_RE.search(line)
        if h:
            heads.append(_norm(h.group(1)))
    return bullets, heads


@dataclass
class ContentDiff:
    bullets_kept: int = 0
    bullets_reworded: int = 0  # present in the edit but not verbatim in the original
    bullets_removed: int = 0
    headings_removed: int = 0
    headings_added: int = 0
    order_changed: bool = False
    removed_examples: list[str] = field(default_factory=list)

    @property
    def unchanged(self) -> bool:
        return (
            self.bullets_reworded == 0 and self.bullets_removed == 0
            and self.headings_removed == 0 and self.headings_added == 0 and not self.order_changed
        )

    def summary(self) -> str:
        if self.unchanged:
            return "no content changes - the document came back as it went in"
        parts = [f"{self.bullets_kept} bullet(s) kept verbatim", f"{self.bullets_reworded} reworded"]
        if self.bullets_removed:
            parts.append(f"{self.bullets_removed} removed")
        if self.headings_removed:
            parts.append(f"{self.headings_removed} entry heading(s) removed")
        if self.headings_added:
            parts.append(f"{self.headings_added} entry heading(s) ADDED")
        if self.order_changed:
            parts.append("order changed")
        return ", ".join(parts)


def content_diff(original: str, edited: str) -> ContentDiff:
    """Bullet/heading-level comparison of the ACTIVE (uncommented) content."""
    ob, oh = _active_items(original)
    eb, eh = _active_items(edited)
    oset, eset = set(ob), set(eb)
    d = ContentDiff()
    d.bullets_kept = sum(1 for b in eb if b in oset)
    d.bullets_reworded = sum(1 for b in eb if b not in oset)
    removed = [b for b in ob if b not in eset]
    # a removed original may be a reworded one; count as removed only if the edit has fewer bullets overall
    d.bullets_removed = max(0, len(ob) - len(eb))
    d.removed_examples = [b[:80] for b in removed[:3]]
    d.headings_removed = sum(1 for h in oh if h not in set(eh))
    d.headings_added = sum(1 for h in eh if h not in set(oh))
    kept_in_order = [b for b in eb if b in oset]
    orig_order = [b for b in ob if b in eset]
    d.order_changed = kept_in_order != orig_order
    return d
