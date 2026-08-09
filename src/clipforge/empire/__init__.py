"""ClipForge AI — Empire Mode.

Fifty-plus channels, multiple brands, multiple users, one scoped dashboard.

    from clipforge.empire import Empire, Plan, Role

    empire = Empire(factory, analytics)
    tenant = empire.add_tenant("Northwind Media", Plan.EMPIRE)
    brand = empire.add_brand(tenant.tenant_id, "Redline")
    print(empire.dashboard(user.user_id).render())

Three findings fall out of running the existing system at this scale, and all
three are arithmetic rather than opinion:

**Scheduling was O(n²).** Each `schedule()` scanned every post to check
spacing, so a bulk import cost 8ms at 300 posts and 469ms at 3,000 — and an
empire holding 45,000 posts ninety days out would have spent roughly two
minutes filling a quarter. A per-account index with a bisect lookup made it
flat: the same 45,000 posts now schedule in under a second.

**500 uploads a day is reachable, but not evenly.** Fifty channels give 300/day
on TikTok and 1,250 on Instagram, because those caps are per account. YouTube's
is per *API project* — six a day for the whole app, however many channels
connect. An empire at this volume is a TikTok operation with a YouTube trickle
unless it has a quota increase or per-tenant projects.

**Ad revenue does not pay for it.** At 191 cents a clip and the blended RPM of
an actual short-form mix, a clip needs millions of views to repay its own
production. `economics.py` computes the number rather than asserting it, and
the conclusion is not that the product fails — it is that the revenue line has
to be sponsorship, affiliate, lead generation or the subscription itself, and a
dashboard reporting "total revenue" from ads alone is selling a fantasy.
"""

from .capacity import (
    CIRCUMVENTION_THRESHOLD,
    CapacityReport,
    PlatformCapacity,
    PoolOwnership,
    QuotaPool,
    assess,
    circumvention_risk,
    plan_pools,
    projects_needed,
)
from .dashboard import Alert, Dashboard, Severity
from .economics import (
    DEFAULT_RPM_CENTS,
    RATES_VERSION,
    Economics,
    RevenueStreams,
    UnitEconomics,
    ad_revenue_cents,
    blended_rpm_cents,
    break_even_views,
    month,
    required_non_ad_revenue_cents,
    unit_economics,
)
from .empire import CREDENTIAL_WARNING_DAYS, Empire, EmpireConfig
from .rollup import (
    ChannelLine,
    Concentration,
    Growth,
    Totals,
    concentration,
    growth,
    leaderboard,
    totals,
)
from .tenancy import (
    GRANTS,
    PLANS,
    AccessDenied,
    Brand,
    Directory,
    Permission,
    Plan,
    PlanLimitExceeded,
    PlanLimits,
    Role,
    Tenant,
    User,
)

__all__ = [
    "AccessDenied",
    "Alert",
    "Brand",
    "CIRCUMVENTION_THRESHOLD",
    "CREDENTIAL_WARNING_DAYS",
    "CapacityReport",
    "ChannelLine",
    "Concentration",
    "DEFAULT_RPM_CENTS",
    "Dashboard",
    "Directory",
    "Economics",
    "Empire",
    "EmpireConfig",
    "GRANTS",
    "Growth",
    "PLANS",
    "Permission",
    "Plan",
    "PlanLimitExceeded",
    "PlanLimits",
    "PlatformCapacity",
    "PoolOwnership",
    "QuotaPool",
    "RATES_VERSION",
    "RevenueStreams",
    "Role",
    "Severity",
    "Tenant",
    "Totals",
    "UnitEconomics",
    "User",
    "ad_revenue_cents",
    "assess",
    "blended_rpm_cents",
    "break_even_views",
    "circumvention_risk",
    "concentration",
    "growth",
    "leaderboard",
    "month",
    "plan_pools",
    "projects_needed",
    "required_non_ad_revenue_cents",
    "totals",
    "unit_economics",
]
