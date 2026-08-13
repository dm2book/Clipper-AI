"""Authentication, with real bcrypt and real JWTs.

## What is real here

Every hash in this file is produced by `bcrypt` and every token by `PyJWT`.
Nothing is stubbed: `AuthFlows` runs the whole suite twice, once against
`MemoryAuthStore` and once against PostgreSQL, for the same reason
`test_store_contract.py` does — the fast in-memory results are only evidence
about production because the same assertions pass on the database.

The bcrypt cost is lowered to 4 for speed. That is the *only* concession, it
changes no behaviour under test, and `ProductionReadinessTest` asserts that a
cost that low is refused by the config guard.

## The assertions worth reading

Most of this file is ordinary flow coverage. The parts that encode a security
property rather than a feature are:

* `test_signup_says_the_same_thing_whether_or_not_the_address_is_taken`
* `test_a_login_for_an_unknown_address_takes_as_long_as_a_real_one`
* `test_reusing_a_rotated_refresh_token_kills_the_whole_family`
* `test_a_reset_link_works_once`
* `test_resetting_a_password_ends_every_session`
* `test_an_algorithm_confusion_token_is_refused`
* `test_the_audit_log_never_contains_a_token_or_a_password`

Each of those is a bug someone ships by writing the obvious implementation,
and each is invisible in a feature test.
"""

from __future__ import annotations

import os
import threading
import time
import unittest
import uuid
from datetime import UTC, datetime, timedelta

from clipforge.auth import (
    AccessTokenIssuer,
    AuthConfig,
    AuthError,
    AuthService,
    EventKind,
    IdentityStatus,
    InvalidToken,
    Keyring,
    MemoryAuthStore,
    MisconfiguredAuth,
    PasswordHasher,
    PasswordPolicy,
    RateLimited,
    RecordingEmailSender,
    SigningKey,
    WeakPassword,
    normalise_email,
)
from clipforge.auth.config import config_from_env, describe_environment
from clipforge.auth.tokens import new_refresh_token

PASSWORD = "a perfectly ordinary long passphrase"
OTHER = "a different perfectly ordinary phrase"
EMAIL = "dana@example.com"

#: Cost 4 rather than 12. The only concession to speed in this file; the
#: construction, the verification and the upgrade path are all unchanged.
FAST = PasswordPolicy(rounds=4)


def _config(**kwargs) -> AuthConfig:
    defaults = dict(
        password_policy=FAST,
        require_verified_email=True,
        verification_url="https://app.test/verify",
        reset_url="https://app.test/reset",
    )
    defaults.update(kwargs)
    return AuthConfig(**defaults)


def _token_from(link: str) -> str:
    return link.split("token=", 1)[1]


# ---------------------------------------------------------------------------
# The contract, run against both stores
# ---------------------------------------------------------------------------


