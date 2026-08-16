"""TOTP, from the RFC rather than from a package.

HOTP (RFC 4226) and TOTP (RFC 6238) together are about forty lines of `hmac`
and `struct`. A dependency would be more code than this, and — the deciding
argument — both RFCs publish test vectors, so an implementation written here
can be proved correct against the standard instead of trusted because it is
popular. `tests/test_auth.py` runs every vector from RFC 6238 Appendix B.

## What makes this safe rather than merely working

**Comparison is constant time.** A digit-by-digit `==` on a six-digit code is
a timing oracle that reduces the search from a million to sixty guesses. This
uses `hmac.compare_digest`.

**Replay is refused.** A code stays valid for its whole thirty-second step and
is accepted inside a small window either side, so without a record of what was
already spent, anyone who observes one code — over the shoulder, in a screen
share, in a phishing proxy — can use it themselves for up to a minute and a
half. `TotpVerifier` returns the counter it matched so the caller can store it
and refuse anything at or below it afterwards.

**Drift is bounded.** One step either side, which is ±30 seconds. Wider is a
support convenience and a real weakening: at ±5 steps an attacker gets eleven
codes per guess instead of three.
"""

from __future__ import annotations

import base64
import hmac
import secrets
import struct
import time
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote

__all__ = [
    "TotpConfig",
    "generate_secret",
    "hotp",
    "totp",
    "verify_totp",
    "provisioning_uri",
    "normalise_code",
]

#: RFC 6238's default and what every authenticator app assumes.
STEP_S = 30
DIGITS = 6

#: One step either side. See the module docstring.
DEFAULT_DRIFT = 1

#: 160 bits, the RFC 4226 recommendation. Base32 of 20 bytes is 32 characters,
#: which is what a user retypes when a QR code will not scan.
SECRET_BYTES = 20


@dataclass(frozen=True, slots=True)
class TotpConfig:
    step_s: int = STEP_S
    digits: int = DIGITS
    drift: int = DEFAULT_DRIFT
    algorithm: str = "sha1"

    def __post_init__(self) -> None:
        if self.digits not in (6, 7, 8):
            raise ValueError("TOTP digits must be 6, 7 or 8")
        if self.step_s <= 0:
            raise ValueError("TOTP step must be positive")
        if self.drift < 0:
            raise ValueError("TOTP drift cannot be negative")


def generate_secret() -> str:
    """A fresh base32 secret, unpadded and uppercase.

    Unpadded because several authenticator apps reject the `=` characters that
    `b32encode` adds, and silently — the user gets a code that never matches
    and no explanation.
    """

    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode().rstrip("=")


def _decode_secret(secret: str) -> bytes:
    cleaned = secret.strip().replace(" ", "").upper()
    # Restore the padding `generate_secret` stripped; b32decode insists on it.
    padding = "=" * (-len(cleaned) % 8)
    try:
        return base64.b32decode(cleaned + padding, casefold=True)
    except Exception as error:                              # noqa: BLE001
        raise ValueError("the MFA secret is not valid base32") from error


def hotp(secret: str, counter: int, config: TotpConfig | None = None) -> str:
    """RFC 4226. The dynamic-truncation step is the fiddly part.

    The low nibble of the last byte selects a four-byte window; the top bit of
    that window is masked off so the result is the same on platforms that would
    otherwise read it as a sign bit.
    """

    config = config or TotpConfig()
    digest = hmac.new(
        _decode_secret(secret), struct.pack(">Q", counter), config.algorithm
    ).digest()
    offset = digest[-1] & 0x0F
    chunk = digest[offset:offset + 4]
    code = struct.unpack(">I", chunk)[0] & 0x7FFF_FFFF
    return str(code % (10 ** config.digits)).zfill(config.digits)


def counter_for(at: float, config: TotpConfig | None = None) -> int:
    config = config or TotpConfig()
    return int(at // config.step_s)


def totp(
    secret: str, at: float | None = None, config: TotpConfig | None = None
) -> str:
    """The code an authenticator app is showing right now."""
    config = config or TotpConfig()
    moment = time.time() if at is None else at
    return hotp(secret, counter_for(moment, config), config)


def normalise_code(code: str) -> str:
    """Strip what a person types that the RFC does not expect.

    Authenticator apps display `123 456`, users paste the space, and a naive
    comparison then fails a correct code. Unicode digits are folded too,
    because a phone keyboard set to another locale can emit them.
    """

    folded = unicodedata.normalize("NFKC", code or "")
    return "".join(ch for ch in folded if ch.isdigit())


def verify_totp(
    secret: str,
    code: str,
    *,
    at: float | None = None,
    config: TotpConfig | None = None,
    last_counter: int | None = None,
) -> int | None:
    """Check a code. Returns the counter it matched, or None.

    The counter is the point: pass the previous one back as `last_counter` and
    a code that has already been spent is refused, which is what closes the
    ninety-second replay window a stolen code otherwise has.

    Every candidate step is compared even after a match, so the time taken does
    not reveal *which* step matched — a difference that would otherwise tell an
    attacker how far the victim's clock has drifted.
    """

    config = config or TotpConfig()
    digits = normalise_code(code)
    if len(digits) != config.digits:
        return None

    moment = time.time() if at is None else at
    centre = counter_for(moment, config)
    matched: int | None = None

    for step in range(-config.drift, config.drift + 1):
        counter = centre + step
        if counter < 0:
            continue
        if hmac.compare_digest(hotp(secret, counter, config), digits):
            # No early return: see the docstring.
            if matched is None:
                matched = counter

    if matched is None:
        return None
    if last_counter is not None and matched <= last_counter:
        return None
    return matched


def provisioning_uri(secret: str, account: str, issuer: str = "ClipForge AI",
                     config: TotpConfig | None = None) -> str:
    """The `otpauth://` URL behind the QR code.

    `issuer` appears twice — in the label and as a parameter — because apps
    disagree about which one they read, and an entry that shows up as a bare
    email address among thirty others is one the user cannot identify later.
    """

    config = config or TotpConfig()
    label = quote(f"{issuer}:{account}", safe="")
    params = (
        f"secret={secret}"
        f"&issuer={quote(issuer, safe='')}"
        f"&algorithm={config.algorithm.upper()}"
        f"&digits={config.digits}"
        f"&period={config.step_s}"
    )
    return f"otpauth://totp/{label}?{params}"
