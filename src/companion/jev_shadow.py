"""Opt-in Jev shadow only. No model loading, authorization or live-result changes."""
import hashlib
import json
import os
import threading
import time
import uuid
from queue import Empty, Queue

from companion import decide, decision_log, jev_states

FAMILIES = {"assign", "recovery", "context_select", "test_select"}
BUDGET_SECONDS = .1
# Keep a hung decide call from creating unbounded abandoned daemon threads.
_SLOT = threading.BoundedSemaphore(1)


def shadow(family, state, options, live_choice, *, pair_id=None):
    """Return live_choice unchanged; at most 100 ms waiting for a shadow decision.

    Only validated structured state, option labels and hashes are persisted. Callers supply
    jev_states projections; free-form result reasons never enter the log.
    """
    if family not in FAMILIES or os.getenv(f"KYRA_JEV_SHADOW_{family.upper()}") != "1":
        return live_choice
    try:
        entry = decide.FAMILIES.get(family)
        if entry is None or tuple(options) != entry.options or live_choice not in entry.options:
            return live_choice
        jev_states.validate(family, state)
        options = list(options)
        serialized = json.dumps(state, sort_keys=True)
        state_hash = hashlib.sha256(serialized.encode()).hexdigest()[:16]
        if not _SLOT.acquire(blocking=False):
            return live_choice
        output = Queue(maxsize=1)

        def ask():
            try:
                output.put(decide.decide(family, serialized, options))
            except Exception:
                output.put(None)
            finally:
                _SLOT.release()

        started = time.monotonic()
        try:
            threading.Thread(target=ask, daemon=True).start()
        except Exception:
            _SLOT.release()
            return live_choice
        result = {"choice": None, "probs": {}, "abstain": True, "reason": "timeout"}
        try:
            d = output.get(timeout=BUDGET_SECONDS)
            if d is not None and (d.choice is None or d.choice in options) and set(d.probs) <= set(options) and all(decide._probability(v) for v in d.probs.values()):
                result = {"choice": d.choice, "probs": d.probs, "abstain": d.abstain,
                          "reason": "abstain" if d.abstain else "decision"}
        except Empty:
            pass
        result["ms"] = (time.monotonic() - started) * 1000
        decision_log.pair(family, state_hash, options, live_choice, "live", result, source="live",
                          state=json.loads(serialized), pair_id=pair_id or uuid.uuid4().hex)
    except Exception:
        pass  # Shadow infrastructure cannot interrupt the live caller.
    return live_choice


def outcome(family, pair_id, gold):
    """Record an independently observed boolean outcome, never a model verdict.

    assign: caller supplies whether the chosen lane finished with PASS from
    lane-watch/verdict evidence. recovery: whether that rung resolved the case.
    Read pair_id from the pair row; shadow's live return value is unchanged.
    """
    if family not in {"assign", "recovery"} or type(gold) is not bool:
        return False
    try:
        rows = list(decision_log.read(family))
        if not any(r.get("kind") == "pair" and r.get("pair_id") == pair_id for r in rows):
            return False
        if any(r.get("kind") == "outcome" and r.get("pair_id") == pair_id for r in rows):
            return True
        decision_log._append(family, {"t": time.time(), "kind": "outcome", "pair_id": pair_id,
                                      "gold": gold, "source": "live"})
        return True
    except Exception:
        return False
