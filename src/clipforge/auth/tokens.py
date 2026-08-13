"""Access tokens that are JWTs, and refresh tokens that deliberately are not.

## Why the two are different shapes

An **access token** is a JWT because it must be checkable without a database
round trip — that is the entire reason to carry claims in a signed blob. It is
short-lived (fifteen minutes) precisely because it cannot be revoked: the only
thing that stops a stolen access token is its own expiry.

A **refresh token** is 256 bits of opaque randomness because it must be
revocable, and a self-describing token cannot be. It is stored as a SHA-256
hash, checked against the database on every use, rotated on every use, and
belongs to a session family that can be killed. Making the refresh token a JWT
too — a common shortcut — produces a credential that stays valid for its full
lifetime after the user clicks "log out everywhere".

## Verification pins the algorithm

`alg` is an attacker-controlled field in the token itself, which is the root of
both classic JWT forgeries: switch it to `none` and the signature is skipped,
or switch an RS256 verifier to HS256 and the public key becomes the HMAC
secret. Both are defeated by refusing to read `alg` from the token and passing
a fixed allow-list to the verifier, which is what `algorithms=[...]` does here.

`iss` and `aud` are required and checked. Without them a token minted by a
different service that happens to share the secret is accepted by this one.

## Secrets rotate through a keyring

`kid` names the key that signed a token. Verification accepts any key in the
ring; signing only ever uses the newest. That is what makes rotation possible
without logging everyone out: deploy the new key alongside the old, sign with
the new, and drop the old once every token issued under it has expired —
fifteen minutes later.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .types import AuthError, Principal, utcnow

__all__ = [
    "AccessTokenIssuer",
    "Keyring",
    "SigningKey",
    "InvalidToken",
    "new_refresh_token",
    "new_opaque_token",
    "ALGORITHM",
    "DEFAULT_ACCESS_TTL_S",
]

ALGORITHM = "HS256"
DEFAULT_ACCESS_TTL_S = 900          # 15 minutes
#: 256 bits, URL-safe. `token_urlsafe(32)` is 43 characters of base64.
TOKEN_BYTES = 32


class InvalidToken(AuthError):
    """A token that cannot be trusted. The reason is never in the message."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(
            "INVALID_TOKEN",
            "That session is no longer valid. Please sign in again.",
            status=401,
        )
        #: For the audit log, not for the response body. "Signature failed"
        #: and "expired 3 seconds ago" are both useful to an operator and both
        #: useful to an attacker.
        self.detail = detail


@dataclass(frozen=True, slots=True)
class SigningKey:
    kid: str
    secret: str

    def __post_init__(self) -> None:
        if len(self.secret) < 32:
            raise ValueError(
                f"signing key {self.kid!r} is {len(self.secret)} characters; "
                f"HS256 needs at least 32 to be worth the algorithm. Generate "
                f"one with `secrets.token_urlsafe(48)`."
            )


@dataclass
class Keyring:
    """The keys that may sign, and the keys that may verify.

    Signing uses `keys[0]`. Verification tries the key named by the token's
    `kid`, and refuses a token whose `kid` is unknown rather than trying them
    all — an unknown `kid` means the token was not minted here.
    """

    keys: tuple[SigningKey, ...]

    def __post_init__(self) -> None:
        if not self.keys:
            raise ValueError("a keyring needs at least one key")
        seen = [k.kid for k in self.keys]
        if len(set(seen)) != len(seen):
            raise ValueError(f"duplicate kid in keyring: {seen}")

    @property
    def signing(self) -> SigningKey:
        return self.keys[0]

    def by_kid(self, kid: str) -> SigningKey | None:
        return next((k for k in self.keys if k.kid == kid), None)

    @classmethod
    def generated(cls) -> Keyring:
        """A random single-key ring.

        For tests and local development. A deployment that calls this issues
        tokens nobody else can verify and invalidates every session on
        restart — which is why `config.py` refuses to reach it in production.
        """

        return cls((SigningKey(kid="dev", secret=secrets.token_urlsafe(48)),))


