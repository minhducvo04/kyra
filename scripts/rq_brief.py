#!/usr/bin/env python3
"""Render a builder brief (or an Opus probe-review prompt) from a READY-QUEUE row, or the F0 contract brief of a DAG node.

Usage: rq_brief.py --node FEATURE NODE [--run-id ID] [--channel K:T] [--model M] [--effort E] [--base REF]
                   [--worktree DIR] [--deadline-minutes N] [--data-dir DIR] [--owner AGENT] [--out DIR]
    B0: renders the F0 brief (companion.feature_team.contract) for one DAG node as JSON plus the human text
    (<out>/<feature>-<node>-<run_id>.json and .md). Every input result path, artifact, branch and worktree
    comes from the R0 run index (data/team/loop/index.json) with completion.json as the fallback; a missing
    input result refuses with the node id, and any listed path that does not exist on disk fails the render.
    Acceptance is a list of commands (pytest on the node's test files, ruff on its Python files, commands
    quoted in the node's acceptance text) plus the acceptance prose as one measurement item.
Usage: rq_brief.py RQnn [--queue PATH] [--base REF] [--owner AGENT] [--lessons-dir DIR] [--no-wiring] [--review] [--reviews-file PATH] [--out DIR]
The queue is a markdown table: Id | Goal | Owner | Files | Brief/contract | Needs.
Append columns Kind | The owner said (verbatim) | Reference-image for brief metadata;
kind may instead be a `kind: ui` (or build, docs, etc.) tag in Needs.
For multiline original words, use --owner-said-file PATH (UTF-8, copied unchanged).
Node JSON uses kind, owner_said and reference_image fields. All modes require kind
and nonempty original words; ui also requires a reference-image path or URL.
Gate-generated nodes use source: gate to attribute owner_said to the gate, not the owner.
--owner names the agent (a lane name, e.g. claude-lane-0): when data/team/lessons/<agent>.md exists its last five lessons are added as "## Lessons".
Prints the output path(s). The queue is private; briefs go to data/team/ (gitignored).
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# data/ is gitignored, so a worktree has none: use the main checkout's copy.
MAIN = ROOT.parents[2] if ROOT.parent.name == "worktrees" else ROOT
DEFAULT_QUEUE = MAIN / "data/team/READY-QUEUE.md"
DEFAULT_REVIEWS = "data/team/mentor-reviews.md"
DEFAULT_OUT = MAIN / "data/team/briefs"
DEFAULT_LESSONS = MAIN / "data/team/lessons"
sys.path.insert(0, str(ROOT / "src"))
from companion.feature_team import repos  # noqa: E402
from companion.feature_team.dag import live_path  # noqa: E402

TIER_CHECKS = {
    "T0": "own tests + both privacy guards + ruff",
    "T1": "T0 plus changed-area browser tests and the smoke set",
    "T2": "Opus probe review on every door, bad inputs, and privacy; full rule set",
}
TIER_VERIFIER = {
    "T0": "any non-author verifier (Haiku or local)",
    "T1": "Sonnet or Codex",
    "T2": "Opus probe",
}
_SIZE_LINE = "one concern, under ~300 changed lines; stack a second branch on the first"
_T2_FILE_MARKERS = (
    "health_words.py",
    "privacy",
    "route_exposure",
    "webapp.py",
    "team_http",
    "workflow_",
    "docs/permissions.md",
    "agents.md",
)


def files_force_t2(files: str) -> bool:
    low = files.lower()
    if any(m in low for m in _T2_FILE_MARKERS):
        return True
    return bool(re.search(r"test_no_\w+", low)) or "guards" in low


def parse_tier_tag(text: str) -> str | None:
    m = re.search(r"\btier:\s*T([012])\b", text, re.I)
    return f"T{m.group(1)}" if m else None


def row_tier(row: dict[str, str]) -> str:
    if files_force_t2(row["files"]):
        return "T2"
    return parse_tier_tag(row["deps"]) or "T1"


def tier_block(tier: str) -> str:
    return (
        f"- Tier: {tier}: {TIER_CHECKS[tier]}\n"
        f"- Verifier: {TIER_VERIFIER[tier]}, re-run on every push, inconclusive holds\n"
        f"- Size: {_SIZE_LINE}\n"
    )

def read_row(queue: Path, rid: str) -> dict[str, str]:
    headers = []
    for line in queue.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        if cells and cells[0].lower() == "id":
            headers = [c.lower() for c in cells]
        if cells and cells[0].lower() == rid.lower() and len(cells) >= 5:
            has_metadata = any(label in headers for label in ("kind", "the owner said (verbatim)", "reference-image"))
            if has_metadata and len(cells) != len(headers):
                raise SystemExit("brief metadata columns must match the header; use --owner-said-file for text containing pipes")
            metadata = {key: cells[headers.index(label)]
                        for label, key in (("kind", "kind"), ("the owner said (verbatim)", "owner_said"),
                                           ("reference-image", "reference_image"))
                        if label in headers and headers.index(label) < len(cells)}
            if has_metadata:
                cells = cells[:6]
            return {
                "id": cells[0],
                "goal": cells[1],
                "owner": cells[2],
                "files": cells[3],
                "contract": " | ".join(cells[4:-1]) if len(cells) > 5 else cells[4],
                "deps": cells[-1] if len(cells) > 5 else "-",
                **metadata,
            }
    raise SystemExit(f"{rid} not found in {queue}")


def intent_block(item: dict, *, owner_said_source: str | None = None) -> str:
    """Fail closed at every renderer; never synthesize original words from a goal."""
    words = item.get("owner_said")
    if not isinstance(words, str) or not words.strip():
        raise SystemExit("brief requires the owner said (verbatim): supply owner_said or --owner-said-file")
    tags = re.findall(r"\bkind:\s*([\w-]+)", item.get("deps", ""), re.I)
    kind = item.get("kind", "")
    if not isinstance(kind, str):
        raise SystemExit("brief requires a kind: tag")
    kinds = {v.strip().lower() for v in [kind, *tags] if v.strip()}
    if len(kinds) != 1:
        raise SystemExit("brief requires one unambiguous kind: tag")
    kind = kinds.pop()
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", kind):
        raise SystemExit("brief requires a valid kind: tag")
    # A fence longer than any in the source preserves Markdown as quoted data.
    fence = "`" * max(3, 1 + max((len(m) for m in re.findall(r"`+", words)), default=0))
    speaker = "Gate" if item.get("source") == "gate" else "the owner"
    attribution = f"from {owner_said_source}" if owner_said_source else "verbatim"
    suffix = ":" if owner_said_source else ""
    block = f"kind: {kind}\n\n## {speaker} said ({attribution}){suffix}\n{fence}text\n{words}\n{fence}\n"
    if kind != "ui":
        return block
    reference = item.get("reference_image")
    if (not isinstance(reference, str) or not reference.strip()
            or reference.strip().lower() in {"-", "none", "n/a", "tbd", "todo"}
            or len(reference.splitlines()) != 1):
        raise SystemExit("ui brief requires a reference-image: path or URL")
    return block + f"""
