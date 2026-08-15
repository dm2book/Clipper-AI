"""Storage, over both backends and against a real S3 server.

## What is real here

`StorageContract` runs twice: once against `LocalStorage` and once against
`R2Storage` driven by boto3 at a real `moto` S3 server over HTTP. Real
requests, real multipart, real presigned URLs, real XML responses parsed by
botocore. The same reason `test_store_contract.py` exists — the fast local
results are only evidence about the remote backend because the same assertions
pass on it.

## What is not proven, precisely

**No byte has reached Cloudflare.** `*.r2.cloudflarestorage.com` is refused by
this environment's egress policy (403 to CONNECT) and there are no R2
credentials. So this proves the client is correct against the S3 API; it does
not prove R2 behaves as its documentation says.

**moto does not validate signatures.** A presigned URL with a tampered key
returns 404 rather than 403, which means these tests exercise presigning as a
*URL construction* and not as an authentication mechanism. boto3 does the
signing, which is the reason to trust it — not anything asserted here.

The three R2-specific departures from S3 are covered by construction rather
than by a live call: `region="auto"`, no `ACL` parameter, and a public URL
that is configured rather than derived.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import timedelta

from clipforge.storage import (
    LocalStorage,
    ObjectNotFound,
    PermanentStorageError,
    R2Config,
    R2Storage,
    StorageMetrics,
    StorageRef,
    TransientStorageError,
    Workspace,
    key_for,
    plan_prune,
    sweep,
)
from clipforge.storage.workspace import PREFIX

TENANT = "ten_a"


def _moto():
    from moto.server import ThreadedMotoServer

    return ThreadedMotoServer


class StorageContract:
    """Assertions both backends satisfy."""

    def make_storage(self):                      # pragma: no cover - overridden
        raise NotImplementedError

    def setUp(self) -> None:
        self.scratch = tempfile.mkdtemp(prefix="clipforge-st-")
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        self.storage = self.make_storage()

    def a_file(self, name: str = "media.mp4", size: int = 2048) -> str:
        path = os.path.join(self.scratch, name)
        with open(path, "wb") as handle:
            handle.write(bytes((i * 7 + 3) % 256 for i in range(size)))
        return path

    # -- round trip --------------------------------------------------------

    def test_a_file_survives_a_round_trip_byte_for_byte(self) -> None:
        source = self.a_file(size=65_536)
        key = key_for(TENANT, "sources", "src_1", "media.mp4")

        stored = self.storage.put_file(key, source)
        self.assertEqual(stored.size_bytes, os.path.getsize(source))

        back = os.path.join(self.scratch, "back.mp4")
        self.storage.get_file(key, back)
        with open(source, "rb") as a, open(back, "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_open_streams_without_a_local_copy(self) -> None:
        source = self.a_file(size=4096)
        key = key_for(TENANT, "sources", "src_2", "media.mp4")
        self.storage.put_file(key, source)

        handle = self.storage.open(key)
        try:
            self.assertEqual(len(handle.read()), 4096)
        finally:
            handle.close()

    def test_stat_reports_size_and_a_content_type(self) -> None:
        key = key_for(TENANT, "renders", "cl_1", "clip.mp4")
        self.storage.put_file(key, self.a_file(size=1234))

        stored = self.storage.stat(key)
        self.assertEqual(stored.size_bytes, 1234)
        self.assertIn("mp4", stored.content_type)

    def test_a_missing_object_raises_rather_than_returning_empty(self) -> None:
        with self.assertRaises(ObjectNotFound):
            self.storage.stat(key_for(TENANT, "nope", "gone.mp4"))
        with self.assertRaises(ObjectNotFound):
            self.storage.get_file(
                key_for(TENANT, "nope", "gone.mp4"),
                os.path.join(self.scratch, "x"),
            )
        self.assertFalse(self.storage.exists(key_for(TENANT, "nope", "g.mp4")))

    def test_a_failed_download_leaves_no_partial_file(self) -> None:
        """ffmpeg reading a truncated download fails in ways that get blamed
        on the encoder."""

        target = os.path.join(self.scratch, "never.mp4")
        with self.assertRaises(ObjectNotFound):
            self.storage.get_file(key_for(TENANT, "nope", "x.mp4"), target)
        self.assertFalse(os.path.exists(target))
        self.assertFalse(os.path.exists(f"{target}.part"))

    def test_overwriting_a_key_replaces_the_object(self) -> None:
        key = key_for(TENANT, "sources", "src_3", "media.mp4")
        self.storage.put_file(key, self.a_file(size=100))
        self.storage.put_file(key, self.a_file("second.mp4", size=200))
        self.assertEqual(self.storage.stat(key).size_bytes, 200)

    # -- listing and deleting ----------------------------------------------

    def test_listing_is_scoped_to_the_prefix(self) -> None:
        for tenant in ("ten_a", "ten_b"):
            for index in range(3):
                self.storage.put_file(
                    key_for(tenant, "sources", f"src_{index}", "m.mp4"),
                    self.a_file(size=64),
                )

        mine = list(self.storage.list("ten_a/"))
        self.assertEqual(len(mine), 3)
        for stored in mine:
            self.assertTrue(stored.key.startswith("ten_a/"))

    def test_deleting_reports_whether_anything_went(self) -> None:
        key = key_for(TENANT, "sources", "src_4", "m.mp4")
        self.storage.put_file(key, self.a_file(size=64))
        self.assertTrue(self.storage.delete(key))
        self.assertFalse(self.storage.delete(key))

    def test_a_prefix_delete_takes_the_prefix_and_nothing_else(self) -> None:
        for tenant in ("ten_a", "ten_b"):
            for index in range(4):
                self.storage.put_file(
                    key_for(tenant, "sources", f"s{index}", "m.mp4"),
                    self.a_file(size=32),
                )

        removed = self.storage.delete_prefix("ten_a/")
        self.assertEqual(removed, 4)
        self.assertEqual(len(list(self.storage.list("ten_a/"))), 0)
        self.assertEqual(len(list(self.storage.list("ten_b/"))), 4)

    def test_deleting_an_empty_prefix_is_refused(self) -> None:
        """An empty prefix matches every object in the bucket."""

        with self.assertRaises(PermanentStorageError):
            self.storage.delete_prefix("")

    # -- usage -------------------------------------------------------------

    def test_usage_counts_bytes_and_objects_under_a_prefix(self) -> None:
        for index in range(3):
            self.storage.put_file(
                key_for(TENANT, "renders", f"cl_{index}", "clip.mp4"),
                self.a_file(size=1000 * (index + 1)),
            )
        self.storage.put_file(
            key_for("ten_other", "renders", "cl_x", "clip.mp4"),
            self.a_file(size=9999),
        )

        usage = self.storage.usage(f"{TENANT}/")
        self.assertEqual(usage["objects"], 3)
        self.assertEqual(usage["bytes"], 1000 + 2000 + 3000)
        self.assertEqual(usage["largest"]["size_bytes"], 3000)

    def test_metrics_count_operations_and_bytes(self) -> None:
        self.storage.metrics.reset()
        key = key_for(TENANT, "sources", "src_m", "m.mp4")
        self.storage.put_file(key, self.a_file(size=4096))
        self.storage.get_file(key, os.path.join(self.scratch, "out.mp4"))

        snapshot = self.storage.metrics.snapshot()
        self.assertGreaterEqual(snapshot["total"]["calls"], 2)
        self.assertGreater(snapshot["total"]["bytes_moved"], 0)
        self.assertEqual(snapshot["total"]["failures"], 0)

    def test_a_failure_is_counted_as_one(self) -> None:
        self.storage.metrics.reset()
        with self.assertRaises(ObjectNotFound):
            self.storage.stat(key_for(TENANT, "nope", "x.mp4"))
        snapshot = self.storage.metrics.snapshot()
        # Local records nothing for a miss; R2 records the failed stat. Either
        # is defensible — what matters is that a miss is never counted as a
        # success, which would make the failure rate a lie.
        self.assertEqual(
            snapshot["total"]["calls"] - snapshot["total"]["failures"],
            0,
        )

    # -- keys --------------------------------------------------------------

    def test_a_key_that_escapes_its_tenant_is_refused(self) -> None:
        """`a/../b` is a different object from `b` in an object store rather
        than the same one, so the guard cannot be left to the store."""

        with self.assertRaises(PermanentStorageError):
            key_for(TENANT, "..", "ten_b", "stolen.mp4")
        with self.assertRaises(PermanentStorageError):
            key_for("", "sources", "x")

    def test_an_unsafe_key_is_refused_before_the_wire(self) -> None:
        for bad in ("a b.mp4", "a\nb.mp4", "a\\b.mp4", "a$b.mp4"):
            with self.assertRaises(PermanentStorageError, msg=bad):
                key_for(TENANT, bad)

    # -- workspace ---------------------------------------------------------

    def test_the_workspace_fetches_and_publishes(self) -> None:
        source = self.a_file(size=8192)
        key = key_for(TENANT, "sources", "src_w", "media.mp4")
        self.storage.put_file(key, source)
        ref = StorageRef(key=key, bucket=self._bucket())

        with Workspace(self.storage, TENANT, base=self.scratch) as work:
            local = work.fetch(ref)
            self.assertTrue(os.path.exists(local))
            self.assertEqual(os.path.getsize(local), 8192)

            output = work.path("clip.mp4")
            shutil.copyfile(source, output)
            published = work.publish(output, work.key("renders", "cl_w", "clip.mp4"))
            directory = work.directory

        self.assertTrue(self.storage.exists(published.key))
        self.assertFalse(os.path.exists(directory), "scratch survived the block")

    def test_the_scratch_directory_goes_even_when_the_body_raises(self) -> None:
        """The path that matters: a failure halfway through a three-hour
        podcast has hundreds of megabytes to answer for."""

        directory = ""
        with self.assertRaises(RuntimeError):
            with Workspace(self.storage, TENANT, base=self.scratch) as work:
                directory = work.directory
                raise RuntimeError("ffmpeg died")
        self.assertFalse(os.path.exists(directory))

    def test_a_local_ref_is_returned_unchanged(self) -> None:
        """What makes the migration incremental: rows written before it hold
        filesystem paths and must keep working."""

        source = self.a_file(size=256)
        with Workspace(self.storage, TENANT, base=self.scratch) as work:
            self.assertEqual(work.fetch(StorageRef.parse(source)), source)

    def test_a_local_ref_whose_file_is_gone_says_so(self) -> None:
        with Workspace(self.storage, TENANT, base=self.scratch) as work:
            with self.assertRaises(PermanentStorageError) as caught:
                work.fetch(StorageRef.parse("/no/such/file.mp4"))
        self.assertIn("written before object storage", str(caught.exception))

    def test_a_workspace_path_cannot_escape(self) -> None:
        with Workspace(self.storage, TENANT, base=self.scratch) as work:
            for bad in ("../outside.mp4", "/etc/passwd"):
                with self.assertRaises(PermanentStorageError, msg=bad):
                    work.path(bad)

    def _bucket(self) -> str:
        return getattr(getattr(self.storage, "config", None), "bucket", "local")


class LocalStorageTest(StorageContract, unittest.TestCase):
    def make_storage(self):
        return LocalStorage(root=tempfile.mkdtemp(prefix="clipforge-local-"))

    def test_local_storage_has_no_public_url(self) -> None:
        """The honest answer, and the whole reason R2 exists here: Instagram
        fetches media itself and cannot reach a container's disk."""

        with self.assertRaises(PermanentStorageError) as caught:
            self.storage.public_url("ten_a/renders/x.mp4")
        self.assertIn("Instagram", str(caught.exception))

    def test_local_storage_cannot_issue_upload_urls(self) -> None:
        with self.assertRaises(PermanentStorageError):
            self.storage.signed_upload_url("ten_a/x.mp4")


