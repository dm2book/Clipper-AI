"""Tenancy isolation, capacity arithmetic, economics, rollups, and scale.

Two of these matter more than the rest. `TestIsolation` checks that one
tenant cannot see another's data through any path, which is the failure that
ends a SaaS. `TestScale` pins the scheduling fix, because a quadratic insert
degrades silently — it is fast in every test and slow only in production.
"""

from __future__ import annotations

import json
import time
import unittest
from datetime import datetime, timedelta, timezone

import _support  # noqa: F401  (path setup)

from clipforge.analytics import (
    AnalyticsEngine,
    PostMetrics,
    PostRecord,
    Snapshot,
)
from clipforge.empire import (
    AccessDenied,
    Brand,
    CIRCUMVENTION_THRESHOLD,
    Directory,
    Empire,
    EmpireConfig,
    GRANTS,
    PLANS,
    Permission,
    Plan,
    PlanLimitExceeded,
    PoolOwnership,
    QuotaPool,
    RevenueStreams,
    Role,
    Severity,
    Tenant,
    User,
    assess,
    blended_rpm_cents,
    break_even_views,
    circumvention_risk,
    concentration,
    growth,
    leaderboard,
    month,
    plan_pools,
    projects_needed,
    required_non_ad_revenue_cents,
    totals,
)
from clipforge.factory import ChannelFactory, ChannelState, Niche, profile
from clipforge.factory.pipeline import ITEM_COST_CENTS
from clipforge.publish import (
    Account,
    MediaAsset,
    Platform,
    PostSpec,
    PublishConfig,
    PublishingSystem,
    TokenSet,
)

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def tokens(account_id: str, platform: Platform, days: int = 3650) -> TokenSet:
    return TokenSet(account_id, platform, "at", "rt",
                    expires_at=NOW + timedelta(hours=1),
                    refresh_valid_until=NOW + timedelta(days=days),
                    obtained_at=NOW)


def post_record(post_id: str, channel_id: str, views: int = 1000,
                days_ago: float = 2.0,
                platform: Platform = Platform.TIKTOK,
                channel_name: str = "") -> PostRecord:
    published = NOW - timedelta(days=days_ago)
    metrics = PostMetrics(post_id, platform, published)
    metrics.record(Snapshot(
        published + timedelta(hours=24), 24.0, views=views,
        likes=int(views * 0.05), comments=int(views * 0.01),
        shares=int(views * 0.008), follows=int(views * 0.004),
        impressions=views * 4, avg_watch_pct=0.45,
    ))
    return PostRecord(
        post_id=post_id, metrics=metrics, channel_id=channel_id,
        channel_name=channel_name or channel_id, niche="business",
        timezone="UTC", hook_type="curiosity", topic="raise",
        creator="Podcast Co", clip_duration_s=28.0,
    )


def build_empire(channels_per_brand: int = 2, brands: int = 2,
                 plan: Plan = Plan.EMPIRE):
    publisher = PublishingSystem(PublishConfig(enforce_spacing=False))
    factory = ChannelFactory(publisher=publisher)
    empire = Empire(factory, AnalyticsEngine())

    tenant = empire.add_tenant("Northwind", plan)
    owner = empire.add_user(tenant.tenant_id, "owner@n.test", Role.OWNER)

    made = []
    for b in range(brands):
        brand = empire.add_brand(tenant.tenant_id, f"Brand{b}")
        for c in range(channels_per_brand):
            accounts = {}
            for platform in profile(Niche.BUSINESS).platforms:
                account_id = f"{platform.value}-{b}-{c}"
                accounts[platform] = account_id
                publisher.connect(
                    Account(account_id, platform, tenant.tenant_id,
                            external_id="e", direct_post_approved=True,
                            business_account=True),
                    tokens(account_id, platform),
                )
            channel = empire.add_channel(
                owner.user_id, brand.brand_id, f"B{b}C{c}", Niche.BUSINESS,
                accounts=accounts,
            )
            factory.activate(channel.channel_id)
            made.append((brand, channel))
    return empire, tenant, owner, made


