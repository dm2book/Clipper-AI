#!/usr/bin/env python3
"""Put a plausible tenant into the database so the dashboard has real rows.

    python demo/seed_dashboard.py --dsn ... --auth-dsn ... --admin-dsn ...

This is **not** mock data for the frontend. Every row goes through the real
stores, into real PostgreSQL, under row-level security, and the dashboard then
reads it back through the real API with no special casing anywhere. The
distinction that matters is where the data lives: a fixture in the frontend is
a lie the UI tells itself, and a row in Postgres is a row in Postgres however
it got there.

Deliberately uneven: a tripped channel, a failed upload, sources without
transcripts, published posts with no measurements. A seed where everything is
green exercises none of the states an operator actually opens the dashboard to
look at.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from clipforge.auth import (  # noqa: E402
    AccessTokenIssuer, AuthConfig, AuthService, IdentityStatus, PasswordHasher,
    PasswordPolicy,
)
from clipforge.auth.postgres import PostgresAuthStore  # noqa: E402
from clipforge.store import (  # noqa: E402
    AcquisitionRunRecord, ChannelRecord, ClipRecord, JobRecord,
    MetricSnapshotRecord, ProjectRecord, SocialAccountRecord, SourceRecord,
    TenantRecord, TranscriptionRunRecord, UploadRecord, UserRecord,
)
from clipforge.store.postgres import PostgresDatabase  # noqa: E402

TENANT = "ten_demo"
EMAIL = "dana@example.com"
PASSWORD = "marmalade tuesday bicycle"
NOW = datetime.now(UTC).replace(microsecond=0)

CHANNELS = [
    ("ch_business", "Founder Stories", "business", "active", 20_000, 6_400, 0),
    ("ch_ai", "AI Weekly", "ai", "active", 25_000, 18_900, 0),
    ("ch_motivation", "Momentum", "motivation", "paused", 15_000, 15_000, 0),
    ("ch_history", "The Long View", "history", "circuit_open", 12_000, 3_100, 5),
]

SOURCES = [
    ("The raise was the mistake", "podcast_feed", "Founder FM", 4210.0, True),
    ("What pricing power looks like", "media_url", "Studio Nine", 2870.0, True),
    ("Nobody tells you about headcount", "podcast_feed", "Founder FM", 3315.0, True),
    ("Inference costs are the new CAC", "media_url", "AI Weekly", 1980.0, True),
    ("Two years of shipping nothing", "podcast_feed", "Founder FM", 5120.0, False),
    ("The quiet part about fundraising", "media_url", "Studio Nine", 2455.0, True),
    ("A hundred years in ten minutes", "media_url", "Archive Reel", 640.0, False),
]

TITLES = [
    "He lost the deal in one sentence",
    "The hire that nearly killed us",
    "Nobody warns you about this",
    "We burned fourteen million dollars",
    "The revenue never followed",
    "I confused growth with a business",
    "We cut the team and shipped faster",
    "That is not in the funding announcement",
]


def seed_tenant(database, rng: random.Random) -> None:
    with database.unit_of_work(TENANT) as uow:
        uow.tenants.save(TenantRecord(id=TENANT, name="Acme Media", plan="agency"))
        uow.projects.save(ProjectRecord(id="proj_demo", tenant_id=TENANT,
                                        name="Acme Media"))
        uow.users.save(UserRecord(id="usr_dana", tenant_id=TENANT, email=EMAIL,
                                  name="Dana", role="owner"))

        for cid, name, niche, state, budget, spent, failures in CHANNELS:
            uow.channels.save(ChannelRecord(
                id=cid, tenant_id=TENANT, project_id="proj_demo", name=name,
                niche=niche, state=state, budget_monthly_cents=budget,
                budget_spent_cents=spent, consecutive_failures=failures,
                circuit_opened_at=NOW - timedelta(hours=6)
                if state == "circuit_open" else None,
                last_error="TikTok returned 429 five times running"
                if state == "circuit_open" else "",
                topics=["business", "startups"],
                total_items=rng.randint(20, 90),
                total_published=rng.randint(8, 40),
                total_blocked=rng.randint(0, 9),
                total_failed=failures,
                created_at=NOW - timedelta(days=rng.randint(30, 120)),
            ))

        for platform in ("tiktok", "youtube", "instagram"):
            uow.accounts.save(SocialAccountRecord(
                id=f"acc_{platform}", tenant_id=TENANT, channel_id="ch_business",
                platform=platform, handle=f"@acmemedia_{platform}",
            ))

        for index, (title, kind, creator, duration, transcribed) in enumerate(SOURCES):
            source_id = f"src_{index:02d}"
            created = NOW - timedelta(days=len(SOURCES) - index, hours=index)
            uow.sources.save(SourceRecord(
                id=source_id, tenant_id=TENANT, title=title, kind=kind,
                creator=creator, duration_s=duration, has_transcript=transcribed,
                url=f"https://example.com/watch/{source_id}",
                topics=["business", "startups"], language="en",
                fingerprint=f"sha256:{source_id}",
                rights_basis="owned" if index % 3 else "licensed",
                rights_expires_at=(NOW + timedelta(days=12)) if index == 1 else None,
                published_at=created - timedelta(days=2),
                created_at=created,
            ))
            uow.acquisitions.save(AcquisitionRunRecord(
                id=f"acq_{index:02d}", tenant_id=TENANT, source_id=source_id,
                kind=kind, ref_key=source_id, state="ready",
                media_path=f"/var/lib/clipforge/media/{source_id}/media.mp4",
                bytes_done=int(duration * 180_000), created_at=created,
            ))
            if transcribed:
                uow.transcriptions.save(TranscriptionRunRecord(
                    id=f"txn_{index:02d}", tenant_id=TENANT, source_id=source_id,
                    state="succeeded", provider="faster-whisper", model="small",
                    language="en", word_count=int(duration * 2.4),
                    segment_count=int(duration / 12), duration_s=duration,
                    elapsed_s=duration / 8, created_at=created,
                ))
            elif index == 4:
                uow.transcriptions.save(TranscriptionRunRecord(
                    id=f"txn_{index:02d}", tenant_id=TENANT, source_id=source_id,
                    state="failed_retryable", provider="faster-whisper",
                    last_error="model host unreachable", attempts=2,
                    created_at=created,
                ))

        clips = []
        for index in range(14):
            clip_id = f"cl_{index:02d}"
            clips.append(clip_id)
            uow.clips.save(ClipRecord(
                id=clip_id, tenant_id=TENANT,
                channel_id=CHANNELS[index % 3][0],
                source_id=f"src_{index % len(SOURCES):02d}",
                start_ms=index * 60_000, end_ms=index * 60_000 + 31_000,
                duration_s=31.0, title=TITLES[index % len(TITLES)],
                virality_score=round(rng.uniform(48, 92), 1),
                hook_text=TITLES[index % len(TITLES)],
                hook_type="curiosity_gap", predicted_lift=rng.uniform(1.1, 2.4),
                created_at=NOW - timedelta(days=14 - index),
            ))

        # Uploads: published, queued, retrying, failed, needs_attention — the
        # states an operator opens this page to find.
        plan = (
            [("published", 9)] + [("scheduled", 4)] + [("retrying", 2)]
            + [("failed", 1)] + [("needs_attention", 1)]
        )
        counter = 0
        for state, count in plan:
            for _ in range(count):
                platform = ("tiktok", "youtube", "instagram")[counter % 3]
                published_at = (
                    NOW - timedelta(days=rng.randint(0, 25), hours=rng.randint(0, 20))
                    if state == "published" else None
                )
                uow.uploads.save(UploadRecord(
                    id=f"up_{counter:02d}", tenant_id=TENANT,
                    channel_id=CHANNELS[counter % 3][0],
                    account_id=f"acc_{platform}", clip_id=clips[counter % len(clips)],
                    platform=platform, state=state,
                    run_at=published_at or NOW + timedelta(hours=counter + 1),
                    next_attempt_at=(NOW + timedelta(minutes=17 * (counter + 1)))
                    if state == "retrying" else None,
                    title=TITLES[counter % len(TITLES)],
                    caption=TITLES[counter % len(TITLES)] + " #business",
                    visibility="public",
                    idempotency_key=f"idem-{counter:02d}",
                    remote_post_id=f"{platform}_{counter:04d}"
                    if state == "published" else "",
                    attempt_count=2 if state in ("retrying", "failed") else 1,
                    last_error=(
                        "TikTok returned 429; retrying with backoff"
                        if state == "retrying" else
                        "Instagram: media fetch failed — the public URL 404'd"
                        if state == "failed" else
                        "Credentials rejected; the account must be reconnected"
                        if state == "needs_attention" else ""
                    ),
                    published_at=published_at,
                    created_at=NOW - timedelta(days=rng.randint(1, 26)),
                ))
                counter += 1

        # Measurements for some published posts, and deliberately not all:
        # "not measured" is a real state and the dashboard renders it apart
        # from zero.
        for upload in uow.uploads.all():
            if upload.state != "published" or rng.random() < 0.3:
                continue
            for age in (1.0, 24.0):
                views = rng.randint(400, 26_000)
                uow.metrics.append(MetricSnapshotRecord(
                    id=f"snap_{upload.id}_{int(age)}", tenant_id=TENANT,
                    upload_id=upload.id,
                    taken_at=(upload.published_at or NOW) + timedelta(hours=age),
                    age_hours=age, views=views,
                    likes=int(views * rng.uniform(0.03, 0.11)),
                    comments=int(views * rng.uniform(0.001, 0.01)),
                    shares=int(views * rng.uniform(0.002, 0.02)),
                    saves=int(views * rng.uniform(0.001, 0.015)),
                    impressions=int(views * rng.uniform(1.2, 2.4)),
                    avg_watch_pct=round(rng.uniform(28, 74), 1),
                    watch_time_s=views * rng.uniform(8, 22),
                ))

        uow.jobs.enqueue(JobRecord(
            id="job_dead_1", tenant_id=TENANT, kind="transcribe",
            state="dead", run_after=NOW - timedelta(hours=3),
            last_error="model host unreachable after 4 attempts",
        ))


def seed_auth(auth_dsn: str) -> None:
    store = PostgresAuthStore(auth_dsn)
    config = AuthConfig(password_policy=PasswordPolicy(rounds=10))
    service = AuthService(
        store, AccessTokenIssuer(config.keyring), config=config,
        hasher=PasswordHasher(config.password_policy),
    )
    existing = store.identity_by_email(EMAIL)
    if existing is None:
        result = service.sign_up(EMAIL, PASSWORD)
        identity_id = result.identity_id
        link = service.sender.links_for(EMAIL)[-1]
        service.verify_email(link.split("token=")[1])
    else:
        # The address already exists — most often left behind by the auth test
        # suite, which uses the same one. Reconcile rather than assume: a
        # seeder that prints a password the account does not have sends the
        # next person to debug the login instead of the dashboard.
        identity_id = existing.identity_id
        existing.password_hash = service.hasher.hash(PASSWORD)
        existing.password_algo = service.config.password_algorithm
        existing.status = IdentityStatus.ACTIVE
        existing.email_verified_at = existing.email_verified_at or NOW
        existing.failed_attempts = 0
        existing.locked_until = None
        store.save_identity(existing)
        print("  note       reused an existing identity and reset its password")
    service.add_membership(identity_id, TENANT, "usr_dana", "owner",
                           "Acme Media")
    store.close()
    print(f"  account    {EMAIL} / {PASSWORD}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("CLIPFORGE_DSN", ""))
    parser.add_argument("--auth-dsn",
                        default=os.environ.get("CLIPFORGE_AUTH_DSN", ""))
    parser.add_argument("--admin-dsn",
                        default=os.environ.get("CLIPFORGE_TEST_ADMIN_DSN", ""))
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    if not args.dsn or not args.auth_dsn:
        print("--dsn and --auth-dsn are required", file=sys.stderr)
        return 2

    if args.admin_dsn:
        import psycopg

        with psycopg.connect(args.admin_dsn) as connection:
            with connection.cursor() as cursor:
                # FORCE ROW LEVEL SECURITY covers the owner too, so even a
                # delete needs a tenant scope. That is the point of FORCE: an
                # application pointed at the migration role by a copy-pasted
                # DATABASE_URL fails here rather than reading everything.
                cursor.execute(
                    "SELECT set_config('app.tenant_id', %s, true)", (TENANT,)
                )
                cursor.execute("DELETE FROM tenants WHERE id = %s", (TENANT,))
            connection.commit()

    rng = random.Random(args.seed)
    database = PostgresDatabase(args.dsn)
    try:
        seed_tenant(database, rng)
    finally:
        database.close()
    seed_auth(args.auth_dsn)

    print(f"  tenant     {TENANT}")
    print("  seeded into PostgreSQL through the real stores\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
