"""Connecting, reconnecting and disconnecting creator accounts.

`oauth.py` has the pieces — an authorisation URL, a PKCE challenge, a code
exchange, a token parser. This drives them, holds the state between the two
halves of the flow, and owns the part nobody enjoys writing: what happens when
a creator abandons a connection halfway, connects the wrong account, or
disconnects one that still has posts scheduled against it.

## The flow has two halves and a gap in the middle

`begin()` returns a URL and remembers a `state` and a PKCE verifier.
`complete()` is called minutes later by a redirect handler with a `code` and a
`state`. Between them the creator is on someone else's website and may never
come back.

So pending connections **expire**, and the `state` is checked rather than
trusted. An unchecked `state` is the CSRF hole in every OAuth integration that
has one: an attacker who can make the creator's browser hit the callback with
their own `code` connects *their* account to the victim's channel, and every
subsequent post goes to the attacker's audience.

The verifier is single-use. `complete()` removes the pending record before it
spends it, so a replayed callback finds nothing.

## Disconnect revokes; it does not just forget

Deleting a token locally leaves a live grant on the platform: the app keeps
whatever access it had, and the creator's "connected apps" page still lists it.
Revocation is attempted first, and a revocation failure does not prevent the
local delete — the operator asked to disconnect, and refusing to because the
platform is down would leave them unable to act at all. The failure is
reported instead.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from .oauth import (
    AuthorizationRequest,
    ClientCredentials,
    TokenSet,
    authorization_url,
    exchange_request,
    parse_token_response,
)
from .types import Account, Platform, Request, utcnow

__all__ = [
    "AccountManager",
    "ConnectionError_",
    "PendingConnection",
    "ConnectionResult",
    "AccountHealth",
    "PENDING_TTL",
]

#: How long a creator has to finish an authorisation before it is abandoned.
#: Generous — people get distracted mid-consent — but not unbounded, because
#: a pending record holds a verifier that is a credential in waiting.
PENDING_TTL = timedelta(minutes=30)

REVOKE_URL = {
    Platform.YOUTUBE: "https://oauth2.googleapis.com/revoke",
    Platform.TIKTOK: "https://open.tiktokapis.com/v2/oauth/revoke/",
}


class ConnectionError_(Exception):
    """The connection could not be established or completed."""


@dataclass(slots=True)
class PendingConnection:
    account_id: str
    platform: Platform
    request: AuthorizationRequest
    created_at: datetime
    #: Free-form: which channel this is being connected for, who started it.
    metadata: dict[str, Any] = field(default_factory=dict)

    def expired(self, now: datetime) -> bool:
        return now - self.created_at > PENDING_TTL


@dataclass(slots=True)
class ConnectionResult:
    account_id: str
    platform: Platform
    tokens: TokenSet
    scopes_granted: tuple[str, ...]
    #: Scopes asked for that the creator did not grant. Non-empty means the
    #: account is connected but cannot do everything the product expects.
    scopes_missing: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.scopes_missing


@dataclass(slots=True)
class AccountHealth:
    account_id: str
    platform: Platform
    connected: bool
    #: Set when the account needs a human before it will work again.
    needs_reauth: bool = False
    expires_at: datetime | None = None
    refresh_valid_until: datetime | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "platform": self.platform.value,
            "connected": self.connected,
            "needs_reauth": self.needs_reauth,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "refresh_valid_until": (
                self.refresh_valid_until.isoformat()
                if self.refresh_valid_until else None
            ),
            "detail": self.detail,
        }


@dataclass
class AccountManager:
    """The lifecycle of a connected platform account."""

    transport: Any
    store: Any
    credentials: dict[Platform, ClientCredentials]
    clock: Callable[[], datetime] = utcnow
    _pending: dict[str, PendingConnection] = field(
        default_factory=dict, init=False, repr=False
    )
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    # -- connecting --------------------------------------------------------

    def begin(
        self,
        account_id: str,
        platform: Platform,
        scopes: tuple[str, ...] = (),
        metadata: dict[str, Any] | None = None,
    ) -> AuthorizationRequest:
        """Start a connection. Returns the URL to send the creator to."""

        credentials = self._credentials_for(platform)
        request = authorization_url(platform, credentials, scopes=scopes)
        pending = PendingConnection(
            account_id=account_id,
            platform=platform,
            request=request,
            created_at=self.clock(),
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._sweep(self.clock())
            self._pending[request.state] = pending
        return request

    def complete(self, state: str, code: str) -> ConnectionResult:
        """Finish a connection from the redirect's `state` and `code`."""

        now = self.clock()
        with self._lock:
            self._sweep(now)
            # Popped, not read. The verifier is single use, and a callback
            # replayed by a browser refresh must not spend it twice.
            pending = self._pending.pop(state, None)

        if pending is None:
            raise ConnectionError_(
                "unknown or expired authorisation state — the connection was "
                "not started here, took too long, or has already been "
                "completed. Start it again."
            )
        if not code:
            raise ConnectionError_(
                f"the {pending.platform.value} callback carried no code — the "
                f"creator most likely declined"
            )

        credentials = self._credentials_for(pending.platform)
        request = exchange_request(
            pending.platform, credentials, code, pending.request.pkce.verifier
        )
        try:
            response = self.transport.send(request)
        except Exception as error:                              # noqa: BLE001
            raise ConnectionError_(
                f"could not reach {pending.platform.value} to exchange the "
                f"authorisation code: {error}"
            ) from error

        if not response.ok:
            raise ConnectionError_(
                f"{pending.platform.value} refused the authorisation code "
                f"({response.status}): {_message(response.body)}"
            )

        try:
            tokens = parse_token_response(
                pending.platform, pending.account_id, response, now
            )
        except ValueError as error:
            raise ConnectionError_(str(error)) from error

        self.store.put(tokens)

        from .limits import limits_for

        wanted = set(limits_for(pending.platform).scopes)
        granted = set(tokens.scopes)
        # Empty means the platform did not echo scopes back, which several do
        # not. Absence of evidence, so nothing is reported missing.
        missing = tuple(sorted(wanted - granted)) if granted else ()

        return ConnectionResult(
            account_id=pending.account_id,
            platform=pending.platform,
            tokens=tokens,
            scopes_granted=tokens.scopes,
            scopes_missing=missing,
        )

    def abandon(self, state: str) -> bool:
        """Drop a pending connection the creator walked away from."""
        with self._lock:
            return self._pending.pop(state, None) is not None

    def pending(self) -> tuple[PendingConnection, ...]:
        with self._lock:
            self._sweep(self.clock())
            return tuple(self._pending.values())

    # -- disconnecting -----------------------------------------------------

    def disconnect(self, account_id: str) -> tuple[bool, str]:
        """Revoke on the platform, then forget locally.

        Returns whether revocation succeeded and why not, if it did not. The
        local delete happens either way — see the module docstring.
        """

        tokens = self.store.get(account_id)
        if tokens is None:
            return True, "no credentials were stored"

        revoked, detail = self._revoke(tokens)
        self.store.delete(account_id)
        return revoked, detail

    def _revoke(self, tokens: TokenSet) -> tuple[bool, str]:
        url = REVOKE_URL.get(tokens.platform)
        if url is None:
            # Facebook has no revoke endpoint for this grant shape; the
            # permission is removed from the Page or the app, not by an API
            # call the app can make on the creator's behalf.
            return False, (
                f"{tokens.platform.value} has no revocation endpoint — the "
                f"creator must remove the app in their account settings for "
                f"the grant to end"
            )

        credentials = self.credentials.get(tokens.platform)
        if tokens.platform is Platform.TIKTOK and credentials is not None:
            request = Request(
                method="POST", url=url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                form_body={
                    "client_key": credentials.client_id,
                    "client_secret": credentials.client_secret,
                    "token": tokens.access_token,
                },
                description="tiktok: revoke access",
            )
        else:
            request = Request(
                method="POST", url=url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                form_body={"token": tokens.refresh_token or tokens.access_token},
                description=f"{tokens.platform.value}: revoke access",
            )

        try:
            response = self.transport.send(request)
        except Exception as error:                              # noqa: BLE001
            return False, f"could not reach the platform to revoke: {error}"
        if not response.ok:
            return False, (
                f"revocation returned {response.status}: "
                f"{_message(response.body)}"
            )
        return True, "revoked"

    # -- health ------------------------------------------------------------

    def health(self, account_id: str, now: datetime | None = None) -> AccountHealth:
        now = now or self.clock()
        tokens = self.store.get(account_id)
        platform = tokens.platform if tokens else Platform.YOUTUBE

        if tokens is None:
            return AccountHealth(
                account_id, platform, connected=False, needs_reauth=True,
                detail="no credentials stored — the account is not connected",
            )

        if not tokens.can_refresh(now) and tokens.is_expired(now):
            return AccountHealth(
                account_id, tokens.platform, connected=False, needs_reauth=True,
                expires_at=tokens.expires_at,
                refresh_valid_until=tokens.refresh_valid_until,
                detail=(
                    "the access token has expired and cannot be renewed — "
                    "the creator must reconnect"
                ),
            )

        if tokens.refresh_valid_until and now >= tokens.refresh_valid_until:
            return AccountHealth(
                account_id, tokens.platform, connected=True, needs_reauth=True,
                expires_at=tokens.expires_at,
                refresh_valid_until=tokens.refresh_valid_until,
                detail=(
                    "the refresh window has closed; the next refresh will "
                    "fail. Reconnect before the access token expires."
                ),
            )

        return AccountHealth(
            account_id, tokens.platform, connected=True,
            expires_at=tokens.expires_at,
            refresh_valid_until=tokens.refresh_valid_until,
            detail="connected",
        )

    def all_health(self, now: datetime | None = None) -> list[AccountHealth]:
        now = now or self.clock()
        return [self.health(a, now) for a in self.store.all_accounts()]

    def needing_attention(self, now: datetime | None = None) -> list[AccountHealth]:
        return [h for h in self.all_health(now) if h.needs_reauth]

    # -- internals ---------------------------------------------------------

    def _credentials_for(self, platform: Platform) -> ClientCredentials:
        credentials = self.credentials.get(platform)
        if credentials is None:
            raise ConnectionError_(
                f"no client credentials configured for {platform.value} — set "
                f"them before connecting an account"
            )
        return credentials

    def _sweep(self, now: datetime) -> None:
        for state in [s for s, p in self._pending.items() if p.expired(now)]:
            del self._pending[state]


def _message(body: Any) -> str:
    if not isinstance(body, dict):
        return str(body)
    payload = body.get("data") if isinstance(body.get("data"), dict) else body
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or error)
    for key in ("error_description", "message", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return "no detail"


def account_for(result: ConnectionResult, **kwargs: Any) -> Account:
    """A publishable `Account` from a completed connection."""
    return Account(
        account_id=result.account_id,
        platform=result.platform,
        org_id=kwargs.pop("org_id", "org1"),
        **kwargs,
    )
