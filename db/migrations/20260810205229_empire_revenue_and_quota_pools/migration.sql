-- Empire: operator-entered revenue, and API quota pools.
--
-- Two tables plus the row-level security they need. The DDL below the fold is
-- Prisma's; everything around it is not, and a new model always needs the
-- hand-written half — Prisma emits no policies, no FORCE, and no updated_at
-- trigger. `tests/test_row_level_security.py` fails on any public table that
-- ends up without them.
--
-- ## Why FORCE has to be lifted around the foreign keys
--
-- `ADD CONSTRAINT ... FOREIGN KEY` makes Postgres run a validation query that
-- reads the *referenced* table. `tenants` and `projects` are FORCEd, so that
-- read goes through `tenant_isolation`, which calls `app.current_tenant()`,
-- which raises because a migration has no tenant scope — and the migration
-- dies on an empty table it was about to create.
--
-- Setting a sentinel scope instead would be worse: the validation would then
-- run against one tenant's rows and quietly conclude the constraint holds.
-- Lifting FORCE for the length of the statement is the honest version, and it
-- is the escape hatch migration 002 documents. It is restored below, and the
-- policy-coverage test is what catches a migration that forgets to.

ALTER TABLE public.tenants NO FORCE ROW LEVEL SECURITY;
ALTER TABLE public.projects NO FORCE ROW LEVEL SECURITY;

-- CreateEnum
CREATE TYPE "PoolOwnershipKind" AS ENUM ('shared_app', 'per_tenant');

-- CreateTable
CREATE TABLE "revenue_entries" (
    "id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "project_id" TEXT NOT NULL,
    "period" TEXT NOT NULL,
    "sponsorship_cents" INTEGER NOT NULL DEFAULT 0,
    "affiliate_cents" INTEGER NOT NULL DEFAULT 0,
    "own_product_cents" INTEGER NOT NULL DEFAULT 0,
    "services_cents" INTEGER NOT NULL DEFAULT 0,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "revenue_entries_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "quota_pools" (
    "id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "platform" "PlatformKind" NOT NULL,
    "ownership" "PoolOwnershipKind" NOT NULL,
    "daily_units" INTEGER NOT NULL DEFAULT 0,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "quota_pools_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "revenue_entries_tenant_id_period_idx" ON "revenue_entries"("tenant_id", "period");

-- CreateIndex
CREATE UNIQUE INDEX "revenue_entries_tenant_id_project_id_period_key" ON "revenue_entries"("tenant_id", "project_id", "period");

-- CreateIndex
CREATE INDEX "quota_pools_tenant_id_platform_idx" ON "quota_pools"("tenant_id", "platform");

-- AddForeignKey
ALTER TABLE "revenue_entries" ADD CONSTRAINT "revenue_entries_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "revenue_entries" ADD CONSTRAINT "revenue_entries_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "projects"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "quota_pools" ADD CONSTRAINT "quota_pools_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

ALTER TABLE public.tenants FORCE ROW LEVEL SECURITY;
ALTER TABLE public.projects FORCE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------
-- Row-level security for the new tables
-- ---------------------------------------------------------------------------

DO $$
DECLARE
  target text;
BEGIN
  FOREACH target IN ARRAY ARRAY['revenue_entries', 'quota_pools'] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', target);
    EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', target);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON public.%I', target);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON public.%I FOR ALL '
      'USING (tenant_id = app.current_tenant()) '
      'WITH CHECK (tenant_id = app.current_tenant())', target);

    EXECUTE format('DROP TRIGGER IF EXISTS %I ON public.%I',
                   target || '_touch_updated_at', target);
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE ON public.%I '
      'FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at()',
      target || '_touch_updated_at', target);
  END LOOP;
END
$$;

-- The default privileges from migration 002 cover tables created by
-- clipforge_owner, but granting explicitly costs nothing and does not depend
-- on which role happened to run the migration.
GRANT SELECT, INSERT, UPDATE, DELETE
  ON TABLE public.revenue_entries, public.quota_pools TO clipforge_app;
