"""One contract, two implementations.

Every test in `StoreContract` runs twice: once against `MemoryDatabase` and
once against a real Postgres, when `CLIPFORGE_TEST_DSN` points at one. The
Postgres case skips when it does not, so the suite stays runnable anywhere —
but the in-memory results only mean something because the same assertions pass
against the database, and the skip is reported rather than silent.

    createdb clipforge_test && (cd db && npx prisma migrate deploy)
    CLIPFORGE_TEST_DSN=postgresql://clipforge_app:...@localhost/clipforge_test \\
        python -m unittest tests.test_store_contract -v
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta

from clipforge.store import (
    ChannelRecord,
    ClipRecord,
    Conflict,
    JobRecord,
    MemoryDatabase,
    MetricSnapshotRecord,
    NotFound,
    ProjectRecord,
    SocialAccountRecord,
    SourceRecord,
    TenantRecord,
    TenantScopeError,
    UploadRecord,
    UserRecord,
)

NOW = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)
A = "ten_acme"
B = "ten_globex"


def _project(tenant: str = A, key: str = "proj_1") -> ProjectRecord:
    return ProjectRecord(id=key, tenant_id=tenant, name="Brand")


def _channel(tenant: str = A, key: str = "ch_1", project: str = "proj_1") -> ChannelRecord:
    return ChannelRecord(
        id=key, tenant_id=tenant, project_id=project, name="Cars", niche="cars"
    )


def _account(tenant: str = A, key: str = "acc_1", channel: str | None = "ch_1"):
    return SocialAccountRecord(
        id=key, tenant_id=tenant, channel_id=channel, platform="tiktok", handle="@a"
    )


def _upload(tenant: str = A, key: str = "up_1", **kwargs) -> UploadRecord:
    defaults = dict(
        id=key,
        tenant_id=tenant,
        channel_id="ch_1",
        account_id="acc_1",
        platform="tiktok",
        run_at=NOW,
        idempotency_key=f"idem-{key}",
    )
    defaults.update(kwargs)
    return UploadRecord(**defaults)


class StoreContract:
    """Assertions both implementations must satisfy. Mixed into the two cases
    below; not collected on its own, because it has no `database`."""

    def database(self):  # pragma: no cover - provided by the subclasses
        raise NotImplementedError

    def setUp(self) -> None:
        self.db = self.database()
        self.addCleanup(self.db.close)
        # Ids are globally unique — the primary key is `id` alone, not
        # (tenant_id, id) — so the two tenants get distinct ones. Tenant A
        # keeps the unsuffixed names the tests below refer to.
        for tenant, tag in ((A, "1"), (B, "b")):
            with self.db.unit_of_work(tenant) as uow:
                uow.tenants.save(TenantRecord(id=tenant, name=tenant))
                uow.projects.save(_project(tenant, f"proj_{tag}"))
                uow.channels.save(_channel(tenant, f"ch_{tag}", f"proj_{tag}"))
                uow.accounts.save(_account(tenant, f"acc_{tag}", f"ch_{tag}"))

    # -- basics ------------------------------------------------------------

    def test_a_record_survives_the_transaction_that_wrote_it(self) -> None:
        with self.db.unit_of_work(A) as uow:
            uow.clips.add(ClipRecord(id="cl_1", tenant_id=A, channel_id="ch_1",
                                     start_ms=0, end_ms=30_000, hook_text="Watch"))
        with self.db.unit_of_work(A) as uow:
            self.assertEqual(uow.clips.require("cl_1").hook_text, "Watch")

    def test_missing_row_is_none_not_an_error(self) -> None:
        with self.db.unit_of_work(A) as uow:
            self.assertIsNone(uow.clips.get("nope"))
            with self.assertRaises(NotFound):
                uow.clips.require("nope")

    def test_add_refuses_a_duplicate_id(self) -> None:
        with self.db.unit_of_work(A) as uow:
            uow.clips.add(ClipRecord(id="cl_1", tenant_id=A, channel_id="ch_1"))
            with self.assertRaises(Conflict):
                uow.clips.add(ClipRecord(id="cl_1", tenant_id=A, channel_id="ch_1"))

    def test_save_is_an_upsert(self) -> None:
        with self.db.unit_of_work(A) as uow:
            uow.clips.save(ClipRecord(id="cl_1", tenant_id=A, channel_id="ch_1",
                                      hook_text="first"))
            uow.clips.save(ClipRecord(id="cl_1", tenant_id=A, channel_id="ch_1",
                                      hook_text="second"))
            self.assertEqual(uow.clips.count(), 1)
            self.assertEqual(uow.clips.require("cl_1").hook_text, "second")

    def test_delete_reports_whether_anything_went(self) -> None:
        with self.db.unit_of_work(A) as uow:
            uow.clips.add(ClipRecord(id="cl_1", tenant_id=A, channel_id="ch_1"))
            self.assertTrue(uow.clips.delete("cl_1"))
            self.assertFalse(uow.clips.delete("cl_1"))

    # -- tenancy -----------------------------------------------------------

    def test_one_tenant_cannot_read_anothers_rows(self) -> None:
        with self.db.unit_of_work(A) as uow:
            uow.clips.add(ClipRecord(id="cl_a", tenant_id=A, channel_id="ch_1",
                                     hook_text="Acme's"))
        with self.db.unit_of_work(B) as uow:
            self.assertIsNone(uow.clips.get("cl_a"))
            self.assertEqual(uow.clips.all(), ())

    def test_writing_another_tenants_record_is_refused(self) -> None:
        with self.db.unit_of_work(B) as uow:
            with self.assertRaises(TenantScopeError):
                uow.clips.add(ClipRecord(id="cl_x", tenant_id=A, channel_id="ch_1"))

    def test_two_tenants_may_hold_the_same_natural_key(self) -> None:
        """A unique index scoped to the tenant, not global. Two customers can
        legitimately have a user at the same work address."""

        for tenant in (A, B):
            with self.db.unit_of_work(tenant) as uow:
                uow.users.add(UserRecord(id=f"u_{tenant}", tenant_id=tenant,
                                         email="ops@shared.example", role="admin"))
        with self.db.unit_of_work(A) as uow:
            self.assertEqual(uow.users.by_email("ops@shared.example").id, f"u_{A}")

    # -- transactions ------------------------------------------------------

    def test_an_exception_rolls_the_whole_block_back(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.db.unit_of_work(A) as uow:
                uow.clips.add(ClipRecord(id="cl_1", tenant_id=A, channel_id="ch_1"))
                uow.clips.add(ClipRecord(id="cl_2", tenant_id=A, channel_id="ch_1"))
                raise RuntimeError("halfway")
        with self.db.unit_of_work(A) as uow:
            self.assertEqual(uow.clips.count(), 0)

    def test_explicit_rollback_abandons_the_work(self) -> None:
        with self.db.unit_of_work(A) as uow:
            uow.clips.add(ClipRecord(id="cl_1", tenant_id=A, channel_id="ch_1"))
            uow.rollback()
        with self.db.unit_of_work(A) as uow:
            self.assertEqual(uow.clips.count(), 0)

    def test_a_fetched_record_is_a_copy_not_a_handle(self) -> None:
        """Mutating what `get` returned must change nothing until it is saved.
        Against Postgres that is inherent; the in-memory store has to work at
        it, and a double that skips the work hides real bugs."""

        with self.db.unit_of_work(A) as uow:
            uow.clips.add(ClipRecord(id="cl_1", tenant_id=A, channel_id="ch_1",
                                     hook_text="original"))
        with self.db.unit_of_work(A) as uow:
            fetched = uow.clips.require("cl_1")
            fetched.hook_text = "mutated in place"
        with self.db.unit_of_work(A) as uow:
            self.assertEqual(uow.clips.require("cl_1").hook_text, "original")

    # -- types round-trip --------------------------------------------------

    def test_json_arrays_and_times_survive_a_round_trip(self) -> None:
        moment = datetime(2026, 3, 14, 9, 30, 15, 123456, tzinfo=UTC)
        with self.db.unit_of_work(A) as uow:
            uow.clips.add(ClipRecord(
                id="cl_1", tenant_id=A, channel_id="ch_1",
                signals=["hook", "payoff"],
                scores={"virality": 0.81, "nested": {"hook": 3}},
                hook_candidates=[{"text": "one", "ctr": 0.04}],
                duration_s=28.5,
                created_at=moment,
            ))
        with self.db.unit_of_work(A) as uow:
            clip = uow.clips.require("cl_1")
        self.assertEqual(clip.signals, ["hook", "payoff"])
        self.assertEqual(clip.scores["nested"]["hook"], 3)
        self.assertEqual(clip.hook_candidates[0]["ctr"], 0.04)
        self.assertAlmostEqual(clip.duration_s, 28.5)

    def test_times_come_back_aware_and_in_utc(self) -> None:
        with self.db.unit_of_work(A) as uow:
            uow.uploads.add(_upload(run_at=datetime(2026, 3, 14, 17, 0, tzinfo=UTC)))
        with self.db.unit_of_work(A) as uow:
            run_at = uow.uploads.require("up_1").run_at
        self.assertIsNotNone(run_at.tzinfo)
        self.assertEqual(run_at.astimezone(UTC).hour, 17)

    def test_null_stays_null_rather_than_becoming_an_empty_dict(self) -> None:
        """`retention_curve` is null when a platform did not report one. An
        imputed empty curve would be counted as a measured bad outcome."""

        with self.db.unit_of_work(A) as uow:
            uow.uploads.add(_upload())
            uow.metrics.append(MetricSnapshotRecord(
                id="ms_1", tenant_id=A, upload_id="up_1", taken_at=NOW,
                age_hours=24.0, views=1000))
        with self.db.unit_of_work(A) as uow:
            self.assertIsNone(uow.metrics.for_upload("up_1")[0].retention_curve)

    # -- uploads -----------------------------------------------------------

    def test_the_idempotency_key_makes_a_double_publish_a_conflict(self) -> None:
        with self.db.unit_of_work(A) as uow:
            uow.uploads.add(_upload(key="up_1", idempotency_key="acc_1:cl_1:1700"))
            with self.assertRaises(Conflict):
                uow.uploads.add(_upload(key="up_2", idempotency_key="acc_1:cl_1:1700"))

    def test_due_returns_only_ready_posts_soonest_first(self) -> None:
        with self.db.unit_of_work(A) as uow:
            uow.uploads.add(_upload(key="late", run_at=NOW + timedelta(hours=2)))
            uow.uploads.add(_upload(key="ready", run_at=NOW - timedelta(minutes=5)))
            uow.uploads.add(_upload(key="soon", run_at=NOW - timedelta(minutes=1)))
            uow.uploads.add(_upload(key="done", run_at=NOW - timedelta(hours=1),
                                    state="published"))
            due = uow.uploads.due(NOW)
        self.assertEqual([u.id for u in due], ["ready", "soon"])

    def test_the_account_window_query_bounds_are_half_open(self) -> None:
        start = NOW
        end = NOW + timedelta(hours=1)
        with self.db.unit_of_work(A) as uow:
            uow.uploads.add(_upload(key="at_start", run_at=start))
            uow.uploads.add(_upload(key="at_end", run_at=end))
            found = uow.uploads.for_account_between("acc_1", start, end)
        self.assertEqual([u.id for u in found], ["at_start"])

    # -- sources -----------------------------------------------------------

    def test_the_same_material_is_not_ingested_twice(self) -> None:
        with self.db.unit_of_work(A) as uow:
            uow.sources.add(SourceRecord(id="src_1", tenant_id=A, title="Talk",
                                         fingerprint="sha:abc"))
            with self.assertRaises(Conflict):
                uow.sources.add(SourceRecord(id="src_2", tenant_id=A, title="Copy",
                                             fingerprint="sha:abc"))

    def test_marking_a_source_used_is_idempotent(self) -> None:
        with self.db.unit_of_work(A) as uow:
            uow.sources.add(SourceRecord(id="src_1", tenant_id=A, fingerprint="f1"))
            uow.sources.mark_used("ch_1", "src_1", NOW)
            uow.sources.mark_used("ch_1", "src_1", NOW + timedelta(days=1))
            self.assertEqual(len(uow.sources.used_by("ch_1")), 1)

    def test_unused_by_excludes_what_the_channel_already_clipped(self) -> None:
        with self.db.unit_of_work(A) as uow:
            uow.sources.add(SourceRecord(id="src_1", tenant_id=A, fingerprint="f1"))
            uow.sources.add(SourceRecord(id="src_2", tenant_id=A, fingerprint="f2"))
            uow.sources.mark_used("ch_1", "src_1", NOW)
            self.assertEqual([s.id for s in uow.sources.unused_by("ch_1")], ["src_2"])

    def test_rights_expiry_sweep_finds_the_lapsed_licences(self) -> None:
        with self.db.unit_of_work(A) as uow:
            uow.sources.add(SourceRecord(id="live", tenant_id=A, fingerprint="f1",
                                         rights_expires_at=NOW + timedelta(days=30)))
            uow.sources.add(SourceRecord(id="lapsed", tenant_id=A, fingerprint="f2",
                                         rights_expires_at=NOW - timedelta(days=1)))
            uow.sources.add(SourceRecord(id="perpetual", tenant_id=A, fingerprint="f3"))
            found = uow.sources.rights_expiring_before(NOW)
        self.assertEqual([s.id for s in found], ["lapsed"])

    # -- metrics -----------------------------------------------------------

    def test_a_double_collection_is_a_conflict_not_a_doubled_count(self) -> None:
        with self.db.unit_of_work(A) as uow:
            uow.uploads.add(_upload())
            uow.metrics.append(MetricSnapshotRecord(id="ms_1", tenant_id=A,
                                                    upload_id="up_1", taken_at=NOW,
                                                    age_hours=24.0, views=1000))
            with self.assertRaises(Conflict):
                uow.metrics.append(MetricSnapshotRecord(id="ms_2", tenant_id=A,
                                                        upload_id="up_1", taken_at=NOW,
                                                        age_hours=24.0, views=1100))

    def test_matched_age_lookup_tolerates_collection_jitter(self) -> None:
        with self.db.unit_of_work(A) as uow:
            uow.uploads.add(_upload(key="up_1"))
            uow.uploads.add(_upload(key="up_2", idempotency_key="idem-2"))
            uow.metrics.append(MetricSnapshotRecord(id="m1", tenant_id=A,
                                                    upload_id="up_1", taken_at=NOW,
                                                    age_hours=23.7))
            uow.metrics.append(MetricSnapshotRecord(id="m2", tenant_id=A,
                                                    upload_id="up_2", taken_at=NOW,
                                                    age_hours=24.2))
            uow.metrics.append(MetricSnapshotRecord(id="m3", tenant_id=A,
                                                    upload_id="up_1", taken_at=NOW,
                                                    age_hours=1.0))
            found = uow.metrics.at_age(24.0, tolerance=0.5)
        self.assertEqual({m.id for m in found}, {"m1", "m2"})

    # -- jobs --------------------------------------------------------------

    def test_a_queued_job_is_claimed_once(self) -> None:
        with self.db.unit_of_work(A) as uow:
            uow.jobs.enqueue(JobRecord(id="j1", tenant_id=A, kind="render_video",
                                       run_after=NOW - timedelta(minutes=1)))
            first = uow.jobs.claim("worker-1", NOW, limit=5)
            second = uow.jobs.claim("worker-2", NOW, limit=5)
        self.assertEqual([j.id for j in first], ["j1"])
        self.assertEqual(second, ())

    def test_claim_takes_the_urgent_job_before_the_merely_older_one(self) -> None:
        """Priority beats age. A single-slot worker must get `high` first even
        though `low` has been waiting two hours — that is the whole reason the
        column exists."""

        with self.db.unit_of_work(A) as uow:
            uow.jobs.enqueue(JobRecord(id="low", tenant_id=A, priority=200,
                                       run_after=NOW - timedelta(hours=2)))
            uow.jobs.enqueue(JobRecord(id="high", tenant_id=A, priority=10,
                                       run_after=NOW - timedelta(minutes=1)))
            first = uow.jobs.claim("w", NOW, limit=1)
            second = uow.jobs.claim("w", NOW, limit=1)
        self.assertEqual([j.id for j in first], ["high"])
        self.assertEqual([j.id for j in second], ["low"])

    def test_a_batch_claim_arrives_in_priority_order(self) -> None:
        """A worker that walks its batch in order should do the urgent work
        first. Postgres does not carry the CTE's ordering into RETURNING, so
        the repository restores it."""

        with self.db.unit_of_work(A) as uow:
            uow.jobs.enqueue(JobRecord(id="low", tenant_id=A, priority=200,
                                       run_after=NOW - timedelta(hours=2)))
            uow.jobs.enqueue(JobRecord(id="mid", tenant_id=A, priority=100,
                                       run_after=NOW - timedelta(minutes=30)))
            uow.jobs.enqueue(JobRecord(id="high", tenant_id=A, priority=10,
                                       run_after=NOW - timedelta(minutes=1)))
            taken = uow.jobs.claim("w", NOW, limit=3)
        self.assertEqual([j.id for j in taken], ["high", "mid", "low"])

    def test_a_job_not_yet_due_is_not_claimed(self) -> None:
        with self.db.unit_of_work(A) as uow:
            uow.jobs.enqueue(JobRecord(id="later", tenant_id=A,
                                       run_after=NOW + timedelta(hours=1)))
            self.assertEqual(uow.jobs.claim("w", NOW), ())

    def test_the_dedupe_key_returns_the_queued_job_rather_than_a_second_one(self) -> None:
        with self.db.unit_of_work(A) as uow:
            first = uow.jobs.enqueue(JobRecord(id="j1", tenant_id=A,
                                               dedupe_key="render:cl_1"))
            again = uow.jobs.enqueue(JobRecord(id="j2", tenant_id=A,
                                               dedupe_key="render:cl_1"))
            self.assertEqual(again.id, first.id)
            self.assertEqual(uow.jobs.count(), 1)

    def test_an_expired_lease_returns_the_job_to_the_queue(self) -> None:
        """The reason leases beat locks: a worker killed mid-job cannot release
        anything, and a lock nobody releases is a queue that stops."""

        with self.db.unit_of_work(A) as uow:
            uow.jobs.enqueue(JobRecord(id="j1", tenant_id=A, run_after=NOW))
            uow.jobs.claim("doomed-worker", NOW, lease_s=60)
            self.assertEqual(uow.jobs.reap(NOW + timedelta(seconds=30)), 0)
            self.assertEqual(uow.jobs.reap(NOW + timedelta(seconds=90)), 1)
            recovered = uow.jobs.claim("worker-2", NOW + timedelta(seconds=91))
        self.assertEqual([j.id for j in recovered], ["j1"])

    def test_a_heartbeat_from_the_wrong_owner_is_refused(self) -> None:
        with self.db.unit_of_work(A) as uow:
            uow.jobs.enqueue(JobRecord(id="j1", tenant_id=A, run_after=NOW))
            uow.jobs.claim("worker-1", NOW, lease_s=60)
            self.assertTrue(uow.jobs.heartbeat("j1", "worker-1", NOW + timedelta(minutes=5)))
            self.assertFalse(uow.jobs.heartbeat("j1", "worker-2", NOW + timedelta(minutes=9)))

    def test_a_failure_retries_until_max_attempts_then_dies(self) -> None:
        with self.db.unit_of_work(A) as uow:
            uow.jobs.enqueue(JobRecord(id="j1", tenant_id=A, run_after=NOW,
                                       max_attempts=3))
            for attempt in range(2):
                uow.jobs.claim("w", NOW)
                job = uow.jobs.fail("j1", f"boom {attempt}",
                                    NOW + timedelta(minutes=1), NOW)
                self.assertEqual(job.state, "queued")
            uow.jobs.claim("w", NOW + timedelta(minutes=2))
            job = uow.jobs.fail("j1", "boom final", NOW + timedelta(minutes=3), NOW)
        self.assertEqual(job.state, "dead")
        self.assertEqual(job.attempts, 3)

    def test_a_failure_with_no_retry_time_dies_immediately(self) -> None:
        with self.db.unit_of_work(A) as uow:
            uow.jobs.enqueue(JobRecord(id="j1", tenant_id=A, run_after=NOW))
            uow.jobs.claim("w", NOW)
            job = uow.jobs.fail("j1", "unrecoverable", None, NOW)
        self.assertEqual(job.state, "dead")

    def test_success_records_the_result(self) -> None:
        with self.db.unit_of_work(A) as uow:
            uow.jobs.enqueue(JobRecord(id="j1", tenant_id=A, run_after=NOW))
            uow.jobs.claim("w", NOW)
            job = uow.jobs.succeed("j1", {"video_id": "vid_1"}, NOW)
        self.assertEqual(job.state, "succeeded")
        self.assertEqual(job.result["video_id"], "vid_1")
        self.assertEqual(job.lease_owner, "")

    # -- channels ----------------------------------------------------------

    def test_circuit_breaker_state_is_persisted_not_recomputed(self) -> None:
        """A channel that tripped before a restart must stay tripped, or a
        deploy silently retries every failing channel at once."""

        with self.db.unit_of_work(A) as uow:
            channel = uow.channels.require("ch_1")
            channel.state = "circuit_open"
            channel.consecutive_failures = 5
            channel.circuit_opened_at = NOW
            channel.last_error = "TikTok 429"
            uow.channels.save(channel)
        with self.db.unit_of_work(A) as uow:
            channel = uow.channels.require("ch_1")
        self.assertEqual(channel.state, "circuit_open")
        self.assertEqual(channel.consecutive_failures, 5)
        self.assertEqual(channel.circuit_opened_at, NOW)

    def test_channels_filter_by_state_and_project(self) -> None:
        with self.db.unit_of_work(A) as uow:
            uow.channels.save(_channel(key="ch_2"))
            paused = _channel(key="ch_3")
            paused.state = "paused"
            uow.channels.save(paused)
            self.assertEqual({c.id for c in uow.channels.in_state("draft")},
                             {"ch_1", "ch_2"})
            self.assertEqual({c.id for c in uow.channels.for_project("proj_1")},
                             {"ch_1", "ch_2", "ch_3"})

    # -- accounts ----------------------------------------------------------

    def test_only_ciphertext_is_stored_for_credentials(self) -> None:
        """The record has no field for a plaintext token, which is the point:
        a database dump must not be a set of working credentials to other
        people's audiences."""

        self.assertNotIn("access_token",
                         {f for f in SocialAccountRecord.__dataclass_fields__})

    def test_expiring_refresh_tokens_are_findable_before_they_lapse(self) -> None:
        with self.db.unit_of_work(A) as uow:
            soon = _account(key="acc_soon", channel="ch_1")
            soon.refresh_valid_until = NOW + timedelta(days=3)
            uow.accounts.save(soon)
            later = _account(key="acc_later", channel="ch_1")
            later.refresh_valid_until = NOW + timedelta(days=200)
            uow.accounts.save(later)
            found = uow.accounts.refresh_expiring_before(NOW + timedelta(days=14))
        self.assertEqual([a.id for a in found], ["acc_soon"])


class MemoryStoreTest(StoreContract, unittest.TestCase):
    def database(self):
        return MemoryDatabase()


_DSN = os.environ.get("CLIPFORGE_TEST_DSN", "")


@unittest.skipUnless(_DSN, "set CLIPFORGE_TEST_DSN to run the contract on Postgres")
class PostgresStoreTest(StoreContract, unittest.TestCase):
    def database(self):
        from clipforge.store.postgres import PostgresDatabase

        database = PostgresDatabase(_DSN, min_size=1, max_size=4)
        self._truncate(database)
        return database

    def _truncate(self, database) -> None:
        """Start each test from an empty database.

        TRUNCATE is DDL-ish and not subject to row-level security, so it runs
        as the owner rather than through a unit of work. Cascading from
        `tenants` clears everything, because every table hangs off it.
        """

        import psycopg

        admin = os.environ.get("CLIPFORGE_TEST_ADMIN_DSN", _DSN)
        with psycopg.connect(admin) as connection:
            with connection.cursor() as cursor:
                cursor.execute("TRUNCATE TABLE tenants CASCADE")
            connection.commit()


if __name__ == "__main__":
    unittest.main()
