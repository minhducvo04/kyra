"""One contract for every storage backend. A second backend is one more entry in BACKENDS.

The Postgres entry needs KYRA_MEMORY_PG_DSN (the app login, kyra_memory_login) and KYRA_MEMORY_PG_ADMIN_DSN (the
installer) on a local, synthetic-data database; without them that entry is skipped. Each Postgres test installs its
own schema and drops it afterwards; scratch roles are named kyra_test_* and dropped by their fixture.
"""
import os
import uuid
from pathlib import Path

import pytest

from kyra_memory.labels import Tier
from kyra_memory.sqlite_backend import SqliteBackend
from kyra_memory.tenancy import TenantError

PUBLIC = (Tier.T1, frozenset())
CANARY = "zebra-quartz-ledger"

PG_DSN_VAR = "KYRA_MEMORY_PG_DSN"
PG_ADMIN_VAR = "KYRA_MEMORY_PG_ADMIN_DSN"
TABLES = ("rules", "pinned", "facts", "faults", "recipes")


def _pg(tmp_path):
    if not (os.environ.get(PG_DSN_VAR) and os.environ.get(PG_ADMIN_VAR)):
        pytest.skip(f"{PG_DSN_VAR} and {PG_ADMIN_VAR} are not set; point them at the local synthetic Postgres")
    pytest.importorskip("psycopg")
    from kyra_memory.pg_backend import PgBackend, install
    schema = "t_" + uuid.uuid4().hex[:12]
    install(PG_ADMIN_VAR, schema)
    try:
        return PgBackend(PG_DSN_VAR, schema=schema)
    except BaseException:
        _drop_schema(schema)
        raise


def _admin():
    import psycopg
    return psycopg.connect(os.environ[PG_ADMIN_VAR], autocommit=True)


def _drop_schema(schema):
    with _admin() as admin:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


BACKENDS = {"sqlite": lambda tmp_path: SqliteBackend(tmp_path, "svc"), "pg": _pg}


def _close(b):
    schema = getattr(b, "schema", None)
    b.close()
    if schema:
        _drop_schema(schema)


@pytest.fixture(params=sorted(BACKENDS))
def backend(request, tmp_path):
    b = BACKENDS[request.param](tmp_path)
    yield b
    _close(b)


@pytest.fixture
def sqlite_backend(tmp_path):
    b = SqliteBackend(tmp_path, "svc")
    yield b
    b.close()


@pytest.fixture
def two(backend):
    backend.open_tenant("acme", create=True)
    backend.open_tenant("globex", create=True)
    backend.add("acme", "rules", f"Acme rule {CANARY}", source="owner", label=PUBLIC)
    backend.add("acme", "facts", f"Acme fact {CANARY}", source="agent", key="k", label=PUBLIC)
    backend.add("globex", "facts", "Globex fact about invoices", source="agent", label=PUBLIC)
    return backend


def test_unknown_tenant_needs_create(backend):
    with pytest.raises(FileNotFoundError):
        backend.open_tenant("acme")


def test_add_current_search_counts(two):
    assert [r.body for r in two.current("globex", "facts")] == ["Globex fact about invoices"]
    assert [r.body for r in two.search("acme", "facts", CANARY)] == [f"Acme fact {CANARY}"]
    assert two.counts("acme")["facts"] == 1
    assert two.stats("acme")["bytes"] > 0


def test_history_follows_a_correction(two):
    new = two.add("acme", "facts", "Acme fact, corrected", source="agent", key="k", label=PUBLIC)
    assert [r.body for r in two.history("acme", "facts", new)] == [f"Acme fact {CANARY}", "Acme fact, corrected"]
    assert len(two.current("acme", "facts")) == 1


def test_one_tenant_never_sees_another(two):
    assert two.search("globex", "facts", CANARY) == []
    assert two.search("globex", "rules", CANARY) == []
    assert not any(CANARY in r.body for t in ("rules", "facts") for r in two.current("globex", t))
    acme_id = two.add("acme", "facts", "Acme second fact", source="agent", label=PUBLIC)  # id 2; globex has only id 1
    assert two.history("globex", "facts", acme_id) == []
    assert two.forget("globex", "facts", acme_id) == 0
    assert two.counts("acme")["facts"] == 2


