"""Encrypting the credentials before they reach a table.

`DurableTokenStore` has always taken `seal` and `unseal` as functions and has
never had an implementation to hand — deliberately, because a package that
publishes videos should not also be a key manager, and the key belongs in a KMS
held by something other than the process that uses it. The consequence, though,
was that nothing in this repository could actually build a durable token store:
every caller would have had to invent its own crypto, and the one that got
written first would have become the format.

So this is the reference implementation, and it is deliberately small.

## AES-256-GCM, with the nonce in the ciphertext

Authenticated encryption, so a tampered ciphertext fails to decrypt rather than
decrypting to something else. A fresh 96-bit nonce per call — the size AES-GCM
is specified for — prepended to the output, because a nonce reused under one
key in GCM does not merely leak plaintext, it leaks the authentication key.

The stored form is `v1.<base64url(nonce||ciphertext||tag)>`. Versioned from the
first day: a key rotation or an algorithm change has to be able to read what
the previous one wrote, and a bare blob with no version is a migration nobody
can write.

## What this does not do

Rotate keys. `CLIPFORGE_TOKEN_KEY` is one key, and re-encrypting under a new
one is an operational job with a table scan in it. The version prefix is what
makes that job possible later; pretending to solve it here would mean shipping
an untested key hierarchy.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Callable

__all__ = ["Sealer", "sealer_from_env", "generate_key", "SealingError"]

VERSION = "v1"
_NONCE_BYTES = 12
KEY_BYTES = 32
ENV_KEY = "CLIPFORGE_TOKEN_KEY"


class SealingError(RuntimeError):
    """The key is missing, malformed, or does not match the ciphertext."""


def generate_key() -> str:
    """A fresh key, base64url, for an operator to put in the environment."""

    return base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).decode("ascii")


@dataclass(frozen=True, slots=True)
class Sealer:
    """Seals and unseals one secret at a time under one key."""

    key: bytes

    def __post_init__(self) -> None:
        if len(self.key) != KEY_BYTES:
            raise SealingError(
                f"the token key must be {KEY_BYTES} bytes, got {len(self.key)}"
            )

    def seal(self, plaintext: str) -> str:
        if not plaintext:
            # An empty secret is an absent secret. Encrypting it would produce
            # a value that reads as "a credential is stored" to every caller
            # that checks for a non-empty string.
            return ""
        aead = self._aead()
        nonce = os.urandom(_NONCE_BYTES)
        blob = nonce + aead.encrypt(nonce, plaintext.encode("utf-8"), None)
        return f"{VERSION}.{base64.urlsafe_b64encode(blob).decode('ascii')}"

    def unseal(self, sealed: str) -> str:
        if not sealed:
            return ""
        version, _, payload = sealed.partition(".")
        if version != VERSION or not payload:
            raise SealingError(
                f"unrecognised sealed value (version {version!r}) — it was "
                f"written by a different scheme than this build can read"
            )
        try:
            blob = base64.urlsafe_b64decode(payload.encode("ascii"))
            nonce, body = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
            return self._aead().decrypt(nonce, body, None).decode("utf-8")
        except SealingError:
            raise
        except Exception as error:                          # noqa: BLE001
            # Deliberately not naming which check failed. The distinction
            # between "wrong key" and "tampered ciphertext" is not one to
            # publish to whoever provoked it.
            raise SealingError(
                "the stored credential could not be decrypted — the key is "
                "wrong, or the value was altered"
            ) from error

    def _aead(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        return AESGCM(self.key)


def sealer_from_env(variable: str = ENV_KEY) -> Sealer:
    """The configured sealer, or a refusal that says how to configure one.

    Raises rather than falling back to storing plaintext. A token store that
    silently writes unencrypted refresh tokens is worse than one that will not
    start: the first is discovered in a database dump.
    """

    raw = os.environ.get(variable, "").strip()
    if not raw:
        raise SealingError(
            f"{variable} is not set, so platform credentials cannot be "
            f"encrypted at rest. Generate one with "
            f"`python -m clipforge.publish.sealing`."
        )
    try:
        key = base64.urlsafe_b64decode(_pad(raw))
    except Exception as error:                              # noqa: BLE001
        raise SealingError(
            f"{variable} is not valid base64url: {error}"
        ) from error
    return Sealer(key)


def _pad(raw: str) -> str:
    """base64url without padding is common in env vars and in .env files."""

    return raw + "=" * (-len(raw) % 4)


def sealer_pair(sealer: Sealer) -> tuple[Callable[[str], str], Callable[[str], str]]:
    """The two functions `DurableTokenStore` wants."""

    return sealer.seal, sealer.unseal


if __name__ == "__main__":                                  # pragma: no cover
    print(generate_key())
