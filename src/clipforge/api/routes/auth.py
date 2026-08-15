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

from fastapi import APIRouter, Request, Response

from ...auth.types import AuthError
from ..deps import ContextDep, LivePrincipalDep, PrincipalDep, _services
from ..schemas import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    MembershipOut,
    MeResponse,
    MessageResponse,
    RefreshRequest,
    SignUpRequest,
    TokenPairOut,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def client_ip(request: Request) -> str:
    if os.environ.get("CLIPFORGE_API_TRUSTED_PROXY", "").strip() in ("1", "true"):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            # Left-most is the original client, when the proxy is honest.
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


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


@router.post("/login", response_model=LoginResponse)
async def log_in(body: LoginRequest, request: Request) -> LoginResponse:
    services = _services(request)
    result = services.auth.log_in(
        body.email, body.password,
        tenant_id=body.tenant_id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    return LoginResponse(
        tokens=_tokens(result.tokens),
        memberships=[
            MembershipOut(
                user_id=m.user_id, tenant_id=m.tenant_id,
                tenant_name=m.tenant_name, role=m.role, active=m.active,
            )
            for m in result.memberships
        ],
        unverified=result.unverified,
    )


@router.post("/refresh", response_model=TokenPairOut)
async def refresh(body: RefreshRequest, request: Request) -> TokenPairOut:
    """Rotate a refresh token.

    Presenting one that has already been rotated away from revokes the whole
    session family — the client sees a 401 and must sign in again, which is the
    correct outcome whether it was a race or a theft.
    """

    services = _services(request)
    pair = services.auth.refresh(
        body.refresh_token,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    return _tokens(pair)


@router.post("/logout", status_code=204)
async def log_out(body: RefreshRequest, request: Request) -> Response:
    services = _services(request)
    services.auth.log_out(body.refresh_token, ip=client_ip(request))
    # 204 whether or not anything was revoked. "That token was already dead"
    # is not information a caller needs, and it is information an attacker
    # holding a stolen token would like.
    return Response(status_code=204)


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
    body: SignUpRequest, request: Request
) -> MessageResponse:
    """Ask for a reset link.

    Takes the signup shape because it needs only the address; the password
    field is ignored. Same uninformative contract: the answer does not depend
    on whether the address is registered.
    """

    services = _services(request)
    services.auth.request_password_reset(body.email, ip=client_ip(request))
    return MessageResponse(
        message=(
            "If that address has an account, a reset link is on its way."
        )
    )
