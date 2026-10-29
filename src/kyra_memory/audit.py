"""Per-tenant audit log and privacy label around any Backend. Audit rows hold ids and counts, never body text."""
import json
import time
from dataclasses import dataclass
from pathlib import Path

from kyra_memory.backend import Backend
from kyra_memory.labels import PrivacyClass, Tier
from kyra_memory.tenancy import TenantError, check_tenant


class PrivacyRefused(PermissionError):
    """A read would return a row above the tenant's label."""


@dataclass(frozen=True)
class TenantLabel:
    max_tier: Tier
    classes: frozenset[PrivacyClass] = frozenset()

    def allows(self, label: tuple[Tier, frozenset[PrivacyClass]]) -> bool:
        tier, classes = label
        return tier <= self.max_tier and classes <= self.classes


class AuditedBackend:
    """Wraps a Backend: one JSONL audit file per tenant, and a label gate on every read.

    Search and history drop rows above the tenant label and record how many were withheld.
    A direct read (current) still refuses when a returned row is above the label.
    """

    def __init__(self, inner: Backend, audit_dir: Path | str, labels: dict[str, TenantLabel], *, actor: str = "system"):
        self.inner = inner
        self.audit_dir = Path(audit_dir)
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.labels = labels
        self.actor = actor

    def audit_path(self, tenant: str) -> Path:
        # check_tenant returns None for a missing tenant; never let that become a shared "None.jsonl".
        if check_tenant(tenant) is None:
            raise TenantError("audit needs a tenant id")
        return self.audit_dir / f"{tenant}.jsonl"

    def _log(self, tenant, action, table, row_id, result, *, withheld=0):
        row = {"time": time.time(), "tenant": tenant, "actor": self.actor, "action": action,
               "table": table, "row_id": row_id, "result": result}
        if withheld:
            row["withheld"] = withheld  # a count only: never the text of a row that was dropped
        with self.audit_path(tenant).open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def _call(self, tenant, action, table, row_id, fn, *, gate=False, drop=False):
        withheld = 0
        try:
            out = fn()
            if gate:
                label = self.labels.get(tenant)
                if drop:
                    kept = []
                    for r in out:
                        if label is None or not label.allows(r.label):
                            withheld += 1
                        else:
                            kept.append(r)
                    out = kept
                else:
                    for r in out:
                        if label is None or not label.allows(r.label):
                            raise PrivacyRefused(f"{action} on {tenant}/{table} returns a row above the tenant label")
        except PrivacyRefused:
            self._log(tenant, action, table, row_id, "refused")
            self._log_completed(tenant)
            raise
        except Exception as exc:
            self._log(tenant, action, table, row_id, f"error:{type(exc).__name__}")
            self._log_completed(tenant)
            raise
        self._log(tenant, action, table, row_id, "ok", withheld=withheld)
        self._log_completed(tenant)
        return out

    def _log_completed(self, tenant):
        """A purge a reader blocked (its forget logged an error) that a later call finished on disk."""
        take = getattr(self.inner, "completed_purges", None)
        for entry in (take(tenant) if take and check_tenant(tenant) is not None else []):
            self._log(tenant, "purge_completed", entry.get("table"), entry.get("row_id"), "ok")

    def open_tenant(self, tenant, *, create=False):
        return self._call(tenant, "open_tenant", None, None, lambda: self.inner.open_tenant(tenant, create=create))

    def add(self, tenant, table, body, *, source, **fields):
        return self._call(tenant, "add", table, None, lambda: self.inner.add(tenant, table, body, source=source, **fields))

    def current(self, tenant, table):
        return self._call(tenant, "current", table, None, lambda: self.inner.current(tenant, table), gate=True)

    def search(self, tenant, table, text):
        return self._call(tenant, "search", table, None, lambda: self.inner.search(tenant, table, text),
                          gate=True, drop=True)

    def history(self, tenant, table, row_id):
        return self._call(tenant, "history", table, row_id, lambda: self.inner.history(tenant, table, row_id),
                          gate=True, drop=True)

    def forget(self, tenant, table, row_id):
        return self._call(tenant, "forget", table, row_id, lambda: self.inner.forget(tenant, table, row_id))

    def delete_tenant(self, tenant):
        return self._call(tenant, "delete_tenant", None, None, lambda: self.inner.delete_tenant(tenant))

    def counts(self, tenant):
        return self._call(tenant, "counts", None, None, lambda: self.inner.counts(tenant))

    def stats(self, tenant):
        return self._call(tenant, "stats", None, None, lambda: self.inner.stats(tenant))

    def close(self):
        self.inner.close()
