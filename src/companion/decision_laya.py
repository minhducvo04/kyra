"""Laya decision backend (RQ45): the client for the resident worker, the paths, and the head metrics.

The worker (`scripts/decision_worker.py`, run in `.venv-laya`) loads the Laya 421M encoder once and keeps one trained
head per decision family under `data/decisions/heads/<family>/`. It listens on a unix socket under `data/decisions/`
and answers one JSON line per request: {"family", "state", "options"} -> {"probs": {option: p}, "ms"}.

`client(family, state, options, timeout=2.0)` is the backend `companion.decide` (RQ44) calls. It returns the probability
per option, or None on any error, a refusal or a timeout: the caller then abstains and runs today's path. Nothing leaves
the Mac (a unix socket only). Stdlib only, Python 3.9-compatible: the trainer in `.venv-laya` imports the metrics here.
"""
from __future__ import annotations

import json
import math
import os
import socket
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from companion.decide import BackendResult

SOCKET_NAME = "laya.sock"
TIMEOUT_S = 2.0
MAX_STATE_CHARS = 4000
MAX_OPTIONS = 256  # full page catalogue and lane slots


def decisions_dir() -> Path:
    from companion.paths import DATA_DIR
    return Path(DATA_DIR) / "decisions"


def socket_path() -> Path:
    """KYRA_DECISION_SOCKET overrides (macOS socket paths must stay under 104 bytes)."""
    env = os.environ.get("KYRA_DECISION_SOCKET")
    return Path(env) if env else decisions_dir() / SOCKET_NAME


def client(family: str, state: str, options: list, *, timeout: float = TIMEOUT_S, path: Path | None = None) -> dict | BackendResult | None:
    """Scores, mapped scores/abstention, or None for an invalid/unavailable worker."""
    if not isinstance(family, str) or not family or not isinstance(state, str) or not isinstance(options, list) \
            or not options or len(options) > MAX_OPTIONS or not all(isinstance(o, str) and o for o in options):
        return None
    request = json.dumps({"family": family, "state": state[:MAX_STATE_CHARS], "options": options}) + "\n"
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(path or socket_path()))
            sock.sendall(request.encode("utf-8"))
            data = b""
            while not data.endswith(b"\n"):
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                sock.settimeout(left)
                chunk = sock.recv(65536)
                if not chunk or len(data) > 1 << 20:
                    return None
                data += chunk
        reply = json.loads(data.decode("utf-8"))
    except (OSError, ValueError):  # no worker, timeout (socket.timeout is an OSError), garbage
        return None
    from companion.decide import BackendResult
    if isinstance(reply, dict) and reply.get('abstain') is True:
        reason = reply.get('reason')
        if reason in {'ask', 'below threshold', 'options outside head', 'option unavailable'}:
            return BackendResult({}, reason=reason)
        return None
    probs = reply.get("probs") if isinstance(reply, dict) else None
    if not isinstance(probs, dict) or set(probs) != set(options):
        return None
    try:
        out = {o: float(probs[o]) for o in options}
    except (TypeError, ValueError):
        return None
    mass = reply.get('unassigned_mass', 0.0)
    if (type(mass) not in (int, float) or not math.isfinite(mass) or not 0 <= mass <= 1
            or any(not math.isfinite(p) or p < 0 or p > 1 for p in out.values())
            or abs(sum(out.values()) + mass - 1) > 1e-6):
        return None
    return BackendResult(out, mass) if mass else out


# ---- head metrics (used by scripts/decision_train.py; pure) -------------------------------------------------------

def accuracy(rows: list) -> float:
    """rows: [(probs: list[float], label_index: int)]."""
    return sum(1 for p, y in rows if max(range(len(p)), key=p.__getitem__) == y) / len(rows) if rows else 0.0


def ece(rows: list, bins: int = 10) -> float:
    """Expected calibration error of the top probability, equal-width bins."""
    if not rows:
        return 0.0
    total, err = len(rows), 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        group = [(max(p), max(range(len(p)), key=p.__getitem__) == y) for p, y in rows
                 if lo < max(p) <= hi or (b == 0 and max(p) == 0)]
        if group:
            conf = sum(c for c, _ in group) / len(group)
            acc = sum(1 for _, ok in group if ok) / len(group)
            err += len(group) / total * abs(conf - acc)
    return err


def risk_coverage(rows: list, thresholds=(0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95)) -> list:
    """[(threshold, coverage, accuracy on the covered rows)]: act only when the top probability >= threshold."""
    out = []
    for t in thresholds:
        kept = [(p, y) for p, y in rows if max(p) >= t]
        out.append((t, len(kept) / len(rows) if rows else 0.0, accuracy(kept) if kept else None))
    return out