class AuthFlows:
    """Every flow. Mixed into the two cases at the bottom of the file."""

    def make_store(self):                        # pragma: no cover - overridden
        raise NotImplementedError

    def setUp(self) -> None:
        self.store = self.make_store()
        self.addCleanup(self.store.close)
        self.sender = RecordingEmailSender()
        self.config = _config()
        self.now = datetime.now(UTC).replace(microsecond=0)
        self.service = AuthService(
            self.store,
            AccessTokenIssuer(self.config.keyring, ttl_s=self.config.access_ttl_s),
            config=self.config,
            hasher=PasswordHasher(FAST),
            sender=self.sender,
            clock=lambda: self.now,
        )

    def advance(self, **delta) -> None:
        self.now = self.now + timedelta(**delta)

    # -- helpers -----------------------------------------------------------

    def register(self, email: str = EMAIL, password: str = PASSWORD,
                 verify: bool = True):
        result = self.service.sign_up(email, password, ip="203.0.113.7")
        if verify:
            link = self.sender.links_for(email)[-1]
            self.service.verify_email(_token_from(link))
        return result

    def logged_in(self, email: str = EMAIL, password: str = PASSWORD):
        self.register(email, password)
        return self.service.log_in(email, password, ip="203.0.113.7")

    # -- signup ------------------------------------------------------------

    def test_signup_creates_a_pending_identity_and_mails_a_link(self) -> None:
        result = self.service.sign_up(EMAIL, PASSWORD)

        self.assertTrue(result.created)
        identity = self.store.identity_by_email(EMAIL)
        self.assertEqual(identity.status, IdentityStatus.PENDING)
        self.assertFalse(identity.verified)
        self.assertTrue(self.sender.links_for(EMAIL))

    def test_the_password_is_never_stored_in_any_recoverable_form(self) -> None:
        self.service.sign_up(EMAIL, PASSWORD)
        identity = self.store.identity_by_email(EMAIL)

        self.assertNotIn(PASSWORD, identity.password_hash)
        self.assertTrue(identity.password_hash.startswith("$2b$"))
        self.assertNotIn(PASSWORD, str(identity.to_dict()))
        self.assertNotIn("password_hash", identity.to_dict())

    def test_signup_says_the_same_thing_whether_or_not_the_address_is_taken(
        self,
    ) -> None:
        """The response must not reveal that an address is registered — that
        is a membership oracle for every customer's staff list."""

        first = self.service.sign_up(EMAIL, PASSWORD)
        second = self.service.sign_up(EMAIL, OTHER)

        self.assertEqual(first.message, second.message)
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        # And the second attempt did not overwrite the first's password.
        self.assertTrue(
            self.service.hasher.verify(
                PASSWORD, self.store.identity_by_email(EMAIL).password_hash
            )
        )

    def test_a_duplicate_signup_warns_the_real_owner(self) -> None:
        """This is what makes the identical response safe: the person who owns
        the address finds out, and the person who typed it learns nothing."""

        self.service.sign_up(EMAIL, PASSWORD)
        self.sender.clear()
        self.service.sign_up(EMAIL, OTHER)

        message = self.sender.last_to(EMAIL)
        self.assertEqual(message.kind, "signup_duplicate")
        self.assertEqual(message.link, "", "the warning must carry no token")

    def test_a_weak_password_is_refused_before_the_address_is_checked(self) -> None:
        """Otherwise 'too short' versus the generic message is the oracle the
        identical response was supposed to close."""

        self.service.sign_up(EMAIL, PASSWORD)
        with self.assertRaises(AuthError) as caught:
            self.service.sign_up(EMAIL, "short")
        self.assertEqual(caught.exception.code, "WEAK_PASSWORD")

    def test_emails_are_normalised_but_not_mangled(self) -> None:
        self.service.sign_up("  Dana@Example.COM ", PASSWORD)
        self.assertIsNotNone(self.store.identity_by_email(EMAIL))
        # Not Gmail's rules: these are two different addresses at most hosts.
        self.assertEqual(normalise_email("first.last@x.com"), "first.last@x.com")
        self.assertEqual(normalise_email("a+tag@x.com"), "a+tag@x.com")

    def test_a_malformed_address_is_refused(self) -> None:
        for bad in ("", "nobody", "@example.com", "a@b", "a@@b.com"):
            with self.assertRaises(AuthError, msg=bad):
                self.service.sign_up(bad, PASSWORD)

    # -- verification ------------------------------------------------------

    def test_verification_activates_the_account(self) -> None:
        self.service.sign_up(EMAIL, PASSWORD)
        token = _token_from(self.sender.links_for(EMAIL)[-1])

        identity = self.service.verify_email(token)
        self.assertTrue(identity.verified)
        self.assertEqual(identity.status, IdentityStatus.ACTIVE)

    def test_a_verification_link_works_once(self) -> None:
        self.service.sign_up(EMAIL, PASSWORD)
        token = _token_from(self.sender.links_for(EMAIL)[-1])
        self.service.verify_email(token)

        with self.assertRaises(AuthError) as caught:
            self.service.verify_email(token)
        self.assertEqual(caught.exception.code, "INVALID_TOKEN")

    def test_an_expired_verification_link_is_refused(self) -> None:
        self.service.sign_up(EMAIL, PASSWORD)
        token = _token_from(self.sender.links_for(EMAIL)[-1])
        self.advance(seconds=self.config.verification_ttl_s + 1)

        with self.assertRaises(AuthError):
            self.service.verify_email(token)

    def test_resending_invalidates_the_previous_link(self) -> None:
        """Two live links means an old email in an inbox is still a way in."""

        self.service.sign_up(EMAIL, PASSWORD)
        first = _token_from(self.sender.links_for(EMAIL)[-1])
        self.service.resend_verification(EMAIL)
        second = _token_from(self.sender.links_for(EMAIL)[-1])

        self.assertNotEqual(first, second)
        with self.assertRaises(AuthError):
            self.service.verify_email(first)
        self.assertTrue(self.service.verify_email(second).verified)

    def test_the_stored_token_is_a_hash_not_the_token(self) -> None:
        self.service.sign_up(EMAIL, PASSWORD)
        raw = _token_from(self.sender.links_for(EMAIL)[-1])

        self.assertIsNone(self.store.token_by_hash(raw),
                          "the raw token was stored verbatim")
        self.assertIsNotNone(
            self.store.token_by_hash(PasswordHasher.hash_token(raw))
        )

    def test_resending_for_an_unknown_address_reveals_nothing(self) -> None:
        self.service.resend_verification("nobody@example.com")
        self.assertEqual(self.sender.deliveries, [])

    # -- login -------------------------------------------------------------

    def test_login_returns_a_usable_token_pair(self) -> None:
        result = self.logged_in()

        self.assertTrue(result.tokens.access_token)
        self.assertTrue(result.tokens.refresh_token)
        self.assertEqual(result.tokens.expires_in_s, self.config.access_ttl_s)
        principal = self.service.authenticate(result.tokens.access_token)
        self.assertEqual(principal.email, EMAIL)
        self.assertEqual(principal.session_id, result.tokens.session_id)

    def test_a_wrong_password_and_an_unknown_address_are_indistinguishable(
        self,
    ) -> None:
        self.register()

        with self.assertRaises(AuthError) as wrong:
            self.service.log_in(EMAIL, "not the password at all")
        with self.assertRaises(AuthError) as unknown:
            self.service.log_in("nobody@example.com", PASSWORD)

        self.assertEqual(wrong.exception.code, unknown.exception.code)
        self.assertEqual(wrong.exception.message, unknown.exception.message)
        self.assertEqual(wrong.exception.status, unknown.exception.status)

    def test_a_login_for_an_unknown_address_takes_as_long_as_a_real_one(
        self,
    ) -> None:
        """A fast 'no' means 'no such account'. The dummy verification exists
        to make the two indistinguishable from outside."""

        self.register()

        def elapsed(email: str) -> float:
            start = time.perf_counter()
            try:
                self.service.log_in(email, "the wrong password entirely")
            except AuthError:
                pass
            return time.perf_counter() - start

        real = min(elapsed(EMAIL) for _ in range(3))
        missing = min(elapsed(f"ghost{uuid.uuid4().hex[:6]}@example.com")
                      for _ in range(3))

        # Generous: this asserts the same order of magnitude, not identical
        # timing. Without the dummy hash the miss is ~1000x faster.
        self.assertGreater(missing, real / 4,
                           "the unknown-address path skipped the hash")

    def test_an_unverified_account_cannot_sign_in_by_default(self) -> None:
        self.service.sign_up(EMAIL, PASSWORD)
        with self.assertRaises(AuthError) as caught:
            self.service.log_in(EMAIL, PASSWORD)
        self.assertEqual(caught.exception.code, "EMAIL_NOT_VERIFIED")

    def test_repeated_failures_lock_the_account_temporarily(self) -> None:
        """The two controls are layered and reachable in that order.

        Per-address rate limiting stops a burst inside one window; the lockout
        catches an attacker who spreads attempts across windows to stay under
        it. So the lockout is deliberately *not* reachable in a single burst,
        and getting there means waiting out the limiter — which is the whole
        point of having both.
        """

        self.register()
        _, window = self.config.rate_limits["login_email"]
        attempts = 0
        while attempts < self.config.max_failed_attempts:
            for _ in range(3):
                if attempts >= self.config.max_failed_attempts:
                    break
                with self.assertRaises(AuthError):
                    self.service.log_in(EMAIL, "wrong")
                attempts += 1
            self.advance(seconds=window + 1)

        with self.assertRaises(AuthError) as caught:
            self.service.log_in(EMAIL, PASSWORD)
        self.assertEqual(caught.exception.code, "ACCOUNT_LOCKED")

        self.advance(seconds=self.config.lockout_s + 1)
        self.assertTrue(self.service.log_in(EMAIL, PASSWORD).tokens.access_token)

    def test_a_successful_login_clears_the_failure_count(self) -> None:
        self.register()
        for _ in range(3):
            with self.assertRaises(AuthError):
                self.service.log_in(EMAIL, "wrong")
        self.service.log_in(EMAIL, PASSWORD)
        self.assertEqual(self.store.identity_by_email(EMAIL).failed_attempts, 0)

    def test_the_hash_is_upgraded_when_the_cost_factor_rises(self) -> None:
        """The one moment the plaintext is legitimately in memory. Without
        this the fleet never migrates off an obsolete cost."""

        self.register()
        before = self.store.identity_by_email(EMAIL).password_hash
        self.assertIn("$04$", before)

        self.service.hasher = PasswordHasher(PasswordPolicy(rounds=6))
        self.service.log_in(EMAIL, PASSWORD)

        after = self.store.identity_by_email(EMAIL).password_hash
        self.assertIn("$06$", after)
        self.assertNotEqual(before, after)
        # And the new hash still verifies the same password.
        self.assertTrue(self.service.hasher.verify(PASSWORD, after))

    # -- memberships -------------------------------------------------------

    def test_a_session_carries_the_only_membership(self) -> None:
        self.register()
        identity = self.store.identity_by_email(EMAIL)
        self.service.add_membership(
            identity.identity_id, "ten_acme", "usr_1", "admin", "Acme"
        )

        result = self.service.log_in(EMAIL, PASSWORD)
        self.assertEqual(result.tokens.tenant_id, "ten_acme")
        principal = self.service.authenticate(result.tokens.access_token)
        self.assertEqual(principal.role, "admin")
        self.assertEqual(principal.user_id, "usr_1")

    def test_two_memberships_require_the_caller_to_choose(self) -> None:
        """Guessing would put someone in the wrong workspace silently."""

        self.register()
        identity = self.store.identity_by_email(EMAIL)
        self.service.add_membership(identity.identity_id, "ten_a", "u_a", "admin")
        self.service.add_membership(identity.identity_id, "ten_b", "u_b", "viewer")

        with self.assertRaises(AuthError) as caught:
            self.service.log_in(EMAIL, PASSWORD)
        self.assertEqual(caught.exception.code, "TENANT_REQUIRED")

        chosen = self.service.log_in(EMAIL, PASSWORD, tenant_id="ten_b")
        self.assertEqual(chosen.tokens.tenant_id, "ten_b")
        self.assertEqual(
            self.service.authenticate(chosen.tokens.access_token).role, "viewer"
        )

    def test_asking_for_a_workspace_you_are_not_in_is_refused(self) -> None:
        self.register()
        identity = self.store.identity_by_email(EMAIL)
        self.service.add_membership(identity.identity_id, "ten_a", "u_a", "admin")

        with self.assertRaises(AuthError) as caught:
            self.service.log_in(EMAIL, PASSWORD, tenant_id="ten_someone_else")
        self.assertEqual(caught.exception.code, "NO_SUCH_MEMBERSHIP")

    def test_one_identity_holds_several_memberships(self) -> None:
        """The whole reason identity and user are separate: the same human
        legitimately works in four client workspaces."""

        self.register()
        identity = self.store.identity_by_email(EMAIL)
        for index in range(4):
            self.service.add_membership(
                identity.identity_id, f"ten_{index}", f"u_{index}", "operator"
            )
        self.assertEqual(len(self.store.memberships(identity.identity_id)), 4)

    # -- refresh -----------------------------------------------------------

    def test_refresh_rotates_the_token(self) -> None:
        result = self.logged_in()
        rotated = self.service.refresh(result.tokens.refresh_token)

        self.assertNotEqual(rotated.refresh_token, result.tokens.refresh_token)
        self.assertEqual(rotated.session_id, result.tokens.session_id)
        self.assertTrue(self.service.authenticate(rotated.access_token))

    def test_the_old_refresh_token_stops_working(self) -> None:
        result = self.logged_in()
        self.service.refresh(result.tokens.refresh_token)

        with self.assertRaises(InvalidToken):
            self.service.refresh(result.tokens.refresh_token)

    def test_reusing_a_rotated_refresh_token_kills_the_whole_family(self) -> None:
        """A replayed token is either a client racing itself or a stolen copy,
        and the two are indistinguishable at the time. Assume the worse one."""

        result = self.logged_in()
        rotated = self.service.refresh(result.tokens.refresh_token)

        with self.assertRaises(InvalidToken):
            self.service.refresh(result.tokens.refresh_token)

        # The thief's replay killed the legitimate user's token too. That is
        # the intended outcome: the real user signs in again, the copy is dead.
        with self.assertRaises(InvalidToken):
            self.service.refresh(rotated.refresh_token)

        events = [e.kind for e in self.store.events_for()]
        self.assertIn(EventKind.TOKEN_REUSE_DETECTED, events)

    def test_a_revoked_session_cannot_refresh(self) -> None:
        result = self.logged_in()
        self.service.log_out(result.tokens.refresh_token)

        with self.assertRaises(InvalidToken):
            self.service.refresh(result.tokens.refresh_token)

    def test_an_unknown_refresh_token_is_refused(self) -> None:
        with self.assertRaises(InvalidToken):
            self.service.refresh(new_refresh_token())

    def test_a_session_cannot_outlive_its_absolute_ceiling(self) -> None:
        """Without a ceiling, a session that keeps refreshing lives for ever,
        and so does a stolen one."""

        result = self.logged_in()
        token = result.tokens.refresh_token
        self.advance(seconds=self.config.session_max_s + 1)

        with self.assertRaises(InvalidToken):
            self.service.refresh(token)

    def test_an_idle_session_expires(self) -> None:
        result = self.logged_in()
        self.advance(seconds=self.config.refresh_ttl_s + 1)
        with self.assertRaises(InvalidToken):
            self.service.refresh(result.tokens.refresh_token)

    # -- logout ------------------------------------------------------------

    def test_logging_out_ends_one_session_and_leaves_the_others(self) -> None:
        first = self.logged_in()
        second = self.service.log_in(EMAIL, PASSWORD)

        self.service.log_out(first.tokens.refresh_token)

        with self.assertRaises(InvalidToken):
            self.service.refresh(first.tokens.refresh_token)
        self.assertTrue(self.service.refresh(second.tokens.refresh_token))

    def test_logging_out_everywhere_ends_all_of_them(self) -> None:
        first = self.logged_in()
        second = self.service.log_in(EMAIL, PASSWORD)
        identity = self.store.identity_by_email(EMAIL)

        self.assertEqual(self.service.log_out_everywhere(identity.identity_id), 2)
        for tokens in (first.tokens, second.tokens):
            with self.assertRaises(InvalidToken):
                self.service.refresh(tokens.refresh_token)

    def test_logging_out_twice_is_not_an_error(self) -> None:
        result = self.logged_in()
        self.assertTrue(self.service.log_out(result.tokens.refresh_token))
        self.assertFalse(self.service.log_out(result.tokens.refresh_token))

    def test_an_access_token_survives_revocation_until_it_expires(self) -> None:
        """Stated as a test because it is the deliberate trade a stateless
        access token makes, and the reason `authenticate_live` exists."""

        result = self.logged_in()
        identity = self.store.identity_by_email(EMAIL)
        self.service.log_out_everywhere(identity.identity_id)

        # Still valid: nothing checked the database.
        self.assertTrue(self.service.authenticate(result.tokens.access_token))
        # And refused the moment something does.
        with self.assertRaises(InvalidToken):
            self.service.authenticate_live(result.tokens.access_token)

    # -- password reset ----------------------------------------------------

    def test_a_reset_link_sets_a_new_password(self) -> None:
        self.register()
        self.service.request_password_reset(EMAIL)
        token = _token_from(self.sender.links_for(EMAIL)[-1])

        self.service.reset_password(token, OTHER)

        with self.assertRaises(AuthError):
            self.service.log_in(EMAIL, PASSWORD)
        self.assertTrue(self.service.log_in(EMAIL, OTHER).tokens.access_token)

    def test_a_reset_link_works_once(self) -> None:
        self.register()
        self.service.request_password_reset(EMAIL)
        token = _token_from(self.sender.links_for(EMAIL)[-1])
        self.service.reset_password(token, OTHER)

        with self.assertRaises(AuthError):
            self.service.reset_password(token, "a third different passphrase")

    def test_requesting_a_reset_invalidates_the_previous_link(self) -> None:
        self.register()
        self.service.request_password_reset(EMAIL)
        first = _token_from(self.sender.links_for(EMAIL)[-1])
        self.service.request_password_reset(EMAIL)
        second = _token_from(self.sender.links_for(EMAIL)[-1])

        with self.assertRaises(AuthError):
            self.service.reset_password(first, OTHER)
        self.assertTrue(self.service.reset_password(second, OTHER))

    def test_an_expired_reset_link_is_refused(self) -> None:
        self.register()
        self.service.request_password_reset(EMAIL)
        token = _token_from(self.sender.links_for(EMAIL)[-1])
        self.advance(seconds=self.config.reset_ttl_s + 1)

        with self.assertRaises(AuthError):
            self.service.reset_password(token, OTHER)

    def test_resetting_a_password_ends_every_session(self) -> None:
        """Without this, 'reset your password' is a gesture: the attacker's
        refresh token outlives the reset."""

        result = self.logged_in()
        self.service.request_password_reset(EMAIL)
        token = _token_from(self.sender.links_for(EMAIL)[-1])
        self.service.reset_password(token, OTHER)

        with self.assertRaises(InvalidToken):
            self.service.refresh(result.tokens.refresh_token)

    def test_a_reset_request_for_an_unknown_address_reveals_nothing(self) -> None:
        self.service.request_password_reset("nobody@example.com")
        self.assertEqual(self.sender.deliveries, [])

    def test_a_reset_unlocks_a_locked_account(self) -> None:
        """Otherwise the remedy offered on the lockout screen does not work.

        A reset clears the failure count *and* the per-address rate limit:
        somebody who has just proved control of the mailbox should not still
        be serving out a limiter aimed at whoever was guessing at them.
        """

        self.register()
        limit, window = self.config.rate_limits["login_email"]
        for _ in range(limit):
            with self.assertRaises(AuthError):
                self.service.log_in(EMAIL, "wrong")

        self.service.request_password_reset(EMAIL)
        token = _token_from(self.sender.links_for(EMAIL)[-1])
        self.service.reset_password(token, OTHER)

        self.assertTrue(self.service.log_in(EMAIL, OTHER).tokens.access_token)

    def test_a_reset_verifies_the_address(self) -> None:
        """Completing a reset proves control of the mailbox, which is what
        verification proves. Asking again is a round trip for nothing."""

        self.service.sign_up(EMAIL, PASSWORD)
        self.service.request_password_reset(EMAIL)
        token = _token_from(self.sender.links_for(EMAIL)[-1])
        identity = self.service.reset_password(token, OTHER)

        self.assertTrue(identity.verified)
        self.assertEqual(identity.status, IdentityStatus.ACTIVE)

    def test_a_weak_new_password_is_refused(self) -> None:
        self.register()
        self.service.request_password_reset(EMAIL)
        token = _token_from(self.sender.links_for(EMAIL)[-1])

        with self.assertRaises(AuthError) as caught:
            self.service.reset_password(token, "abc")
        self.assertEqual(caught.exception.code, "WEAK_PASSWORD")

    def test_a_reset_notifies_the_owner(self) -> None:
        """The notification is how someone finds out their account was taken
        over, at the one moment it is still recoverable."""

        self.register()
        self.service.request_password_reset(EMAIL)
        token = _token_from(self.sender.links_for(EMAIL)[-1])
        self.sender.clear()
        self.service.reset_password(token, OTHER)

        self.assertEqual(self.sender.last_to(EMAIL).kind, "password_changed")

    # -- change password ---------------------------------------------------

    def test_changing_a_password_requires_the_current_one(self) -> None:
        """An unattended laptop is a session; without this check it is a
        permanent takeover."""

        result = self.logged_in()
        identity = self.store.identity_by_email(EMAIL)

        with self.assertRaises(AuthError) as caught:
            self.service.change_password(identity.identity_id, "wrong", OTHER)
        self.assertEqual(caught.exception.code, "INVALID_CREDENTIALS")

        self.service.change_password(identity.identity_id, PASSWORD, OTHER)
        self.assertTrue(self.service.log_in(EMAIL, OTHER))

    def test_changing_a_password_keeps_the_current_session_and_kills_others(
        self,
    ) -> None:
        first = self.logged_in()
        second = self.service.log_in(EMAIL, PASSWORD)
        identity = self.store.identity_by_email(EMAIL)

        self.service.change_password(
            identity.identity_id, PASSWORD, OTHER,
            keep_current_session=second.tokens.session_id,
        )

        with self.assertRaises(InvalidToken):
            self.service.refresh(first.tokens.refresh_token)
        self.assertTrue(self.service.refresh(second.tokens.refresh_token))

    def test_the_new_password_must_differ_from_the_old(self) -> None:
        self.logged_in()
        identity = self.store.identity_by_email(EMAIL)
        with self.assertRaises(AuthError) as caught:
            self.service.change_password(identity.identity_id, PASSWORD, PASSWORD)
        self.assertEqual(caught.exception.code, "PASSWORD_UNCHANGED")

    # -- deletion ----------------------------------------------------------

    def test_deletion_is_scheduled_not_immediate(self) -> None:
        result = self.logged_in()
        identity = self.store.identity_by_email(EMAIL)

        self.service.request_deletion(identity.identity_id)
        stored = self.store.identity(identity.identity_id)

        self.assertEqual(stored.status, IdentityStatus.PENDING_DELETION)
        self.assertIsNotNone(stored.delete_after)
        # Signed out immediately, even though the data is still there.
        with self.assertRaises(InvalidToken):
            self.service.refresh(result.tokens.refresh_token)

    def test_signing_in_cancels_a_pending_deletion(self) -> None:
        """Someone who comes back inside the grace period has changed their
        mind. Making them find a settings page is a way to lose the account."""

        self.register()
        identity = self.store.identity_by_email(EMAIL)
        self.service.request_deletion(identity.identity_id)

        self.service.log_in(EMAIL, PASSWORD)
        stored = self.store.identity(identity.identity_id)
        self.assertEqual(stored.status, IdentityStatus.ACTIVE)
        self.assertIsNone(stored.delete_after)

    def test_the_purge_only_takes_accounts_past_the_grace_period(self) -> None:
        self.register()
        identity = self.store.identity_by_email(EMAIL)
        self.service.request_deletion(identity.identity_id)

        self.assertEqual(self.service.purge_due(self.now), [])
        self.advance(seconds=self.config.deletion_grace_s + 1)
        self.assertEqual(self.service.purge_due(self.now), [identity.identity_id])

    def test_a_purge_overwrites_the_personal_data_and_keeps_the_row(self) -> None:
        """The row survives so foreign keys and the audit trail stay intact —
        a log with a dangling identity id cannot answer 'who did this'."""

        self.register()
        identity = self.store.identity_by_email(EMAIL)
        self.service.add_membership(
            identity.identity_id, "ten_acme", "usr_1", "admin"
        )
        self.service.request_deletion(identity.identity_id)
        self.advance(seconds=self.config.deletion_grace_s + 1)
        self.service.purge_due(self.now)

        purged = self.store.identity(identity.identity_id)
        self.assertIsNotNone(purged)
        self.assertEqual(purged.status, IdentityStatus.DELETED)
        self.assertEqual(purged.password_hash, "")
        self.assertNotIn("dana", purged.email)
        self.assertIsNone(self.store.identity_by_email(EMAIL))
        self.assertEqual(self.store.memberships(identity.identity_id), ())

    def test_a_purged_account_cannot_sign_in(self) -> None:
        self.register()
        identity = self.store.identity_by_email(EMAIL)
        self.service.request_deletion(identity.identity_id)
        self.advance(seconds=self.config.deletion_grace_s + 1)
        self.service.purge_due(self.now)

        with self.assertRaises(AuthError):
            self.service.log_in(EMAIL, PASSWORD)

    def test_the_address_is_free_again_after_a_purge(self) -> None:
        self.register()
        identity = self.store.identity_by_email(EMAIL)
        self.service.request_deletion(identity.identity_id)
        self.advance(seconds=self.config.deletion_grace_s + 1)
        self.service.purge_due(self.now)

        fresh = self.service.sign_up(EMAIL, OTHER)
        self.assertTrue(fresh.created)

    # -- rate limiting -----------------------------------------------------

    def test_login_attempts_are_limited_per_address(self) -> None:
        """Stops a botnet spread over thousands of hosts grinding one account,
        which a per-IP limit alone does not see."""

        self.register()
        limit, _ = self.config.rate_limits["login_email"]
        for index in range(limit):
            with self.assertRaises(AuthError):
                self.service.log_in(EMAIL, "wrong", ip=f"198.51.100.{index}")

        with self.assertRaises(RateLimited):
            self.service.log_in(EMAIL, "wrong", ip="198.51.100.200")

    def test_login_attempts_are_limited_per_ip(self) -> None:
        """Stops one host working through a list of addresses, which a
        per-address limit alone does not see."""

        limit, _ = self.config.rate_limits["login_ip"]
        for index in range(limit):
            with self.assertRaises(AuthError):
                self.service.log_in(f"nobody{index}@example.com", "x",
                                    ip="198.51.100.9")

        with self.assertRaises(RateLimited):
            self.service.log_in("another@example.com", "x", ip="198.51.100.9")

    def test_the_window_rolls(self) -> None:
        limit, window = self.config.rate_limits["login_email"]
        self.register()
        for _ in range(limit):
            with self.assertRaises(AuthError):
                self.service.log_in(EMAIL, "wrong")
        with self.assertRaises(RateLimited):
            self.service.log_in(EMAIL, "wrong")

        self.advance(seconds=window + 1)
        with self.assertRaises(AuthError) as caught:
            self.service.log_in(EMAIL, "wrong")
        self.assertNotIsInstance(caught.exception, RateLimited)

    def test_a_rate_limit_says_when_to_come_back(self) -> None:
        limit, window = self.config.rate_limits["signup"]
        for index in range(limit):
            self.service.sign_up(f"user{index}@example.com", PASSWORD,
                                 ip="198.51.100.5")
        with self.assertRaises(RateLimited) as caught:
            self.service.sign_up("one-too-many@example.com", PASSWORD,
                                 ip="198.51.100.5")
        self.assertEqual(caught.exception.retry_after_s, float(window))
        self.assertEqual(caught.exception.status, 429)

    def test_a_successful_login_clears_the_address_limit(self) -> None:
        self.register()
        for _ in range(3):
            with self.assertRaises(AuthError):
                self.service.log_in(EMAIL, "wrong")
        self.service.log_in(EMAIL, PASSWORD)

        limit, _ = self.config.rate_limits["login_email"]
        for _ in range(limit - 1):
            with self.assertRaises(AuthError) as caught:
                self.service.log_in(EMAIL, "wrong")
            self.assertNotIsInstance(caught.exception, RateLimited)

    # -- audit -------------------------------------------------------------

    def test_every_interesting_event_is_recorded(self) -> None:
        self.register()
        self.service.log_in(EMAIL, PASSWORD)
        with self.assertRaises(AuthError):
            self.service.log_in(EMAIL, "wrong")

        kinds = {e.kind for e in self.store.events_for()}
        self.assertLessEqual(
            {EventKind.SIGNUP_STARTED, EventKind.EMAIL_VERIFIED,
             EventKind.LOGIN_SUCCEEDED, EventKind.LOGIN_FAILED},
            kinds,
        )

    def test_a_failed_login_records_the_address_even_with_no_account(self) -> None:
        """Someone working through a list of addresses is a pattern worth
        being able to see, and there is no identity to hang it on."""

        with self.assertRaises(AuthError):
            self.service.log_in("ghost@example.com", "x", ip="198.51.100.1")

        events = [e for e in self.store.events_for()
                  if e.kind is EventKind.LOGIN_FAILED]
        self.assertEqual(events[-1].email, "ghost@example.com")
        self.assertEqual(events[-1].ip, "198.51.100.1")
        self.assertFalse(events[-1].succeeded)

    def test_the_audit_log_never_contains_a_token_or_a_password(self) -> None:
        """An audit log holding a live reset token is a second copy of
        everyone's password."""

        self.service.sign_up(EMAIL, PASSWORD)
        verification = _token_from(self.sender.links_for(EMAIL)[-1])
        self.service.verify_email(verification)
        result = self.service.log_in(EMAIL, PASSWORD)
        self.service.request_password_reset(EMAIL)
        reset = _token_from(self.sender.links_for(EMAIL)[-1])

        blob = repr([e.to_dict() for e in self.store.events_for()])
        for secret in (PASSWORD, verification, reset,
                       result.tokens.refresh_token, result.tokens.access_token):
            self.assertNotIn(secret, blob)

    def test_the_audit_log_records_who_and_from_where(self) -> None:
        self.register()
        self.service.log_in(EMAIL, PASSWORD, ip="203.0.113.7",
                            user_agent="Firefox/1")
        identity = self.store.identity_by_email(EMAIL)

        events = [e for e in self.store.events_for(identity.identity_id)
                  if e.kind is EventKind.LOGIN_SUCCEEDED]
        self.assertTrue(events)
        self.assertEqual(events[-1].ip, "203.0.113.7")
        self.assertEqual(events[-1].identity_id, identity.identity_id)

    # -- sessions listing --------------------------------------------------

    def test_sessions_can_be_listed_for_a_where_am_i_signed_in_screen(self) -> None:
        self.register()
        self.service.log_in(EMAIL, PASSWORD, ip="203.0.113.7",
                            user_agent="Firefox/1")
        self.service.log_in(EMAIL, PASSWORD, ip="198.51.100.4",
                            user_agent="Safari/2")
        identity = self.store.identity_by_email(EMAIL)

        sessions = self.service.sessions(identity.identity_id)
        self.assertEqual(len(sessions), 2)
        self.assertEqual({s.ip for s in sessions},
                         {"203.0.113.7", "198.51.100.4"})
        self.assertNotIn("token_hash", sessions[0].to_dict())


