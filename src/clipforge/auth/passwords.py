"""Password hashing, and the three things people get wrong about it.

## bcrypt truncates at 72 bytes, and this one does not

bcrypt ignores everything past the 72nd byte of its input. On older releases
that happened silently, which turns a 100-character passphrase into a
72-character one and makes the last 28 characters decorative. `bcrypt` 5 raises
instead — better, but a `ValueError` on signup for a user with a long
passphrase is still the wrong answer.

So the password is SHA-256'd first and the digest is base64'd before it reaches
bcrypt: a fixed 44-byte input, well under the limit, with the full entropy of
the original preserved. The base64 matters as much as the hash — a raw digest
can contain a NUL byte, and bcrypt truncates at the first one, which would
quietly weaken roughly one hash in every 180.

This is a standard construction (`bcrypt(base64(sha256(password)))`) and it is
recorded in the stored algorithm tag so a future change is detectable rather
than a silent incompatibility.

## Comparison is constant time, and so is the failure path

`bcrypt.checkpw` is constant time with respect to the hash. It is *not* called
at all when the email is unknown, and that difference is measurable from
outside — a fast "no" means "no such account", a slow one means "wrong
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
import re
import unicodedata
from dataclasses import dataclass

__all__ = [
    "PasswordPolicy",
    "PasswordHasher",
    "WeakPassword",
    "ALGORITHM",
    "MIN_LENGTH",
]

#: The tag stored alongside every hash. Change it whenever the construction
#: changes, never when only the cost changes — the cost is inside the hash.
ALGORITHM = "bcrypt-sha256-b64"

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
    #: bcrypt work factor. 12 is roughly 250ms on a 2026 server core, which is
    #: the usual balance between "slow for an attacker" and "not a login
    #: latency complaint". Lower it in tests, never in production.
    rounds: int = 12
    #: Extra denied values — a breach corpus, the product name, a customer's
    #: own domain. Compared case-insensitively after normalisation.
    deny: frozenset[str] = frozenset()
    #: Reject a password that contains the local part of the email. "jsmith"
    #: with password "jsmith2026!" is guessed on the first try by anyone who
    #: has seen the address.
    reject_email_similarity: bool = True

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
    """bcrypt, with the pre-hash that makes it length-safe."""

    def __init__(self, policy: PasswordPolicy | None = None) -> None:
        self.policy = policy or PasswordPolicy()
        try:
            import bcrypt
        except ImportError as error:                        # pragma: no cover
            raise RuntimeError(
                "the `bcrypt` package is required for password hashing. "
                "Install it with `pip install 'clipforge[auth]'`. There is "
                "deliberately no pure-Python fallback: a weaker hash that "
                "activates when a dependency is missing is a weaker hash "
                "nobody notices in production."
            ) from error
        self._bcrypt = bcrypt
        #: A hash of a value nobody knows, used to spend realistic time on a
        #: login for an address that does not exist. Built once, because
        #: building it costs exactly as much as a real hash.
        self._dummy = self.hash("not a real password, only a stopwatch")

    # -- hashing -----------------------------------------------------------

    def prepare(self, password: str) -> bytes:
        """The bcrypt input: base64 of the SHA-256 of the password.

        Fixed at 44 bytes, so bcrypt's 72-byte ceiling is unreachable, and
        base64'd so the digest cannot contain a NUL that bcrypt would truncate
        at.
        """

        normalised = unicodedata.normalize("NFKC", password).encode()
        return base64.b64encode(hashlib.sha256(normalised).digest())

    def hash(self, password: str) -> str:
        salt = self._bcrypt.gensalt(rounds=self.policy.rounds)
        return self._bcrypt.hashpw(self.prepare(password), salt).decode()

    def verify(self, password: str, stored: str) -> bool:
        if not stored:
            # No hash on the identity: an invited account that never set a
            # password. Still spend the time, so the answer is not fast.
            self.verify_dummy()
            return False
        try:
            return self._bcrypt.checkpw(self.prepare(password), stored.encode())
        except (ValueError, TypeError):
            # A malformed stored hash. Not a match, and not an exception the
            # login path should have to handle.
            return False

    def verify_dummy(self) -> bool:
        """Spend a verification's worth of time and return False.

        Called when the email is unknown. Without it the miss returns in
        microseconds and the hit takes 250ms, which is a reliable oracle for
        "does this address have an account here" — the exact question the rest
        of this package works to keep unanswerable.
        """

        self._bcrypt.checkpw(self.prepare("stopwatch"), self._dummy.encode())
        return False

    # -- upgrades ----------------------------------------------------------

    def needs_rehash(self, stored: str, algo: str) -> bool:
        """Whether this hash was made by an older policy."""

        if algo != ALGORITHM or not stored:
            return True
        match = re.match(r"^\$2[aby]\$(\d{2})\$", stored)
        if match is None:
            return True
        return int(match.group(1)) < self.policy.rounds

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
