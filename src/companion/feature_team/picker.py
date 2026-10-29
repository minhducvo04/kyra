"""Pick a feature team: ranked Engineers plus about twice as many Interns.

Uses rollback.json, docs/model-fit.md and the effort policy.
Only configured sender channels are eligible. No network, no model calls.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import sys
import tempfile
from pathlib import Path

from companion.feature_team import muse as _muse

ROOT = Path(__file__).resolve().parents[3]
ROLLBACK = ROOT / "data" / "team" / "rollback.json"
MODEL_FIT = ROOT / "docs" / "model-fit.md"
FEATURES_DIR = ROOT / "data" / "team" / "features"
EFFORT_POLICY = ROOT / "data" / "team" / "effort-policy.json"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODEL_IDS = {
    "Claude Fable": "claude-fable-5-1",
    "Claude Opus": "claude-opus-5-5",
    "Claude Sonnet": "claude-sonnet-5-5",
    "Claude Haiku": "claude-haiku-5-5",
    "Codex Astra": "codex-astra",
    "Codex Sol": "codex-light",
    "Grok": "grok-4.7",
    "Muse Wonder Full": "muse-wonder-full-1.0",
    "Muse Wonder Full 2": "muse-wonder-full-1.0",
}
# Roster rows for the extra Muse slots (muse:2 and up) name "Muse Wonder Full 3".. so their briefs carry the real model
# id; the picker's ranking ROSTER keeps two Muse names, and _rollback_key finds these by model.
MODEL_IDS.update({f"Muse Wonder Full {n}": _muse.MODEL for n in range(3, _muse.SLOT_COUNT + 1)})

# name -> (role, channel, rollback key, model-fit keyword)
ROSTER: dict[str, tuple[str, str, str, str]] = {
    "Claude Fable": ("Engineer", "claude:fable", "Claude Fable", "fable"),
    "Claude Opus": ("Engineer", "claude:opus", "Claude Opus", "opus"),
    "Codex Astra": ("Engineer", "codex:1", "Codex", "astra"),
    "Grok": ("Engineer", "grok", "Grok", "grok"),
    "Muse Wonder Full": ("Engineer", "muse:0", "Muse", "muse"),
    "Muse Wonder Full 2": ("Engineer", "muse:1", "Muse", "muse"),
    "Cursor": ("Engineer", "cursor:worker", "Cursor", "cursor"),
    "Qwen 27B": ("Engineer", "local:qwen-27b", "Qwen 27B", "qwen 27"),
    "Qwen 35B": ("Engineer", "local:qwen-35b", "Qwen 35B", "qwen 35"),
    "Claude Sonnet": ("Intern", "claude:sonnet", "Claude Sonnet", "sonnet"),
    "Claude Haiku": ("Intern", "claude:haiku", "Claude Haiku", "haiku"),
    "Codex Sol": ("Intern", "codex:0", "Codex", "sol"),
    "Qwen 7B": ("Intern", "local:qwen-7b", "Qwen 7B", "qwen 7"),
    "Qwen 2B": ("Intern", "local:qwen-2b", "Qwen 2B", "qwen 2b"),
}
BONUS = 0.05  # one specialty hit; rates are per landed commit, so this is small but decisive on ties


def supported_channel(channel: str) -> bool:
    kind, _, target = channel.partition(":")
    if kind == "muse":
        from companion.feature_team.muse import SLOTS, enabled
        return channel in SLOTS and enabled()
    if kind == "cursor":
        return Path(target).is_absolute() and Path(target).is_dir()
    return kind == "grok" or (kind in {"claude", "codex"} and bool(target))


def policy_effort(
    name: str, role: str, policy: dict, *, task_kind: str = "build", strict: bool = True,
) -> str:
    """Model override wins; build Interns use build_small before the build table."""
    if not isinstance(policy, dict):
        if strict:
            raise ValueError("effort policy must be an object with task tables")
        policy = {}
    kind = "build_small" if task_kind == "build" and role.lower() == "intern" else task_kind
    table = policy.get(kind, policy.get(task_kind, {}))
    overrides = policy.get("overrides", {})
    if not isinstance(table, dict) or not isinstance(overrides, dict):
        if strict:
            raise ValueError("effort policy must be an object with task tables")
        table = table if isinstance(table, dict) else {}
        overrides = overrides if isinstance(overrides, dict) else {}
    default = policy.get("default", None if strict else "medium")
    if not strict and default not in EFFORTS:
        default = "medium"
    model = MODEL_IDS.get(name, name.lower().replace(" ", "-"))
    effort = overrides.get(model, table.get(model, default))
    if effort not in EFFORTS:
        if strict:
            raise ValueError("effort policy must supply a valid task/default effort")
        return default
    return effort


def load_rollback(path: Path) -> dict[str, float | None]:
    """Map rollback key -> bounce + fix_first per landed (None when n/a)."""
    rows = json.loads(Path(path).read_text(encoding="utf-8")).get("rows", [])
    out: dict[str, float | None] = {}
    for r in rows:
        b, f = r.get("bounce_per_landed"), r.get("fix_first_per_landed")
        out[str(r.get("key"))] = None if b is None or f is None else float(b) + float(f)
    return out


def _rollback_key(worker: dict, channel: str) -> str | None:
    """Roster rollback key for a team.json worker: by name, then model id, then channel."""
    if worker.get("name") in ROSTER:
        return ROSTER[worker["name"]][2]
    model = worker.get("model") or MODEL_IDS.get(worker.get("name"))
    by_model = next((n for n, m in MODEL_IDS.items() if m == model), None)
    if by_model in ROSTER:
        return ROSTER[by_model][2]
    return next((r[2] for r in ROSTER.values() if r[1] == channel), None)


def rank(table: list[dict], features: list[dict], rollback_path: Path = ROLLBACK) -> list[dict]:
    """The dispatcher's lane order: lowest bounce + fix-first per landed commit first.

    A lane with no rollback row (or no roster match) gets the median, as in pick_team.
    Ties keep the table's order, so a missing rollback.json leaves the table unchanged; a malformed or
    unreadable one does too, with a stderr warning, so a bad file never stops a dispatch tick.
    Callers rank before dispatch.budget_table, which stays the last step before select.
    Pure apart from reading rollback_path; no network, no model calls.
    """
    try:
        rates = load_rollback(rollback_path) if Path(rollback_path).exists() else {}
    except (OSError, ValueError, TypeError, AttributeError):
        print("Warning: rollback.json missing fields or unreadable; lanes keep table order", file=sys.stderr)
        rates = {}
    known = [v for v in rates.values() if v is not None]
    middle = statistics.median(known) if known else 0.0
    workers = {w.get("channel"): w for f in features for w in f.get("team", [])}

    def rate(entry: dict) -> float:
        key = _rollback_key(workers.get(entry["channel"], {}), entry["channel"])
        value = rates.get(key) if key is not None else None
        return middle if value is None else value

    return sorted(table, key=rate)


def load_best_at(path: Path) -> list[tuple[str, str]]:
    """First markdown table only: [(row text for Model/App, Best-at text)], lowercase."""
    rows: list[tuple[str, str]] = []
    header: list[str] | None = None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            if header:
                break
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            header = [c.lower() for c in cells]
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        if "best at" in header and len(cells) == len(header):
            who = " ".join(cells[: header.index("best at")])
            rows.append((who.lower(), cells[header.index("best at")].lower()))
    return rows


def load_efforts(path: Path, task_kind: str) -> dict[str, str]:
    """Resolve model overrides, then task-kind cells, with a medium fallback."""
    try:
        policy = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        policy = {}
    return {
        name: policy_effort(name, entry[0], policy, task_kind=task_kind, strict=False)
        for name, entry in ROSTER.items()
    }


def pick_team(
    feature: str,
    specialty_tags: list[str],
    rollback_path: Path = ROLLBACK,
    model_fit_path: Path = MODEL_FIT,
    max_engineers: int = 5,
    n_engineers: int = 2,
    out_dir: Path | None = None,
    effort_policy_path: Path | None = None,
    *,
    task_kind: str = "build",
) -> list[dict]:
    """Rank, persist team.json and return the roster.

    An explicit policy path (used by CLI init) must supply valid efforts.
    The default policy lookup retains the medium fallback for missing/bad data.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", feature):
        raise ValueError("feature must be a plain directory name")
    rates = load_rollback(rollback_path) if Path(rollback_path).exists() else {}
    known = [v for v in rates.values() if v is not None]
    middle = statistics.median(known) if known else 0.0
    fit = load_best_at(model_fit_path)
    tags = [t.lower() for t in specialty_tags]

    scored = []  # (score, name, reason)
    for name, (_role, _ch, rkey, kw) in ROSTER.items():
        if not supported_channel(_ch):
            continue
        rate = rates.get(rkey)
        base = middle if rate is None else rate
        hit = [t for who, best in fit if kw in who for t in tags if t in best]
        score = -base + BONUS * len(hit)
        reason = ("rollback n/a" if rate is None else f"rollback {rate:.2f}/landed") + (
            f", fit: {','.join(sorted(set(hit)))}" if hit else ""
        )
        scored.append((score, name, reason))
    scored.sort(key=lambda s: -s[0])  # stable: roster order breaks ties

    n_eng = max(1, min(n_engineers, max_engineers, 5))
    engineers = [s for s in scored if ROSTER[s[1]][0] == "Engineer"][:n_eng]
    interns_pool = [s for s in scored if ROSTER[s[1]][0] == "Intern"]
    interns = interns_pool[: max(len(engineers), min(2 * len(engineers), len(interns_pool)))]

    if effort_policy_path is None:
        efforts = load_efforts(EFFORT_POLICY, task_kind)
    else:
        policy = json.loads(Path(effort_policy_path).read_text(encoding="utf-8"))
        efforts = {
            name: policy_effort(name, ROSTER[name][0], policy, task_kind=task_kind)
            for _score, name, _reason in engineers + interns
        }
    team = [
        {"name": n, "role": ROSTER[n][0], "channel": ROSTER[n][1], "effort": efforts[n], "rank_reason": why}
        for _s, n, why in engineers + interns
    ]
    dest = (out_dir or FEATURES_DIR) / feature
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "team.json"
    if path.is_symlink():
        raise ValueError("team.json must not be a symlink")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=dest, delete=False) as output:
        temporary = Path(output.name)
        output.write(json.dumps(team, indent=2) + "\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return team