# ---------------------------------------------------------------------------
# The two bindings
# ---------------------------------------------------------------------------


class MemoryAuthTest(AuthFlows, unittest.TestCase):
    def make_store(self):
        return MemoryAuthStore()


_DSN = os.environ.get("CLIPFORGE_AUTH_TEST_DSN", "")


@unittest.skipUnless(
    _DSN, "set CLIPFORGE_AUTH_TEST_DSN to run the auth suite on Postgres"
)
class PostgresAuthTest(AuthFlows, unittest.TestCase):
    def make_store(self):
        from clipforge.auth.postgres import PostgresAuthStore

        import psycopg

        admin = os.environ.get("CLIPFORGE_TEST_ADMIN_DSN", _DSN)
        with psycopg.connect(admin) as connection:
            with connection.cursor() as cursor:
                # Ordered by dependency; the audit log is not cascaded from
                # identities because it deliberately has no foreign key.
                cursor.execute(
                    "TRUNCATE TABLE auth_audit_log, auth_rate_limits, "
                    "auth_tokens, auth_sessions, auth_memberships, "
                    "auth_identities CASCADE"
                )
            connection.commit()
        return PostgresAuthStore(_DSN, min_size=1, max_size=4)


# ---------------------------------------------------------------------------
# Things that need no store
# ---------------------------------------------------------------------------


