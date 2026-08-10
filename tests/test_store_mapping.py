"""Round trips through the mappers, field by field.

Not a couple of examples. Every mapper here is checked against the *complete*
field list of the dataclass it converts, so a field added to either side
without a mapping fails a test rather than silently vanishing at the next
restart — which is the failure this whole layer exists to prevent, and the one
that is invisible until someone notices a caption is missing in production.

`test_every_field_is_accounted_for` is the one that does the enforcing. The
others explain what the interesting fields are for.
"""

from __future__ import annotations

import dataclasses
import unittest
from datetime import UTC, datetime, timedelta

from clipforge.factory.channel import Budget, Channel, ChannelHealth, ChannelState
from clipforge.factory.niches import Niche
from clipforge.factory.sources import Rights, RightsBasis, Source, SourceKind
from clipforge.publish.oauth import TokenSet
from clipforge.publish.types import (
    Account,
    Attempt,
    MediaAsset,
    Platform,
    PostSpec,
    PostState,
    ScheduledPost,
    Visibility,
)
from clipforge.store.mappers import (
    apply_tokens,
    to_account,
    to_account_record,
    to_channel,
    to_channel_record,
    to_scheduled_post,
    to_source,
    to_source_record,
    to_token_set,
    to_upload_record,
)
from clipforge.store.records import SocialAccountRecord

NOW = datetime(2026, 4, 2, 15, 30, tzinfo=UTC)
TENANT = "ten_map"
CHANNEL = "ch_map"
PROJECT = "proj_map"


def _post() -> ScheduledPost:
    """A post with every optional field populated.

    Deliberately not a minimal fixture: a mapper that drops a field is only
    caught if the field had something in it, and defaults that happen to match
    on both sides hide exactly the bug being looked for.
    """

    return ScheduledPost(
        post_id="post_abc123",
        account_id="acc_tt",
        platform=Platform.INSTAGRAM,
        spec=PostSpec(
            asset=MediaAsset(
                asset_id="asset_9", path="/out/9.mp4",
                public_url="https://cdn.example/9.mp4", size_bytes=9_100_000,
                duration_s=29.75, width=1080, height=1920, fps=60,
                checksum="blake2b:aa11",
            ),
            title="The one line that ended it",
            caption="He said it in a meeting and lost the account",
            hashtags=("business", "negotiation"),
            per_platform_caption={"youtube": "A longer description, for search"},
            visibility=Visibility.PRIVATE,
            category_id="27",
            made_for_kids=True,
            metadata={"clip_id": "cl_9", "experiment": "hooks-A"},
        ),
        run_at=NOW + timedelta(hours=6),
        state=PostState.RETRYING,
        attempts=[
            Attempt(number=1, started_at=NOW, finished_at=NOW + timedelta(minutes=2),
                    state=PostState.FAILED, error_code="429",
                    error_message="rate limited", disposition="backoff",
                    remote_ref="publish_77"),
        ],
        remote_post_id="ig_5150",
        idempotency_key="acc_tt:asset_9:1743607800",
        series_id="weekly-business",
        lease_until=NOW + timedelta(minutes=5),
        next_attempt_at=NOW + timedelta(minutes=30),
        last_error="rate limited",
    )


def _source() -> Source:
    return Source(
        source_id="src_7",
        title="Ninety minutes on pricing",
        kind=SourceKind.PODCAST,
        rights=Rights(
            basis=RightsBasis.CREATIVE_COMMONS, reference="CC-BY-4.0",
            attribution="Studio Nine, CC BY 4.0", commercial_use=False,
            derivatives=True, verified_at=NOW - timedelta(days=30),
            expires_at=NOW + timedelta(days=300),
        ),
        url="https://example.com/ep/7",
        creator="Studio Nine",
        duration_s=5_400.0,
        published_at=NOW - timedelta(days=60),
        language="de",
        topics=("business", "pricing"),
        has_transcript=True,
    )


def _channel() -> Channel:
    return Channel(
        channel_id=CHANNEL,
        name="Pricing Clips",
        niche=Niche.BUSINESS,
        org_id=TENANT,
        accounts={Platform.TIKTOK: "acc_tt", Platform.YOUTUBE: "acc_yt"},
        topics=("business", "pricing"),
        accepted_rights=frozenset({RightsBasis.LICENSED, RightsBasis.OWNED}),
        monetised=False,
        timezone="Europe/Berlin",
        state=ChannelState.CIRCUIT_OPEN,
        budget=Budget(monthly_cents=75_000, spent_cents=21_400, period="2026-04"),
        health=ChannelHealth(
            consecutive_failures=5, total_items=140, total_published=118,
            total_blocked=14, total_failed=8, opened_at=NOW,
            last_error="TikTok 429",
        ),
        cadence_override=4,
        quality_floor_override=0.62,
        used_fingerprints={"fp_a", "fp_b"},
        created_at=NOW - timedelta(days=90),
    )


