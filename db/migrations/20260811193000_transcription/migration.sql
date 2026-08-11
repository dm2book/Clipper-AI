-- Transcription runs: media in, words with timings out.
--
-- Prisma's DDL below; the hand-written half — policy, FORCE, updated_at
-- trigger — around it, as every new model needs. FORCE is lifted around the
-- foreign keys because the validation query a new FK runs reads the
-- referenced table through `tenant_isolation`, and a migration has no tenant
-- scope. See db/README.md.

ALTER TABLE public.tenants NO FORCE ROW LEVEL SECURITY;
ALTER TABLE public.sources NO FORCE ROW LEVEL SECURITY;

-- CreateEnum
CREATE TYPE "TranscriptionState" AS ENUM ('queued', 'processing', 'succeeded', 'failed_retryable', 'failed_permanent');

-- CreateTable
CREATE TABLE "transcription_runs" (
    "id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "source_id" TEXT,
    "state" "TranscriptionState" NOT NULL DEFAULT 'queued',
    "provider" TEXT NOT NULL DEFAULT '',
    "model" TEXT NOT NULL DEFAULT '',
    "media_path" TEXT NOT NULL DEFAULT '',
    "text" TEXT NOT NULL DEFAULT '',
    "transcript" JSONB,
    "language" TEXT NOT NULL DEFAULT '',
    "language_confidence" DOUBLE PRECISION,
    "word_count" INTEGER NOT NULL DEFAULT 0,
    "segment_count" INTEGER NOT NULL DEFAULT 0,
    "mean_confidence" DOUBLE PRECISION,
    "duration_s" DOUBLE PRECISION,
    "elapsed_s" DOUBLE PRECISION,
    "attempts" INTEGER NOT NULL DEFAULT 0,
    "last_error" TEXT NOT NULL DEFAULT '',
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "started_at" TIMESTAMPTZ(6),
    "finished_at" TIMESTAMPTZ(6),

    CONSTRAINT "transcription_runs_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "transcription_runs_tenant_id_state_idx" ON "transcription_runs"("tenant_id", "state");

-- CreateIndex
CREATE UNIQUE INDEX "transcription_runs_tenant_id_source_id_key" ON "transcription_runs"("tenant_id", "source_id");

-- AddForeignKey
ALTER TABLE "transcription_runs" ADD CONSTRAINT "transcription_runs_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "transcription_runs" ADD CONSTRAINT "transcription_runs_source_id_fkey" FOREIGN KEY ("source_id") REFERENCES "sources"("id") ON DELETE CASCADE ON UPDATE CASCADE;

ALTER TABLE public.tenants FORCE ROW LEVEL SECURITY;
ALTER TABLE public.sources FORCE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------
-- Row-level security for the new table
-- ---------------------------------------------------------------------------

ALTER TABLE public.transcription_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.transcription_runs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.transcription_runs;
CREATE POLICY tenant_isolation ON public.transcription_runs
  FOR ALL
  USING (tenant_id = app.current_tenant())
  WITH CHECK (tenant_id = app.current_tenant());

DROP TRIGGER IF EXISTS transcription_runs_touch_updated_at
  ON public.transcription_runs;
CREATE TRIGGER transcription_runs_touch_updated_at
  BEFORE UPDATE ON public.transcription_runs
  FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();

GRANT SELECT, INSERT, UPDATE, DELETE
  ON TABLE public.transcription_runs TO clipforge_app;