class PasswordTest(unittest.TestCase):
    def setUp(self) -> None:
        self.hasher = PasswordHasher(FAST)

    def test_a_hash_is_real_bcrypt(self) -> None:
        stored = self.hasher.hash(PASSWORD)
        self.assertRegex(stored, r"^\$2[aby]\$\d{2}\$")
        self.assertTrue(self.hasher.verify(PASSWORD, stored))
        self.assertFalse(self.hasher.verify(PASSWORD + "!", stored))

    def test_the_same_password_hashes_differently_every_time(self) -> None:
        """Per-hash salt. Identical hashes would say which users share a
        password, and make one rainbow table serve all of them."""

        self.assertNotEqual(self.hasher.hash(PASSWORD), self.hasher.hash(PASSWORD))

    def test_a_password_longer_than_bcrypts_limit_is_not_truncated(self) -> None:
        """bcrypt stops at 72 bytes. Without the pre-hash, two different long
        passphrases sharing a prefix would be the same password."""

        base = "correct horse battery staple " * 4        # ~116 bytes
        first = base + "AAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        second = base + "BBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
        self.assertGreater(len(first.encode()), 72)

        stored = self.hasher.hash(first)
        self.assertTrue(self.hasher.verify(first, stored))
        self.assertFalse(self.hasher.verify(second, stored),
                         "the tail past 72 bytes was ignored")

    def test_a_password_with_a_null_byte_is_handled(self) -> None:
        """bcrypt truncates at a NUL. The base64 in the pre-hash is what stops
        a digest that happens to contain one from weakening the result."""

        stored = self.hasher.hash("before\x00after and some more length")
        self.assertTrue(self.hasher.verify("before\x00after and some more length",
                                           stored))
        self.assertFalse(self.hasher.verify("before", stored))

    def test_unicode_normalisation_means_the_same_typing_works(self) -> None:
        composed = "café passphrase that is long"
        decomposed = "café passphrase that is long"
        self.assertNotEqual(composed, decomposed)
        self.assertTrue(self.hasher.verify(decomposed, self.hasher.hash(composed)))

    def test_a_malformed_stored_hash_is_a_miss_not_a_crash(self) -> None:
        self.assertFalse(self.hasher.verify(PASSWORD, "not-a-bcrypt-hash"))
        self.assertFalse(self.hasher.verify(PASSWORD, ""))

    def test_the_policy_refuses_the_obvious(self) -> None:
        policy = PasswordPolicy(rounds=4)
        for bad in ("short", "password", "123456789012", "abcdefghijkl",
                    "aaaaaaaaaaaaaa", "qwertyuiop", " padded password  "):
            with self.assertRaises(WeakPassword, msg=bad):
                policy.check(bad)

    def test_the_policy_refuses_a_password_containing_the_address(self) -> None:
        with self.assertRaises(WeakPassword):
            PasswordPolicy(rounds=4).check("jsmith-is-my-password",
                                           "jsmith@example.com")

    def test_the_policy_accepts_an_ordinary_long_phrase(self) -> None:
        PasswordPolicy(rounds=4).check("marmalade tuesday bicycle", EMAIL)

    def test_needs_rehash_notices_a_raised_cost(self) -> None:
        from clipforge.auth.passwords import ALGORITHM

        cheap = PasswordHasher(PasswordPolicy(rounds=4)).hash(PASSWORD)
        dear = PasswordHasher(PasswordPolicy(rounds=6))
        self.assertTrue(dear.needs_rehash(cheap, ALGORITHM))
        self.assertFalse(dear.needs_rehash(dear.hash(PASSWORD), ALGORITHM))
        self.assertTrue(dear.needs_rehash(cheap, "some-older-scheme"))


class TokenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.keyring = Keyring((SigningKey("k1", "x" * 40),))
        self.issuer = AccessTokenIssuer(self.keyring)

    def _issue(self, **kwargs) -> str:
        defaults = dict(identity_id="idn_1", email=EMAIL, tenant_id="ten_1",
                        user_id="usr_1", role="admin", session_id="ses_1")
        defaults.update(kwargs)
        return self.issuer.issue(**defaults)[0]

    def test_a_token_round_trips(self) -> None:
        principal = self.issuer.verify(self._issue())
        self.assertEqual(principal.identity_id, "idn_1")
        self.assertEqual(principal.tenant_id, "ten_1")
        self.assertEqual(principal.role, "admin")
        self.assertTrue(principal.jti)

    def test_a_tampered_payload_is_refused(self) -> None:
        import base64
        import json

        header, payload, signature = self._issue().split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
        claims["role"] = "owner"
        forged = base64.urlsafe_b64encode(
            json.dumps(claims).encode()
        ).rstrip(b"=").decode()

        with self.assertRaises(InvalidToken):
            self.issuer.verify(f"{header}.{forged}.{signature}")

    def test_an_algorithm_confusion_token_is_refused(self) -> None:
        """`alg` is attacker-controlled, so it is never read from the token.
        An unsigned token claiming `alg: none` is the classic forgery."""

        import base64
        import json

        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "none", "typ": "JWT", "kid": "k1"}).encode()
        ).rstrip(b"=").decode()
        claims = base64.urlsafe_b64encode(json.dumps({
            "sub": "idn_evil", "iss": "clipforge", "aud": "clipforge-api",
            "iat": 0, "nbf": 0, "exp": 9_999_999_999, "jti": "x",
        }).encode()).rstrip(b"=").decode()

        with self.assertRaises(InvalidToken):
            self.issuer.verify(f"{header}.{claims}.")

    def test_a_token_signed_with_another_key_is_refused(self) -> None:
        other = AccessTokenIssuer(Keyring((SigningKey("k1", "y" * 40),)))
        foreign = other.issue(
            identity_id="idn_1", email=EMAIL, tenant_id="t", user_id="u",
            role="owner", session_id="s",
        )[0]
        with self.assertRaises(InvalidToken):
            self.issuer.verify(foreign)

    def test_an_unknown_kid_is_refused_rather_than_tried_against_every_key(
        self,
    ) -> None:
        other = AccessTokenIssuer(Keyring((SigningKey("unknown", "x" * 40),)))
        token = other.issue(
            identity_id="i", email=EMAIL, tenant_id="t", user_id="u",
            role="r", session_id="s",
        )[0]
        with self.assertRaises(InvalidToken):
            self.issuer.verify(token)

    def test_an_expired_token_is_refused(self) -> None:
        issuer = AccessTokenIssuer(self.keyring, ttl_s=1)
        token = issuer.issue(
            identity_id="i", email=EMAIL, tenant_id="t", user_id="u",
            role="r", session_id="s",
            now=datetime.now(UTC) - timedelta(hours=1),
        )[0]
        with self.assertRaises(InvalidToken):
            issuer.verify(token)

    def test_a_token_for_another_audience_is_refused(self) -> None:
        """Without this check, a token minted by a sibling service that shares
        the secret is accepted here."""

        other = AccessTokenIssuer(self.keyring, audience="some-other-api")
        token = other.issue(
            identity_id="i", email=EMAIL, tenant_id="t", user_id="u",
            role="r", session_id="s",
        )[0]
        with self.assertRaises(InvalidToken):
            self.issuer.verify(token)

    def test_key_rotation_keeps_old_tokens_valid(self) -> None:
        """Deploy the new key alongside the old, sign with the new, drop the
        old once every token issued under it has expired."""

        old = self._issue()
        rotated = AccessTokenIssuer(Keyring((
            SigningKey("k2", "z" * 40), SigningKey("k1", "x" * 40),
        )))
        self.assertTrue(rotated.verify(old))
        fresh = rotated.issue(
            identity_id="i", email=EMAIL, tenant_id="t", user_id="u",
            role="r", session_id="s",
        )[0]
        self.assertTrue(rotated.verify(fresh))
        # And the old issuer cannot verify the new token: k2 is not in its ring.
        with self.assertRaises(InvalidToken):
            self.issuer.verify(fresh)

    def test_rubbish_is_refused_without_raising_something_else(self) -> None:
        for bad in ("", "x", "a.b.c", "not.a.token", "..", None):
            with self.assertRaises(InvalidToken):
                self.issuer.verify(bad)   # type: ignore[arg-type]

    def test_a_short_signing_key_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            SigningKey("k", "too short")

    def test_refresh_tokens_are_unguessable_and_unique(self) -> None:
        tokens = {new_refresh_token() for _ in range(500)}
        self.assertEqual(len(tokens), 500)
        self.assertGreaterEqual(len(next(iter(tokens))), 40)


