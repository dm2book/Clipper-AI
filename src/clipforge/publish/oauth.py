"""OAuth 2.0 connection and token lifecycle for the three platforms.

Flows are built, not performed: this module produces the authorisation URL and
the token-exchange requests, and interprets the replies. The HTTP belongs to a
transport, which keeps the whole thing exercisable offline and keeps secrets
out of the layer that formats logs.

### The per-platform traps

**TikTok calls it `client_key`, not `client_id`.** Every other OAuth provider
uses `client_id`, so this is copied wrong roughly once per integration and
fails with an unhelpful error.

**Google returns a refresh token only once.** Without `access_type=offline` and
`prompt=consent`, a re-authorisation of an already-consented account returns an
access token and no refresh token — so the connection works in testing, and
then dies an hour later with nothing to refresh from. The first consent
appears to succeed either way, which is what makes it a trap.

**Facebook's first token is short-lived.** The code exchange returns a token
good for about an hour; it has to be exchanged again for a long-lived one via
`fb_exchange_token`. Skipping the second exchange produces a publisher that
works all afternoon and is dead by morning.

### PKCE

Used everywhere here, not only where mandated. The authorisation code travels
through a browser redirect and, for a desktop or mobile client, through the
operating system's URL handling; PKCE is what stops an intercepted code being
redeemable by anything but the client that started the flow.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Protocol

from .limits import limits_for
from .types import Platform, Request, Response, UTC, utcnow

AUTHORIZE_URL: dict[Platform, str] = {
    Platform.TIKTOK: "https://www.tiktok.com/v2/auth/authorize/",
    Platform.YOUTUBE: "https://accounts.google.com/o/oauth2/v2/auth",
    Platform.INSTAGRAM: "https://www.facebook.com/v21.0/dialog/oauth",
}

TOKEN_URL: dict[Platform, str] = {
    Platform.TIKTOK: "https://open.tiktokapis.com/v2/oauth/token/",
    Platform.YOUTUBE: "https://oauth2.googleapis.com/token",
    Platform.INSTAGRAM: "https://graph.facebook.com/v21.0/oauth/access_token",
}

#: Refresh this long before the access token actually expires. A token that
#: expires mid-upload fails a large transfer that had nearly finished.
REFRESH_SKEW_S = 300

#: How far ahead of the refresh-grace deadline to start warning. Two weeks is
#: enough for a customer to notice an email and reconnect an account before a
#: quarter's worth of scheduled posts starts failing.
HORIZON_WARNING_DAYS = 14


@dataclass(frozen=True, slots=True)
class PkceChallenge:
    verifier: str
    challenge: str
    method: str = "S256"


def make_pkce() -> PkceChallenge:
    """A fresh PKCE verifier and its S256 challenge."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return PkceChallenge(verifier=verifier, challenge=challenge)


def make_state() -> str:
    """CSRF state. Must be stored server-side and compared on the callback."""
    return secrets.token_urlsafe(32)


@dataclass(frozen=True, slots=True)
class ClientCredentials:
    client_id: str
    client_secret: str
    redirect_uri: str


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    """Everything needed to start a connection and finish it later."""

    url: str
    state: str
    pkce: PkceChallenge
    platform: Platform

    def to_dict(self) -> dict[str, Any]:
        # The verifier is a secret until it is spent on the token exchange.
        return {
            "url": self.url,
            "state": self.state,
            "platform": self.platform.value,
            "code_challenge": self.pkce.challenge,
        }


def _encode(params: dict[str, str]) -> str:
    from urllib.parse import urlencode

    return urlencode(params)


def authorization_url(
    platform: Platform,
    credentials: ClientCredentials,
    scopes: tuple[str, ...] = (),
    pkce: PkceChallenge | None = None,
    state: str = "",
) -> AuthorizationRequest:
    """Build the URL the creator is sent to."""
    pkce = pkce or make_pkce()
    state = state or make_state()
    scopes = scopes or limits_for(platform).scopes

    params: dict[str, str] = {
        "response_type": "code",
        "redirect_uri": credentials.redirect_uri,
        "state": state,
        "code_challenge": pkce.challenge,
        "code_challenge_method": pkce.method,
    }

    if platform is Platform.TIKTOK:
        # Not `client_id`. TikTok is the odd one out.
        params["client_key"] = credentials.client_id
        params["scope"] = ",".join(scopes)
    elif platform is Platform.YOUTUBE:
        params["client_id"] = credentials.client_id
        params["scope"] = " ".join(scopes)
        # Both of these are required to receive a refresh token, and both are
        # silently optional as far as the first consent is concerned.
        params["access_type"] = "offline"
        params["prompt"] = "consent"
        params["include_granted_scopes"] = "true"
    else:
        params["client_id"] = credentials.client_id
        params["scope"] = ",".join(scopes)

    return AuthorizationRequest(
        url=f"{AUTHORIZE_URL[platform]}?{_encode(params)}",
        state=state,
        pkce=pkce,
        platform=platform,
    )


