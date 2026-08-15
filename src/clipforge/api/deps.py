"""What every request needs: who is asking, and what they may see.

## The tenant comes from the token, never from the request

There is no `?tenant_id=` anywhere in this API, and there must never be. The
tenant is a claim inside a signed access token, so asking for another
customer's data means forging a signature rather than editing a URL. Every
read then goes through `unit_of_work(principal.tenant_id)`, which sets
`app.tenant_id` for the transaction and puts row-level security underneath the
Python — two independent checks, and the database's one does not trust this
layer at all.

## `Context` is built once per request

The services it holds are expensive to construct — a connection pool, a bcrypt
hasher that builds a dummy hash on init — so they live on the app and are
handed to a request rather than rebuilt for it. `Context` is the small,
per-request thing: who is asking and which tenant they are in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Iterator

from fastapi import Depends, Header, HTTPException, Request

from ..auth.types import AuthError, Principal
from .schemas import MembershipOut

__all__ = [
    "Context",
    "Services",
    "current_principal",
    "current_context",
    "require_role",
    "PrincipalDep",
    "ContextDep",
]


@dataclass
class Services:
    """Everything the app builds once and shares.

    Held on `app.state` rather than in a module global so a test can stand up
    two apps with two databases in one process — which `tests/test_api.py`
    does, and which a module global quietly makes impossible.
    """

    database: Any
    auth: Any
    #: Optional. Present only when the deployment has one configured, and the
    #: capability list reports its absence rather than the UI guessing.
    acquisition_factory: Any = None

    def close(self) -> None:
        for held in (self.database, getattr(self.auth, "store", None)):
            if held is not None and hasattr(held, "close"):
                held.close()


@dataclass(slots=True)
class Context:
    """One request's identity and scope."""

    principal: Principal
    services: Services

    @property
    def tenant_id(self) -> str:
        return self.principal.tenant_id

    def unit_of_work(self):
        """A tenant-scoped transaction.

        Refuses rather than falling back to an unscoped read when the token
        carries no tenant. A session with no workspace is legitimate — someone
        who has signed up and not yet joined one — but it may not read data,
        and the alternative to refusing is a query that returns everything.
        """

        if not self.tenant_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "NO_WORKSPACE",
                    "message": (
                        "This account is not a member of any workspace yet."
                    ),
                },
            )
        return self.services.database.unit_of_work(self.tenant_id)

    def memberships(self) -> list[MembershipOut]:
        held = self.services.auth.store.memberships(self.principal.identity_id)
        return [
            MembershipOut(
                user_id=m.user_id, tenant_id=m.tenant_id,
                tenant_name=m.tenant_name, role=m.role, active=m.active,
            )
            for m in held
        ]


def _services(request: Request) -> Services:
    services = getattr(request.app.state, "services", None)
    if services is None:                                    # pragma: no cover
        raise HTTPException(
            status_code=503,
            detail={"code": "NOT_READY", "message": "The API is starting up."},
        )
    return services


def current_principal(
    request: Request,
    authorization: Annotated[str, Header()] = "",
) -> Principal:
    """Verify the bearer token, or refuse.

    Deliberately stateless: no database read. That is the whole reason the
    access token is a signed blob and the whole reason it lives fifteen
    minutes. Endpoints that change something destructive should ask for
    `live_principal` instead and pay one read to catch a revoked session.
    """

    services = _services(request)
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "NOT_AUTHENTICATED",
                "message": "Sign in to continue.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return services.auth.authenticate(token)
    except AuthError as error:
        raise HTTPException(
            status_code=error.status,
            detail={"code": error.code, "message": error.message},
            headers={"WWW-Authenticate": "Bearer"},
        ) from error


def live_principal(
    request: Request,
    authorization: Annotated[str, Header()] = "",
) -> Principal:
    """Verify the token *and* that the session is still alive.

    One database read. Worth it on anything a revoked session must not be able
    to do — the fifteen minutes between a "sign out everywhere" and a token's
    natural expiry is fifteen minutes too long for changing a password or
    disconnecting an account.
    """

    services = _services(request)
    _, _, token = authorization.partition(" ")
    try:
        return services.auth.authenticate_live(token)
    except AuthError as error:
        raise HTTPException(
            status_code=error.status,
            detail={"code": error.code, "message": error.message},
            headers={"WWW-Authenticate": "Bearer"},
        ) from error


def current_context(
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
) -> Context:
    return Context(principal=principal, services=_services(request))


PrincipalDep = Annotated[Principal, Depends(current_principal)]
LivePrincipalDep = Annotated[Principal, Depends(live_principal)]
ContextDep = Annotated[Context, Depends(current_context)]


#: Ordered by blast radius, matching `empire.tenancy.Role`. A check is
#: "at least this role", so a comparison is an index rather than a set of
#: special cases that drifts.
_ROLE_ORDER = ("viewer", "analyst", "editor", "operator", "admin", "owner")


def require_role(minimum: str):
    """A dependency that refuses anyone below `minimum`.

    Coarse on purpose. The fine-grained permission table lives in
    `empire.tenancy` and is the authority; this is the transport-level gate
    that keeps a viewer from reaching a mutating endpoint at all.
    """

    def check(context: ContextDep) -> Context:
        role = (context.principal.role or "viewer").lower()
        try:
            held = _ROLE_ORDER.index(role)
        except ValueError:
            held = -1
        if held < _ROLE_ORDER.index(minimum):
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "FORBIDDEN",
                    "message": (
                        f"This needs the {minimum} role or higher. "
                        f"You are {role}."
                    ),
                },
            )
        return context

    return check