class TestRolesAndPlans(unittest.TestCase):
    def test_owner_holds_every_permission(self):
        self.assertEqual(GRANTS[Role.OWNER], frozenset(Permission))

    def test_permissions_narrow_down_the_roles(self):
        order = [Role.OWNER, Role.ADMIN, Role.OPERATOR, Role.EDITOR,
                 Role.VIEWER]
        sizes = [len(GRANTS[r]) for r in order]
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_analyst_reads_but_cannot_act(self):
        grants = GRANTS[Role.ANALYST]
        self.assertIn(Permission.VIEW_ANALYTICS, grants)
        self.assertNotIn(Permission.MANAGE_CHANNELS, grants)
        self.assertNotIn(Permission.SCHEDULE_POSTS, grants)

    def test_editor_schedules_but_cannot_see_revenue(self):
        grants = GRANTS[Role.EDITOR]
        self.assertIn(Permission.SCHEDULE_POSTS, grants)
        self.assertNotIn(Permission.VIEW_REVENUE, grants)

    def test_operator_cannot_connect_accounts(self):
        # Blast radius, not seniority: connecting an account is a credential
        # operation and belongs above channel management.
        self.assertNotIn(Permission.CONNECT_ACCOUNTS, GRANTS[Role.OPERATOR])

    def test_plans_are_monotonic(self):
        order = [Plan.STARTER, Plan.STUDIO, Plan.AGENCY, Plan.EMPIRE]
        for field in ("max_brands", "max_channels", "max_users",
                      "max_uploads_per_day", "monthly_price_cents"):
            values = [getattr(PLANS[p], field) for p in order]
            self.assertEqual(values, sorted(values), field)

    def test_empire_plan_covers_the_stated_target(self):
        self.assertGreaterEqual(PLANS[Plan.EMPIRE].max_channels, 50)
        self.assertGreaterEqual(PLANS[Plan.EMPIRE].max_uploads_per_day, 500)


class TestIsolation(unittest.TestCase):
    def setUp(self):
        self.directory = Directory()
        self.a = self.directory.add_tenant(Tenant("t_a", "Alpha", Plan.AGENCY))
        self.b = self.directory.add_tenant(Tenant("t_b", "Beta", Plan.AGENCY))
        self.brand_a = self.directory.add_brand(Brand("b_a", "t_a", "A Brand"))
        self.brand_b = self.directory.add_brand(Brand("b_b", "t_b", "B Brand"))
        self.directory.attach_channel("b_a", "ch_a")
        self.directory.attach_channel("b_b", "ch_b")
        self.user_a = self.directory.add_user(
            User("u_a", "t_a", "a@a.test", Role.OWNER))

    def test_a_tenant_cannot_fetch_another_tenants_brand(self):
        with self.assertRaises(KeyError):
            self.directory.brand("b_b", tenant_id="t_a")

    def test_channel_listing_is_scoped(self):
        self.assertEqual(self.directory.channels("t_a"), ("ch_a",))
        self.assertEqual(self.directory.channels("t_b"), ("ch_b",))

    def test_visible_channels_never_cross_tenants(self):
        self.assertEqual(self.directory.visible_channels("u_a"), ("ch_a",))
        self.assertFalse(self.directory.scope_check("u_a", "ch_b"))

    def test_users_are_scoped(self):
        self.assertEqual([u.user_id for u in self.directory.users("t_a")],
                         ["u_a"])
        self.assertEqual(self.directory.users("t_b"), ())

    def test_brand_restriction_narrows_within_a_tenant(self):
        self.directory.add_brand(Brand("b_a2", "t_a", "Second"))
        self.directory.attach_channel("b_a2", "ch_a2")
        limited = self.directory.add_user(User(
            "u_lim", "t_a", "lim@a.test", Role.VIEWER,
            brand_ids=frozenset({"b_a"}),
        ))
        self.assertEqual(self.directory.visible_channels(limited.user_id),
                         ("ch_a",))

    def test_require_raises_rather_than_returning_false(self):
        analyst = self.directory.add_user(
            User("u_an", "t_a", "an@a.test", Role.ANALYST))
        with self.assertRaises(AccessDenied):
            self.directory.require(analyst.user_id,
                                   Permission.MANAGE_CHANNELS)

    def test_a_deactivated_user_is_denied(self):
        dead = self.directory.add_user(
            User("u_x", "t_a", "x@a.test", Role.OWNER, active=False))
        with self.assertRaises(AccessDenied):
            self.directory.require(dead.user_id, Permission.VIEW_ANALYTICS)

    def test_a_suspended_tenant_denies_everyone(self):
        self.a.suspended = True
        with self.assertRaises(AccessDenied):
            self.directory.require("u_a", Permission.VIEW_ANALYTICS)

    def test_plan_limits_are_enforced_on_brands(self):
        directory = Directory()
        directory.add_tenant(Tenant("t", "Small", Plan.STARTER))
        directory.add_brand(Brand("b1", "t", "One"))
        with self.assertRaises(PlanLimitExceeded):
            directory.add_brand(Brand("b2", "t", "Two"))

    def test_plan_limits_are_enforced_on_channels(self):
        directory = Directory()
        directory.add_tenant(Tenant("t", "Small", Plan.STARTER))
        directory.add_brand(Brand("b1", "t", "One"))
        for i in range(PLANS[Plan.STARTER].max_channels):
            directory.attach_channel("b1", f"c{i}")
        with self.assertRaises(PlanLimitExceeded):
            directory.attach_channel("b1", "one-too-many")

    def test_plan_limits_are_enforced_on_users(self):
        directory = Directory()
        directory.add_tenant(Tenant("t", "Small", Plan.STARTER))
        for i in range(PLANS[Plan.STARTER].max_users):
            directory.add_user(User(f"u{i}", "t", f"{i}@t.test", Role.VIEWER))
        with self.assertRaises(PlanLimitExceeded):
            directory.add_user(User("extra", "t", "x@t.test", Role.VIEWER))