def exchange_request(
    platform: Platform,
    credentials: ClientCredentials,
    code: str,
    verifier: str,
) -> Request:
    """Trade an authorisation code for tokens."""
    form: dict[str, str] = {
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": credentials.redirect_uri,
        "code_verifier": verifier,
        "client_secret": credentials.client_secret,
    }
    form["client_key" if platform is Platform.TIKTOK else "client_id"] = (
        credentials.client_id
    )

    return Request(
        method="POST",
        url=TOKEN_URL[platform],
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        form_body=form,
        description=f"{platform.value}: exchange authorisation code",
    )


def long_lived_exchange_request(
    credentials: ClientCredentials, short_lived_token: str
) -> Request:
    """Second Facebook exchange: ~1 hour becomes ~60 days.

    Not optional. The code exchange alone yields a token that expires the same
    afternoon, which is long enough for an integration to look finished.
    """
    return Request(
        method="GET",
        url=TOKEN_URL[Platform.INSTAGRAM],
        form_body={
            "grant_type": "fb_exchange_token",
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "fb_exchange_token": short_lived_token,
        },
        description="instagram: exchange for a long-lived token",
    )


def refresh_request(
    platform: Platform, credentials: ClientCredentials, refresh_token: str
) -> Request:
    """Renew an access token."""
    if platform is Platform.INSTAGRAM:
        # Facebook has no refresh grant: a long-lived token is re-exchanged
        # for a new long-lived token, which resets the sixty days.
        return long_lived_exchange_request(credentials, refresh_token)

    form: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_secret": credentials.client_secret,
    }
    form["client_key" if platform is Platform.TIKTOK else "client_id"] = (
        credentials.client_id
    )

    return Request(
        method="POST",
        url=TOKEN_URL[platform],
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        form_body=form,
        description=f"{platform.value}: refresh access token",
    )