class ProductionReadinessTest(unittest.TestCase):
    def test_a_generated_signing_key_is_refused(self) -> None:
        """It works perfectly, signs everyone out on restart, and cannot be
        verified by a second instance."""

        with self.assertRaises(MisconfiguredAuth) as caught:
            AuthConfig().require_production_ready()
        self.assertIn("generated at startup", str(caught.exception))

    def test_a_sound_configuration_passes(self) -> None:
        AuthConfig(
            keyring=Keyring((SigningKey("k1", "x" * 40),)),
            keyring_is_ephemeral=False,
        ).require_production_ready()

    def test_each_unsafe_setting_is_named(self) -> None:
        config = AuthConfig(
            keyring=Keyring((SigningKey("k1", "x" * 40),)),
            keyring_is_ephemeral=False,
            require_verified_email=False,
            password_policy=PasswordPolicy(rounds=4),
            rate_limits={},
            access_ttl_s=86_400,
            reset_url="http://app.test/reset",
        )
        with self.assertRaises(MisconfiguredAuth) as caught:
            config.require_production_ready()

        message = str(caught.exception)
        for expected in ("unverified email", "bcrypt cost", "rate limiting",
                         "cannot be revoked", "clear text"):
            self.assertIn(expected, message)

    def test_the_environment_report_never_echoes_a_secret(self) -> None:
        os.environ["CLIPFORGE_AUTH_SIGNING_KEYS"] = "k9:" + "s" * 40
        self.addCleanup(os.environ.pop, "CLIPFORGE_AUTH_SIGNING_KEYS", None)

        report = describe_environment()
        self.assertEqual(report["signing_keys"], ["k9"])
        self.assertNotIn("s" * 40, repr(report))
        self.assertTrue(report["production_ready"])

    def test_a_malformed_signing_key_list_is_refused(self) -> None:
        os.environ["CLIPFORGE_AUTH_SIGNING_KEYS"] = "no-colon-here"
        self.addCleanup(os.environ.pop, "CLIPFORGE_AUTH_SIGNING_KEYS", None)
        with self.assertRaises(MisconfiguredAuth):
            config_from_env()