class TestCapacity(unittest.TestCase):
    def test_account_scoped_platforms_scale_with_accounts(self):
        small = assess({Platform.TIKTOK: 10}, [], 100)
        large = assess({Platform.TIKTOK: 50}, [], 100)
        self.assertEqual(large.ceiling_per_day, 5 * small.ceiling_per_day)

    def test_youtube_does_not_scale_with_channels(self):
        # The arithmetic that defines Empire Mode: a project is the app.
        pool = QuotaPool("yt", Platform.YOUTUBE, PoolOwnership.SHARED_APP)
        ten = assess({Platform.YOUTUBE: 10}, [pool], 100)
        fifty = assess({Platform.YOUTUBE: 50}, [pool], 100)
        self.assertEqual(ten.ceiling_per_day, fifty.ceiling_per_day)
        self.assertEqual(fifty.ceiling_per_day, 6)

    def test_more_projects_do_raise_youtube_capacity(self):
        pools = [
            QuotaPool(f"yt{i}", Platform.YOUTUBE, PoolOwnership.PER_TENANT,
                      tenant_id=f"t{i}")
            for i in range(4)
        ]
        report = assess({Platform.YOUTUBE: 50}, pools, 100)
        self.assertEqual(report.ceiling_per_day, 24)

    def test_no_pool_means_no_youtube_capacity(self):
        self.assertEqual(assess({Platform.YOUTUBE: 50}, [], 100).ceiling_per_day, 0)

    def test_500_a_day_is_reachable_but_lopsided(self):
        report = assess(
            {Platform.TIKTOK: 50, Platform.INSTAGRAM: 50, Platform.YOUTUBE: 50},
            [QuotaPool("yt", Platform.YOUTUBE, PoolOwnership.SHARED_APP)],
            500,
        )
        self.assertTrue(report.feasible)
        mix = report.mix()
        self.assertEqual(mix["youtube"], 6)
        self.assertGreater(mix["tiktok"], 100)
        self.assertTrue(any("YouTube is" in w for w in report.warnings))

    def test_an_unreachable_target_reports_the_shortfall(self):
        report = assess({Platform.TIKTOK: 5}, [], 500)
        self.assertFalse(report.feasible)
        self.assertEqual(report.shortfall, 500 - 30)

    def test_projects_needed_arithmetic(self):
        self.assertEqual(projects_needed(Platform.YOUTUBE, 6), 1)
        self.assertEqual(projects_needed(Platform.YOUTUBE, 7), 2)
        self.assertEqual(projects_needed(Platform.YOUTUBE, 167), 28)

    def test_account_scoped_platforms_need_no_projects(self):
        self.assertEqual(projects_needed(Platform.TIKTOK, 500), 0)

    def test_many_shared_projects_are_flagged_as_circumvention(self):
        pools = [
            QuotaPool(f"yt{i}", Platform.YOUTUBE, PoolOwnership.SHARED_APP)
            for i in range(CIRCUMVENTION_THRESHOLD + 3)
        ]
        risks = circumvention_risk(pools)
        self.assertTrue(risks)
        self.assertIn("circumvention", risks[0])

    def test_per_tenant_projects_are_not_flagged(self):
        pools = plan_pools(Platform.YOUTUBE, [f"t{i}" for i in range(30)], 167)
        self.assertEqual(circumvention_risk(pools), [])
        self.assertEqual(len(pools), 30)


