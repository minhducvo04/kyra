"""Deliver once via Codex exec or the Claude courier; never retry uncertainty.

Callers supply a freshly selected AppTask and correlate rollout lines to this
delivery. Refresh is pure; observe persists later observations under the send lock.
"""

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from companion import app_tasks, delivery_ledger
from companion.file_lock import exclusive
from companion.paths import DATA_DIR, write_json

DEFAULT_STORE = delivery_ledger.DEFAULT_STORE
DEFAULT_PINS = DATA_DIR / "dispatch" / "destinations.json"
_OPEN = delivery_ledger.OPEN_STATES


class DuplicateDelivery(ValueError):
    """An assignment already has an open or uncertain delivery."""


class StaleResult(ValueError):
    """A previous result must not establish completion of a new delivery."""


def load_pins(path=DEFAULT_PINS):
    path = Path(path)
    if not path.exists():
        return {"codex": None, "claude": None}
    pins = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(pins, dict) or set(pins) != {"codex", "claude"}
            or any(value is not None and not app_tasks._text(value) for value in pins.values())):
        raise ValueError("Invalid destination pins")
    return pins


def save_pin(app, task_id, path=DEFAULT_PINS):
    if app not in {"codex", "claude"} or (task_id is not None and not app_tasks._text(task_id)):
        raise ValueError("Invalid destination pin")
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as lock, exclusive(lock):
        pins = load_pins(path)
        pins[app] = task_id
        write_json(path, pins)
    return pins


def load(store_path=DEFAULT_STORE, *, legacy_store_path=None, max_bytes=None):
    """Compatibility entry point; storage belongs to delivery_ledger."""
    return delivery_ledger.read(store_path, legacy_store_path=legacy_store_path, max_bytes=max_bytes)


def send(task, assignment_path, result_path, *, kind, store_path=DEFAULT_STORE,
         runner=subprocess.run, now=time.time, preserve_model=False, legacy_store_path=None):
    """Persist uncertainty before sending, then record the transport outcome.

    A lock serializes check/reservation/send/write across processes. If killed
    after reservation, the durable ambiguous record prevents automatic resend.
    The Claude courier's own model is fixed by V1; recorded model/effort describe
    the intended worker, not a verified change to the receiving Claude session.
    """
    assignment, result = Path(assignment_path), Path(result_path)
    if not assignment.is_absolute() or not result.is_absolute():
        raise ValueError("Delivery requires absolute assignment and result paths")
    assignment, result = str(assignment.resolve()), str(result.resolve())
    # Only Claude has a receiving app session whose model can be preserved.
    model, effort = ((None, None) if preserve_model and task.app == "claude"
                     else app_tasks.pick_model(kind)[task.app])
    if task.app == "codex":
        argv = app_tasks.codex_exec_command(task, assignment, model=model, effort=effort)
    else:
        argv = app_tasks.claude_courier_command(task, assignment)
    with delivery_ledger.locked(store_path, legacy_store_path) as ledger:
        rows = ledger.rows
        if os.path.lexists(result_path):
            raise StaleResult("A result already exists; review and move it before sending again")
        if any(str(Path(row["assignment"]).resolve()) == assignment and row["state"] in _OPEN for row in rows):
            raise DuplicateDelivery("Assignment already has an open or ambiguous delivery")
        offset = 0
        rollout = task.source if task.app == "claude" else None
        if rollout:
            try:
                offset = Path(rollout).stat().st_size
            except FileNotFoundError:
                pass  # A discovered task can create its rollout after delivery.
        delivery = {"id": uuid.uuid4().hex, "app": task.app, "task_id": task.id, "task_name": task.name,
                    "assignment": assignment, "result": result, "kind": kind, "model": model, "effort": effort,
                    "state": "ambiguous", "sent_at": now(), "detail": "Reserved; send outcome not yet known",
                    "rollout": rollout, "rollout_offset": offset}
        ledger.append(delivery)
        try:
            done = runner(argv, cwd=task.cwd, timeout=1800 if task.app == "codex" else 30, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, shell=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            delivery["detail"] = f"{type(exc).__name__}: delivery uncertain; inspect before retrying"
        else:
            accepted = done.returncode == 0
            lines = (done.stdout or "").strip().splitlines()
            if task.app == "claude":
                accepted = accepted and bool(lines) and lines[-1] == f"KYRA-DELIVERED {task.name}"
            delivery["state"] = "queued" if accepted else "failed"
            delivery["detail"] = ("Transport accepted; acknowledgment pending" if accepted
                                  else lines[-1] if task.app == "claude" and lines
                                  else f"Transport exited with status {done.returncode}")
        ledger.save()
    return delivery


def refresh(delivery, *, rollout_lines=None, claude_status=None, exists=os.path.exists):
    """Observe a delivery without mutating it or reviving an uncertain send."""
    updated = dict(delivery)
    if delivery["state"] in {"failed", "ambiguous", "completed"}:
        return updated
    if delivery["app"] == "codex" and rollout_lines is not None:
        rollout_lines = list(rollout_lines)
        reply = app_tasks.codex_reply(rollout_lines, marker=delivery["assignment"])
        if reply and not updated.get("reply"):
            updated["reply"] = reply
    if exists(delivery["result"]):
        updated["state"] = "completed"
    elif delivery["app"] == "codex" and rollout_lines is not None:
        state = app_tasks.codex_state(rollout_lines, marker=delivery["assignment"])
        if state in {"running", "acknowledged"}:
            updated["state"] = state
        elif updated.get("reply"):
            updated["state"] = "acknowledged"
    elif delivery["app"] == "claude" and claude_status == "busy":
        updated["state"] = "running"
    return updated


def observe(store_path=DEFAULT_STORE, *, read_rollout, claude_status, exists=os.path.exists,
            legacy_store_path=None):
    """Persist observations without racing sends or retrying uncertain deliveries."""
    with delivery_ledger.locked(store_path, legacy_store_path) as ledger:
        rows = ledger.rows
        updated = []
        for row in rows:
            if row["state"] not in {"queued", "acknowledged", "running"}:
                updated.append(row)
                continue
            lines, status = None, None
            if row["app"] == "codex" and row.get("rollout"):
                lines = read_rollout(row["rollout"], row.get("rollout_offset", 0))
            elif row["app"] == "claude":
                status = claude_status(row["task_id"])
            updated.append(refresh(row, rollout_lines=lines, claude_status=status, exists=exists))
        if updated != rows:
            ledger.save(updated)
    return updated
