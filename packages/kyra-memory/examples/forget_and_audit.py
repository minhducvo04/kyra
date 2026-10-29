"""Copy this file to try kyra-memory forget with an audit trail.

    PYTHONPATH=src python packages/kyra-memory/examples/forget_and_audit.py
"""
import json
import tempfile
from pathlib import Path

from kyra_memory.audit import AuditedBackend, TenantLabel
from kyra_memory.labels import Tier
from kyra_memory.sqlite_backend import SqliteBackend

SCOPE = "demo"
ACME = "acme"
GLOBEX = "globex"
ACME_BODY = "boiler needs spare key"
GLOBEX_BODY = "invoice schedules weekly visit"
PUBLIC = (Tier.T1, frozenset())


def _audit_actions(backend: AuditedBackend, tenant: str) -> str:
    rows = [json.loads(line) for line in backend.audit_path(tenant).read_text().splitlines()]
    return ",".join(f"{row['action']}={row['result']}" for row in rows)


def _audit_holds_body(backend: AuditedBackend, tenant: str) -> bool:
    text = backend.audit_path(tenant).read_text()
    return ACME_BODY in text or GLOBEX_BODY in text


def run(root: Path) -> None:
    labels = {ACME: TenantLabel(Tier.T1), GLOBEX: TenantLabel(Tier.T1)}
    backend = AuditedBackend(
        SqliteBackend(root / "db", SCOPE),
        root / "audit",
        labels,
        actor="forget-example",
    )
    try:
        backend.open_tenant(ACME, create=True)
        backend.open_tenant(GLOBEX, create=True)
        acme_id = backend.add(ACME, "facts", ACME_BODY, source="forget-example", label=PUBLIC)
        globex_id = backend.add(GLOBEX, "facts", GLOBEX_BODY, source="forget-example", label=PUBLIC)
        print(f"store: {root}")
        print(f"{ACME} added id={acme_id}")
        print(f"{GLOBEX} added id={globex_id}")
        print(f"{ACME} search spare key: {len(backend.search(ACME, 'facts', 'spare key'))}")
        erased = backend.forget(ACME, "facts", acme_id)
        print(f"{ACME} forget id={acme_id} rows={erased}")
        print(f"{ACME} search spare key: {len(backend.search(ACME, 'facts', 'spare key'))}")
        print(f"{GLOBEX} search weekly visit: {len(backend.search(GLOBEX, 'facts', 'weekly visit'))}")
        print(f"{ACME} audit: {_audit_actions(backend, ACME)}")
        print(f"{GLOBEX} audit: {_audit_actions(backend, GLOBEX)}")
        print(f"audit holds body text: {int(_audit_holds_body(backend, ACME) or _audit_holds_body(backend, GLOBEX))}")
    finally:
        backend.close()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="kyra-memory-forget-") as tmp:
        run(Path(tmp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