class TestEconomics(unittest.TestCase):
    def test_sub_minute_clips_earn_nothing_on_tiktok_or_instagram(self):
        # Creator Rewards requires >60s; Instagram has no general share.
        revenue, breakdown = __import__(
            "clipforge.empire.economics", fromlist=["ad_revenue_cents"]
        ).ad_revenue_cents({
            Platform.TIKTOK: 1_000_000, Platform.INSTAGRAM: 1_000_000,
        })
        self.assertEqual(revenue, 0)
        self.assertEqual(breakdown["tiktok"], 0)

    def test_youtube_pays_something(self):
        result = month(100, {Platform.YOUTUBE: 1_000_000})
        self.assertGreater(result.ad_revenue_cents, 0)

    def test_blended_rpm_is_dragged_down_by_the_mix(self):
        youtube_only = blended_rpm_cents({Platform.YOUTUBE: 1_000_000})
        realistic = blended_rpm_cents({
            Platform.YOUTUBE: 100_000, Platform.TIKTOK: 900_000,
        })
        self.assertLess(realistic, youtube_only / 5)

    def test_break_even_is_tens_of_thousands_of_views(self):
        views = break_even_views()
        self.assertGreater(views, 10_000)

    def test_break_even_is_infinite_at_zero_rpm(self):
        self.assertEqual(break_even_views(rpm_cents=0.0), float("inf"))

    def test_a_realistic_month_loses_money_on_ads_alone(self):
        # The finding, computed rather than asserted.
        uploads = 500 * 30
        views = {
            Platform.TIKTOK: 300 * 30 * 3000,
            Platform.INSTAGRAM: 194 * 30 * 3000,
            Platform.YOUTUBE: 6 * 30 * 3000,
        }
        result = month(uploads, views)
        self.assertFalse(result.profitable)
        self.assertLess(result.ad_revenue_cents,
                        result.production_cost_cents / 100)
        self.assertTrue(any("unmonetised by ads" in n for n in result.notes))

    def test_non_ad_revenue_can_make_it_work(self):
        uploads = 500 * 30
        views = {Platform.TIKTOK: 300 * 30 * 3000}
        needed = required_non_ad_revenue_cents(uploads, views)
        result = month(uploads, views,
                       RevenueStreams(sponsorship_cents=needed + 100_000))
        self.assertTrue(result.profitable)

    def test_production_cost_comes_from_the_factory(self):
        result = month(100, {Platform.TIKTOK: 1000})
        self.assertEqual(result.production_cost_cents, 100 * ITEM_COST_CENTS)

    def test_ad_share_is_reported(self):
        result = month(10, {Platform.YOUTUBE: 100_000},
                       RevenueStreams(sponsorship_cents=1_000_000))
        self.assertLess(result.ad_share, 0.1)
        self.assertTrue(any("Ads are" in n for n in result.notes))

    def test_serialises(self):
        payload = json.loads(json.dumps(
            month(10, {Platform.YOUTUBE: 1000}).to_dict()
        ))
        self.assertIn("net_cents", payload)

    def test_a_zero_rpm_portfolio_still_serialises(self):
        # An all-TikTok portfolio has an infinite break-even, and infinity
        # neither rounds to an integer nor exists in JSON.
        payload = json.loads(json.dumps(
            month(10, {Platform.TIKTOK: 1_000_000}).to_dict()
        ))
        self.assertIsNone(payload["unit"]["break_even_views"])
        self.assertTrue(payload["unit"]["break_even_unreachable"])


