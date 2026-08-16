"""What an account is, and the vocabulary for everything that happens to one.

## An identity is not a user

The two are deliberately separate, and the split is the most consequential
decision in this package.

`users` is tenant-scoped and always has been: `unique(tenant_id, email)`, with
a comment explaining that a global unique would leak one customer's staff list
to another customer's signup form. That reasoning is sound and this package
does not overturn it.

But *authentication* cannot be tenant-scoped, because at the moment someone
types an email and a password there is no tenant yet. Asking for a workspace
slug before the password is a real cost paid by every user on every login, to
solve a problem that only exists for the rare person who belongs to two
workspaces.

So: an **identity** is the human — one globally unique email, one password, one
verification state. A **user** is a membership — that human's role inside one
tenant. One identity, many memberships, which is also what happens in reality
when an agency operator works across four client workspaces.

The leak the original comment worried about is real and is handled where it
actually lives: every response that could reveal whether an email is registered
is deliberately identical whether it is or not. See `service.py`.

## Errors say what a caller may do, never why

`AuthError` carries a `code` for machines and a `message` for humans, and the
message is written for the *end user*, not the operator. That is why a wrong
password and an unknown email produce the same `INVALID_CREDENTIALS` with the
same text: any difference between them, including a difference in how long the
answer takes, is an account enumeration oracle.

The detail an operator needs goes to the audit log, which is not user-visible.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

__all__ = [
    "AuthError",
    "AuthEvent",
    "Device",
    "EventKind",
    "Identity",
    "IdentityStatus",
    "Membership",
    "MfaFactor",
    "MfaKind",
    "Principal",
    "RateLimited",
    "RecoveryCode",
    "Session",
    "TokenKind",
    "TokenPair",
    "VerificationToken",
    "utcnow",
]


def utcnow() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


class IdentityStatus(str, enum.Enum):
    """Where an account is in its life."""

    #: Registered, email not yet confirmed. May or may not be able to log in,
    #: depending on `AuthConfig.require_verified_email`.
    PENDING = "pending"
    ACTIVE = "active"
    #: Too many failed attempts. Temporary, and lifts by itself.
    LOCKED = "locked"
    #: Deletion requested. Still recoverable until the grace period ends.
    PENDING_DELETION = "pending_deletion"
    #: Purged. The row survives only to keep foreign keys and the audit trail
    #: intact; every personal field on it has been overwritten.
    DELETED = "deleted"

    @property
    def can_log_in(self) -> bool:
        return self in (IdentityStatus.PENDING, IdentityStatus.ACTIVE)


class TokenKind(str, enum.Enum):
    EMAIL_VERIFICATION = "email_verification"
    PASSWORD_RESET = "password_reset"
    #: Issued when a password has been accepted but a second factor is still
    #: owed. Deliberately the same machinery as the other two — hashed at rest,
    #: single use, short expiry — because a challenge that outlives its purpose
    #: or can be spent twice is a password bypass.
    MFA_CHALLENGE = "mfa_challenge"


class MfaKind(str, enum.Enum):
    """The second factors this system can hold.

    Only `TOTP` is implemented. The others are named because the storage,
    the enrolment flow and the login challenge are all factor-agnostic —
    adding WebAuthn means an implementation of `verify`, not a new column and
    not a second login path. That is what "MFA-ready" has to mean to be worth
    saying.
    """

    TOTP = "totp"
    WEBAUTHN = "webauthn"
    RECOVERY = "recovery"


class EventKind(str, enum.Enum):
    """Everything worth being able to answer questions about later.

    The list is chosen by the questions an incident asks — "who got in", "from
    where", "what changed", "when did it start" — rather than by what is easy
    to instrument.
    """

    SIGNUP_STARTED = "signup_started"
    SIGNUP_DUPLICATE = "signup_duplicate"
    EMAIL_VERIFIED = "email_verified"
    VERIFICATION_RESENT = "verification_resent"
    LOGIN_SUCCEEDED = "login_succeeded"
    LOGIN_FAILED = "login_failed"
    LOGIN_BLOCKED = "login_blocked"
    ACCOUNT_LOCKED = "account_locked"
    ACCOUNT_UNLOCKED = "account_unlocked"
    TOKEN_REFRESHED = "token_refreshed"
    TOKEN_REUSE_DETECTED = "token_reuse_detected"
    SESSION_REVOKED = "session_revoked"
    ALL_SESSIONS_REVOKED = "all_sessions_revoked"
    PASSWORD_RESET_REQUESTED = "password_reset_requested"
    PASSWORD_RESET_COMPLETED = "password_reset_completed"
    PASSWORD_CHANGED = "password_changed"
    DELETION_REQUESTED = "deletion_requested"
    DELETION_CANCELLED = "deletion_cancelled"
    DELETION_COMPLETED = "deletion_completed"
    RATE_LIMITED = "rate_limited"
    MFA_ENROLMENT_STARTED = "mfa_enrolment_started"
    MFA_ENABLED = "mfa_enabled"
    MFA_DISABLED = "mfa_disabled"
    MFA_CHALLENGED = "mfa_challenged"
    MFA_SUCCEEDED = "mfa_succeeded"
    MFA_FAILED = "mfa_failed"
    MFA_RECOVERY_USED = "mfa_recovery_used"
    DEVICE_FIRST_SEEN = "device_first_seen"
    DEVICE_REVOKED = "device_revoked"


class AuthError(Exception):
    """Something a caller can act on, phrased for the person who sees it."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        #: The HTTP status this maps to, so a transport layer does not have to
        #: keep a second copy of the same table.
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message}}


