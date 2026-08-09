"""Empire Mode — orchestration.

Wires the tenancy model over the channel factory, the publisher and the
analytics engine, and produces one scoped dashboard per user.

    tenant → brands → channels (factory) → accounts (publisher)
                          ↓
                  posts → metrics (analytics)
                          ↓
                  rollup → dashboard

The orchestration itself is thin. What this file adds is the two things that
only appear at scale:

**Scoping.** Every read resolves what the calling user may see through the
directory before touching data. Fetching broadly and filtering in the view is
the pattern that eventually shows one customer another's revenue.

**Alerting.** At fifty channels an aggregate cannot show you the two that
stopped, because forty-eight healthy ones drown them. `alerts()` walks the
portfolio for the specific conditions that silently end a channel's output —
a tripped breaker, an exhausted budget, a credential about to expire, a licence
lapsing inside the scheduling horizon — and puts them above the totals.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Sequence

from ..analytics import AnalyticsEngine
from ..analytics.attribution import PostRecord
from ..analytics.metrics import PRIMARY_CHECKPOINT_H
from ..factory import ChannelFactory, ChannelState, Niche
from ..publish.types import Platform, ensure_utc, utcnow
from .capacity import CapacityReport, PoolOwnership, QuotaPool, assess
from .dashboard import Alert, Dashboard, Severity
from .economics import Economics, RevenueStreams, month
from .rollup import Growth, Totals, growth, leaderboard, totals
from .tenancy import (
    AccessDenied,
    Brand,
    Directory,
    Permission,
    Plan,
    PlanLimitExceeded,
    Role,
    Tenant,
    User,
)

#: Credentials expiring inside this window are worth a warning while there is
#: still time to reconnect.
CREDENTIAL_WARNING_DAYS = 21


@dataclass(slots=True)
class EmpireConfig:
    #: Uploads a day the empire is aiming for, across every channel.
    target_uploads_per_day: int = 500
    period_days: int = 7
    checkpoint_h: float = PRIMARY_CHECKPOINT_H
    leaderboard_rows: int = 20


class Empire:
    """Multi-tenant, multi-brand control over the whole system."""

    def __init__(
        self,
        factory: ChannelFactory,
        analytics: AnalyticsEngine,
        directory: Directory | None = None,
        config: EmpireConfig | None = None,
    ) -> None:
        self.factory = factory
        self.analytics = analytics
        self.directory = directory or Directory()
        self.config = config or EmpireConfig()
        self.pools: list[QuotaPool] = []
        self.revenue: dict[str, RevenueStreams] = {}

    # -- setup -------------------------------------------------------------------

    def add_tenant(self, name: str, plan: Plan = Plan.STARTER) -> Tenant:
        return self.directory.add_tenant(
            Tenant(tenant_id=f"t_{uuid.uuid4().hex[:8]}", name=name, plan=plan)
        )

    def add_brand(
        self, tenant_id: str, name: str, budget_cents: int = 0,
        timezone: str = "UTC",
    ) -> Brand:
        return self.directory.add_brand(Brand(
            brand_id=f"b_{uuid.uuid4().hex[:8]}", tenant_id=tenant_id,
            name=name, budget_cents=budget_cents, timezone=timezone,
        ))

    def add_user(
        self, tenant_id: str, email: str, role: Role,
        brand_ids: Sequence[str] = (), name: str = "",
    ) -> User:
        return self.directory.add_user(User(
            user_id=f"u_{uuid.uuid4().hex[:8]}", tenant_id=tenant_id,
            email=email, role=role, brand_ids=frozenset(brand_ids), name=name,
        ))

    def add_channel(
        self, user_id: str, brand_id: str, name: str, niche: Niche,
        accounts: dict[Platform, str] | None = None, **kwargs: Any,
    ):
        """Create a channel under a brand. Requires permission and headroom."""
        user = self.directory.require(user_id, Permission.MANAGE_BRANDS)
        brand = self.directory.brand(brand_id, user.tenant_id)

        channel = self.factory.create_channel(
            name, niche, accounts=accounts,
            timezone=brand.timezone, **kwargs,
        )
        self.directory.attach_channel(brand.brand_id, channel.channel_id)
        return channel

    def add_pool(self, pool: QuotaPool) -> QuotaPool:
        self.pools.append(pool)
        return pool

    def record_revenue(self, brand_id: str, streams: RevenueStreams) -> None:
        """Non-ad revenue for a brand.

        Supplied rather than measured: sponsorship and affiliate income arrives
        through channels this system has no visibility into, and inventing a
        number would be worse than showing zero.
        """
        self.revenue[brand_id] = streams

    # -- scoped reads ---------------------------------------------------------------

    def visible_records(
        self, user_id: str, since: datetime, until: datetime,
        brand_id: str = "",
    ) -> tuple[PostRecord, ...]:
        """Analytics records this user may see."""
        self.directory.require(user_id, Permission.VIEW_ANALYTICS)
        allowed = set(self.directory.visible_channels(user_id))
        if brand_id:
            brand = self.directory.brand(
                brand_id, self.directory.user(user_id).tenant_id
            )
            allowed &= brand.channel_ids

        return tuple(
            r for r in self.analytics.store.select(
                since=since, until=until,
                checkpoint_h=self.config.checkpoint_h,
            )
            if r.channel_id in allowed
        )

    # -- alerts -----------------------------------------------------------------------

    def alerts(self, user_id: str, now: datetime | None = None) -> list[Alert]:
        """What is quietly failing.

        Invisible in any aggregate: at fifty channels a dead one moves the
        portfolio total by two percent, which is indistinguishable from a slow
        week.
        """
        now = ensure_utc(now or utcnow())
        user = self.directory.user(user_id)
        visible = set(self.directory.visible_channels(user_id))
        out: list[Alert] = []

        for channel_id in sorted(visible):
            channel = self.factory.channels.get(channel_id)
            if channel is None:
                continue

            # Roll the budget to `now` first. A channel that exhausted its
            # August budget is not blocked in November, and an alert saying
            # otherwise sends someone to fix a channel that is already fine.
            channel.budget.roll(now)
            if (channel.state is ChannelState.BUDGET_EXHAUSTED
                    and not channel.budget.exhausted):
                channel.state = ChannelState.ACTIVE

            if channel.state is ChannelState.CIRCUIT_OPEN:
                out.append(Alert(
                    Severity.CRITICAL, channel.name,
                    f"{channel.name} has stopped",
                    f"{channel.health.consecutive_failures} consecutive "
                    f"failures: {channel.health.last_error[:110]}",
                    "Fix the underlying failure, then reactivate the channel.",
                ))
            elif channel.state is ChannelState.BUDGET_EXHAUSTED:
                out.append(Alert(
                    Severity.CRITICAL, channel.name,
                    f"{channel.name} is out of budget",
                    f"${channel.budget.monthly_cents / 100:.0f} spent for "
                    f"{channel.budget.period}; it will produce nothing until "
                    f"the month rolls.",
                    "Raise the channel budget or accept the pause.",
                ))
            elif channel.state is ChannelState.PAUSED:
                out.append(Alert(
                    Severity.INFO, channel.name,
                    f"{channel.name} is paused", "Paused by an operator.",
                ))

            if not channel.accounts and channel.state is ChannelState.ACTIVE:
                out.append(Alert(
                    Severity.CRITICAL, channel.name,
                    f"{channel.name} has no connected accounts",
                    "Active, but there is nowhere for its clips to go.",
                    "Connect at least one platform account.",
                ))

            for platform, account_id in sorted(
                channel.accounts.items(), key=lambda kv: kv[0].value
            ):
                tokens = self.factory.publisher.tokens.get(account_id)
                if tokens is None:
                    out.append(Alert(
                        Severity.CRITICAL, channel.name,
                        f"{channel.name}: no credentials for {platform.value}",
                        "The account is attached but has no stored token.",
                        "Reconnect the account.",
                    ))
                    continue
                if not tokens.can_refresh(now):
                    out.append(Alert(
                        Severity.CRITICAL, channel.name,
                        f"{channel.name}: {platform.value} credentials dead",
                        "The refresh token can no longer be renewed.",
                        "Reconnect the account by hand; retrying cannot fix it.",
                    ))
                elif tokens.refresh_valid_until and (
                    tokens.refresh_valid_until - now
                    <= timedelta(days=CREDENTIAL_WARNING_DAYS)
                ):
                    days = (tokens.refresh_valid_until - now).days
                    out.append(Alert(
                        Severity.WARNING, channel.name,
                        f"{channel.name}: {platform.value} credentials expire "
                        f"in {days}d",
                        f"Anything scheduled past "
                        f"{tokens.refresh_valid_until:%d %b} will fail.",
                        "Reconnect before the deadline.",
                    ))

        # Rights paperwork, which expires quietly and stops a brand rather
        # than a channel.
        rights = self.factory.rights_report(now)
        if rights["unverified"] and user.can(Permission.MANAGE_BRANDS):
            out.append(Alert(
                Severity.WARNING, "empire",
                f"{rights['unverified']} sources have no rights basis",
                "They will not publish, which is correct — but they are "
                "occupying library slots and producing nothing.",
                "Record a licence or remove them.",
            ))
        for entry in rights["expiring_within_90_days"][:3]:
            out.append(Alert(
                Severity.WARNING, "empire",
                f"licence on {entry['source_id']} expires in "
                f"{entry['days']}d",
                f"Anything scheduled past {entry['expires']} would publish "
                f"without one.",
                "Renew the licence or stop scheduling from that source.",
            ))

        return out

    # -- capacity and economics -------------------------------------------------------

    def capacity(self, tenant_id: str = "") -> CapacityReport:
        """Whether the empire can post what it intends to."""
        channels = (
            [self.factory.channels[c] for c in self.directory.channels(tenant_id)
             if c in self.factory.channels]
            if tenant_id else list(self.factory.channels.values())
        )
        accounts: dict[Platform, int] = {}
        for channel in channels:
            for platform in channel.platforms:
                accounts[platform] = accounts.get(platform, 0) + 1

        return assess(accounts, self.pools, self.config.target_uploads_per_day)

    def economics(
        self, records: Sequence[PostRecord], brand_ids: Sequence[str] = (),
    ) -> Economics:
        """Cost the period, with whatever non-ad revenue has been recorded."""
        views: dict[Platform, int] = {}
        for record in records:
            snapshot = (
                record.metrics.at_age(self.config.checkpoint_h)
                or record.metrics.latest
            )
            if snapshot is None:
                continue
            views[record.platform] = views.get(record.platform, 0) + snapshot.views

        combined = RevenueStreams(
            sponsorship_cents=sum(
                self.revenue.get(b, RevenueStreams()).sponsorship_cents
                for b in brand_ids
            ),
            affiliate_cents=sum(
                self.revenue.get(b, RevenueStreams()).affiliate_cents
                for b in brand_ids
            ),
            own_product_cents=sum(
                self.revenue.get(b, RevenueStreams()).own_product_cents
                for b in brand_ids
            ),
            services_cents=sum(
                self.revenue.get(b, RevenueStreams()).services_cents
                for b in brand_ids
            ),
        )
        return month(len(records), views, combined)

    # -- the dashboard -------------------------------------------------------------------

    def dashboard(
        self, user_id: str, now: datetime | None = None, brand_id: str = "",
    ) -> Dashboard:
        """One scoped view of the whole empire."""
        now = ensure_utc(now or utcnow())
        user = self.directory.require(user_id, Permission.VIEW_ANALYTICS)
        tenant = self.directory.tenant(user.tenant_id)

        period = timedelta(days=self.config.period_days)
        current = self.visible_records(user_id, now - period, now, brand_id)
        previous = self.visible_records(
            user_id, now - period * 2, now - period, brand_id
        )

        brands = [
            b for b in self.directory.brands(user.tenant_id)
            if user.sees_brand(b.brand_id)
            and (not brand_id or b.brand_id == brand_id)
        ]
        names = {
            c.channel_id: c.name for c in self.factory.channels.values()
        }
        brand_names = {b.brand_id: b.name for b in brands}
        brand_of = {
            channel_id: self.directory.brand_of(channel_id)
            for channel_id in names
        }

        board = leaderboard(
            current, names, brand_of, brand_names, self.config.checkpoint_h
        )

        scope_note = ""
        if user.brand_ids:
            scope_note = (
                f"Scoped to {len(brands)} brand(s): "
                f"{', '.join(sorted(b.name for b in brands))}"
            )

        economics = None
        if user.can(Permission.VIEW_REVENUE):
            economics = self.economics(
                current, [b.brand_id for b in brands]
            )

        return Dashboard(
            tenant_name=tenant.name,
            user_email=user.email,
            role=user.role.value,
            generated_at=now,
            period_days=self.config.period_days,
            totals=totals(current, len(brands), self.config.checkpoint_h),
            growth=[
                growth(metric, current, previous, self.config.checkpoint_h)
                for metric in ("views", "engagement_rate", "follows")
            ],
            economics=economics,
            leaderboard=board[: self.config.leaderboard_rows],
            alerts=self.alerts(user_id, now),
            capacity=self.capacity(user.tenant_id).to_dict(),
            scope_note=scope_note,
        )

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        now = ensure_utc(now or utcnow())
        capacity = self.capacity()
        return {
            "directory": self.directory.to_dict(),
            "channels": len(self.factory.channels),
            "active_channels": sum(
                1 for c in self.factory.channels.values()
                if c.state is ChannelState.ACTIVE
            ),
            "scheduled_posts": len(self.factory.publisher.calendar),
            "tracked_posts": len(self.analytics.store),
            "capacity": capacity.to_dict(),
            "quota_pools": [p.to_dict() for p in self.pools],
        }