@dataclass(slots=True)
class TokenSet:
    """Credentials for one account, with the clock attached."""

    account_id: str
    platform: Platform
    access_token: str
    refresh_token: str = ""
    expires_at: datetime | None = None
    scopes: tuple[str, ...] = ()
    #: When the refresh token itself stops being usable. Beyond this the
    #: creator has to reauthorise by hand, and every post scheduled past it
    #: will fail no matter how healthy the system is.
    refresh_valid_until: datetime | None = None
    obtained_at: datetime = field(default_factory=utcnow)

    def is_expired(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return self.expires_at is not None and now >= self.expires_at

    def needs_refresh(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        if self.expires_at is None:
            return False
        return now >= self.expires_at - timedelta(seconds=REFRESH_SKEW_S)

    def can_refresh(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        if not self.refresh_token:
            return False
        if self.refresh_valid_until is None:
            return True
        return now < self.refresh_valid_until

    def covers(self, moment: datetime, now: datetime | None = None) -> bool:
        """Whether a post at `moment` can expect live credentials.

        The question is not "is the access token valid now" — it will have been
        refreshed a hundred times before a post three months out fires. It is
        whether the *refresh* path survives that long unattended.
        """
        if self.refresh_valid_until is None:
            return True
        return moment <= self.refresh_valid_until

    def horizon_warning(self, moment: datetime) -> str:
        """A warning when a scheduled post sits near or past the deadline."""
        if self.refresh_valid_until is None:
            return ""
        remaining = self.refresh_valid_until - moment
        if remaining.total_seconds() < 0:
            return (
                f"{self.platform.value} credentials for {self.account_id} "
                f"stop being renewable on "
                f"{self.refresh_valid_until:%Y-%m-%d}, before this post is due "
                f"to run. It will fail unless the account is reconnected."
            )
        if remaining <= timedelta(days=HORIZON_WARNING_DAYS):
            return (
                f"{self.platform.value} credentials for {self.account_id} "
                f"expire {remaining.days}d after this post — the schedule is "
                f"running close to the reauthorisation deadline."
            )
        return ""

    def to_dict(self) -> dict[str, Any]:
        """Serialised **without** the secrets. There is no safe default."""
        return {
            "account_id": self.account_id,
            "platform": self.platform.value,
            "scopes": list(self.scopes),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "refresh_valid_until": (
                self.refresh_valid_until.isoformat()
                if self.refresh_valid_until else None
            ),
            "obtained_at": self.obtained_at.isoformat(),
            "has_refresh_token": bool(self.refresh_token),
        }


def parse_token_response(
    platform: Platform,
    account_id: str,
    response: Response,
    now: datetime | None = None,
) -> TokenSet:
    """Turn a token endpoint reply into a `TokenSet`.

    TikTok nests its payload under `data` on some endpoints and returns it flat
    on others, so both shapes are accepted rather than assumed.
    """
    now = now or utcnow()
    body = response.body
    payload = body.get("data") if isinstance(body.get("data"), dict) else body

    access = payload.get("access_token", "")
    if not access:
        raise ValueError(
            f"{platform.value} token response contained no access_token: "
            f"{sorted(payload)}"
        )

    expires_in = int(payload.get("expires_in") or 0)
    policy = limits_for(platform).tokens
    expires_at = now + timedelta(seconds=expires_in or policy.access_ttl_s)

    refresh = payload.get("refresh_token", "")
    if platform is Platform.INSTAGRAM:
        # Facebook re-exchanges the long-lived token itself; it is both the
        # access token and the thing used to renew it.
        refresh = refresh or access

    refresh_expires_in = int(payload.get("refresh_expires_in") or 0)
    refresh_valid_until = (
        now + timedelta(seconds=refresh_expires_in)
        if refresh_expires_in
        else now + timedelta(days=policy.refresh_grace_days)
    )

    scope_raw = payload.get("scope", "")
    scopes = tuple(
        part for part in scope_raw.replace(",", " ").split() if part
    )

    return TokenSet(
        account_id=account_id,
        platform=platform,
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
        scopes=scopes,
        refresh_valid_until=refresh_valid_until,
        obtained_at=now,
    )


class TokenStore(Protocol):
    def get(self, account_id: str) -> TokenSet | None: ...
    def put(self, tokens: TokenSet) -> None: ...
    def delete(self, account_id: str) -> None: ...
    def all_accounts(self) -> tuple[str, ...]: ...


class InMemoryTokenStore:
    """Reference store. **Not for production** — see `SealedTokenStore`."""

    def __init__(self) -> None:
        self._tokens: dict[str, TokenSet] = {}

    def get(self, account_id: str) -> TokenSet | None:
        return self._tokens.get(account_id)

    def put(self, tokens: TokenSet) -> None:
        self._tokens[tokens.account_id] = tokens

    def delete(self, account_id: str) -> None:
        self._tokens.pop(account_id, None)

    def all_accounts(self) -> tuple[str, ...]:
        return tuple(self._tokens)


class SealedTokenStore:
    """Wraps a store so tokens are encrypted at rest.

    Deliberately takes `seal` and `unseal` rather than implementing crypto
    here. Refresh tokens are long-lived credentials to other people's
    audiences: a compromised store is an attacker posting to every connected
    account, and that key belongs in a KMS or HSM, held by something other
    than the process that publishes.
    """

    def __init__(
        self,
        inner: TokenStore,
        seal: Callable[[str], str],
        unseal: Callable[[str], str],
    ) -> None:
        self._inner = inner
        self._seal = seal
        self._unseal = unseal

    def get(self, account_id: str) -> TokenSet | None:
        tokens = self._inner.get(account_id)
        if tokens is None:
            return None
        return TokenSet(
            account_id=tokens.account_id,
            platform=tokens.platform,
            access_token=self._unseal(tokens.access_token),
            refresh_token=(
                self._unseal(tokens.refresh_token) if tokens.refresh_token else ""
            ),
            expires_at=tokens.expires_at,
            scopes=tokens.scopes,
            refresh_valid_until=tokens.refresh_valid_until,
            obtained_at=tokens.obtained_at,
        )

    def put(self, tokens: TokenSet) -> None:
        self._inner.put(TokenSet(
            account_id=tokens.account_id,
            platform=tokens.platform,
            access_token=self._seal(tokens.access_token),
            refresh_token=(
                self._seal(tokens.refresh_token) if tokens.refresh_token else ""
            ),
            expires_at=tokens.expires_at,
            scopes=tokens.scopes,
            refresh_valid_until=tokens.refresh_valid_until,
            obtained_at=tokens.obtained_at,
        ))

    def delete(self, account_id: str) -> None:
        self._inner.delete(account_id)

    def all_accounts(self) -> tuple[str, ...]:
        return self._inner.all_accounts()


def accounts_needing_attention(
    store: TokenStore, horizon: datetime, now: datetime | None = None
) -> list[tuple[str, str]]:
    """Accounts whose credentials will not survive to `horizon`.

    Run this on a schedule and email the results. It is the difference between
    a customer reconnecting an account in March and a customer discovering in
    June that nothing has posted since March.
    """
    now = now or utcnow()
    problems: list[tuple[str, str]] = []

    for account_id in store.all_accounts():
        tokens = store.get(account_id)
        if tokens is None:
            continue
        if not tokens.can_refresh(now):
            problems.append((
                account_id,
                f"{tokens.platform.value} refresh token is no longer usable — "
                f"the account must be reconnected by hand",
            ))
            continue
        warning = tokens.horizon_warning(horizon)
        if warning:
            problems.append((account_id, warning))

    return problems
