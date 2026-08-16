"""Getting a verification or reset link to a person.

**No email is sent from this repository.** There is no SMTP client and no
provider integration, and this module does not pretend otherwise: the default
implementation records the message and hands it back, which is what the tests
and the demo use. `SmtpEmailSender` exists and speaks real SMTP, but no server
has ever accepted a message from it here.

That is a deliberate boundary rather than an oversight. A stub that silently
swallowed mail would make signup *look* like it worked while every user waits
forever for a link, and that failure is invisible until a customer complains.
`RecordingEmailSender` makes the outbound message inspectable so the flow can
be exercised end to end, and `deliveries` is the evidence a test asserts on.

## What is in a message, and what is not

The token is in the link and nowhere else. It is not logged, not put in the
subject, and not written to the audit trail — an audit log that holds a live
reset token is a second copy of everyone's password.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

__all__ = [
    "Email",
    "EmailSender",
    "RecordingEmailSender",
    "ConsoleEmailSender",
    "SmtpEmailSender",
    "EmailDeliveryFailed",
]


class EmailDeliveryFailed(Exception):
    """The message could not be handed to a provider."""


@dataclass(slots=True)
class Email:
    to: str
    subject: str
    text: str
    #: Kept apart from the body so a test can assert on it without parsing
    #: prose, and so a sender can build its own template around it.
    link: str = ""
    kind: str = ""

    def redacted(self) -> dict[str, Any]:
        """Loggable. The link carries the token, so it is not in here."""
        return {"to": self.to, "subject": self.subject, "kind": self.kind}


class EmailSender(Protocol):
    def send(self, message: Email) -> None: ...


@dataclass
class RecordingEmailSender:
    """Keeps every message instead of sending it. The default.

    Production must replace this. A deployment that leaves it in place will
    register users who never receive a verification link and cannot reset a
    password, and nothing will look broken from the inside.
    """

    deliveries: list[Email] = field(default_factory=list)

    def send(self, message: Email) -> None:
        self.deliveries.append(message)

    # -- convenience for tests and the demo --------------------------------

    def last_to(self, address: str) -> Email | None:
        for message in reversed(self.deliveries):
            if message.to == address:
                return message
        return None

    def links_for(self, address: str) -> list[str]:
        return [m.link for m in self.deliveries if m.to == address and m.link]

    def clear(self) -> None:
        self.deliveries.clear()


@dataclass
class ConsoleEmailSender:
    """Prints the message. For local development, where the link is needed."""

    stream: Any = None

    def send(self, message: Email) -> None:
        import sys

        stream = self.stream or sys.stdout
        print(f"\n  ── email to {message.to} ─────────────", file=stream)
        print(f"  {message.subject}\n", file=stream)
        print(f"  {message.text}", file=stream)
        if message.link:
            print(f"\n  {message.link}\n", file=stream)


@dataclass
class SmtpEmailSender:
    """Real SMTP over TLS.

    **Unverified.** The code is a straightforward use of `smtplib` and no
    server has accepted a message from it in this environment — there is no
    SMTP host reachable here and no credentials to authenticate with. Treat it
    as a starting point that needs one real send before it is believed.
    """

    host: str
    port: int = 587
    username: str = ""
    password: str = ""
    sender: str = "no-reply@clipforge.example"
    timeout_s: float = 15.0
    use_tls: bool = True

    def send(self, message: Email) -> None:
        import smtplib
        from email.message import EmailMessage

        payload = EmailMessage()
        payload["From"] = self.sender
        payload["To"] = message.to
        payload["Subject"] = message.subject
        body = message.text + (f"\n\n{message.link}\n" if message.link else "")
        payload.set_content(body)

        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout_s) as smtp:
                if self.use_tls:
                    smtp.starttls()
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.send_message(payload)
        except Exception as error:                          # noqa: BLE001
            # Logged without the link: a mail failure that writes the token to
            # the log turns an outage into a credential leak.
            log.warning("email delivery failed: %s", message.redacted())
            raise EmailDeliveryFailed(
                f"could not deliver {message.kind or 'message'} to "
                f"{message.to}: {error}"
            ) from error


# ---------------------------------------------------------------------------
# The messages themselves
# ---------------------------------------------------------------------------


def verification_email(to: str, link: str, expires_in_h: int) -> Email:
    return Email(
        to=to,
        subject="Confirm your ClipForge address",
        text=(
            "Confirm this address to finish setting up your ClipForge "
            f"account. The link works once and expires in {expires_in_h} "
            "hours.\n\n"
            "If you did not create an account, you can ignore this — nothing "
            "will happen until the link is used."
        ),
        link=link,
        kind="email_verification",
    )


def reset_email(to: str, link: str, expires_in_m: int) -> Email:
    return Email(
        to=to,
        subject="Reset your ClipForge password",
        text=(
            "Use this link to choose a new password. It works once and "
            f"expires in {expires_in_m} minutes.\n\n"
            "If you did not ask for this, you can ignore it — your password "
            "has not changed. Signing in as usual will invalidate the link."
        ),
        link=link,
        kind="password_reset",
    )


def unexpected_signup_email(to: str) -> Email:
    """Sent when someone tries to register an address that already exists.

    This is what makes the signup response safe to make identical in both
    cases: the person who owns the address finds out, and the person who
    typed it learns nothing. It is also a genuinely useful warning — somebody
    is trying to register as you.
    """

    return Email(
        to=to,
        subject="Someone tried to sign up with your address",
        text=(
            "Someone entered this address on the ClipForge signup form. You "
            "already have an account, so nothing was created and nothing has "
            "changed.\n\n"
            "If it was you, sign in as usual, or reset your password if you "
            "have forgotten it. If it was not, no action is needed."
        ),
        kind="signup_duplicate",
    )


def password_changed_email(to: str) -> Email:
    """After a password change or reset. Not optional.

    This is the notification that lets a person find out their account was
    taken over, at the one moment it is still recoverable.
    """

    return Email(
        to=to,
        subject="Your ClipForge password was changed",
        text=(
            "The password on your ClipForge account was just changed, and "
            "every signed-in device was signed out.\n\n"
            "If this was not you, reset your password immediately and contact "
            "support — someone else had access to your account."
        ),
        kind="password_changed",
    )


def deletion_requested_email(to: str, when: str) -> Email:
    return Email(
        to=to,
        subject="Your ClipForge account is scheduled for deletion",
        text=(
            f"Your account and its content will be permanently deleted on "
            f"{when}. Until then you can cancel by signing in.\n\n"
            "If you did not request this, sign in now to cancel it and change "
            "your password."
        ),
        kind="deletion_requested",
    )


def mfa_enabled_email(to: str) -> Email:
    """Sent when a second factor is switched on.

    An attacker who has the password and enrols their own authenticator has
    locked the real owner out permanently. This message is the only chance the
    owner gets to notice before that happens.
    """

    return Email(
        to=to,
        subject="Two-factor authentication is on",
        text=(
            "Two-factor authentication was switched on for your ClipForge "
            "account. From now on, signing in needs a code from your "
            "authenticator app as well as your password.\n\n"
            "If this was not you, somebody else knows your password. Reset it "
            "immediately and contact support — they can remove the factor "
            "once they have confirmed who you are."
        ),
        kind="mfa_enabled",
    )


def mfa_disabled_email(to: str) -> Email:
    """Sent when it is switched off. The more dangerous of the pair."""

    return Email(
        to=to,
        subject="Two-factor authentication is off",
        text=(
            "Two-factor authentication was switched off for your ClipForge "
            "account. Your password is now the only thing protecting it.\n\n"
            "If this was not you, reset your password now and switch two-"
            "factor authentication back on."
        ),
        kind="mfa_disabled",
    )


def recovery_code_used_email(to: str) -> Email:
    """Sent when a recovery code is spent.

    Worth a message of its own rather than folding into the sign-in notice: a
    recovery code means somebody could not use the authenticator, which is
    either a lost phone or a person who is not you.
    """

    return Email(
        to=to,
        subject="A recovery code was used to sign in",
        text=(
            "Somebody signed in to your ClipForge account using a recovery "
            "code instead of your authenticator app. That code has now been "
            "used and will not work again.\n\n"
            "If this was you, no action is needed — though it is worth "
            "generating a fresh set if you are running low.\n\n"
            "If it was not you, reset your password and regenerate your "
            "recovery codes straight away."
        ),
        kind="recovery_code_used",
    )


def new_device_email(to: str, label: str, when: str) -> Email:
    """Sent the first time a device signs in.

    The single most useful security email there is: it is the one a person
    reads and immediately knows is wrong.
    """

    return Email(
        to=to,
        subject=f"New sign-in from {label}",
        text=(
            f"Your ClipForge account was signed in to from {label} on {when}."
            f"\n\nIf that was you, nothing to do. If not, reset your password "
            f"— and sign that device out from Settings."
        ),
        kind="new_device",
    )
