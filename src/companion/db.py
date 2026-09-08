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

from sqlalchemy import Engine, create_engine

from companion.schema import metadata
from companion.settings import get_settings


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
    metadata.create_all(engine)
    return engine
