-- Postgres tier for kyra_memory (MS4). One shared set of tables; the tenant is a column and Postgres row-level
-- security keeps each session to its own tenant. Idempotent: safe to run on every start. Synthetic/local use only.
SELECT pg_advisory_xact_lock(727401);

-- kyra_memory_app holds the table grants; kyra_memory_login is what the app DSN logs in as. NOINHERIT: the login role
-- reaches the tables only through the backend's per-transaction SET LOCAL ROLE kyra_memory_app. Neither role owns
-- anything here or can bypass RLS; the installer (the admin DSN) owns the tables. Set a password on the login role
-- yourself if your pg_hba.conf needs one (this file never holds a secret).
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kyra_memory_app') THEN
    CREATE ROLE kyra_memory_app NOLOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kyra_memory_login') THEN
    CREATE ROLE kyra_memory_login LOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
END $$;
GRANT kyra_memory_app TO kyra_memory_login;

-- The tenant id format is tenancy.TENANT_RE; '' (an unset app.tenant after a SET LOCAL) can never be a tenant.
CREATE TABLE IF NOT EXISTS tenants (tenant_id TEXT PRIMARY KEY);
ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_tenant_id_format;
ALTER TABLE tenants ADD CONSTRAINT tenants_tenant_id_format CHECK (tenant_id ~ '^[a-z][a-z0-9_-]{0,63}$');

DO $$
DECLARE
  t text;
  extra text;
BEGIN
  FOREACH t IN ARRAY ARRAY['rules', 'pinned', 'facts', 'faults', 'recipes'] LOOP
    extra := CASE t
      WHEN 'facts' THEN ', wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0,'
                        ' confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5, valid_from TEXT, valid_to TEXT'
      WHEN 'faults' THEN ', signature TEXT NOT NULL, seen INTEGER NOT NULL DEFAULT 1'
      ELSE '' END;
    EXECUTE format('CREATE TABLE IF NOT EXISTS %I ('
      ' tenant_id TEXT NOT NULL REFERENCES tenants (tenant_id) ON DELETE CASCADE,'
      ' id BIGINT GENERATED ALWAYS AS IDENTITY,'
      ' body TEXT NOT NULL CHECK (length(body) > 0),'
      ' topic TEXT NOT NULL DEFAULT '''','
      ' key TEXT,'
      ' source TEXT NOT NULL CHECK (length(source) > 0),'
      ' event_time TEXT NOT NULL,'
      ' knowledge_time TEXT NOT NULL,'
      ' tier INTEGER NOT NULL,'
      ' classes TEXT NOT NULL,'
      ' status TEXT NOT NULL DEFAULT ''active'' CHECK (status IN (''active'', ''superseded'', ''pruned'')),'
      ' supersedes BIGINT,'
      ' superseded_by BIGINT,'
      ' ended_at TEXT%s,'
      ' PRIMARY KEY (tenant_id, id))', t, extra);
    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I (tenant_id, status, key)', t || '_tenant_status_key', t);
    EXECUTE format(
      'ALTER TABLE %I ADD COLUMN IF NOT EXISTS fts tsvector GENERATED ALWAYS AS (to_tsvector(''simple'', coalesce(body, '''') || '' '' || coalesce(topic, '''') || '' '' || coalesce(key, ''''))) STORED',
      t);
    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I USING GIN (fts)', t || '_fts', t);
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
    EXECUTE format('CREATE POLICY tenant_isolation ON %I'
      ' USING (tenant_id = NULLIF(current_setting(''app.tenant'', true), ''''))'
      ' WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant'', true), ''''))', t);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO kyra_memory_app', t);
  END LOOP;
END $$;

ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON tenants;
CREATE POLICY tenant_isolation ON tenants
  USING (tenant_id = NULLIF(current_setting('app.tenant', true), ''))
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant', true), ''));
GRANT SELECT, INSERT, UPDATE, DELETE ON tenants TO kyra_memory_app;