class RateLimited(AuthError):
    """Too many attempts. Carries when to come back."""

    def __init__(self, message: str, retry_after_s: float) -> None:
        super().__init__("RATE_LIMITED", message, status=429)
        self.retry_after_s = retry_after_s

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["error"]["retry_after_s"] = round(self.retry_after_s, 1)
        return payload


@dataclass(slots=True)
class Identity:
    """One human's credentials. Never tenant-scoped."""

    identity_id: str
    email: str
    password_hash: str = ""
    #: Which KDF produced `password_hash`. Stored so the hash can be upgraded
    #: on the next successful login when the cost factor or the algorithm
    #: changes, without asking anyone to reset anything.
    password_algo: str = ""
    status: IdentityStatus = IdentityStatus.PENDING
    email_verified_at: datetime | None = None
    #: Consecutive failures. Reset by any success.
    failed_attempts: int = 0
    locked_until: datetime | None = None
    last_login_at: datetime | None = None
    #: When the grace period ends and the purge may run.
    delete_after: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def verified(self) -> bool:
        return self.email_verified_at is not None

    def locked(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return self.locked_until is not None and now < self.locked_until

    def to_dict(self) -> dict[str, Any]:
        """Safe to serialise. The hash is not in it, and never should be."""
        return {
            "identity_id": self.identity_id,
            "email": self.email,
            "status": self.status.value,
            "verified": self.verified,
            "created_at": self.created_at.isoformat(),
            "last_login_at": (
                self.last_login_at.isoformat() if self.last_login_at else None
            ),
        }


@dataclass(slots=True)
class Membership:
    """An identity's place in one tenant. Mirrors a row in `users`."""

    user_id: str
    tenant_id: str
    identity_id: str
    role: str = "viewer"
    active: bool = True
    tenant_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "tenant_name": self.tenant_name,
            "role": self.role,
            "active": self.active,
        }


