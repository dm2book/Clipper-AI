"""ClipForge AI — authentication.

Email and password, JWT access tokens, rotating refresh tokens, verification
and reset links, rate limiting and an audit trail, on PostgreSQL.

    from clipforge.auth import AuthService, AccessTokenIssuer, MemoryAuthStore

    service = AuthService(store, AccessTokenIssuer(config.keyring))
    service.sign_up("dana@example.com", "a long enough passphrase")
    service.verify_email(token)
    result = service.log_in("dana@example.com", "a long enough passphrase")
    principal = service.authenticate(result.tokens.access_token)

## An identity is not a user

`users` stays tenant-scoped — `unique(tenant_id, email)`, because a global
unique would leak one customer's staff list to another's signup form. But
authentication happens before a tenant is known, so an **identity** is the
human (one global email, one password) and a **user** is a membership (that
human's role in one tenant). One identity, many memberships.

The leak that reasoning worried about is handled where it actually lives:
signup, login and password reset return byte-identical answers whether or not
the address is registered, and spend the same time doing it.

## What this package refuses to do

* **Store a token it could store a hash of.** Refresh, reset and verification
  tokens are all kept as SHA-256. A database dump is not a set of working
  logins.
* **Let the request path read a password hash.** The auth tables are granted to
  `clipforge_auth` and nothing else; `clipforge_app`, which every request uses,
  has no access to them at any tenant setting.
* **Start with an unsafe default.** `AuthConfig.require_production_ready()`
  refuses a generated signing key, a bcrypt cost under 10, unverified sign-in,
  disabled rate limiting and `http://` links.

## What is verified, and what is not

Every flow in this package is exercised against real bcrypt and real PyJWT,
in memory and against PostgreSQL — see `tests/test_auth.py`.

**No email has ever been sent.** `RecordingEmailSender` is the default and
records messages instead of delivering them; `SmtpEmailSender` speaks real SMTP
and no server has accepted a message from it here. Until that is wired up, a
deployment registers users who never receive a verification link.

**There is no HTTP layer.** This is a library: it verifies a token and returns
a `Principal`, and nothing in the repository yet turns an HTTP request into a
call on it.
"""

from __future__ import annotations

from .config import (
    AuthConfig,
    DEFAULT_RATE_LIMITS,
    ENV_PREFIX,
    MisconfiguredAuth,
    config_from_env,
    describe_environment,
)
from .email import (
    ConsoleEmailSender,
    Email,
    EmailDeliveryFailed,
    EmailSender,
    RecordingEmailSender,
    SmtpEmailSender,
)
from .passwords import (
    ALGORITHM as PASSWORD_ALGORITHM,
    MIN_LENGTH,
    PasswordHasher,
    PasswordPolicy,
    WeakPassword,
)
from .service import AuthService, LoginResult, SignUpResult
from .store import AuthStore, DuplicateEmail, MemoryAuthStore, RateLimitBucket
from .tokens import (
    AccessTokenIssuer,
    InvalidToken,
    Keyring,
    SigningKey,
    new_opaque_token,
    new_refresh_token,
)
from .types import (
    AuthError,
    AuthEvent,
    EventKind,
    Identity,
    IdentityStatus,
    Membership,
    Principal,
    RateLimited,
    Session,
    TokenKind,
    TokenPair,
    VerificationToken,
    normalise_email,
)

__all__ = [
    "ALGORITHM",
    "AccessTokenIssuer",
    "AuthConfig",
    "AuthError",
    "AuthEvent",
    "AuthService",
    "AuthStore",
    "ConsoleEmailSender",
    "DEFAULT_RATE_LIMITS",
    "DuplicateEmail",
    "ENV_PREFIX",
    "Email",
    "EmailDeliveryFailed",
    "EmailSender",
    "EventKind",
    "Identity",
    "IdentityStatus",
    "InvalidToken",
    "Keyring",
    "LoginResult",
    "MIN_LENGTH",
    "Membership",
    "MemoryAuthStore",
    "MisconfiguredAuth",
    "PASSWORD_ALGORITHM",
    "PasswordHasher",
    "PasswordPolicy",
    "Principal",
    "RateLimitBucket",
    "RateLimited",
    "RecordingEmailSender",
    "Session",
    "SignUpResult",
    "SigningKey",
    "SmtpEmailSender",
    "TokenKind",
    "TokenPair",
    "VerificationToken",
    "WeakPassword",
    "config_from_env",
    "describe_environment",
    "new_opaque_token",
    "new_refresh_token",
    "normalise_email",
]

#: Re-exported under its own name too, since `PASSWORD_ALGORITHM` reads oddly
#: at a call site that is already inside `clipforge.auth.passwords`.
ALGORITHM = PASSWORD_ALGORITHM
