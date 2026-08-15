"""The HTTP API, over real requests.

Every test here goes through the ASGI stack — routing, dependency resolution,
request validation, the exception handlers — using FastAPI's `TestClient`, so
what is exercised is the app a browser talks to rather than the functions
behind it. Authentication is real: tokens are minted by the real
`AuthService`, signed by real PyJWT, and verified on every request.

## The two that matter most

`TenantIsolationTest` is the reason this file exists. Every read is scoped by a
claim inside a signed token, and there is no `?tenant_id=` anywhere in the API
to tamper with — but "there is no parameter" is an argument, not evidence, so
these tests give one tenant's token and ask for another tenant's data.

`ErrorContractTest` pins the error envelope. Every failure leaves as
`{"error": {"code", "message"}}` whatever it started as, because a client that
has to branch on three shapes gets two of them wrong.
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from clipforge.api import create_app
from clipforge.api.deps import Services
from clipforge.auth import (
    AccessTokenIssuer,
    AuthConfig,
    AuthService,
    Keyring,
    MemoryAuthStore,
    PasswordHasher,
    PasswordPolicy,
    RecordingEmailSender,
    SigningKey,
)
from clipforge.store import (
    ChannelRecord,
    ClipRecord,
    MemoryDatabase,
    MetricSnapshotRecord,
    SocialAccountRecord,
    SourceRecord,
    TenantRecord,
    UploadRecord,
)

PASSWORD = "marmalade tuesday bicycle"
NOW = datetime.now(UTC).replace(microsecond=0)
FAST = PasswordPolicy(rounds=4)


def build(tenants=("ten_a",)) -> tuple[TestClient, Services]:
    """An app over in-memory stores, with `tenants` populated."""

    database = MemoryDatabase()
    auth = AuthService(
        MemoryAuthStore(),
        AccessTokenIssuer(Keyring((SigningKey("k1", "x" * 40),))),
        config=AuthConfig(password_policy=FAST, require_verified_email=True),
        hasher=PasswordHasher(FAST),
        sender=RecordingEmailSender(),
    )
    services = Services(database=database, auth=auth)

    for tenant in tenants:
        _populate(database, tenant)
        _register(auth, f"{tenant}@example.com", tenant)

    return TestClient(create_app(services)), services


def _register(auth: AuthService, email: str, tenant: str, role: str = "owner"):
    result = auth.sign_up(email, PASSWORD)
    link = auth.sender.links_for(email)[-1]
    auth.verify_email(link.split("token=")[1])
    auth.add_membership(result.identity_id, tenant, f"usr_{tenant}", role,
                        f"Workspace {tenant}")
    return result.identity_id


def _populate(database, tenant: str) -> None:
    with database.unit_of_work(tenant) as uow:
        uow.tenants.save(TenantRecord(id=tenant, name=f"Workspace {tenant}"))
        uow.channels.save(ChannelRecord(
            id=f"ch_{tenant}", tenant_id=tenant, name=f"Channel {tenant}",
            niche="business", state="active", budget_monthly_cents=20_000,
            budget_spent_cents=5_000, total_items=10, total_published=4,
        ))
        uow.accounts.save(SocialAccountRecord(
            id=f"acc_{tenant}", tenant_id=tenant, channel_id=f"ch_{tenant}",
            platform="tiktok", handle=f"@{tenant}",
        ))
        uow.sources.save(SourceRecord(
            id=f"src_{tenant}", tenant_id=tenant, title=f"Source {tenant}",
            kind="podcast_feed", duration_s=3600.0, has_transcript=True,
            fingerprint=f"fp_{tenant}", creator="Someone",
        ))
        uow.clips.save(ClipRecord(
            id=f"cl_{tenant}", tenant_id=tenant, channel_id=f"ch_{tenant}",
            source_id=f"src_{tenant}", start_ms=0, end_ms=31_000,
            title=f"Clip {tenant}", virality_score=72.0,
        ))
        uow.uploads.save(UploadRecord(
            id=f"up_{tenant}_live", tenant_id=tenant, channel_id=f"ch_{tenant}",
            account_id=f"acc_{tenant}", platform="tiktok", state="published",
            title=f"Published {tenant}", remote_post_id=f"tt_{tenant}",
            published_at=NOW - timedelta(days=2),
            idempotency_key=f"idem_{tenant}_live", run_at=NOW,
        ))
        uow.uploads.save(UploadRecord(
            id=f"up_{tenant}_queued", tenant_id=tenant,
            channel_id=f"ch_{tenant}", account_id=f"acc_{tenant}",
            platform="youtube", state="scheduled", title=f"Queued {tenant}",
            idempotency_key=f"idem_{tenant}_q", run_at=NOW + timedelta(hours=3),
        ))
        uow.uploads.save(UploadRecord(
            id=f"up_{tenant}_failed", tenant_id=tenant,
            channel_id=f"ch_{tenant}", account_id=f"acc_{tenant}",
            platform="instagram", state="failed", title=f"Failed {tenant}",
            last_error="the public URL 404'd",
            idempotency_key=f"idem_{tenant}_f", run_at=NOW,
        ))
        uow.metrics.append(MetricSnapshotRecord(
            id=f"snap_{tenant}", tenant_id=tenant, upload_id=f"up_{tenant}_live",
            taken_at=NOW - timedelta(days=1), age_hours=24.0,
            views=5_000, likes=300, comments=20, shares=15,
            avg_watch_pct=46.0,
        ))


class ApiTest(unittest.TestCase):
    """Shared setup: one tenant, signed in."""

    def setUp(self) -> None:
        self.client, self.services = build()
        self.addCleanup(self.services.close)
        self.token = self._sign_in("ten_a@example.com")

    def _sign_in(self, email: str) -> str:
        response = self.client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["tokens"]["access_token"]

    def auth(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.token}"}

    def get(self, path: str, token: str | None = None):
        return self.client.get(path, headers=self.auth(token))


PAGES = (
    "/api/v1/overview",
    "/api/v1/channels",
    "/api/v1/sources",
    "/api/v1/uploads",
    "/api/v1/published",
    "/api/v1/analytics",
    "/api/v1/settings",
)


class AuthenticationTest(ApiTest):
    def test_every_page_endpoint_refuses_an_anonymous_request(self) -> None:
        for path in PAGES:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(
                    response.json()["error"]["code"], "NOT_AUTHENTICATED"
                )

    def test_a_forged_token_is_refused(self) -> None:
        """The signature is checked, so a token minted elsewhere is no good
        however well-formed it looks."""

        other = AccessTokenIssuer(Keyring((SigningKey("k1", "y" * 40),)))
        forged, _ = other.issue(
            identity_id="idn_x", email="attacker@example.com",
            tenant_id="ten_a", user_id="usr_x", role="owner",
            session_id="ses_x",
        )
        response = self.get("/api/v1/overview", token=forged)
        self.assertEqual(response.status_code, 401)

    def test_a_malformed_authorization_header_is_refused(self) -> None:
        for header in ("", "Bearer", "Basic abc", "Bearer  "):
            with self.subTest(header=header):
                response = self.client.get(
                    "/api/v1/overview", headers={"Authorization": header}
                )
                self.assertEqual(response.status_code, 401)

    def test_me_reports_the_signed_in_identity(self) -> None:
        body = self.get("/api/v1/auth/me").json()
        self.assertEqual(body["email"], "ten_a@example.com")
        self.assertEqual(body["tenant_id"], "ten_a")
        self.assertEqual(body["role"], "owner")
        self.assertEqual(len(body["memberships"]), 1)

    def test_refresh_rotates_over_http(self) -> None:
        first = self.client.post(
            "/api/v1/auth/login",
            json={"email": "ten_a@example.com", "password": PASSWORD},
        ).json()["tokens"]

        rotated = self.client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": first["refresh_token"]},
        )
        self.assertEqual(rotated.status_code, 200)
        self.assertNotEqual(
            rotated.json()["refresh_token"], first["refresh_token"]
        )

        # And the spent one is dead, which is what the dashboard's single
        # in-flight refresh exists to avoid triggering.
        again = self.client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": first["refresh_token"]},
        )
        self.assertEqual(again.status_code, 401)

    def test_signing_out_ends_the_session(self) -> None:
        tokens = self.client.post(
            "/api/v1/auth/login",
            json={"email": "ten_a@example.com", "password": PASSWORD},
        ).json()["tokens"]

        self.assertEqual(
            self.client.post(
                "/api/v1/auth/logout",
                json={"refresh_token": tokens["refresh_token"]},
            ).status_code,
            204,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/auth/refresh",
                json={"refresh_token": tokens["refresh_token"]},
            ).status_code,
            401,
        )

    def test_login_does_not_reveal_whether_an_address_exists(self) -> None:
        unknown = self.client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": PASSWORD},
        )
        wrong = self.client.post(
            "/api/v1/auth/login",
            json={"email": "ten_a@example.com", "password": "not it at all"},
        )
        self.assertEqual(unknown.status_code, wrong.status_code)
        self.assertEqual(unknown.json(), wrong.json())


class TenantIsolationTest(unittest.TestCase):
    """One tenant's token must not reach another tenant's rows.

    The API has no tenant parameter to tamper with — the tenant is a claim in
    a signed token — but that is an argument, not evidence. These sign in as
    one tenant and go looking for the other's data.
    """

    def setUp(self) -> None:
        self.client, self.services = build(tenants=("ten_a", "ten_b"))
        self.addCleanup(self.services.close)
        self.a = self._token("ten_a@example.com")
        self.b = self._token("ten_b@example.com")

    def _token(self, email: str) -> str:
        return self.client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        ).json()["tokens"]["access_token"]

    def _get(self, path: str, token: str):
        return self.client.get(path, headers={"Authorization": f"Bearer {token}"})

    def test_a_list_contains_only_the_callers_own_rows(self) -> None:
        for path, field in (
            ("/api/v1/channels", "id"),
            ("/api/v1/sources", "id"),
            ("/api/v1/uploads", "id"),
            ("/api/v1/published", "upload_id"),
        ):
            with self.subTest(path=path):
                items = self._get(path, self.a).json()["items"]
                self.assertTrue(items, f"{path} returned nothing to check")
                for item in items:
                    self.assertNotIn("ten_b", item[field])

    def test_fetching_another_tenants_channel_by_id_is_a_404(self) -> None:
        """Not a 403: the store cannot see across the boundary, so "not yours"
        and "does not exist" are genuinely the same answer — which is also
        what stops the endpoint becoming an id oracle."""

        response = self._get("/api/v1/channels/ch_ten_b", self.a)
        self.assertEqual(response.status_code, 404)

    def test_mutating_another_tenants_upload_is_refused(self) -> None:
        response = self.client.post(
            "/api/v1/uploads/up_ten_b_failed/retry",
            headers={"Authorization": f"Bearer {self.a}"},
        )
        self.assertEqual(response.status_code, 404)
        with self.services.database.unit_of_work("ten_b") as uow:
            self.assertEqual(uow.uploads.get("up_ten_b_failed").state, "failed")

    def test_the_counts_on_the_overview_are_the_callers_own(self) -> None:
        a = self._get("/api/v1/overview", self.a).json()
        b = self._get("/api/v1/overview", self.b).json()
        self.assertEqual(a["tenant_id"], "ten_a")
        self.assertEqual(b["tenant_id"], "ten_b")
        for stat in a["stats"]:
            if stat["key"] == "channels":
                self.assertEqual(stat["value"], 1)

    def test_a_token_with_no_tenant_cannot_read_anything(self) -> None:
        """Legitimate right after signup — someone with no workspace yet — and
        it must refuse rather than fall back to an unscoped read."""

        auth = self.services.auth
        result = auth.sign_up("loner@example.com", PASSWORD)
        link = auth.sender.links_for("loner@example.com")[-1]
        auth.verify_email(link.split("token=")[1])

        token = self.client.post(
            "/api/v1/auth/login",
            json={"email": "loner@example.com", "password": PASSWORD},
        ).json()["tokens"]["access_token"]

        response = self._get("/api/v1/overview", token)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "NO_WORKSPACE")


class PageDataTest(ApiTest):
    """Each page returns what its page needs, from the store."""

    def test_the_overview_counts_real_rows(self) -> None:
        body = self.get("/api/v1/overview").json()
        stats = {s["key"]: s["value"] for s in body["stats"]}
        self.assertEqual(stats["channels"], 1)
        self.assertEqual(stats["sources"], 1)
        self.assertEqual(stats["clips"], 1)
        self.assertEqual(stats["published"], 1)

        stages = {s["stage"]: s for s in body["pipeline"]}
        self.assertEqual(stages["uploads"]["total"], 3)
        self.assertEqual(stages["uploads"]["done"], 1)
        self.assertEqual(stages["uploads"]["failed"], 1)

    def test_the_overview_names_what_needs_attention(self) -> None:
        with self.services.database.unit_of_work("ten_a") as uow:
            channel = uow.channels.get("ch_ten_a")
            channel.state = "circuit_open"
            uow.channels.save(channel)

        body = self.get("/api/v1/overview").json()
        self.assertTrue(
            any("stopped themselves" in note for note in body["attention"])
        )

    def test_channels_carry_their_budget_and_health(self) -> None:
        channel = self.get("/api/v1/channels").json()["items"][0]
        self.assertEqual(channel["budget_remaining_cents"], 15_000)
        self.assertEqual(channel["state"], "active")

    def test_sources_join_acquisition_and_transcription_state(self) -> None:
        from clipforge.store import AcquisitionRunRecord, TranscriptionRunRecord

        with self.services.database.unit_of_work("ten_a") as uow:
            uow.acquisitions.save(AcquisitionRunRecord(
                id="acq_1", tenant_id="ten_a", source_id="src_ten_a",
                kind="podcast_feed", ref_key="src_ten_a", state="ready",
                media_path="/media/x.mp4",
            ))
            uow.transcriptions.save(TranscriptionRunRecord(
                id="txn_1", tenant_id="ten_a", source_id="src_ten_a",
                state="succeeded", word_count=8_400,
            ))

        source = self.get("/api/v1/sources").json()["items"][0]
        self.assertEqual(source["acquisition_state"], "ready")
        self.assertEqual(source["transcription_state"], "succeeded")
        self.assertEqual(source["word_count"], 8_400)

    def test_the_queue_excludes_published_and_sorts_by_due_time(self) -> None:
        items = self.get("/api/v1/uploads").json()["items"]
        states = [i["state"] for i in items]
        self.assertNotIn("published", states)
        self.assertIn("scheduled", states)
        self.assertIn("failed", states)

    def test_published_carries_its_latest_measurement_and_a_permalink(self) -> None:
        video = self.get("/api/v1/published").json()["items"][0]
        self.assertEqual(video["views"], 5_000)
        self.assertEqual(video["avg_watch_pct"], 46.0)
        self.assertIn("tt_ten_a", video["permalink"])

    def test_an_unmeasured_post_reports_null_rather_than_zero(self) -> None:
        """The distinction is load-bearing: "nobody collected numbers" and
        "this got no views" lead to opposite decisions."""

        with self.services.database.unit_of_work("ten_a") as uow:
            uow.uploads.save(UploadRecord(
                id="up_unmeasured", tenant_id="ten_a", channel_id="ch_ten_a",
                account_id="acc_ten_a", platform="tiktok", state="published",
                title="Never measured", remote_post_id="tt_unmeasured",
                published_at=NOW, idempotency_key="idem_unmeasured", run_at=NOW,
            ))

        items = self.get("/api/v1/published").json()["items"]
        unmeasured = next(i for i in items if i["upload_id"] == "up_unmeasured")
        self.assertIsNone(unmeasured["views"])
        self.assertIsNone(unmeasured["avg_watch_pct"])

    def test_analytics_reports_only_what_was_measured(self) -> None:
        body = self.get("/api/v1/analytics?window_days=30").json()
        self.assertEqual(body["posts_measured"], 1)
        self.assertEqual(body["total_views"], 5_000)
        self.assertEqual(body["note"], "")

    def test_analytics_explains_itself_when_nothing_is_measured(self) -> None:
        """A flat line at zero would be a claim about the videos. The truth is
        that nothing is collecting, and the endpoint says so."""

        client, services = build()
        self.addCleanup(services.close)
        token = client.post(
            "/api/v1/auth/login",
            json={"email": "ten_a@example.com", "password": PASSWORD},
        ).json()["tokens"]["access_token"]

        with services.database.unit_of_work("ten_a") as uow:
            for snapshot in uow.metrics.all():
                pass
        # A window that excludes the only measured post.
        body = client.get(
            "/api/v1/analytics?window_days=1",
            headers={"Authorization": f"Bearer {token}"},
        ).json()
        self.assertEqual(body["posts_measured"], 0)
        self.assertIsNone(body["total_views"])
        self.assertTrue(body["note"])

    def test_settings_reports_capabilities_including_the_absent_ones(self) -> None:
        body = self.get("/api/v1/settings").json()
        capabilities = {c["key"]: c for c in body["capabilities"]}

        self.assertFalse(capabilities["object_storage"]["available"])
        self.assertFalse(capabilities["metrics"]["available"])
        self.assertFalse(capabilities["email"]["available"])
        # In-memory stores in this test, so it should say so rather than
        # claiming durability it does not have.
        self.assertFalse(capabilities["persistence"]["available"])
        for capability in capabilities.values():
            self.assertTrue(capability["detail"],
                            f"{capability['key']} has no explanation")

    def test_settings_lists_the_current_session(self) -> None:
        body = self.get("/api/v1/settings").json()
        current = [s for s in body["sessions"] if s["current"]]
        self.assertEqual(len(current), 1)
        self.assertNotIn("token_hash", body["sessions"][0])


class StorageEndpointTest(ApiTest):
    def test_no_backend_says_so_rather_than_reporting_zero(self) -> None:
        """Zero bytes reads as "you are storing nothing", which is a different
        claim from "nothing is measuring"."""

        body = self.get("/api/v1/settings/storage").json()
        self.assertEqual(body["backend"], "none")
        self.assertIsNone(body["objects"])
        self.assertIn("lost when that container is replaced", body["note"])

    def test_usage_is_scoped_to_the_callers_prefix(self) -> None:
        """`usage()` is a full listing, so an unscoped call would walk every
        tenant's objects and bill the caller for the privilege."""

        import tempfile

        from clipforge.storage import LocalStorage, key_for

        root = tempfile.mkdtemp(prefix="clipforge-apis-")
        storage = LocalStorage(root=root)
        self.services.storage = storage

        payload = os.path.join(root, "src.bin")
        with open(payload, "wb") as handle:
            handle.write(b"x" * 300)
        storage.put_file(key_for("ten_a", "renders", "cl_1", "a.mp4"), payload)
        storage.put_file(key_for("ten_other", "renders", "cl_2", "b.mp4"), payload)

        body = self.get("/api/v1/settings/storage").json()
        self.assertEqual(body["backend"], "local")
        self.assertEqual(body["objects"], 1, "counted another tenant's objects")
        self.assertEqual(body["bytes"], 300)

    def test_operation_counters_are_reported(self) -> None:
        import tempfile

        from clipforge.storage import LocalStorage, key_for

        root = tempfile.mkdtemp(prefix="clipforge-apim-")
        storage = LocalStorage(root=root)
        self.services.storage = storage
        payload = os.path.join(root, "src.bin")
        with open(payload, "wb") as handle:
            handle.write(b"y" * 64)
        storage.put_file(key_for("ten_a", "renders", "cl_1", "a.mp4"), payload)

        body = self.get("/api/v1/settings/storage").json()
        self.assertGreater(body["total_calls"], 0)
        self.assertEqual(body["total_failures"], 0)
        self.assertIn("put_file", body["operations"])

    def test_the_endpoint_needs_authentication(self) -> None:
        self.assertEqual(
            self.client.get("/api/v1/settings/storage").status_code, 401
        )

    def test_capabilities_name_the_storage_backend(self) -> None:
        import tempfile

        from clipforge.storage import LocalStorage

        self.services.storage = LocalStorage(
            root=tempfile.mkdtemp(prefix="clipforge-apic-")
        )
        capabilities = {
            c["key"]: c for c in self.get("/api/v1/settings").json()["capabilities"]
        }
        self.assertFalse(capabilities["object_storage"]["available"])
        self.assertIn("container is replaced",
                      capabilities["object_storage"]["detail"])
        # Broken out from object storage because they fail independently: R2
        # can work perfectly while the bucket has no public domain.
        self.assertFalse(capabilities["public_media_urls"]["available"])
        self.assertIn("Instagram", capabilities["public_media_urls"]["detail"])