@dataclass(slots=True)
class Session:
    """One refresh-token family.

    A session is a *family*, not a token: refreshing rotates the token and
    keeps the family. `previous_hash` is what makes theft detectable — see
    `service.refresh`.
    """

    session_id: str
    identity_id: str
    #: SHA-256 of the current refresh token. The token itself is never stored;
    #: a database dump must not be a set of working logins.
    token_hash: str = ""
    #: The hash this rotated away from, kept for exactly one generation so a
    #: replay of the spent token is recognised rather than merely rejected.
    previous_hash: str = ""
    #: Which tenant this session is acting in. A user with four memberships
    #: has four sessions, and an access token is only ever good for one.
    tenant_id: str = ""
    #: The browser or app this session belongs to, when one is identifiable.
    #: Empty for a client that sends no device cookie — an API script, curl —
    #: which is normal and not an error.
    device_id: str = ""
    user_agent: str = ""
    ip: str = ""
    #: True when a second factor was presented while starting this session.
    #: Recorded rather than inferred, because "the account has MFA" and "this
    #: session passed it" are different facts and only the second one should
    #: gate a sensitive action.
    mfa_satisfied: bool = False
    issued_at: datetime = field(default_factory=utcnow)
    #: Extended on each rotation, but never past `absolute_expires_at`.
    expires_at: datetime | None = None
    #: The ceiling. A session that keeps refreshing forever is a stolen token
    #: that works forever.
    absolute_expires_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_reason: str = ""
    rotations: int = 0

    def active(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and now >= self.expires_at:
            return False
        if self.absolute_expires_at is not None and now >= self.absolute_expires_at:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "ip": self.ip,
            "user_agent": self.user_agent[:120],
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "revoked": self.revoked_at is not None,
            "rotations": self.rotations,
        }


@dataclass(slots=True)
class VerificationToken:
    """An email-verification or password-reset token, as stored.

    Stored hashed and single-use. A reset token in a database dump is a
    password reset for every account in it, and the mitigation costs one call
    to `sha256`.
    """

    token_id: str
    identity_id: str
    kind: TokenKind
    token_hash: str
    expires_at: datetime
    created_at: datetime = field(default_factory=utcnow)
    used_at: datetime | None = None
    #: Where the request came from, for the audit trail.
    requested_ip: str = ""

    def usable(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return self.used_at is None and now < self.expires_at


@dataclass(slots=True)
class MfaFactor:
    """One second factor belonging to an identity.

    `secret` holds the base32 TOTP seed. It is stored recoverable, unlike a
    password, and there is no way around that: verifying a TOTP code requires
    recomputing the HMAC, so the server needs the seed itself. What that means
    in practice is worth being blunt about — **a database dump is a set of
    working authenticator apps**. The mitigations are the ones that apply to
    any recoverable secret: encrypt the column at rest, restrict the role that
    can read it, and treat a dump as a full MFA reset. `auth_mfa_factors` is
    readable only by `clipforge_auth` for that reason.

    `last_counter` is what makes replay refusable — see `totp.verify_totp`.
    """

    factor_id: str
    identity_id: str
    kind: MfaKind = MfaKind.TOTP
    #: What the user calls it. "iPhone", "1Password", "the old phone".
    label: str = ""
    secret: str = ""
    created_at: datetime = field(default_factory=utcnow)
    #: Set when the user has proved they can produce a code from it. An
    #: unconfirmed factor never gates a login: enrolling and then failing to
    #: scan the QR code must not lock somebody out of their own account.
    confirmed_at: datetime | None = None
    last_used_at: datetime | None = None
    #: The highest TOTP step already spent, so a captured code cannot be
    #: replayed inside its validity window.
    last_counter: int | None = None

    @property
    def active(self) -> bool:
        return self.confirmed_at is not None

    def to_dict(self) -> dict[str, Any]:
        """Never includes the secret. That is the whole point of the method."""
        return {
            "factor_id": self.factor_id,
            "kind": self.kind.value,
            "label": self.label,
            "created_at": self.created_at.isoformat(),
            "confirmed_at": (
                self.confirmed_at.isoformat() if self.confirmed_at else None
            ),
            "last_used_at": (
                self.last_used_at.isoformat() if self.last_used_at else None
            ),
            "active": self.active,
        }


@dataclass(slots=True)
class RecoveryCode:
    """A single-use way back in when the phone is gone.

    Stored as a SHA-256 hash, like a refresh token and for the same reason: a
    dump of this table must not be a set of working logins. Not Argon2 —
    these are high-entropy values from `secrets`, so there is nothing to
    guess and a slow hash would only add latency.
    """

    code_id: str
    identity_id: str
    code_hash: str
    created_at: datetime = field(default_factory=utcnow)
    used_at: datetime | None = None
    used_ip: str = ""

    @property
    def spent(self) -> bool:
        return self.used_at is not None


@dataclass(slots=True)
class Device:
    """A browser or app that has signed in, across sessions.

    Identity comes from a long-lived random cookie, not from a fingerprint of
    the user agent and IP. Fingerprinting is unreliable — two identical
    laptops on one office IP are one device — and it is a tracking technique
    applied to people who did not ask for it. The cost of the honest approach
    is that clearing cookies looks like a new device, which is a support
    question rather than a security hole.
    """

    device_id: str
    identity_id: str
    #: A readable summary of the user agent: "Chrome on macOS".
    label: str = ""
    user_agent: str = ""
    last_ip: str = ""
    first_seen_at: datetime = field(default_factory=utcnow)
    last_seen_at: datetime = field(default_factory=utcnow)
    revoked_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "label": self.label,
            "user_agent": self.user_agent,
            "last_ip": self.last_ip,
            "first_seen_at": self.first_seen_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat(),
            "active": self.active,
        }


