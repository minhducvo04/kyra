"""Load validated feature dependency graphs without executing their contents."""

import hashlib
import json
import logging
import re
from copy import deepcopy
from datetime import UTC, datetime
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

log = logging.getLogger(__name__)

# verify.py's runner heads: an acceptance line that starts with one is run as a command
_FUSED_WARNED: set[str] = set()
RUNNERS = ("pytest", "python", "python3", ".venv/bin/python", "ruff", "bash", "sh", "make", "git", "npm")


def fused_command(line: str) -> bool:
    """True when an acceptance line starts as a command but carries prose or a second command, like 2026-10-16
    STORY-TRAIN's `git diff --check pytest tests/... The story tells ...` (verify ran it and failed with exit 128).
    Prose words: one ending in ; or , or a plain word ending in . or :, or a second runner (not after -m)."""
    words = line.split()
    if not words or not (words[0] in RUNNERS or words[0].endswith(("/python", "/python3", "/pytest"))):
        return False
    return any((word in RUNNERS and prev != "-m") or word[-1] in ";," or re.fullmatch(r"[A-Za-z]+[.:]", word)
               for prev, word in zip(words, words[1:], strict=False))


class Dag:
    """Validated nodes in file order; queries return detached data.

    Start nodes and direct dependents are returned as IDs. Unknown IDs raise
    KeyError. Extra node fields are preserved for downstream consumers.
    """

    def __init__(self, nodes: list[dict], *, strict: bool = False):
        """``strict`` refuses an acceptance line that fuses a command with prose; otherwise such a line is one
        warning, so the live DAGs that already hold one keep loading (promote refuses it on new or changed nodes).
        It stays for a whole-file check, such as a new DAG before it goes live, where every fused line should fail."""
        self._nodes = {}
        for index, item in enumerate(nodes):
            if not isinstance(item, dict):
                raise ValueError(f"node {index}: expected an object")
            id = item.get("id")
            if not isinstance(id, str) or not id.strip():
                raise ValueError(f"node {index}: id must be a non-empty string")
            if id in self._nodes:
                raise ValueError(f"duplicate node id: {id}")
            inputs = item.get("inputs")
            if not isinstance(inputs, list) or any(
                not isinstance(value, str) or not value.strip() for value in inputs
            ):
                raise ValueError(f"node {id}: inputs must be a list of IDs")
            if not isinstance(item.get("intern_friendly"), bool):
                raise ValueError(f"node {id}: intern_friendly must be bool")
            if item.get("tier") not in ("T0", "T1", "T2"):
                raise ValueError(f"node {id}: tier must be T0, T1 or T2")
            acceptance = item.get("acceptance")
            if not isinstance(acceptance, str) or not acceptance.strip():
                raise ValueError(f"node {id}: acceptance must be a non-empty string")
            for number, line in enumerate(acceptance.splitlines(), 1):
                if fused_command(line):
                    why = (f"node {id}: acceptance line {number} fuses a command with prose; put the command alone "
                           f"on its line: {line.strip()[:120]}")
                    if strict:
                        raise ValueError(why)
                    if why not in _FUSED_WARNED:  # live DAGs already hold such lines; they still load
                        _FUSED_WARNED.add(why)
                        log.warning("%s", why)
            self._nodes[id] = deepcopy(item)
        for id, item in self._nodes.items():
            for dependency in item["inputs"]:
                if dependency not in self._nodes:
                    raise ValueError(f"node {id}: missing input {dependency}")
        try:
            TopologicalSorter({id: n["inputs"] for id, n in self._nodes.items()}).prepare()
        except CycleError as exc:
            raise ValueError("cycle: " + " -> ".join(exc.args[1])) from None

    def start_nodes(self) -> list[str]:
        """Return IDs with no inputs, in file order."""
        return [id for id, item in self._nodes.items() if not item["inputs"]]

    def dependents(self, id: str) -> list[str]:
        """Return direct dependent IDs, in file order."""
        self._nodes[id]  # Unknown IDs are errors, not leaf nodes.
        return [key for key, item in self._nodes.items() if id in item["inputs"]]

    def node(self, id: str) -> dict:
        """Return an independent copy of the node record."""
        return deepcopy(self._nodes[id])


