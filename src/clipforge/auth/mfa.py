"""Second factors: enrolment, the login challenge, and the way back in.

Mixed into `AuthService` rather than written as a separate service, because
every one of these flows has to touch the same rate limiter, the same audit
log and the same session machinery. A parallel service would need its own copy
of all three, and the copy that drifts is always the one guarding the login.

## The shape of an MFA login

    log_in(email, password)        -> LoginResult(mfa=MfaChallenge(...))
    complete_mfa(challenge, code)  -> LoginResult(tokens=...)

The password step returns **no session and no tokens** when a factor is owed.
The challenge is a single-use, five-minute token in `auth_tokens`, hashed at
rest like every other token here. Anything less — a flag on a real session, a
signed blob the server does not track — makes the second factor advisory,
because a client that simply skips the second call is already logged in.

## Enrolment cannot lock you out

`begin_enrolment` stores an *unconfirmed* factor and returns the secret. It
does not gate anything. Only `confirm_enrolment`, which requires a working
code, marks the factor active. Someone who starts enrolling, fails to scan the
QR code and closes the tab is exactly where they were, which is the behaviour
a support queue never hears about.

## Recovery codes are the actual failure mode

Phones are lost far more often than passwords are stolen, and an account with
TOTP and no recovery path is an account that becomes support's problem. Ten
single-use codes are issued at confirmation, stored as SHA-256 (they are
high-entropy already, so Argon2 would buy latency and nothing else), and
regenerating replaces the set rather than extending it.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .types import (
    AuthError,
    EventKind,
    Identity,
    MfaFactor,
    MfaKind,
    RecoveryCode,
    TokenKind,
    VerificationToken,
)
from . import totp as totp_mod

__all__ = [
    "MfaChallenge",
    "EnrolmentStarted",
    "RECOVERY_CODE_COUNT",
    "format_recovery_code",
    "device_label",
]

#: Ten is the number every major provider settled on, and the reasoning is
#: sound: enough that losing the printout to a coffee spill is survivable,
#: few enough that a person will actually store them somewhere.
RECOVERY_CODE_COUNT = 10
#: 40 bits per code in Crockford-ish base32. Not a password — it is one of ten
#: values, single use, behind a rate limit — so the guessing budget is what
#: matters, not the entropy headline.
_RECOVERY_BYTES = 5

#: How long a half-finished login stays resumable. Long enough to open an
#: authenticator app and read a code; short enough that a challenge left on a
#: shared computer expires before the next person sits down.
CHALLENGE_TTL_S = 300


@dataclass(frozen=True, slots=True)
class MfaChallenge:
    """Returned instead of tokens when a second factor is owed."""

    challenge_token: str
    identity_id: str
    expires_at: datetime
    #: Which factors could answer this, so a client can show the right prompt.
    kinds: tuple[str, ...] = ("totp",)
    recovery_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "challenge_token": self.challenge_token,
            "expires_at": self.expires_at.isoformat(),
            "kinds": list(self.kinds),
            "recovery_available": self.recovery_available,
        }


@dataclass(frozen=True, slots=True)
class EnrolmentStarted:
    """What the user needs to add the factor, and nothing that outlives it."""

    factor_id: str
    secret: str
    uri: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor_id": self.factor_id,
            "secret": self.secret,
            "otpauth_uri": self.uri,
        }


def format_recovery_code(raw: bytes) -> str:
    """`A1B2C-D3E4F`, which is what people can copy without transcribing wrong.

    Base32 without the letters that get confused with digits by hand. The
    hyphen is cosmetic and stripped before comparison.
    """

    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"      # no I, O, 0, 1
    value = int.from_bytes(raw, "big")
    digits = []
    for _ in range(10):
        digits.append(alphabet[value % len(alphabet)])
        value //= len(alphabet)
    body = "".join(reversed(digits))
    return f"{body[:5]}-{body[5:]}"


def normalise_recovery_code(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def device_label(user_agent: str) -> str:
    """A name a person recognises in a list of their own devices.

    Crude on purpose. Full user-agent parsing needs a database that goes stale,
    and the job here is only to tell one row apart from another well enough
    that "sign this one out" is a decision somebody can make. Unknown agents
    keep a truncated raw string rather than being labelled "Unknown", which
    would make every API client identical in the list.
    """

    agent = (user_agent or "").strip()
    if not agent:
        return "Unknown device"

    browsers = (
        ("Edg/", "Edge"), ("OPR/", "Opera"), ("Chrome/", "Chrome"),
        ("Firefox/", "Firefox"), ("Safari/", "Safari"), ("curl/", "curl"),
        ("python-requests", "Python"), ("Postman", "Postman"),
    )
    systems = (
        ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
        ("Mac OS X", "macOS"), ("Macintosh", "macOS"), ("Windows", "Windows"),
        ("CrOS", "ChromeOS"), ("Linux", "Linux"),
    )
    browser = next((name for token, name in browsers if token in agent), "")
    system = next((name for token, name in systems if token in agent), "")

    if browser and system:
        return f"{browser} on {system}"
    if browser:
        return browser
    if system:
        return system
    return agent[:40]


# ---------------------------------------------------------------------------
# The mixin
# ---------------------------------------------------------------------------


class MfaMixin:
    """Enrolment, challenge and recovery, over an `AuthStore`.

    Mixed into `AuthService`, which supplies `store`, `hasher`, `clock`,
    `config`, `_audit`, `_limit` and `_start_session`.
    """

    # -- enrolment ---------------------------------------------------------

    def begin_enrolment(
        self, identity_id: str, *, label: str = "", ip: str = "",
    ) -> EnrolmentStarted:
        """Create an unconfirmed TOTP factor and hand back its secret.

        The secret leaves the server exactly once, here. There is no endpoint
        that reads it back: a user who loses the QR code before confirming
        starts again, which costs them ten seconds and removes an obvious way
        to harvest seeds from a stolen session.
        """

        now = self.clock()
        self._limit("mfa_enrol", identity_id, now, ip=ip)

        identity = self.store.identity(identity_id)
        if identity is None:
            raise AuthError("NOT_FOUND", "No such account.", status=404)

        # Abandoned attempts are cleared first, so repeatedly opening the
        # enrolment page does not accumulate dead rows that later look like a
        # user with six authenticators.
        for existing in self.store.factors_for(identity_id):
            if not existing.active:
                self.store.delete_factor(existing.factor_id)

        secret = totp_mod.generate_secret()
        factor = MfaFactor(
            factor_id=f"mfa_{uuid.uuid4().hex[:16]}",
            identity_id=identity_id,
            kind=MfaKind.TOTP,
            label=label[:60] or "Authenticator app",
            secret=secret,
            created_at=now,
        )
        self.store.create_factor(factor)
        self._audit(EventKind.MFA_ENROLMENT_STARTED, identity_id=identity_id,
                    email=identity.email, ip=ip)
        return EnrolmentStarted(
            factor_id=factor.factor_id,
            secret=secret,
            uri=totp_mod.provisioning_uri(
                secret, identity.email, self.config.mfa_issuer,
            ),
        )

    def confirm_enrolment(
        self, identity_id: str, factor_id: str, code: str, *, ip: str = "",
    ) -> tuple[MfaFactor, tuple[str, ...]]:
        """Activate a factor once the user proves it works.

        Returns the factor and the recovery codes, which are shown once and
        never again — they are stored hashed, so the server genuinely cannot
        reproduce them later.
        """

        now = self.clock()
        self._limit("mfa_confirm", identity_id, now, ip=ip)

        factor = self.store.factor(factor_id)
        if factor is None or factor.identity_id != identity_id:
            raise AuthError("NOT_FOUND", "No such factor.", status=404)
        if factor.active:
            raise AuthError(
                "ALREADY_CONFIRMED", "That factor is already switched on."
            )

        counter = totp_mod.verify_totp(
            factor.secret, code, at=now.timestamp(), config=self.config.totp,
        )
        if counter is None:
            self._audit(EventKind.MFA_FAILED, identity_id=identity_id, ip=ip,
                        succeeded=False, detail="enrolment code did not match")
            raise AuthError(
                "INVALID_CODE",
                "That code is not right. Check your authenticator app is "
                "showing the current code and that your device clock is "
                "correct.",
            )

        factor.confirmed_at = now
        factor.last_used_at = now
        factor.last_counter = counter
        self.store.save_factor(factor)

        codes = self._issue_recovery_codes(identity_id, now)
        self._audit(EventKind.MFA_ENABLED, identity_id=identity_id, ip=ip)
        self._send_safely(
            "mfa_enabled_email", self.store.identity(identity_id),
        )
        return factor, codes

    def disable_mfa(self, identity_id: str, *, ip: str = "") -> int:
        """Remove every factor and every recovery code.

        Callers must re-check the password first. This does not do it, because
        the two places that call it — a settings page and an admin action —
        prove presence in different ways, and a check buried here would be
        skipped by whichever caller was written second.
        """

        removed = 0
        for factor in self.store.factors_for(identity_id):
            if self.store.delete_factor(factor.factor_id):
                removed += 1
        self.store.replace_recovery_codes(identity_id, [])
        if removed:
            self._audit(EventKind.MFA_DISABLED, identity_id=identity_id, ip=ip)
            self._send_safely(
                "mfa_disabled_email", self.store.identity(identity_id),
            )
        return removed

    def active_factors(self, identity_id: str) -> tuple[MfaFactor, ...]:
        return tuple(f for f in self.store.factors_for(identity_id) if f.active)

    def mfa_required(self, identity_id: str) -> bool:
        return bool(self.active_factors(identity_id))

    # -- recovery codes ----------------------------------------------------

    def _issue_recovery_codes(
        self, identity_id: str, now: datetime,
    ) -> tuple[str, ...]:
        plain: list[str] = []
        records: list[RecoveryCode] = []
        for _ in range(RECOVERY_CODE_COUNT):
            code = format_recovery_code(secrets.token_bytes(_RECOVERY_BYTES))
            plain.append(code)
            records.append(RecoveryCode(
                code_id=f"rec_{uuid.uuid4().hex[:16]}",
                identity_id=identity_id,
                code_hash=self.hasher.hash_token(normalise_recovery_code(code)),
                created_at=now,
            ))
        self.store.replace_recovery_codes(identity_id, records)
        return tuple(plain)

    def regenerate_recovery_codes(
        self, identity_id: str, *, ip: str = "",
    ) -> tuple[str, ...]:
        now = self.clock()
        self._limit("mfa_recovery_regen", identity_id, now, ip=ip)
        if not self.mfa_required(identity_id):
            raise AuthError(
                "MFA_NOT_ENABLED",
                "There is no second factor on this account, so recovery codes "
                "would not let anybody in.",
            )
        return self._issue_recovery_codes(identity_id, now)

    def recovery_codes_remaining(self, identity_id: str) -> int:
        return sum(
            1 for code in self.store.recovery_codes_for(identity_id)
            if not code.spent
        )

    # -- the login challenge ----------------------------------------------

    def _issue_challenge(
        self, identity: Identity, now: datetime, *, ip: str,
    ) -> MfaChallenge:
        raw = secrets.token_urlsafe(32)
        self.store.invalidate_tokens(
            identity.identity_id, TokenKind.MFA_CHALLENGE, now,
        )
        expires = now + timedelta(seconds=CHALLENGE_TTL_S)
        self.store.create_token(VerificationToken(
            token_id=f"tok_{uuid.uuid4().hex[:16]}",
            identity_id=identity.identity_id,
            kind=TokenKind.MFA_CHALLENGE,
            token_hash=self.hasher.hash_token(raw),
            expires_at=expires,
            created_at=now,
            requested_ip=ip,
        ))
        self._audit(EventKind.MFA_CHALLENGED,
                    identity_id=identity.identity_id, email=identity.email,
                    ip=ip)
        return MfaChallenge(
            challenge_token=raw,
            identity_id=identity.identity_id,
            expires_at=expires,
            kinds=tuple(
                sorted({f.kind.value for f in self.active_factors(
                    identity.identity_id
                )})
            ),
            recovery_available=self.recovery_codes_remaining(
                identity.identity_id
            ) > 0,
        )

    def complete_mfa(
        self,
        challenge_token: str,
        code: str,
        *,
        tenant_id: str = "",
        ip: str = "",
        user_agent: str = "",
        device_id: str = "",
    ):
        """Finish a login that stopped at the second factor.

        Accepts either a TOTP code or a recovery code; which one it was is in
        the audit trail, because a recovery code being spent is a fact somebody
        should be able to see later.
        """

        from .service import LoginResult                    # circular at import

        now = self.clock()
        self._limit("mfa_verify", (ip or challenge_token[:16]), now, ip=ip)

        token = self.store.token_by_hash(
            self.hasher.hash_token(challenge_token)
        )
        if token is None or token.kind is not TokenKind.MFA_CHALLENGE or (
            not token.usable(now)
        ):
            raise AuthError(
                "INVALID_CHALLENGE",
                "That sign-in attempt has expired. Start again.",
                status=401,
            )

        identity = self.store.identity(token.identity_id)
        if identity is None or not identity.status.can_log_in:
            raise AuthError(
                "INVALID_CHALLENGE",
                "That sign-in attempt has expired. Start again.",
                status=401,
            )

        used_recovery = self._spend_second_factor(identity, code, now, ip=ip)
        if used_recovery is None:
            # The challenge is *not* consumed on a wrong code — otherwise a
            # single mistyped digit means signing in from scratch. The rate
            # limit above is what bounds guessing.
            self._audit(EventKind.MFA_FAILED, identity_id=identity.identity_id,
                        email=identity.email, ip=ip, succeeded=False,
                        detail="second factor did not match")
            raise AuthError(
                "INVALID_CODE",
                "That code is not right. Try the current code from your "
                "authenticator app, or use a recovery code.",
                status=401,
            )

        token.used_at = now
        self.store.save_token(token)

        memberships = self.store.memberships(identity.identity_id)
        chosen = self._choose_membership(memberships, tenant_id)
        tokens = self._start_session(
            identity, chosen, now, ip=ip, user_agent=user_agent,
            device_id=device_id, mfa_satisfied=True,
        )
        self._audit(EventKind.MFA_SUCCEEDED, identity_id=identity.identity_id,
                    email=identity.email, ip=ip,
                    tenant_id=tokens.tenant_id, session_id=tokens.session_id,
                    detail="recovery code" if used_recovery else "totp")
        self._audit(EventKind.LOGIN_SUCCEEDED,
                    identity_id=identity.identity_id, email=identity.email,
                    ip=ip, user_agent=user_agent, tenant_id=tokens.tenant_id,
                    session_id=tokens.session_id)
        return LoginResult(
            tokens=tokens, identity=identity, memberships=memberships,
            unverified=not identity.verified,
        )

    def _spend_second_factor(
        self, identity: Identity, code: str, now: datetime, *, ip: str,
    ) -> bool | None:
        """True if a recovery code was spent, False for TOTP, None for no match."""

        for factor in self.active_factors(identity.identity_id):
            if factor.kind is not MfaKind.TOTP:
                continue                                    # nothing else yet
            counter = totp_mod.verify_totp(
                factor.secret, code, at=now.timestamp(),
                config=self.config.totp, last_counter=factor.last_counter,
            )
            if counter is not None:
                factor.last_counter = counter
                factor.last_used_at = now
                self.store.save_factor(factor)
                return False

        presented = normalise_recovery_code(code)
        if not presented:
            return None
        for record in self.store.recovery_codes_for(identity.identity_id):
            if record.spent:
                continue
            if self.hasher.same_token(presented, record.code_hash):
                record.used_at = now
                record.used_ip = ip
                self.store.save_recovery_code(record)
                # Extracted rather than inlined: a multi-line expression
                # inside an f-string is a 3.12 feature and this targets 3.11.
                left = self.recovery_codes_remaining(identity.identity_id)
                self._audit(EventKind.MFA_RECOVERY_USED,
                            identity_id=identity.identity_id,
                            email=identity.email, ip=ip,
                            detail=f"{left} recovery codes left")
                self._send_safely("recovery_code_used_email", identity)
                return True
        return None

    # -- helpers -----------------------------------------------------------

    def _send_safely(self, builder: str, identity: Identity | None) -> None:
        """Send a security notice if the email module knows how to build one.

        Tolerant by design: these are notifications about a change that has
        already happened, and failing the change because the notice could not
        be built would be the wrong way round.
        """

        if identity is None:
            return
        from . import email as email_mod

        factory = getattr(email_mod, builder, None)
        if factory is None:
            return
        try:
            self._send(factory(identity.email))
        except Exception:                                   # noqa: BLE001
            pass
