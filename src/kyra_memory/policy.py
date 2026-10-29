"""Per-tenant quotas and retention for service (tenant) stores (fork F4, proceeded on guess).

A TenantPolicy bounds one tenant three ways: total rows across all tables,
bytes on disk (database plus WAL/SHM sidecars), and writes in a sliding
60-second window (every committed record is one write, counted after its
transaction commits; a rolled-back batch counts not at all; one batch may
overshoot the bound and still lands, then the tenant is refused until the
window drains). Every
tenant-store write path is checked (backend.add, the inbox merger's add,
proposal, used and metric records, proposal accept, set_weights,
record_outcome), a write over any bound is refused with
QuotaExceededError before it touches disk, and the backend counts the
refusal per tenant and per bound. Rows in the proposals table count
toward max_rows, and the max_bytes check adds the incoming record's text
bytes (body, source, topic, key and, for proposals, refs, quote, origin)
and what the open transaction already added (the larger of its page
growth and the bytes its earlier checks admitted, since a merge batch is
not on disk until it commits and may refill pages released by forget), so
neither one large write nor a long batch can overshoot the bound. After a
commit leaves the tenant over the bound only because the WAL still holds
a copy the autocheckpoint already wrote to the database, the WAL is
truncated; a reader that blocks the truncate keeps the WAL counted, so
the next write is refused instead. Bulk writes are counted by an upper
bound on what they store, not by the inbox line: a ``used`` record by its
usage rows (text, 8 bytes per number, index entry and row overhead per item),
set_weights by its entries (non-numeric weights are refused). Usage,
metrics, applied_outcomes and fact_weights rows are side data and do not
count toward max_rows (it bounds memory rows and proposals, the rows a
caller can read back); max_bytes bounds them. Bounds are per
tenant, so one noisy tenant never stops another.

Known limit (out of scope for MS5): the rate window lives in memory per
SqliteBackend instance, so two backends (or processes) on one tenant each
keep their own window and together can exceed the bound. Rows and
bytes are read from the store, so they hold across instances.

Retention is a TTL in seconds per table. Rows whose knowledge_time (when the
store learned them, never caller-supplied) is older than the TTL are expired
by SqliteBackend.expire(). Pending proposals expire by their learned_at
(the store clock at merge), not by the inbox line's caller-set ``at``.

Why hard delete exists here and not for the owner's own scope: the owner's
store (tenant=None, opened directly through MemoryStore) keeps superseded
rows forever on purpose. That history is the audit trail and powers
``as_of`` ("what did we believe at time T"). A tenant store is service data
held for someone else: it must honour erasure and retention, so forget(),
TTL expiry and delete_tenant() hard-delete (secure_delete, WAL truncation,
VACUUM on expiry, the whole folder on delete) instead of superseding.
Hard delete is only reachable through the tenant-keyed
backend, which refuses a missing tenant id, so it can never hit the
owner's own scope.
"""
import math
from dataclasses import dataclass, field
from datetime import timedelta

from kyra_memory.store import TABLES

RATE_WINDOW_SECONDS = 60.0


class QuotaExceededError(RuntimeError):
    """A write was refused because the tenant is over a policy bound."""

    def __init__(self, tenant: str, kind: str, limit: float, observed: float):
        super().__init__(f"tenant {tenant!r} is over {kind}: {observed} >= {limit}")
        self.tenant = tenant
        self.kind = kind
        self.limit = limit
        self.observed = observed
        self.report = None  # a refused merge sets it to the MergeReport of the inboxes that committed first


@dataclass(frozen=True)
class TenantPolicy:
    """Bounds for one tenant. None means unbounded on that axis."""

    max_rows: int | None = None
    max_bytes: int | None = None
    writes_per_minute: int | None = None
    ttl_seconds: dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        for name in ("max_rows", "max_bytes", "writes_per_minute"):
            value = getattr(self, name)
            if value is None:
                continue
            if type(value) is not int:  # bool and float are refused too
                raise ValueError(f"{name} must be an int or None")
            if value <= 0:
                raise ValueError(f"{name} must be positive or None")
        if not isinstance(self.ttl_seconds, dict):
            raise ValueError("ttl_seconds must be a dict of table to seconds")
        for table, ttl in self.ttl_seconds.items():
            if table not in TABLES:
                raise ValueError(f"unknown table {table!r} in ttl_seconds")
            if type(ttl) is bool or not isinstance(ttl, (int, float)):
                raise ValueError("a TTL must be a number of seconds")
            if not math.isfinite(ttl) or not 0 < ttl <= timedelta.max.total_seconds():
                raise ValueError("a TTL must be positive, finite and no larger than timedelta.max")
