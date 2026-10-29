"""Copy this file to try kyra-memory: 50 synthetic facts, five searches.

    PYTHONPATH=src python packages/kyra-memory/examples/search_many.py
"""
import tempfile
from pathlib import Path

from kyra_memory.sqlite_backend import SqliteBackend

SCOPE = "demo"
TENANT = "acme"
SUBJECTS = (
    "boiler", "invoice", "ladder", "van", "quote",
    "permit", "gutter", "thermostat", "contract", "roster",
)
OBJECTS = ("spare key", "weekly visit", "site photo", "signed form", "parts order")
FACTS = [f"{subject} needs {obj}" for subject in SUBJECTS for obj in OBJECTS]
QUERIES = ("spare key", "weekly visit", "boiler", "invoice", "missing widget")


def run(root: Path) -> None:
    backend = SqliteBackend(root, SCOPE)
    try:
        backend.open_tenant(TENANT, create=True)
        for body in FACTS:
            backend.add(TENANT, "facts", body, source="search-many")
        print(f"store: {root}")
        print(f"loaded: {backend.counts(TENANT)['facts']}")
        for query in QUERIES:
            rows = backend.search(TENANT, "facts", query)
            if rows:
                print(f"search {query}: {len(rows)} {rows[0].body}")
            else:
                print(f"search {query}: 0")
    finally:
        backend.close()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="kyra-memory-search-many-") as tmp:
        run(Path(tmp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
