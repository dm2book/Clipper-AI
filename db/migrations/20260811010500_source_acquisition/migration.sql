-- Source acquisition: the record of turning a pasted URL into material.
--
-- Prisma's DDL is below the fold; the rest is the hand-written half every new
-- model needs — policies, FORCE, and the updated_at trigger, none of which
-- Prisma emits. `test_every_tenant_scoped_table_has_a_policy` fails on any
-- public table without them.
--
-- FORCE is lifted around the foreign keys and restored after. The validation
-- query a new FK runs reads the referenced table, that read goes through
-- `tenant_isolation`, and a migration has no tenant scope — so it raises on an
-- empty table it was about to create. See db/README.md; migration 003 is the
-- other worked example.

ALTER TABLE public.tenants NO FORCE ROW LEVEL SECURITY;
ALTER TABLE public.sources NO FORCE ROW LEVEL SECURITY;

-- CreateEnum
CREATE TYPE "AcquisitionState" AS ENUM ('queued', 'fetching_metadata', 'downloading', 'probing', 'ready', 'failed', 'cancelled');

-- CreateEnum
CREATE TYPE "AcquisitionInputKind" AS ENUM ('youtube_video', 'youtube_channel', 'podcast_feed', 'local_file', 'media_url');

-- CreateTable
CREATE TABLE "acquisition_runs" (
    "id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "source_id" TEXT,
    "channel_id" TEXT,
    "kind" "AcquisitionInputKind" NOT NULL,
    "state" "AcquisitionState" NOT NULL DEFAULT 'queued',
    "ref_key" TEXT NOT NULL,
    "ref_raw" TEXT NOT NULL DEFAULT '',
    "url" TEXT NOT NULL DEFAULT '',
    "title" TEXT NOT NULL DEFAULT '',
    "creator" TEXT NOT NULL DEFAULT '',
    "external_id" TEXT NOT NULL DEFAULT '',
    "published_at" TIMESTAMPTZ(6),
    "media_path" TEXT NOT NULL DEFAULT '',
    "bytes_done" BIGINT NOT NULL DEFAULT 0,
    "bytes_total" BIGINT,
    "validator" TEXT NOT NULL DEFAULT '',
    "content_type" TEXT NOT NULL DEFAULT '',
    "checksum" TEXT NOT NULL DEFAULT '',
    "resumable" BOOLEAN NOT NULL DEFAULT false,
    "duration_s" DOUBLE PRECISION,
    "width" INTEGER,
    "height" INTEGER,
    "has_audio" BOOLEAN NOT NULL DEFAULT false,
    "has_video" BOOLEAN NOT NULL DEFAULT false,
    "prober" TEXT NOT NULL DEFAULT '',
    "thumbnail_path" TEXT NOT NULL DEFAULT '',
    "thumbnail_origin" TEXT NOT NULL DEFAULT '',
    "metadata" JSONB,
    "attempts" INTEGER NOT NULL DEFAULT 0,
    "last_error" TEXT NOT NULL DEFAULT '',
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "finished_at" TIMESTAMPTZ(6),

    CONSTRAINT "acquisition_runs_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "acquisition_runs_tenant_id_state_idx" ON "acquisition_runs"("tenant_id", "state");

-- CreateIndex
CREATE INDEX "acquisition_runs_tenant_id_source_id_idx" ON "acquisition_runs"("tenant_id", "source_id");

-- CreateIndex
CREATE UNIQUE INDEX "acquisition_runs_tenant_id_channel_id_kind_ref_key_key" ON "acquisition_runs"("tenant_id", "channel_id", "kind", "ref_key");

-- AddForeignKey
ALTER TABLE "acquisition_runs" ADD CONSTRAINT "acquisition_runs_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "acquisition_runs" ADD CONSTRAINT "acquisition_runs_source_id_fkey" FOREIGN KEY ("source_id") REFERENCES "sources"("id") ON DELETE SET NULL ON UPDATE CASCADE;

ALTER TABLE public.tenants FORCE ROW LEVEL SECURITY;
ALTER TABLE public.sources FORCE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------
-- Row-level security for the new table
-- ---------------------------------------------------------------------------

ALTER TABLE public.acquisition_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.acquisition_runs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.acquisition_runs;
CREATE POLICY tenant_isolation ON public.acquisition_runs
  FOR ALL
  USING (tenant_id = app.current_tenant())
  WITH CHECK (tenant_id = app.current_tenant());

DROP TRIGGER IF EXISTS acquisition_runs_touch_updated_at ON public.acquisition_runs;
CREATE TRIGGER acquisition_runs_touch_updated_at
  BEFORE UPDATE ON public.acquisition_runs
  FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();

GRANT SELECT, INSERT, UPDATE, DELETE
  ON TABLE public.acquisition_runs TO clipforge_app;