def load(path: str | Path, *, strict: bool = False) -> Dag:
    """Read a JSON object with a nodes list; invalid schemas raise ValueError (``strict``: see Dag)."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("nodes"), list):
        raise ValueError("DAG must be an object with a nodes list")
    return Dag(document["nodes"], strict=strict)


# Files under data/ are never overwritten, so a promoted DAG is a new <feature>.dag.vN.json beside the base
# <feature>.dag.json. Every reader resolves the live DAG here; no caller builds a DAG path of its own.
# Only versions promote wrote count: it appends {feature, version, sha256, promoted_at} to REGISTRY in the same
# folder. Other vN files (scratch copies written when the rails refused an overwrite) are ignored, never live.
BASE_SUFFIX = ".dag.json"
REGISTRY = "dag-promotions.jsonl"


def _versions(folder: Path, feature: str) -> list[tuple[int, Path]]:
    pattern = re.compile(re.escape(feature) + r"\.dag\.v([0-9]+)\.json")
    return sorted((int(match[1]), path) for path in Path(folder).glob(f"{feature}.dag.v*.json")
                  if (match := pattern.fullmatch(path.name)))


def _registered(folder: Path, feature: str) -> dict[int, str]:
    """{version: sha256} from the registry rows for feature; unreadable or malformed rows are skipped."""
    try:
        lines = (folder / REGISTRY).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    rows = {}
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and row.get("feature") == feature and type(row.get("version")) is int
                and isinstance(row.get("sha256"), str)):
            rows[row["version"]] = row["sha256"]
    return rows


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


_WARNED: set[tuple[Path, str]] = set()


def live_path(folder: str | Path, feature: str) -> Path:
    """The highest registered <feature>.dag.vN.json whose hash matches its row, else <feature>.dag.json.

    Unregistered or mismatched version files are ignored, with one warning line naming them.
    """
    folder = Path(folder)
    registered = _registered(folder, feature)
    chosen, ignored = None, []
    for version, path in reversed(_versions(folder, feature)):
        if version not in registered:
            ignored.append(f"{path.name} (unregistered)")
        elif chosen is None and _sha256(path) == registered[version]:
            chosen = path
        elif chosen is None:
            ignored.append(f"{path.name} (hash differs from its registry row)")
    message = f"live DAG for {feature} ignores " + ", ".join(reversed(ignored))
    if ignored and (folder, message) not in _WARNED:  # once per process, not once per dispatcher tick
        _WARNED.add((folder, message))
        log.warning("%s", message)
    return chosen or folder / f"{feature}{BASE_SUFFIX}"


def proposed_path(folder: str | Path, feature: str) -> Path:
    return Path(folder) / f"{feature}-proposed{BASE_SUFFIX}"


def next_path(folder: str | Path, feature: str) -> Path:
    versions = _versions(Path(folder), feature)
    return Path(folder) / f"{feature}.dag.v{versions[-1][0] + 1 if versions else 1}.json"


def feature_ids(folder: str | Path) -> list[str]:
    """Names with a base <name>.dag.json in folder, in file-name order (proposals included; callers filter)."""
    return [path.name.removesuffix(BASE_SUFFIX) for path in sorted(Path(folder).glob(f"*{BASE_SUFFIX}"))]


# The fields that say what a node is; a started node whose spec changes would carry its old status to new work.
SPEC_FIELDS = ("title", "task", "acceptance", "kind", "inputs", "files", "m1_command", "m1")


def promote(folder: str | Path, feature: str, rows: dict) -> Path:
    """Copy the validated proposal to the next version, create-only; ``rows`` are the completion map's node rows.

    Refuses when a live node that is not done is absent from the proposal, when a node in both has a status
    other than queued and a changed spec, or when the proposal equals the live DAG. Nothing is overwritten,
    renamed or deleted, and completion rows are not touched. The caller holds the feature's dispatch lock.
    """
    live_file = live_path(folder, feature)
    live = load(live_file)
    source = proposed_path(folder, feature)
    proposed = load(source)
    # a new or changed node never carries a command fused with prose; an unchanged inherited line (a done node's)
    # only warns, or such a node, neither droppable nor changeable, would block every later promote (review o-rev-vf)
    for id, item in proposed._nodes.items():
        if id in live._nodes and live._nodes[id]["acceptance"] == item["acceptance"]:
            continue
        for number, line in enumerate(item["acceptance"].splitlines(), 1):
            if fused_command(line):
                raise ValueError(f"promote refused: node {id}: acceptance line {number} fuses a command with prose; "
                                 f"put the command alone on its line: {line.strip()[:120]}")

    def status(id):
        row = rows.get(id)
        return row.get("status") if isinstance(row, dict) else None

    dropped = [id for id in live._nodes if id not in proposed._nodes and status(id) != "done"]
    if dropped:
        raise ValueError(f"promote refused: live nodes not done and absent from {source.name}: " + ", ".join(dropped))
    changed = [id for id in live._nodes if id in proposed._nodes and status(id) not in (None, "queued")
               and any(live._nodes[id].get(k) != proposed._nodes[id].get(k) for k in SPEC_FIELDS)]
    if changed:
        raise ValueError(f"promote refused: started nodes whose spec changes in {source.name}: " + ", ".join(changed))
    if json.loads(source.read_text(encoding="utf-8")) == json.loads(live_file.read_text(encoding="utf-8")):
        raise ValueError(f"promote refused: {source.name} is identical to the live DAG {live_file.name}")
    target = next_path(folder, feature)
    content = source.read_bytes()
    with target.open("xb") as output:
        output.write(content)
    _append_row(folder, feature, int(target.name.removeprefix(f"{feature}.dag.v").removesuffix(".json")), content)
    return target


def _append_row(folder: str | Path, feature: str, version: int, content: bytes) -> None:
    row = {"feature": feature, "version": version, "sha256": hashlib.sha256(content).hexdigest(),
           "promoted_at": datetime.now(UTC).isoformat()}
    with (Path(folder) / REGISTRY).open("a", encoding="utf-8") as registry:  # append-only, never rewritten
        registry.write(json.dumps(row) + "\n")


def register(folder: str | Path, feature: str, version: int) -> Path:
    """Register an existing <feature>.dag.v<version>.json (one promoted before the registry existed).

    Refuses a missing or invalid DAG and a version that already has a row; appends one row with the file's hash.
    The file itself is only read. The caller holds the feature's dispatch lock.
    """
    if type(version) is not int or version < 1:
        raise ValueError("version must be a positive integer")
    path = Path(folder) / f"{feature}.dag.v{version}.json"
    if version in _registered(Path(folder), feature):
        raise ValueError(f"register refused: {path.name} is already registered")
    content = path.read_bytes()
    load(path)  # an older promoted file may hold a fused line; it only warns
    _append_row(folder, feature, version, content)
    return path
