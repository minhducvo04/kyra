"""Model-free closed-question decisions. Confidence is never authorization.

Backends receive (family, state, options) and return {option: probability}.
Calibration format: {family: {"t": minimum_probability, "m": minimum_margin}}.
Register vocabularies before use. Missing calibration always abstains.
"""

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from queue import Empty, Queue
from threading import BoundedSemaphore, Thread
from typing import Literal

from companion.paths import DATA_DIR

Kind = Literal["route", "need_big", "rank", "hold_only"]
THRESHOLDS_PATH = DATA_DIR / "decisions" / "thresholds.json"
TIMEOUT_SECONDS = 2.0
_ALLOWED_HOLD = {"hold", "no_opinion"}
# A timed-out callable cannot be killed safely: retain its slot until it exits.
# This bounds abandoned work; the serving worker must enforce socket deadlines.
_SLOT = BoundedSemaphore(1)


@dataclass(frozen=True)
class Decision:
    choice: str | None
    probs: dict[str, float]
    abstain: bool
    reason: str


@dataclass(frozen=True)
class BackendResult:
    """Catalogue scores retain probability mass for options outside this request."""
    probs: dict[str, float]
    unassigned_mass: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class Family:
    name: str
    kind: Kind
    options: tuple[str, ...]


FAMILIES: dict[str, Family] = {}


def register_family(name: str, kind: Kind, options: list[str]) -> None:
    """Register a fixed vocabulary; conflicting registrations refuse."""
    if not isinstance(name, str) or not name or kind not in ("route", "need_big", "rank", "hold_only"):
        raise ValueError("invalid family")
    if not isinstance(options, list) or not options or any(not isinstance(o, str) or not o for o in options) or len(set(options)) != len(options):
        raise ValueError("invalid options")
    if kind == "hold_only" and not set(options) <= _ALLOWED_HOLD:
        raise ValueError("hold_only cannot authorize an action")
    entry = Family(name, kind, tuple(options))
    if name in FAMILIES and FAMILIES[name] != entry:
        raise ValueError("conflicting family")
    FAMILIES[name] = entry


def _probability(value: object) -> bool:
    return type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value)


def decide(family: str, state: str, options: list[str],
           backend: Callable[[str, str, list[str]], dict[str, float] | BackendResult] | None = None) -> Decision:
    """Abstain on errors, timeouts and hold-only authorization attempts."""
    def abstain(reason, probs=None):
        return Decision(None, probs or {}, True, reason)

    if (not isinstance(family, str) or not isinstance(state, str)
            or not isinstance(options, list) or not options
            or any(not isinstance(o, str) or not o for o in options)):
        return abstain("invalid_request")
    entry = FAMILIES.get(family)
    if entry is None or not isinstance(state, str) or tuple(options) != entry.options:
        return abstain("invalid_request")
    if entry.kind == "hold_only" and not set(entry.options) <= _ALLOWED_HOLD:
        return abstain("invalid_hold_options")
    try:
        threshold = json.loads(THRESHOLDS_PATH.read_text(encoding="utf-8"))[family]
        t, m = threshold["t"], threshold["m"]
        if not _probability(t) or not _probability(m):
            return abstain("invalid_thresholds")
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return abstain("missing_or_invalid_thresholds")
    if backend is None:
        return abstain("backend_unavailable")
    if not _SLOT.acquire(blocking=False):
        return abstain("backend_busy")
    output = Queue(maxsize=1)

    def run():
        try:
            output.put((True, backend(family, state, list(options))))
        except BaseException:
            output.put((False, None))
        finally:
            _SLOT.release()

    try:
        Thread(target=run, daemon=True).start()
    except RuntimeError:
        _SLOT.release()
        return abstain("backend_unavailable")
    try:
        ok, probs = output.get(timeout=TIMEOUT_SECONDS)
    except Empty:
        return abstain("timeout")
    if not ok:
        return abstain("backend_error")
    unassigned = 0.0
    if isinstance(probs, BackendResult):
        if probs.reason:
            return abstain(probs.reason)
        unassigned, probs = probs.unassigned_mass, probs.probs
        if not _probability(unassigned):
            return abstain("invalid_probabilities")
    if not isinstance(probs, dict):
        return abstain("invalid_probabilities")
    if entry.kind == "hold_only" and not set(probs) <= _ALLOWED_HOLD:
        return abstain("invalid_hold_options")
    if set(probs) != set(options) or not all(_probability(p) for p in probs.values()):
        return abstain("invalid_probabilities")
    probs = {o: float(probs[o]) for o in options}
    if not math.isclose(sum(probs.values()) + unassigned, 1.0, abs_tol=1e-6):
        return abstain("invalid_probabilities")
    ranked = sorted(probs, key=probs.get, reverse=True)
    top = probs[ranked[0]]
    second = max(probs[ranked[1]] if len(ranked) > 1 else 0.0, unassigned)
    if top < t or top - second < m:
        return abstain("below_threshold_or_margin", probs)
    choice = ranked[0]
    if entry.kind == "hold_only" and choice not in _ALLOWED_HOLD:
        return abstain("invalid_hold_options")
    return Decision(choice, probs, False, "calibrated")


def _threshold_entry(family: str) -> dict | None:
    try:
        entry = json.loads(THRESHOLDS_PATH.read_text(encoding="utf-8"))[family]
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None
    return entry if isinstance(entry, dict) and _probability(entry.get("t")) and _probability(entry.get("m")) else None


def is_live(family: str) -> bool:
    """A family is live only when its thresholds.json entry carries valid t, m and `"live": true` (written by the
    live gate, scripts/decision_report.py --heldout --apply, after the head beat the rules on the frozen sets)."""
    entry = _threshold_entry(family)
    return bool(entry) and entry.get("live") is True


def live_choice(family: str, state: str, options: list[str], backend=None) -> Decision | None:
    """The head's calibrated Decision where the family is live, else None: the caller then runs today's path.

    Nothing is logged here; the caller logs the result through its usual decision_log call with source "head",
    which is what the weekly "calls avoided" count reads. Confidence is never authorization: the choice is one of
    the registered options and the caller still validates it against its live catalogue.
    """
    if not is_live(family):
        return None
    if backend is None:
        from companion.decision_laya import client as backend
    d = decide(family, state, options, backend)
    return None if d.abstain else d


RULES = {}

register_family("lead_route", "route", sorted(["app_cursor", "app_antigravity", "app_claude", "app_codex", "app_all",
                                                "team_plan", "worker_task", "answer", "clarify"]))
register_family("memory_gate", "route", ["propose", "refuse"])
register_family("rerank", "rank", ["yes", "no"])


def register_assign_family(lane_ids: list[str] | None = None) -> None:
    """Use dispatcher lane ids; absent tables leave assign unavailable until supplied."""
    if lane_ids is None:
        return
    register_family("assign", "route", list(dict.fromkeys(lane_ids)))


register_assign_family()
register_family("recovery", "route", ["retry_once", "send_to_author", "escalate_board", "diagnose", "abstain"])
register_family("context_select", "rank", ["yes", "no"])
register_family("test_select", "rank", ["yes", "no"])