@dataclass(slots=True)
class TokenPair:
    """What a caller gets on login and on refresh."""

    access_token: str
    refresh_token: str
    #: Seconds until the access token expires. Clients should refresh before
    #: this, not after a 401.
    expires_in_s: int
    session_id: str
    tenant_id: str = ""
    token_type: str = "Bearer"

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in_s,
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
        }


@dataclass(slots=True)
class Principal:
    """A verified access token, as the request path sees it.

    This is what the rest of the system should accept in place of a bare
    `user_id`. `empire.Directory.require()` takes a user id and trusts the
    caller to have established it; a `Principal` is that establishment.
    """

    identity_id: str
    email: str
    tenant_id: str
    user_id: str
    role: str
    session_id: str
    issued_at: datetime
    expires_at: datetime
    #: The token's unique id, so a specific token can be denied.
    jti: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity_id": self.identity_id,
            "email": self.email,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "role": self.role,
            "session_id": self.session_id,
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass(slots=True)
class AuthEvent:
    """One line in the audit log. Append-only, and never holds a secret."""

    event_id: str
    kind: EventKind
    at: datetime = field(default_factory=utcnow)
    identity_id: str = ""
    #: Lowercased email, kept even when no identity matched — an attacker
    #: guessing addresses is a pattern worth being able to see.
    email: str = ""
    tenant_id: str = ""
    session_id: str = ""
    ip: str = ""
    user_agent: str = ""
    succeeded: bool = True
    #: Operator-facing detail. This is where the real reason goes when the
    #: user-facing message is deliberately vague.
    detail: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "at": self.at.isoformat(),
            "identity_id": self.identity_id,
            "email": self.email,
            "tenant_id": self.tenant_id,
            "ip": self.ip,
            "succeeded": self.succeeded,
            "detail": self.detail,
            "metadata": self.metadata,
        }


def normalise_email(raw: str) -> str:
    """Lowercased and trimmed, and nothing cleverer than that.

    Deliberately *not* stripping dots or `+tags`: those rules are Gmail's, not
    the internet's, and applying them globally makes `first.last@company.com`
    and `firstlast@company.com` the same account at a company where they are
    two different people.
    """

    return raw.strip().lower()
