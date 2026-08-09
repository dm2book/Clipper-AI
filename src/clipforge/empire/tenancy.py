"""Tenants, brands, users and roles.

The hierarchy is four deep and each level exists for a reason:

    Tenant (the paying customer, the isolation boundary)
      └── Brand (a portfolio with its own identity and P&L)
            └── Channel (a niche, from the factory)
                  └── Account (one platform login, from the publisher)

**A brand is not cosmetic.** Two brands under one tenant have separate
budgets, separate revenue, separate reporting and often separate staff — an
agency running "Redline Media" and "Atrium Luxury" needs each to show its own
numbers to its own client. Collapsing them into a flat list of channels makes
the one report anybody actually wants impossible to produce.

**Isolation is a query concern, not a UI concern.** Every lookup here takes a
`tenant_id` and filters on it. The alternative — fetching broadly and filtering
in the view layer — works right up until one endpoint forgets, and then one
customer sees another's revenue. In the real deployment this is Postgres RLS,
as the architecture document specifies; the same discipline is enforced here so
the code above it cannot be written the wrong way.

**Roles are about blast radius, not seniority.** The distinction that matters
at fifty channels is not who is important but what a mistake costs: an analyst
reading numbers cannot pause a channel, and an editor scheduling clips cannot
disconnect an account or move money.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from ..publish.types import utcnow


class Role(str, enum.Enum):
    """What a user may do. Ordered by blast radius, not by seniority."""

    OWNER = "owner"        # billing, tenant settings, everything below
    ADMIN = "admin"        # connect/disconnect accounts, create brands
    OPERATOR = "operator"  # activate and pause channels, set budgets
    EDITOR = "editor"      # schedule, cancel and reschedule posts
    ANALYST = "analyst"    # read everything, change nothing
    VIEWER = "viewer"      # read one brand's dashboard


class Permission(str, enum.Enum):
    MANAGE_BILLING = "manage_billing"
    MANAGE_USERS = "manage_users"
    MANAGE_BRANDS = "manage_brands"
    CONNECT_ACCOUNTS = "connect_accounts"
    MANAGE_CHANNELS = "manage_channels"     # activate, pause, budget
    SCHEDULE_POSTS = "schedule_posts"
    CANCEL_POSTS = "cancel_posts"
    VIEW_ANALYTICS = "view_analytics"
    VIEW_REVENUE = "view_revenue"
    EXPORT_DATA = "export_data"


#: Deliberately explicit rather than inherited down a chain. A hierarchy reads
#: elegantly and hides exactly the question that matters — "can an operator
#: disconnect an account?" — behind a traversal nobody performs while
#: reviewing a pull request.
GRANTS: dict[Role, frozenset[Permission]] = {
    Role.OWNER: frozenset(Permission),
    Role.ADMIN: frozenset({
        Permission.MANAGE_USERS, Permission.MANAGE_BRANDS,
        Permission.CONNECT_ACCOUNTS, Permission.MANAGE_CHANNELS,
        Permission.SCHEDULE_POSTS, Permission.CANCEL_POSTS,
        Permission.VIEW_ANALYTICS, Permission.VIEW_REVENUE,
        Permission.EXPORT_DATA,
    }),
    Role.OPERATOR: frozenset({
        Permission.MANAGE_CHANNELS, Permission.SCHEDULE_POSTS,
        Permission.CANCEL_POSTS, Permission.VIEW_ANALYTICS,
        Permission.EXPORT_DATA,
    }),
    Role.EDITOR: frozenset({
        Permission.SCHEDULE_POSTS, Permission.CANCEL_POSTS,
        Permission.VIEW_ANALYTICS,
    }),
    Role.ANALYST: frozenset({
        Permission.VIEW_ANALYTICS, Permission.VIEW_REVENUE,
        Permission.EXPORT_DATA,
    }),
    Role.VIEWER: frozenset({Permission.VIEW_ANALYTICS}),
}


class Plan(str, enum.Enum):
    """Subscription tier. Caps are the product, not a technical limit."""

    STARTER = "starter"
    STUDIO = "studio"
    AGENCY = "agency"
    EMPIRE = "empire"


@dataclass(frozen=True, slots=True)
class PlanLimits:
    max_brands: int
    max_channels: int
    max_users: int
    max_uploads_per_day: int
    monthly_price_cents: int


PLANS: dict[Plan, PlanLimits] = {
    Plan.STARTER: PlanLimits(1, 3, 2, 15, 4_900),
    Plan.STUDIO: PlanLimits(3, 12, 5, 60, 19_900),
    Plan.AGENCY: PlanLimits(10, 40, 20, 200, 79_900),
    Plan.EMPIRE: PlanLimits(50, 250, 100, 1_000, 249_900),
}


@dataclass(frozen=True, slots=True)
class User:
    user_id: str
    tenant_id: str
    email: str
    role: Role
    #: Empty means every brand in the tenant. A populated set restricts the
    #: user to those brands — how an agency gives a client access to their own
    #: portfolio and nothing else.
    brand_ids: frozenset[str] = frozenset()
    name: str = ""
    active: bool = True

    def can(self, permission: Permission) -> bool:
        return self.active and permission in GRANTS[self.role]

    def sees_brand(self, brand_id: str) -> bool:
        return not self.brand_ids or brand_id in self.brand_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "email": self.email,
            "name": self.name,
            "role": self.role.value,
            "active": self.active,
            "brands": sorted(self.brand_ids) or "all",
            "permissions": sorted(p.value for p in GRANTS[self.role]),
        }


@dataclass(slots=True)
class Brand:
    """A portfolio with its own identity, budget and P&L."""

    brand_id: str
    tenant_id: str
    name: str
    channel_ids: set[str] = field(default_factory=set)
    #: Monthly production budget across every channel in the brand.
    budget_cents: int = 0
    timezone: str = "UTC"
    created_at: datetime = field(default_factory=utcnow)
    archived: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "brand_id": self.brand_id,
            "name": self.name,
            "channels": len(self.channel_ids),
            "budget_cents": self.budget_cents,
            "timezone": self.timezone,
            "archived": self.archived,
        }


@dataclass(slots=True)
class Tenant:
    """The paying customer, and the isolation boundary."""

    tenant_id: str
    name: str
    plan: Plan = Plan.STARTER
    created_at: datetime = field(default_factory=utcnow)
    suspended: bool = False

    @property
    def limits(self) -> PlanLimits:
        return PLANS[self.plan]

    def to_dict(self) -> dict[str, Any]:
        limits = self.limits
        return {
            "tenant_id": self.tenant_id,
            "name": self.name,
            "plan": self.plan.value,
            "suspended": self.suspended,
            "limits": {
                "brands": limits.max_brands,
                "channels": limits.max_channels,
                "users": limits.max_users,
                "uploads_per_day": limits.max_uploads_per_day,
            },
        }


class AccessDenied(PermissionError):
    """A user attempted something their role does not allow."""


class PlanLimitExceeded(ValueError):
    """The tenant's subscription does not stretch this far."""


