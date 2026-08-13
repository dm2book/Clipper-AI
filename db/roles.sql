-- Cluster roles for ClipForge AI. Run once, as a superuser, before the first
-- `prisma migrate deploy`. Not a migration: roles are cluster-scoped rather
-- than database-scoped, and the migration role deliberately has no CREATEROLE.
--
-- Migration 002 grants privileges to these roles by name. If this file has not
-- been run, that migration fails with "role does not exist" — which is the
-- correct failure, because the alternative is a schema with no one allowed to
-- read it.
--
--   psql -v ON_ERROR_STOP=1 \
--        -v owner_password="$PGOWNER_PASSWORD" \
--        -v app_password="$PGAPP_PASSWORD" \
--        -v worker_password="$PGWORKER_PASSWORD" \
--        -v auth_password="$PGAUTH_PASSWORD" \
--        -f db/roles.sql
--
-- Passwords are passed in, never written here.

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------------
-- clipforge_owner — owns the schema, runs migrations, is never used at runtime.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'clipforge_owner') THEN
    CREATE ROLE clipforge_owner LOGIN;
  END IF;
END
$$;
ALTER ROLE clipforge_owner PASSWORD :'owner_password';

-- ---------------------------------------------------------------------------
-- clipforge_app — the request path. Everything the application does under a
-- known tenant runs as this role.
--
-- Not a superuser and not the table owner, on purpose. Postgres lets both
-- bypass row-level security, so an application connected as either turns every
-- policy in migration 002 into a no-op — and, worse, the isolation tests keep
-- passing while proving nothing. NOBYPASSRLS is stated explicitly rather than
-- left to the default, because the default is what a future ALTER ROLE will be
-- compared against.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'clipforge_app') THEN
    CREATE ROLE clipforge_app LOGIN;
  END IF;
END
$$;
ALTER ROLE clipforge_app NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
ALTER ROLE clipforge_app PASSWORD :'app_password';

-- ---------------------------------------------------------------------------
-- clipforge_worker — the job dispatcher, and the one role that sees across
-- tenants.
--
-- The queue is necessarily cross-tenant: a worker picking up the oldest due
-- job cannot know whose job it is until it has read one. So this role holds
-- BYPASSRLS — but its grants (migration 002) reach exactly one table, `jobs`,
-- and the dispatcher's contract is narrow: claim a job, read its tenant_id,
-- then open a separate clipforge_app connection scoped to that tenant and do
-- the actual work there. The blast radius of BYPASSRLS is one queue table, and
-- no clip, caption, token or metric is ever read through it.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'clipforge_worker') THEN
    CREATE ROLE clipforge_worker LOGIN;
  END IF;
END
$$;
ALTER ROLE clipforge_worker NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
ALTER ROLE clipforge_worker PASSWORD :'worker_password';

-- ---------------------------------------------------------------------------
-- clipforge_auth — the authentication service, and the only role that can see
-- a password hash.
--
-- Narrow in the way clipforge_worker is narrow. Its grants (migration 006)
-- reach the five `auth_*` tables and nothing else, and — the point of the
-- whole arrangement — clipforge_app is granted *nothing* on those tables in
-- return. The request path therefore cannot read a credential at any tenant
-- setting, so an injection in the application reaches clips and captions and
-- stops there.
--
-- NOBYPASSRLS despite the auth tables having no policies: it must never be
-- able to read tenant data, and the day someone adds a policy-bearing table
-- to its grants is the day the attribute matters.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'clipforge_auth') THEN
    CREATE ROLE clipforge_auth LOGIN;
  END IF;
END
$$;
ALTER ROLE clipforge_auth NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
ALTER ROLE clipforge_auth PASSWORD :'auth_password';