@dataclass
class AccessTokenIssuer:
    """Mints and verifies access tokens."""

    keyring: Keyring
    issuer: str = "clipforge"
    audience: str = "clipforge-api"
    ttl_s: int = DEFAULT_ACCESS_TTL_S
    #: Tolerance for clock skew between the signing and verifying hosts. Small
    #: on purpose: it extends the life of every token by this much.
    leeway_s: int = 10

    def __post_init__(self) -> None:
        try:
            import jwt
        except ImportError as error:                        # pragma: no cover
            raise RuntimeError(
                "the `PyJWT` package is required for access tokens. Install "
                "it with `pip install 'clipforge[auth]'`."
            ) from error
        self._jwt = jwt

    def issue(
        self,
        *,
        identity_id: str,
        email: str,
        tenant_id: str,
        user_id: str,
        role: str,
        session_id: str,
        now: datetime | None = None,
    ) -> tuple[str, datetime]:
        """A signed access token and the moment it stops being valid."""

        now = now or utcnow()
        expires = now + timedelta(seconds=self.ttl_s)
        claims = {
            "iss": self.issuer,
            "aud": self.audience,
            "sub": identity_id,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int(expires.timestamp()),
            # A unique id per token, so one can be denied individually and so
            # the audit log can tie a request back to the login that made it.
            "jti": uuid.uuid4().hex,
            "email": email,
            "tid": tenant_id,
            "uid": user_id,
            "role": role,
            "sid": session_id,
        }
        token = self._jwt.encode(
            claims,
            self.keyring.signing.secret,
            algorithm=ALGORITHM,
            headers={"kid": self.keyring.signing.kid},
        )
        return token, expires

    def verify(self, token: str, now: datetime | None = None) -> Principal:
        """Check a token and return who it says they are.

        Raises `InvalidToken` for everything: a bad signature, an unknown key,
        an expired token and a malformed one are one outcome as far as a caller
        is concerned, and telling them apart is a service to an attacker.
        """

        if not token or not isinstance(token, str):
            raise InvalidToken("empty token")

        try:
            header = self._jwt.get_unverified_header(token)
        except Exception as error:                          # noqa: BLE001
            raise InvalidToken(f"unreadable header: {error}") from error

        key = self.keyring.by_kid(str(header.get("kid", "")))
        if key is None:
            raise InvalidToken(f"unknown kid {header.get('kid')!r}")

        try:
            claims = self._jwt.decode(
                token,
                key.secret,
                # Pinned. Never read from the token: that is the `alg=none`
                # and RS256→HS256 confusion class in one line.
                algorithms=[ALGORITHM],
                issuer=self.issuer,
                audience=self.audience,
                leeway=self.leeway_s,
                options={
                    "require": ["exp", "iat", "nbf", "iss", "aud", "sub", "jti"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iss": True,
                    "verify_aud": True,
                },
            )
        except Exception as error:                          # noqa: BLE001
            raise InvalidToken(f"{type(error).__name__}: {error}") from error

        return Principal(
            identity_id=str(claims["sub"]),
            email=str(claims.get("email", "")),
            tenant_id=str(claims.get("tid", "")),
            user_id=str(claims.get("uid", "")),
            role=str(claims.get("role", "")),
            session_id=str(claims.get("sid", "")),
            issued_at=_moment(claims["iat"]),
            expires_at=_moment(claims["exp"]),
            jti=str(claims["jti"]),
        )


def _moment(value: Any) -> datetime:
    from datetime import UTC

    return datetime.fromtimestamp(int(value), UTC)


def new_refresh_token() -> str:
    """256 bits of opaque randomness from the OS CSPRNG.

    Not a JWT, and not derived from anything about the user: a refresh token
    that encodes an account id lets anyone holding one learn something, and
    lets anyone who guesses the scheme try to forge one.
    """

    return secrets.token_urlsafe(TOKEN_BYTES)


def new_opaque_token() -> str:
    """For email verification and password reset links.

    Same strength as a refresh token. These arrive by email, live in inbox
    history and sometimes in server logs at the receiving end, which is why
    they are short-lived and single-use rather than merely unguessable.
    """

    return secrets.token_urlsafe(TOKEN_BYTES)
