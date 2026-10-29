"""Postgres backend: one shared set of tables, the tenant is a column.

Isolation is Postgres row-level security, not application filtering. Every table has `tenant_id NOT NULL`, an index
led by `tenant_id` and a policy on `NULLIF(current_setting('app.tenant', true), '')`; each call runs in one transaction
that sets `SET LOCAL ROLE kyra_memory_app`, its search_path and `app.tenant` locally, so a session that sets nothing
reads zero rows, a hand-written `WHERE tenant_id = 'other'` still sees nothing, and a transaction-mode pooler cannot
carry a role or tenant from one transaction into the next.

Two DSNs, each passed as the NAME of an environment variable so the secret is never an argument, message or repr:
`install(admin_env, schema)` runs the schema as the installer (it owns the tables); `PgBackend(app_env, schema=...)`
logs in as `kyra_memory_login` (LOGIN, NOINHERIT, member of `kyra_memory_app` only). The backend refuses to run when
the logged-in role or the app role is, or is a member of, a SUPERUSER or BYPASSRLS role, owns (or is a member of the
owner of) the schema or any object in it, or can reach the tables without the per-transaction SET ROLE.

Not in this tier (the SQLite tier has them): per-tenant quotas, TTL expiry, inbox merging, byte-level erasure.
Row ids come from one identity counter, so they are unique per table, not per tenant.
"""
import json
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path

from kyra_memory.labels import UNKNOWN, combine
from kyra_memory.store import (
    TABLES,
    Row,
    _check_time,
    _label,
    _stored_label,
    fault_signature,
    now_iso,
    require_scope_allowed,
    strip_ids,
)
from kyra_memory.tenancy import check_tenant

APP_ROLE = "kyra_memory_app"
LOGIN_ROLE = "kyra_memory_login"
SCHEMA_RE = re.compile(r"[a-z][a-z0-9_]{0,40}")
ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_EXTRA_COLS = {"facts": ("wins", "losses", "confidence", "valid_from", "valid_to"), "faults": ("signature", "seen")}

# One row: can the logged-in role (session_user) or the role it switched to (current_user) skip the policies?
_RISKY_SQL = """
SELECT EXISTS (SELECT 1 FROM pg_roles r WHERE (r.rolsuper OR r.rolbypassrls)
               AND (pg_has_role(session_user, r.oid, 'MEMBER') OR pg_has_role(current_user, r.oid, 'MEMBER')))
         AS bypass,
       EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspname = %(schema)s
               AND (pg_has_role(session_user, n.nspowner, 'MEMBER') OR pg_has_role(current_user, n.nspowner, 'MEMBER')))
    OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %(schema)s
               AND (pg_has_role(session_user, c.relowner, 'MEMBER') OR pg_has_role(current_user, c.relowner, 'MEMBER')))
         AS owner,
       has_table_privilege(session_user, 'tenants', 'SELECT') AS direct
"""


def _check_names(env_name: str, schema: str, allow_restricted: bool) -> None:
    if not isinstance(env_name, str) or not ENV_NAME_RE.fullmatch(env_name):
        raise ValueError("pass the name of the environment variable that holds the DSN, not the DSN")
    if not isinstance(schema, str) or not SCHEMA_RE.fullmatch(schema):
        raise ValueError("invalid schema name")
    require_scope_allowed(schema, allow_restricted)  # the schema is the scope: "health" needs allow_restricted


def _connect(env_name: str, **kwargs):
    """Connect with the DSN in os.environ[env_name]; no error, traceback or chained exception carries the DSN."""
    import psycopg

    if not os.environ.get(env_name):
        raise ValueError(f"{env_name} is not set")
    try:
        return psycopg.connect(os.environ[env_name], **kwargs)
    except psycopg.Error:
        pass  # raised below, outside the except block: psycopg's error (it can quote the DSN) is not even __context__
    raise ConnectionError(f"could not connect to Postgres with the DSN in {env_name}")


def install(admin_env: str, schema: str = "public", *, allow_restricted: bool = False) -> None:
    """Create or update the schema as the installer (the DSN in os.environ[admin_env]); idempotent."""
    from psycopg import sql

    _check_names(admin_env, schema, allow_restricted)
    with _connect(admin_env, autocommit=True) as conn, conn.transaction():
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
        conn.execute(Path(__file__).with_name("pg_schema.sql").read_text(encoding="utf-8"))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(APP_ROLE)))