class TestRollup(unittest.TestCase):
    def test_totals_sum_the_six_metrics(self):
        records = [post_record(f"p{i}", "ch1", views=1000) for i in range(5)]
        result = totals(records, brands=1)
        self.assertEqual(result.uploads, 5)
        self.assertEqual(result.views, 5000)
        self.assertEqual(result.channels, 1)
        self.assertEqual(result.subscribers, 5 * 4)

    def test_concentration_detects_a_one_channel_portfolio(self):
        spread = concentration([1000.0] + [1.0] * 49)
        self.assertGreater(spread.top_1_share, 0.9)
        self.assertIn("one channel", spread.verdict)

    def test_concentration_accepts_an_even_spread(self):
        spread = concentration([100.0] * 20)
        self.assertLess(spread.top_1_share, 0.1)
        self.assertIn("broadly", spread.verdict)

    def test_concentration_counts_dormant_channels(self):
        self.assertEqual(concentration([1000.0] + [0.0] * 9).dormant, 9)

    def test_growth_separates_expansion_from_improvement(self):
        # Ten channels last week, twenty this week, same per-channel output.
        previous = [post_record(f"a{i}", f"ch{i}", views=1000, days_ago=10)
                    for i in range(10)]
        current = [post_record(f"b{i}", f"ch{i}", views=1000, days_ago=2)
                   for i in range(20)]
        result = growth("views", current, previous)

        self.assertAlmostEqual(result.raw_change, 1.0, places=2)
        self.assertAlmostEqual(result.same_channel_change, 0.0, places=2)
        self.assertEqual(result.channels_added, 10)
        self.assertGreater(result.from_expansion, 0.5)

    def test_growth_on_a_flat_portfolio_is_not_significant(self):
        previous = [post_record(f"a{i}", f"ch{i%3}", views=1000, days_ago=10)
                    for i in range(12)]
        current = [post_record(f"b{i}", f"ch{i%3}", views=1000, days_ago=2)
                   for i in range(12)]
        self.assertFalse(growth("views", current, previous).significant)

    def test_leaderboard_ranks_and_computes_per_upload(self):
        # Busy posts five times as often and wins on total views; Good is
        # three times better per upload. A total-views ranking flatters the
        # channel that simply posts more, which is why the per-upload column
        # exists.
        records = (
            [post_record(f"a{i}", "ch1", views=300) for i in range(10)]
            + [post_record(f"b{i}", "ch2", views=900) for i in range(2)]
        )
        board = leaderboard(records, names={"ch1": "Busy", "ch2": "Good"})
        self.assertEqual(board[0].channel_name, "Busy")
        self.assertEqual(board[0].views, 3000)
        self.assertEqual(board[1].views, 1800)
        self.assertGreater(board[1].views_per_upload, board[0].views_per_upload)


