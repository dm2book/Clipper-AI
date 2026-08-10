-- CreateSchema
CREATE SCHEMA IF NOT EXISTS "public";

-- CreateEnum
CREATE TYPE "PlanTier" AS ENUM ('starter', 'studio', 'agency', 'empire');

-- CreateEnum
CREATE TYPE "UserRole" AS ENUM ('owner', 'admin', 'operator', 'editor', 'analyst', 'viewer');

-- CreateEnum
CREATE TYPE "ChannelState" AS ENUM ('draft', 'active', 'paused', 'budget_exhausted', 'circuit_open');

-- CreateEnum
CREATE TYPE "PlatformKind" AS ENUM ('tiktok', 'youtube', 'instagram');

-- CreateEnum
CREATE TYPE "RightsBasisKind" AS ENUM ('owned', 'licensed', 'creator_permission', 'public_domain', 'creative_commons', 'stock', 'unverified');

-- CreateEnum
CREATE TYPE "VideoState" AS ENUM ('pending', 'rendering', 'ready', 'failed');

-- CreateEnum
CREATE TYPE "ScheduleFrequency" AS ENUM ('daily', 'weekly', 'monthly');

-- CreateEnum
CREATE TYPE "UploadState" AS ENUM ('draft', 'scheduled', 'claimed', 'uploading', 'processing', 'published', 'awaiting_creator', 'retrying', 'failed', 'needs_attention', 'cancelled');

-- CreateEnum
CREATE TYPE "JobKind" AS ENUM ('discover_sources', 'transcribe', 'detect_clips', 'generate_hooks', 'build_captions', 'render_video', 'publish_upload', 'collect_metrics', 'refresh_token', 'weekly_report');

-- CreateEnum
CREATE TYPE "JobState" AS ENUM ('queued', 'leased', 'running', 'succeeded', 'failed', 'dead');

-- CreateTable
CREATE TABLE "tenants" (
    "id" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "plan" "PlanTier" NOT NULL DEFAULT 'starter',
    "suspended" BOOLEAN NOT NULL DEFAULT false,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "tenants_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "users" (
    "id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "email" TEXT NOT NULL,
    "name" TEXT NOT NULL DEFAULT '',
    "role" "UserRole" NOT NULL,
    "active" BOOLEAN NOT NULL DEFAULT true,
    "project_ids" TEXT[] DEFAULT ARRAY[]::TEXT[],
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "users_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "projects" (
    "id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "timezone" TEXT NOT NULL DEFAULT 'UTC',
    "budget_cents" INTEGER NOT NULL DEFAULT 0,
    "archived" BOOLEAN NOT NULL DEFAULT false,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "projects_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "channels" (
    "id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "project_id" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "niche" TEXT NOT NULL,
    "state" "ChannelState" NOT NULL DEFAULT 'draft',
    "timezone" TEXT NOT NULL DEFAULT 'UTC',
    "topics" TEXT[] DEFAULT ARRAY[]::TEXT[],
    "accepted_rights" TEXT[] DEFAULT ARRAY[]::TEXT[],
    "monetised" BOOLEAN NOT NULL DEFAULT true,
    "cadence_override" INTEGER NOT NULL DEFAULT 0,
    "quality_floor_override" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "budget_monthly_cents" INTEGER NOT NULL DEFAULT 20000,
    "budget_spent_cents" INTEGER NOT NULL DEFAULT 0,
    "budget_period" TEXT NOT NULL DEFAULT '',
    "consecutive_failures" INTEGER NOT NULL DEFAULT 0,
    "circuit_opened_at" TIMESTAMPTZ(6),
    "last_error" TEXT NOT NULL DEFAULT '',
    "total_items" INTEGER NOT NULL DEFAULT 0,
    "total_published" INTEGER NOT NULL DEFAULT 0,
    "total_blocked" INTEGER NOT NULL DEFAULT 0,
    "total_failed" INTEGER NOT NULL DEFAULT 0,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "channels_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "social_accounts" (
    "id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "channel_id" TEXT,
    "platform" "PlatformKind" NOT NULL,
    "handle" TEXT NOT NULL DEFAULT '',
    "external_id" TEXT NOT NULL DEFAULT '',
    "timezone" TEXT NOT NULL DEFAULT 'UTC',
    "direct_post_approved" BOOLEAN NOT NULL DEFAULT false,
    "business_account" BOOLEAN NOT NULL DEFAULT false,
    "enabled" BOOLEAN NOT NULL DEFAULT true,
    "access_token_sealed" TEXT NOT NULL DEFAULT '',
    "refresh_token_sealed" TEXT NOT NULL DEFAULT '',
    "token_expires_at" TIMESTAMPTZ(6),
    "refresh_valid_until" TIMESTAMPTZ(6),
    "token_obtained_at" TIMESTAMPTZ(6),
    "scopes" TEXT[] DEFAULT ARRAY[]::TEXT[],
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "social_accounts_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "sources" (
    "id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "title" TEXT NOT NULL,
    "kind" TEXT NOT NULL,
    "url" TEXT NOT NULL DEFAULT '',
    "creator" TEXT NOT NULL DEFAULT '',
    "language" TEXT NOT NULL DEFAULT 'en',
    "topics" TEXT[] DEFAULT ARRAY[]::TEXT[],
    "duration_s" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "has_transcript" BOOLEAN NOT NULL DEFAULT false,
    "published_at" TIMESTAMPTZ(6),
    "fingerprint" TEXT NOT NULL,
    "rights_basis" "RightsBasisKind" NOT NULL DEFAULT 'unverified',
    "rights_reference" TEXT NOT NULL DEFAULT '',
    "rights_attribution" TEXT NOT NULL DEFAULT '',
    "commercial_use" BOOLEAN NOT NULL DEFAULT true,
    "derivatives" BOOLEAN NOT NULL DEFAULT true,
    "rights_verified_at" TIMESTAMPTZ(6),
    "rights_expires_at" TIMESTAMPTZ(6),
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "sources_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "channel_source_uses" (
    "tenant_id" TEXT NOT NULL,
    "channel_id" TEXT NOT NULL,
    "source_id" TEXT NOT NULL,
    "used_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "channel_source_uses_pkey" PRIMARY KEY ("channel_id","source_id")
);

