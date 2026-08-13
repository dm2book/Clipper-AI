"""Where credentials live, and why it is not the tenant store.

`clipforge.store` is reached through `clipforge_app`, a role that connects
under row-level security with a tenant already chosen. Authentication happens
*before* a tenant is known, so none of that applies — and more importantly,
none of it should.

Password hashes, refresh tokens and reset tokens are kept on a separate
connection as a separate role, `clipforge_auth`, and `clipforge_app` is granted
nothing on those tables at all. The consequence is worth the duplication: a SQL
injection anywhere in the request path reaches clips, captions and metrics, and
cannot reach a single password hash. Sharing a `UnitOfWork` would give that up
for the convenience of one less connection.

The protocol below is implemented twice — in memory here, against Postgres in
`postgres.py` — and `tests/test_auth.py` runs one suite against both, for the
same reason `test_store_contract.py` does: the fast in-memory tests are only
evidence about production if the same assertions pass on the database.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from .types import (
    AuthEvent,
    Identity,
    IdentityStatus,
    Membership,
    Session,
    TokenKind,
    VerificationToken,
    normalise_email,
    utcnow,
)

__all__ = [
    "AuthStore",
    "MemoryAuthStore",
    "RateLimitBucket",
    "DuplicateEmail",
]


class DuplicateEmail(Exception):
    """The email is already registered.

    Never surfaced to a caller of `service.sign_up` — see the enumeration note
    there — but the store must still refuse, or two identities end up sharing
    an address and login becomes ambiguous.
    """


@dataclass(slots=True)
class RateLimitBucket:
    key: str
    action: str
    window_started_at: datetime
    count: int = 0


class AuthStore(Protocol):
    """Everything the service needs to persist. Deliberately narrow."""

    # -- identities --------------------------------------------------------
    def create_identity(self, identity: Identity) -> Identity: ...
    def identity(self, identity_id: str) -> Identity | None: ...
    def identity_by_email(self, email: str) -> Identity | None: ...
    def save_identity(self, identity: Identity) -> Identity: ...
    def identities_due_for_deletion(self, now: datetime) -> tuple[Identity, ...]: ...

    # -- memberships -------------------------------------------------------
    def memberships(self, identity_id: str) -> tuple[Membership, ...]: ...
    def add_membership(self, membership: Membership) -> Membership: ...
    def remove_memberships(self, identity_id: str) -> int: ...

    # -- sessions ----------------------------------------------------------
    def create_session(self, session: Session) -> Session: ...
    def session(self, session_id: str) -> Session | None: ...
    def session_by_token_hash(self, token_hash: str) -> Session | None: ...
    def save_session(self, session: Session) -> Session: ...
    def sessions_for(self, identity_id: str) -> tuple[Session, ...]: ...
    def revoke_sessions(
        self, identity_id: str, now: datetime, reason: str
    ) -> int: ...

    # -- verification and reset tokens -------------------------------------
    def create_token(self, token: VerificationToken) -> VerificationToken: ...
    def token_by_hash(self, token_hash: str) -> VerificationToken | None: ...
    def save_token(self, token: VerificationToken) -> VerificationToken: ...
    def invalidate_tokens(
        self, identity_id: str, kind: TokenKind, now: datetime
    ) -> int: ...

    # -- rate limiting -----------------------------------------------------
    def hit_rate_limit(
        self, key: str, action: str, window_s: int, now: datetime
    ) -> int: ...
    def reset_rate_limit(self, key: str, action: str) -> None: ...

    # -- audit -------------------------------------------------------------
    def record_event(self, event: AuthEvent) -> AuthEvent: ...
    def events_for(
        self, identity_id: str = "", limit: int = 100
    ) -> tuple[AuthEvent, ...]: ...

    def close(self) -> None: ...


class MemoryAuthStore:
    """The reference implementation. Not for production — nothing survives.

    Locked rather than lock-free: the service's rate limiter and its refresh
    rotation both read-then-write, and the tests exercise them from several
    threads on purpose. An in-memory double that is not thread safe turns a
    real concurrency bug into a flaky test that gets retried.
    """

    def __init__(self) -> None:
        self._identities: dict[str, Identity] = {}
        self._by_email: dict[str, str] = {}
        self._memberships: dict[str, list[Membership]] = {}
        self._sessions: dict[str, Session] = {}
        self._tokens: dict[str, VerificationToken] = {}
        self._buckets: dict[tuple[str, str], RateLimitBucket] = {}
        self._events: list[AuthEvent] = []
        self._lock = threading.RLock()

    # -- identities --------------------------------------------------------

    def create_identity(self, identity: Identity) -> Identity:
        with self._lock:
            email = normalise_email(identity.email)
            if email in self._by_email:
                raise DuplicateEmail(email)
            identity.email = email
            self._identities[identity.identity_id] = copy.deepcopy(identity)
            self._by_email[email] = identity.identity_id
            return copy.deepcopy(identity)

    def identity(self, identity_id: str) -> Identity | None:
        with self._lock:
            found = self._identities.get(identity_id)
            return copy.deepcopy(found) if found else None

    def identity_by_email(self, email: str) -> Identity | None:
        with self._lock:
            identity_id = self._by_email.get(normalise_email(email))
            if identity_id is None:
                return None
            return copy.deepcopy(self._identities[identity_id])

    def save_identity(self, identity: Identity) -> Identity:
        with self._lock:
            identity.updated_at = utcnow()
            existing = self._identities.get(identity.identity_id)
            if existing is not None and existing.email != identity.email:
                self._by_email.pop(existing.email, None)
                self._by_email[identity.email] = identity.identity_id
            self._identities[identity.identity_id] = copy.deepcopy(identity)
            return copy.deepcopy(identity)

    def identities_due_for_deletion(self, now: datetime) -> tuple[Identity, ...]:
        with self._lock:
            return tuple(
                copy.deepcopy(i) for i in self._identities.values()
                if i.status is IdentityStatus.PENDING_DELETION
                and i.delete_after is not None and now >= i.delete_after
            )

    # -- memberships -------------------------------------------------------

    def memberships(self, identity_id: str) -> tuple[Membership, ...]:
        with self._lock:
            return tuple(
                copy.deepcopy(m) for m in self._memberships.get(identity_id, [])
            )

    def add_membership(self, membership: Membership) -> Membership:
        with self._lock:
            held = self._memberships.setdefault(membership.identity_id, [])
            held[:] = [m for m in held if m.user_id != membership.user_id]
            held.append(copy.deepcopy(membership))
            return copy.deepcopy(membership)

    def remove_memberships(self, identity_id: str) -> int:
        with self._lock:
            return len(self._memberships.pop(identity_id, []))

    # -- sessions ----------------------------------------------------------

    def create_session(self, session: Session) -> Session:
        with self._lock:
            self._sessions[session.session_id] = copy.deepcopy(session)
            return copy.deepcopy(session)

    def session(self, session_id: str) -> Session | None:
        with self._lock:
            found = self._sessions.get(session_id)
            return copy.deepcopy(found) if found else None

    def session_by_token_hash(self, token_hash: str) -> Session | None:
        with self._lock:
            for held in self._sessions.values():
                # `previous_hash` is matched too, and that is the point: a
                # replay of an already-rotated token must be recognised as
                # theft rather than dismissed as unknown.
                if token_hash in (held.token_hash, held.previous_hash):
                    if token_hash:
                        return copy.deepcopy(held)
            return None

    def save_session(self, session: Session) -> Session:
        with self._lock:
            self._sessions[session.session_id] = copy.deepcopy(session)
            return copy.deepcopy(session)

    def sessions_for(self, identity_id: str) -> tuple[Session, ...]:
        with self._lock:
            return tuple(
                copy.deepcopy(s) for s in self._sessions.values()
                if s.identity_id == identity_id
            )

    def revoke_sessions(self, identity_id: str, now: datetime, reason: str) -> int:
        with self._lock:
            count = 0
            for held in self._sessions.values():
                if held.identity_id == identity_id and held.revoked_at is None:
                    held.revoked_at = now
                    held.revoked_reason = reason
                    count += 1
            return count

    # -- tokens ------------------------------------------------------------

    def create_token(self, token: VerificationToken) -> VerificationToken:
        with self._lock:
            self._tokens[token.token_id] = copy.deepcopy(token)
            return copy.deepcopy(token)

    def token_by_hash(self, token_hash: str) -> VerificationToken | None:
        with self._lock:
            for held in self._tokens.values():
                if held.token_hash == token_hash:
                    return copy.deepcopy(held)
            return None

    def save_token(self, token: VerificationToken) -> VerificationToken:
        with self._lock:
            self._tokens[token.token_id] = copy.deepcopy(token)
            return copy.deepcopy(token)

    def invalidate_tokens(
        self, identity_id: str, kind: TokenKind, now: datetime
    ) -> int:
        with self._lock:
            count = 0
            for held in self._tokens.values():
                if (held.identity_id == identity_id and held.kind is kind
                        and held.used_at is None):
                    held.used_at = now
                    count += 1
            return count

    # -- rate limiting -----------------------------------------------------

    def hit_rate_limit(
        self, key: str, action: str, window_s: int, now: datetime
    ) -> int:
        """Count this attempt and return the running total for the window."""

        with self._lock:
            bucket = self._buckets.get((key, action))
            if bucket is None or now - bucket.window_started_at >= timedelta(
                seconds=window_s
            ):
                bucket = RateLimitBucket(key, action, now, 0)
                self._buckets[(key, action)] = bucket
            bucket.count += 1
            return bucket.count

    def reset_rate_limit(self, key: str, action: str) -> None:
        with self._lock:
            self._buckets.pop((key, action), None)

    # -- audit -------------------------------------------------------------

    def record_event(self, event: AuthEvent) -> AuthEvent:
        with self._lock:
            self._events.append(copy.deepcopy(event))
            return copy.deepcopy(event)

    def events_for(
        self, identity_id: str = "", limit: int = 100
    ) -> tuple[AuthEvent, ...]:
        with self._lock:
            found = [
                e for e in self._events
                if not identity_id or e.identity_id == identity_id
            ]
            return tuple(copy.deepcopy(e) for e in found[-limit:])

    def close(self) -> None:
        return None