def test_forget_erases_the_whole_chain(two):
    new = two.add("acme", "facts", "Acme fact, corrected", source="agent", key="k", label=PUBLIC)
    assert two.forget("acme", "facts", new) == 2
    assert two.counts("acme")["facts"] == 0
    assert two.search("acme", "facts", "Acme fact") == []
    assert two.counts("globex")["facts"] == 1


def test_delete_tenant_leaves_the_others(two, tmp_path):
    two.delete_tenant("acme")
    assert not (tmp_path / "tenants" / "acme").exists()
    assert two.counts("globex")["facts"] == 1
    assert two.search("globex", "facts", "invoices")
    with pytest.raises(FileNotFoundError):
        two.open_tenant("acme")


@pytest.mark.parametrize("bad", ["../acme", "Acme", ""])
def test_bad_tenant_id_is_refused(backend, bad):
    with pytest.raises(TenantError):
        backend.open_tenant(bad, create=True)


def test_forget_refuses_an_injected_table_name(two):
    with pytest.raises(ValueError):
        two.forget("acme", "facts WHERE 1=1; --", 1)
    assert two.counts("acme")["facts"] == 1


def test_forget_leaves_no_marker_in_the_files(tmp_path):
    marker = "zqxv-purge-marker-7731"
    b = SqliteBackend(tmp_path, "svc")
    b.open_tenant("acme", create=True)
    rid = b.add("acme", "facts", f"secret {marker}", source="agent", label=PUBLIC)
    b.add("acme", "facts", "other", source="agent", label=PUBLIC)
    files = list((tmp_path / "tenants" / "acme").glob("*.sqlite3*"))
    assert any(marker.encode() in f.read_bytes() for f in files)  # the marker really reached disk
    assert b.forget("acme", "facts", rid) == 1
    for f in (tmp_path / "tenants" / "acme").glob("*.sqlite3*"):
        assert marker.encode() not in f.read_bytes(), f.name
    b.close()


def test_delete_tenant_raises_when_the_folder_stays(sqlite_backend, tmp_path):
    two = sqlite_backend
    two.open_tenant("acme", create=True)
    folder = tmp_path / "tenants" / "acme"
    os.chmod(folder, 0o500)  # files inside cannot be unlinked
    try:
        with pytest.raises(OSError):
            two.delete_tenant("acme")
    finally:
        os.chmod(folder, 0o700)
    assert folder.exists()


def test_delete_tenant_refuses_a_symlinked_tenants_dir(sqlite_backend, tmp_path):
    backend = sqlite_backend
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    (outside / "evil").mkdir(parents=True)
    (tmp_path / "tenants").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        backend.delete_tenant("evil")
    assert (outside / "evil").exists()


def test_forget_purges_only_its_own_tables_proposals(tmp_path):
    b = SqliteBackend(tmp_path, "svc")
    b.open_tenant("acme", create=True)
    st = b._store("acme")
    with st.transaction() as db:
        st._ensure_proposals(db)
        for tbl, body in (("facts", "fact text zq1"), ("recipes", "recipe text zq2")):
            db.execute("INSERT INTO proposals (tbl, body, source, tier, classes, refs, quote, confidence, origin, team, at,"
                       " state, fact_id) VALUES (?, ?, 'agent', 1, '[]', '[]', '', 1.0, 'o', 't', 'x', 'accepted', 1)",
                       (tbl, body))
    b.add("acme", "facts", "fact text zq1", source="agent", label=PUBLIC)
    b.add("acme", "recipes", "recipe text zq2", source="agent", topic="build", label=PUBLIC)
    assert b.forget("acme", "facts", 1) == 1
    assert [p["body"] for p in st.proposals("accepted")] == ["recipe text zq2"]
    assert b.forget("acme", "recipes", 1) == 1
    assert st.proposals("accepted") == []
    b.close()


