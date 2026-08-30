-- Verification as a job kind, and an index for finding a clip's uploads.
--
-- The pipeline now chains itself: every handler queues its own successor, and
-- the last link is a read back from the platform that can move a post from
-- `published` to `needs_attention`. That read is a job like any other, so it
-- needs a value in `JobKind`.
--
-- `ALTER TYPE ... ADD VALUE` cannot run inside a transaction block in
-- PostgreSQL before 12, and Prisma wraps each migration in one. On 16 it is
-- allowed, with one restriction that matters here: the new value cannot be
-- used in the *same* transaction that adds it. Nothing below does, and no
-- application code can either — the value only becomes reachable once this
-- migration has committed.

ALTER TYPE "JobKind" ADD VALUE IF NOT EXISTS 'verify_upload';

-- ---------------------------------------------------------------------------
-- Finding the posts waiting on a render
-- ---------------------------------------------------------------------------
--
-- A finished render has to locate the uploads booked against its clip so it
-- can point them at the file and move them out of `draft`. That is a lookup by
-- `clip_id` filtered on state, run once per render, and without an index it is
-- a sequential scan of every upload the tenant has ever made — which grows
-- without bound while the set it is looking for stays at one or two rows.
--
-- Not mirrored in `schema.prisma`: Prisma's `@@index` has no way to
-- express a `WHERE` predicate, so writing it there would create a second,
-- larger index rather than this one. `db/README.md`'s rule stands for
-- tables and columns; this is a physical detail Prisma cannot model.
--
-- Partial, on the two states a render can promote from. A published upload is
-- never promoted again, and keeping the millions of them out of the index is
-- the whole saving.

CREATE INDEX IF NOT EXISTS "uploads_clip_pending_idx"
  ON "uploads" ("tenant_id", "clip_id")
  WHERE "state" IN ('draft', 'scheduled');
