"""Agent control plane: the normalized event model, its store and redaction.

Metadata only, local only: events live under ``<data dir>/agent_control/``
and nothing in this package makes a network call or opens a secret file.
"""

from companion.agent_control.events import (
    ACTIONS,
    ACTORS,
    ATTRS_KEYS,
    FIELDS,
    GENESIS,
    OUTCOMES,
    SOURCES,
    Event,
    adapters,
    make_event_id,
    register,
    unregister,
    validate_event,
)
from companion.agent_control.redact import scrub, scrub_text
from companion.agent_control.store import ChainReport, EventStore

__all__ = [
    "ACTIONS", "ACTORS", "ATTRS_KEYS", "FIELDS", "GENESIS", "OUTCOMES", "SOURCES",
    "ChainReport", "Event", "EventStore", "adapters", "make_event_id", "register",
    "scrub", "scrub_text", "unregister", "validate_event",
]