def _account() -> Account:
    return Account(
        account_id="acc_tt", platform=Platform.TIKTOK, org_id=TENANT,
        handle="@pricing", external_id="open_id_88", timezone="Europe/Berlin",
        direct_post_approved=True, business_account=True, enabled=False,
    )


def _tokens() -> TokenSet:
    return TokenSet(
        account_id="acc_tt", platform=Platform.TIKTOK,
        access_token="at-plain", refresh_token="rt-plain",
        expires_at=NOW + timedelta(hours=2),
        scopes=("video.publish", "video.upload"),
        refresh_valid_until=NOW + timedelta(days=300),
        obtained_at=NOW,
    )


def _seal(plain: str) -> str:
    return f"sealed:{plain}"


def _unseal(sealed: str) -> str:
    return sealed.removeprefix("sealed:")


class MappingTest(unittest.TestCase):
    # -- the enforcing test ------------------------------------------------

    def test_every_field_is_accounted_for(self) -> None:
        """Compare whole objects, not chosen fields.

        This is what makes the mappers safe to extend. Adding a field to
        `ScheduledPost` or `Channel` and forgetting the mapper fails here, at
        the moment the field is added, instead of at the first restart after
        it ships.
        """

        post = _post()
        restored = to_scheduled_post(
            to_upload_record(post, tenant_id=TENANT, channel_id=CHANNEL)
        )
        self._assert_same(post, restored, skip=())

        source = _source()
        self._assert_same(
            source, to_source(to_source_record(source, tenant_id=TENANT)), skip=()
        )

        account = _account()
        self._assert_same(
            account,
            to_account(to_account_record(account, tenant_id=TENANT)),
            skip=(),
        )

        channel = _channel()
        record = to_channel_record(channel, tenant_id=TENANT, project_id=PROJECT)
        self._assert_same(
            channel,
            to_channel(
                record,
                accounts=channel.accounts,
                used_fingerprints=channel.used_fingerprints,
            ),
            # Both come from other tables by design — see `to_channel_record`.
            # They are passed back in above, so this only excludes them from
            # the automatic sweep, not from the assertion.
            skip=(),
        )

    def _assert_same(self, original, restored, skip: tuple[str, ...]) -> None:
        fields = [
            f.name for f in dataclasses.fields(original) if f.name not in skip
        ]
        self.assertTrue(fields, "nothing compared — is this a dataclass?")
        for name in fields:
            with self.subTest(type(original).__name__, field=name):
                self.assertEqual(
                    getattr(restored, name),
                    getattr(original, name),
                    f"{type(original).__name__}.{name} did not survive the round trip",
                )

    # -- the fields worth explaining --------------------------------------

    def test_the_media_asset_survives_inside_the_metadata(self) -> None:
        """It has no columns of its own. Eleven columns nothing filters on
        would be eleven columns to migrate; the asset rides in `metadata`."""

        post = _post()
        restored = to_scheduled_post(
            to_upload_record(post, tenant_id=TENANT, channel_id=CHANNEL)
        )
        self.assertEqual(restored.spec.asset, post.spec.asset)
        self.assertEqual(restored.spec.asset.public_url,
                         "https://cdn.example/9.mp4")

    def test_the_callers_own_metadata_is_not_swallowed(self) -> None:
        """The mapper stashes things under reserved keys. A caller's own keys
        have to come back untouched and without the reserved ones leaking in."""

        post = _post()
        restored = to_scheduled_post(
            to_upload_record(post, tenant_id=TENANT, channel_id=CHANNEL)
        )
        self.assertEqual(
            restored.spec.metadata, {"clip_id": "cl_9", "experiment": "hooks-A"}
        )

    def test_the_attempt_history_comes_back_whole(self) -> None:
        """The audit trail. When a post lands twice, the only way to find out
        why is to see every request the system believed it was making."""

        post = _post()
        restored = to_scheduled_post(
            to_upload_record(post, tenant_id=TENANT, channel_id=CHANNEL)
        )
        self.assertEqual(len(restored.attempts), 1)
        attempt = restored.attempts[0]
        self.assertEqual(attempt.number, 1)
        self.assertEqual(attempt.state, PostState.FAILED)
        self.assertEqual(attempt.error_code, "429")
        self.assertEqual(attempt.disposition, "backoff")
        self.assertEqual(attempt.remote_ref, "publish_77")
        self.assertEqual(attempt.finished_at, NOW + timedelta(minutes=2))

    def test_published_at_is_derived_from_the_attempt_that_confirmed(self) -> None:
        """The post has no such field, and inventing one would put two answers
        in the system."""

        post = _post()
        self.assertIsNone(
            to_upload_record(post, tenant_id=TENANT, channel_id=CHANNEL).published_at
        )
        post.attempts.append(Attempt(
            number=2, started_at=NOW + timedelta(minutes=30),
            finished_at=NOW + timedelta(minutes=31), state=PostState.PUBLISHED,
        ))
        self.assertEqual(
            to_upload_record(post, tenant_id=TENANT, channel_id=CHANNEL).published_at,
            NOW + timedelta(minutes=31),
        )

    def test_a_tiktok_draft_is_recorded_as_awaiting_creator_not_published(self) -> None:
        """TikTok's unaudited path drops a draft in the creator's inbox. Storing
        that as `published` would put a green tick on a calendar next to
        something nobody can watch."""

        post = _post()
        post.state = PostState.AWAITING_CREATOR
        record = to_upload_record(post, tenant_id=TENANT, channel_id=CHANNEL)
        self.assertEqual(record.state, "awaiting_creator")
        self.assertEqual(
            to_scheduled_post(record).state, PostState.AWAITING_CREATOR
        )

    def test_the_series_id_does_not_become_a_dangling_foreign_key(self) -> None:
        """`schedule_id` points into `schedules`. A series id from the
        publishing engine's own recurrence is not always backed by a row there,
        and pointing the column at one that does not exist trades a lost field
        for a failed insert."""

        record = to_upload_record(_post(), tenant_id=TENANT, channel_id=CHANNEL)
        self.assertIsNone(record.schedule_id)
        self.assertEqual(to_scheduled_post(record).series_id, "weekly-business")

    def test_rights_are_columns_because_they_are_queried(self) -> None:
        """The expiry sweep and the clearance gate both filter on them, and a
        filter over JSON is a sequential scan."""

        record = to_source_record(_source(), tenant_id=TENANT)
        self.assertEqual(record.rights_basis, "creative_commons")
        self.assertFalse(record.commercial_use)
        self.assertEqual(record.rights_expires_at, NOW + timedelta(days=300))

    def test_the_circuit_breaker_survives_the_mapper(self) -> None:
        channel = _channel()
        record = to_channel_record(channel, tenant_id=TENANT, project_id=PROJECT)
        self.assertEqual(record.state, "circuit_open")
        self.assertEqual(record.consecutive_failures, 5)
        self.assertEqual(record.circuit_opened_at, NOW)
        restored = to_channel(record)
        self.assertTrue(restored.health.circuit_open(NOW))

    def test_the_channel_mapper_writes_neither_accounts_nor_fingerprints(self) -> None:
        """Both belong to other tables. Storing them here as well would be
        storing two answers, and they disagree the first time an account is
        disconnected."""

        record = to_channel_record(_channel(), tenant_id=TENANT, project_id=PROJECT)
        self.assertNotIn("accounts", record.__dataclass_fields__)
        self.assertNotIn("used_fingerprints", record.__dataclass_fields__)
        bare = to_channel(record)
        self.assertEqual(bare.accounts, {})
        self.assertEqual(bare.used_fingerprints, set())

    # -- credentials -------------------------------------------------------

    def test_credentials_round_trip_only_as_ciphertext(self) -> None:
        record = apply_tokens(
            SocialAccountRecord(id="acc_tt", tenant_id=TENANT, platform="tiktok"),
            _tokens(),
            seal=_seal,
        )
        self.assertEqual(record.access_token_sealed, "sealed:at-plain")
        self.assertEqual(record.refresh_token_sealed, "sealed:rt-plain")

        restored = to_token_set(record, unseal=_unseal)
        self.assertEqual(restored.access_token, "at-plain")
        self.assertEqual(restored.refresh_token, "rt-plain")
        self.assertEqual(restored.scopes, ("video.publish", "video.upload"))
        self.assertEqual(restored.refresh_valid_until, NOW + timedelta(days=300))
        self.assertEqual(restored.obtained_at, NOW)

    def test_sealing_is_not_optional(self) -> None:
        """No default. A signature that lets `seal` be omitted is one somebody
        fills in with `lambda s: s` at two in the morning."""

        with self.assertRaises(TypeError):
            apply_tokens(
                SocialAccountRecord(id="a", tenant_id=TENANT, platform="tiktok"),
                _tokens(),
            )

    def test_the_account_mapper_never_touches_the_token_columns(self) -> None:
        """A mapper that could write plaintext into them by accident is one
        that eventually does."""

        record = to_account_record(_account(), tenant_id=TENANT)
        self.assertEqual(record.access_token_sealed, "")
        self.assertEqual(record.refresh_token_sealed, "")
        self.assertIsNone(record.token_expires_at)

    def test_an_empty_token_seals_to_empty_rather_than_to_ciphertext(self) -> None:
        """An account with no refresh token — YouTube without offline access —
        must not end up with a sealed empty string that unseals to something
        the refresh path treats as a usable credential."""

        tokens = _tokens()
        tokens.refresh_token = ""
        record = apply_tokens(
            SocialAccountRecord(id="a", tenant_id=TENANT, platform="youtube"),
            tokens, seal=_seal,
        )
        self.assertEqual(record.refresh_token_sealed, "")
        self.assertEqual(to_token_set(record, unseal=_unseal).refresh_token, "")


if __name__ == "__main__":
    unittest.main()