class TestEmpireDashboard(unittest.TestCase):
    def setUp(self):
        self.empire, self.tenant, self.owner, self.made = build_empire()
        for index, (_, channel) in enumerate(self.made):
            for post in range(4):
                self.empire.analytics.track(post_record(
                    f"p{index}-{post}", channel.channel_id,
                    views=1000 * (index + 1), days_ago=2.0,
                    channel_name=channel.name,
                ))

    def test_dashboard_builds(self):
        board = self.empire.dashboard(self.owner.user_id, NOW)
        self.assertEqual(board.totals.channels, 4)
        self.assertEqual(board.totals.brands, 2)
        self.assertTrue(board.leaderboard)

    def test_dashboard_renders(self):
        text = self.empire.dashboard(self.owner.user_id, NOW).render()
        self.assertIn("EMPIRE", text)
        self.assertIn("TOTALS", text)

    def test_a_viewer_sees_only_their_brand(self):
        brand = self.made[0][0]
        client = self.empire.add_user(
            self.tenant.tenant_id, "client@x.test", Role.VIEWER,
            brand_ids=[brand.brand_id],
        )
        board = self.empire.dashboard(client.user_id, NOW)
        self.assertEqual(board.totals.brands, 1)
        self.assertEqual(board.totals.channels, 2)
        self.assertIn("Scoped to", board.scope_note)

    def test_a_viewer_cannot_see_revenue(self):
        client = self.empire.add_user(
            self.tenant.tenant_id, "c@x.test", Role.VIEWER)
        self.assertIsNone(self.empire.dashboard(client.user_id, NOW).economics)

    def test_an_owner_can(self):
        self.assertIsNotNone(
            self.empire.dashboard(self.owner.user_id, NOW).economics
        )

    def test_a_stopped_channel_becomes_a_critical_alert(self):
        channel = self.made[0][1]
        for _ in range(6):
            channel.health.record_failure("upload rejected", NOW)
        channel.state = ChannelState.CIRCUIT_OPEN

        alerts = self.empire.alerts(self.owner.user_id, NOW)
        self.assertTrue(any(
            a.severity is Severity.CRITICAL and "has stopped" in a.title
            for a in alerts
        ))

    def test_an_expiring_credential_warns_before_it_dies(self):
        channel = self.made[0][1]
        platform, account_id = next(iter(channel.accounts.items()))
        self.empire.factory.publisher.tokens.put(
            tokens(account_id, platform, days=5)
        )
        alerts = self.empire.alerts(self.owner.user_id, NOW)
        self.assertTrue(any(
            a.severity is Severity.WARNING and "expire" in a.title
            for a in alerts
        ))

    def test_a_stale_budget_period_does_not_alert(self):
        # Exhausted in a previous month is not exhausted now.
        channel = self.made[0][1]
        channel.budget.period = "2026-01"
        channel.budget.charge(channel.budget.monthly_cents)
        channel.state = ChannelState.BUDGET_EXHAUSTED

        alerts = self.empire.alerts(self.owner.user_id, NOW)
        self.assertFalse(any("out of budget" in a.title for a in alerts))
        self.assertIs(channel.state, ChannelState.ACTIVE)

    def test_a_current_budget_exhaustion_does_alert(self):
        channel = self.made[0][1]
        channel.budget.period = NOW.strftime("%Y-%m")
        channel.budget.charge(channel.budget.monthly_cents)
        channel.state = ChannelState.BUDGET_EXHAUSTED

        alerts = self.empire.alerts(self.owner.user_id, NOW)
        self.assertTrue(any("out of budget" in a.title for a in alerts))

    def test_alerts_are_scoped_to_the_user(self):
        other_brand = self.made[-1][0]
        other_channel = self.made[-1][1]
        for _ in range(6):
            other_channel.health.record_failure("boom", NOW)
        other_channel.state = ChannelState.CIRCUIT_OPEN

        client = self.empire.add_user(
            self.tenant.tenant_id, "c2@x.test", Role.VIEWER,
            brand_ids=[self.made[0][0].brand_id],
        )
        alerts = self.empire.alerts(client.user_id, NOW)
        self.assertFalse(any(other_channel.name in a.title for a in alerts))

    def test_analytics_reads_require_permission(self):
        # There is no role without VIEW_ANALYTICS, so deactivate instead.
        blocked = self.empire.add_user(
            self.tenant.tenant_id, "off@x.test", Role.VIEWER)
        self.empire.directory._users[blocked.user_id] = User(
            blocked.user_id, blocked.tenant_id, blocked.email,
            blocked.role, active=False,
        )
        with self.assertRaises(AccessDenied):
            self.empire.dashboard(blocked.user_id, NOW)

    def test_status_serialises(self):
        payload = json.loads(json.dumps(self.empire.status(NOW), default=str))
        self.assertIn("capacity", payload)
        self.assertEqual(payload["channels"], 4)

    def test_dashboard_serialises(self):
        payload = json.loads(json.dumps(
            self.empire.dashboard(self.owner.user_id, NOW).to_dict(),
            default=str,
        ))
        self.assertIn("totals", payload)
        self.assertIn("alerts", payload)