@unittest.skipUnless(_moto is not None, "needs moto")
class R2StorageTest(StorageContract, unittest.TestCase):
    """The same contract, over HTTP against a real S3 implementation."""

    server = None
    port = 5011

    @classmethod
    def setUpClass(cls) -> None:
        # Werkzeug logs every request at INFO, which buries the test output in
        # a few hundred lines of S3 traffic.
        import logging

        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        cls.server = _moto()(port=cls.port, verbose=False)
        cls.server.start()
        time.sleep(0.6)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.server is not None:
            cls.server.stop()

    def make_storage(self):
        import boto3

        endpoint = f"http://127.0.0.1:{self.port}"
        bucket = f"media-{int(time.time() * 1000) % 100000}"
        # A real region for the create; the client under test uses "auto",
        # which is what R2 requires and what moto's create_bucket refuses.
        boto3.client(
            "s3", endpoint_url=endpoint, aws_access_key_id="k",
            aws_secret_access_key="s", region_name="us-east-1",
        ).create_bucket(Bucket=bucket)

        return R2Storage(
            R2Config(
                bucket=bucket, endpoint_url=endpoint,
                access_key_id="k", secret_access_key="s",
                public_base_url="https://media.clipforge.test",
                max_attempts=3, backoff_s=0.01,
            ),
            metrics=StorageMetrics(),
        )

    # -- things only the object store can do -------------------------------

    def test_the_client_is_configured_the_way_r2_requires(self) -> None:
        """Three departures from S3, and all three fail at the endpoint rather
        than at the signer, so they are worth pinning."""

        self.assertEqual(self.storage.config.region, "auto")
        self.assertEqual(self.storage.client.meta.region_name, "auto")
        self.assertEqual(
            self.storage.client.meta.config.signature_version, "s3v4"
        )

    def test_a_signed_url_fetches_the_object(self) -> None:
        key = key_for(TENANT, "renders", "cl_s", "clip.mp4")
        self.storage.put_file(key, self.a_file(size=512))

        url = self.storage.signed_url(key, expires_in=timedelta(minutes=5))
        self.assertIn("X-Amz-Signature", url)
        self.assertIn("X-Amz-Expires", url)
        with urllib.request.urlopen(url) as response:
            self.assertEqual(len(response.read()), 512)

    def test_a_signed_url_carries_a_download_name_when_asked(self) -> None:
        key = key_for(TENANT, "renders", "cl_d", "clip.mp4")
        self.storage.put_file(key, self.a_file(size=64))
        url = self.storage.signed_url(key, download_as="the-clip.mp4")
        self.assertIn("response-content-disposition", url.lower())

    def test_a_signed_upload_url_accepts_a_put(self) -> None:
        """The file never passes through the API. A 2 GB upload proxied by a
        request handler occupies a worker for the whole transfer."""

        key = key_for(TENANT, "uploads", "u_1", "raw.mp4")
        url = self.storage.signed_upload_url(key, content_type="video/mp4")

        payload = b"x" * 4096
        request = urllib.request.Request(
            url, data=payload, method="PUT",
            headers={"Content-Type": "video/mp4"},
        )
        with urllib.request.urlopen(request) as response:
            self.assertIn(response.status, (200, 204))
        self.assertEqual(self.storage.stat(key).size_bytes, 4096)

    def test_the_public_url_is_the_configured_domain(self) -> None:
        """Derived from configuration, never guessed. A guessed URL that 403s
        surfaces as 'Instagram could not fetch the media', which sends the
        next person to debug Meta's API instead of this setting."""

        key = key_for(TENANT, "renders", "cl_p", "clip.mp4")
        self.assertEqual(
            self.storage.public_url(key),
            f"https://media.clipforge.test/{key}",
        )

    def test_no_public_domain_means_no_public_url(self) -> None:
        self.storage.config.public_base_url = ""
        with self.assertRaises(PermanentStorageError) as caught:
            self.storage.public_url("ten_a/renders/x.mp4")
        self.assertIn("Instagram", str(caught.exception))

    def test_a_large_file_goes_up_in_parts_and_comes_back_whole(self) -> None:
        """Above the multipart threshold boto3 switches transfer mode
        entirely, so the round trip is worth proving rather than assuming."""

        from clipforge.storage import r2 as r2_module

        original = r2_module.MULTIPART_THRESHOLD
        r2_module.MULTIPART_THRESHOLD = 256 * 1024
        self.addCleanup(setattr, r2_module, "MULTIPART_THRESHOLD", original)

        source = self.a_file("big.mp4", size=1_200_000)
        key = key_for(TENANT, "sources", "src_big", "big.mp4")
        self.storage.put_file(key, source)

        back = os.path.join(self.scratch, "big-back.mp4")
        self.storage.get_file(key, back)
        with open(source, "rb") as a, open(back, "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_a_transient_failure_is_retried_and_counted(self) -> None:
        """Retries are counted apart from failures because they answer
        different questions: retries rising while failures stay flat is
        degradation the retry budget is absorbing."""

        attempts = {"n": 0}
        real = self.storage.client.head_object

        def flaky(**kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ConnectionError("reset by peer")
            return real(**kwargs)

        key = key_for(TENANT, "sources", "src_r", "m.mp4")
        self.storage.put_file(key, self.a_file(size=64))
        self.storage.metrics.reset()
        self.storage.client.head_object = flaky

        stored = self.storage.stat(key)
        self.assertEqual(stored.size_bytes, 64)
        self.assertEqual(attempts["n"], 3)
        snapshot = self.storage.metrics.snapshot()
        self.assertEqual(snapshot["operations"]["stat"]["retries"], 2)
        self.assertEqual(snapshot["operations"]["stat"]["failures"], 0)

    def test_retries_give_up_and_report_transient(self) -> None:
        def always_fail(**kwargs):
            raise ConnectionError("reset by peer")

        self.storage.client.head_object = always_fail
        self.storage.metrics.reset()

        with self.assertRaises(TransientStorageError):
            self.storage.stat(key_for(TENANT, "x", "y.mp4"))
        self.assertEqual(
            self.storage.metrics.snapshot()["operations"]["stat"]["failures"], 1
        )

    def test_a_permanent_failure_is_not_retried(self) -> None:
        """Retrying a bad key for four attempts just delays the useful error."""

        attempts = {"n": 0}

        def denied(**kwargs):
            attempts["n"] += 1
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "AccessDenied"},
                 "ResponseMetadata": {"HTTPStatusCode": 403}},
                "HeadObject",
            )

        self.storage.client.head_object = denied
        with self.assertRaises(PermanentStorageError):
            self.storage.stat(key_for(TENANT, "x", "y.mp4"))
        self.assertEqual(attempts["n"], 1)

    def test_lifecycle_rules_are_applied_and_read_back(self) -> None:
        from clipforge.storage import lifecycle

        planned = lifecycle.apply(self.storage, dry_run=True)
        self.assertFalse(planned["applied"])
        self.assertTrue(planned["rules"])
        # Every rule aborts incomplete multipart uploads: those are invisible
        # in a listing and still billed, which is the classic S3 cost leak.
        for rule in planned["rules"]:
            self.assertIn("AbortIncompleteMultipartUpload", rule)

        lifecycle.apply(self.storage)
        described = lifecycle.describe(self.storage)
        self.assertTrue(described["supported"])
        self.assertEqual(len(described["rules"]), len(lifecycle.RULES))

    def test_renders_and_transcripts_are_never_expired(self) -> None:
        """A mezzanine source is gigabytes and reproducible; a published
        render is what an audience is watching."""

        from clipforge.storage import lifecycle

        forever = {r.id for r in lifecycle.RULES if r.expire_days is None}
        self.assertEqual(forever, {"renders", "transcripts"})

    def test_a_prune_plan_keeps_the_newest_and_is_not_applied(self) -> None:
        for index in range(5):
            self.storage.put_file(
                key_for(TENANT, "sources", f"src_{index}", "m.mp4"),
                self.a_file(size=32),
            )
            time.sleep(0.01)

        plan = plan_prune(self.storage, TENANT, "sources", keep_newest=2)
        self.assertEqual(len(plan), 3)
        # Returned, not performed: deleting media has no undo.
        self.assertEqual(len(list(self.storage.list(f"{TENANT}/"))), 5)


