"""Keeping access tokens alive.

`oauth.py` builds a refresh request and parses a token response. Nothing sent
one. This closes that loop: it decides *whether* to refresh, performs the
round trip, stores the result, and turns the two interesting failures into
states an operator can act on.

## Refresh ahead of expiry, not at it

`TokenSet.needs_refresh()` fires `REFRESH_SKEW_S` before the token actually
expires. That margin matters more than it looks: an upload is minutes long,
and a token that was valid when the session opened can expire while the third
chunk is in flight. Refreshing at the moment of expiry means the skew is
whatever the clock drift happens to be.

## The two failures are not the same

* **The refresh itself was rejected** (`invalid_grant`, a revoked app, a
  password change) — nothing retryable has happened. The creator has to
  reauthorise, and every post for that account will fail until they do. This
  raises `ReauthRequired`, and the engine turns it into `NEEDS_ATTENTION`
  rather than burning attempts on it.
* **The refresh could not be delivered** (timeout, DNS, a 500) — the
  credentials are probably fine and the network is not. This raises
  `RefreshFailed`, which is retryable, because giving up here would ask a
  human to reconnect a perfectly healthy account.

Conflating the two produces either a system that never tells you to reconnect
or one that tells you constantly.

## One refresh at a time, per account

A refresh is a write: several platforms rotate the refresh token and invalidate
the old one on use. Two workers refreshing the same account concurrently means
the loser holds a token the platform has already retired — the account then
fails on every subsequent post with credentials that look present and valid.
A per-account lock makes the second caller wait and then find fresh tokens
already stored.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .oauth import ClientCredentials, TokenSet, parse_token_response, refresh_request
from .types import Platform, Response, utcnow

__all__ = [
    "TokenRefresher",
    "RefreshFailed",
    "ReauthRequired",
    "RefreshResult",
]

#: Error codes that mean the grant is dead. Retrying cannot help and the
#: creator must reauthorise. Names differ per platform, so all three
#: vocabularies are listed rather than assuming Google's.
_DEAD_GRANT = frozenset({
    "invalid_grant",
    "invalid_request",
    "unauthorized_client",
    "invalid_client",
    "access_denied",
    # TikTok
    "invalid_refresh_token",
    "refresh_token_expired",
    # Facebook / Instagram
    "oauthexception",
})


class RefreshFailed(Exception):
    """The refresh could not be completed. Probably transient."""


class ReauthRequired(Exception):
    """The grant is dead. A human must reconnect the account."""


@dataclass(slots=True)
class RefreshResult:
    tokens: TokenSet
    refreshed: bool
    reason: str = ""


@dataclass
class TokenRefresher:
    """Refreshes access tokens against the platforms, and stores them.

    `credentials` maps a platform to its app registration. A platform absent
    from the map cannot be refreshed at all — which is a configuration error
    worth naming loudly rather than a reason to fail the post obscurely.
    """

    transport: Any
    store: Any
    credentials: dict[Platform, ClientCredentials]
    clock: Callable[[], datetime] = utcnow
    _locks: dict[str, threading.Lock] = field(
        default_factory=lambda: defaultdict(threading.Lock), init=False, repr=False
    )
    _guard: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def ensure_fresh(
        self, account_id: str, now: datetime | None = None
    ) -> RefreshResult:
        """Tokens good for the next few minutes, refreshing if needed."""

        now = now or self.clock()
        tokens = self.store.get(account_id)
        if tokens is None:
            raise ReauthRequired(f"no credentials stored for {account_id}")

        if not tokens.needs_refresh(now):
            return RefreshResult(tokens, refreshed=False, reason="still valid")

        with self._lock_for(account_id):
            # Re-read inside the lock. Another worker may have refreshed while
            # this one waited, and refreshing again would retire the token it
            # just stored.
            current = self.store.get(account_id) or tokens
            if not current.needs_refresh(now):
                return RefreshResult(
                    current, refreshed=False, reason="refreshed by another worker"
                )
            return self._refresh(current, now)

    # -- internals ---------------------------------------------------------

    def _lock_for(self, account_id: str) -> threading.Lock:
        with self._guard:
            return self._locks[account_id]

    def _refresh(self, tokens: TokenSet, now: datetime) -> RefreshResult:
        if not tokens.can_refresh(now):
            raise ReauthRequired(
                f"{tokens.platform.value} credentials for {tokens.account_id} "
                f"can no longer be renewed — the account must be reconnected"
            )

        credentials = self.credentials.get(tokens.platform)
        if credentials is None:
            raise RefreshFailed(
                f"no client credentials configured for "
                f"{tokens.platform.value} — cannot refresh "
                f"{tokens.account_id}. This is a deployment problem, not a "
                f"problem with the account."
            )

        request = refresh_request(
            tokens.platform, credentials, tokens.refresh_token
        )
        try:
            response = self.transport.send(request)
        except TimeoutError as error:
            raise RefreshFailed(f"token refresh timed out: {error}") from error
        except Exception as error:                              # noqa: BLE001
            raise RefreshFailed(f"token refresh failed: {error}") from error

        if not response.ok:
            self._raise_for(tokens, response)

        try:
            fresh = parse_token_response(
                tokens.platform, tokens.account_id, response, now
            )
        except ValueError as error:
            raise RefreshFailed(
                f"{tokens.platform.value} returned a token response that could "
                f"not be read: {error}"
            ) from error

        # Platforms that do not re-issue a refresh token leave the old one in
        # force. Dropping it here would leave the account unable to refresh
        # again — a fault that only shows up one token lifetime later.
        if not fresh.refresh_token:
            fresh.refresh_token = tokens.refresh_token
            fresh.refresh_valid_until = tokens.refresh_valid_until

        self.store.put(fresh)
        return RefreshResult(fresh, refreshed=True, reason="refreshed")

    def _raise_for(self, tokens: TokenSet, response: Response) -> None:
        body = response.body if isinstance(response.body, dict) else {}
        payload = body.get("data") if isinstance(body.get("data"), dict) else body
        error = payload.get("error")
        code = ""
        if isinstance(error, dict):
            code = str(error.get("type") or error.get("code") or "")
        elif isinstance(error, str):
            code = error
        code = code.strip().lower()
        detail = (
            payload.get("error_description")
            or (error.get("message") if isinstance(error, dict) else "")
            or payload.get("message")
            or f"HTTP {response.status}"
        )

        if code in _DEAD_GRANT or response.status in (400, 401, 403):
            raise ReauthRequired(
                f"{tokens.platform.value} refused to renew credentials for "
                f"{tokens.account_id} ({code or response.status}): {detail}. "
                f"The account must be reconnected."
            )
        raise RefreshFailed(
            f"{tokens.platform.value} token refresh returned "
            f"{response.status}: {detail}"
        )