class TestScale(unittest.TestCase):
    """Pins the scheduling fix.

    A quadratic insert is fast in every unit test and slow only in production,
    which is exactly why it needs a test that would notice.
    """

    def schedule_many(self, count: int, accounts: int = 50) -> float:
        publisher = PublishingSystem()
        for i in range(accounts):
            account_id = f"a{i}"
            publisher.connect(
                Account(account_id, Platform.TIKTOK, "t", external_id="e",
                        direct_post_approved=True),
                tokens(account_id, Platform.TIKTOK, days=365),
            )
        spec = PostSpec(
            asset=MediaAsset("a", path="/x.mp4", size_bytes=1024,
                             duration_s=20.0),
            title="t",
        )
        start = time.perf_counter()
        for i in range(count):
            publisher.schedule(
                f"a{i % accounts}", spec,
                NOW + timedelta(days=1, minutes=90 * (i // accounts)),
            )
        return time.perf_counter() - start

    def test_scheduling_cost_per_post_stays_flat(self):
        small = self.schedule_many(500) / 500
        large = self.schedule_many(5000) / 5000
        # Quadratic would make the per-post cost grow ~10x here.
        self.assertLess(large, small * 4,
                        f"per-post cost grew from {small * 1000:.3f}ms to "
                        f"{large * 1000:.3f}ms — insert is superlinear again")

    def test_empire_scale_schedules_quickly(self):
        self.assertLess(self.schedule_many(20_000, accounts=150), 8.0)

    def test_the_account_index_stays_ordered(self):
        publisher = PublishingSystem(PublishConfig(enforce_spacing=False))
        publisher.connect(
            Account("a", Platform.TIKTOK, "t", external_id="e",
                    direct_post_approved=True),
            tokens("a", Platform.TIKTOK),
        )
        spec = PostSpec(
            asset=MediaAsset("x", path="/x.mp4", size_bytes=1024,
                             duration_s=20.0), title="t")
        # Insert out of order.
        for offset in (50, 10, 30, 20, 40):
            publisher.schedule("a", spec, NOW + timedelta(days=offset))

        times = [p.run_at for p in publisher.calendar.account_posts("a")]
        self.assertEqual(times, sorted(times))

    def test_rescheduling_keeps_the_index_ordered(self):
        publisher = PublishingSystem(PublishConfig(enforce_spacing=False))
        publisher.connect(
            Account("a", Platform.TIKTOK, "t", external_id="e",
                    direct_post_approved=True),
            tokens("a", Platform.TIKTOK),
        )
        spec = PostSpec(
            asset=MediaAsset("x", path="/x.mp4", size_bytes=1024,
                             duration_s=20.0), title="t")
        posts = [
            publisher.schedule("a", spec, NOW + timedelta(days=d))
            for d in (10, 20, 30)
        ]
        publisher.reschedule(posts[0].post_id, NOW + timedelta(days=40))

        times = [p.run_at for p in publisher.calendar.account_posts("a")]
        self.assertEqual(times, sorted(times))

    def test_spacing_still_catches_a_clash_after_the_index_change(self):
        publisher = PublishingSystem()
        publisher.connect(
            Account("a", Platform.TIKTOK, "t", external_id="e",
                    direct_post_approved=True),
            tokens("a", Platform.TIKTOK),
        )
        spec = PostSpec(
            asset=MediaAsset("x", path="/x.mp4", size_bytes=1024,
                             duration_s=20.0), title="t")
        publisher.schedule("a", spec, NOW + timedelta(days=1))

        from clipforge.publish import ScheduleError

        with self.assertRaises(ScheduleError):
            publisher.schedule("a", spec,
                               NOW + timedelta(days=1, minutes=20))

    def test_removal_drops_the_post_from_the_index(self):
        publisher = PublishingSystem()
        publisher.connect(
            Account("a", Platform.TIKTOK, "t", external_id="e",
                    direct_post_approved=True),
            tokens("a", Platform.TIKTOK),
        )
        spec = PostSpec(
            asset=MediaAsset("x", path="/x.mp4", size_bytes=1024,
                             duration_s=20.0), title="t")
        post = publisher.schedule("a", spec, NOW + timedelta(days=1))
        publisher.calendar.remove(post.post_id)
        self.assertEqual(publisher.calendar.account_posts("a"), ())
        self.assertEqual(len(publisher.calendar), 0)


if __name__ == "__main__":
    unittest.main()
