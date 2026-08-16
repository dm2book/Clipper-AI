"""Sign in, refresh, sign out, and who am I.

Thin on purpose. Every rule — the identical answers that stop account
enumeration, the timing equalisation, refresh rotation and reuse detection,
rate limiting, the audit trail — lives in `clipforge.auth` and is tested there
against real bcrypt and a real database. This module turns HTTP into those
calls and turns their exceptions into responses, and does nothing else.

The client IP comes from the socket, not from `X-Forwarded-For`. That header is
attacker-controlled unless a proxy you trust overwrites it, and trusting it
blindly hands anyone a way to spread their login attempts across a million
fictitious addresses and never hit the per-IP limit. A deployment behind a real
proxy should set `CLIPFORGE_API_TRUSTED_PROXY=1` and terminate that trust at
the proxy, which is the only place it can be established.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Header, HTTPException, Request, Response

from ...auth.types import AuthError
from ..cookies import (
    CSRF_HEADER,
    CookieConfig,
    cookie_config_from_env,
    csrf_ok,
    new_csrf_token,
    new_device_id,
)
from ..deps import ContextDep, LivePrincipalDep, PrincipalDep, _services
from ..schemas import (
    ChangePasswordRequest,
    DeviceOut,
    DeleteAccountRequest,
    EmailRequest,
    LoginRequest,
    LoginResponse,
    MembershipOut,
    MfaChallengeOut,
    MfaConfirmRequest,
    MfaConfirmResponse,
    MfaEnrolRequest,
    MfaEnrolResponse,
    MfaFactorOut,
    MfaStatusOut,
    MfaVerifyRequest,
    MeResponse,
    MessageResponse,
    PasswordResetRequest,
    RefreshRequest,
    SignUpRequest,
    TokenPairOut,
    VerifyEmailRequest,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def client_ip(request: Request) -> str:
    if os.environ.get("CLIPFORGE_API_TRUSTED_PROXY", "").strip() in ("1", "true"):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            # Left-most is the original client, when the proxy is honest.
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""




# ---------------------------------------------------------------------------
# Cookie plumbing
# ---------------------------------------------------------------------------


def cookies_for(request: Request) -> CookieConfig:
    """The cookie policy, cached on the app so it is read once."""
    held = getattr(request.app.state, "cookie_config", None)
    if held is None:
        held = cookie_config_from_env()
        request.app.state.cookie_config = held
    return held


def read_device_id(request: Request) -> str:
    """The device cookie, or empty. Never minted here.

    Minting on a read would give a device id to every anonymous visitor and
    fill the table with rows for people who never signed in.
    """

    policy = cookies_for(request)
    return (
        request.cookies.get(policy.device_name)
        or request.cookies.get("clipforge_device")
        or ""
    )


def read_refresh(request: Request, body_token: str = "") -> str:
    """Prefer the body, fall back to the cookie.

    Both are supported on purpose. A browser uses the cookie and never handles
    the token in script; a CLI or a mobile client has no cookie jar worth the
    name and passes it explicitly. Refusing one of the two would either break
    non-browser clients or force the browser to hold a long-lived credential
    where injected script can read it.
    """

    if body_token:
        return body_token
    policy = cookies_for(request)
    return (
        request.cookies.get(policy.refresh_name)
        or request.cookies.get("clipforge_refresh")
        or ""
    )


def require_csrf(request: Request, header_value: str) -> None:
    """Enforce double-submit, but only for a request authenticated by cookie.

    A request carrying its own refresh token in the body is not a CSRF
    candidate: an attacker's page cannot read the token to put it there. So
    the check applies exactly when the cookie is what is being spent, which is
    also the only case where the browser attached credentials by itself.
    """

    policy = cookies_for(request)
    cookie_value = (
        request.cookies.get(policy.csrf_name)
        or request.cookies.get("clipforge_csrf")
        or ""
    )
    if csrf_ok(cookie_value, header_value):
        return
    raise HTTPException(
        status_code=403,
        detail={
            "code": "CSRF_FAILED",
            "message": (
                "This request is missing its CSRF token. Reload the page and "
                "try again."
            ),
        },
    )


def attach_session_cookies(
    response: Response, request: Request, pair, device_id: str,
) -> str:
    """Set the refresh, device and CSRF cookies for a fresh session.

    Returns the CSRF token so it can also go in the response body — a client
    that reads it from the body avoids parsing cookies at all, and the cookie
    is still what the server compares against.
    """

    policy = cookies_for(request)
    response.set_cookie(
        policy.refresh_name, pair.refresh_token,
        max_age=policy.refresh_max_age_s, httponly=True,
        secure=policy.secure, samesite=policy.same_site,
        path=policy.effective_refresh_path,
    )
    response.set_cookie(
        policy.device_name, device_id,
        max_age=policy.device_max_age_s, httponly=True,
        secure=policy.secure, samesite=policy.same_site, path="/",
    )
    token = new_csrf_token()
    # Deliberately *not* HttpOnly: the whole double-submit scheme depends on
    # the page being able to read this one and echo it in a header.
    response.set_cookie(
        policy.csrf_name, token,
        max_age=policy.refresh_max_age_s, httponly=False,
        secure=policy.secure, samesite=policy.same_site, path="/",
    )
    return token


def clear_session_cookies(response: Response, request: Request) -> None:
    policy = cookies_for(request)
    response.delete_cookie(policy.refresh_name,
                           path=policy.effective_refresh_path)
    response.delete_cookie(policy.csrf_name, path="/")
    # The device cookie survives sign-out on purpose: it is what makes the
    # *next* sign-in a recognised device rather than a new-device alert every
    # time somebody logs out.



def _tokens(pair) -> TokenPairOut:
    return TokenPairOut(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in_s,
        session_id=pair.session_id,
        tenant_id=pair.tenant_id,
    )


@router.post("/signup", response_model=MessageResponse)
async def sign_up(body: SignUpRequest, request: Request) -> MessageResponse:
    """Register an address.

    The response is identical whether or not the address was already taken.
    That is not a simplification — it is the whole defence, and the owner of an
    existing address is told by email instead.
    """

    services = _services(request)
    result = services.auth.sign_up(
        body.email, body.password,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    return MessageResponse(message=result.message)


def _memberships(result) -> list[MembershipOut]:
    return [
        MembershipOut(
            user_id=m.user_id, tenant_id=m.tenant_id,
            tenant_name=m.tenant_name, role=m.role, active=m.active,
        )
        for m in result.memberships
    ]


def _login_response(
    result, request: Request, response: Response, device_id: str,
) -> LoginResponse:
    """Turn a `LoginResult` into a response, setting cookies when complete.

    A challenge sets no cookies at all. Half a login must leave no credential
    behind — a refresh cookie written before the second factor was presented
    would be a signed-in browser that merely has not been asked yet.
    """

    if result.mfa is not None:
        return LoginResponse(
            tokens=None, memberships=_memberships(result),
            unverified=result.unverified,
            mfa=MfaChallengeOut(**result.mfa.to_dict()),
        )
    csrf = attach_session_cookies(response, request, result.tokens, device_id)
    return LoginResponse(
        tokens=_tokens(result.tokens), memberships=_memberships(result),
        unverified=result.unverified, csrf_token=csrf,
    )


@router.post("/login", response_model=LoginResponse)
async def log_in(
    body: LoginRequest, request: Request, response: Response
) -> LoginResponse:
    """Exchange a password for a session, or for a second-factor challenge."""

    services = _services(request)
    device_id = read_device_id(request) or new_device_id()
    result = services.auth.log_in(
        body.email, body.password,
        tenant_id=body.tenant_id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
        device_id=device_id,
    )
    return _login_response(result, request, response, device_id)


@router.post("/mfa/verify", response_model=LoginResponse)
async def complete_mfa(
    body: MfaVerifyRequest, request: Request, response: Response
) -> LoginResponse:
    """Finish a login that stopped at the second factor.

    Takes a TOTP code or a recovery code; the service decides which it was and
    records it. Unauthenticated, because the challenge token is the credential
    — there is no session yet, which is the entire point.
    """

    services = _services(request)
    device_id = read_device_id(request) or new_device_id()
    result = services.auth.complete_mfa(
        body.challenge_token, body.code,
        tenant_id=body.tenant_id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
        device_id=device_id,
    )
    return _login_response(result, request, response, device_id)


@router.post("/refresh", response_model=TokenPairOut)
async def refresh(
    request: Request,
    response: Response,
    body: RefreshRequest | None = None,
    x_csrf_token: str = Header(default="", alias=CSRF_HEADER),
) -> TokenPairOut:
    """Rotate a refresh token, from the body or from the cookie.

    Presenting one that has already been rotated away from revokes the whole
    session family — the client sees a 401 and must sign in again, which is the
    correct outcome whether it was a race or a theft.

    CSRF is checked only when the cookie is what is being spent. A caller that
    supplied the token itself cannot be a cross-site forgery, because the
    attacker's page could not read the token to send it.
    """

    services = _services(request)
    supplied = body.refresh_token if body and body.refresh_token else ""
    if not supplied:
        require_csrf(request, x_csrf_token)
    token = read_refresh(request, supplied)
    if not token:
        raise HTTPException(
            status_code=401,
            detail={"code": "NOT_AUTHENTICATED",
                    "message": "No refresh token was supplied."},
        )

    pair = services.auth.refresh(
        token,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    if not supplied:
        # Rotation means the cookie now holds a spent token. Not replacing it
        # would log the browser out on its next refresh.
        attach_session_cookies(
            response, request, pair, read_device_id(request),
        )
    return _tokens(pair)


@router.post("/logout", status_code=204)
async def log_out(
    request: Request,
    response: Response,
    body: RefreshRequest | None = None,
    x_csrf_token: str = Header(default="", alias=CSRF_HEADER),
) -> Response:
    services = _services(request)
    supplied = body.refresh_token if body and body.refresh_token else ""
    if not supplied:
        require_csrf(request, x_csrf_token)
    token = read_refresh(request, supplied)
    if token:
        services.auth.log_out(token, ip=client_ip(request))
    # 204 whether or not anything was revoked. "That token was already dead"
    # is not information a caller needs, and it is information an attacker
    # holding a stolen token would like.
    out = Response(status_code=204)
    clear_session_cookies(out, request)
    return out


@router.post("/logout-everywhere", status_code=204)
async def log_out_everywhere(
    principal: LivePrincipalDep, request: Request
) -> Response:
    services = _services(request)
    services.auth.log_out_everywhere(
        principal.identity_id, ip=client_ip(request)
    )
    return Response(status_code=204)


@router.get("/me", response_model=MeResponse)
async def me(context: ContextDep, principal: PrincipalDep) -> MeResponse:
    return MeResponse(
        identity_id=principal.identity_id,
        email=principal.email,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        role=principal.role,
        session_id=principal.session_id,
        expires_at=principal.expires_at,
        memberships=context.memberships(),
    )


@router.post("/password", response_model=MessageResponse)
async def change_password(
    body: ChangePasswordRequest,
    principal: LivePrincipalDep,
    request: Request,
) -> MessageResponse:
    """Change a password from inside a session.

    `live_principal`, not the stateless one: a revoked session must not be
    able to set a new password during the fifteen minutes its access token
    remains cryptographically valid.
    """

    services = _services(request)
    services.auth.change_password(
        principal.identity_id, body.current_password, body.new_password,
        ip=client_ip(request),
        # The tab doing the change keeps its session; every other one dies.
        keep_current_session=principal.session_id,
    )
    return MessageResponse(
        message="Password changed. Every other device has been signed out."
    )


@router.post("/password/reset-request", response_model=MessageResponse)
async def request_reset(
    body: EmailRequest, request: Request
) -> MessageResponse:
    """Ask for a reset link.

    Same uninformative contract as signup: the answer does not depend on
    whether the address is registered.
    """

    services = _services(request)
    services.auth.request_password_reset(body.email, ip=client_ip(request))
    return MessageResponse(
        message=(
            "If that address has an account, a reset link is on its way."
        )
    )


@router.post("/password/reset", response_model=MessageResponse)
async def reset_password(
    body: PasswordResetRequest, request: Request
) -> MessageResponse:
    """Spend a reset token on a new password.

    The other half of `reset-request`, which shipped without it — so a link
    could be asked for and never redeemed, and the reset flow had no ending.
    Every session for the identity dies here, which is what makes "reset your
    password" a remedy for a compromise rather than a gesture.
    """

    services = _services(request)
    services.auth.reset_password(
        body.token, body.new_password, ip=client_ip(request),
    )
    return MessageResponse(
        message=(
            "Password set. Every device has been signed out — sign in again "
            "with the new password."
        )
    )


@router.post("/verify", response_model=MessageResponse)
async def verify_email(
    body: VerifyEmailRequest, request: Request
) -> MessageResponse:
    """Confirm an address from the emailed token.

    Unauthenticated on purpose: the token *is* the credential. Requiring a
    session first would mean that a deployment which blocks unverified sign-in
    has accounts that can never verify.
    """

    services = _services(request)
    identity = services.auth.verify_email(body.token, ip=client_ip(request))
    return MessageResponse(
        message=f"{identity.email} is confirmed. You can sign in now."
    )


@router.post("/verify/resend", response_model=MessageResponse)
async def resend_verification(
    body: EmailRequest, request: Request
) -> MessageResponse:
    """Send another confirmation link, if one is owed."""

    services = _services(request)
    services.auth.resend_verification(body.email, ip=client_ip(request))
    return MessageResponse(
        message=(
            "If that address has an unconfirmed account, a new link is on "
            "its way."
        )
    )


@router.post("/account/delete", response_model=MessageResponse)
async def request_deletion(
    body: DeleteAccountRequest, principal: LivePrincipalDep, request: Request
) -> MessageResponse:
    """Schedule this account for deletion after a grace period.

    `LivePrincipalDep` rather than the stateless dependency: this pays one
    database read to confirm the session has not been revoked, which is the
    right trade for the only action here that cannot be undone once the grace
    window closes.

    The password is re-checked first through `check_password`, which exists for
    this and is rate limited on the same bucket as a password change — an
    endpoint that confirms a password without a limit is a password oracle for
    anyone holding a stolen session.
    """

    services = _services(request)
    ip = client_ip(request)
    services.auth.check_password(principal.identity_id, body.password, ip=ip)
    identity = services.auth.request_deletion(principal.identity_id, ip=ip)
    when = getattr(identity, "delete_after", None)
    return MessageResponse(
        message=(
            "Account scheduled for deletion"
            + (f" on {when:%d %B %Y}" if when else "")
            + ". Sign in again before then to cancel it."
        )
    )


# ---------------------------------------------------------------------------
# Second factors
# ---------------------------------------------------------------------------


@router.get("/mfa", response_model=MfaStatusOut)
async def mfa_status(principal: PrincipalDep, request: Request) -> MfaStatusOut:
    services = _services(request)
    factors = services.auth.store.factors_for(principal.identity_id)
    return MfaStatusOut(
        enabled=any(f.active for f in factors),
        factors=[
            MfaFactorOut(
                factor_id=f.factor_id, kind=f.kind.value, label=f.label,
                active=f.active, created_at=f.created_at,
                last_used_at=f.last_used_at,
            )
            for f in factors
        ],
        recovery_codes_remaining=services.auth.recovery_codes_remaining(
            principal.identity_id
        ),
    )


@router.post("/mfa/enrol", response_model=MfaEnrolResponse)
async def begin_enrolment(
    body: MfaEnrolRequest, principal: LivePrincipalDep, request: Request
) -> MfaEnrolResponse:
    """Start adding an authenticator. Does not switch anything on.

    `LivePrincipalDep` because this hands out a secret, and a revoked session
    must not be able to add a factor to somebody's account.
    """

    services = _services(request)
    started = services.auth.begin_enrolment(
        principal.identity_id, label=body.label, ip=client_ip(request),
    )
    return MfaEnrolResponse(**started.to_dict())


@router.post("/mfa/confirm", response_model=MfaConfirmResponse)
async def confirm_enrolment(
    body: MfaConfirmRequest, principal: LivePrincipalDep, request: Request
) -> MfaConfirmResponse:
    """Prove the authenticator works, and get the recovery codes."""

    services = _services(request)
    _factor, codes = services.auth.confirm_enrolment(
        principal.identity_id, body.factor_id, body.code,
        ip=client_ip(request),
    )
    return MfaConfirmResponse(
        recovery_codes=list(codes),
        message=(
            "Two-factor authentication is on. Save these recovery codes "
            "somewhere safe — they are shown once and cannot be retrieved."
        ),
    )


@router.post("/mfa/disable", response_model=MessageResponse)
async def disable_mfa(
    body: DeleteAccountRequest, principal: LivePrincipalDep, request: Request
) -> MessageResponse:
    """Turn every factor off. Requires the password again.

    Reuses `DeleteAccountRequest` because the shape is the same — a password
    and nothing else — and inventing a second identical model would only give
    the generated client two names for one thing.
    """

    services = _services(request)
    ip = client_ip(request)
    services.auth.check_password(principal.identity_id, body.password, ip=ip)
    removed = services.auth.disable_mfa(principal.identity_id, ip=ip)
    return MessageResponse(
        message=(
            f"Two-factor authentication is off ({removed} factor(s) removed). "
            f"Your password is now the only thing protecting this account."
            if removed else "There was no second factor on this account."
        )
    )


@router.post("/mfa/recovery-codes", response_model=MfaConfirmResponse)
async def regenerate_recovery_codes(
    body: DeleteAccountRequest, principal: LivePrincipalDep, request: Request
) -> MfaConfirmResponse:
    """Issue a fresh set, invalidating the old one."""

    services = _services(request)
    ip = client_ip(request)
    services.auth.check_password(principal.identity_id, body.password, ip=ip)
    codes = services.auth.regenerate_recovery_codes(
        principal.identity_id, ip=ip
    )
    return MfaConfirmResponse(
        recovery_codes=list(codes),
        message="New recovery codes. Every previous code has stopped working.",
    )


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


@router.get("/devices", response_model=list[DeviceOut])
async def list_devices(
    principal: PrincipalDep, request: Request
) -> list[DeviceOut]:
    services = _services(request)
    here = read_device_id(request)
    return [
        DeviceOut(
            device_id=d.device_id, label=d.label, user_agent=d.user_agent,
            last_ip=d.last_ip, first_seen_at=d.first_seen_at,
            last_seen_at=d.last_seen_at, active=d.active,
            current=bool(here) and d.device_id == here,
        )
        for d in services.auth.devices(principal.identity_id)
    ]


@router.post("/devices/{device_id}/revoke", response_model=MessageResponse)
async def revoke_device(
    device_id: str, principal: LivePrincipalDep, request: Request
) -> MessageResponse:
    """Sign one device out and stop its sessions refreshing."""

    services = _services(request)
    revoked = services.auth.revoke_device(
        principal.identity_id, device_id, ip=client_ip(request),
    )
    return MessageResponse(
        message=f"Device signed out ({revoked} session(s) ended)."
    )
