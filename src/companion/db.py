"""Engine construction for the relational stores.

Two modes, chosen by configuration rather than code (v2 Phase 1):
- DATABASE_URL set (e.g. postgresql+psycopg://user:pw@host/db): every
  store shares one engine on that database. This is the container / AWS
  shape.
- DATABASE_URL empty (the laptop default): each store keeps its own
  SQLite file under DATA_DIR, exactly where v1 put it, so nothing about
  local use changes and existing data is read as-is.

A store can also be handed an explicit path (tests, scripts) or an
explicit engine (test fixtures that share one Postgres).
"""
from functools import cache
from pathlib import Path
from threading import Lock

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import OperationalError

from companion.schema import metadata
from companion.settings import Settings, get_settings

_create_all_lock = Lock()


class LocalOnlyError(RuntimeError):
    """The effective database does not hold the local-personal capability."""


def _local_personal_reason(url: URL, settings: Settings) -> str | None:
    if settings.tenant != "personal":
        return "the personal tenant is required"
    if not settings.owner_machine:
        return "the host is not declared as the owner's machine"
    if url.get_backend_name() != "sqlite":
        return f"backend {url.get_backend_name()} on host {url.host or '(unspecified)'} is not SQLite"
    # SQLite URI filenames can select a different path or VFS. Only ordinary
    # filenames and SQLite's standard in-memory destination qualify here.
    if url.query.get("uri", "false").lower() == "true" or (url.database or "").startswith("file:"):
        return "SQLite URI destinations are not supported by the local-personal capability"
    if url.database in (None, "", ":memory:"):
        return None
    if not Path(url.database).is_absolute():
        return "the SQLite path must be absolute"
    if not Path(url.database).resolve().is_relative_to(settings.data_dir.resolve()):
        return "the SQLite file resolves outside the configured data directory"
    return None


def is_local_personal(engine: Engine, settings: Settings | None = None) -> bool:
    """Inspect the effective destination without connecting or creating tables."""
    try:
        return _local_personal_reason(engine.url, settings or get_settings()) is None
    except Exception:
        return False


def local_personal_engine(
    default_path: Path, explicit: Path | str | None = None, *,
    engine: Engine | None = None, settings: Settings | None = None,
) -> Engine:
    """Validate the selected destination before any filesystem or database write."""
    try:
        settings = settings or get_settings()
        if engine is not None:
            url = engine.url
        else:
            destination = explicit if explicit is not None else settings.database_url or default_path
            value = str(destination)
            url = make_url(normalize_db_url(value)) if "://" in value else URL.create("sqlite", database=value)
            if (url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:")
                    and not url.database.startswith("file:") and url.query.get("uri", "false").lower() != "true"):
                # Resolve before engine construction, so later cwd changes cannot
                # change the connection destination or its capability check.
                url = url.set(database=str(Path(url.database).resolve()))
        reason = _local_personal_reason(url, settings)
    except Exception:
        # Parser/configuration exceptions can contain the original credentials.
        raise LocalOnlyError("could not validate the local-personal database destination") from None
    if reason is not None:
        raise LocalOnlyError(reason)
    if engine is None:
        if url.database not in (None, "", ":memory:"):
            sqlite_url(url.database)  # Make parent directories only after validation.
        engine = _engine_for(url.render_as_string(hide_password=False))
    create_tables(engine)
    return engine


def create_tables(engine: Engine) -> None:
    """Serialize first-use DDL, tolerating creation by another process."""
    with _create_all_lock:
        try:
            metadata.create_all(engine)
        except OperationalError as exc:
            if "already exists" not in str(exc):
                raise


def sqlite_url(path: Path | str) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path}"


def normalize_db_url(url: str) -> str:
    """Point a managed provider's connection string at the driver we install.

    Render, Heroku, Fly and the RDS console all hand out "postgres://" or
    "postgresql://". SQLAlchemy resolves both to psycopg2; requirements-web.txt
    installs psycopg[binary] (psycopg 3). Copy-pasting the string a provider gives
    you would therefore fail at startup with ModuleNotFoundError: psycopg2, which
    reads as a broken image rather than a URL that needs one word changed.

    An explicitly chosen driver is left alone - saying +psycopg2 is a decision.
    """
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


@cache
def _engine_for(url: str) -> Engine:
    if url.startswith("sqlite"):
        # The web app serves requests from a thread pool; sqlite3 objects
        # are per-thread by default, same reason v1 passed check_same_thread=False.
        engine = create_engine(url, connect_args={"check_same_thread": False})
    else:
        engine = create_engine(url, pool_pre_ping=True)
    return engine


def engine_for_store(default_path: Path, explicit: Path | str | None = None) -> Engine:
    """explicit path -> that SQLite file; else DATABASE_URL if set; else the
    store's default SQLite file. Tables are created if missing (idempotent);
    schema *changes* go through Alembic."""
    if explicit is not None and not str(explicit).startswith(("postgresql", "sqlite:")):
        url = sqlite_url(explicit)
    elif explicit is not None:
        url = str(explicit)
    else:
        url = get_settings().database_url or sqlite_url(default_path)
    engine = _engine_for(normalize_db_url(url))
    create_tables(engine)
    return engine
