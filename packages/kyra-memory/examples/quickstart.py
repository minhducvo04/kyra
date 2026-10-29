"""Copy this file to try kyra-memory: two synthetic tenants, search, isolation, forget.

    PYTHONPATH=src python packages/kyra-memory/examples/quickstart.py
"""
import tempfile
from pathlib import Path

from kyra_memory.sqlite_backend import SqliteBackend

SCOPE = "demo"
ACME = "acme"
GLOBEX = "globex"
ACME_BODY = "boiler needs spare key"
GLOBEX_BODY = "invoice schedules weekly visit"


def run(root: Path) -> None:
    backend = SqliteBackend(root, SCOPE)
    try:
        backend.open_tenant(ACME, create=True)
        backend.open_tenant(GLOBEX, create=True)
        acme_id = backend.add(ACME, "facts", ACME_BODY, source="quickstart")
        globex_id = backend.add(GLOBEX, "facts", GLOBEX_BODY, source="quickstart")
        print(f"store: {root}")
        print(f"{ACME} added id={acme_id}")
        print(f"{GLOBEX} added id={globex_id}")
        print(f"{ACME} search spare key: {len(backend.search(ACME, 'facts', 'spare key'))}")
        print(f"{GLOBEX} search spare key: {len(backend.search(GLOBEX, 'facts', 'spare key'))}")
        print(f"{GLOBEX} search weekly visit: {len(backend.search(GLOBEX, 'facts', 'weekly visit'))}")
        print(f"{ACME} search weekly visit: {len(backend.search(ACME, 'facts', 'weekly visit'))}")
        erased = backend.forget(ACME, "facts", acme_id)
        print(f"{ACME} forget id={acme_id} rows={erased}")
        print(f"{ACME} search spare key: {len(backend.search(ACME, 'facts', 'spare key'))}")
        print(f"{GLOBEX} search weekly visit: {len(backend.search(GLOBEX, 'facts', 'weekly visit'))}")
    finally:
        backend.close()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="kyra-memory-quickstart-") as tmp:
        run(Path(tmp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
