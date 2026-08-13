"""The auth store, on PostgreSQL, as its own role.

Connects as `clipforge_auth`, which is granted the five `auth_*` tables and
nothing else. `clipforge_app` — the role the whole request path uses — is
granted nothing here at all, so injection anywhere in the application reaches
clips and captions and cannot reach a password hash.

There are no row-level security policies on these tables, and that is not an
omission. RLS scopes rows to `app.current_tenant()`, and authentication happens
before a tenant exists. The boundary is the grant instead, which is stronger:
the app role cannot see the rows at any tenant setting.

## Rate limiting is an upsert, not a read-then-write

`hit_rate_limit` is one statement. The obvious two-statement version — read the
counter, add one, write it back — loses increments under exactly the load a
rate limiter exists for, and the limiter fails open at the moment it matters.
The window roll is in the same statement for the same reason.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .store import DuplicateEmail
from .types import (
    AuthEvent,
    EventKind,
    Identity,
    IdentityStatus,
    Membership,
    Session,
    TokenKind,
    VerificationToken,
    normalise_email,
)

__all__ = ["PostgresAuthStore"]


class PostgresAuthStore:
    """`AuthStore` over psycopg. One connection pool, one role."""

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 8) -> None:
        try:
            import psycopg
        except ImportError as error:                        # pragma: no cover
            raise RuntimeError(
                "psycopg is required for the Postgres auth store — "
                "`pip install 'clipforge[db]'`"
            ) from error
        self._psycopg = psycopg
        self._dsn = dsn
        self._pool = None
        try:
            from psycopg_pool import ConnectionPool
        except ImportError:
            ConnectionPool = None                           # noqa: N806
        if ConnectionPool is not None:
            self._pool = ConnectionPool(
                dsn, min_size=min_size, max_size=max_size, open=True,
            )
            self._pool.wait(timeout=10)

    # -- plumbing ----------------------------------------------------------

    def _connect(self):
        if self._pool is not None:
            return self._pool.connection()
        return self._psycopg.connect(self._dsn)

    def _one(self, sql: str, params: tuple = ()) -> tuple | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.fetchone()

    def _all(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.fetchall()

    def _run(self, sql: str, params: tuple = ()) -> int:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.rowcount

    # -- identities --------------------------------------------------------

    _IDENTITY_COLUMNS = (
        "id, email, password_hash, password_algo, status, email_verified_at, "
        "failed_attempts, locked_until, last_login_at, delete_after, "
        "created_at, updated_at"
    )

    def create_identity(self, identity: Identity) -> Identity:
        identity.email = normalise_email(identity.email)
        try:
            self._run(
                f"INSERT INTO auth_identities ({self._IDENTITY_COLUMNS}) "
                f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    identity.identity_id, identity.email, identity.password_hash,
                    identity.password_algo, identity.status.value,
                    identity.email_verified_at, identity.failed_attempts,
                    identity.locked_until, identity.last_login_at,
                    identity.delete_after, identity.created_at,
                    identity.updated_at,
                ),
            )
        except self._psycopg.errors.UniqueViolation as error:
            raise DuplicateEmail(identity.email) from error
        return identity

    def identity(self, identity_id: str) -> Identity | None:
        row = self._one(
            f"SELECT {self._IDENTITY_COLUMNS} FROM auth_identities WHERE id = %s",
            (identity_id,),
        )
        return _identity(row) if row else None

    def identity_by_email(self, email: str) -> Identity | None:
        row = self._one(
            f"SELECT {self._IDENTITY_COLUMNS} FROM auth_identities "
            f"WHERE email = %s",
            (normalise_email(email),),
        )
        return _identity(row) if row else None

    def save_identity(self, identity: Identity) -> Identity:
        identity.updated_at = datetime.now(UTC)
        self._run(
            "UPDATE auth_identities SET email=%s, password_hash=%s, "
            "password_algo=%s, status=%s, email_verified_at=%s, "
            "failed_attempts=%s, locked_until=%s, last_login_at=%s, "
            "delete_after=%s, updated_at=%s WHERE id=%s",
            (
                normalise_email(identity.email), identity.password_hash,
                identity.password_algo, identity.status.value,
                identity.email_verified_at, identity.failed_attempts,
                identity.locked_until, identity.last_login_at,
                identity.delete_after, identity.updated_at,
                identity.identity_id,
            ),
        )
        return identity

    def identities_due_for_deletion(self, now: datetime) -> tuple[Identity, ...]:
        rows = self._all(
            f"SELECT {self._IDENTITY_COLUMNS} FROM auth_identities "
            f"WHERE status = %s AND delete_after IS NOT NULL "
            f"AND delete_after <= %s",
            (IdentityStatus.PENDING_DELETION.value, now),
        )
        return tuple(_identity(row) for row in rows)

    # -- memberships -------------------------------------------------------

    def memberships(self, identity_id: str) -> tuple[Membership, ...]:
        rows = self._all(
            "SELECT user_id, tenant_id, identity_id, role, active, tenant_name "
            "FROM auth_memberships WHERE identity_id = %s ORDER BY tenant_id",
            (identity_id,),
        )
        return tuple(
            Membership(user_id=r[0], tenant_id=r[1], identity_id=r[2],
                       role=r[3], active=r[4], tenant_name=r[5] or "")
            for r in rows
        )

    def add_membership(self, membership: Membership) -> Membership:
        self._run(
            "INSERT INTO auth_memberships "
            "(user_id, tenant_id, identity_id, role, active, tenant_name) "
            "VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "tenant_id=EXCLUDED.tenant_id, identity_id=EXCLUDED.identity_id, "
            "role=EXCLUDED.role, active=EXCLUDED.active, "
            "tenant_name=EXCLUDED.tenant_name",
            (membership.user_id, membership.tenant_id, membership.identity_id,
             membership.role, membership.active, membership.tenant_name),
        )
        return membership

    def remove_memberships(self, identity_id: str) -> int:
        return self._run(
            "DELETE FROM auth_memberships WHERE identity_id = %s", (identity_id,)
        )

    # -- sessions ----------------------------------------------------------

    _SESSION_COLUMNS = (
        "id, identity_id, token_hash, previous_hash, tenant_id, user_agent, "
        "ip, issued_at, expires_at, absolute_expires_at, revoked_at, "
        "revoked_reason, rotations"
    )

    def create_session(self, session: Session) -> Session:
        self._run(
            f"INSERT INTO auth_sessions ({self._SESSION_COLUMNS}) "
            f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                session.session_id, session.identity_id, session.token_hash,
                session.previous_hash or None, session.tenant_id or None,
                session.user_agent, session.ip or None, session.issued_at,
                session.expires_at, session.absolute_expires_at,
                session.revoked_at, session.revoked_reason, session.rotations,
            ),
        )
        return session

    def session(self, session_id: str) -> Session | None:
        row = self._one(
            f"SELECT {self._SESSION_COLUMNS} FROM auth_sessions WHERE id = %s",
            (session_id,),
        )
        return _session(row) if row else None

    def session_by_token_hash(self, token_hash: str) -> Session | None:
        if not token_hash:
            return None
        # `previous_hash` is matched deliberately: a replayed token that has
        # already been rotated away from must be found, so the service can
        # recognise it as reuse rather than as an unknown token.
        row = self._one(
            f"SELECT {self._SESSION_COLUMNS} FROM auth_sessions "
            f"WHERE token_hash = %s OR previous_hash = %s "
            f"ORDER BY (token_hash = %s) DESC LIMIT 1",
            (token_hash, token_hash, token_hash),
        )
        return _session(row) if row else None

    def save_session(self, session: Session) -> Session:
        self._run(
            "UPDATE auth_sessions SET token_hash=%s, previous_hash=%s, "
            "tenant_id=%s, expires_at=%s, revoked_at=%s, revoked_reason=%s, "
            "rotations=%s WHERE id=%s",
            (session.token_hash, session.previous_hash or None,
             session.tenant_id or None, session.expires_at, session.revoked_at,
             session.revoked_reason, session.rotations, session.session_id),
        )
        return session

    def sessions_for(self, identity_id: str) -> tuple[Session, ...]:
        rows = self._all(
            f"SELECT {self._SESSION_COLUMNS} FROM auth_sessions "
            f"WHERE identity_id = %s ORDER BY issued_at DESC",
            (identity_id,),
        )
        return tuple(_session(row) for row in rows)

    def revoke_sessions(self, identity_id: str, now: datetime, reason: str) -> int:
        return self._run(
            "UPDATE auth_sessions SET revoked_at=%s, revoked_reason=%s "
            "WHERE identity_id=%s AND revoked_at IS NULL",
            (now, reason, identity_id),
        )

    # -- tokens ------------------------------------------------------------

    _TOKEN_COLUMNS = (
        "id, identity_id, kind, token_hash, expires_at, created_at, used_at, "
        "requested_ip"
    )

    def create_token(self, token: VerificationToken) -> VerificationToken:
        self._run(
            f"INSERT INTO auth_tokens ({self._TOKEN_COLUMNS}) "
            f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (token.token_id, token.identity_id, token.kind.value,
             token.token_hash, token.expires_at, token.created_at,
             token.used_at, token.requested_ip or None),
        )
        return token

    def token_by_hash(self, token_hash: str) -> VerificationToken | None:
        row = self._one(
            f"SELECT {self._TOKEN_COLUMNS} FROM auth_tokens WHERE token_hash = %s",
            (token_hash,),
        )
        return _token(row) if row else None

    def save_token(self, token: VerificationToken) -> VerificationToken:
        self._run(
            "UPDATE auth_tokens SET used_at=%s WHERE id=%s",
            (token.used_at, token.token_id),
        )
        return token

    def invalidate_tokens(
        self, identity_id: str, kind: TokenKind, now: datetime
    ) -> int:
        return self._run(
            "UPDATE auth_tokens SET used_at=%s "
            "WHERE identity_id=%s AND kind=%s AND used_at IS NULL",
            (now, identity_id, kind.value),
        )

    # -- rate limiting -----------------------------------------------------

    def hit_rate_limit(
        self, key: str, action: str, window_s: int, now: datetime
    ) -> int:
        """Count this attempt atomically and return the window's total.

        One statement, on purpose. Read-then-write loses increments under
        concurrency, which means the limiter leaks exactly when it is being
        tested by the traffic it exists to stop.
        """

        row = self._one(
            "INSERT INTO auth_rate_limits (key, action, window_started_at, count) "
            "VALUES (%s, %s, %s, 1) "
            "ON CONFLICT (key, action) DO UPDATE SET "
            "  count = CASE "
            "    WHEN auth_rate_limits.window_started_at <= %s THEN 1 "
            "    ELSE auth_rate_limits.count + 1 END, "
            "  window_started_at = CASE "
            "    WHEN auth_rate_limits.window_started_at <= %s THEN EXCLUDED.window_started_at "
            "    ELSE auth_rate_limits.window_started_at END "
            "RETURNING count",
            (key, action, now, now - timedelta(seconds=window_s),
             now - timedelta(seconds=window_s)),
        )
        return int(row[0]) if row else 1

    def reset_rate_limit(self, key: str, action: str) -> None:
        self._run(
            "DELETE FROM auth_rate_limits WHERE key = %s AND action = %s",
            (key, action),
        )

    # -- audit -------------------------------------------------------------

    def record_event(self, event: AuthEvent) -> AuthEvent:
        from json import dumps

        self._run(
            "INSERT INTO auth_audit_log (id, kind, at, identity_id, email, "
            "tenant_id, session_id, ip, user_agent, succeeded, detail, metadata) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
            (event.event_id, event.kind.value, event.at,
             event.identity_id or None, event.email or None,
             event.tenant_id or None, event.session_id or None,
             event.ip or None, event.user_agent, event.succeeded,
             event.detail, dumps(event.metadata or {})),
        )
        return event

    def events_for(
        self, identity_id: str = "", limit: int = 100
    ) -> tuple[AuthEvent, ...]:
        if identity_id:
            rows = self._all(
                "SELECT id, kind, at, identity_id, email, tenant_id, "
                "session_id, ip, user_agent, succeeded, detail, metadata "
                "FROM auth_audit_log WHERE identity_id = %s "
                "ORDER BY at, id LIMIT %s",
                (identity_id, limit),
            )
        else:
            rows = self._all(
                "SELECT id, kind, at, identity_id, email, tenant_id, "
                "session_id, ip, user_agent, succeeded, detail, metadata "
                "FROM auth_audit_log ORDER BY at, id LIMIT %s",
                (limit,),
            )
        return tuple(_event(row) for row in rows)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()


# ---------------------------------------------------------------------------
# Row mapping
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    """A column that the records declare as `str`, whatever psycopg returns.

    `INET` comes back as an `ipaddress.IPv4Address`, which compares unequal to
    the string that was written and silently changes the type crossing the
    store boundary. The in-memory store returns a `str`, so without this the
    two implementations disagree — which is the whole reason one suite runs
    against both.
    """

    return "" if value is None else str(value)


def _identity(row: tuple) -> Identity:
    return Identity(
        identity_id=row[0], email=row[1], password_hash=row[2] or "",
        password_algo=row[3] or "", status=IdentityStatus(row[4]),
        email_verified_at=row[5], failed_attempts=row[6] or 0,
        locked_until=row[7], last_login_at=row[8], delete_after=row[9],
        created_at=row[10], updated_at=row[11],
    )


def _session(row: tuple) -> Session:
    return Session(
        session_id=row[0], identity_id=row[1], token_hash=row[2] or "",
        previous_hash=row[3] or "", tenant_id=row[4] or "",
        user_agent=row[5] or "", ip=_text(row[6]), issued_at=row[7],
        expires_at=row[8], absolute_expires_at=row[9], revoked_at=row[10],
        revoked_reason=row[11] or "", rotations=row[12] or 0,
    )


def _token(row: tuple) -> VerificationToken:
    return VerificationToken(
        token_id=row[0], identity_id=row[1], kind=TokenKind(row[2]),
        token_hash=row[3], expires_at=row[4], created_at=row[5],
        used_at=row[6], requested_ip=_text(row[7]),
    )


def _event(row: tuple) -> AuthEvent:
    return AuthEvent(
        event_id=row[0], kind=EventKind(row[1]), at=row[2],
        identity_id=row[3] or "", email=row[4] or "", tenant_id=row[5] or "",
        session_id=row[6] or "", ip=_text(row[7]), user_agent=row[8] or "",
        succeeded=row[9], detail=row[10] or "", metadata=row[11] or {},
    )