## UI taste gate
reference-image: {reference.strip()}
Read `.claude/skills/media-review/SKILL.md`, `taste-skill` and its `references/inspiration.md` if present before building. Inspect the reference first.
Required media-review submission bundle:
1. Commit sha and branch.
2. Screens at 1536 (side rail closed), 1280 and 390.
3. Zoomed crops of every changed object.
4. Measured contrast (worst pixel and p95), lightness L* where relevant, frame time p95 and asset sizes in KB.
5. Drive recipe (feature file and recipe number) and Doctor line (`kyra_doctor.py` result).
6. Not verified list, including device, motion and reduced-motion limits.
An incomplete bundle is returned unreviewed. The non-author Media lead (Opus) must inspect the images, score each ask and record SIGN-OFF or SIGN-OFF WITH NOTES (overall at least 8/10, no blocking notes) under the reviewed sha in the arc's `art-direction.md` before showing the owner. Otherwise FIX and resubmit. Green tests are not visual sign-off.
"""


def row_base(row: dict[str, str]) -> str | None:
    """A base named in the row ("Base <ref> [<sha>]"): the sha when given, else the ref."""
    m = re.search(r"\bBase[: ]+([\w./-]+)(?: +([0-9a-f]{7,40})\b)?", row["contract"] + " " + row["deps"])
    return (m.group(2) or m.group(1)) if m else None


def _rules(low: str, base: str, wiring: str) -> str:
    return f"""## Standard rules
