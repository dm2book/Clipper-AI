-- Row-level security, updated_at enforcement, and runtime grants.
--
-- Hand-written because Prisma models none of it. Everything here is idempotent
-- and safe to re-run.
--
-- The shape of the isolation guarantee:
--
--   * Every tenant-scoped table carries `tenant_id` on the row, so each policy
--     is a predicate over the row it protects. No policy below contains a
--     join, because a policy that needs a join is a policy that will one day
--     be written wrong and no test will notice.
--   * The tenant comes from `app.current_tenant()`, which reads a per
--     transaction GUC set by `SET LOCAL app.tenant_id`. Connection-pooled
--     safely: `SET LOCAL` dies with the transaction, so a pooled connection
--     cannot carry one customer's tenant into the next customer's query.
--   * `app.current_tenant()` raises when the GUC is unset rather than
--     returning NULL. A NULL would make every policy fail closed, which sounds
--     safe and is not: a forgotten `SET LOCAL` would then look like a tenant
--     with no data, and "the dashboard is empty" gets diagnosed as a product
--     bug for a week. An error names the mistake at the first query.

-- ---------------------------------------------------------------------------
-- 1. The tenant GUC
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS app;

CREATE OR REPLACE FUNCTION app.current_tenant() RETURNS text
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
  value text;
BEGIN
  value := current_setting('app.tenant_id', true);
  IF value IS NULL OR value = '' THEN
    RAISE EXCEPTION
      'app.tenant_id is not set: this statement has no tenant scope'
      USING ERRCODE = '42501';  -- insufficient_privilege
  END IF;
  RETURN value;
END;
$$;

COMMENT ON FUNCTION app.current_tenant() IS
  'Tenant for the current transaction, from SET LOCAL app.tenant_id. Raises '
  'when unset. STABLE and argument-free, so the planner folds it to a constant '
  'once per query and the (tenant_id, ...) indexes stay usable.';

-- ---------------------------------------------------------------------------
-- 2. updated_at
--
-- Prisma's `@updatedAt` is maintained by the Prisma client, which this
-- application does not use — it talks to Postgres through psycopg. Without a
-- trigger the column would be whatever each hand-written UPDATE remembered to
-- set, which is a convention, and conventions are not what "nothing important
-- may be lost" is built on. The INSERT side is covered by the DEFAULT in
-- migration 001.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION app.touch_updated_at() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

DO $$
DECLARE
  target text;
BEGIN
  FOREACH target IN ARRAY ARRAY[
    'tenants', 'users', 'projects', 'channels', 'social_accounts', 'sources',
    'videos', 'clips', 'schedules', 'uploads', 'jobs'
  ] LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS %I ON public.%I',
                   target || '_touch_updated_at', target);
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE ON public.%I '
      'FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at()',
      target || '_touch_updated_at', target);
  END LOOP;
END
$$;

-- ---------------------------------------------------------------------------
-- 3. Row-level security
--
-- FORCE as well as ENABLE. ENABLE alone leaves the table owner exempt, and the
-- single most likely way this isolation gets lost in production is not an
-- attacker — it is a copy-pasted DATABASE_URL that points the application at
-- clipforge_owner. FORCE is what makes that mistake fail loudly instead of
-- quietly serving every tenant's rows to everyone.
--
-- The cost is that a migration doing a data backfill must scope itself too. If
-- one ever needs to run unscoped, it does so explicitly and puts it back:
--
--   ALTER TABLE public.clips NO FORCE ROW LEVEL SECURITY;
--   -- ... backfill ...
--   ALTER TABLE public.clips FORCE ROW LEVEL SECURITY;
-- ---------------------------------------------------------------------------

-- `tenants` is the one table keyed by `id` rather than `tenant_id`.
ALTER TABLE public.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.tenants;
CREATE POLICY tenant_isolation ON public.tenants
  FOR ALL
  USING (id = app.current_tenant())
  WITH CHECK (id = app.current_tenant());

DO $$
DECLARE
  target text;
BEGIN
  FOREACH target IN ARRAY ARRAY[
    'users', 'projects', 'channels', 'social_accounts', 'sources',
    'channel_source_uses', 'videos', 'clips', 'schedules', 'uploads',
    'metric_snapshots', 'jobs'
  ] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', target);
    EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', target);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON public.%I', target);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON public.%I FOR ALL '
      'USING (tenant_id = app.current_tenant()) '
      'WITH CHECK (tenant_id = app.current_tenant())', target);
  END LOOP;
END
$$;

-- ---------------------------------------------------------------------------
-- 4. Grants
-- ---------------------------------------------------------------------------

GRANT USAGE ON SCHEMA public TO clipforge_app, clipforge_worker;
GRANT USAGE ON SCHEMA app TO clipforge_app;
GRANT EXECUTE ON FUNCTION app.current_tenant() TO clipforge_app;

GRANT SELECT, INSERT, UPDATE, DELETE
  ON ALL TABLES IN SCHEMA public TO clipforge_app;

-- The migration ledger is Prisma's, and the application has no business in it.
REVOKE ALL ON TABLE public._prisma_migrations FROM clipforge_app;

-- metric_snapshots is append-only. The analytics engine compares posts at
-- matched ages; a snapshot rewritten after the fact makes every such
-- comparison quietly wrong, and the original reading cannot be recovered.
-- Enforced as a privilege rather than trusted to the repository layer.
REVOKE UPDATE, DELETE ON TABLE public.metric_snapshots FROM clipforge_app;

-- The dispatcher's entire reach. BYPASSRLS on clipforge_worker means these
-- grants are the only thing bounding it, so they stop at the queue.
GRANT SELECT, INSERT, UPDATE ON TABLE public.jobs TO clipforge_worker;

-- Tables added by later migrations inherit the same grants, so a new table is
-- not silently unreadable until someone notices in staging.
ALTER DEFAULT PRIVILEGES FOR ROLE clipforge_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO clipforge_app;
ALTER DEFAULT PRIVILEGES FOR ROLE clipforge_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO clipforge_app;