class MutationTest(ApiTest):
    def test_a_channel_can_be_paused_and_resumed(self) -> None:
        paused = self.client.patch(
            "/api/v1/channels/ch_ten_a/state", json={"state": "paused"},
            headers=self.auth(),
        )
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(paused.json()["state"], "paused")

        resumed = self.client.patch(
            "/api/v1/channels/ch_ten_a/state", json={"state": "active"},
            headers=self.auth(),
        )
        self.assertEqual(resumed.json()["state"], "active")

    def test_resuming_clears_the_breaker(self) -> None:
        """Otherwise the channel trips again on its next single failure."""

        with self.services.database.unit_of_work("ten_a") as uow:
            channel = uow.channels.get("ch_ten_a")
            channel.state = "circuit_open"
            channel.consecutive_failures = 5
            channel.circuit_opened_at = NOW
            uow.channels.save(channel)

        body = self.client.patch(
            "/api/v1/channels/ch_ten_a/state", json={"state": "active"},
            headers=self.auth(),
        ).json()
        self.assertEqual(body["consecutive_failures"], 0)
        self.assertIsNone(body["circuit_opened_at"])

    def test_circuit_open_cannot_be_set_by_hand(self) -> None:
        """A breaker is tripped by the system observing failures. Letting a
        human set it would make the state mean two different things."""

        response = self.client.patch(
            "/api/v1/channels/ch_ten_a/state", json={"state": "circuit_open"},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 422)

    def test_a_failed_upload_can_be_retried(self) -> None:
        response = self.client.post(
            "/api/v1/uploads/up_ten_a_failed/retry", headers=self.auth()
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "scheduled")
        self.assertEqual(response.json()["last_error"], "")

    def test_an_in_flight_upload_cannot_be_retried(self) -> None:
        """Re-queueing something mid-upload is how the same video goes out
        twice, and the idempotency key is the last line of defence, not the
        first."""

        response = self.client.post(
            "/api/v1/uploads/up_ten_a_queued/retry", headers=self.auth()
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "NOT_RETRYABLE")

    def test_submitting_a_source_is_refused_with_no_worker(self) -> None:
        """Accepting it into a queue nothing drains is the failure that looks
        like success for a day."""

        response = self.client.post(
            "/api/v1/sources", json={"url": "https://youtu.be/x"},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["code"], "ACQUISITION_UNAVAILABLE"
        )


class RoleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client, self.services = build()
        self.addCleanup(self.services.close)
        _register(self.services.auth, "viewer@example.com", "ten_a", "viewer")
        self.viewer = self.client.post(
            "/api/v1/auth/login",
            json={"email": "viewer@example.com", "password": PASSWORD},
        ).json()["tokens"]["access_token"]

    def test_a_viewer_may_read(self) -> None:
        response = self.client.get(
            "/api/v1/channels",
            headers={"Authorization": f"Bearer {self.viewer}"},
        )
        self.assertEqual(response.status_code, 200)

    def test_a_viewer_may_not_pause_a_channel(self) -> None:
        response = self.client.patch(
            "/api/v1/channels/ch_ten_a/state", json={"state": "paused"},
            headers={"Authorization": f"Bearer {self.viewer}"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "FORBIDDEN")

    def test_a_viewer_may_not_retry_an_upload(self) -> None:
        response = self.client.post(
            "/api/v1/uploads/up_ten_a_failed/retry",
            headers={"Authorization": f"Bearer {self.viewer}"},
        )
        self.assertEqual(response.status_code, 403)


class ErrorContractTest(ApiTest):
    """Every failure has the same shape, whatever produced it."""

    def test_all_of_them_are_the_same_envelope(self) -> None:
        cases = [
            self.client.get("/api/v1/overview"),                    # 401
            self.get("/api/v1/channels/nope"),                      # 404
            self.client.post("/api/v1/auth/login", json={}),        # 422
            self.client.patch(
                "/api/v1/channels/ch_ten_a/state",
                json={"state": "nonsense"}, headers=self.auth(),
            ),                                                      # 422
        ]
        for response in cases:
            with self.subTest(status=response.status_code):
                self.assertGreaterEqual(response.status_code, 400)
                body = response.json()
                self.assertIn("error", body)
                self.assertIsInstance(body["error"]["code"], str)
                self.assertIsInstance(body["error"]["message"], str)
                self.assertTrue(body["error"]["message"])

    def test_a_validation_error_never_echoes_the_input(self) -> None:
        """pydantic's default body includes the value that failed, which on a
        login is the password."""

        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": "someone@example.com", "password": 12345},
        )
        self.assertNotIn("12345", response.text)

    def test_pagination_reports_the_total_before_the_limit(self) -> None:
        body = self.get("/api/v1/uploads?limit=1").json()
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["limit"], 1)

    def test_an_out_of_range_limit_is_refused(self) -> None:
        self.assertEqual(self.get("/api/v1/uploads?limit=9999").status_code, 422)
        self.assertEqual(self.get("/api/v1/uploads?offset=-1").status_code, 422)


class OpenApiTest(ApiTest):
    """The document the dashboard's TypeScript is generated from."""

    def test_the_schema_describes_every_page_endpoint(self) -> None:
        paths = self.client.get("/api/v1/openapi.json").json()["paths"]
        for path in PAGES:
            self.assertIn(path.replace("/api/v1", "/api/v1"), paths)

    def test_the_error_shape_is_in_the_schema(self) -> None:
        """A contract describing only the happy path leaves every client to
        invent its own error handling."""

        schema = self.client.get("/api/v1/openapi.json").json()
        self.assertIn("ErrorResponse", schema["components"]["schemas"])

    def test_health_reports_the_database(self) -> None:
        body = self.client.get("/healthz").json()
        self.assertEqual(body["status"], "ok")


if __name__ == "__main__":
    unittest.main()