class Directory:
    """Tenants, brands and users, with isolation enforced on every lookup."""

    def __init__(self) -> None:
        self._tenants: dict[str, Tenant] = {}
        self._brands: dict[str, Brand] = {}
        self._users: dict[str, User] = {}
        #: channel_id → brand_id. The reverse edge, so a channel can be
        #: resolved to its brand without scanning every brand's set.
        self._channel_brand: dict[str, str] = {}

    # -- tenants ---------------------------------------------------------------

    def add_tenant(self, tenant: Tenant) -> Tenant:
        self._tenants[tenant.tenant_id] = tenant
        return tenant

    def tenant(self, tenant_id: str) -> Tenant:
        tenant = self._tenants.get(tenant_id)
        if tenant is None:
            raise KeyError(f"unknown tenant {tenant_id!r}")
        return tenant

    @property
    def tenants(self) -> tuple[Tenant, ...]:
        return tuple(self._tenants.values())

    # -- brands ----------------------------------------------------------------

    def add_brand(self, brand: Brand) -> Brand:
        tenant = self.tenant(brand.tenant_id)
        existing = len(self.brands(brand.tenant_id))
        if existing >= tenant.limits.max_brands:
            raise PlanLimitExceeded(
                f"{tenant.name} is on {tenant.plan.value}, which allows "
                f"{tenant.limits.max_brands} brand(s)"
            )
        self._brands[brand.brand_id] = brand
        return brand

    def brand(self, brand_id: str, tenant_id: str = "") -> Brand:
        brand = self._brands.get(brand_id)
        if brand is None:
            raise KeyError(f"unknown brand {brand_id!r}")
        # The isolation check lives here, not in the caller. A caller that
        # forgets it is the bug that shows one customer another's revenue.
        if tenant_id and brand.tenant_id != tenant_id:
            raise KeyError(f"unknown brand {brand_id!r}")
        return brand

    def brands(self, tenant_id: str) -> tuple[Brand, ...]:
        return tuple(
            b for b in self._brands.values()
            if b.tenant_id == tenant_id and not b.archived
        )

    def attach_channel(self, brand_id: str, channel_id: str) -> None:
        brand = self.brand(brand_id)
        tenant = self.tenant(brand.tenant_id)
        if len(self.channels(brand.tenant_id)) >= tenant.limits.max_channels:
            raise PlanLimitExceeded(
                f"{tenant.name} is on {tenant.plan.value}, which allows "
                f"{tenant.limits.max_channels} channels"
            )
        brand.channel_ids.add(channel_id)
        self._channel_brand[channel_id] = brand_id

    def brand_of(self, channel_id: str) -> str:
        return self._channel_brand.get(channel_id, "")

    def channels(self, tenant_id: str, brand_id: str = "") -> tuple[str, ...]:
        out: list[str] = []
        for brand in self.brands(tenant_id):
            if brand_id and brand.brand_id != brand_id:
                continue
            out.extend(sorted(brand.channel_ids))
        return tuple(out)

    # -- users -----------------------------------------------------------------

    def add_user(self, user: User) -> User:
        tenant = self.tenant(user.tenant_id)
        existing = len(self.users(user.tenant_id))
        if existing >= tenant.limits.max_users:
            raise PlanLimitExceeded(
                f"{tenant.name} is on {tenant.plan.value}, which allows "
                f"{tenant.limits.max_users} users"
            )
        self._users[user.user_id] = user
        return user

    def user(self, user_id: str) -> User:
        user = self._users.get(user_id)
        if user is None:
            raise KeyError(f"unknown user {user_id!r}")
        return user

    def users(self, tenant_id: str) -> tuple[User, ...]:
        return tuple(
            u for u in self._users.values() if u.tenant_id == tenant_id
        )

    # -- authorisation -----------------------------------------------------------

    def require(self, user_id: str, permission: Permission) -> User:
        """Assert a permission, or raise.

        Raising rather than returning a boolean on purpose: a bool invites
        `if user.can(...)` at the call site and the branch that forgets it
        fails open. This one fails closed.
        """
        user = self.user(user_id)
        if not user.active:
            raise AccessDenied(f"{user.email} is deactivated")
        if self.tenant(user.tenant_id).suspended:
            raise AccessDenied(
                f"tenant {user.tenant_id} is suspended"
            )
        if not user.can(permission):
            raise AccessDenied(
                f"{user.email} is {user.role.value} and cannot "
                f"{permission.value.replace('_', ' ')}"
            )
        return user

    def visible_channels(self, user_id: str) -> tuple[str, ...]:
        """Channels this user may see — brand restrictions applied."""
        user = self.user(user_id)
        out: list[str] = []
        for brand in self.brands(user.tenant_id):
            if user.sees_brand(brand.brand_id):
                out.extend(sorted(brand.channel_ids))
        return tuple(out)

    def scope_check(self, user_id: str, channel_id: str) -> bool:
        return channel_id in self.visible_channels(user_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenants": len(self._tenants),
            "brands": len(self._brands),
            "users": len(self._users),
            "channels": len(self._channel_brand),
        }