class PgBackend:
    def __init__(self, app_env: str, *, schema: str = "public", allow_restricted: bool = False, clock=None):
        from psycopg import sql
        from psycopg.rows import dict_row

        _check_names(app_env, schema, allow_restricted)
        self.schema = schema
        self._role = APP_ROLE
        self._enter = sql.SQL("SET LOCAL ROLE {}; SET LOCAL search_path TO {}").format(sql.Identifier(self._role),
                                                                                     sql.Identifier(schema))
        self._clock = clock or now_iso
        self._lock = threading.RLock()  # one shared connection, like the SQLite store's lock
        self._opened: set[str] = set()
        self.queries = 0
        self._conn = _connect(app_env, autocommit=True, row_factory=dict_row)
        try:
            with self.session(None) as db:
                risky = db.execute(_RISKY_SQL, {"schema": schema}).fetchone()
            if any(risky.values()):
                raise PermissionError("the Postgres login role or the app role can bypass row-level security "
                                      f"({', '.join(k for k, v in risky.items() if v)}); refusing to run")
        except BaseException:
            self._conn.close()
            raise

    def __repr__(self) -> str:
        return "PgBackend(<connected>)" if not self._conn.closed else "PgBackend(<closed>)"

    @contextmanager
    def session(self, tenant: str | None):
        """One transaction as the app role with `app.tenant` set for its duration (None resets it)."""
        check_tenant(tenant)
        with self._lock, self._conn.transaction():
            self._conn.execute(self._enter)  # per transaction: a RESET ROLE or a pooler cannot carry over
            if tenant is None:
                self._conn.execute("RESET app.tenant")  # a session-level SET from raw SQL must not reach this one
                user = self._conn.execute("SELECT current_user AS u").fetchone()["u"]
            else:
                user = self._conn.execute("SELECT current_user AS u, set_config('app.tenant', %s, true)",
                                          (tenant,)).fetchone()["u"]
            if user != self._role:
                raise PermissionError("the session is not running as the app role; refusing to run")
            yield self._conn

    @staticmethod
    def _named(tenant: str) -> str:
        if check_tenant(tenant) is None:
            raise ValueError("a backend call needs a tenant id")
        return tenant

    @staticmethod
    def _table(table: str) -> str:
        if table not in TABLES:
            raise ValueError(f"unknown table {table!r}")
        return table

    def _check_open(self, tenant: str) -> None:
        if self._named(tenant) not in self._opened:
            raise KeyError(f"tenant {tenant!r} is not open")

    def open_tenant(self, tenant: str, *, create: bool = False) -> None:
        with self.session(self._named(tenant)) as db:
            if db.execute("SELECT 1 FROM tenants WHERE tenant_id = %s", (tenant,)).fetchone() is None:
                if not create:
                    raise FileNotFoundError(f"unknown tenant {tenant!r}")
                db.execute("INSERT INTO tenants (tenant_id) VALUES (%s)", (tenant,))
        self._opened.add(tenant)

    # ---- writers ---------------------------------------------------------

    def add(self, tenant: str, table: str, body: str, *, source: str, topic: str = "", key: str | None = None,
            event_time: str | None = None, label=UNKNOWN, supersedes: int | None = None,
            confidence: float | None = None, signature: str | None = None) -> int:
        """Same rules as MemoryStore.add: a row with the same key (or fault signature) supersedes the active one."""
        self._check_open(tenant)
        self._table(table)
        body = (strip_ids(body) if table == "recipes" else body).strip()
        if not body or not source or not source.strip():
            raise ValueError("body and source are required")
        if table == "recipes" and not topic.strip():
            raise ValueError("a recipe needs its task kind as topic")
        label = _label(label)
        knowledge = self._clock()
        event = _check_time(event_time) if event_time else knowledge
        cols = {"body": body, "topic": topic.strip(), "key": key, "source": source.strip(), "event_time": event,
                "knowledge_time": knowledge}
        if table == "facts":
            cols.update(valid_from=event, confidence=0.5 if confidence is None else float(confidence))
        if table == "faults":
            cols["signature"] = fault_signature(signature or body)
        with self.session(tenant) as db:
            old = None
            lock = "FOR UPDATE"
            if supersedes is not None:
                old = db.execute(f"SELECT * FROM {table} WHERE id = %s {lock}", (supersedes,)).fetchone()
                if old is None or old["status"] != "active":
                    raise ValueError(f"{table} row {supersedes} is not an active row to supersede")
            elif table == "faults":
                old = db.execute(f"SELECT * FROM faults WHERE status = 'active' AND signature = %s {lock}",
                                 (cols["signature"],)).fetchone()
                if old is not None and old["body"] == body:
                    old_label = _stored_label(old["tier"], old["classes"])
                    label = combine([label, old_label])
                    if label == old_label:
                        oid = old["id"]
                        db.execute("UPDATE faults SET seen = seen + 1 WHERE id = %s", (oid,))
                        return oid
                    cols["seen"] = old["seen"] + 1
            elif key is not None:
                old = db.execute(f"SELECT * FROM {table} WHERE status = 'active' AND key = %s {lock}", (key,)).fetchone()
            if old is not None:
                label = combine([label, _stored_label(old["tier"], old["classes"])])
                cols["supersedes"] = old["id"]
                if cols["key"] is None:
                    cols["key"] = old["key"]
            cols["tier"], cols["classes"] = int(label[0]), json.dumps(sorted(c.value for c in label[1]))
            names = ", ".join(cols)
            new_id = db.execute(
                f"INSERT INTO {table} (tenant_id, {names}) VALUES (%s, {', '.join(['%s'] * len(cols))}) RETURNING id",
                (tenant, *cols.values())).fetchone()["id"]
            if old is not None:
                oid = cols["supersedes"]
                db.execute(f"UPDATE {table} SET status = 'superseded', superseded_by = %s, ended_at = %s WHERE id = %s",
                           (new_id, knowledge, oid))
                if table == "facts":
                    db.execute("UPDATE facts SET valid_to = %s WHERE id = %s", (event, oid))
            return new_id

    # ---- readers ---------------------------------------------------------

    def _rows(self, tenant: str, table: str, where: str = "true", args: tuple = ()) -> list[Row]:
        self._check_open(tenant)
        self._table(table)
        with self.session(tenant) as db:
            self.queries += 1
            cur = db.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY id", args)
            return [self._row(table, r) for r in cur.fetchall()]

    @staticmethod
    def _row(table: str, r: dict) -> Row:
        return Row(table, r["id"], r["body"], r["topic"], r["key"], r["source"], r["event_time"], r["knowledge_time"],
                   _stored_label(r["tier"], r["classes"]), r["status"], r["supersedes"], r["superseded_by"],
                   r["ended_at"], {c: r[c] for c in _EXTRA_COLS.get(table, ())})

    def current(self, tenant: str, table: str) -> list[Row]:
        return self._rows(tenant, table, "status = 'active'")

    def search(self, tenant: str, table: str, text: str) -> list[Row]:
        from psycopg.errors import UndefinedColumn

        from kyra_memory.fts import like_where, postgres_clause
        like = f"%{text}%"
        fallback = like_where("ILIKE", "%s")
        where, args = postgres_clause(text)
        if where is not None:
            try:
                return self._rows(tenant, table, where, args)
            except UndefinedColumn:
                pass
        return self._rows(tenant, table, fallback, (like,) * 3)

    def _get(self, tenant: str, table: str, row_id: int) -> Row | None:
        rows = self._rows(tenant, table, "id = %s", (row_id,))
        return rows[0] if rows else None

    def history(self, tenant: str, table: str, row_id: int) -> list[Row]:
        row = self._get(tenant, table, row_id)
        if row is None:
            return []
        while row.supersedes is not None:
            row = self._get(tenant, table, row.supersedes)
        chain = [row]
        while chain[-1].superseded_by is not None:
            chain.append(self._get(tenant, table, chain[-1].superseded_by))
        return chain

    # ---- erasure ---------------------------------------------------------

    def forget(self, tenant: str, table: str, row_id: int) -> int:
        """Delete the whole supersede chain. Dead tuples stay in the heap until autovacuum; this does not zero them."""
        ids = [r.id for r in self.history(tenant, table, row_id)]
        if not ids:
            return 0
        with self.session(tenant) as db:
            return db.execute(f"DELETE FROM {table} WHERE id = ANY(%s)", (ids,)).rowcount

    def delete_tenant(self, tenant: str) -> None:
        """One DELETE on the tenant row; the foreign keys cascade to every table."""
        with self.session(self._named(tenant)) as db:
            db.execute("DELETE FROM tenants WHERE tenant_id = %s", (tenant,))
        self._opened.discard(tenant)

    # ---- reporting -------------------------------------------------------

    def counts(self, tenant: str) -> dict[str, int]:
        self._check_open(tenant)
        with self.session(tenant) as db:
            return {t: db.execute(f"SELECT count(*) AS n FROM {t} WHERE status = 'active'").fetchone()["n"]
                    for t in TABLES}

    def stats(self, tenant: str) -> dict[str, float]:
        self._check_open(tenant)
        with self.session(tenant) as db:
            size = sum(db.execute(f"SELECT coalesce(sum(pg_column_size(t.*)), 0) AS n FROM {t} t").fetchone()["n"]
                       for t in TABLES)
        return {"bytes": float(size), "queries": self.queries, "congestion_events": 0}

    def close(self) -> None:
        self._opened.clear()
        if not self._conn.closed:
            self._conn.close()
