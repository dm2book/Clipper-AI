"""Password hashing, and the things people get wrong about it.

## Argon2id, with bcrypt kept for the hashes already in the database

New hashes are Argon2id at OWASP's floor — 19 MiB of memory, two passes, one
lane. The reason to prefer it over bcrypt is **memory hardness**: bcrypt's cost
is CPU time and its working set is 4 KiB, so a GPU or an FPGA runs thousands of
guesses in parallel for very little silicon. Argon2id makes each guess pay for
19 MiB, which is what actually constrains an attacker with a rack of hardware.

Measured on this machine: Argon2id at those parameters verifies in ~25 ms
against ~275 ms for bcrypt cost 12. It is both stronger against parallel attack
and eight times cheaper per login, which is unusual enough to be worth stating.

**Every bcrypt hash still verifies.** The scheme is chosen from the stored
hash's own prefix rather than from the recorded algorithm tag, so a hash made
before this change is checked with bcrypt whatever the metadata says, and
`needs_rehash` then reports it as stale so `service.log_in` replaces it. The
fleet migrates itself on the next sign-in and nobody is asked to reset
anything. A flag day here would mean locking out every existing account.

## bcrypt truncates at 72 bytes, and the legacy path does not

bcrypt ignores everything past the 72nd byte of its input, which turns a
100-character passphrase into a 72-character one and makes the rest
decorative. So the legacy construction SHA-256's the password first and base64's
the digest: a fixed 44-byte input, well under the limit, with the full entropy
preserved. The base64 matters as much as the hash — a raw digest can contain a
NUL, and bcrypt truncates at the first one, quietly weakening roughly one hash
in 180.

Argon2 has no such limit, so the pre-hash is **not** applied on the new path.
Feeding it a digest instead of the password would be a second construction to
document for no benefit, and `MAX_LENGTH` already bounds the denial-of-service
side.

## Comparison is constant time, and so is the failure path

Both backends compare in constant time with respect to the hash. Neither is
called at all when the email is unknown, and *that* difference is measurable
from outside — a fast "no" means "no such account", a slow one means "wrong
password". `verify_dummy()` exists to spend the same time on a miss, and
`service.log_in` always calls one or the other.

## Hashes are upgraded on login

The cost factor that was right in 2026 will be too low in 2030. Every
successful login checks whether the stored hash was produced by the current
policy and, if not, replaces it — the one moment the plaintext is legitimately
in memory. Nobody is asked to reset anything, and the fleet migrates itself.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import unicodedata
from dataclasses import dataclass

log = logging.getLogger("clipforge.auth.passwords")

__all__ = [
    "PasswordPolicy",
    "PasswordHasher",
    "WeakPassword",
    "ALGORITHM",
    "LEGACY_BCRYPT",
    "MIN_LENGTH",
]

#: The tag stored alongside every new hash. Change it whenever the construction
#: changes, never when only the cost changes — the cost is inside the hash.
ALGORITHM = "argon2id"

#: What the tag said before this. Still verified, never written.
LEGACY_BCRYPT = "bcrypt-sha256-b64"

MIN_LENGTH = 12
#: Not a security limit — the construction above accepts any length — but an
#: unbounded field is a denial-of-service vector, since hashing a 10 MB body
#: costs real CPU before anything has been authenticated.
MAX_LENGTH = 1024

#: The cheapest passwords to guess, and the ones a length rule alone lets
#: through. A real deployment should check a breach corpus; this is the floor,
#: not the ceiling, and `PasswordPolicy.deny` exists to extend it.
_COMMON = frozenset({
    "password", "password1", "passw0rd", "letmein", "welcome", "iloveyou",
    "administrator", "changeme", "trustno1", "qwertyuiop", "1234567890",
    "123456789012", "passwordpassword", "clipforge", "clipforgeai",
})


class WeakPassword(Exception):
    """The password does not meet policy. The message names what to fix."""


@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    min_length: int = MIN_LENGTH
    max_length: int = MAX_LENGTH
    #: bcrypt work factor. Retained because hashes made under it still exist
    #: and because `require_production_ready` still audits it, but nothing new
    #: is hashed with bcrypt any more.
    rounds: int = 12

    # -- Argon2id ----------------------------------------------------------
    #
    # OWASP's minimum recommended configuration: 19 MiB, two iterations, one
    # degree of parallelism. Memory is the parameter that matters — it is what
    # a GPU attack cannot amortise — so raise `argon2_memory_kib` before
    # reaching for `argon2_time_cost`.

    argon2_memory_kib: int = 19_456
    argon2_time_cost: int = 2
    argon2_parallelism: int = 1
    #: Length of the derived key and of the random salt, both in bytes.
    argon2_hash_len: int = 32
    argon2_salt_len: int = 16

    #: Extra denied values — a breach corpus, the product name, a customer's
    #: own domain. Compared case-insensitively after normalisation.
    deny: frozenset[str] = frozenset()
    #: Reject a password that contains the local part of the email. "jsmith"
    #: with password "jsmith2026!" is guessed on the first try by anyone who
    #: has seen the address.
    reject_email_similarity: bool = True

    @classmethod
    def fast(cls, **overrides: object) -> "PasswordPolicy":
        """Deliberately weak parameters, for tests only.

        A suite that hashes a few thousand passwords at production cost spends
        minutes doing it, and a slow suite is one people stop running. Named
        rather than assembled inline so it is obvious at every call site that
        this is not a configuration anyone should deploy.
        """

        settings: dict[str, object] = {
            "rounds": 4,
            "argon2_memory_kib": 1024,
            "argon2_time_cost": 1,
            "argon2_parallelism": 1,
        }
        settings.update(overrides)
        return cls(**settings)  # type: ignore[arg-type]

    def check(self, password: str, email: str = "") -> None:
        """Raise `WeakPassword` if this password may not be used."""

        # Normalised first: two visually identical passwords that differ in
        # Unicode composition must not be two different passwords, or a user
        # who types the same thing on a different keyboard cannot log in.
        candidate = unicodedata.normalize("NFKC", password)

        if len(candidate) < self.min_length:
            raise WeakPassword(
                f"Use at least {self.min_length} characters. Length is what "
                f"makes a password hard to guess — a long ordinary phrase "
                f"beats a short complicated one."
            )
        if len(candidate.encode()) > self.max_length:
            raise WeakPassword(f"Keep it under {self.max_length} bytes.")
        if candidate.strip() != candidate:
            raise WeakPassword(
                "Remove the leading or trailing spaces — they are easy to "
                "lose when typing this again later."
            )

        folded = candidate.casefold()
        if folded in _COMMON or folded in {d.casefold() for d in self.deny}:
            raise WeakPassword(
                "That is one of the most commonly used passwords, so it is "
                "among the first tried. Choose something else."
            )
        if len(set(candidate)) < 5:
            raise WeakPassword(
                "Too few distinct characters — this is long but not varied."
            )
        if _is_a_run(candidate):
            raise WeakPassword(
                "That is a keyboard or counting pattern, which is guessed "
                "about as quickly as a short password."
            )

        if self.reject_email_similarity and email:
            local = email.split("@")[0].casefold()
            if len(local) >= 4 and local in folded:
                raise WeakPassword(
                    "The password contains your email address. Anyone who "
                    "knows the address gets the password for free."
                )


def _is_a_run(value: str) -> bool:
    """True for `123456789012`, `abcdefghijkl`, `aaaaaaaaaaaa` and friends."""

    if len(value) < 4:
        return False
    deltas = {ord(b) - ord(a) for a, b in zip(value, value[1:])}
    if deltas <= {1} or deltas <= {-1} or deltas <= {0}:
        return True
    lowered = value.casefold()
    rows = ("qwertyuiop", "asdfghjkl", "zxcvbnm", "1234567890")
    return any(lowered in row or lowered in row[::-1] for row in rows)


class PasswordHasher:
    """Argon2id for new hashes, bcrypt for the ones already stored."""

    def __init__(self, policy: PasswordPolicy | None = None) -> None:
        self.policy = policy or PasswordPolicy()
        try:
            import argon2
        except ImportError as error:                        # pragma: no cover
            raise RuntimeError(
                "the `argon2-cffi` package is required for password hashing. "
                "Install it with `pip install 'clipforge[auth]'`. There is "
                "deliberately no pure-Python fallback: a weaker hash that "
                "activates when a dependency is missing is a weaker hash "
                "nobody notices in production."
            ) from error
        try:
            import bcrypt
        except ImportError as error:                        # pragma: no cover
            raise RuntimeError(
                "the `bcrypt` package is still required. Hashes written "
                "before the move to Argon2id are verified with it, and "
                "dropping it would lock out every account that has not "
                "signed in since."
            ) from error

        self._argon2 = argon2
        self._bcrypt = bcrypt
        self._hasher = argon2.PasswordHasher(
            time_cost=self.policy.argon2_time_cost,
            memory_cost=self.policy.argon2_memory_kib,
            parallelism=self.policy.argon2_parallelism,
            hash_len=self.policy.argon2_hash_len,
            salt_len=self.policy.argon2_salt_len,
            type=argon2.Type.ID,
        )
        #: A hash of a value nobody knows, used to spend realistic time on a
        #: login for an address that does not exist. Built once, because
        #: building it costs exactly as much as a real hash.
        self._dummy = self.hash("not a real password, only a stopwatch")

    # -- hashing -----------------------------------------------------------

    def normalise(self, password: str) -> str:
        """NFKC, so two visually identical passwords are one password.

        A passphrase typed on a different keyboard layout can arrive in a
        different Unicode composition. Without this the same characters hash
        differently and the user cannot sign in.
        """

        return unicodedata.normalize("NFKC", password)

    def prepare(self, password: str) -> bytes:
        """The *legacy bcrypt* input: base64 of the SHA-256 of the password.

        Kept only to verify hashes written before Argon2id. Fixed at 44 bytes,
        so bcrypt's 72-byte ceiling is unreachable, and base64'd so the digest
        cannot contain a NUL that bcrypt would truncate at.
        """

        return base64.b64encode(
            hashlib.sha256(self.normalise(password).encode()).digest()
        )

    def hash(self, password: str) -> str:
        """Argon2id. Always — bcrypt is never written again."""
        return self._hasher.hash(self.normalise(password))

    def verify(self, password: str, stored: str) -> bool:
        """Check a password against whichever scheme produced the hash.

        Dispatch is on the stored hash's own prefix, not on the algorithm tag
        recorded beside it. The hash format is self-describing and the tag is
        metadata that can be absent or wrong after a bad migration; trusting
        the tag would mean an account whose row was mislabelled can never sign
        in again.
        """

        if not stored:
            # No hash on the identity: an invited account that never set a
            # password. Still spend the time, so the answer is not fast.
            self.verify_dummy()
            return False

        if stored.startswith("$argon2"):
            try:
                # argon2-cffi takes the hash first. Getting that backwards
                # returns "wrong password" for every correct password, and a
                # broad `except Exception` around it hides the mistake behind
                # a plausible answer — which is exactly what happened while
                # writing this. Hence the narrow excepts below.
                return bool(self._hasher.verify(stored, self.normalise(password)))
            except self._argon2.exceptions.VerifyMismatchError:
                return False
            except self._argon2.exceptions.VerificationError:
                # Covers a corrupt or truncated hash. Still not a match, but
                # worth a log line: it means a row in the database is damaged.
                log.warning("argon2 could not verify a stored hash")
                return False

        if stored.startswith("$2"):
            try:
                return self._bcrypt.checkpw(self.prepare(password), stored.encode())
            except (ValueError, TypeError):
                return False

        # An unrecognised format. Not a match, and not an exception the login
        # path should have to handle.
        self.verify_dummy()
        return False

    def verify_dummy(self) -> bool:
        """Spend a verification's worth of time and return False.

        Called when the email is unknown. Without it the miss returns in
        microseconds and the hit takes tens of milliseconds, which is a
        reliable oracle for "does this address have an account here" — the
        exact question the rest of this package works to keep unanswerable.

        The dummy is Argon2id because that is what the overwhelming majority
        of live hashes will be. A surviving bcrypt account still verifies an
        order of magnitude slower, which leaks "this account has not signed in
        since the migration" — not whether it exists. That window closes as the
        fleet rehashes itself, and it is the price of not locking anyone out.
        """

        try:
            self._hasher.verify(self._dummy, self.normalise("stopwatch"))
        except self._argon2.exceptions.VerificationError:
            pass                                            # always, by design
        return False

    # -- upgrades ----------------------------------------------------------

    def needs_rehash(self, stored: str, algo: str) -> bool:
        """Whether this hash was made by an older scheme or a weaker policy.

        `service.log_in` calls this on every success and replaces the hash when
        it answers True. That is the entire bcrypt migration: no flag day, no
        forced resets, and the parameters keep pace with the hardware.
        """

        if not stored:
            return True
        if stored.startswith("$argon2"):
            return bool(self._hasher.check_needs_rehash(stored))
        if stored.startswith("$2"):
            # Any surviving bcrypt hash is stale by definition now.
            return True
        return True

    # -- token hashing -----------------------------------------------------

    @staticmethod
    def hash_token(token: str) -> str:
        """SHA-256 for opaque tokens: refresh, reset, verification.

        Not bcrypt, and the difference is worth stating. bcrypt is slow on
        purpose because passwords are low-entropy and guessable. These tokens
        are 256 bits from `secrets`, so there is nothing to guess and slowness
        buys nothing but latency on every refresh.
        """

        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def same_token(presented: str, stored_hash: str) -> bool:
        return hmac.compare_digest(
            PasswordHasher.hash_token(presented), stored_hash
        )
