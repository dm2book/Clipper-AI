"""What a deployment decides, and what it is not allowed to decide.

Everything here has a default that is safe for a test and explicitly unsafe for
production, plus a `require_production_ready()` that refuses to start when one
of the unsafe defaults survived into a real deployment. A generated signing key
is the clearest example: it works perfectly, and it silently signs everyone out
on every restart and cannot be verified by a second instance.

Rate limits are the second: the defaults are deliberately strict, because the
cost of a limit that is slightly too tight is a user waiting a minute, and the
cost of one that is too loose is an unmetered password-guessing endpoint.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .passwords import ALGORITHM, PasswordPolicy
from .tokens import Keyring, SigningKey

__all__ = ["AuthConfig", "ENV_PREFIX", "config_from_env", "MisconfiguredAuth"]

ENV_PREFIX = "CLIPFORGE_AUTH_"


class MisconfiguredAuth(Exception):
    """A setting that is fine in a test and dangerous in production."""


#: `action -> (attempts, window_seconds)`.
#:
#: Two limits guard login rather than one, and they answer different attacks.
#: `login_ip` stops one host working through a list of addresses;
#: `login_email` stops a botnet spread across thousands of hosts working
#: through one account's passwords. Either alone leaves the other wide open.
DEFAULT_RATE_LIMITS: dict[str, tuple[int, int]] = {
    "login_ip": (20, 300),
    "login_email": (8, 300),
    "signup": (5, 3600),
    "reset_request": (5, 3600),
    "reset": (10, 3600),
    "verify": (20, 3600),
    "verify_resend": (5, 3600),
    "refresh": (120, 60),
    "change_password": (5, 900),
}


@dataclass
class AuthConfig:
    # -- lifetimes ---------------------------------------------------------
    #: Short, because an access token cannot be revoked. This number is the
    #: actual window of exposure for a stolen one.
    access_ttl_s: int = 900                     # 15 minutes
    #: Idle timeout: a refresh unused for this long stops working.
    refresh_ttl_s: int = 60 * 60 * 24 * 14      # 14 days
    #: Absolute ceiling regardless of activity. Without it a session that
    #: keeps refreshing lives for ever, and so does a stolen one.
    session_max_s: int = 60 * 60 * 24 * 90      # 90 days
    verification_ttl_s: int = 60 * 60 * 24      # 24 hours
    #: Much shorter than verification. A reset link is a live credential, and
    #: it sits in an inbox.
    reset_ttl_s: int = 60 * 30                  # 30 minutes
    #: How long a deleted account can still be recovered.
    deletion_grace_s: int = 60 * 60 * 24 * 30   # 30 days

    # -- lockout -----------------------------------------------------------
    max_failed_attempts: int = 10
    lockout_s: int = 900

    # -- policy ------------------------------------------------------------
    require_verified_email: bool = True
    password_policy: PasswordPolicy = field(default_factory=PasswordPolicy)
    password_algorithm: str = ALGORITHM
    rate_limits: dict[str, tuple[int, int]] = field(
        default_factory=lambda: dict(DEFAULT_RATE_LIMITS)
    )

    # -- links -------------------------------------------------------------
    verification_url: str = "https://app.clipforge.example/verify"
    reset_url: str = "https://app.clipforge.example/reset"

    # -- tokens ------------------------------------------------------------
    keyring: Keyring = field(default_factory=Keyring.generated)
    issuer: str = "clipforge"
    audience: str = "clipforge-api"
    #: True when `keyring` was generated rather than supplied. Tracked so
    #: `require_production_ready` can catch it.
    keyring_is_ephemeral: bool = True

    # -- user-facing copy --------------------------------------------------
    #: One sentence, used whether or not an account was created. Wording it
    #: once here is what stops a well-meaning change to one branch from
    #: reintroducing the enumeration oracle.
    signup_message: str = (
        "Check your inbox. If that address can be registered, a confirmation "
        "link is on its way."
    )
    invalid_link_message: str = (
        "That link is invalid or has expired. Request a new one."
    )

    def require_production_ready(self) -> None:
        """Refuse to start on a configuration that is unsafe in production."""

        problems: list[str] = []
        if self.keyring_is_ephemeral:
            problems.append(
                f"the JWT signing key was generated at startup — set "
                f"{ENV_PREFIX}SIGNING_KEYS, or every restart signs all users "
                f"out and a second instance cannot verify the first's tokens"
            )
        if not self.require_verified_email:
            problems.append(
                "unverified email addresses can sign in, so anyone can hold "
                "an account on an address they do not control"
            )
        if self.password_policy.rounds < 10:
            problems.append(
                f"bcrypt cost is {self.password_policy.rounds}; production "
                f"wants 12 or more"
            )
        if not self.rate_limits:
            problems.append("rate limiting is disabled")
        if self.access_ttl_s > 3600:
            problems.append(
                f"access tokens live {self.access_ttl_s}s and cannot be "
                f"revoked; keep them under an hour"
            )
        if self.verification_url.startswith("http://") or (
            self.reset_url.startswith("http://")
        ):
            problems.append(
                "verification and reset links are http://, so the token is "
                "sent in clear text"
            )
        if problems:
            raise MisconfiguredAuth(
                "authentication is not production ready:\n  - "
                + "\n  - ".join(problems)
            )


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"{ENV_PREFIX}{name}", default).strip()


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise MisconfiguredAuth(
            f"{ENV_PREFIX}{name} must be a whole number, got {raw!r}"
        ) from error


def config_from_env() -> AuthConfig:
    """Build a config from the environment.

    Signing keys come from `CLIPFORGE_AUTH_SIGNING_KEYS` as
    `kid:secret,kid:secret`, newest first. Several are accepted so a key can be
    rotated without ending every session: sign with the new one, keep verifying
    with the old until the last token issued under it has expired.

    **No secret has a default.** Absent, the keyring is generated and marked
    ephemeral, which `require_production_ready()` then refuses.
    """

    config = AuthConfig()

    raw_keys = _env("SIGNING_KEYS")
    if raw_keys:
        keys: list[SigningKey] = []
        for entry in raw_keys.split(","):
            kid, _, secret = entry.strip().partition(":")
            if not kid or not secret:
                raise MisconfiguredAuth(
                    f"{ENV_PREFIX}SIGNING_KEYS entries must be 'kid:secret'; "
                    f"got {entry.strip()!r}"
                )
            keys.append(SigningKey(kid=kid, secret=secret))
        config.keyring = Keyring(tuple(keys))
        config.keyring_is_ephemeral = False

    config.access_ttl_s = _int("ACCESS_TTL_S", config.access_ttl_s)
    config.refresh_ttl_s = _int("REFRESH_TTL_S", config.refresh_ttl_s)
    config.session_max_s = _int("SESSION_MAX_S", config.session_max_s)
    config.verification_ttl_s = _int("VERIFICATION_TTL_S", config.verification_ttl_s)
    config.reset_ttl_s = _int("RESET_TTL_S", config.reset_ttl_s)
    config.deletion_grace_s = _int("DELETION_GRACE_S", config.deletion_grace_s)
    config.max_failed_attempts = _int("MAX_FAILED_ATTEMPTS",
                                      config.max_failed_attempts)
    config.lockout_s = _int("LOCKOUT_S", config.lockout_s)

    config.issuer = _env("ISSUER", config.issuer)
    config.audience = _env("AUDIENCE", config.audience)
    config.verification_url = _env("VERIFICATION_URL", config.verification_url)
    config.reset_url = _env("RESET_URL", config.reset_url)

    verified = _env("REQUIRE_VERIFIED_EMAIL")
    if verified:
        config.require_verified_email = verified.lower() not in ("0", "false", "no")

    rounds = _int("BCRYPT_ROUNDS", config.password_policy.rounds)
    min_length = _int("PASSWORD_MIN_LENGTH", config.password_policy.min_length)
    config.password_policy = PasswordPolicy(
        min_length=min_length, rounds=rounds,
        max_length=config.password_policy.max_length,
    )
    return config


def describe_environment() -> dict[str, object]:
    """What is configured, for a startup log. No secret is echoed."""

    config = config_from_env()
    try:
        config.require_production_ready()
        problems: list[str] = []
    except MisconfiguredAuth as error:
        problems = [line.strip("- ") for line in str(error).splitlines()[1:]]

    return {
        "env_prefix": ENV_PREFIX,
        "signing_keys": [k.kid for k in config.keyring.keys],
        "signing_key_source": (
            "generated at startup" if config.keyring_is_ephemeral
            else f"{ENV_PREFIX}SIGNING_KEYS"
        ),
        "access_ttl_s": config.access_ttl_s,
        "refresh_ttl_s": config.refresh_ttl_s,
        "session_max_s": config.session_max_s,
        "bcrypt_rounds": config.password_policy.rounds,
        "require_verified_email": config.require_verified_email,
        "rate_limits": dict(config.rate_limits),
        "production_ready": not problems,
        "problems": problems,
    }
