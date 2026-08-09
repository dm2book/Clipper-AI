"""Whether the empire can physically post what it plans to.

500 uploads a day is reachable, but not the way anyone assumes, and the reason
is worth stating before any of it is built.

Per-account daily caps are 6 on TikTok and 25 on Instagram, so fifty channels
give 300 and 1,250 respectively — adding channels adds capacity, exactly as
expected. YouTube does not work that way. Its 10,000 daily quota units at 1,600
per upload is **6 uploads a day per API project**, and a project is the app, not
the channel. Fifty YouTube channels connected to one app share those six.

So an empire posting 500 a day is posting roughly 300 to TikTok, 194 to
Instagram and 6 to YouTube. Not a balanced portfolio — a TikTok operation with
a YouTube trickle.

### The two ways past it, and only one is legitimate

**A quota increase** on the app, granted by Google after an audit. This is the
supported path and the only one that works for a shared app.

**Per-tenant projects**, where each customer connects their own Google Cloud
project and their own OAuth client. Also legitimate, because the quota being
consumed belongs to the customer whose content it is. This is how serious
multi-tenant publishers are built, and it makes YouTube capacity scale with
customers rather than being a fixed six.

What is **not** legitimate is one operator standing up twenty-seven projects to
multiply their own allowance. That is quota circumvention, it is against
Google's terms, and the projects get terminated together — which at empire
scale means every channel stops on the same afternoon. `QuotaPool.ownership`
exists so the difference is modelled rather than left to whoever wires up the
credentials, and `circumvention_risk()` names it when the shape looks wrong.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..publish.limits import limits_for
from ..publish.types import Platform


class PoolOwnership(str, enum.Enum):
    """Whose API allowance is being spent."""

    #: One app shared by every tenant. Capacity is fixed until Google grants
    #: an increase; adding projects to work around it is circumvention.
    SHARED_APP = "shared_app"
    #: The customer's own project and OAuth client. Their quota, their
    #: content — scales with customers, and is the supported multi-tenant path.
    PER_TENANT = "per_tenant"


@dataclass(frozen=True, slots=True)
class QuotaPool:
    """One source of API allowance for one platform."""

    pool_id: str
    platform: Platform
    ownership: PoolOwnership
    #: Daily unit budget. Defaults to the platform's standard allowance.
    daily_units: int = 0
    #: Tenant this pool belongs to, for `PER_TENANT`.
    tenant_id: str = ""

    @property
    def budget(self) -> int:
        entry = limits_for(self.platform)
        return self.daily_units or entry.rate.daily_budget

    @property
    def uploads_per_day(self) -> int:
        entry = limits_for(self.platform)
        if entry.rate.quota_scope != "project":
            # Account-scoped platforms are not limited by a pool at all; their
            # ceiling comes from how many accounts exist.
            return 0
        cost = entry.rate.upload_cost or 1
        return self.budget // cost

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool_id": self.pool_id,
            "platform": self.platform.value,
            "ownership": self.ownership.value,
            "tenant_id": self.tenant_id,
            "daily_units": self.budget,
            "uploads_per_day": self.uploads_per_day,
        }


@dataclass(frozen=True, slots=True)
class PlatformCapacity:
    platform: Platform
    accounts: int
    per_account_cap: int
    pools: int
    scope: str
    ceiling: int
    #: Where the ceiling comes from, in words.
    binding_constraint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform.value,
            "accounts": self.accounts,
            "per_account_cap": self.per_account_cap,
            "pools": self.pools,
            "scope": self.scope,
            "ceiling_per_day": self.ceiling,
            "binding_constraint": self.binding_constraint,
        }


@dataclass(frozen=True, slots=True)
class CapacityReport:
    """What the empire can actually post in a day."""

    target_per_day: int
    ceiling_per_day: int
    platforms: tuple[PlatformCapacity, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def feasible(self) -> bool:
        return self.ceiling_per_day >= self.target_per_day

    @property
    def shortfall(self) -> int:
        return max(0, self.target_per_day - self.ceiling_per_day)

    def mix(self) -> dict[str, int]:
        """The upload mix the ceilings force, whatever mix was wanted."""
        return {p.platform.value: p.ceiling for p in self.platforms}

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_per_day": self.target_per_day,
            "ceiling_per_day": self.ceiling_per_day,
            "feasible": self.feasible,
            "shortfall": self.shortfall,
            "forced_mix": self.mix(),
            "platforms": [p.to_dict() for p in self.platforms],
            "warnings": list(self.warnings),
        }


def projects_needed(platform: Platform, uploads_per_day: int) -> int:
    """API projects required to sustain a rate on a project-scoped platform."""
    entry = limits_for(platform)
    if entry.rate.quota_scope != "project" or uploads_per_day <= 0:
        return 0
    per_project = entry.rate.posts_per_day or 1
    return math.ceil(uploads_per_day / per_project)


def assess(
    accounts_per_platform: dict[Platform, int],
    pools: Sequence[QuotaPool],
    target_per_day: int,
) -> CapacityReport:
    """What this configuration can post, and where it runs out."""
    platforms: list[PlatformCapacity] = []
    warnings: list[str] = []
    total = 0

    for platform in Platform:
        entry = limits_for(platform)
        accounts = accounts_per_platform.get(platform, 0)
        platform_pools = [p for p in pools if p.platform is platform]

        if entry.rate.quota_scope == "project":
            pool_capacity = sum(p.uploads_per_day for p in platform_pools)
            account_capacity = accounts * entry.rate.posts_per_day
            ceiling = min(pool_capacity, account_capacity) if accounts else 0
            constraint = (
                f"{len(platform_pools)} API project(s) at "
                f"{entry.rate.posts_per_day}/day each — a project is the app, "
                f"not the channel, so connecting more channels does not raise "
                f"this"
            )
            if pool_capacity < account_capacity:
                constraint = f"API quota: {constraint}"
        else:
            ceiling = accounts * entry.rate.posts_per_day
            constraint = (
                f"{accounts} account(s) at {entry.rate.posts_per_day}/day "
                f"each — scales with accounts"
            )

        total += ceiling
        platforms.append(PlatformCapacity(
            platform=platform, accounts=accounts,
            per_account_cap=entry.rate.posts_per_day,
            pools=len(platform_pools), scope=entry.rate.quota_scope,
            ceiling=ceiling, binding_constraint=constraint,
        ))

    if total < target_per_day:
        warnings.append(
            f"target of {target_per_day}/day exceeds the ceiling of "
            f"{total}/day by {target_per_day - total}"
        )

    # The asymmetry that defines the whole plan.
    by_platform = {p.platform: p for p in platforms}
    youtube = by_platform.get(Platform.YOUTUBE)
    if youtube and total:
        share = youtube.ceiling / total
        if share < 0.10 and youtube.accounts:
            balanced = target_per_day // max(1, len(Platform))
            warnings.append(
                f"YouTube is {share * 100:.0f}% of capacity ({youtube.ceiling} "
                f"of {total}/day) across {youtube.accounts} connected "
                f"channels. An even split would need {balanced}/day there, "
                f"which is "
                f"{projects_needed(Platform.YOUTUBE, balanced)} API projects "
                f"or a quota increase. Plan for a TikTok-led portfolio with a "
                f"YouTube trickle, or get the increase before promising "
                f"otherwise."
            )

    warnings.extend(circumvention_risk(pools))
    return CapacityReport(target_per_day, total, tuple(platforms),
                          tuple(warnings))


#: More shared-app projects than this on one platform stops looking like
#: redundancy and starts looking like quota multiplication.
CIRCUMVENTION_THRESHOLD = 2


def circumvention_risk(pools: Sequence[QuotaPool]) -> list[str]:
    """Flag a pool configuration that reads as quota circumvention.

    Worth catching in the capacity planner rather than in a suspension email.
    Projects created to multiply one operator's allowance are terminated
    together, so at empire scale the failure is not one channel degrading —
    it is every channel stopping on the same afternoon.
    """
    out: list[str] = []
    shared: dict[Platform, int] = {}
    for pool in pools:
        if pool.ownership is PoolOwnership.SHARED_APP:
            shared[pool.platform] = shared.get(pool.platform, 0) + 1

    for platform, count in sorted(shared.items(), key=lambda kv: kv[0].value):
        if count > CIRCUMVENTION_THRESHOLD:
            out.append(
                f"{count} shared-app {platform.value} projects. Standing up "
                f"projects to multiply one app's allowance is quota "
                f"circumvention, and they are terminated together — every "
                f"channel stops at once. Request a quota increase, or move "
                f"tenants onto their own projects."
            )
    return out


def plan_pools(
    platform: Platform, tenants: Sequence[str], uploads_per_day: int
) -> list[QuotaPool]:
    """The legitimate way to reach a rate: one project per tenant.

    Capacity then scales with customers, because each customer's quota is
    being spent on that customer's own content.
    """
    entry = limits_for(platform)
    if entry.rate.quota_scope != "project":
        return []
    return [
        QuotaPool(f"{platform.value}-{tenant}", platform,
                  PoolOwnership.PER_TENANT, tenant_id=tenant)
        for tenant in tenants
    ]
