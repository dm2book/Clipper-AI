"""OAuth, platform limits, upload state machines, and the worker loop."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

import _support  # noqa: F401  (path setup)

from clipforge.publish import (
    Account,
    ClientCredentials,
    InMemoryTokenStore,
    LIMITS,
    MediaAsset,
    Platform,
    PostSpec,
    PostState,
    PublishConfig,
    PublishingSystem,
    RecordingTransport,
    Response,
    ScheduleError,
    SealedTokenStore,
    TokenSet,
    Visibility,
    accounts_needing_attention,
    adapter_for,
    authorization_url,
    automation_gap,
    daily,
    effective_visibility,
    exchange_request,
    is_short_form,
    limits_for,
    make_pkce,
    monthly_on,
    parse_token_response,
    readiness,
    refresh_request,
    validate,
    weekdays_at,
)
from clipforge.publish.limits import tiktok_chunking, google_chunk_size
from clipforge.publish.oauth import long_lived_exchange_request

UTC = timezone.utc
NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
CREDS = ClientCredentials("client-abc", "secret-xyz",
                          "https://clipforge.test/cb")


def account(platform: Platform, **kwargs) -> Account:
    defaults = dict(
        account_id=f"{platform.value}-1", platform=platform, org_id="org1",
        external_id="ext-1", timezone="Europe/Amsterdam",
        direct_post_approved=True, business_account=True,
    )
    defaults.update(kwargs)
    return Account(**defaults)


def tokens_for(account_id: str, platform: Platform, days: int = 3650) -> TokenSet:
    return TokenSet(
        account_id=account_id, platform=platform,
        access_token="at", refresh_token="rt",
        expires_at=NOW + timedelta(hours=1),
        refresh_valid_until=NOW + timedelta(days=days),
        obtained_at=NOW,
    )


def spec(public_url: bool = True, **kwargs) -> PostSpec:
    defaults = dict(
        asset=MediaAsset(
            "clip-1", path="/renders/clip-1.mp4",
            public_url="https://cdn.test/clip-1.mp4" if public_url else "",
            size_bytes=18 * 1024**2, duration_s=28.0,
        ),
        title="The raise was the mistake",
        caption="What I have never told anyone",
    )
    defaults.update(kwargs)
    return PostSpec(**defaults)


class TestOAuthFlows(unittest.TestCase):
    def test_pkce_challenge_is_the_sha256_of_the_verifier(self):
        import base64
        import hashlib

        pkce = make_pkce()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(pkce.verifier.encode()).digest()
        ).rstrip(b"=").decode()
        self.assertEqual(pkce.challenge, expected)
        self.assertEqual(pkce.method, "S256")

    def test_pkce_is_fresh_each_time(self):
        self.assertNotEqual(make_pkce().verifier, make_pkce().verifier)

    def test_tiktok_uses_client_key_not_client_id(self):
        # Every other provider uses client_id; TikTok does not, and copying
        # the wrong one fails with an unhelpful error.
        url = authorization_url(Platform.TIKTOK, CREDS).url
        self.assertIn("client_key=client-abc", url)
        self.assertNotIn("client_id=", url)

    def test_google_asks_for_offline_access_and_forces_consent(self):
        # Without both, a re-authorisation returns no refresh token and the
        # connection dies an hour later with nothing to renew from.
        url = authorization_url(Platform.YOUTUBE, CREDS).url
        self.assertIn("access_type=offline", url)
        self.assertIn("prompt=consent", url)

    def test_every_platform_sends_pkce_and_state(self):
        for platform in Platform:
            auth = authorization_url(platform, CREDS)
            self.assertIn("code_challenge=", auth.url)
            self.assertIn("code_challenge_method=S256", auth.url)
            self.assertIn(auth.state, auth.url)

    def test_default_scopes_come_from_the_limits_table(self):
        url = authorization_url(Platform.YOUTUBE, CREDS).url
        self.assertIn("youtube.upload", url)

    def test_exchange_carries_the_verifier_and_secret(self):
        request = exchange_request(Platform.YOUTUBE, CREDS, "code-1", "verifier-1")
        self.assertEqual(request.form_body["code_verifier"], "verifier-1")
        self.assertEqual(request.form_body["grant_type"], "authorization_code")

    def test_instagram_refresh_is_a_re_exchange_not_a_refresh_grant(self):
        # Facebook has no refresh grant; a long-lived token is traded for a
        # new long-lived token.
        request = refresh_request(Platform.INSTAGRAM, CREDS, "llt")
        self.assertEqual(request.form_body["grant_type"], "fb_exchange_token")
        self.assertEqual(request.form_body["fb_exchange_token"], "llt")

    def test_long_lived_exchange_exists_and_is_distinct(self):
        request = long_lived_exchange_request(CREDS, "short")
        self.assertIn("fb_exchange_token", request.form_body)

    def test_secrets_are_redacted_from_a_request_log(self):
        request = exchange_request(Platform.TIKTOK, CREDS, "code", "verifier")
        redacted = request.redacted()
        self.assertEqual(redacted["body"]["client_secret"], "<redacted>")
        self.assertEqual(redacted["body"]["code_verifier"], "<redacted>")
        self.assertNotIn("secret-xyz", json.dumps(redacted))

    def test_bearer_headers_are_redacted(self):
        from clipforge.publish.types import Request

        request = Request("GET", "https://x", {"Authorization": "Bearer hunter2"})
        self.assertNotIn("hunter2", json.dumps(request.redacted()))


class TestTokenLifecycle(unittest.TestCase):
    def test_parses_a_flat_token_response(self):
        tokens = parse_token_response(
            Platform.YOUTUBE, "a1",
            Response(200, {}, {"access_token": "at", "refresh_token": "rt",
                               "expires_in": 3600, "scope": "a b"}),
            now=NOW,
        )
        self.assertEqual(tokens.access_token, "at")
        self.assertEqual(tokens.scopes, ("a", "b"))
        self.assertEqual(tokens.expires_at, NOW + timedelta(seconds=3600))

    def test_parses_tiktoks_nested_envelope(self):
        tokens = parse_token_response(
            Platform.TIKTOK, "a1",
            Response(200, {}, {"data": {"access_token": "at",
                                        "refresh_token": "rt",
                                        "expires_in": 86400}}),
            now=NOW,
        )
        self.assertEqual(tokens.access_token, "at")

    def test_instagram_token_doubles_as_its_own_refresh_token(self):
        tokens = parse_token_response(
            Platform.INSTAGRAM, "a1",
            Response(200, {}, {"access_token": "llt", "expires_in": 5183944}),
            now=NOW,
        )
        self.assertEqual(tokens.refresh_token, "llt")

    def test_a_response_with_no_token_raises(self):
        with self.assertRaises(ValueError):
            parse_token_response(Platform.YOUTUBE, "a1",
                                 Response(200, {}, {"error": "nope"}))

    def test_needs_refresh_fires_before_expiry(self):
        tokens = tokens_for("a1", Platform.YOUTUBE)
        self.assertFalse(tokens.needs_refresh(NOW))
        self.assertTrue(tokens.needs_refresh(NOW + timedelta(minutes=56)))

    def test_covers_answers_the_long_dated_question(self):
        tokens = tokens_for("a1", Platform.INSTAGRAM, days=60)
        self.assertTrue(tokens.covers(NOW + timedelta(days=30)))
        self.assertFalse(tokens.covers(NOW + timedelta(days=90)))

    def test_horizon_warning_names_the_deadline(self):
        tokens = tokens_for("a1", Platform.INSTAGRAM, days=60)
        warning = tokens.horizon_warning(NOW + timedelta(days=90))
        self.assertIn("2026-10-31", warning)
        self.assertIn("reconnected", warning)

    def test_a_post_close_to_the_deadline_also_warns(self):
        tokens = tokens_for("a1", Platform.INSTAGRAM, days=60)
        self.assertTrue(tokens.horizon_warning(NOW + timedelta(days=52)))
        self.assertFalse(tokens.horizon_warning(NOW + timedelta(days=10)))

    def test_serialised_tokens_never_contain_the_secrets(self):
        tokens = TokenSet(
            account_id="a1", platform=Platform.TIKTOK,
            access_token="SECRET-ACCESS", refresh_token="SECRET-REFRESH",
            expires_at=NOW, refresh_valid_until=NOW, obtained_at=NOW,
        )
        payload = json.dumps(tokens.to_dict())
        self.assertNotIn("SECRET-ACCESS", payload)
        self.assertNotIn("SECRET-REFRESH", payload)
        # But the caller can still tell whether a refresh path exists.
        self.assertTrue(tokens.to_dict()["has_refresh_token"])

    def test_sealed_store_round_trips_through_a_cipher(self):
        store = SealedTokenStore(
            InMemoryTokenStore(),
            seal=lambda v: f"enc({v})",
            unseal=lambda v: v[4:-1],
        )
        store.put(tokens_for("a1", Platform.YOUTUBE))
        self.assertEqual(store.get("a1").access_token, "at")
        self.assertEqual(store.get("a1").refresh_token, "rt")

    def test_sealed_store_holds_ciphertext_underneath(self):
        inner = InMemoryTokenStore()
        store = SealedTokenStore(inner, lambda v: f"enc({v})", lambda v: v[4:-1])
        store.put(tokens_for("a1", Platform.YOUTUBE))
        self.assertEqual(inner.get("a1").access_token, "enc(at)")

    def test_accounts_needing_attention_finds_short_horizons(self):
        store = InMemoryTokenStore()
        store.put(tokens_for("ig", Platform.INSTAGRAM, days=60))
        store.put(tokens_for("yt", Platform.YOUTUBE, days=3650))
        problems = accounts_needing_attention(
            store, NOW + timedelta(days=120), now=NOW
        )
        self.assertEqual([a for a, _ in problems], ["ig"])


class TestLimits(unittest.TestCase):
    def test_every_platform_has_limits(self):
        for platform in Platform:
            self.assertIn(platform, LIMITS)

    def test_only_youtube_schedules_server_side(self):
        scheduling = {
            p for p in Platform if limits_for(p).tokens.server_side_scheduling
        }
        self.assertEqual(scheduling, {Platform.YOUTUBE})

    def test_youtube_quota_is_project_scoped(self):
        rate = limits_for(Platform.YOUTUBE).rate
        self.assertEqual(rate.quota_scope, "project")
        self.assertEqual(rate.daily_budget // rate.upload_cost, rate.posts_per_day)

    def test_instagram_has_the_shortest_credential_horizon(self):
        horizons = {
            p: limits_for(p).tokens.refresh_grace_days for p in Platform
        }
        self.assertEqual(min(horizons, key=horizons.get), Platform.INSTAGRAM)

    def test_unaudited_tiktok_is_blocked(self):
        self.assertTrue(automation_gap(
            account(Platform.TIKTOK, direct_post_approved=False)))
        self.assertFalse(automation_gap(account(Platform.TIKTOK)))

    def test_personal_instagram_is_blocked(self):
        gap = automation_gap(account(Platform.INSTAGRAM, business_account=False))
        self.assertIn("Business or Creator", gap)

    def test_a_blocked_account_reports_the_visibility_it_will_really_get(self):
        blocked = account(Platform.TIKTOK, direct_post_approved=False)
        self.assertIs(
            effective_visibility(blocked, Visibility.PUBLIC), Visibility.PRIVATE
        )
        self.assertIs(
            effective_visibility(account(Platform.TIKTOK), Visibility.PUBLIC),
            Visibility.PUBLIC,
        )

    def test_readiness_names_the_degradation(self):
        report = readiness(account(Platform.TIKTOK, direct_post_approved=False))
        self.assertFalse(report.automated)
        self.assertEqual(report.degraded_to, "draft in creator inbox")

    def test_validation_catches_an_over_long_caption(self):
        problems = validate(
            spec(caption="x" * 3000), account(Platform.TIKTOK)
        )
        self.assertTrue(any("caption" in p for p in problems))

    def test_validation_catches_an_over_long_title(self):
        problems = validate(spec(title="x" * 200), account(Platform.YOUTUBE))
        self.assertTrue(any("title" in p for p in problems))

    def test_validation_requires_a_youtube_title(self):
        problems = validate(spec(title=""), account(Platform.YOUTUBE))
        self.assertTrue(any("title" in p for p in problems))

    def test_validation_requires_a_public_url_for_instagram(self):
        problems = validate(spec(public_url=False), account(Platform.INSTAGRAM))
        self.assertTrue(any("public_url" in p for p in problems))

    def test_validation_catches_too_many_hashtags(self):
        problems = validate(
            spec(hashtags=tuple(f"t{i}" for i in range(40))),
            account(Platform.INSTAGRAM),
        )
        self.assertTrue(any("hashtags" in p for p in problems))

    def test_validation_catches_a_too_short_clip(self):
        short = MediaAsset("c", path="/c.mp4", public_url="https://x/c.mp4",
                           size_bytes=1024, duration_s=1.0)
        problems = validate(PostSpec(asset=short, title="t"),
                            account(Platform.TIKTOK))
        self.assertTrue(any("minimum" in p for p in problems))

    def test_a_valid_post_has_no_problems(self):
        for platform in Platform:
            self.assertEqual(validate(spec(), account(platform)), [])

    def test_short_form_is_inferred_from_the_file(self):
        vertical = MediaAsset("c", width=1080, height=1920, duration_s=28.0)
        long_form = MediaAsset("c", width=1080, height=1920, duration_s=600.0)
        self.assertTrue(is_short_form(vertical, Platform.YOUTUBE))
        self.assertFalse(is_short_form(long_form, Platform.YOUTUBE))

    def test_tiktok_small_file_is_a_single_chunk(self):
        chunk, count = tiktok_chunking(2 * 1024**2)
        self.assertEqual(count, 1)
        self.assertEqual(chunk, 2 * 1024**2)

    def test_tiktok_chunk_count_never_exceeds_the_cap(self):
        for size in (10 * 1024**2, 500 * 1024**2, 4 * 1024**3):
            chunk, count = tiktok_chunking(size)
            self.assertLessEqual(count, 1000)
            self.assertGreaterEqual(chunk, 5 * 1024**2)

    def test_google_chunk_is_a_multiple_of_256k(self):
        for preferred in (1, 300_000, 8 * 1024**2, 9_999_999):
            self.assertEqual(google_chunk_size(preferred) % (256 * 1024), 0)


class TestAdapters(unittest.TestCase):
    def drive(self, platform: Platform, responses: list[Response],
              **account_kwargs):
        system = PublishingSystem()
        acct = account(platform, **account_kwargs)
        system.connect(acct, tokens_for(acct.account_id, platform))
        post = system.schedule(acct.account_id, spec(), NOW + timedelta(days=1))
        transport = RecordingTransport(responses)
        result = system.run_post(post, transport, now=NOW + timedelta(days=1))
        return result, transport, post

    def test_youtube_resumable_happy_path(self):
        result, transport, _ = self.drive(Platform.YOUTUBE, [
            Response(200, {"Location": "https://upload/session"}),
            Response(308, {"Range": "bytes=0-8388607"}),
            Response(308, {"Range": "bytes=0-16777215"}),
            Response(200, {}, {"id": "vid_1"}),
        ])
        self.assertIs(result.state, PostState.PUBLISHED)
        self.assertEqual(result.remote_post_id, "vid_1")
        self.assertEqual(transport.sent[0].method, "POST")
        self.assertTrue(all(r.method == "PUT" for r in transport.sent[1:]))

    def test_youtube_uses_the_servers_byte_count_not_its_own(self):
        # The 308's Range header is authoritative. Trusting the local offset
        # after a partial write corrupts the upload.
        result, transport, _ = self.drive(Platform.YOUTUBE, [
            Response(200, {"Location": "https://upload/session"}),
            Response(308, {"Range": "bytes=0-4194303"}),   # only half landed
            Response(308, {"Range": "bytes=0-12582911"}),
            Response(308, {"Range": "bytes=0-16777215"}),
            Response(200, {}, {"id": "vid_1"}),
        ])
        self.assertIs(result.state, PostState.PUBLISHED)
        second_chunk = transport.sent[2]
        self.assertEqual(second_chunk.byte_range[0], 4194304)

    def test_youtube_sets_publish_at_for_a_future_public_post(self):
        system = PublishingSystem()
        acct = account(Platform.YOUTUBE)
        system.connect(acct, tokens_for(acct.account_id, Platform.YOUTUBE))
        post = system.schedule(acct.account_id, spec(), NOW + timedelta(days=30))

        adapter = adapter_for(Platform.YOUTUBE)
        step = adapter.begin(post.spec, acct,
                             tokens_for(acct.account_id, Platform.YOUTUBE),
                             post.run_at, post.idempotency_key)
        status = step.request.json_body["status"]
        self.assertIn("publishAt", status)
        # publishAt is only honoured on a private video.
        self.assertEqual(status["privacyStatus"], "private")

    def test_youtube_reconcile_is_a_question_not_a_write(self):
        adapter = adapter_for(Platform.YOUTUBE)
        request = adapter.reconcile(
            {"session_uri": "https://upload/s", "size": 100},
            tokens_for("a", Platform.YOUTUBE),
        )
        self.assertEqual(request.headers["Content-Range"], "bytes */100")
        self.assertIsNone(request.byte_range)

    def test_tiktok_direct_post_happy_path(self):
        result, transport, _ = self.drive(Platform.TIKTOK, [
            Response(200, {}, {"data": {"publish_id": "pub_1",
                                        "upload_url": "https://up/x"}}),
            Response(200), Response(200), Response(200),
            Response(200, {}, {"data": {"status": "PROCESSING_UPLOAD"}}),
            Response(200, {}, {"data": {"status": "PUBLISH_COMPLETE",
                                        "publicaly_available_post_id": ["v_9"]}}),
        ])
        self.assertIs(result.state, PostState.PUBLISHED)
        self.assertEqual(result.remote_post_id, "v_9")
        self.assertIn("direct post", transport.sent[0].description)

    def test_tiktok_last_chunk_absorbs_the_remainder(self):
        # An exact ceil-division split leaves an undersized final chunk, which
        # TikTok rejects in a multi-chunk upload.
        _, transport, _ = self.drive(Platform.TIKTOK, [
            Response(200, {}, {"data": {"publish_id": "p",
                                        "upload_url": "https://up/x"}}),
            Response(200), Response(200), Response(200),
            Response(200, {}, {"data": {"status": "PUBLISH_COMPLETE"}}),
        ])
        chunks = [r for r in transport.sent if r.byte_range]
        first_size = chunks[0].byte_range[1] - chunks[0].byte_range[0] + 1
        last_size = chunks[-1].byte_range[1] - chunks[-1].byte_range[0] + 1
        self.assertGreaterEqual(last_size, first_size)
        self.assertEqual(chunks[-1].byte_range[1], 18 * 1024**2 - 1)

    def test_tiktok_chunks_are_contiguous_and_cover_the_file(self):
        _, transport, _ = self.drive(Platform.TIKTOK, [
            Response(200, {}, {"data": {"publish_id": "p",
                                        "upload_url": "https://up/x"}}),
            Response(200), Response(200), Response(200),
            Response(200, {}, {"data": {"status": "PUBLISH_COMPLETE"}}),
        ])
        chunks = [r.byte_range for r in transport.sent if r.byte_range]
        self.assertEqual(chunks[0][0], 0)
        for first, second in zip(chunks, chunks[1:]):
            self.assertEqual(second[0], first[1] + 1)

    def test_unaudited_tiktok_goes_to_the_inbox_and_is_not_published(self):
        result, transport, post = self.drive(
            Platform.TIKTOK,
            [
                Response(200, {}, {"data": {"publish_id": "p",
                                            "upload_url": "https://up/x"}}),
                Response(200), Response(200), Response(200),
                Response(200, {}, {"data": {"status": "SEND_TO_USER_INBOX"}}),
            ],
            direct_post_approved=False,
        )
        # Delivered, but not live. Calling this "published" would put a green
        # tick next to something nobody can watch.
        self.assertIs(result.state, PostState.AWAITING_CREATOR)
        self.assertTrue(result.draft)
        self.assertFalse(result.published)
        self.assertTrue(result.delivered)
        self.assertIn("inbox", transport.sent[0].description)

    def test_tiktok_rejection_after_upload_is_an_error(self):
        result, _, _ = self.drive(Platform.TIKTOK, [
            Response(200, {}, {"data": {"publish_id": "p",
                                        "upload_url": "https://up/x"}}),
            Response(200), Response(200), Response(200),
            Response(200, {}, {"data": {"status": "FAILED",
                                        "fail_reason": "invalid_file_upload"}}),
        ])
        self.assertIs(result.state, PostState.FAILED)

    def test_instagram_container_then_publish(self):
        result, transport, _ = self.drive(Platform.INSTAGRAM, [
            Response(200, {}, {"id": "cont_1"}),
            Response(200, {}, {"status_code": "IN_PROGRESS"}),
            Response(200, {}, {"status_code": "FINISHED"}),
            Response(200, {}, {"id": "media_1"}),
        ])
        self.assertIs(result.state, PostState.PUBLISHED)
        self.assertEqual(result.remote_post_id, "media_1")
        self.assertIn("media_publish", transport.sent[-1].url)

    def test_instagram_never_uploads_bytes(self):
        _, transport, _ = self.drive(Platform.INSTAGRAM, [
            Response(200, {}, {"id": "cont_1"}),
            Response(200, {}, {"status_code": "FINISHED"}),
            Response(200, {}, {"id": "media_1"}),
        ])
        self.assertTrue(all(r.byte_range is None for r in transport.sent))
        self.assertIn("video_url", transport.sent[0].form_body)

    def test_instagram_container_error_fails_the_post(self):
        result, _, _ = self.drive(Platform.INSTAGRAM, [
            Response(200, {}, {"id": "cont_1"}),
            Response(200, {}, {"status_code": "ERROR",
                               "status": "unsupported_format"}),
        ])
        self.assertIs(result.state, PostState.FAILED)
        self.assertEqual(result.disposition, "fail")

    def test_instagram_reconcile_lists_recent_media(self):
        adapter = adapter_for(Platform.INSTAGRAM)
        request = adapter.reconcile({"ig_user_id": "17841"},
                                    tokens_for("a", Platform.INSTAGRAM))
        self.assertEqual(request.method, "GET")
        self.assertIn("/media", request.url)


class TestScheduling(unittest.TestCase):
    def setUp(self):
        self.system = PublishingSystem(timezone="Europe/Amsterdam")
        for platform in Platform:
            acct = account(platform)
            self.system.connect(
                acct,
                tokens_for(acct.account_id, platform,
                           days=limits_for(platform).tokens.refresh_grace_days),
            )

    def test_schedules_a_post(self):
        post = self.system.schedule("youtube-1", spec(), NOW + timedelta(days=7))
        self.assertIs(post.state, PostState.SCHEDULED)
        self.assertEqual(len(self.system.calendar), 1)

    def test_run_at_is_normalised_to_utc(self):
        from zoneinfo import ZoneInfo

        local = datetime(2026, 10, 5, 17, 0, tzinfo=ZoneInfo("Europe/Amsterdam"))
        post = self.system.schedule("youtube-1", spec(), local)
        self.assertEqual(post.run_at.tzinfo, UTC)
        self.assertEqual(post.run_at.hour, 15)

    def test_naive_datetimes_are_rejected(self):
        with self.assertRaises(ValueError):
            self.system.schedule("youtube-1", spec(),
                                 datetime(2026, 10, 5, 17, 0))

    def test_the_past_is_rejected(self):
        with self.assertRaises(ScheduleError):
            self.system.schedule("youtube-1", spec(),
                                 datetime(2020, 1, 1, tzinfo=UTC))

    def test_validation_runs_at_schedule_time_not_at_post_time(self):
        with self.assertRaises(ScheduleError) as caught:
            self.system.schedule("youtube-1", spec(title="x" * 300),
                                 NOW + timedelta(days=7))
        self.assertTrue(any("title" in p for p in caught.exception.problems))

    def test_every_problem_is_reported_at_once(self):
        with self.assertRaises(ScheduleError) as caught:
            self.system.schedule(
                "youtube-1", spec(title="", caption="x" * 9000),
                datetime(2020, 1, 1, tzinfo=UTC),
            )
        self.assertGreaterEqual(len(caught.exception.problems), 3)

    def test_spacing_is_enforced(self):
        self.system.schedule("youtube-1", spec(), NOW + timedelta(days=7))
        with self.assertRaises(ScheduleError):
            self.system.schedule("youtube-1", spec(),
                                 NOW + timedelta(days=7, minutes=30))

    def test_spacing_can_be_forced(self):
        self.system.schedule("youtube-1", spec(), NOW + timedelta(days=7))
        forced = self.system.schedule(
            "youtube-1", spec(), NOW + timedelta(days=7, minutes=30), force=True
        )
        self.assertIsNotNone(forced)
        self.assertTrue(self.system.calendar.conflicts())

    def test_a_post_past_the_credential_horizon_is_refused(self):
        # The headline case for "months ahead": an Instagram token is good for
        # sixty days, so a ninety-day post would fail silently at run time.
        with self.assertRaises(ScheduleError) as caught:
            self.system.schedule("instagram-1", spec(),
                                 NOW + timedelta(days=200))
        self.assertTrue(
            any("renewable" in p for p in caught.exception.problems)
        )

    def test_youtube_can_be_scheduled_far_ahead(self):
        post = self.system.schedule("youtube-1", spec(),
                                    NOW + timedelta(days=300))
        self.assertIs(post.state, PostState.SCHEDULED)

    def test_unknown_account(self):
        with self.assertRaises(KeyError):
            self.system.schedule("nope", spec(), NOW + timedelta(days=1))

    def test_idempotency_key_is_derived_and_stable(self):
        first = self.system.schedule("youtube-1", spec(), NOW + timedelta(days=7))
        self.system.cancel(first.post_id)
        second = self.system.schedule("youtube-1", spec(), NOW + timedelta(days=7),
                                      force=True)
        self.assertEqual(first.idempotency_key, second.idempotency_key)

    def test_different_slots_get_different_keys(self):
        first = self.system.schedule("youtube-1", spec(), NOW + timedelta(days=7))
        second = self.system.schedule("youtube-1", spec(), NOW + timedelta(days=9))
        self.assertNotEqual(first.idempotency_key, second.idempotency_key)

    def test_bulk_places_clips_onto_a_rule(self):
        specs = [spec(asset=MediaAsset(f"c{i}", path=f"/c{i}.mp4",
                                       public_url=f"https://x/c{i}.mp4",
                                       size_bytes=1024, duration_s=20.0))
                 for i in range(10)]
        placed, rejected = self.system.schedule_bulk(
            "youtube-1", specs, weekdays_at(17, 0, "Europe/Amsterdam"),
            start=NOW,
        )
        self.assertEqual(len(placed), 10)
        self.assertEqual(rejected, [])
        self.assertEqual(len({p.run_at for p in placed}), 10)

    def test_bulk_reports_what_it_could_not_place(self):
        bad = spec(title="")   # YouTube requires a title
        placed, rejected = self.system.schedule_bulk(
            "youtube-1", [bad], weekdays_at(17, 0, "UTC"), start=NOW,
        )
        self.assertEqual(placed, [])
        self.assertEqual(len(rejected), 1)

    def test_bulk_shares_one_series_id(self):
        specs = [spec(asset=MediaAsset(f"c{i}", path=f"/c{i}.mp4",
                                       size_bytes=1024, duration_s=20.0))
                 for i in range(3)]
        placed, _ = self.system.schedule_bulk(
            "youtube-1", specs, weekdays_at(17, 0, "UTC"), start=NOW)
        self.assertEqual(len({p.series_id for p in placed}), 1)

    def test_series_reports_refusals_rather_than_swallowing_them(self):
        # Asking for six months of Instagram posts against a sixty-day token
        # must not silently return two.
        placed, refused = self.system.schedule_series(
            "instagram-1", spec(), monthly_on([1], 9, 0, "UTC"),
            start=NOW, horizon_days=180,
        )
        self.assertTrue(refused)
        self.assertTrue(any("renewable" in r for r in refused))
        self.assertLess(len(placed), 6)

    def test_cancel(self):
        post = self.system.schedule("youtube-1", spec(), NOW + timedelta(days=7))
        self.assertTrue(self.system.cancel(post.post_id))
        self.assertIs(post.state, PostState.CANCELLED)
        self.assertFalse(self.system.cancel(post.post_id))

    def test_cancel_a_whole_series(self):
        specs = [spec(asset=MediaAsset(f"c{i}", path=f"/c{i}.mp4",
                                       size_bytes=1024, duration_s=20.0))
                 for i in range(5)]
        placed, _ = self.system.schedule_bulk(
            "youtube-1", specs, weekdays_at(17, 0, "UTC"), start=NOW)
        self.assertEqual(self.system.cancel_series(placed[0].series_id), 5)

    def test_reschedule(self):
        post = self.system.schedule("youtube-1", spec(), NOW + timedelta(days=7))
        self.system.reschedule(post.post_id, NOW + timedelta(days=14))
        self.assertEqual(post.run_at, NOW + timedelta(days=14))

    def test_extend_a_series_forward(self):
        placed, _ = self.system.schedule_series(
            "youtube-1", spec(), daily(9, 0, "UTC"),
            start=NOW, horizon_days=10,
        )
        more, _ = self.system.extend_series(
            placed[0].series_id, "youtube-1", spec(), NOW + timedelta(days=20)
        )
        self.assertTrue(more)
        self.assertGreater(min(p.run_at for p in more),
                           max(p.run_at for p in placed))


class TestWorkerLoop(unittest.TestCase):
    def setUp(self):
        self.system = PublishingSystem()
        acct = account(Platform.YOUTUBE)
        self.system.connect(acct, tokens_for(acct.account_id, Platform.YOUTUBE))
        self.post = self.system.schedule("youtube-1", spec(),
                                         NOW + timedelta(days=1))
        self.later = NOW + timedelta(days=1, minutes=1)

    def test_claim_takes_a_lease(self):
        claimed = self.system.claim(self.later)
        self.assertEqual(len(claimed), 1)
        self.assertIsNotNone(claimed[0].lease_until)
        self.assertIs(claimed[0].state, PostState.CLAIMED)

    def test_a_leased_post_is_not_claimed_twice(self):
        self.system.claim(self.later)
        self.post.state = PostState.SCHEDULED   # a second worker looks again
        self.assertEqual(self.system.claim(self.later), [])

    def test_a_lapsed_lease_becomes_claimable_again(self):
        # A worker killed mid-post must not hold the job forever.
        self.system.claim(self.later)
        self.post.state = PostState.SCHEDULED
        self.assertEqual(len(self.system.claim(self.later + timedelta(hours=2))), 1)

    def test_a_future_post_is_not_claimed(self):
        self.assertEqual(self.system.claim(NOW), [])

    def test_transient_failure_schedules_a_retry(self):
        transport = RecordingTransport([Response(503)])
        result = self.system.run_post(self.post, transport, now=self.later)
        self.assertIs(result.state, PostState.RETRYING)
        self.assertIsNotNone(self.post.next_attempt_at)
        self.assertGreater(self.post.next_attempt_at, self.later)

    def test_a_retry_releases_the_lease(self):
        self.system.claim(self.later)
        self.system.run_post(self.post,
                             RecordingTransport([Response(503)]), now=self.later)
        self.assertIsNone(self.post.lease_until)

    def test_dead_credentials_escalate_rather_than_retry(self):
        transport = RecordingTransport([
            Response(401, {}, {"error": {"code": "access_token_invalid"}})
        ])
        result = self.system.run_post(self.post, transport, now=self.later)
        self.assertIs(result.state, PostState.NEEDS_ATTENTION)
        self.assertEqual(self.post.attempt_count, 1)
        self.assertIn(self.post, self.system.needs_attention())

    def test_permanent_rejection_fails_immediately(self):
        transport = RecordingTransport([
            Response(400, {}, {"error": {"code": "invalidDescription"}})
        ])
        result = self.system.run_post(self.post, transport, now=self.later)
        self.assertIs(result.state, PostState.FAILED)

    def test_an_ambiguous_failure_reconciles_instead_of_retrying(self):
        # A 500 *after* the session was opened may mean the post exists.
        transport = RecordingTransport([
            Response(200, {"Location": "https://upload/session"}),
            Response(500),
        ])
        result = self.system.run_post(self.post, transport, now=self.later)
        self.assertEqual(result.disposition, "reconcile")

    def test_attempts_accumulate_across_retries(self):
        for _ in range(3):
            self.post.state = PostState.SCHEDULED
            self.system.run_post(self.post, RecordingTransport([Response(503)]),
                                 now=self.later)
        self.assertEqual(self.post.attempt_count, 3)
        self.assertEqual(len(self.post.to_dict()["attempts"]), 3)

    def test_retries_are_eventually_given_up_on(self):
        from clipforge.publish.retry import MAX_ATTEMPTS

        for _ in range(MAX_ATTEMPTS + 1):
            self.post.state = PostState.SCHEDULED
            result = self.system.run_post(
                self.post, RecordingTransport([Response(503)]), now=self.later
            )
        self.assertIs(result.state, PostState.FAILED)
        self.assertIn("gave up", self.post.last_error)

    def test_missing_credentials_escalate(self):
        self.system.tokens.delete("youtube-1")
        result = self.system.run_post(
            self.post, RecordingTransport([]), now=self.later
        )
        self.assertIs(result.state, PostState.NEEDS_ATTENTION)

    def test_tick_runs_everything_due(self):
        transport = RecordingTransport([
            Response(200, {"Location": "https://upload/s"}),
            Response(308, {"Range": "bytes=0-8388607"}),
            Response(308, {"Range": "bytes=0-16777215"}),
            Response(200, {}, {"id": "vid"}),
        ])
        results = self.system.tick(transport, now=self.later)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].published)

    def test_status_summarises_the_system(self):
        status = self.system.status()
        self.assertEqual(status["accounts"], 1)
        self.assertEqual(status["total"], 1)
        self.assertIn("automation", status)

    def test_json_round_trip(self):
        payload = json.loads(json.dumps(self.system.status()))
        self.assertIn("by_state", payload)
        post_payload = json.loads(json.dumps(self.post.to_dict()))
        self.assertEqual(post_payload["post_id"], self.post.post_id)


if __name__ == "__main__":
    unittest.main()
