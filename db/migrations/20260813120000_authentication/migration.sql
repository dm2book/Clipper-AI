-- Authentication: identities, sessions, tokens, rate limits and an audit log.
--
-- These five tables are the exception to every convention in migration 002,
-- and each departure is deliberate.
--
-- **No row-level security, and no policies.** RLS scopes rows to
-- `app.current_tenant()`. Authentication happens before a tenant is known —
-- at the moment someone types an email and a password there is nothing to
-- scope to — so a policy here could only ever be `USING (true)`, which is a
-- policy in name only. The boundary is the grant instead.
--
-- **`clipforge_app` is granted nothing.** Migration 002 sets ALTER DEFAULT
-- PRIVILEGES so every later table is readable by the application role. That is
-- right for tenant data and wrong for password hashes, so the grant is revoked
-- explicitly below. The result is stronger than RLS: the role the whole
-- request path runs as cannot read these rows at any tenant setting, so an
-- injection in the application reaches clips and captions and cannot reach a
-- credential.
--
-- **A fourth role, `clipforge_auth`.** Narrow in the same way
-- `clipforge_worker` is narrow: it reaches these five tables and nothing else.
-- Only the authentication service connects as it.

-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------

CREATE TYPE "IdentityStatus" AS ENUM (
  'pending', 'active', 'locked', 'pending_deletion', 'deleted'
);

CREATE TYPE "AuthTokenKind" AS ENUM (
  'email_verification', 'password_reset'
);

-- ---------------------------------------------------------------------------
-- Identities
-- ---------------------------------------------------------------------------

