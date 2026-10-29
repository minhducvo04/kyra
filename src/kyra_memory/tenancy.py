"""Tenant ids and the directory a tenant's files live in.

One database file per tenant per scope. `tenant=None` uses the flat layout (`<root>/<scope>.sqlite3`); a tenant's
files, inbox, hot files and vector index all sit under `<root>/tenants/<tenant>/`, so no query can reach another
tenant's rows. The id is checked before any path is built.
"""
import re
from pathlib import Path

TENANT_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")


class TenantError(ValueError):
    """The tenant id is not a plain name."""


def check_tenant(tenant: str | None) -> str | None:
    if tenant is not None and (not isinstance(tenant, str) or not TENANT_RE.fullmatch(tenant)):
        raise TenantError(f"invalid tenant id: {tenant!r}")
    return tenant


def tenant_root(root: Path | str, tenant: str | None = None) -> Path:
    """The directory holding one tenant's files; `root` itself when there is no tenant."""
    if check_tenant(tenant) is None:
        return Path(root)
    base = Path(root) / "tenants"
    path = base / tenant
    # a symlinked tenants folder or tenant folder would redirect one tenant's writes into another's
    if base.is_symlink() or path.is_symlink() or not path.resolve().is_relative_to(base.resolve()):
        raise TenantError(f"tenant path is a symlink or escapes the tenants folder: {tenant!r}")
    return path


def tombstone_path(root: Path | str, tenant: str) -> Path:
    """Deletion markers live outside the erased folder and contain only id and time."""
    if check_tenant(tenant) is None:
        raise TenantError("a deletion marker needs a tenant id")
    path = tenant_root(root, tenant).parent / ".deleted" / tenant
    if path.parent.is_symlink() or path.is_symlink():
        raise TenantError("a symlinked deletion marker is refused")
    return path


def check_not_deleted(root: Path | str, tenant: str | None) -> None:
    """Use the existing missing-tenant refusal for late opens and writes."""
    if tenant is not None and tombstone_path(root, tenant).exists():
        raise FileNotFoundError(f"tenant {tenant!r} was deleted; explicit creation is required")