class ConcurrencyTest(unittest.TestCase):
    """The races that produce two valid credentials where there should be one."""

    def setUp(self) -> None:
        self.store = MemoryAuthStore()
        self.service = AuthService(
            self.store, AccessTokenIssuer(Keyring.generated()),
            config=_config(), hasher=PasswordHasher(FAST),
            sender=RecordingEmailSender(),
        )
        self.service.sign_up(EMAIL, PASSWORD)
        token = _token_from(self.service.sender.links_for(EMAIL)[-1])
        self.service.verify_email(token)

    def test_concurrent_refreshes_do_not_produce_two_live_tokens(self) -> None:
        """Two tabs refreshing at the same instant is ordinary. Without the
        per-session lock both read the pre-rotation row and both succeed,
        leaving two tokens where the design allows one."""

        result = self.service.log_in(EMAIL, PASSWORD)
        barrier = threading.Barrier(6)
        succeeded: list = []
        failed: list = []

        def attempt() -> None:
            barrier.wait(timeout=5)
            try:
                succeeded.append(self.service.refresh(result.tokens.refresh_token))
            except Exception as error:                      # noqa: BLE001
                failed.append(error)

        threads = [threading.Thread(target=attempt) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(succeeded) + len(failed), 6)
        self.assertLessEqual(len(succeeded), 1,
                             "one refresh token produced two live sessions")

    def test_concurrent_signups_for_one_address_create_one_identity(self) -> None:
        barrier = threading.Barrier(6)
        created: list = []

        def attempt(index: int) -> None:
            barrier.wait(timeout=5)
            outcome = self.service.sign_up("race@example.com", PASSWORD,
                                           ip=f"198.51.100.{index}")
            if outcome.created:
                created.append(outcome)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(created), 1)


if __name__ == "__main__":
    unittest.main()
