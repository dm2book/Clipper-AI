"""The channel factory and the analytics store, backed by the database.

Same shape as `test_durable_publishing.py`: in-memory when
`CLIPFORGE_TEST_DSN` is unset, Postgres when it is set.

The assertions worth reading are the ones about state that is *cheap to lose
and expensive to lose*: a tripped circuit breaker, a month's recorded spend,
and the set of sources a channel has already clipped. Each of them is silent
when it goes missing, and each has a specific, visible consequence — a deploy
that retries every failing channel at once, a budget that resets on restart, a
channel that republishes material it already posted.
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta

from clipforge.analytics.attribution import PostRecord
from clipforge.analytics.metrics import PostMetrics, RetentionCurve, Snapshot
from clipforge.factory.channel import ChannelState
from clipforge.factory.factory import ChannelFactory
from clipforge.factory.niches import Niche
from clipforge.factory.sources import Rights, RightsBasis, Source, SourceKind
from clipforge.publish.types import Platform
from clipforge.store import (
    ChannelRecord,
    ClipRecord,
    MemoryDatabase,
    ProjectRecord,
    SocialAccountRecord,
    TenantRecord,
    UploadRecord,
)
from clipforge.store.durable import (
    DurableAnalyticsStore,
    DurableChannelBook,
    DurableSourceRegistry,
)

NOW = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)
TENANT = "ten_factory"
PROJECT = "proj_factory"


def _source(source_id: str = "src_1", **kwargs) -> Source:
    defaults = dict(
        source_id=source_id,
        title="A long interview",
        kind=SourceKind.LONGFORM_VIDEO,
        rights=Rights(basis=RightsBasis.LICENSED, reference="LIC-2026-004",
                      verified_at=NOW - timedelta(days=10)),
        creator="Studio Nine",
        duration_s=3600.0,
        topics=("business", "cars"),
        has_transcript=True,
    )
    defaults.update(kwargs)
    return Source(**defaults)


class _Backend:
    dsn = os.environ.get("CLIPFORGE_TEST_DSN", "")
    admin_dsn = os.environ.get("CLIPFORGE_TEST_ADMIN_DSN", "") or dsn

    @classmethod
    def open(cls):
        if not cls.dsn:
            return MemoryDatabase()
        import psycopg

        from clipforge.store.postgres import PostgresDatabase

        with psycopg.connect(cls.admin_dsn, autocommit=True) as connection:
            connection.execute("TRUNCATE TABLE tenants CASCADE")
        return PostgresDatabase(cls.dsn, min_size=1, max_size=4)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _Backend.open()
        self.addCleanup(self.db.close)
        with self.db.unit_of_work(TENANT) as uow:
            uow.tenants.save(TenantRecord(id=TENANT, name="Factory Co"))
            uow.projects.save(ProjectRecord(id=PROJECT, tenant_id=TENANT,
                                            name="Brand"))

    def _channels(self) -> DurableChannelBook:
        return DurableChannelBook(self.db, TENANT, project_id=PROJECT)

    def _sources(self) -> DurableSourceRegistry:
        return DurableSourceRegistry(self.db, TENANT)

    def _factory(self) -> ChannelFactory:
        return ChannelFactory(finder=self._sources(), channels=self._channels())


class DurableSourcesTest(_Base):
    def test_a_registered_source_is_there_in_a_new_registry(self) -> None:
        self._sources().register(_source())
        found = self._sources().get("src_1")
        self.assertIsNotNone(found)
        self.assertEqual(found.title, "A long interview")
        self.assertEqual(found.kind, SourceKind.LONGFORM_VIDEO)
        self.assertEqual(found.topics, ("business", "cars"))
        self.assertTrue(found.has_transcript)

    def test_the_rights_posture_round_trips_intact(self) -> None:
        """The field that decides whether anything may be published at all.
        A licence that came back as `unverified` would silently stop a channel;
        one that came back as `licensed` when it was not is worse."""

        verified = NOW - timedelta(days=10)
        expires = NOW + timedelta(days=355)
        self._sources().register(_source(rights=Rights(
            basis=RightsBasis.CREATIVE_COMMONS,
            reference="CC-BY-4.0",
            attribution="Studio Nine, CC BY 4.0",
            commercial_use=False,
            derivatives=True,
            verified_at=verified,
            expires_at=expires,
        )))
        rights = self._sources().get("src_1").rights
        self.assertEqual(rights.basis, RightsBasis.CREATIVE_COMMONS)
        self.assertEqual(rights.reference, "CC-BY-4.0")
        self.assertEqual(rights.attribution, "Studio Nine, CC BY 4.0")
        self.assertFalse(rights.commercial_use)
        self.assertTrue(rights.derivatives)
        self.assertEqual(rights.verified_at, verified)
        self.assertEqual(rights.expires_at, expires)

    def test_the_same_material_cannot_be_registered_twice(self) -> None:
        """The fingerprint is derived from creator and id, so the same upload
        reappearing under a new URL is recognised rather than clipped again."""

        registry = self._sources()
        registry.register(_source("src_1", creator="Studio Nine"))
        original = registry.get("src_1")
        self.assertEqual(
            registry.by_fingerprint(original.fingerprint).source_id, "src_1"
        )

    def test_lapsed_licences_are_findable(self) -> None:
        registry = self._sources()
        registry.register(_source("live", rights=Rights(
            basis=RightsBasis.LICENSED, expires_at=NOW + timedelta(days=90))))
        registry.register(_source("lapsed", rights=Rights(
            basis=RightsBasis.LICENSED, expires_at=NOW - timedelta(days=1))))
        registry.register(_source("perpetual"))
        self.assertEqual(
            [s.source_id for s in registry.expiring_before(NOW)], ["lapsed"]
        )

    def test_find_still_ranks_the_way_it_did(self) -> None:
        """The scoring is inherited, not reimplemented in SQL. Two rankings
        would drift apart, and the one nobody reads would be the live one."""

        registry = self._sources()
        registry.register(_source("on_topic", topics=("cars", "luxury")))
        registry.register(_source("off_topic", topics=("cooking",)))
        # A podcast is not a source kind the Cars profile draws from, so this
        # also pins that the kind filter survived the move to a table.
        registry.register(_source("wrong_kind", kind=SourceKind.PODCAST,
                                  topics=("cars",)))
        found = registry.find(Niche.CARS, ["cars"], limit=10)
        self.assertEqual([s.source_id for s in found], ["on_topic"])


class DurableChannelsTest(_Base):
    def test_a_created_channel_is_there_after_a_restart(self) -> None:
        factory = self._factory()
        channel = factory.create_channel("Fast Cars", Niche.CARS,
                                         topics=("cars", "supercars"),
                                         budget_cents=50_000, timezone="Europe/Berlin")

        reopened = self._factory().channels[channel.channel_id]
        self.assertEqual(reopened.name, "Fast Cars")
        self.assertEqual(reopened.niche, Niche.CARS)
        self.assertEqual(reopened.topics, ("cars", "supercars"))
        self.assertEqual(reopened.budget.monthly_cents, 50_000)
        self.assertEqual(reopened.timezone, "Europe/Berlin")
        self.assertEqual(reopened.accepted_rights, channel.accepted_rights)

    def test_a_tripped_breaker_stays_tripped(self) -> None:
        """The one that matters most. A channel that stopped itself because it
        was failing must not come back running after a deploy — otherwise every
        failing channel in the portfolio retries at once, at exactly the moment
        the operator is least able to see it."""

        factory = self._factory()
        channel = factory.create_channel("Fragile", Niche.CARS)
        for attempt in range(6):
            channel.health.record_failure(f"TikTok 429 ({attempt})", NOW)
        channel.state = ChannelState.CIRCUIT_OPEN
        factory.channels[channel.channel_id] = channel

        recovered = self._factory().channels[channel.channel_id]
        self.assertEqual(recovered.state, ChannelState.CIRCUIT_OPEN)
        self.assertEqual(recovered.health.consecutive_failures, 6)
        self.assertTrue(recovered.health.circuit_open(NOW))
        self.assertEqual(recovered.health.last_error, "TikTok 429 (5)")

    def test_spend_is_not_forgotten_by_a_restart(self) -> None:
        """A budget that resets when the process does is not a budget."""

        factory = self._factory()
        channel = factory.create_channel("Spender", Niche.BUSINESS,
                                         budget_cents=10_000)
        channel.budget.charge(3_500)
        factory.channels[channel.channel_id] = channel

        recovered = self._factory().channels[channel.channel_id]
        self.assertEqual(recovered.budget.spent_cents, 3_500)
        self.assertEqual(recovered.budget.remaining_cents, 6_500)
        self.assertEqual(recovered.budget.period, channel.budget.period)

    def test_activation_survives(self) -> None:
        factory = self._factory()
        channel = factory.create_channel(
            "Live", Niche.CARS, accounts={Platform.TIKTOK: "acc_tt"}
        )
        factory.activate(channel.channel_id)
        self.assertEqual(
            self._factory().channels[channel.channel_id].state, ChannelState.ACTIVE
        )

    def test_connected_accounts_come_back_from_the_account_rows(self) -> None:
        """Not stored twice. The account row owns the channel link, so a
        disconnected account cannot leave a stale entry behind."""

        factory = self._factory()
        channel = factory.create_channel(
            "Multi", Niche.GAMING,
            accounts={Platform.TIKTOK: "acc_tt", Platform.YOUTUBE: "acc_yt"},
        )
        recovered = self._factory().channels[channel.channel_id]
        self.assertEqual(
            recovered.accounts,
            {Platform.TIKTOK: "acc_tt", Platform.YOUTUBE: "acc_yt"},
        )

        with self.db.unit_of_work(TENANT) as uow:
            account = uow.accounts.require("acc_yt")
            account.channel_id = None
            uow.accounts.save(account)
        self.assertEqual(
            self._factory().channels[channel.channel_id].accounts,
            {Platform.TIKTOK: "acc_tt"},
        )

    def test_used_material_is_not_clipped_again_after_a_restart(self) -> None:
        """Without this the factory republishes the same interview every time
        it is redeployed, which is the most visible possible failure: the same
        clip, twice, on a real audience's feed."""

        registry = self._sources()
        registry.register(_source("src_1"))
        fingerprint = registry.get("src_1").fingerprint

        factory = ChannelFactory(finder=registry, channels=self._channels())
        channel = factory.create_channel("Cars", Niche.CARS)
        channel.used_fingerprints.add(fingerprint)
        factory.channels[channel.channel_id] = channel

        recovered = self._factory().channels[channel.channel_id]
        self.assertEqual(recovered.used_fingerprints, {fingerprint})

    def test_the_factory_sees_every_channel_it_created(self) -> None:
        factory = self._factory()
        for name in ("Cars", "Luxury", "Business"):
            factory.create_channel(name, Niche.CARS)
        self.assertEqual(len(self._factory().channels), 3)
        self.assertEqual(
            {c.name for c in self._factory().channels.values()},
            {"Cars", "Luxury", "Business"},
        )