-- CreateTable
CREATE TABLE "videos" (
    "id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "clip_id" TEXT,
    "source_id" TEXT,
    "state" "VideoState" NOT NULL DEFAULT 'pending',
    "storage_key" TEXT NOT NULL DEFAULT '',
    "public_url" TEXT NOT NULL DEFAULT '',
    "checksum" TEXT NOT NULL DEFAULT '',
    "size_bytes" BIGINT NOT NULL DEFAULT 0,
    "duration_s" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "width" INTEGER NOT NULL DEFAULT 1080,
    "height" INTEGER NOT NULL DEFAULT 1920,
    "fps" INTEGER NOT NULL DEFAULT 60,
    "render_plan" JSONB,
    "render_error" TEXT NOT NULL DEFAULT '',
    "rendered_at" TIMESTAMPTZ(6),
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "videos_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "clips" (
    "id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "channel_id" TEXT NOT NULL,
    "source_id" TEXT,
    "start_ms" INTEGER NOT NULL,
    "end_ms" INTEGER NOT NULL,
    "duration_s" DOUBLE PRECISION NOT NULL,
    "title" TEXT NOT NULL DEFAULT '',
    "transcript" TEXT NOT NULL DEFAULT '',
    "virality_score" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "scores" JSONB,
    "features" JSONB,
    "signals" TEXT[] DEFAULT ARRAY[]::TEXT[],
    "weights_version" TEXT NOT NULL DEFAULT '',
    "hook_text" TEXT NOT NULL DEFAULT '',
    "hook_type" TEXT NOT NULL DEFAULT '',
    "hook_rank" INTEGER NOT NULL DEFAULT 0,
    "hook_explored" BOOLEAN NOT NULL DEFAULT false,
    "predicted_lift" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "hook_candidates" JSONB,
    "caption_track" JSONB,
    "topic" TEXT NOT NULL DEFAULT '',
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "clips_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "schedules" (
    "id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "channel_id" TEXT NOT NULL,
    "frequency" "ScheduleFrequency" NOT NULL,
    "timezone" TEXT NOT NULL DEFAULT 'UTC',
    "times_local" TEXT[] DEFAULT ARRAY[]::TEXT[],
    "weekdays" INTEGER[] DEFAULT ARRAY[]::INTEGER[],
    "month_days" INTEGER[] DEFAULT ARRAY[]::INTEGER[],
    "interval" INTEGER NOT NULL DEFAULT 1,
    "starts_on" TIMESTAMPTZ(6),
    "ends_on" TIMESTAMPTZ(6),
    "max_occurrences" INTEGER NOT NULL DEFAULT 0,
    "nonexistent_time_policy" TEXT NOT NULL DEFAULT 'shift',
    "ambiguous_time_policy" TEXT NOT NULL DEFAULT 'first',
    "enabled" BOOLEAN NOT NULL DEFAULT true,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "schedules_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "uploads" (
    "id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "channel_id" TEXT NOT NULL,
    "account_id" TEXT NOT NULL,
    "clip_id" TEXT,
    "video_id" TEXT,
    "schedule_id" TEXT,
    "platform" "PlatformKind" NOT NULL,
    "state" "UploadState" NOT NULL DEFAULT 'scheduled',
    "run_at" TIMESTAMPTZ(6) NOT NULL,
    "next_attempt_at" TIMESTAMPTZ(6),
    "lease_until" TIMESTAMPTZ(6),
    "lease_owner" TEXT NOT NULL DEFAULT '',
    "title" TEXT NOT NULL DEFAULT '',
    "caption" TEXT NOT NULL DEFAULT '',
    "visibility" TEXT NOT NULL DEFAULT 'public',
    "metadata" JSONB,
    "idempotency_key" TEXT NOT NULL,
    "remote_post_id" TEXT NOT NULL DEFAULT '',
    "attempt_count" INTEGER NOT NULL DEFAULT 0,
    "attempts" JSONB,
    "last_error" TEXT NOT NULL DEFAULT '',
    "published_at" TIMESTAMPTZ(6),
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "uploads_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "metric_snapshots" (
    "id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "upload_id" TEXT NOT NULL,
    "taken_at" TIMESTAMPTZ(6) NOT NULL,
    "age_hours" DOUBLE PRECISION NOT NULL,
    "views" INTEGER NOT NULL DEFAULT 0,
    "likes" INTEGER NOT NULL DEFAULT 0,
    "comments" INTEGER NOT NULL DEFAULT 0,
    "shares" INTEGER NOT NULL DEFAULT 0,
    "saves" INTEGER NOT NULL DEFAULT 0,
    "follows" INTEGER NOT NULL DEFAULT 0,
    "impressions" INTEGER NOT NULL DEFAULT 0,
    "watch_time_s" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "avg_watch_pct" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "retention_curve" JSONB,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "metric_snapshots_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "jobs" (
    "id" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "channel_id" TEXT,
    "kind" "JobKind" NOT NULL,
    "state" "JobState" NOT NULL DEFAULT 'queued',
    "priority" INTEGER NOT NULL DEFAULT 100,
    "payload" JSONB,
    "result" JSONB,
    "run_after" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "lease_until" TIMESTAMPTZ(6),
    "lease_owner" TEXT NOT NULL DEFAULT '',
    "attempts" INTEGER NOT NULL DEFAULT 0,
    "max_attempts" INTEGER NOT NULL DEFAULT 8,
    "last_error" TEXT NOT NULL DEFAULT '',
    "dedupe_key" TEXT,
    "started_at" TIMESTAMPTZ(6),
    "finished_at" TIMESTAMPTZ(6),
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "jobs_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "users_tenant_id_active_idx" ON "users"("tenant_id", "active");

-- CreateIndex
CREATE UNIQUE INDEX "users_tenant_id_email_key" ON "users"("tenant_id", "email");

-- CreateIndex
CREATE INDEX "projects_tenant_id_archived_idx" ON "projects"("tenant_id", "archived");

-- CreateIndex
CREATE INDEX "channels_tenant_id_state_idx" ON "channels"("tenant_id", "state");

-- CreateIndex
CREATE INDEX "channels_tenant_id_project_id_idx" ON "channels"("tenant_id", "project_id");

-- CreateIndex
CREATE INDEX "social_accounts_tenant_id_platform_idx" ON "social_accounts"("tenant_id", "platform");

-- CreateIndex
CREATE INDEX "social_accounts_tenant_id_channel_id_idx" ON "social_accounts"("tenant_id", "channel_id");

-- CreateIndex
CREATE INDEX "social_accounts_refresh_valid_until_idx" ON "social_accounts"("refresh_valid_until");

-- CreateIndex
CREATE INDEX "sources_tenant_id_rights_basis_idx" ON "sources"("tenant_id", "rights_basis");

-- CreateIndex
CREATE INDEX "sources_tenant_id_rights_expires_at_idx" ON "sources"("tenant_id", "rights_expires_at");

-- CreateIndex
CREATE UNIQUE INDEX "sources_tenant_id_fingerprint_key" ON "sources"("tenant_id", "fingerprint");

-- CreateIndex
CREATE INDEX "channel_source_uses_tenant_id_idx" ON "channel_source_uses"("tenant_id");

-- CreateIndex
CREATE INDEX "videos_tenant_id_state_idx" ON "videos"("tenant_id", "state");

-- CreateIndex
CREATE INDEX "videos_tenant_id_clip_id_idx" ON "videos"("tenant_id", "clip_id");

-- CreateIndex
CREATE INDEX "clips_tenant_id_channel_id_idx" ON "clips"("tenant_id", "channel_id");

-- CreateIndex
CREATE INDEX "clips_tenant_id_created_at_idx" ON "clips"("tenant_id", "created_at");

-- CreateIndex
CREATE INDEX "schedules_tenant_id_channel_id_enabled_idx" ON "schedules"("tenant_id", "channel_id", "enabled");

-- CreateIndex
CREATE INDEX "uploads_tenant_id_state_run_at_idx" ON "uploads"("tenant_id", "state", "run_at");

-- CreateIndex
CREATE INDEX "uploads_tenant_id_account_id_run_at_idx" ON "uploads"("tenant_id", "account_id", "run_at");

-- CreateIndex
CREATE INDEX "uploads_tenant_id_channel_id_run_at_idx" ON "uploads"("tenant_id", "channel_id", "run_at");

-- CreateIndex
CREATE UNIQUE INDEX "uploads_tenant_id_idempotency_key_key" ON "uploads"("tenant_id", "idempotency_key");

-- CreateIndex
CREATE INDEX "metric_snapshots_tenant_id_taken_at_idx" ON "metric_snapshots"("tenant_id", "taken_at");

-- CreateIndex
CREATE INDEX "metric_snapshots_tenant_id_upload_id_idx" ON "metric_snapshots"("tenant_id", "upload_id");

-- CreateIndex
CREATE UNIQUE INDEX "metric_snapshots_upload_id_age_hours_key" ON "metric_snapshots"("upload_id", "age_hours");

-- CreateIndex
CREATE INDEX "jobs_state_run_after_priority_idx" ON "jobs"("state", "run_after", "priority");

-- CreateIndex
CREATE INDEX "jobs_tenant_id_kind_state_idx" ON "jobs"("tenant_id", "kind", "state");

-- CreateIndex
CREATE UNIQUE INDEX "jobs_tenant_id_dedupe_key_key" ON "jobs"("tenant_id", "dedupe_key");

-- AddForeignKey
ALTER TABLE "users" ADD CONSTRAINT "users_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "projects" ADD CONSTRAINT "projects_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "channels" ADD CONSTRAINT "channels_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "channels" ADD CONSTRAINT "channels_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "projects"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "social_accounts" ADD CONSTRAINT "social_accounts_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "social_accounts" ADD CONSTRAINT "social_accounts_channel_id_fkey" FOREIGN KEY ("channel_id") REFERENCES "channels"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "sources" ADD CONSTRAINT "sources_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "channel_source_uses" ADD CONSTRAINT "channel_source_uses_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "channel_source_uses" ADD CONSTRAINT "channel_source_uses_channel_id_fkey" FOREIGN KEY ("channel_id") REFERENCES "channels"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "channel_source_uses" ADD CONSTRAINT "channel_source_uses_source_id_fkey" FOREIGN KEY ("source_id") REFERENCES "sources"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "videos" ADD CONSTRAINT "videos_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "videos" ADD CONSTRAINT "videos_clip_id_fkey" FOREIGN KEY ("clip_id") REFERENCES "clips"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "videos" ADD CONSTRAINT "videos_source_id_fkey" FOREIGN KEY ("source_id") REFERENCES "sources"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "clips" ADD CONSTRAINT "clips_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "clips" ADD CONSTRAINT "clips_channel_id_fkey" FOREIGN KEY ("channel_id") REFERENCES "channels"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "clips" ADD CONSTRAINT "clips_source_id_fkey" FOREIGN KEY ("source_id") REFERENCES "sources"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "schedules" ADD CONSTRAINT "schedules_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "schedules" ADD CONSTRAINT "schedules_channel_id_fkey" FOREIGN KEY ("channel_id") REFERENCES "channels"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "uploads" ADD CONSTRAINT "uploads_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "uploads" ADD CONSTRAINT "uploads_channel_id_fkey" FOREIGN KEY ("channel_id") REFERENCES "channels"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "uploads" ADD CONSTRAINT "uploads_account_id_fkey" FOREIGN KEY ("account_id") REFERENCES "social_accounts"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "uploads" ADD CONSTRAINT "uploads_clip_id_fkey" FOREIGN KEY ("clip_id") REFERENCES "clips"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "uploads" ADD CONSTRAINT "uploads_video_id_fkey" FOREIGN KEY ("video_id") REFERENCES "videos"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "uploads" ADD CONSTRAINT "uploads_schedule_id_fkey" FOREIGN KEY ("schedule_id") REFERENCES "schedules"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "metric_snapshots" ADD CONSTRAINT "metric_snapshots_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "metric_snapshots" ADD CONSTRAINT "metric_snapshots_upload_id_fkey" FOREIGN KEY ("upload_id") REFERENCES "uploads"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "jobs" ADD CONSTRAINT "jobs_tenant_id_fkey" FOREIGN KEY ("tenant_id") REFERENCES "tenants"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "jobs" ADD CONSTRAINT "jobs_channel_id_fkey" FOREIGN KEY ("channel_id") REFERENCES "channels"("id") ON DELETE SET NULL ON UPDATE CASCADE;

