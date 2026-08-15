"""Choosing how verification and reset links leave the process.

```sh
CLIPFORGE_EMAIL_BACKEND=smtp        # smtp | console | recording
CLIPFORGE_SMTP_HOST=smtp.example.com
CLIPFORGE_SMTP_PORT=587
CLIPFORGE_SMTP_USER=apikey
CLIPFORGE_SMTP_PASSWORD=...
CLIPFORGE_EMAIL_FROM=no-reply@yourdomain.com
CLIPFORGE_SMTP_TLS=1
```

## Why this file exists

`AuthService` defaults its sender to `RecordingEmailSender`, which appends the
message to a list in memory and returns. That is the right default for a
library — tests assert on `deliveries` — and it was, until now, also what a
deployment got, because nothing ever passed a sender in. Signup succeeded, the
audit log recorded a verification token, and no human ever received a link. The
account could not be verified and the password could not be reset.

The failure is invisible from the outside, which is what makes it worth a
module rather than a line. `RecordingEmailSender` in production is not a
degraded mode; it is a silent outage.

## Nothing is guessed

An unset `CLIPFORGE_EMAIL_BACKEND` selects `recording`, and `smtp` with no host
is an error rather than a quiet fallback. Falling back to a sender that
discards mail would reproduce exactly the bug this replaces — the deployment
would look configured and deliver nothing. If someone asks for SMTP, they get
SMTP or they get told why not.

The one thing this cannot check is whether the credentials work. `describe()`
says so, and the settings capability list reports it, because "a host is
configured" and "a server accepted a message" are different facts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from ..auth import email as email_mod

__all__ = ["EmailBackend", "email_sender_from_env", "describe_sender"]


class EmailConfigError(RuntimeError):
    """The mail configuration asks for something it did not supply."""


@dataclass(frozen=True, slots=True)
class EmailBackend:
    """What was selected, for the capability list."""

    name: str
    delivers: bool
    detail: str


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def email_sender_from_env() -> Any:
    """Build the sender this deployment asked for.

    Raises rather than degrading when `smtp` is requested and unusable — see
    the module docstring. Every other backend is always constructible.
    """

    backend = (_env("CLIPFORGE_EMAIL_BACKEND", "recording") or "recording").lower()

    if backend in ("recording", "memory", "none"):
        return email_mod.RecordingEmailSender()

    if backend in ("console", "stdout", "log"):
        return email_mod.ConsoleEmailSender()

    if backend == "smtp":
        host = _env("CLIPFORGE_SMTP_HOST")
        if not host:
            raise EmailConfigError(
                "CLIPFORGE_EMAIL_BACKEND=smtp needs CLIPFORGE_SMTP_HOST. "
                "Refusing to fall back to a sender that discards mail: signup "
                "would appear to work and no verification link would arrive."
            )
        sender = _env("CLIPFORGE_EMAIL_FROM")
        if not sender:
            raise EmailConfigError(
                "CLIPFORGE_EMAIL_BACKEND=smtp needs CLIPFORGE_EMAIL_FROM. "
                "Most providers reject a message whose From address is not one "
                "of your verified domains, and the rejection arrives as a "
                "delivery failure hours later."
            )
        try:
            port = int(_env("CLIPFORGE_SMTP_PORT", "587"))
        except ValueError as error:
            raise EmailConfigError(
                f"CLIPFORGE_SMTP_PORT must be a number, got "
                f"{_env('CLIPFORGE_SMTP_PORT')!r}"
            ) from error
        return email_mod.SmtpEmailSender(
            host=host,
            port=port,
            username=_env("CLIPFORGE_SMTP_USER"),
            password=_env("CLIPFORGE_SMTP_PASSWORD"),
            sender=sender,
            use_tls=_env("CLIPFORGE_SMTP_TLS", "1") not in ("0", "false", "no"),
        )

    raise EmailConfigError(
        f"unknown CLIPFORGE_EMAIL_BACKEND {backend!r}. "
        f"Use smtp, console or recording."
    )


def describe_sender(sender: Any) -> EmailBackend:
    """What this sender will actually do with a message.

    `delivers` is about reaching a person, not about the call succeeding.
    `ConsoleEmailSender` never raises and never delivers, which is fine for a
    developer reading their own terminal and useless to a customer.
    """

    name = type(sender).__name__

    if isinstance(sender, email_mod.SmtpEmailSender):
        return EmailBackend(
            name="smtp", delivers=True,
            detail=(
                f"SMTP to {sender.host}:{sender.port} as "
                f"{sender.sender}. Configured, but no message has been "
                f"accepted by that server from this process — the first real "
                f"signup is the test."
            ),
        )
    if isinstance(sender, email_mod.ConsoleEmailSender):
        return EmailBackend(
            name="console", delivers=False,
            detail=(
                "Verification and reset links are printed to this process's "
                "stdout and sent to nobody. Fine for local development; a "
                "silent outage in production."
            ),
        )
    if isinstance(sender, email_mod.RecordingEmailSender):
        return EmailBackend(
            name="recording", delivers=False,
            detail=(
                "No mail transport is configured, so verification and reset "
                "links are kept in memory and never delivered. Signup will "
                "appear to succeed and no account can confirm its address. "
                "Set CLIPFORGE_EMAIL_BACKEND=smtp."
            ),
        )
    return EmailBackend(
        name=name, delivers=True,
        detail=f"Custom sender {name}; delivery behaviour is unknown here.",
    )
