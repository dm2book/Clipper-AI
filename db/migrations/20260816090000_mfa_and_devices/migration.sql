-- Second factors, recovery codes and device tracking.
--
-- Follows migration 006's conventions exactly, and for the same reasons: no
-- row-level security (authentication happens before a tenant is known, so a
-- policy here could only be `USING (true)`), no grant to `clipforge_app`, and
-- everything reachable only by `clipforge_auth`.
--
-- One new consideration of its own. `auth_mfa_factors.secret` is a **shared
-- secret stored recoverably**, unlike a password hash — verifying a TOTP code
-- means recomputing the HMAC, so the server needs the seed itself. A dump of
-- this table is therefore a set of working authenticator apps. That cannot be
-- designed away while the factor is TOTP; what it can be is contained, which
-- is why the table is readable by exactly one narrow role and why a dump
-- should be treated as a reason to reset every factor.
--
-- KNOWN DIVERGENCE, inherited from migration 006 and extended here.
-- `db/README.md` calls `schema.prisma` the source of truth, and none of the
-- `auth_*` tables appear in it: 006 created them in raw SQL, and these three
-- follow that precedent for consistency rather than modelling half the auth
-- schema in Prisma and half in SQL. The consequence is real and worth stating
-- — `prisma migrate dev` sees these tables as drift and will offer to drop
-- them. Until the auth schema is modelled in Prisma, migrations touching
-- `auth_*` are written by hand and applied with psql, and a Prisma-generated
-- migration mentioning any of these table names must not be accepted.

-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------

-- A third token kind. Added to the existing enum rather than given its own
-- table so a challenge inherits the hashing, expiry and single-use handling
-- that `auth_tokens` already enforces.
ALTER TYPE "AuthTokenKind" ADD VALUE IF NOT EXISTS 'mfa_challenge';

CREATE TYPE "MfaKind" AS ENUM ('totp', 'webauthn', 'recovery');

-- ---------------------------------------------------------------------------
-- Factors
-- ---------------------------------------------------------------------------

CREATE TABLE "auth_mfa_factors" (
  "id"            TEXT PRIMARY KEY,
  "identity_id"   TEXT NOT NULL
                  REFERENCES "auth_identities"("id") ON DELETE CASCADE,
  "kind"          "MfaKind" NOT NULL DEFAULT 'totp',
  "label"         TEXT NOT NULL DEFAULT '',
  -- Base32 TOTP seed. See the header.
  "secret"        TEXT NOT NULL DEFAULT '',
  "created_at"    TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  -- NULL until the user has produced a working code. An unconfirmed factor
  -- never gates a login, so a half-finished enrolment cannot lock anyone out.
  "confirmed_at"  TIMESTAMPTZ(6),
  "last_used_at"  TIMESTAMPTZ(6),
  -- Highest TOTP step already spent. This is what makes a captured code
  -- unusable inside its own validity window.
  "last_counter"  BIGINT
);

CREATE INDEX "auth_mfa_factors_identity_idx"
  ON "auth_mfa_factors" ("identity_id");

-- Partial: only confirmed factors are interesting to the login path, and it
-- reads them on every sign-in for an account that has any.
CREATE INDEX "auth_mfa_factors_active_idx"
  ON "auth_mfa_factors" ("identity_id")
  WHERE "confirmed_at" IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Recovery codes
-- ---------------------------------------------------------------------------

CREATE TABLE "auth_recovery_codes" (
  "id"           TEXT PRIMARY KEY,
  "identity_id"  TEXT NOT NULL
                 REFERENCES "auth_identities"("id") ON DELETE CASCADE,
  -- SHA-256, like a refresh token. Not Argon2: these are high-entropy values
  -- from `secrets`, so a slow hash would buy latency and no security.
  "code_hash"    TEXT NOT NULL,
  "created_at"   TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "used_at"      TIMESTAMPTZ(6),
  "used_ip"      INET
);

CREATE INDEX "auth_recovery_codes_identity_idx"
  ON "auth_recovery_codes" ("identity_id");

-- A given code exists once across the whole system. Two identities holding
-- the same hash would mean one printout opened two accounts.
CREATE UNIQUE INDEX "auth_recovery_codes_hash_key"
  ON "auth_recovery_codes" ("code_hash");

-- ---------------------------------------------------------------------------
-- Devices
-- ---------------------------------------------------------------------------

CREATE TABLE "auth_devices" (
  "id"             TEXT PRIMARY KEY,
  "identity_id"    TEXT NOT NULL
                   REFERENCES "auth_identities"("id") ON DELETE CASCADE,
  "label"          TEXT NOT NULL DEFAULT '',
  "user_agent"     TEXT NOT NULL DEFAULT '',
  "last_ip"        INET,
  -- Never updated after insert. It is the fact a "new device" alert is judged
  -- against, so refreshing it would silence every future alert for it.
  "first_seen_at"  TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "last_seen_at"   TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "revoked_at"     TIMESTAMPTZ(6)
);

CREATE INDEX "auth_devices_identity_idx"
  ON "auth_devices" ("identity_id", "last_seen_at" DESC);

-- ---------------------------------------------------------------------------
-- Sessions gain a device and an MFA flag
-- ---------------------------------------------------------------------------

-- No foreign key to auth_devices. A session from a client that sends no
-- device cookie — curl, a mobile app, a CI job — legitimately has none, and
-- an FK would force either a NULL that means two different things or a fake
-- device row for every API script.
ALTER TABLE "auth_sessions"
  ADD COLUMN IF NOT EXISTS "device_id" TEXT;

-- "The account has MFA" and "this session passed it" are different facts, and
-- only the second should gate a sensitive action. A session started before a
-- factor was added has this false, correctly.
ALTER TABLE "auth_sessions"
  ADD COLUMN IF NOT EXISTS "mfa_satisfied" BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS "auth_sessions_device_idx"
  ON "auth_sessions" ("device_id")
  WHERE "device_id" IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

-- Same shape as migration 006: the application role gets nothing, and only
-- the authentication service can reach these rows. `clipforge_app` is not
-- mentioned because migration 002's default privileges were already revoked
-- for this schema's auth tables; these inherit that.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
  "auth_mfa_factors", "auth_recovery_codes", "auth_devices"
  TO clipforge_auth;

REVOKE ALL ON TABLE
  "auth_mfa_factors", "auth_recovery_codes", "auth_devices"
  FROM clipforge_app;