- Read `AGENTS.md` and `.claude/skills/test-impact/SKILL.md` first. Ack in one line before working.
- Worktree: `git -C {MAIN} worktree add {MAIN}/.claude/worktrees/{low} -b session/{date.today().isoformat()}-{low} {base}`. Address it with `git -C`, never `cd`.
- Stage explicit files only (never `git add -A`, never anything under `data/`). Never push. Never print secrets.
{wiring}- Tests are hermetic: temp `KYRA_DATA_DIR`. Run your own tests, both privacy guards (`tests/test_no_third_party_pii.py tests/test_no_personal_files.py`) and `ruff check src scripts tests`; read the pytest summary line, not the dots.
- POSIX-only imports (`fcntl`, `termios`, `resource`) must be lazy (inside the function): the Windows import test fails on a module-level import.
- Any new fixed-position element needs a 390px overlap check (it must not cover Send or other controls).
- If your branch fails the gate or a review, the fix comes back to you with the failing test; answer with the fix and a regression test.
- Commit with `git -C`, message says what was verified.
"""


FEATURE_MAP = Path(".claude/skills/kyra-verify/features")


def feature_names(root: Path = ROOT) -> list[str]:
    """Feature stems listed in the map's README table (written or still to write)."""
    readme = root / FEATURE_MAP / "README.md"
    if not readme.exists():
        return []
    return re.findall(r"^\| \[?([a-z][a-z0-9-]*?)(?:\.md)?\]?(?:\([^)]*\))? \|", readme.read_text(encoding="utf-8"), re.M)


def row_feature(row: dict[str, str], names: list[str] | None = None) -> str | None:
    """The `feature: <name>` tag in the row, else the feature whose name appears in the files' paths."""
    m = re.search(r"\bfeature:\s*([\w-]+)", row["contract"] + " " + row["files"], re.I)
    if m:
        return m.group(1).lower()
    names = feature_names() if names is None else names
    tokens = {t.rstrip("s") for t in re.split(r"[^a-z0-9]+", row["files"].lower()) if t}
    for n in names:
        if n.rstrip("s") in tokens:
            return n
    return None


def existing_features(root: Path = ROOT) -> list[str]:
    d = root / FEATURE_MAP
    return sorted(f.stem for f in d.glob("*.md") if f.name != "README.md") if d.is_dir() else []


def real_check(row: dict[str, str], names: list[str] | None = None, existing: list[str] | None = None) -> str:
    """Name the feature file only if it exists: exact stem, else a stem like <feature>-..."""
    f = row_feature(row, names)
    have = existing_features() if existing is None else existing
    hit = next((h for h in have if f and (h == f or h.startswith(f + "-"))), None)
    if not hit:
        return "none yet: add one (`.claude/skills/kyra-verify/features/<feature>.md`, template in its README)"
    return f"`{FEATURE_MAP}/{hit}.md` (read it, drive its recipes, then update it if the feature changed)"


def build_brief(row: dict[str, str], base: str, owner: str | None, no_wiring: bool) -> str:
    rid, low = row["id"], row["id"].lower()
    wiring = (
        "- Do NOT edit `webapp.py` or `route_exposure.py`. Put the exact wiring lines "
        "(import, include_router, exposure entry) in your reply; the webapp owner wires them.\n"
        if no_wiring
        else "- Wire your routes yourself where the row says so; keep the edit to the lines you need.\n"
    )
    return f"""# {rid} builder brief ({owner or row['owner']})

{intent_block(row)}
Goal: {row['goal']}
Depends on: {row['deps']}

## Files you own
{row['files']}
Touch nothing else.

## Contract
{row['contract']}

## Package tier
{tier_block(row_tier(row))}
## Standard rules
- Read `AGENTS.md` and `.claude/skills/test-impact/SKILL.md` first. Ack in one line before working.
- Worktree: `git -C {MAIN} worktree add {MAIN}/.claude/worktrees/{low} -b session/{date.today().isoformat()}-{low} {base}`. Address it with `git -C`, never `cd`.
- Stage explicit files only (never `git add -A`, never anything under `data/`). Never push. Never print secrets.
{wiring}- Tests are hermetic: temp `KYRA_DATA_DIR`. Run your own tests, both privacy guards (`tests/test_no_third_party_pii.py tests/test_no_personal_files.py`) and `ruff check src scripts tests`; read the pytest summary line, not the dots.
- POSIX-only imports (`fcntl`, `termios`, `resource`) must be lazy (inside the function): the Windows import test fails on a module-level import.
- Any new fixed-position element needs a 390px overlap check (it must not cover Send or other controls).
- If your branch fails the gate or a review, the fix comes back to you with the failing test; answer with the fix and a regression test.
- Commit with `git -C`, message says what was verified.
- Drive with your own headless Playwright (page_check.py or a 10-line script) at 1280 and 390; never the shared in-app Browser pane (698px, tabs jump between agents).
- Privacy contracts are storage rules, not "never sees text": text stays in memory and the local worker; never persisted, logged or sent to a cloud model. Write and test them that way.

## Real check
Feature map: {real_check(row)}. Also use `.claude/skills/kyra-verify/SKILL.md` (Doctor, then Drive).

## Reply format (under 120 words)
- Commit sha and branch
- Feature map: unchanged | updated <file>
- Summary lines of the tests, guards and ruff
- Wiring lines (exact text) if `--no-wiring`
- Bugs found in neighbouring code (do not fix)
"""