def test_forget_raises_when_a_reader_blocks_the_wal_purge(tmp_path):
    import sqlite3
    b = SqliteBackend(tmp_path, "svc")
    b.open_tenant("acme", create=True)
    rid = b.add("acme", "facts", "reader marker", source="agent", label=PUBLIC)
    b._store("acme")._db.execute("PRAGMA busy_timeout=0")
    reader = sqlite3.connect(b._store("acme").path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM facts").fetchone()
    try:
        with pytest.raises(RuntimeError):
            b.forget("acme", "facts", rid)
    finally:
        reader.close()
    b.close()


def test_delete_tenant_refuses_a_symlinked_tenant_folder(sqlite_backend, tmp_path):
    backend = sqlite_backend
    outside = tmp_path.parent / (tmp_path.name + "-out2")
    outside.mkdir()
    (tmp_path / "tenants").mkdir()
    (tmp_path / "tenants" / "evil").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        backend.delete_tenant("evil")
    assert outside.exists()


def test_retry_after_the_reader_closes_leaves_no_marker_in_the_wal(tmp_path):
    import sqlite3
    marker = "zqxv-retry-marker-5521"
    b = SqliteBackend(tmp_path, "svc")
    b.open_tenant("acme", create=True)
    rid = b.add("acme", "facts", f"secret {marker}", source="agent", label=PUBLIC)
    store = b._store("acme")
    store._db.execute("PRAGMA busy_timeout=0")
    reader = sqlite3.connect(store.path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM facts").fetchone()
    with pytest.raises(RuntimeError):
        b.forget("acme", "facts", rid)
    reader.close()
    b.forget("acme", "facts", rid)  # retry: no rows left, but the WAL must still be truncated
    for f in (tmp_path / "tenants" / "acme").glob("*.sqlite3*"):
        assert marker.encode() not in f.read_bytes(), f.name
    b.close()


# ---- Postgres tier: row-level security does the isolation ----------------------------------------------------

@pytest.fixture
def pg(tmp_path):
    b = _pg(tmp_path)
    b.open_tenant("acme", create=True)
    b.open_tenant("globex", create=True)
    b.add("acme", "facts", f"Acme fact {CANARY}", source="agent", label=PUBLIC)
    b.add("globex", "facts", "Globex fact about invoices", source="agent", label=PUBLIC)
    yield b
    _close(b)


def test_a_session_without_a_tenant_reads_zero_rows(pg):
    with pg.session(None) as db:
        assert [db.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in TABLES] == [0] * 5
        assert db.execute("SELECT count(*) AS n FROM tenants").fetchone()["n"] == 0


def test_a_tenant_session_cannot_reach_another_tenant_even_with_its_own_where(pg):
    with pg.session("globex") as db:
        assert db.execute("SELECT count(*) AS n FROM facts WHERE tenant_id = 'acme'").fetchone()["n"] == 0
        assert db.execute("SELECT count(*) AS n FROM facts").fetchone()["n"] == 1
    import psycopg
    with pytest.raises(psycopg.errors.InsufficientPrivilege), pg.session("globex") as db:
        db.execute("INSERT INTO rules (tenant_id, body, source, event_time, knowledge_time, tier, classes)"
                   " VALUES ('acme', 'planted', 's', 'x', 'x', 1, '[]')")


def test_every_tenant_table_is_locked_down(pg):
    with pg.session("acme") as db:
        assert db.execute("SELECT rolsuper OR rolbypassrls AS risky FROM pg_roles WHERE rolname = current_user"
                          ).fetchone()["risky"] is False
        for t in (*TABLES, "tenants"):
            col = db.execute("SELECT is_nullable FROM information_schema.columns WHERE table_schema = current_schema()"
                             " AND table_name = %s AND column_name = 'tenant_id'", (t,)).fetchone()
            assert col["is_nullable"] == "NO", t
            flags = db.execute("SELECT relrowsecurity AS on_, relforcerowsecurity AS forced FROM pg_class"
                               " WHERE oid = to_regclass(%s)", (t,)).fetchone()
            assert (flags["on_"], flags["forced"]) == (True, True), t
            policy = db.execute("SELECT qual FROM pg_policies WHERE schemaname = current_schema() AND tablename = %s",
                                (t,)).fetchone()
            assert "NULLIF(current_setting('app.tenant'::text, true), ''::text)" in policy["qual"], t
        for t in TABLES:  # the named index, not the (tenant_id, id) primary key
            index = db.execute("SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() AND indexname = %s",
                               (f"{t}_tenant_status_key",)).fetchone()
            assert index and index["indexdef"].endswith("(tenant_id, status, key)"), t


def test_tenants_check_matches_tenancy_and_refuses_the_empty_id(pg):
    import psycopg

    from kyra_memory.tenancy import TENANT_RE
    assert f"'^{TENANT_RE.pattern}$'" in (Path(__file__).parents[2] / "src/kyra_memory/pg_schema.sql").read_text()
    with pytest.raises(psycopg.errors.CheckViolation), _admin() as admin:  # the owner skips RLS; the CHECK still holds
        admin.execute(f'INSERT INTO "{pg.schema}".tenants (tenant_id) VALUES (\'\')')


def test_raw_sql_cannot_carry_a_role_or_tenant_into_the_next_call(pg):
    with pg.session("globex") as db:
        db.execute("SET app.tenant = 'acme'")  # session level, outlives the transaction
        db.execute("RESET ROLE")  # back to the NOINHERIT login role
    with pg.session(None) as db:
        assert db.execute("SELECT count(*) AS n FROM facts").fetchone()["n"] == 0
    assert [r.body for r in pg.current("globex", "facts")] == ["Globex fact about invoices"]
    # between calls the connection holds no role or tenant a pooler could hand to the next client
    row = pg._conn.execute("SELECT current_user AS u, current_setting('app.tenant', true) AS t").fetchone()
    assert (row["u"], row["t"] or "") == ("kyra_memory_login", "")


def test_a_restricted_scope_needs_allow_restricted():
    pytest.importorskip("psycopg")
    from kyra_memory.pg_backend import PgBackend, install
    from kyra_memory.store import ScopeError
    with pytest.raises(ScopeError):
        install(PG_ADMIN_VAR, "health")
    with pytest.raises(ScopeError):
        PgBackend(PG_DSN_VAR, schema="health")


# ---- the bypass refusal: scratch roles, the shared kyra_memory_app is never altered ----------------------------

@pytest.fixture
def scratch(pg, monkeypatch):
    """A scratch login (NOINHERIT) in a scratch app role with the table grants; the backend logs in as it.

    Roles are cluster-wide, so each test gets its own names (parallel workers never share one)."""
    from psycopg.conninfo import make_conninfo

    from kyra_memory import pg_backend
    suffix = uuid.uuid4().hex[:8]
    roles = {"login": f"kyra_test_login_{suffix}", "app": f"kyra_test_app_{suffix}",
             "helper": f"kyra_test_helper_{suffix}"}
    LOGIN, APP, HELPER = roles["login"], roles["app"], roles["helper"]
    admin = _admin()

    def reset():
        for r in (LOGIN, APP, HELPER):
            if admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (r,)).fetchone():
                admin.execute(f"REASSIGN OWNED BY {r} TO CURRENT_USER")
                admin.execute(f"DROP OWNED BY {r}")
                admin.execute(f"DROP ROLE {r}")

    try:
        admin.execute(f"CREATE ROLE {APP} NOLOGIN NOSUPERUSER NOBYPASSRLS")
        admin.execute(f"CREATE ROLE {HELPER} NOLOGIN NOSUPERUSER NOBYPASSRLS")
        admin.execute(f"CREATE ROLE {LOGIN} LOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS IN ROLE {APP}")
        admin.execute(f'GRANT USAGE ON SCHEMA "{pg.schema}" TO {APP}')
        admin.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{pg.schema}" TO {APP}')
        monkeypatch.setenv("KYRA_TEST_SCRATCH_DSN", make_conninfo(os.environ[PG_DSN_VAR], user=LOGIN))
        monkeypatch.setattr(pg_backend, "APP_ROLE", APP)
        yield admin, pg.schema, roles
    finally:
        try:
            reset()
        finally:
            admin.close()


def _open_scratch(schema):
    from kyra_memory.pg_backend import PgBackend
    PgBackend("KYRA_TEST_SCRATCH_DSN", schema=schema).close()


def test_a_plain_scratch_login_runs(scratch):
    _open_scratch(scratch[1])  # the control: the same roles with nothing risky are accepted


@pytest.mark.parametrize("setup", [
    pytest.param(["ALTER ROLE {login} BYPASSRLS"], id="login-bypassrls"),
    pytest.param(['ALTER TABLE "{schema}".facts OWNER TO {login}'], id="login-owns-a-table"),
    pytest.param(["ALTER ROLE {helper} BYPASSRLS", "GRANT {helper} TO {app}"], id="app-member-of-bypassrls"),
    pytest.param(["ALTER ROLE {helper} SUPERUSER", "GRANT {helper} TO {login}"], id="login-member-of-superuser"),
    pytest.param(["GRANT {app} TO {login} WITH INHERIT TRUE"], id="login-reaches-tables-without-set-role"),
])
def test_refuses_a_role_that_can_bypass_the_policies(scratch, setup):
    admin, schema, roles = scratch
    for s in setup:
        admin.execute(s.format(schema=schema, **roles))
    with pytest.raises(PermissionError):
        _open_scratch(schema)


def test_refuses_the_admin_dsn(pg):
    from kyra_memory.pg_backend import PgBackend
    with _admin() as admin:
        if not admin.execute("SELECT rolsuper FROM pg_roles WHERE rolname = session_user").fetchone()[0]:
            pytest.skip("the admin DSN is not a superuser")
    with pytest.raises(PermissionError):
        PgBackend(PG_ADMIN_VAR, schema=pg.schema)


# ---- the DSN never appears in an error, a traceback or a repr ---------------------------------------------------

PLANTED = "PLANTEDpw" + "9q"


@pytest.mark.parametrize("dsn", [f"postgresql://kyra:{PLANTED}%zz@127.0.0.1:1/x",  # malformed: psycopg echoes it
                                 f"postgresql://kyra:{PLANTED}@127.0.0.1:1/x"])  # refused connect
def test_a_bad_dsn_never_shows_its_password(dsn, monkeypatch):
    import traceback

    pytest.importorskip("psycopg")
    from kyra_memory.pg_backend import PgBackend
    monkeypatch.setenv("KYRA_TEST_BAD_DSN", dsn)
    with pytest.raises(ConnectionError) as err:
        PgBackend("KYRA_TEST_BAD_DSN")
    assert err.value.__context__ is None and err.value.__cause__ is None
    shown = "".join(traceback.format_exception(err.value)) + repr(err.value)
    assert PLANTED not in shown and "KYRA_TEST_BAD_DSN" in shown
    with pytest.raises(ValueError) as err:
        PgBackend(dsn)  # a DSN passed where the variable name belongs is refused without echoing it
    assert PLANTED not in "".join(traceback.format_exception(err.value))


def test_a_connected_backend_repr_hides_the_password(pg, monkeypatch):
    from psycopg.conninfo import make_conninfo

    from kyra_memory.pg_backend import PgBackend
    monkeypatch.setenv("KYRA_TEST_PW_DSN", make_conninfo(os.environ[PG_DSN_VAR], password=PLANTED))
    b = PgBackend("KYRA_TEST_PW_DSN", schema=pg.schema)  # trust auth ignores the password
    try:
        assert PLANTED not in repr(b) + str(b)
    finally:
        b.close()