CREATE TABLE "auth_identities" (
  "id"                TEXT PRIMARY KEY,
  -- Globally unique, and lowercased by the application before it arrives.
  -- This is the one identifier in the system that is not tenant-scoped: a
  -- person has one password, not one per workspace.
  "email"             TEXT NOT NULL,
  "password_hash"     TEXT NOT NULL DEFAULT '',
  -- Which construction produced the hash, so it can be upgraded on the next
  -- login when the cost factor changes rather than by a mass reset.
  "password_algo"     TEXT NOT NULL DEFAULT '',
  "status"            "IdentityStatus" NOT NULL DEFAULT 'pending',
  "email_verified_at" TIMESTAMPTZ(6),
  "failed_attempts"   INTEGER NOT NULL DEFAULT 0,
  "locked_until"      TIMESTAMPTZ(6),
  "last_login_at"     TIMESTAMPTZ(6),
  -- When the grace period ends and the purge may run.
  "delete_after"      TIMESTAMPTZ(6),
  "created_at"        TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updated_at"        TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX "auth_identities_email_key" ON "auth_identities"("email");
CREATE INDEX "auth_identities_status_idx" ON "auth_identities"("status");
-- Partial: the purge sweep asks only about the handful of rows awaiting one,
-- and a full index on a nullable column the sweep never otherwise reads is
-- write cost for no read benefit.
CREATE INDEX "auth_identities_delete_after_idx"
  ON "auth_identities"("delete_after")
  WHERE "delete_after" IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Memberships — the join between an identity and `users`
-- ---------------------------------------------------------------------------

CREATE TABLE "auth_memberships" (
  -- Mirrors `users.id`. Not a foreign key: `users` is tenant-scoped and under
  -- FORCE RLS, and a FK from here would make every membership write depend on
  -- a tenant setting this connection does not have.
  "user_id"     TEXT PRIMARY KEY,
  "tenant_id"   TEXT NOT NULL,
  "identity_id" TEXT NOT NULL REFERENCES "auth_identities"("id") ON DELETE CASCADE,
  "role"        TEXT NOT NULL DEFAULT 'viewer',
  "active"      BOOLEAN NOT NULL DEFAULT true,
  -- Denormalised so a login can offer "which workspace?" without reading a
  -- tenant-scoped table it has no grant on.
  "tenant_name" TEXT NOT NULL DEFAULT '',
  "created_at"  TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX "auth_memberships_identity_idx" ON "auth_memberships"("identity_id");
CREATE UNIQUE INDEX "auth_memberships_identity_tenant_key"
  ON "auth_memberships"("identity_id", "tenant_id");

-- ---------------------------------------------------------------------------
-- Sessions — one row per refresh-token family
-- ---------------------------------------------------------------------------

CREATE TABLE "auth_sessions" (
  "id"                 TEXT PRIMARY KEY,
  "identity_id"        TEXT NOT NULL REFERENCES "auth_identities"("id") ON DELETE CASCADE,
  -- SHA-256 of the current refresh token. The token itself is never stored:
  -- a database dump must not be a set of working logins.
  "token_hash"         TEXT NOT NULL,
  -- The hash this rotated away from, kept one generation. Presenting it again
  -- is either a client racing itself or a stolen copy, and the two are not
  -- distinguishable at the time, so the family is revoked.
  "previous_hash"      TEXT,
  "tenant_id"          TEXT,
  "user_agent"         TEXT NOT NULL DEFAULT '',
  "ip"                 INET,
  "issued_at"          TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "expires_at"         TIMESTAMPTZ(6),
  -- The ceiling regardless of activity. A session that refreshes for ever is
  -- a stolen token that works for ever.
  "absolute_expires_at" TIMESTAMPTZ(6),
  "revoked_at"         TIMESTAMPTZ(6),
  "revoked_reason"     TEXT NOT NULL DEFAULT '',
  "rotations"          INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX "auth_sessions_token_hash_key" ON "auth_sessions"("token_hash");
CREATE INDEX "auth_sessions_previous_hash_idx"
  ON "auth_sessions"("previous_hash") WHERE "previous_hash" IS NOT NULL;
CREATE INDEX "auth_sessions_identity_idx" ON "auth_sessions"("identity_id");

-- ---------------------------------------------------------------------------
-- Verification and reset tokens
-- ---------------------------------------------------------------------------

CREATE TABLE "auth_tokens" (
  "id"           TEXT PRIMARY KEY,
  "identity_id"  TEXT NOT NULL REFERENCES "auth_identities"("id") ON DELETE CASCADE,
  "kind"         "AuthTokenKind" NOT NULL,
  -- Hashed for the same reason as a refresh token, and more urgently: a reset
  -- token in a dump is a password reset for every account in it.
  "token_hash"   TEXT NOT NULL,
  "expires_at"   TIMESTAMPTZ(6) NOT NULL,
  "created_at"   TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  -- Set the moment it is spent. Single use is enforced here, not by deletion,
  -- so a replay is distinguishable from a token that never existed.
  "used_at"      TIMESTAMPTZ(6),
  "requested_ip" INET
);

CREATE UNIQUE INDEX "auth_tokens_token_hash_key" ON "auth_tokens"("token_hash");
CREATE INDEX "auth_tokens_identity_kind_idx"
  ON "auth_tokens"("identity_id", "kind") WHERE "used_at" IS NULL;

-- ---------------------------------------------------------------------------
-- Rate limits
-- ---------------------------------------------------------------------------

CREATE TABLE "auth_rate_limits" (
  "key"               TEXT NOT NULL,
  "action"            TEXT NOT NULL,
  "window_started_at" TIMESTAMPTZ(6) NOT NULL,
  "count"             INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY ("key", "action")
);

-- Durable on purpose. An in-process counter resets on deploy, and "deploy to
-- clear the lockout" is a rate limiter an attacker can wait out.
CREATE INDEX "auth_rate_limits_window_idx" ON "auth_rate_limits"("window_started_at");

-- ---------------------------------------------------------------------------
-- Audit log
-- ---------------------------------------------------------------------------

CREATE TABLE "auth_audit_log" (
  "id"          TEXT PRIMARY KEY,
  "kind"        TEXT NOT NULL,
  "at"          TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  -- Nullable and *not* a foreign key: a failed login for an address that has
  -- no account is exactly the event worth keeping, and a FK would refuse it.
  "identity_id" TEXT,
  "email"       TEXT,
  "tenant_id"   TEXT,
  "session_id"  TEXT,
  "ip"          INET,
  "user_agent"  TEXT NOT NULL DEFAULT '',
  "succeeded"   BOOLEAN NOT NULL DEFAULT true,
  "detail"      TEXT NOT NULL DEFAULT '',
  "metadata"    JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX "auth_audit_log_identity_idx" ON "auth_audit_log"("identity_id", "at");
CREATE INDEX "auth_audit_log_email_idx" ON "auth_audit_log"("email", "at");
CREATE INDEX "auth_audit_log_kind_idx" ON "auth_audit_log"("kind", "at");

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

-- The request path gets nothing. Migration 002's ALTER DEFAULT PRIVILEGES
-- granted clipforge_app every later table in this schema, which is right for
-- tenant data and wrong for these five. Revoked explicitly, because a default
-- that is silently correct today is a default that silently changes.
REVOKE ALL ON TABLE
  "auth_identities", "auth_memberships", "auth_sessions", "auth_tokens",
  "auth_rate_limits", "auth_audit_log"
  FROM clipforge_app;

DO $$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clipforge_worker') THEN
    REVOKE ALL ON TABLE
      "auth_identities", "auth_memberships", "auth_sessions", "auth_tokens",
      "auth_rate_limits", "auth_audit_log"
      FROM clipforge_worker;
  END IF;
END
$$;

-- The authentication service. The role is created by db/roles.sql, not here:
-- roles are cluster-scoped and clipforge_owner deliberately has no CREATEROLE.
-- If roles.sql has not been re-run since this migration was added, the grant
-- below fails with "role does not exist" — which is the correct failure, in
-- the same way migration 002 fails for the other three.
GRANT USAGE ON SCHEMA public TO clipforge_auth;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
  "auth_identities", "auth_memberships", "auth_sessions", "auth_tokens",
  "auth_rate_limits"
  TO clipforge_auth;

-- Append-only, enforced as a privilege rather than a convention. An audit log
-- the authenticating service can rewrite is an audit log that says whatever
-- the attacker who reached that service wants it to say.
GRANT SELECT, INSERT ON TABLE "auth_audit_log" TO clipforge_auth;
REVOKE UPDATE, DELETE ON TABLE "auth_audit_log" FROM clipforge_auth;

-- ---------------------------------------------------------------------------
-- updated_at
-- ---------------------------------------------------------------------------

CREATE TRIGGER auth_identities_touch
  BEFORE UPDATE ON "auth_identities"
  FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();
