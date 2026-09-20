"""Session-bound, single-use approvals for normalized tool actions."""
import hashlib
import json
import secrets
import time
from copy import deepcopy
from dataclasses import dataclass, replace
from threading import Lock

from companion.privacy import PrivacyClass, Tier, combine
from companion.provider import current_release_label


class ApprovalError(Exception):
    """The requested approval is missing or cannot be used."""


class ApprovalNotFound(ApprovalError):
    """No action belongs to this id and session."""


@dataclass(frozen=True)
class PendingAction:
    id: str
    tool: str
    arguments: dict
    session: str
    status: str
    created_at: float
    expires_at: float
    label: tuple[Tier, frozenset[PrivacyClass]]


class ApprovalRequired(ApprovalError):
    """A tool turn must pause until the server approves this action."""

    def __init__(self, pending: PendingAction):
        super().__init__("approval required")
        self.pending = pending


def describe(pending: PendingAction) -> str:
    arguments = json.dumps(pending.arguments, ensure_ascii=False, separators=(",", ":"))
    return f"{pending.tool} {arguments}"


def reply_for_result(result) -> str:
    if isinstance(result, dict):
        if "error" in result:
            return f"That did not work: {result['error']}"
        if "note" in result:
            return f"Done. {result['note']}"
    return "Done."


class ApprovalStore:
    """In-memory lifecycle; returned actions never alias the stored arguments."""

    def __init__(self, ttl_seconds=300, clock=time.time):
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = Lock()
        self._actions: dict[str, PendingAction] = {}
        self._pending_ids: dict[tuple[str, str, str], str] = {}
        # (count, id) of the newest request per session: a turn that pauses on an action, new or already pending,
        # is how a conversation knows which action its next reply may answer.
        self._requests: dict[str, tuple[int, str]] = {}

    def request(self, tool: str, arguments: dict, *, session="local") -> PendingAction:
        arguments = deepcopy(arguments)
        canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), allow_nan=False)
        key = (tool, hashlib.sha256(canonical.encode()).hexdigest(), session)
        label = current_release_label()
        with self._lock:
            now = self._clock()
            action_id = self._pending_ids.get(key)
            if action_id is not None:
                action = self._expire(self._actions[action_id], now)
                if action.status == "pending":
                    action = replace(action, label=combine([action.label, label]))
                    self._actions[action.id] = action
                    self._requests[session] = (self._requests.get(session, (0, ""))[0] + 1, action.id)
                    return deepcopy(action)
            action_id = secrets.token_urlsafe()
            action = PendingAction(action_id, tool, arguments, session, "pending", now, now + self._ttl_seconds, label)
            self._actions[action_id] = action
            self._pending_ids[key] = action_id
            self._requests[session] = (self._requests.get(session, (0, ""))[0] + 1, action_id)
            return deepcopy(action)

    def request_count(self, session: str) -> int:
        with self._lock:
            return self._requests.get(session, (0, ""))[0]

    def last_requested(self, session: str) -> str | None:
        with self._lock:
            return self._requests.get(session, (0, None))[1]

    def approve(self, action_id: str, *, session: str) -> PendingAction:
        return self._transition(action_id, session, {"pending"}, "approved")

    def deny(self, action_id: str, *, session: str) -> PendingAction:
        return self._transition(action_id, session, {"pending", "approved"}, "denied")

    def consume(self, action_id: str, *, session: str) -> PendingAction:
        """Claim before execution, so a failed or concurrent run cannot replay it."""
        return self._transition(action_id, session, {"approved"}, "consumed")

    def pending(self, session: str) -> list[PendingAction]:
        with self._lock:
            now = self._clock()
            actions = [self._expire(action, now) for action in self._actions.values() if action.session == session]
            return deepcopy([action for action in actions if action.status == "pending"])

    def _expire(self, action: PendingAction, now: float) -> PendingAction:
        # Called only while holding the lifecycle lock.
        if action.status in {"pending", "approved"} and now >= action.expires_at:
            action = replace(action, status="expired")
            self._actions[action.id] = action
        return action

    def _transition(self, action_id: str, session: str, allowed: set[str], status: str) -> PendingAction:
        with self._lock:
            action = self._actions.get(action_id)
            if action is None or action.session != session:
                raise ApprovalNotFound("approval not found for this session")
            action = self._expire(action, self._clock())
            if action.status not in allowed:
                raise ApprovalError(f"approval is {action.status}")
            action = replace(action, status=status)
            self._actions[action_id] = action
            return deepcopy(action)