class DurableAnalyticsTest(_Base):
    def _published_post(self, post_id: str = "up_1", *, views: int = 1000):
        """A published upload with its clip, ready for readings."""

        with self.db.unit_of_work(TENANT) as uow:
            uow.channels.save(ChannelRecord(
                id="ch_1", tenant_id=TENANT, project_id=PROJECT, name="Cars",
                niche="cars", timezone="Europe/Berlin"))
            uow.accounts.save(SocialAccountRecord(
                id="acc_1", tenant_id=TENANT, channel_id="ch_1", platform="tiktok"))
            uow.clips.save(ClipRecord(id="cl_1", tenant_id=TENANT,
                                      channel_id="ch_1", duration_s=28.0))
            uow.uploads.save(UploadRecord(
                id=post_id, tenant_id=TENANT, channel_id="ch_1",
                account_id="acc_1", clip_id="cl_1", platform="tiktok",
                state="published", run_at=NOW, published_at=NOW,
                idempotency_key=f"idem-{post_id}"))

        return PostRecord(
            post_id=post_id,
            metrics=PostMetrics(
                post_id=post_id, platform=Platform.TIKTOK, published_at=NOW,
                snapshots=[
                    Snapshot(taken_at=NOW + timedelta(hours=1), age_hours=1.0,
                             views=views // 10, likes=12),
                    Snapshot(taken_at=NOW + timedelta(hours=24), age_hours=24.0,
                             views=views, likes=140, comments=9, shares=22,
                             avg_watch_pct=0.61,
                             retention=RetentionCurve(
                                 points=((0.0, 1.0), (0.5, 0.63), (1.0, 0.31)))),
                ],
            ),
            channel_id="ch_1", channel_name="Cars", niche="cars",
            account_id="acc_1", timezone="Europe/Berlin",
            hook_text="He lost the deal in one sentence", hook_type="curiosity",
            predicted_lift=0.18, hook_rank=2, explored=True,
            topic="negotiation", source_id="", clip_duration_s=28.0,
            caption_style="karaoke", gameplay_bed="subway_surfers",
            predicted_virality=0.72, hook_weights_version="hooks-v3",
            viral_weights_version="viral-v2", extra={"experiment": "hooks-A"},
        )

    def test_readings_survive_a_restart(self) -> None:
        store = DurableAnalyticsStore(self.db, TENANT)
        store.add(self._published_post())

        reopened = DurableAnalyticsStore(self.db, TENANT)
        self.assertEqual(reopened.load(), 1)
        record = reopened.get("up_1")
        self.assertIsNotNone(record)
        self.assertEqual(len(record.metrics.snapshots), 2)
        self.assertEqual(record.metrics.at_age(24.0).views, 1000)
        self.assertEqual(record.metrics.at_age(24.0).shares, 22)

    def test_the_decisions_behind_a_post_come_back_too(self) -> None:
        """Without these the readings are numbers with nothing attached, and
        every "best hook" or "best length" finding becomes unanswerable."""

        DurableAnalyticsStore(self.db, TENANT).add(self._published_post())
        reopened = DurableAnalyticsStore(self.db, TENANT)
        reopened.load()
        record = reopened.get("up_1")
        self.assertEqual(record.hook_text, "He lost the deal in one sentence")
        self.assertEqual(record.hook_type, "curiosity")
        self.assertEqual(record.hook_rank, 2)
        self.assertTrue(record.explored)
        self.assertAlmostEqual(record.predicted_lift, 0.18)
        self.assertEqual(record.topic, "negotiation")
        self.assertAlmostEqual(record.clip_duration_s, 28.0)
        self.assertEqual(record.caption_style, "karaoke")
        self.assertEqual(record.gameplay_bed, "subway_surfers")
        self.assertEqual(record.hook_weights_version, "hooks-v3")
        self.assertEqual(record.viral_weights_version, "viral-v2")
        self.assertEqual(record.extra, {"experiment": "hooks-A"})
        # Derived from the channel row rather than stored on the post.
        self.assertEqual(record.channel_name, "Cars")
        self.assertEqual(record.niche, "cars")
        self.assertEqual(record.timezone, "Europe/Berlin")

    def test_a_missing_retention_curve_stays_missing(self) -> None:
        """Null, not an imputed flat curve. Only YouTube reports one, and an
        invented curve is counted as a measured bad outcome."""

        record = self._published_post()
        # Snapshot is frozen — an immutable reading is the whole point — so
        # the curve-less one is built rather than edited.
        first = record.metrics.snapshots[0]
        record.metrics.snapshots[0] = Snapshot(
            taken_at=first.taken_at, age_hours=first.age_hours,
            views=first.views, likes=first.likes,
        )
        DurableAnalyticsStore(self.db, TENANT).add(record)

        reopened = DurableAnalyticsStore(self.db, TENANT)
        reopened.load()
        snapshots = reopened.get("up_1").metrics.snapshots
        self.assertFalse(snapshots[0].retention.available)
        self.assertTrue(snapshots[1].retention.available)
        self.assertAlmostEqual(snapshots[1].retention.at(0.5), 0.63)

    def test_a_repeat_collection_at_the_same_age_is_not_counted_twice(self) -> None:
        """Append-only, enforced by the database. A doubled view count is
        invisible in a dashboard and poisons every comparison built on it."""

        store = DurableAnalyticsStore(self.db, TENANT)
        store.add(self._published_post())
        store.add(self._published_post(views=1_000_000))

        reopened = DurableAnalyticsStore(self.db, TENANT)
        reopened.load()
        snapshots = reopened.get("up_1").metrics.snapshots
        self.assertEqual(len(snapshots), 2)
        # The first reading stands. A later collection at an age already read
        # is a duplicate, not a correction.
        self.assertEqual(reopened.get("up_1").metrics.at_age(24.0).views, 1000)

    def test_readings_for_an_unknown_post_do_not_blow_up(self) -> None:
        """Analytics legitimately arrive for posts this system did not
        schedule — a backfill, or a channel taken over mid-life."""

        store = DurableAnalyticsStore(self.db, TENANT)
        orphan = self._published_post("up_missing")
        with self.db.unit_of_work(TENANT) as uow:
            uow.uploads.delete("up_missing")
        store.add(orphan)
        self.assertIsNotNone(store.get("up_missing"))

    def test_the_statistics_are_the_inherited_ones(self) -> None:
        """`select`, `group` and `coverage` are not reimplemented over SQL.
        Two implementations of the same statistic drift, and the one nobody
        reads is the one that ships."""

        store = DurableAnalyticsStore(self.db, TENANT)
        store.add(self._published_post())
        mature = store.select(checkpoint_h=24.0)
        self.assertEqual([r.post_id for r in mature], ["up_1"])
        self.assertEqual(
            store.group(mature, "hook_type", "views", 24.0), {"curiosity": [1000.0]}
        )
        self.assertEqual(store.coverage(mature, "views", 24.0), (1, 1))


if __name__ == "__main__":
    unittest.main()