FEATURES = MAIN / "data/team/feature-teams"
RUNS = MAIN / "data/team/features"
NODE_CLOSE = (
    "Effort low. Compact your context after this node. On the feature's final merge signal, "
    "clear it; on a fix request, keep it and redo."
)


def _result_path(feature: str, nid: str, runs: Path) -> str:
    try:
        data = json.loads((runs / feature / "completion.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "pending"
    entry = (data.get("nodes", data) if isinstance(data, dict) else {}).get(nid)
    if isinstance(entry, dict):
        entry = entry.get("result") or entry.get("path")
    return str(entry) if entry else "pending"


BLOCKED_RULE = (
    "Never ask the owner, and never at night. Ask the author of the prerequisite node first, then the Board. "
    "Every question is a failure: it gets a fix and a lesson, because this brief should have carried enough data."
)


def _author_lines(feature: str, inputs: list[str], runs: Path) -> str:
    def load(name: str):
        try:
            return json.loads((runs / feature / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    comp, team = load("completion.json"), load("team.json")
    nodes = comp.get("nodes", comp) if isinstance(comp, dict) else {}
    chan = {m.get("name"): m.get("channel") for m in team if isinstance(m, dict)} if isinstance(team, list) else {}
    if not inputs:
        return "- Board only"
    out = []
    for i in inputs:
        e = nodes.get(i)
        by = e.get("by") if isinstance(e, dict) else None
        if not by:
            out.append(f"- {i}: author pending")
        else:
            out.append(f"- {i}: {by} via {chan.get(by) or 'Board'}")
    return "\n".join(out)


def build_node_brief(feature: str, nid: str, base: str, features: Path = FEATURES, runs: Path = RUNS,
                     *, owner_said: str | None = None) -> str:
    src = live_path(features, feature)
    if not src.is_file():
        raise SystemExit(f"{src} not found")
    nodes = {n["id"]: n for n in json.loads(src.read_text(encoding="utf-8")).get("nodes", [])}
    if nid not in nodes:
        raise SystemExit(f"{nid} not found in {src.name}")
    n = nodes[nid]
    if owner_said is not None:
        n["owner_said"] = owner_said
    intent = intent_block(n)
    low = f"{feature}-{nid.lower()}"
    inputs = "\n".join(f"- {i}: {_result_path(feature, i, runs)}" for i in n.get("inputs", [])) or "- none"
    files = "\n".join(f"- {f}" for f in n.get("files", []))
    wiring = "- Wire your routes yourself where the row says so; keep the edit to the lines you need.\n"
    return f"""# {feature} node {nid} brief

{intent}
Title: {n['title']}
Tier: {n.get('tier', '-')}

## Files you own
{files}
Touch nothing else.

## Inputs (results)
{inputs}

## Acceptance
{n.get('acceptance', '')}

## If you are blocked
{BLOCKED_RULE}
{_author_lines(feature, n.get("inputs", []), runs)}

{_rules(low, base, wiring)}
## Reply format (under 120 words)
- Commit sha and branch
- Summary lines of the tests, guards and ruff
- Bugs found in neighbouring code (do not fix)

{NODE_CLOSE}
"""


# ---- B0: the F0 contract brief of a DAG node -----------------------------------------------------------------------

DENY = (
    "no pushes, no force, no history rewrite, never the public remote",
    "touch only the files this brief lists; stage explicit files, never git add -A, never anything under data/",
    "no .env reads and no secrets printed (check length or prefix only)",
    "no personal data in tracked files; event rows carry paths and counts, never transcript text",
    "no model loads unless the node says so; no browser phase here (the landing train runs it)",
    "a worker saying done flips nothing: the node is done only when verify.py passes its acceptance commands",
    "blocked: write the question in the result's open_questions, continue on your guess, never ask the owner at night",
)
_RUNNER_HEADS = ("pytest", "python", "python3", ".venv/bin/python", "ruff", "bash", "sh", "make", "git", "npm")
_QUOTED_COMMAND = re.compile(r"`([^`\n]+)`")


def load_index(data_dir: Path) -> dict:
    """The R0 run index (feature -> node -> latest run facts), or {} when none exists yet."""
    try:
        document = json.loads((data_dir / "team" / "loop" / "index.json").read_text(encoding="utf-8"))
        return document.get("features", {}) if isinstance(document, dict) else {}
    except (OSError, ValueError):
        return {}


def input_facts(feature: str, nid: str, index: dict, runs: Path) -> dict | None:
    """{result, sha, branch, worktree, artifacts, state, run_id} for an input node from the index, else from the
    feature's completion.json (legacy done nodes), else None."""
    row = index.get(feature, {}).get(nid)
    if isinstance(row, dict) and row.get("result"):
        return {"result": str(row["result"]), "sha": row.get("sha"), "branch": row.get("branch"),
                "worktree": row.get("worktree"), "artifacts": list(row.get("artifacts") or []),
                "state": row.get("state"), "run_id": row.get("latest")}
    try:
        comp = json.loads((runs / feature / "completion.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = (comp.get("nodes", comp) if isinstance(comp, dict) else {}).get(nid)
    if isinstance(entry, dict) and entry.get("status") == "done" and (entry.get("result") or entry.get("result_path")):
        return {"result": str(entry.get("result") or entry.get("result_path")), "sha": entry.get("sha"), "branch": None,
                "worktree": None, "artifacts": [], "state": "done", "run_id": None}
    return None


def acceptance_commands(node: dict) -> list[str]:
    """Commands first (pytest on the node's test files, ruff on its Python files, backtick commands from the prose,
    acceptance lines that are a bare command), then each prose line as a measurement item; verify.py runs the
    commands and logs the measurements. Each acceptance line stays its own item: joining them fused STORY-TRAIN's
    `git diff --check` with the next command and the prose (2026-10-16)."""
    from companion.feature_team import contract

    files = [f[4:] if f.startswith("NEW ") else f for f in node.get("files", [])]
    tests = [f for f in files if f.startswith("tests/test_") and f.endswith(".py")]
    python = [f for f in files if f.endswith(".py")]
    items: list[str] = []
    if tests:
        items.append("pytest " + " ".join(tests))
    if python:
        items.append("ruff check " + " ".join(python))
    def command(text):
        try:
            head = shlex.split(text)[0]
        except (ValueError, IndexError):
            return False
        return head in _RUNNER_HEADS or head.endswith("/python")

    lines = contract.acceptance_items(node.get("acceptance", ""))
    for line in lines:
        for quoted in [line] if command(line) else _QUOTED_COMMAND.findall(line):
            if command(quoted) and quoted not in items:
                items.append(quoted)
    items.extend(line for line in lines if not command(line) and line not in items)
    return items


def render_brief(run: dict, *, features: Path = FEATURES, runs: Path = RUNS, data_dir: Path | None = None,
                 index: dict | None = None) -> dict:
    """The F0 brief for one dispatched node run, validated, plus its human text and warnings.

    ``run`` holds the dispatch facts: run_id, feature, node, channel, model, effort (and optionally base, worktree,
    deadline, result, rules, tier, owner_said). Inputs come from the R0 index (``index`` or data/team/loop/index.json under
    ``data_dir``, default the main checkout's data/) with completion.json as the fallback; an input without a
    result refuses with ``inputs: no result path for input node <id>``, and an input result or artifact path
    that is not on disk refuses with the node id too. Original words come from run.owner_said, then the DAG's
    top-level owner_said, then the feature-queue note; absent or invalid words refuse the render.
    Returns {"brief", "text", "warnings", "inputs"}.
    """
    from companion.feature_team import contract

    feature, nid = run["feature"], run["node"]
    data_dir = Path(data_dir) if data_dir is not None else MAIN / "data"
    src = live_path(features, feature)
    if not src.is_file():
        raise ValueError(f"feature: {src} not found")
    dag = json.loads(src.read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in dag.get("nodes", [])}
    if nid not in nodes:
        raise ValueError(f"node: {nid} not in {src.name}")
    node = nodes[nid]
    words, source = run.get("owner_said"), "run"
    entry = {}  # the feature's queue entry, when the words or the plan inputs come from it
    if words is None:
        words, source = dag.get("owner_said"), "DAG"
    if words is None:
        source = "feature-queue note"
        queue_path = data_dir / "team" / "feature-queue.json"
        if queue_path.exists():
            try:
                entries = json.loads(queue_path.read_text(encoding="utf-8"))["features"]
                if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
                    raise ValueError("invalid features")
                matches = [entry for entry in entries if entry.get("name") == feature]
                if len(matches) > 1:
                    raise ValueError("duplicate feature")
                words = matches[0].get("note") if matches else None
                entry = matches[0] if matches else {}
            except (OSError, ValueError, KeyError, TypeError):
                raise ValueError(f"owner_said: invalid feature queue at {queue_path}") from None
    node["owner_said"] = words
    intent_block(node, owner_said_source=source)
    # A plan-kind worker runs on a channel that cannot read data/, so the plan's inputs (the queue entry's
    # "inputs" paths, e.g. The owner's spec) travel inside the brief text, bounded, never as paths to open.
    plan_inputs = {}
    if node.get("kind") == "plan":
        if not entry:
            entry = _queue_entry(data_dir, feature)
        for item in entry.get("inputs", []) if isinstance(entry.get("inputs", []), list) else []:
            path = _plan_input_path(item, data_dir)
            try:
                plan_inputs[str(item)] = path.read_text(encoding="utf-8")[:PLAN_INPUT_CHARS]
            except OSError:
                raise ValueError(f"inputs: plan input {item} is not readable") from None
    index = load_index(data_dir) if index is None else index
    facts, warnings = {}, []
    from companion.feature_team.dispatch import gate_closed
    try:
        rows = json.loads((runs / feature / "completion.json").read_text(encoding="utf-8"))
        rows = rows.get("nodes", rows) if isinstance(rows, dict) else {}
    except (OSError, ValueError):
        rows = {}
    for i in node.get("inputs", []):
        if gate_closed(nodes.get(i), rows.get(i)):
            # closed by done --gate: the owner's or the Board's note, nothing to land on master
            fact = {"result": str(rows[i]["result"]), "sha": None, "branch": None, "worktree": None,
                    "artifacts": [], "state": "done", "run_id": None}
        else:
            fact = input_facts(feature, i, index, runs)
        if fact is None:
            raise ValueError(f"inputs: no result path for input node {i}")
        if not Path(fact["result"]).exists():
            raise ValueError(f"inputs: {i} names a path that is not on disk: {fact['result']}")
        missing = repos.unlanded(data_dir, feature, fact["artifacts"])
        if missing:
            raise ValueError(f"inputs: waiting for {i} to land: {', '.join(missing)} not on master yet")
        if fact["state"] not in ("verify_pass", "landed", "done"):
            warnings.append(f"input {i} is {fact['state']}, not verified")
        facts[i] = fact
    base = run.get("base") or next((f["sha"] for f in reversed(list(facts.values())) if f.get("sha")), None) or "master"
    rules = run.get("rules") or (str(src) if isinstance(dag.get("rules"), dict) else str(MAIN / "AGENTS.md"))
    if not Path(rules).exists():
        raise ValueError(f"rules: {rules} is not on disk")
    low = f"{feature}-{nid.lower()}"
    deadline = run.get("deadline") or time.time() + 90 * 60
    deny = list(DENY)
    for key in ("stop", "privacy"):
        line = " ".join(str((dag.get("rules") or {}).get(key, "")).split())
        if line and line not in deny:
            deny.append(line)
    brief = contract.brief_from_node(
        node, run_id=run["run_id"], feature=feature, channel=run["channel"], model=run["model"], effort=run["effort"],
        base=base, worktree=run.get("worktree") or str(MAIN / ".claude" / "worktrees" / low),
        inputs={i: f["result"] for i, f in facts.items()}, deadline=deadline,
        result=run.get("result") or str(data_dir / "team" / "loop" / "results" / f"{run['run_id']}.json"),
        rules=rules, acceptance=acceptance_commands(node), deny=deny)
    if not Path(brief["worktree"]).exists():
        warnings.append(f"worktree {brief['worktree']} does not exist yet (the dispatcher creates it from {base})")
    return {"brief": brief, "text": brief_text(brief, node, facts, warnings, owner_said_source=source,
                                               plan_inputs=plan_inputs),
            "warnings": warnings, "inputs": facts}


PLAN_INPUT_CHARS = 24000


def _queue_entry(data_dir: Path, feature: str) -> dict:
    """The feature's entry in data/team/feature-queue.json, or {} (missing or invalid queue: no inputs)."""
    try:
        entries = json.loads((data_dir / "team" / "feature-queue.json").read_text(encoding="utf-8"))["features"]
        matches = [e for e in entries if isinstance(e, dict) and e.get("name") == feature]
        return matches[0] if len(matches) == 1 else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _plan_input_path(item, data_dir: Path) -> Path:
    """A queue entry's plan input: a data/ path (read here, on the Board side) or an absolute path inside data/."""
    path = Path(str(item))
    if path.is_absolute():
        resolved = path.resolve()
    elif path.parts and path.parts[0] == "data":
        resolved = (data_dir / Path(*path.parts[1:])).resolve()
    else:
        resolved = (data_dir / path).resolve()
    if not resolved.is_relative_to(Path(data_dir).resolve()):
        raise ValueError(f"inputs: plan input must stay under the data dir: {item}")
    return resolved


def brief_text(brief: dict, node: dict, facts: dict, warnings: list, *, owner_said_source: str | None = None,
               plan_inputs: dict | None = None) -> str:
    """The human form of the F0 brief: every path verbatim, nothing to search for."""
    from companion.feature_team import contract
    from companion.feature_team.node_worker import worker_command_block

    plan_block = ""
    if brief.get("kind") == "plan":
        feature = brief["feature"]
        inlined = "".join(f"\n### {name}\n\n{text.rstrip()}\n" for name, text in (plan_inputs or {}).items())
        plan_block = f"""## Plan output (read this before anything else)
You cannot read or write anything under data/ from this channel; everything you need is in this brief.
Write two files at the root of your worktree `{brief['worktree']}`:
- `PLAN-{feature}.md`: the plan, the owner's words quoted verbatim, assumptions marked, approval requirements kept.
- `{feature}-proposed.dag.json`: the proposed DAG in the loop's node shape (id, title, inputs, files, acceptance, tier, kind, intern_friendly).
List both in the result's artifacts as those relative paths. The harvest copies them to data/team/feature-teams/ ({feature}-plan.md and {feature}-proposed.dag.json) and verify checks them there; nothing you write under data/ counts.
{"## Plan inputs (inlined; the files themselves are not reachable from here)" + inlined if inlined else ""}
"""
    inputs = []
    for i, f in facts.items():
        inputs.append(f"- {i}: result `{f['result']}`; sha {f.get('sha') or '-'}; branch {f.get('branch') or '-'}; "
                      f"worktree {f.get('worktree') or '-'}; state {f.get('state') or '-'}")
        inputs += [f"  - artifact `{a}`" for a in f.get("artifacts", [])]
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(brief["deadline"]))
    return f"""# {brief['feature']} node {brief['node']} brief (run {brief['run_id']})

{intent_block(node, owner_said_source=owner_said_source)}
Title: {brief['title']}
Tier: {node.get('tier', '-')}. Channel {brief['channel']}, model {brief['model']}, effort {brief['effort']}.
Base: {brief['base']}. Worktree: `{brief['worktree']}` (branch session/{Path(brief['worktree']).name.split('--', 1)[-1]}).
Deadline: {when} (epoch {brief['deadline']:.0f}).

{plan_block}## Files you own
{chr(10).join(f"- {f}" for f in brief['files']) or '- none'}
Touch nothing else.

## Inputs (results, from the run index)
{chr(10).join(inputs) or '- none'}

## Acceptance (verify.py runs the commands at your result sha; the last item is the measured prose)
{chr(10).join(f"- `{a}`" if _is_command(a) else f"- {a}" for a in brief['acceptance'])}

## Commands you run
{worker_command_block(brief)}
## Deny
{chr(10).join(f"- {d}" for d in brief['deny'])}

## Result
Write the F0 result (companion.feature_team.contract.validate_result shape) to `{brief['result']}`: run_id, feature, node, worker (exactly `{brief['channel']}`), sha, branch, status (done|blocked|failed), artifacts, proof rows (command, exit, log), tests, open_questions, tokens, started, finished.
`tests` is a JSON list of the test commands you ran, never a summary string or a count dict; `exit` is an integer. Example:
```json
{contract.result_example(brief)}
```
Rules: `{brief['rules']}`.

## If you are blocked
{BLOCKED_RULE}

{"## Warnings" + chr(10) + chr(10).join(f"- {w}" for w in warnings) + chr(10) if warnings else ""}
## Reply format (under 120 words)
- Commit sha and branch
- Summary lines of the tests, guards and ruff
- Bugs found in neighbouring code (do not fix)

{NODE_CLOSE}
"""


def _is_command(item: str) -> bool:
    try:
        head = shlex.split(item)[0]
    except (ValueError, IndexError):
        return False
    return head in _RUNNER_HEADS or head.endswith("/python")


def write_brief(rendered: dict, out: Path) -> tuple[Path, Path]:
    brief = rendered["brief"]
    out.mkdir(parents=True, exist_ok=True)
    stem = out / f"{brief['feature']}-{brief['node']}-{brief['run_id']}"
    stem.with_suffix(".json").write_text(json.dumps(brief, indent=1, allow_nan=False) + "\n", encoding="utf-8")
    stem.with_suffix(".md").write_text(rendered["text"], encoding="utf-8")
    return stem.with_suffix(".json"), stem.with_suffix(".md")


def add_lessons(text: str, owner: str, root: Path) -> str:
    """Put the owner's last five lessons (data/team/lessons/<owner>.md) before the reply format; nothing when there are none."""
    from companion import lessons_log

    try:
        section = lessons_log.brief_section(owner, root=root)
    except ValueError:  # an owner that is a model name with odd characters has no lessons file
        return text
    before, marker, after = text.rpartition("\n## Reply format")
    return before + "\n" + section + marker + after if section and marker else text


def build_review(row: dict[str, str], base: str, reviews_file: str = DEFAULT_REVIEWS) -> str:
    rid = row["id"]
    return f"""# {rid} probe review (Opus, non-author)

{intent_block(row)}
Review the {rid} package: {row['goal']}
Files: {row['files']}
Contract: {row['contract']}

## Setup
- You are not the author. Review a scratch copy: `git -C {MAIN} archive <sha> | tar -x -C <scratch dir>`; if it depends on queued branches, review a merge with them and state the landing order. Never push, never stage `data/`.
- Read `AGENTS.md`, `.claude/skills/test-impact/SKILL.md` and the author's recorded evidence (`python scripts/review_evidence.py show <sha>`). Base: {base}.

## Probe with real requests, not code reading
- Doors: loopback, LAN signed, LAN unsigned, tunnel. Each must behave as the contract says.
- Path variants: trailing slash, case change, `//`, `%2e`, encoded slashes, query strings.
- Bad inputs: empty, huge, wrong type, unicode, null bytes, repeated or conflicting fields.
- Errors must never echo values (inputs, labels, tokens, health or family text).
- Privacy: health and family data stay off the LAN door and the tunnel.

## Verdict
Append to `{reviews_file}`: PASS or FIX-FIRST, each finding with the probe that showed it and the named author. Reply in under 80 words.
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("rid", nargs="?")
    ap.add_argument("--node", nargs=2, metavar=("FEATURE", "NODE"))
    ap.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    ap.add_argument("--base", help="default: the base named in the row, else origin/master")
    ap.add_argument("--owner")
    ap.add_argument("--lessons-dir", type=Path, default=DEFAULT_LESSONS)
    ap.add_argument("--no-wiring", action="store_true")
    ap.add_argument("--review", action="store_true")
    ap.add_argument("--reviews-file", default=DEFAULT_REVIEWS)
    ap.add_argument("--owner-said-file", type=Path, help="UTF-8 original words, copied verbatim into any brief")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--run-id", help="--node: the run id (default: a new one from contract.new_run_id)")
    ap.add_argument("--channel", default="claude:lane-0", help="--node: kind:target the dispatcher sends through")
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--worktree", help="--node: the worker's worktree (default .claude/worktrees/<feature>-<node>)")
    ap.add_argument("--deadline-minutes", type=float, default=90.0)
    ap.add_argument("--data-dir", type=Path, default=None, help="--node: where the run index and results live (default data/)")
    a = ap.parse_args(argv)
    owner_said = None
    if a.owner_said_file:
        try:
            owner_said = a.owner_said_file.read_bytes().decode("utf-8")
        except (OSError, UnicodeError):
            raise SystemExit("cannot read --owner-said-file as UTF-8") from None
    if a.node:
        from companion.feature_team import contract
        feature, nid = a.node
        run = {"run_id": a.run_id or contract.new_run_id(feature, nid), "feature": feature, "node": nid,
               "channel": a.channel, "model": a.model, "effort": a.effort, "base": a.base, "worktree": a.worktree,
               "deadline": time.time() + a.deadline_minutes * 60, "owner_said": owner_said}
        try:
            paths = {} if a.data_dir is None else {
                "features": a.data_dir / "team/feature-teams",
                "runs": a.data_dir / "team/features",
            }
            rendered = render_brief(run, data_dir=a.data_dir, **paths)
        except ValueError as exc:
            raise SystemExit(f"refused: {exc}") from None
        if a.owner:
            rendered["text"] = add_lessons(rendered["text"], a.owner, a.lessons_dir)
        for path in write_brief(rendered, a.out):
            print(path)
        for w in rendered["warnings"]:
            print(f"warning: {w}", file=sys.stderr)
        return 0
    if not a.rid:
        raise SystemExit("give an RQ id or --node FEATURE NODE")
    if not re.fullmatch(r"RQ\d+", a.rid, re.I):
        raise SystemExit("id must look like RQ12")
    row = read_row(a.queue, a.rid)
    if owner_said is not None:
        row["owner_said"] = owner_said
    base = a.base or row_base(row) or "origin/master"
    text = build_review(row, base, a.reviews_file) if a.review else build_brief(row, base, a.owner, a.no_wiring)
    if a.owner and not a.review:
        text = add_lessons(text, a.owner, a.lessons_dir)
    a.out.mkdir(parents=True, exist_ok=True)
    path = a.out / (f"{row['id']}-review.md" if a.review else f"{row['id']}.md")
    path.write_text(text, encoding="utf-8")
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
