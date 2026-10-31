"""Decision log: one append-only logger every decision point can call in one line.

Rows go to data/decisions/<family>.jsonl. log() records a decision and returns its state_hash; outcome() appends
a later result keyed by that hash; read() yields rows back. Logging never raises into the caller: any failure goes
to logging.warning and the call returns None or False. A state whose text matches the health or family wording (the
health_words.denied detector) is refused and never written.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from companion import health_words, paths

_log = logging.getLogger(__name__)

ROTATE_BYTES = 50 * 1024 * 1024
_FAMILY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def decisions_dir() -> Path:
    return paths.DATA_DIR / "decisions"


def _family_path(family: str) -> Path:
    if not isinstance(family, str) or not _FAMILY_RE.match(family):
        raise ValueError(f"bad decision family: {family!r}")
    return decisions_dir() / f"{family}.jsonl"


def _state_hash(state) -> str:
    blob = json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _append(family: str, row: dict) -> None:
    path = _family_path(family)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.stat().st_size >= ROTATE_BYTES:
        path.rename(path.with_name(f"{family}.{time.time_ns()}.jsonl"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def log(family: str, state, options, choice, source: str, confidence: float | None = None,
        latency_ms: float | None = None) -> str | None:
    """Append one decision row. Returns its state_hash, or None when refused or on error."""
    try:
        state_text = json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)
        if health_words.denied(state_text):
            _log.warning("decision_log refused a health-worded state for family %s", family)
            return None
        state_hash = _state_hash(state)
        row = {"t": time.time(), "state_hash": state_hash, "state": state, "options": options,
               "choice": choice, "source": source, "confidence": confidence, "latency_ms": latency_ms}
        _append(family, row)
        return state_hash
    except Exception as exc:  # never raise into the caller
        _log.warning("decision_log could not write family %s: %s", family, exc)
        return None


def outcome(family: str, state_hash: str, outcome) -> bool:
    """Append an outcome row joined to a decision by state_hash. Returns True when written."""
    try:
        _append(family, {"t": time.time(), "state_hash": state_hash, "outcome": outcome})
        return True
    except Exception as exc:
        _log.warning("decision_log could not write outcome for family %s: %s", family, exc)
        return False


def read(family: str, since: float = 0.0) -> Iterator[dict]:
    """Yield rows of a family with t >= since, oldest first. A missing or unreadable file yields nothing."""
    try:
        path = _family_path(family)
    except ValueError as exc:
        _log.warning("decision_log read refused: %s", exc)
        return
    if not path.is_file():
        return
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and float(row.get("t", 0)) >= since:
                    yield row
    except OSError as exc:
        _log.warning("decision_log read failed for family %s: %s", family, exc)


SHADOW_FAMILIES = ("home_router", "lead_route", "rerank", "memory_gate")
SHADOW_BUDGET_S = 0.1


def pair(family: str, state_hash: str | None, options, live_choice, live_source: str, shadow: dict, source: str = "live", *, state=None, pair_id=None) -> bool:
    """Append one live/shadow pair row (hashes and choices only). `shadow` holds choice, probs, abstain, ms, reason."""
    try:
        row = {"t": time.time(), "kind": "pair", "state_hash": state_hash, "options": options,
               "live_choice": live_choice, "live_source": live_source, "source": source,
               "shadow_choice": shadow.get("choice"), "shadow_probs": shadow.get("probs") or {},
               "shadow_abstain": bool(shadow.get("abstain", True)), "shadow_ms": shadow.get("ms"),
               "shadow_reason": str(shadow.get("reason", ""))[:64]}
        if state is not None:
            from companion.jev_states import validate
            validate(family, state)
            if not isinstance(pair_id, str) or not re.fullmatch(r"[0-9a-f]{32}", pair_id):
                raise ValueError("invalid pair id")
            row.update(state=state, pair_id=pair_id)
        # Registered option ids (decide() enforces them) are fixed vocabulary, not text: the catalogue's "health" node
        # id must not trip the wording check. Anything not registered, and every other field, is still checked.
        from companion import decide
        reg = set(decide.FAMILIES[family].options) if family in decide.FAMILIES else set()
        checked = {k: v for k, v in row.items() if k not in ("shadow_choice", "shadow_probs", "options", "live_choice")}
        checked["options"] = [o for o in (options if isinstance(options, list) else [options]) if o not in reg]
        checked["live_choice"] = [c for c in (live_choice if isinstance(live_choice, list) else [live_choice]) if c not in reg]
        checked["shadow"] = [k for k in (shadow.get("probs") or {}) if k not in reg] + (
            [shadow.get("choice")] if shadow.get("choice") not in reg else [])
        if health_words.denied(json.dumps(checked, ensure_ascii=False, default=str)):
            _log.warning("decision_log refused a health-worded pair for family %s", family)
            return False
        _append(family, row)
        return True
    except Exception as exc:
        _log.warning("decision_log could not write pair for family %s: %s", family, exc)
        return False


def _shadow_run(family, state_hash, options, live_choice, live_source, decide_state=None, decide_options=None, request_source="live") -> None:
    """Ask decide() about the in-memory input (decide_state) and log only the hash, choices and probabilities."""
    result: dict = {}

    def ask():
        try:
            from companion import decide
            d = decide.decide(family, decide_state if decide_state is not None else state_hash,
                              list(decide_options if decide_options is not None else options))
            result.update(choice=d.choice, probs=d.probs, abstain=d.abstain, reason=d.reason)
        except BaseException as exc:
            result.update(choice=None, abstain=True, reason=f"error:{type(exc).__name__}")

    start = time.monotonic()
    worker = threading.Thread(target=ask, daemon=True)
    worker.start()
    worker.join(SHADOW_BUDGET_S)
    if worker.is_alive():
        result = {"choice": None, "abstain": True, "reason": "timeout"}
    result["ms"] = (time.monotonic() - start) * 1000
    pair(family, state_hash, options, live_choice, live_source, result, source=request_source)


def _shadow_on(family) -> bool:
    return family in SHADOW_FAMILIES and os.environ.get(f"KYRA_DECIDE_SHADOW_{family.upper()}") == "1"


def log_shadow(family: str, state, options, choice, source: str, confidence: float | None = None,
               latency_ms: float | None = None, shadow_state: str | None = None,
               shadow_options=None, *, request_source="live") -> str | None:
    """log(), then when KYRA_DECIDE_SHADOW_<FAMILY>=1 start a daemon thread that asks decide() and logs a pair row.

    decide() gets `shadow_state` (the real in-memory input) and `shadow_options` (the family's fixed set); neither is
    ever written: the log gets the state hash and choices only. Without shadow_state it gets the hash. Never waits.
    """
    state_hash = log(family, state, options, choice, source, confidence, latency_ms)
    try:
        if state_hash and _shadow_on(family):
            threading.Thread(target=_shadow_run, args=(family, state_hash, options, choice, source,
                                                       shadow_state, shadow_options, request_source), daemon=True).start()
    except Exception as exc:
        _log.warning("decision_log shadow not started for family %s: %s", family, exc)
    return state_hash


def shadow_many(family: str, items) -> None:
    """Pointwise shadow: `items` is a list, or a callable that builds it (run in the thread, so a flag-off call costs
    nothing). One daemon thread walks the items in order (decide() allows one call at a time), and for each
    logs a decision row and a pair row. Each item is a dict with state, options, choice, source, shadow_state,
    shadow_options. Does nothing when the family flag is off. Never waits, never raises."""
    try:
        if not _shadow_on(family):
            return

        def walk():
            for it in (items() if callable(items) else items):
                state_hash = log(family, it["state"], it["options"], it["choice"], it["source"])
                if state_hash:
                    _shadow_run(family, state_hash, it["options"], it["choice"], it["source"],
                                it.get("shadow_state"), it.get("shadow_options"))
        threading.Thread(target=walk, daemon=True).start()
    except Exception as exc:
        _log.warning("decision_log pointwise shadow not started for family %s: %s", family, exc)


GATE_FILE = "live-gate"  # data/decisions/live-gate.jsonl: one row per live/shadow verdict; not a decision family


def gate(verdict: dict) -> bool:
    """Append one live-gate row (the family, the verdict, its threshold and numbers; never state text)."""
    try:
        keys = ("family", "live", "reason", "m", "n", "dev_n", "covered", "coverage", "rules_acc", "head_acc",
                "head_covered_agreement", "rules_covered_acc", "ask_rows", "ask_acted", "ece", "no_answer", "head")
        # "t" is the row time like every other row; the verdict's threshold t is stored as "threshold"
        row = {"t": time.time(), "kind": "gate", "threshold": verdict.get("t"), **{k: verdict.get(k) for k in keys}}
        if not isinstance(row["family"], str) or not _FAMILY_RE.match(row["family"]) or type(row["live"]) is not bool:
            raise ValueError("gate row needs a family and a boolean live")
        _append(GATE_FILE, row)
        return True
    except Exception as exc:
        _log.warning("decision_log could not write a gate row: %s", exc)
        return False


def gates() -> dict:
    """The latest gate row per family ({} when none were logged), in verdict shape (threshold back under "t")."""
    out = {}
    for row in read(GATE_FILE):
        if row.get("kind") == "gate" and isinstance(row.get("family"), str):
            out[row["family"]] = {**row, "t": row.get("threshold"), "logged_at": row.get("t")}
    return out


def avoided(family: str, since: float = 0.0) -> dict:
    """Paid or big-model calls the head took over in the window: decisions whose source is "head" (the live head
    answered and the caller never asked the model), plus the pair rows where a shadow head agreed with the live
    choice without abstaining (what going live would have saved)."""
    out = {"decisions": 0, "avoided": 0, "would_avoid": 0, "pairs": 0}
    for row in read(family, since):
        if row.get("kind") == "pair":
            out["pairs"] += 1
            if not row.get("shadow_abstain", True) and row.get("shadow_choice") == row.get("live_choice"):
                out["would_avoid"] += 1
        elif "choice" in row:
            out["decisions"] += 1
            out["avoided"] += row.get("source") == "head"
    return out


def training_allowed(row: dict) -> bool:
    """Held-out evaluation rows must never become training examples."""
    source = row.get("source", "live")
    return isinstance(source, str) and source.strip().lower() in {"live", "synthetic"}