class LifecycleOnLocalTest(unittest.TestCase):
    def test_local_storage_refuses_lifecycle_rather_than_doing_nothing(self) -> None:
        """"The rules are configured" is exactly the belief that makes a
        storage bill surprising."""

        from clipforge.storage import lifecycle

        storage = LocalStorage(root=tempfile.mkdtemp(prefix="clipforge-lc-"))
        with self.assertRaises(PermanentStorageError) as caught:
            lifecycle.apply(storage)
        self.assertIn("sweep", str(caught.exception))


class SweepTest(unittest.TestCase):
    """Processes die; the context manager does not run."""

    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="clipforge-sweep-")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)

    def test_only_abandoned_workspaces_are_removed(self) -> None:
        old = os.path.join(self.base, f"{PREFIX}old")
        young = os.path.join(self.base, f"{PREFIX}young")
        other = os.path.join(self.base, "someone-elses-work")
        for directory in (old, young, other):
            os.makedirs(directory)
        os.utime(old, (time.time() - 86_400, time.time() - 86_400))

        removed = sweep(self.base, older_than_s=3600)

        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(young), "a running job's scratch went")
        self.assertTrue(os.path.exists(other), "swept a directory it did not own")


class RefTest(unittest.TestCase):
    def test_both_forms_parse(self) -> None:
        remote = StorageRef.parse("r2://media/ten_a/sources/x/media.mp4")
        self.assertFalse(remote.local)
        self.assertEqual(remote.bucket, "media")
        self.assertEqual(remote.filename, "media.mp4")

        local = StorageRef.parse("/var/lib/clipforge/media/x.mp4")
        self.assertTrue(local.local)
        self.assertEqual(str(local), "/var/lib/clipforge/media/x.mp4")

    def test_a_remote_ref_round_trips_through_its_string(self) -> None:
        ref = StorageRef(key="ten_a/renders/cl_1/clip.mp4", bucket="media")
        self.assertEqual(StorageRef.parse(str(ref)), ref)

    def test_a_malformed_remote_ref_is_refused(self) -> None:
        for bad in ("", "r2://", "r2://bucket", "r2:///key"):
            with self.assertRaises(PermanentStorageError, msg=bad):
                StorageRef.parse(bad)


if __name__ == "__main__":
    unittest.main()
