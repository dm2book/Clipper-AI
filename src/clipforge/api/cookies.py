"""Cookies, and the CSRF protection that having cookies makes necessary.

## What goes in a cookie, and what does not

Only two things: the **refresh token** and the **device id**. Both are
`HttpOnly`, so injected script cannot read them.

The **access token stays out of cookies** and is returned in the response body
for the client to hold in memory. That is the decision the rest of this file
follows from, and it is worth the paragraph:

- A cookie is attached by the browser to every request to this origin, whether
  the page meant to send it or not. That is precisely what CSRF is. Put the
  access token in a cookie and *every* mutating endpoint needs CSRF checking.
- Keep it in memory and only the two endpoints that read the refresh cookie —
  refresh and logout — need protecting. Everything else authenticates with an
  `Authorization` header, which a browser never attaches on its own and which
  is therefore immune to CSRF by construction.
- The cost is that a page reload loses the access token. That is what the
  refresh cookie is for, and the reload path exercises it on every visit,
  which is much better than a refresh path that only runs after fifteen
  minutes and is therefore only tested in production.

`web/src/api/client.ts` documented the old localStorage approach as "the
honest option until this is set up". This is that.

## Double-submit, and why it is enough here

The CSRF token is sent twice: in a readable cookie and in an `X-CSRF-Token`
header. An attacker's page can *cause* the cookie to be sent but cannot read
it to copy it into the header, because the same-origin policy stops them
reading another origin's cookies. Comparison is `compare_digest`.

This is weaker than a per-session server-side token in one specific way: it
trusts that nothing can write cookies for this domain. A subdomain takeover
breaks it. `__Host-` prefixes are used where the browser supports them, which
forbids a `Domain` attribute and so keeps a sibling subdomain from setting
them.

`SameSite=Lax` is a second, independent layer. Not `Strict`, because that also
suppresses the cookie when arriving from a link in an email — which is exactly
how the verification and reset flows land.
"""

from __future__ import annotations

import hmac
import os
import secrets
from dataclasses import dataclass

__all__ = [
    "CookieConfig",
    "cookie_config_from_env",
    "REFRESH_COOKIE",
    "DEVICE_COOKIE",
    "CSRF_COOKIE",
    "CSRF_HEADER",
    "new_csrf_token",
    "new_device_id",
    "csrf_ok",
]

#: `__Host-` is a real security control, not decoration: a browser refuses a
#: `__Host-` cookie that carries a `Domain` attribute or a `Path` other than
#: `/`, which stops `evil.example.com` from setting one that `app.example.com`
#: would then read. It also requires `Secure`, so these names are only usable
#: over HTTPS — `cookie_config_from_env` falls back to unprefixed names when
#: running insecurely for local development.
REFRESH_COOKIE_SECURE = "__Host-clipforge_refresh"
DEVICE_COOKIE_SECURE = "__Host-clipforge_device"
CSRF_COOKIE_SECURE = "__Host-clipforge_csrf"

REFRESH_COOKIE = "clipforge_refresh"
DEVICE_COOKIE = "clipforge_device"
CSRF_COOKIE = "clipforge_csrf"

CSRF_HEADER = "X-CSRF-Token"

#: 400 days is the ceiling Chrome enforces on any cookie; asking for longer
#: silently gets this anyway. A device identity that expires sooner would keep
#: alerting the same person about the same laptop.
DEVICE_MAX_AGE_S = 400 * 24 * 3600


@dataclass(frozen=True, slots=True)
class CookieConfig:
    """How this deployment sets cookies."""

    secure: bool = True
    #: `lax` rather than `strict`. See the module docstring.
    same_site: str = "lax"
    refresh_max_age_s: int = 60 * 60 * 24 * 14
    device_max_age_s: int = DEVICE_MAX_AGE_S
    #: Scoping the refresh cookie to the auth prefix means it is not attached
    #: to the hundreds of ordinary API calls that have no use for it, which
    #: shrinks the surface on which it can leak. `__Host-` forbids a path
    #: other than `/`, so this only applies to the insecure local names.
    refresh_path: str = "/api/v1/auth"

    @property
    def refresh_name(self) -> str:
        return REFRESH_COOKIE_SECURE if self.secure else REFRESH_COOKIE

    @property
    def device_name(self) -> str:
        return DEVICE_COOKIE_SECURE if self.secure else DEVICE_COOKIE

    @property
    def csrf_name(self) -> str:
        return CSRF_COOKIE_SECURE if self.secure else CSRF_COOKIE

    @property
    def effective_refresh_path(self) -> str:
        return "/" if self.secure else self.refresh_path


def cookie_config_from_env() -> CookieConfig:
    """Read the cookie policy, defaulting to secure.

    `CLIPFORGE_COOKIE_SECURE=0` is what a developer sets to work over
    `http://localhost`, and it is the only way to get insecure cookies — the
    default is secure, so forgetting to configure anything produces the safe
    behaviour rather than the convenient one.
    """

    secure = os.environ.get("CLIPFORGE_COOKIE_SECURE", "1").strip().lower()
    same_site = os.environ.get("CLIPFORGE_COOKIE_SAMESITE", "lax").strip().lower()
    if same_site not in ("lax", "strict", "none"):
        same_site = "lax"
    return CookieConfig(
        secure=secure not in ("0", "false", "no"),
        same_site=same_site,
    )


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def new_device_id() -> str:
    return f"dev_{secrets.token_urlsafe(24)}"


def csrf_ok(cookie_value: str, header_value: str) -> bool:
    """Constant-time double-submit comparison.

    Both halves must be present. An empty cookie matching an empty header
    would otherwise pass, which is the state every cross-site request is in.
    """

    if not cookie_value or not header_value:
        return False
    return hmac.compare_digest(cookie_value, header_value)
