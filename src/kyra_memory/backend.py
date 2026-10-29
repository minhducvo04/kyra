"""The storage interface every memory backend implements. The contract suite in tests runs against each backend."""
from typing import Protocol

from kyra_memory.store import Row


class Backend(Protocol):
    """Tenant-keyed memory storage. A backend never lets a call for one tenant read or change another tenant's rows."""

    def open_tenant(self, tenant: str, *, create: bool = False) -> None:
        """Make the tenant usable. Raises FileNotFoundError for an unknown tenant unless `create`."""

    def add(self, tenant: str, table: str, body: str, *, source: str, **fields) -> int:
        """Insert a row; a row with the same key supersedes the active one. Returns the new row id."""

    def current(self, tenant: str, table: str) -> list[Row]: ...

    def search(self, tenant: str, table: str, text: str) -> list[Row]: ...

    def history(self, tenant: str, table: str, row_id: int) -> list[Row]:
        """The supersede chain containing row_id, oldest first."""

    def forget(self, tenant: str, table: str, row_id: int) -> int:
        """Erase the row and its whole supersede chain, not just mark it ended. Returns rows erased."""

    def delete_tenant(self, tenant: str) -> None:
        """Erase everything the tenant owns. Other tenants are untouched."""

    def counts(self, tenant: str) -> dict[str, int]:
        """Active rows per table."""

    def stats(self, tenant: str) -> dict[str, float]:
        """Size and load figures: `bytes`, `queries`, `congestion_events`."""
