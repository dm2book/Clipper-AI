"""The flows: signup, verification, login, refresh, reset, deletion.

Everything in this module is arranged around three ideas.

## 1. The answer must not depend on whether the account exists

Signup, login and password reset all take an email address, and all three have
an obvious implementation that leaks whether that address is registered — a
different message, a different status code, or simply a faster reply. Any of
the three lets someone test a list of a million addresses against this service
and learn which of your customers work where.

So: `sign_up` returns the same result either way and mails the existing owner
instead of erroring. `request_password_reset` returns the same result either
way. `log_in` returns one `INVALID_CREDENTIALS` for an unknown address and a
wrong password, and spends a real bcrypt verification on the unknown address so
the two take the same time.

## 2. Refresh tokens rotate, and reuse means theft

Every refresh mints a new token and retires the old one. If a retired token is
ever presented again, exactly one of two things happened: a client raced itself,
or an attacker copied a token and the real client has already rotated past it.
The second is not distinguishable from the first at the moment it happens, and
it is much worse, so the whole session family is revoked and the event is
recorded. The legitimate user signs in again; the attacker's copy is dead.

## 3. Anything that changes a credential ends every session

Password reset, password change and email change all revoke every session for
the identity. This is the step that makes "reset your password" a real remedy
for a compromise rather than a gesture — without it the attacker's existing
refresh token outlives the reset.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from . import email as email_mod
from .config import AuthConfig
from .mfa import MfaMixin, device_label
from .passwords import PasswordHasher, WeakPassword
from .store import AuthStore, DuplicateEmail
from .tokens import AccessTokenIssuer, InvalidToken, new_opaque_token, new_refresh_token
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
    utcnow,
)

log = logging.getLogger("clipforge.auth")

__all__ = ["AuthService", "SignUpResult", "LoginResult"]


@dataclass(slots=True)
class SignUpResult:
    """Deliberately uninformative about whether anything was created.

    `created` is for the caller's own tests and metrics, never for a response
    body. `message` is what the user should see, and it is the same sentence
    in both cases.
    """

    message: str
    created: bool = False
    identity_id: str = ""


@dataclass(slots=True)
class LoginResult:
    """The outcome of a password step, which is not always a session.

    When a second factor is owed, `tokens` is None and `mfa` carries the
    challenge. Modelled as one result type rather than two so a caller that
    forgets about MFA gets an obvious `None` where it wanted a token, instead
    of a plausible-looking session that skipped a factor.
    """

    tokens: TokenPair | None
    identity: Identity
    memberships: tuple[Membership, ...] = ()
    #: True when the account exists but has not confirmed its address, and the
    #: deployment allows unverified sign-in. The UI should nag.
    unverified: bool = False
    #: Set when the password was right and a second factor is still required.
    mfa: Any = None

    @property
    def complete(self) -> bool:
        return self.tokens is not None


class AuthService(MfaMixin):
    """Every authentication flow, over an `AuthStore`."""

    def __init__(
        self,
        store: AuthStore,
        issuer: AccessTokenIssuer,
        *,
        config: AuthConfig | None = None,
        hasher: PasswordHasher | None = None,
        sender: Any | None = None,
        clock: Callable[[], datetime] = utcnow,
        provisioner: Callable[[Identity], None] | None = None,
    ) -> None:
        self.store = store
        self.issuer = issuer
        self.config = config or AuthConfig()
        self.hasher = hasher or PasswordHasher(self.config.password_policy)
        self.sender = sender or email_mod.RecordingEmailSender()
        self.clock = clock
        #: Called during login when an authenticated identity turns out to
        #: have no live membership, and given one chance to create one.
        #:
        #: A callback rather than a database handle, because tenants live in
        #: the application schema behind `clipforge_app` while this service
        #: connects as `clipforge_auth` — a role scoped to the five `auth_*`
        #: tables precisely so the request path cannot reach a password hash.
        #: Handing this object an application connection would undo that.
        #: `api.onboarding.ensure_workspace` is what fills it in.
        self.provisioner = provisioner
        #: Serialises refresh rotation per session. Two tabs refreshing at the
        #: same instant is ordinary, and without this one of them reads the
        #: pre-rotation row and both write, leaving two live tokens.
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    # -- signup ------------------------------------------------------------

    def sign_up(
        self,
        email: str,
        password: str,
        *,
        ip: str = "",
        user_agent: str = "",
    ) -> SignUpResult:
        """Register an address, or quietly tell its owner someone tried.

        The response is identical whether the address was free or taken. See
        the module docstring.
        """

        now = self.clock()
        address = normalise_email(email)
        self._require_email_shape(address)
        self._limit("signup", ip or address, now, ip=ip, email=address)

        # Checked before the existence lookup, so a weak password is rejected
        # for a taken address too — otherwise "your password is too short"
        # versus the generic success message is itself the oracle.
        try:
            self.config.password_policy.check(password, address)
        except WeakPassword as error:
            raise AuthError("WEAK_PASSWORD", str(error)) from error

        identity = Identity(
            identity_id=f"idn_{uuid.uuid4().hex[:16]}",
            email=address,
            password_hash=self.hasher.hash(password),
            password_algo=self.config.password_algorithm,
            status=IdentityStatus.PENDING,
            created_at=now,
            updated_at=now,
        )

        try:
            created = self.store.create_identity(identity)
        except DuplicateEmail:
            # The address is taken. Tell its owner, not the person asking.
            self._audit(EventKind.SIGNUP_DUPLICATE, email=address, ip=ip,
                        user_agent=user_agent, succeeded=False,
                        detail="signup attempted for an existing address")
            self._send(email_mod.unexpected_signup_email(address))
            return SignUpResult(message=self.config.signup_message, created=False)

        self._audit(EventKind.SIGNUP_STARTED, identity_id=created.identity_id,
                    email=address, ip=ip, user_agent=user_agent)
        self._issue_verification(created, ip=ip)
        return SignUpResult(
            message=self.config.signup_message,
            created=True,
            identity_id=created.identity_id,
        )

    def resend_verification(self, email: str, *, ip: str = "") -> None:
        """Same uninformative contract as signup."""

        now = self.clock()
        address = normalise_email(email)
        self._limit("verify_resend", ip or address, now, ip=ip, email=address)
        identity = self.store.identity_by_email(address)
        if identity is None or identity.verified:
            return
        self._audit(EventKind.VERIFICATION_RESENT,
                    identity_id=identity.identity_id, email=address, ip=ip)
        self._issue_verification(identity, ip=ip)

    def verify_email(self, token: str, *, ip: str = "") -> Identity:
        """Confirm an address from the emailed token."""

        now = self.clock()
        self._limit("verify", ip or "anonymous", now, ip=ip)
        record = self._spend_token(token, TokenKind.EMAIL_VERIFICATION, now)

        identity = self.store.identity(record.identity_id)
        if identity is None:
            raise AuthError("INVALID_TOKEN", self.config.invalid_link_message,
                            status=400)

        if not identity.verified:
            identity.email_verified_at = now
            if identity.status is IdentityStatus.PENDING:
                identity.status = IdentityStatus.ACTIVE
            identity = self.store.save_identity(identity)

        self._audit(EventKind.EMAIL_VERIFIED, identity_id=identity.identity_id,
                    email=identity.email, ip=ip)
        return identity

    # -- login -------------------------------------------------------------

    def log_in(
        self,
        email: str,
        password: str,
        *,
        tenant_id: str = "",
        ip: str = "",
        user_agent: str = "",
        device_id: str = "",
    ) -> LoginResult:
        """Exchange an email and password for a token pair.

        `tenant_id` chooses which membership the session acts in. Omitted, the
        only membership is used; with several and none named, the caller must
        choose — the tokens are per tenant, so guessing would silently put
        someone in the wrong workspace.
        """

        now = self.clock()
        address = normalise_email(email)
        self._limit("login_ip", ip or "anonymous", now, ip=ip, email=address)
        self._limit("login_email", address, now, ip=ip, email=address)

        identity = self.store.identity_by_email(address)

        if identity is None:
            # Spend the time a real verification would. Returning here in
            # microseconds is a reliable "no such account" oracle.
            self.hasher.verify_dummy()
            self._audit(EventKind.LOGIN_FAILED, email=address, ip=ip,
                        user_agent=user_agent, succeeded=False,
                        detail="no identity for this address")
            raise self._invalid_credentials()

        if identity.locked(now):
            self._audit(EventKind.LOGIN_BLOCKED,
                        identity_id=identity.identity_id, email=address, ip=ip,
                        succeeded=False, detail="account temporarily locked")
            raise AuthError(
                "ACCOUNT_LOCKED",
                "Too many failed sign-in attempts. Try again shortly, or "
                "reset your password.",
                status=429,
            )

        if identity.status in (IdentityStatus.DELETED,):
            self.hasher.verify_dummy()
            self._audit(EventKind.LOGIN_FAILED,
                        identity_id=identity.identity_id, email=address, ip=ip,
                        succeeded=False, detail="identity is deleted")
            raise self._invalid_credentials()

        if not self.hasher.verify(password, identity.password_hash):
            self._register_failure(identity, now, ip=ip, user_agent=user_agent)
            raise self._invalid_credentials()

        if identity.status is IdentityStatus.PENDING and (
            self.config.require_verified_email
        ):
            self._audit(EventKind.LOGIN_BLOCKED,
                        identity_id=identity.identity_id, email=address, ip=ip,
                        succeeded=False, detail="email not verified")
            raise AuthError(
                "EMAIL_NOT_VERIFIED",
                "Confirm your email address before signing in. Check your "
                "inbox for the link, or request a new one.",
                status=403,
            )

        # Signing in cancels a pending deletion. Someone who comes back before
        # the grace period ends has plainly changed their mind, and making
        # them find a settings page to say so is a way to lose the account.
        if identity.status is IdentityStatus.PENDING_DELETION:
            identity.status = (
                IdentityStatus.ACTIVE if identity.verified
                else IdentityStatus.PENDING
            )
            identity.delete_after = None
            self._audit(EventKind.DELETION_CANCELLED,
                        identity_id=identity.identity_id, email=address, ip=ip,
                        detail="cancelled by signing in")

        # The one moment the plaintext is legitimately in memory, so it is the
        # one moment the stored hash can be upgraded.
        if self.hasher.needs_rehash(identity.password_hash, identity.password_algo):
            identity.password_hash = self.hasher.hash(password)
            identity.password_algo = self.config.password_algorithm

        identity.failed_attempts = 0
        identity.locked_until = None
        identity.last_login_at = now
        identity = self.store.save_identity(identity)

        self._clear_limit("login_email", address)

        # The password was right. If a second factor is owed, stop here and
        # hand back a challenge — no session, no tokens. Anything else makes
        # the factor advisory, because a client that skips the second call
        # would already be signed in.
        if self.mfa_required(identity.identity_id):
            challenge = self._issue_challenge(identity, now, ip=ip)
            return LoginResult(
                tokens=None, identity=identity,
                memberships=self.store.memberships(identity.identity_id),
                unverified=not identity.verified, mfa=challenge,
            )

        memberships = self.store.memberships(identity.identity_id)

        # A verified person with nowhere to work. Provisioning happens here,
        # at first sign-in, rather than at signup: an address that registers
        # and never returns then leaves no tenant row behind, and there is one
        # code path instead of two. It also repairs accounts created before
        # workspaces were provisioned at all.
        if not tenant_id and not any(m.active for m in memberships):
            if self.provisioner is not None:
                try:
                    self.provisioner(identity)
                except Exception:                           # noqa: BLE001
                    # Never fail a login over this. The credentials were
                    # valid; the session is simply workspace-less, which the
                    # API already handles and reports.
                    log.exception(
                        "provisioning a workspace failed for identity %s",
                        identity.identity_id,
                    )
                else:
                    memberships = self.store.memberships(identity.identity_id)

        chosen = self._choose_membership(memberships, tenant_id)

        tokens = self._start_session(
            identity, chosen, now, ip=ip, user_agent=user_agent,
            device_id=device_id,
        )
        self._audit(EventKind.LOGIN_SUCCEEDED, identity_id=identity.identity_id,
                    email=address, ip=ip, user_agent=user_agent,
                    tenant_id=tokens.tenant_id, session_id=tokens.session_id)

        return LoginResult(
            tokens=tokens, identity=identity, memberships=memberships,
            unverified=not identity.verified,
        )

    def _choose_membership(
        self, memberships: tuple[Membership, ...], tenant_id: str
    ) -> Membership | None:
        live = [m for m in memberships if m.active]
        if tenant_id:
            for membership in live:
                if membership.tenant_id == tenant_id:
                    return membership
            raise AuthError(
                "NO_SUCH_MEMBERSHIP",
                "You do not have access to that workspace.",
                status=403,
            )
        if len(live) == 1:
            return live[0]
        if not live:
            # A verified human with no workspace yet. Legitimate right after
            # signup, so it is a session with no tenant rather than an error —
            # enough to create or accept an invitation to one.
            return None
        raise AuthError(
            "TENANT_REQUIRED",
            "You belong to more than one workspace. Choose which one to open.",
            status=409,
        )

    def _register_failure(
        self, identity: Identity, now: datetime, *, ip: str, user_agent: str
    ) -> None:
        identity.failed_attempts += 1
        detail = "wrong password"
        if identity.failed_attempts >= self.config.max_failed_attempts:
            identity.locked_until = now + timedelta(
                seconds=self.config.lockout_s
            )
            identity.failed_attempts = 0
            detail = (
                f"locked for {self.config.lockout_s}s after "
                f"{self.config.max_failed_attempts} failures"
            )
            self._audit(EventKind.ACCOUNT_LOCKED,
                        identity_id=identity.identity_id, email=identity.email,
                        ip=ip, succeeded=False, detail=detail)
        self.store.save_identity(identity)
        self._audit(EventKind.LOGIN_FAILED, identity_id=identity.identity_id,
                    email=identity.email, ip=ip, user_agent=user_agent,
                    succeeded=False, detail=detail)

    def _invalid_credentials(self) -> AuthError:
        return AuthError(
            "INVALID_CREDENTIALS",
            "That email and password do not match an account.",
            status=401,
        )

    # -- sessions ----------------------------------------------------------

    def _start_session(
        self,
        identity: Identity,
        membership: Membership | None,
        now: datetime,
        *,
        ip: str,
        user_agent: str,
        device_id: str = "",
        mfa_satisfied: bool = False,
    ) -> TokenPair:
        if device_id:
            self._record_device(identity, device_id, now, ip=ip,
                                user_agent=user_agent)
        refresh = new_refresh_token()
        session = Session(
            session_id=f"ses_{uuid.uuid4().hex[:16]}",
            identity_id=identity.identity_id,
            token_hash=self.hasher.hash_token(refresh),
            tenant_id=membership.tenant_id if membership else "",
            device_id=device_id,
            mfa_satisfied=mfa_satisfied,
            ip=ip,
            user_agent=user_agent[:400],
            issued_at=now,
            expires_at=now + timedelta(seconds=self.config.refresh_ttl_s),
            absolute_expires_at=now + timedelta(
                seconds=self.config.session_max_s
            ),
        )
        self.store.create_session(session)

        access, expires = self.issuer.issue(
            identity_id=identity.identity_id,
            email=identity.email,
            tenant_id=session.tenant_id,
            user_id=membership.user_id if membership else "",
            role=membership.role if membership else "",
            session_id=session.session_id,
            now=now,
        )
        return TokenPair(
            access_token=access,
            refresh_token=refresh,
            expires_in_s=int((expires - now).total_seconds()),
            session_id=session.session_id,
            tenant_id=session.tenant_id,
        )

    def refresh(
        self, refresh_token: str, *, ip: str = "", user_agent: str = ""
    ) -> TokenPair:
        """Rotate a refresh token for a new pair.

        Presenting a token that has already been rotated away from revokes the
        entire session family — see the module docstring.
        """

        now = self.clock()
        self._limit("refresh", ip or "anonymous", now, ip=ip)

        presented = self.hasher.hash_token(refresh_token)
        session = self.store.session_by_token_hash(presented)
        if session is None:
            self._audit(EventKind.TOKEN_REFRESHED, ip=ip, succeeded=False,
                        detail="refresh token not recognised")
            raise InvalidToken("no session for this refresh token")

        with self._lock_for(session.session_id):
            # Re-read under the lock: a concurrent refresh may have rotated
            # this session between the lookup and here.
            session = self.store.session(session.session_id) or session

            if presented == session.previous_hash and session.previous_hash:
                # The spent token came back. Either a client raced itself or a
                # copy is in circulation, and the two are indistinguishable
                # right now. Assume the expensive one.
                self.store.revoke_sessions(
                    session.identity_id, now, "refresh token reuse detected"
                )
                self._audit(
                    EventKind.TOKEN_REUSE_DETECTED,
                    identity_id=session.identity_id, ip=ip,
                    user_agent=user_agent, session_id=session.session_id,
                    succeeded=False,
                    detail=(
                        "a rotated refresh token was presented again; every "
                        "session for this identity was revoked"
                    ),
                )
                raise InvalidToken("refresh token reuse")

            if not session.active(now):
                raise InvalidToken("session is revoked or expired")

            identity = self.store.identity(session.identity_id)
            if identity is None or not identity.status.can_log_in:
                raise InvalidToken("identity cannot sign in")

            rotated = new_refresh_token()
            session.previous_hash = session.token_hash
            session.token_hash = self.hasher.hash_token(rotated)
            session.rotations += 1
            # Extended, but never past the absolute ceiling.
            session.expires_at = min(
                now + timedelta(seconds=self.config.refresh_ttl_s),
                session.absolute_expires_at or now + timedelta(
                    seconds=self.config.session_max_s
                ),
            )
            self.store.save_session(session)

        membership = self._membership_in(identity.identity_id, session.tenant_id)
        access, expires = self.issuer.issue(
            identity_id=identity.identity_id,
            email=identity.email,
            tenant_id=session.tenant_id,
            user_id=membership.user_id if membership else "",
            role=membership.role if membership else "",
            session_id=session.session_id,
            now=now,
        )
        self._audit(EventKind.TOKEN_REFRESHED, identity_id=identity.identity_id,
                    email=identity.email, ip=ip, session_id=session.session_id,
                    tenant_id=session.tenant_id)

        return TokenPair(
            access_token=access,
            refresh_token=rotated,
            expires_in_s=int((expires - now).total_seconds()),
            session_id=session.session_id,
            tenant_id=session.tenant_id,
        )

    def log_out(self, refresh_token: str, *, ip: str = "") -> bool:
        """End one session. Idempotent."""

        now = self.clock()
        session = self.store.session_by_token_hash(
            self.hasher.hash_token(refresh_token)
        )
        if session is None or session.revoked_at is not None:
            return False
        session.revoked_at = now
        session.revoked_reason = "signed out"
        self.store.save_session(session)
        self._audit(EventKind.SESSION_REVOKED, identity_id=session.identity_id,
                    session_id=session.session_id, ip=ip, detail="signed out")
        return True

    def log_out_everywhere(
        self, identity_id: str, *, ip: str = "", reason: str = "signed out everywhere"
    ) -> int:
        now = self.clock()
        count = self.store.revoke_sessions(identity_id, now, reason)
        self._audit(EventKind.ALL_SESSIONS_REVOKED, identity_id=identity_id,
                    ip=ip, detail=f"{count} sessions revoked: {reason}")
        return count

    def sessions(self, identity_id: str) -> tuple[Session, ...]:
        """Every session, for a "where am I signed in" screen."""
        return self.store.sessions_for(identity_id)

    # -- devices -----------------------------------------------------------

    def _record_device(
        self, identity: Identity, device_id: str, now: datetime, *,
        ip: str, user_agent: str,
    ) -> None:
        """Touch the device row, and warn on one that has never been seen.

        The email is the point of the whole feature. A sign-in from a device
        the owner does not recognise is the earliest signal a stolen password
        gives off, and it is often the only one.
        """

        from .types import Device

        known = self.store.device(device_id)
        label = device_label(user_agent)
        self.store.upsert_device(Device(
            device_id=device_id,
            identity_id=identity.identity_id,
            label=label,
            user_agent=user_agent[:400],
            last_ip=ip,
            first_seen_at=known.first_seen_at if known else now,
            last_seen_at=now,
            revoked_at=known.revoked_at if known else None,
        ))
        if known is not None:
            return

        self._audit(EventKind.DEVICE_FIRST_SEEN,
                    identity_id=identity.identity_id, email=identity.email,
                    ip=ip, user_agent=user_agent, detail=label)
        try:
            self._send(email_mod.new_device_email(
                identity.email, label, now.strftime("%d %B %Y at %H:%M UTC"),
            ))
        except Exception:                                   # noqa: BLE001
            # A notice about a sign-in that has already happened must not fail
            # the sign-in.
            log.warning("could not send a new-device notice to %s",
                        identity.identity_id)

    def devices(self, identity_id: str):
        """Every device that has signed in, most recent first."""
        return self.store.devices_for(identity_id)

    def revoke_device(
        self, identity_id: str, device_id: str, *, ip: str = "",
    ) -> int:
        """Sign out one device and stop its sessions refreshing.

        Revoking the device row alone would not do it: the sessions already
        issued keep refreshing on their own tokens, so the phone stays signed
        in and the user watches a button do nothing.
        """

        now = self.clock()
        device = self.store.device(device_id)
        if device is None or device.identity_id != identity_id:
            raise AuthError("NOT_FOUND", "No such device.", status=404)

        # `upsert_device` deliberately never touches `revoked_at` — otherwise
        # the next sign-in from a revoked device would quietly un-revoke it —
        # so the revocation goes through its own call.
        self.store.set_device_revoked(device_id, now)

        revoked = 0
        for session in self.store.sessions_for(identity_id):
            if session.device_id != device_id or not session.active(now):
                continue
            session.revoked_at = now
            session.revoked_reason = "device signed out"
            self.store.save_session(session)
            revoked += 1

        self._audit(EventKind.DEVICE_REVOKED, identity_id=identity_id,
                    ip=ip, detail=f"{device.label}: {revoked} session(s)")
        return revoked

    def authenticate(self, access_token: str) -> Principal:
        """Verify an access token. The request path's entry point.

        Deliberately does not touch the database: that is the whole reason the
        access token is a signed blob and the whole reason it is short-lived.
        A caller that needs a revocation check on every request wants
        `authenticate_live` and should understand what it costs.
        """

        return self.issuer.verify(access_token, self.clock())

    def authenticate_live(self, access_token: str) -> Principal:
        """Verify the token *and* confirm the session is still alive.

        One database read per request. Worth it on anything destructive —
        deleting a channel, disconnecting an account, changing billing — where
        the fifteen-minute window between a revocation and a token's natural
        expiry is fifteen minutes too long.
        """

        principal = self.authenticate(access_token)
        session = self.store.session(principal.session_id)
        if session is None or not session.active(self.clock()):
            raise InvalidToken("session revoked")
        return principal

    # -- passwords ---------------------------------------------------------

    def request_password_reset(self, email: str, *, ip: str = "") -> None:
        """Send a reset link, or do nothing. The caller cannot tell which."""

        now = self.clock()
        address = normalise_email(email)
        self._limit("reset_request", ip or address, now, ip=ip, email=address)

        identity = self.store.identity_by_email(address)
        if identity is None or identity.status is IdentityStatus.DELETED:
            self._audit(EventKind.PASSWORD_RESET_REQUESTED, email=address,
                        ip=ip, succeeded=False,
                        detail="no identity for this address")
            return

        # Any earlier reset link stops working the moment a new one is asked
        # for. Two live links means an old email in an inbox is still a way in.
        self.store.invalidate_tokens(
            identity.identity_id, TokenKind.PASSWORD_RESET, now
        )
        raw = new_opaque_token()
        self.store.create_token(VerificationToken(
            token_id=f"tok_{uuid.uuid4().hex[:16]}",
            identity_id=identity.identity_id,
            kind=TokenKind.PASSWORD_RESET,
            token_hash=self.hasher.hash_token(raw),
            expires_at=now + timedelta(seconds=self.config.reset_ttl_s),
            created_at=now,
            requested_ip=ip,
        ))
        self._audit(EventKind.PASSWORD_RESET_REQUESTED,
                    identity_id=identity.identity_id, email=address, ip=ip)
        self._send(email_mod.reset_email(
            address,
            f"{self.config.reset_url}?token={raw}",
            self.config.reset_ttl_s // 60,
        ))

    def reset_password(
        self, token: str, new_password: str, *, ip: str = ""
    ) -> Identity:
        """Set a new password from a reset link, and end every session."""

        now = self.clock()
        self._limit("reset", ip or "anonymous", now, ip=ip)
        record = self._spend_token(token, TokenKind.PASSWORD_RESET, now)

        identity = self.store.identity(record.identity_id)
        if identity is None or identity.status is IdentityStatus.DELETED:
            raise AuthError("INVALID_TOKEN", self.config.invalid_link_message)

        try:
            self.config.password_policy.check(new_password, identity.email)
        except WeakPassword as error:
            raise AuthError("WEAK_PASSWORD", str(error)) from error

        identity.password_hash = self.hasher.hash(new_password)
        identity.password_algo = self.config.password_algorithm
        identity.failed_attempts = 0
        identity.locked_until = None
        # Completing a reset proves control of the mailbox, which is the same
        # thing email verification proves. Making someone verify afterwards is
        # a second round trip for evidence already in hand.
        if not identity.verified:
            identity.email_verified_at = now
        if identity.status in (IdentityStatus.PENDING, IdentityStatus.LOCKED):
            identity.status = IdentityStatus.ACTIVE
        identity = self.store.save_identity(identity)

        self.log_out_everywhere(
            identity.identity_id, ip=ip, reason="password reset"
        )
        self._clear_limit("login_email", identity.email)
        self._audit(EventKind.PASSWORD_RESET_COMPLETED,
                    identity_id=identity.identity_id, email=identity.email,
                    ip=ip)
        self._send(email_mod.password_changed_email(identity.email))
        return identity

    def check_password(
        self, identity_id: str, password: str, *, ip: str = "",
    ) -> Identity:
        """Re-verify the password of an already-authenticated identity.

        For the actions that need a second proof of presence — deleting an
        account, and anything else added later with no undo. Rate limited on
        the same bucket as `change_password`, because an endpoint that
        confirms a password without a limit is a password oracle for anyone
        who has stolen a session.

        Split out rather than reusing `change_password` with the same value
        twice: that path rejects an unchanged password with
        `PASSWORD_UNCHANGED`, so the "correct password" case failed and the
        wrong one returned 403 where the caller wanted 401. Two different
        questions deserve two methods.
        """

        now = self.clock()
        self._limit("change_password", identity_id, now, ip=ip)

        identity = self.store.identity(identity_id)
        if identity is None:
            raise AuthError("NOT_FOUND", "No such account.", status=404)
        if not self.hasher.verify(password, identity.password_hash):
            self._audit(EventKind.LOGIN_FAILED, identity_id=identity_id,
                        email=identity.email, ip=ip, succeeded=False,
                        detail="password re-check failed")
            raise AuthError(
                "INVALID_CREDENTIALS", "That password is not correct.",
                status=401,
            )
        return identity

    def change_password(
        self,
        identity_id: str,
        current_password: str,
        new_password: str,
        *,
        ip: str = "",
        keep_current_session: str = "",
    ) -> Identity:
        """Change a password from inside a session.

        The current password is required even though the caller is already
        authenticated: an unattended laptop is the common case, and without
        this check it is a permanent account takeover rather than a session.
        """

        now = self.clock()
        self._limit("change_password", identity_id, now, ip=ip)

        identity = self.store.identity(identity_id)
        if identity is None:
            raise AuthError("NOT_FOUND", "No such account.", status=404)
        if not self.hasher.verify(current_password, identity.password_hash):
            self._audit(EventKind.PASSWORD_CHANGED, identity_id=identity_id,
                        email=identity.email, ip=ip, succeeded=False,
                        detail="current password did not match")
            raise AuthError(
                "INVALID_CREDENTIALS", "That is not your current password.",
                status=403,
            )
        try:
            self.config.password_policy.check(new_password, identity.email)
        except WeakPassword as error:
            raise AuthError("WEAK_PASSWORD", str(error)) from error
        if self.hasher.verify(new_password, identity.password_hash):
            raise AuthError(
                "PASSWORD_UNCHANGED",
                "That is already your password. Choose a different one.",
            )

        identity.password_hash = self.hasher.hash(new_password)
        identity.password_algo = self.config.password_algorithm
        identity = self.store.save_identity(identity)

        # Every other session dies. The one making the change survives, or
        # changing a password would sign you out of the tab you did it in.
        revoked = 0
        for session in self.store.sessions_for(identity_id):
            if session.session_id == keep_current_session:
                continue
            if session.revoked_at is None:
                session.revoked_at = now
                session.revoked_reason = "password changed"
                self.store.save_session(session)
                revoked += 1

        self._audit(EventKind.PASSWORD_CHANGED, identity_id=identity_id,
                    email=identity.email, ip=ip,
                    detail=f"{revoked} other sessions revoked")
        self._send(email_mod.password_changed_email(identity.email))
        return identity

    # -- deletion ----------------------------------------------------------

    def request_deletion(self, identity_id: str, *, ip: str = "") -> Identity:
        """Schedule deletion after a grace period, and sign everything out.

        A grace period rather than an immediate purge, because the request
        arrives both from people who mean it and from people whose account has
        just been taken over. The notification email is what makes the second
        case recoverable.
        """

        now = self.clock()
        identity = self.store.identity(identity_id)
        if identity is None:
            raise AuthError("NOT_FOUND", "No such account.", status=404)

        identity.status = IdentityStatus.PENDING_DELETION
        identity.delete_after = now + timedelta(
            seconds=self.config.deletion_grace_s
        )
        identity = self.store.save_identity(identity)

        self.log_out_everywhere(identity_id, ip=ip, reason="deletion requested")
        self._audit(EventKind.DELETION_REQUESTED, identity_id=identity_id,
                    email=identity.email, ip=ip,
                    detail=f"purge due {identity.delete_after.isoformat()}")
        self._send(email_mod.deletion_requested_email(
            identity.email, identity.delete_after.strftime("%d %B %Y")
        ))
        return identity

    def cancel_deletion(self, identity_id: str, *, ip: str = "") -> Identity:
        identity = self.store.identity(identity_id)
        if identity is None:
            raise AuthError("NOT_FOUND", "No such account.", status=404)
        if identity.status is not IdentityStatus.PENDING_DELETION:
            return identity
        identity.status = (
            IdentityStatus.ACTIVE if identity.verified else IdentityStatus.PENDING
        )
        identity.delete_after = None
        identity = self.store.save_identity(identity)
        self._audit(EventKind.DELETION_CANCELLED, identity_id=identity_id,
                    email=identity.email, ip=ip)
        return identity

    def purge_due(self, now: datetime | None = None) -> list[str]:
        """Complete the deletions whose grace period has ended.

        The row is not dropped. It is overwritten: the email becomes an opaque
        placeholder, the hash is emptied, and the status says `deleted`. That
        keeps foreign keys and the audit trail intact — an audit log with a
        dangling identity id cannot answer "who did this" about anything that
        happened before the deletion — while leaving no personal data behind.

        This is one step of the deletion workflow, not all of it. Clips,
        renders, transcripts and object storage are the tenant's data and are
        purged by the tenant-side workflow; this call is the credential half.
        """

        now = now or self.clock()
        purged: list[str] = []
        for identity in self.store.identities_due_for_deletion(now):
            original = identity.email
            identity.email = f"deleted+{identity.identity_id}@invalid"
            identity.password_hash = ""
            identity.password_algo = ""
            identity.status = IdentityStatus.DELETED
            identity.email_verified_at = None
            identity.delete_after = None
            identity.last_login_at = None
            self.store.save_identity(identity)
            self.store.revoke_sessions(
                identity.identity_id, now, "account deleted"
            )
            self.store.remove_memberships(identity.identity_id)
            self._audit(
                EventKind.DELETION_COMPLETED,
                identity_id=identity.identity_id,
                # The address is deliberately not recorded here: the point of
                # the purge is that it stops existing in this system.
                email="", detail=f"identity purged ({len(original)} char address)",
            )
            purged.append(identity.identity_id)
        return purged

    # -- memberships -------------------------------------------------------

    def add_membership(
        self, identity_id: str, tenant_id: str, user_id: str, role: str,
        tenant_name: str = "",
    ) -> Membership:
        """Link an identity to a tenant. The join between auth and the app."""

        if self.store.identity(identity_id) is None:
            raise AuthError("NOT_FOUND", "No such account.", status=404)
        return self.store.add_membership(Membership(
            user_id=user_id, tenant_id=tenant_id, identity_id=identity_id,
            role=role, tenant_name=tenant_name,
        ))

    def _membership_in(self, identity_id: str, tenant_id: str) -> Membership | None:
        for membership in self.store.memberships(identity_id):
            if membership.tenant_id == tenant_id and membership.active:
                return membership
        return None

    # -- internals ---------------------------------------------------------

    def _issue_verification(self, identity: Identity, *, ip: str) -> str:
        now = self.clock()
        self.store.invalidate_tokens(
            identity.identity_id, TokenKind.EMAIL_VERIFICATION, now
        )
        raw = new_opaque_token()
        self.store.create_token(VerificationToken(
            token_id=f"tok_{uuid.uuid4().hex[:16]}",
            identity_id=identity.identity_id,
            kind=TokenKind.EMAIL_VERIFICATION,
            token_hash=self.hasher.hash_token(raw),
            expires_at=now + timedelta(seconds=self.config.verification_ttl_s),
            created_at=now,
            requested_ip=ip,
        ))
        self._send(email_mod.verification_email(
            identity.email,
            f"{self.config.verification_url}?token={raw}",
            self.config.verification_ttl_s // 3600,
        ))
        return raw

    def _spend_token(
        self, token: str, kind: TokenKind, now: datetime
    ) -> VerificationToken:
        """Look a token up, mark it used, and refuse everything else.

        Marking used happens here rather than at the call site, so no flow can
        forget it and leave a reset link working twice.
        """

        if not token:
            raise AuthError("INVALID_TOKEN", self.config.invalid_link_message)
        record = self.store.token_by_hash(self.hasher.hash_token(token))
        if record is None or record.kind is not kind or not record.usable(now):
            raise AuthError("INVALID_TOKEN", self.config.invalid_link_message)
        record.used_at = now
        self.store.save_token(record)
        return record

    @staticmethod
    def _bucket(action: str, key: str) -> str:
        """The rate-limit bucket key.

        One function, because the counter and its reset must agree. Building
        the key inline in both places is how `reset_rate_limit` ends up
        clearing a bucket nothing ever incremented — the limiter then looks
        correct and quietly never resets.
        """

        return f"{action}:{key}"

    def _limit(
        self, action: str, key: str, now: datetime, *, ip: str = "",
        email: str = "",
    ) -> None:
        rule = self.config.rate_limits.get(action)
        if rule is None:
            return
        limit, window_s = rule
        count = self.store.hit_rate_limit(
            self._bucket(action, key), action, window_s, now
        )
        if count > limit:
            self._audit(EventKind.RATE_LIMITED, email=email, ip=ip,
                        succeeded=False,
                        detail=f"{action} limit {limit}/{window_s}s exceeded "
                               f"({count} attempts)")
            raise RateLimited(
                "Too many attempts. Wait a moment and try again.",
                retry_after_s=float(window_s),
            )

    def _clear_limit(self, action: str, key: str) -> None:
        self.store.reset_rate_limit(self._bucket(action, key), action)

    def _lock_for(self, session_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(session_id, threading.Lock())

    def _audit(self, kind: EventKind, **fields: Any) -> None:
        self.store.record_event(AuthEvent(
            event_id=f"evt_{uuid.uuid4().hex[:16]}",
            kind=kind,
            at=self.clock(),
            **fields,
        ))

    def _send(self, message: Any) -> None:
        try:
            self.sender.send(message)
        except Exception:                                   # noqa: BLE001
            # A mail outage must not fail a signup or a password reset: the
            # account is created either way, and the user can ask for another
            # link. Failing the whole call would leave an identity that
            # exists, cannot be verified, and cannot be re-registered.
            self._audit(EventKind.RATE_LIMITED, succeeded=False,
                        detail=f"email delivery failed for {message.kind}")

    def _require_email_shape(self, address: str) -> None:
        """A deliberately loose check.

        The only authority on whether an address works is whether mail to it
        arrives, which is what the verification step is for. A strict regex
        here rejects valid addresses — plus-tags, new TLDs, unicode locals —
        and buys nothing the verification email does not already prove.
        """

        if address.count("@") != 1 or address.startswith("@") or address.endswith("@"):
            raise AuthError("INVALID_EMAIL", "That does not look like an email address.")
        local, _, domain = address.partition("@")
        if not local or "." not in domain or domain.endswith("."):
            raise AuthError("INVALID_EMAIL", "That does not look like an email address.")
        if len(address) > 320:
            raise AuthError("INVALID_EMAIL", "That address is too long.")
