"""The publishing system, backed by the store instead of by dictionaries.

Two things are checked here that the store's own tests cannot check:

* that `PublishingSystem` works unchanged when its dictionaries are swapped
  for the durable versions — the point of injecting them rather than rewriting
  the engine around a database;
* that a schedule survives a restart *as a schedule*, not merely as rows. A
  calendar that reloaded its posts but lost their leases, attempts or states
  would pass a row-count assertion and still double-publish.

Runs against both backends: in-memory when `CLIPFORGE_TEST_DSN` is unset,
Postgres when it is set. The in-memory case is not a durability test — it is
there so the wiring is exercised everywhere, and it is the Postgres case that
makes the claim.
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta

from clipforge.publish.engine import PublishConfig, PublishingSystem
from clipforge.publish.oauth import TokenSet
from clipforge.publish.types import (
    Account,
    MediaAsset,
    Platform,
    PostSpec,
    PostState,
)
from clipforge.store import (
    ChannelRecord,
    MemoryDatabase,
    ProjectRecord,
    TenantRecord,
)
from clipforge.store.durable import (
    DurableAccountBook,
    DurableTokenStore,
    PersistentCalendar,
)

#: Derived from the real clock rather than pinned to a date. `schedule()`
#: refuses a run_at in the past against `utcnow()`, so a hard-coded fixture
#: date is a suite that passes until the day it silently starts failing.
NOW = (
    datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    + timedelta(days=1)
)
TENANT = "ten_pub"
CHANNEL = "ch_pub"
PROJECT = "proj_pub"

#: Stands in for a KMS. Reversible and useless as encryption, which is the
#: point of it living in a test rather than in the store: the real key is held
#: by something other than the process that publishes.
def _seal(plain: str) -> str:
    return f"sealed:{plain}"


def _unseal(sealed: str) -> str:
    return sealed.removeprefix("sealed:")


def _spec(asset_id: str = "asset_1") -> PostSpec:
    return PostSpec(
        asset=MediaAsset(asset_id=asset_id, path=f"/out/{asset_id}.mp4",
                         public_url=f"https://cdn.example/{asset_id}.mp4",
                         size_bytes=8_400_000, duration_s=31.5),
        title="How he lost the deal",
        caption="The one line that ended it",
        hashtags=("business", "negotiation"),
        per_platform_caption={"youtube": "A longer description for search"},
        metadata={"clip_id": "cl_9", "hook_type": "curiosity"},
    )


class _Backend:
    """Chooses in-memory or Postgres from the environment."""

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


class DurablePublishingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _Backend.open()
        self.addCleanup(self.db.close)
        with self.db.unit_of_work(TENANT) as uow:
            uow.tenants.save(TenantRecord(id=TENANT, name="Publisher Co"))
            uow.projects.save(ProjectRecord(id=PROJECT, tenant_id=TENANT,
                                            name="Brand"))
            uow.channels.save(ChannelRecord(id=CHANNEL, tenant_id=TENANT,
                                            project_id=PROJECT, name="Business",
                                            niche="business", state="active"))

    def _system(self, *, load: bool = False) -> PublishingSystem:
        """A publishing system wired to the store. `load` rehydrates first."""

        calendar = PersistentCalendar(self.db, TENANT, channel_id=CHANNEL)
        if load:
            calendar.load(NOW)
        return PublishingSystem(
            config=PublishConfig(worker_id="w1"),
            token_store=DurableTokenStore(self.db, TENANT, seal=_seal,
                                          unseal=_unseal),
            accounts=DurableAccountBook(self.db, TENANT, channel_id=CHANNEL),
            calendar=calendar,
        )

    def _connect(self, system: PublishingSystem) -> None:
        system.connect(
            Account(account_id="acc_tt", platform=Platform.TIKTOK, org_id=TENANT,
                    handle="@business", direct_post_approved=True),
            TokenSet(account_id="acc_tt", platform=Platform.TIKTOK,
                     access_token="at-secret", refresh_token="rt-secret",
                     expires_at=NOW + timedelta(hours=2),
                     refresh_valid_until=NOW + timedelta(days=300),
                     scopes=("video.publish",), obtained_at=NOW),
        )

    # -- wiring ------------------------------------------------------------

    def test_the_engine_works_unchanged_with_durable_stores(self) -> None:
        system = self._system()
        self._connect(system)
        post = system.schedule("acc_tt", _spec(), NOW + timedelta(hours=3))
        self.assertEqual(post.state, PostState.SCHEDULED)
        self.assertEqual(len(system.calendar), 1)
        self.assertEqual(len(system.accounts), 1)
        self.assertIn("acc_tt", system.accounts)
        self.assertEqual(system.accounts["acc_tt"].handle, "@business")

    def test_readiness_reads_accounts_back_out_of_the_store(self) -> None:
        system = self._system()
        self._connect(system)
        reports = system.readiness()
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].account_id, "acc_tt")

    # -- survival ----------------------------------------------------------

    def test_a_schedule_survives_a_new_process(self) -> None:
        first = self._system()
        self._connect(first)
        for hours in (3, 27, 51):
            first.schedule("acc_tt", _spec(f"asset_{hours}"),
                           NOW + timedelta(hours=hours))
        self.assertEqual(len(first.calendar), 3)

        # A different object graph entirely — nothing carried over but the
        # database. This is the in-process stand-in for a restart; the real
        # kill-the-process version is in test_restart_survival.py.
        second = self._system(load=True)
        self.assertEqual(len(second.calendar), 3)
        self.assertEqual(
            [p.run_at for p in second.calendar.posts],
            [NOW + timedelta(hours=h) for h in (3, 27, 51)],
        )

    def test_the_whole_post_comes_back_not_just_its_time(self) -> None:
        """Field by field. A calendar that reloaded times and lost captions
        would pass a count assertion and post empty descriptions."""

        first = self._system()
        self._connect(first)
        original = first.schedule("acc_tt", _spec(), NOW + timedelta(hours=3),
                                  series_id="weekly-business")

        restored = self._system(load=True).calendar.get(original.post_id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.account_id, original.account_id)
        self.assertEqual(restored.platform, original.platform)
        self.assertEqual(restored.run_at, original.run_at)
        self.assertEqual(restored.state, original.state)
        self.assertEqual(restored.idempotency_key, original.idempotency_key)
        self.assertEqual(restored.series_id, "weekly-business")
        self.assertEqual(restored.spec.title, original.spec.title)
        self.assertEqual(restored.spec.caption, original.spec.caption)
        self.assertEqual(restored.spec.hashtags, original.spec.hashtags)
        self.assertEqual(restored.spec.per_platform_caption,
                         original.spec.per_platform_caption)
        self.assertEqual(restored.spec.visibility, original.spec.visibility)
        self.assertEqual(restored.spec.metadata, original.spec.metadata)
        self.assertEqual(restored.spec.asset, original.spec.asset)

    def test_a_lease_survives_so_a_second_worker_does_not_republish(self) -> None:
        """The failure this prevents: worker one claims a post and dies, worker
        two starts, sees no lease because leases lived in memory, and publishes
        the same video to the same audience."""

        first = self._system()
        self._connect(first)
        first.schedule("acc_tt", _spec(), NOW - timedelta(minutes=5))
        claimed = first.claim(NOW)
        self.assertEqual(len(claimed), 1)

        second = self._system(load=True)
        post = second.calendar.posts[0]
        self.assertEqual(post.state, PostState.CLAIMED)
        self.assertIsNotNone(post.lease_until)
        self.assertEqual(second.claim(NOW), [])

        # Once the lease lapses it is claimable again — the other half of why
        # this is a lease and not a lock.
        self.assertEqual(
            len(second.claim(NOW + timedelta(seconds=PublishConfig().lease_s + 60))),
            1,
        )

    def test_a_post_claimed_by_a_crashed_worker_is_not_stuck_for_ever(self) -> None:
        """Found by making leases durable.

        While leases lived in memory a crash took the `CLAIMED` state with it,
        and the post came back looking `SCHEDULED`. Persisted, the state
        survives the crash — so unless an expired lease makes a post claimable
        again, a worker dying at the wrong moment means that clip silently
        never posts, and nobody finds out until a creator asks about it.
        """

        first = self._system()
        self._connect(first)
        post = first.schedule("acc_tt", _spec(), NOW - timedelta(minutes=5))
        first.claim(NOW)

        # The worker dies here. Nothing releases the lease.
        after = NOW + timedelta(seconds=PublishConfig().lease_s + 1)
        recovered = self._system(load=True)
        self.assertEqual(
            [p.post_id for p in recovered.claim(after)], [post.post_id]
        )

    def test_an_in_flight_post_is_not_reclaimed_when_its_lease_lapses(self) -> None:
        """The other side of the same rule. In `uploading` or `processing` the
        platform has already been told to create something, so the post must be
        reconciled against it — re-running it is how the same video reaches the
        same audience twice."""

        first = self._system()
        self._connect(first)
        post = first.schedule("acc_tt", _spec(), NOW - timedelta(minutes=5))
        first.claim(NOW)
        post.state = PostState.UPLOADING
        first.calendar.persist(post)

        after = NOW + timedelta(seconds=PublishConfig().lease_s + 1)
        self.assertEqual(self._system(load=True).claim(after), [])

    def test_a_cancellation_is_not_undone_by_a_restart(self) -> None:
        first = self._system()
        self._connect(first)
        post = first.schedule("acc_tt", _spec(), NOW + timedelta(hours=3))
        self.assertTrue(first.cancel(post.post_id))

        second = self._system(load=True)
        self.assertEqual(second.calendar.get(post.post_id).state,
                         PostState.CANCELLED)

    def test_a_reschedule_keeps_one_row_rather_than_leaving_a_ghost(self) -> None:
        first = self._system()
        self._connect(first)
        post = first.schedule("acc_tt", _spec(), NOW + timedelta(hours=3))
        first.reschedule(post.post_id, NOW + timedelta(hours=30))

        second = self._system(load=True)
        self.assertEqual(len(second.calendar), 1)
        self.assertEqual(second.calendar.get(post.post_id).run_at,
                         NOW + timedelta(hours=30))

    # -- credentials -------------------------------------------------------

    def test_tokens_round_trip_and_are_stored_sealed(self) -> None:
        system = self._system()
        self._connect(system)
        tokens = system.tokens.get("acc_tt")
        self.assertEqual(tokens.access_token, "at-secret")
        self.assertEqual(tokens.refresh_token, "rt-secret")
        self.assertEqual(tokens.scopes, ("video.publish",))

        with self.db.unit_of_work(TENANT) as uow:
            record = uow.accounts.require("acc_tt")
        self.assertEqual(record.access_token_sealed, "sealed:at-secret")
        self.assertEqual(record.refresh_token_sealed, "sealed:rt-secret")
        # The record has nowhere to put a plaintext token even by accident.
        self.assertNotIn("access_token", record.__dataclass_fields__)

    def test_disconnecting_clears_credentials_without_deleting_history(self) -> None:
        system = self._system()
        self._connect(system)
        post = system.schedule("acc_tt", _spec(), NOW + timedelta(hours=3))
        system.tokens.delete("acc_tt")

        self.assertIsNone(system.tokens.get("acc_tt"))
        # The account and its scheduled post are still there. Deleting the row
        # would have cascaded the post away with it.
        self.assertIn("acc_tt", system.accounts)
        self.assertIsNotNone(self._system(load=True).calendar.get(post.post_id))

    def test_updating_an_account_does_not_wipe_its_tokens(self) -> None:
        """`accounts[id] = account` is a rename, not a disconnect."""

        system = self._system()
        self._connect(system)
        system.accounts["acc_tt"] = Account(
            account_id="acc_tt", platform=Platform.TIKTOK, org_id=TENANT,
            handle="@business_renamed", direct_post_approved=True,
        )
        self.assertEqual(system.accounts["acc_tt"].handle, "@business_renamed")
        self.assertEqual(system.tokens.get("acc_tt").access_token, "at-secret")

    def test_expiring_credentials_are_findable_before_a_channel_goes_quiet(self) -> None:
        system = self._system()
        self._connect(system)
        store = system.tokens
        self.assertEqual(store.expiring_before(NOW + timedelta(days=30)), ())
        self.assertEqual(store.expiring_before(NOW + timedelta(days=400)),
                         ("acc_tt",))

    # -- the working-set window -------------------------------------------

    def test_load_is_bounded_by_its_horizon(self) -> None:
        """The index is a cache of a window, not of the table. A post beyond
        the horizon is still in the database and still publishes — it is just
        not in this process's view yet."""

        first = self._system()
        self._connect(first)
        near = first.schedule("acc_tt", _spec("near"), NOW + timedelta(days=10))
        far = first.schedule("acc_tt", _spec("far"), NOW + timedelta(days=200))

        windowed = PersistentCalendar(self.db, TENANT, channel_id=CHANNEL)
        windowed.load(NOW, horizon_days=90)
        self.assertEqual([p.post_id for p in windowed.posts], [near.post_id])

        wide = PersistentCalendar(self.db, TENANT, channel_id=CHANNEL)
        wide.load(NOW, horizon_days=365)
        self.assertEqual(
            {p.post_id for p in wide.posts}, {near.post_id, far.post_id}
        )

    def test_rehydration_does_not_rewrite_what_it_read(self) -> None:
        """The guard against the obvious bug: `ContentCalendar.__init__` and
        `load` both go through `add`, which writes through."""

        first = self._system()
        self._connect(first)
        first.schedule("acc_tt", _spec(), NOW + timedelta(hours=3))

        with self.db.unit_of_work(TENANT) as uow:
            first_written = uow.uploads.all()[0].updated_at

        self._system(load=True)
        with self.db.unit_of_work(TENANT) as uow:
            self.assertEqual(uow.uploads.count(), 1)
            self.assertEqual(uow.uploads.all()[0].updated_at, first_written)


if __name__ == "__main__":
    unittest.main()
