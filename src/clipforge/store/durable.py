"""Durable replacements for the engines' in-memory stores.

Before this module the decision layer held everything in dictionaries: the
calendar's posts, the publishing system's accounts, its OAuth tokens, the
factory's channels, the analytics store's readings. All of it survived exactly
as long as the process did.

Each class here is a drop-in for one of those dictionaries, keeping the same
interface so the engines did not have to be rewritten around a database.

## Write-through, not write-behind

Every mutation reaches Postgres before the call returns. Batching writes would
be faster and would reintroduce precisely the failure being removed: work that
looked saved and was not.

## Why the calendar keeps a working set in memory

`PersistentCalendar` is the one that is not purely a pass-through. The calendar
answers questions — day occupancy, spacing conflicts, per-account windows,
month views — that are cheap over a sorted in-memory index and would be a
round trip each over SQL. So it keeps its index, and Postgres holds the truth:
every write goes through, and `load()` rebuilds the index at startup.

The important consequence is that the index is a **cache of a window**, not of
the table. `load()` takes a horizon and pulls the posts inside it, because a
year of an empire's uploads is millions of rows and none of them belong in a
worker's heap. Posts outside the window are still in the database and still
publish — they are simply not in this process's view until the window moves.
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from datetime import datetime, timedelta
from typing import Any, Callable

from ..analytics.attribution import AnalyticsStore, PostRecord
from ..analytics.metrics import PostMetrics, RetentionCurve, Snapshot
from ..factory.channel import Channel
from ..factory.sources import RegistrySourceFinder
from ..factory.sources import Source as FactorySource
from ..publish.calendar import ContentCalendar
from ..publish.oauth import TokenSet
from ..publish.types import Account, Platform, ScheduledPost, ensure_utc, utcnow
from .errors import NotFound
from .mappers import (
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
from .records import MetricSnapshotRecord, SocialAccountRecord

__all__ = [
    "DurableTokenStore",
    "DurableAccountBook",
    "PersistentCalendar",
    "DurableSourceRegistry",
    "DurableChannelBook",
    "DurableAnalyticsStore",
    "DEFAULT_HORIZON_DAYS",
]

#: How far ahead `PersistentCalendar.load` pulls by default. Ninety days covers
#: the "weeks and months ahead" the scheduler supports, without loading a
#: portfolio's entire history into a worker.
DEFAULT_HORIZON_DAYS = 90

#: How far *back* to load. Far enough to see recent failures and retries;
#: not so far that a year of published posts rides along in memory.
DEFAULT_LOOKBACK_DAYS = 14


class _TenantBound:
    """Shared plumbing: a database and the one tenant this object speaks for."""

    def __init__(self, database: Any, tenant_id: str) -> None:
        if not tenant_id:
            raise ValueError("a durable store needs a tenant")
        self._database = database
        self.tenant_id = tenant_id

    def _uow(self):
        return self._database.unit_of_work(self.tenant_id)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class DurableTokenStore(_TenantBound):
    """`TokenStore` backed by the `social_accounts` table.

    Tokens live on the account row rather than in a table of their own: there
    is exactly one credential set per account, and a separate table would only
    add a join and a way for the two to disagree about which accounts exist.

    `seal` and `unseal` are required. Refresh tokens are long-lived credentials
    to other people's audiences, and what is written here is ciphertext, so a
    database dump is not a set of working logins. The key belongs in a KMS held
    by something other than the process that publishes — which is why this
    takes functions rather than implementing any crypto itself.

    One transaction per call. That is a round trip the in-memory store did not
    make, and it is the correct default: a token read inside a larger unit of
    work would otherwise see whatever that transaction had already written,
    which is not what a credential lookup should mean.
    """

    def __init__(
        self,
        database: Any,
        tenant_id: str,
        *,
        seal: Callable[[str], str],
        unseal: Callable[[str], str],
    ) -> None:
        super().__init__(database, tenant_id)
        self._seal = seal
        self._unseal = unseal

    def get(self, account_id: str) -> TokenSet | None:
        with self._uow() as uow:
            record = uow.accounts.get(account_id)
        if record is None or not record.access_token_sealed:
            return None
        return to_token_set(record, unseal=self._unseal)

    def put(self, tokens: TokenSet) -> None:
        with self._uow() as uow:
            record = uow.accounts.get(tokens.account_id)
            if record is None:
                # An account can be authorised before it is otherwise
                # configured. Creating a minimal row beats refusing the
                # credential and losing an OAuth callback that will not repeat.
                record = SocialAccountRecord(
                    id=tokens.account_id,
                    tenant_id=self.tenant_id,
                    platform=tokens.platform.value,
                )
            uow.accounts.save(apply_tokens(record, tokens, seal=self._seal))

    def delete(self, account_id: str) -> None:
        """Forget the credentials, keep the account.

        Disconnecting is not deleting: the account's posts, metrics and history
        stay, and a row removed here would cascade them away.
        """

        with self._uow() as uow:
            record = uow.accounts.get(account_id)
            if record is None:
                return
            record.access_token_sealed = ""
            record.refresh_token_sealed = ""
            record.token_expires_at = None
            record.refresh_valid_until = None
            record.token_obtained_at = None
            record.scopes = []
            uow.accounts.save(record)

    def all_accounts(self) -> tuple[str, ...]:
        with self._uow() as uow:
            return tuple(
                r.id for r in uow.accounts.all() if r.access_token_sealed
            )

    def expiring_before(self, moment: datetime) -> tuple[str, ...]:
        """Accounts whose refresh token lapses before `moment`.

        Not on the `TokenStore` protocol; available because the index exists
        and because a refresh token that lapses unnoticed is a channel that
        silently stops posting, with the customer finding out first.
        """

        with self._uow() as uow:
            return tuple(a.id for a in uow.accounts.refresh_expiring_before(moment))


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


class DurableAccountBook(_TenantBound, MutableMapping[str, Account]):
    """`dict[str, Account]` that happens to be a table.

    A `MutableMapping` rather than a new interface so `PublishingSystem` keeps
    working unchanged: `self.accounts[id] = account`, `.values()`, `in`, `len`
    all mean what they meant.

    Reads are not cached. An account's `enabled` flag and audit status are
    edited from the dashboard, by a different process, and a cached copy is how
    a system keeps posting to an account somebody just disconnected.
    """

    def __init__(
        self, database: Any, tenant_id: str, *, channel_id: str | None = None
    ) -> None:
        super().__init__(database, tenant_id)
        #: Attached to accounts created through this book. The publishing
        #: engine has no notion of channels; the factory does.
        self.channel_id = channel_id

    def __getitem__(self, account_id: str) -> Account:
        with self._uow() as uow:
            record = uow.accounts.get(account_id)
        if record is None:
            raise KeyError(account_id)
        return to_account(record)

    def __setitem__(self, account_id: str, account: Account) -> None:
        if account_id != account.account_id:
            raise ValueError(
                f"key {account_id!r} does not match account {account.account_id!r}"
            )
        with self._uow() as uow:
            existing = uow.accounts.get(account_id)
            record = to_account_record(
                account,
                tenant_id=self.tenant_id,
                channel_id=(existing.channel_id if existing else self.channel_id),
            )
            if existing is not None:
                # Credentials are not the caller's to overwrite here. An
                # `accounts[id] = account` that silently wiped the tokens would
                # disconnect a live account as a side effect of renaming it.
                record.access_token_sealed = existing.access_token_sealed
                record.refresh_token_sealed = existing.refresh_token_sealed
                record.token_expires_at = existing.token_expires_at
                record.refresh_valid_until = existing.refresh_valid_until
                record.token_obtained_at = existing.token_obtained_at
                record.scopes = existing.scopes
            uow.accounts.save(record)

    def __delitem__(self, account_id: str) -> None:
        with self._uow() as uow:
            if not uow.accounts.delete(account_id):
                raise KeyError(account_id)

    def __iter__(self) -> Iterator[str]:
        with self._uow() as uow:
            return iter([r.id for r in uow.accounts.all()])

    def __len__(self) -> int:
        with self._uow() as uow:
            return uow.accounts.count()

    def values(self):  # type: ignore[override]
        """Overridden so listing every account is one query, not one per key.

        `MutableMapping` would derive this from `__iter__` and `__getitem__`,
        which is N+1 round trips — fine at three accounts, not at five hundred.
        """

        with self._uow() as uow:
            return [to_account(r) for r in uow.accounts.all()]

    def items(self):  # type: ignore[override]
        return [(a.account_id, a) for a in self.values()]


# ---------------------------------------------------------------------------
# The calendar
# ---------------------------------------------------------------------------


class PersistentCalendar(ContentCalendar, _TenantBound):
    """`ContentCalendar` whose posts are rows in `uploads`.

    Every `add`, `move`, `remove` and `persist` writes through before
    returning. The in-memory index stays, because that is what makes the
    spacing check a binary search instead of a query — see the comment on
    `ContentCalendar._by_account` for what it cost when it was linear.

    Restart behaviour is the whole point: `load()` rebuilds the index from the
    table, so a process that comes back finds the same calendar it left.
    """

    def __init__(
        self,
        database: Any,
        tenant_id: str,
        *,
        channel_id: str,
        tz: str = "UTC",
    ) -> None:
        # Guard first: `ContentCalendar.__init__` calls `self.add`, which this
        # class overrides to write through. Without the flag, rehydrating a
        # calendar would write every post it just read straight back.
        self._writing_through = False
        ContentCalendar.__init__(self, tz=tz)
        _TenantBound.__init__(self, database, tenant_id)
        self.channel_id = channel_id
        self._writing_through = True

    # -- write-through -----------------------------------------------------

    def _save(self, *posts: ScheduledPost) -> None:
        if not self._writing_through or not posts:
            return
        with self._uow() as uow:
            for post in posts:
                uow.uploads.save(
                    to_upload_record(
                        post, tenant_id=self.tenant_id, channel_id=self.channel_id
                    )
                )

    def add(self, post: ScheduledPost) -> None:
        super().add(post)
        self._save(post)

    def remove(self, post_id: str) -> None:
        super().remove(post_id)
        if self._writing_through:
            with self._uow() as uow:
                uow.uploads.delete(post_id)

    def move(self, post_id: str, run_at: datetime) -> ScheduledPost:
        # `super().move` routes through `remove` then `add`, so the row would
        # be deleted and reinserted — losing `created_at`, and briefly losing
        # the post entirely if the process died between the two. Reorder the
        # index without touching the database, then save once.
        was = self._writing_through
        self._writing_through = False
        try:
            post = super().move(post_id, run_at)
        finally:
            self._writing_through = was
        self._save(post)
        return post

    def persist(self, *posts: ScheduledPost) -> None:
        """Write the given posts' current state through.

        The engine mutates posts in place — state, attempts, lease, error — and
        this is how those transitions reach the table. Called with no arguments
        it flushes everything in the index, which is the safe answer when a
        caller is not sure what it touched.
        """

        self._save(*(posts or self.posts))

    # -- rehydration -------------------------------------------------------

    def load(
        self,
        now: datetime | None = None,
        *,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        horizon_days: int = DEFAULT_HORIZON_DAYS,
    ) -> int:
        """Rebuild the index from the table. Returns how many posts came back.

        Bounded by a window on purpose. An empire scheduling 500 uploads a day
        ninety days out is 45,000 rows, which is a reasonable working set; the
        same query without a horizon is every post the account has ever made,
        which is not. Posts outside the window are not lost — they are in the
        table, and they publish when the window reaches them.
        """

        now = ensure_utc(now or utcnow())
        start = now - timedelta(days=lookback_days)
        end = now + timedelta(days=horizon_days)

        with self._uow() as uow:
            records = uow.uploads.for_channel_between(self.channel_id, start, end)

        was = self._writing_through
        self._writing_through = False
        try:
            for record in records:
                self.add(to_scheduled_post(record))
        finally:
            self._writing_through = was
        return len(records)

    @classmethod
    def restore(
        cls,
        database: Any,
        tenant_id: str,
        *,
        channel_id: str,
        tz: str = "UTC",
        now: datetime | None = None,
        **window: int,
    ) -> PersistentCalendar:
        """Open a calendar and fill it from the table in one step."""

        calendar = cls(database, tenant_id, channel_id=channel_id, tz=tz)
        calendar.load(now, **window)
        return calendar


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


class DurableSourceRegistry(RegistrySourceFinder, _TenantBound):
    """The rights-cleared library, in `sources` rather than a dictionary.

    Inherits `find` unchanged. The scoring — kind, topic overlap, recency, a
    bonus for material that already has a transcript — is a product decision,
    not a storage one, and reimplementing it in SQL would give two rankings
    that drift apart.

    The cost is that `find` reads the whole library per call. That is the right
    trade at the size this table actually reaches: a rights-cleared library is
    entered by hand, licence by licence, and it is thousands of rows, not
    millions. If it ever stops being, the scoring moves into the query and this
    comment is the reason to look there first.
    """

    def __init__(self, database: Any, tenant_id: str) -> None:
        RegistrySourceFinder.__init__(self)
        _TenantBound.__init__(self, database, tenant_id)

    def register(self, source: FactorySource) -> None:
        with self._uow() as uow:
            record = to_source_record(source, tenant_id=self.tenant_id)
            existing = uow.sources.get(source.source_id)
            if existing is not None:
                record.created_at = existing.created_at
            uow.sources.save(record)

    def remove(self, source_id: str) -> None:
        with self._uow() as uow:
            uow.sources.delete(source_id)

    @property
    def all(self) -> tuple[FactorySource, ...]:
        with self._uow() as uow:
            return tuple(to_source(r) for r in uow.sources.all())

    def get(self, source_id: str) -> FactorySource | None:
        with self._uow() as uow:
            record = uow.sources.get(source_id)
        return to_source(record) if record else None

    def by_fingerprint(self, fingerprint: str) -> FactorySource | None:
        with self._uow() as uow:
            record = uow.sources.by_fingerprint(fingerprint)
        return to_source(record) if record else None

    def expiring_before(self, moment: datetime) -> tuple[FactorySource, ...]:
        """Licences that lapse before `moment`.

        A factory scheduling three months ahead will happily publish under a
        licence that expired in March unless something checks, and this is the
        query that checks. Index-backed on `(tenant_id, rights_expires_at)`.
        """

        with self._uow() as uow:
            return tuple(
                to_source(r) for r in uow.sources.rights_expiring_before(moment)
            )


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


class DurableChannelBook(_TenantBound, MutableMapping[str, "Channel"]):
    """`dict[str, Channel]` backed by the `channels` table.

    Two of a channel's fields are assembled from other tables rather than
    stored on the row, because storing them twice means storing two answers:

    * `accounts` — the platform-to-account map is rebuilt from the account rows
      that point at this channel, so disconnecting an account cannot leave a
      stale entry pointing at it;
    * `used_fingerprints` — the set of already-clipped material comes from
      `channel_source_uses` joined to `sources`. An array column on the channel
      would be rewritten in full on every append, and that set grows for as
      long as the channel runs.

    Everything else round-trips on the row, including the circuit-breaker
    state. That one matters: a channel that tripped before a restart must stay
    tripped, or a deploy quietly retries every failing channel at once.
    """

    def __init__(self, database: Any, tenant_id: str, *, project_id: str) -> None:
        super().__init__(database, tenant_id)
        self.project_id = project_id

    def _fingerprints_by_source(self, uow: Any) -> dict[str, str]:
        """source id -> fingerprint, read once.

        Fetched up front rather than per use: a channel that has run for a year
        has thousands of `channel_source_uses` rows, and a lookup each would
        make loading one channel thousands of queries — the classic N+1, and
        the reason `values()` reads the sources once and passes the map down.
        """

        return {s.id: s.fingerprint for s in uow.sources.all()}

    def _hydrate(
        self, uow: Any, record: Any, fingerprints: dict[str, str] | None = None
    ) -> Channel:
        accounts = {
            Platform(a.platform): a.id for a in uow.accounts.for_channel(record.id)
        }
        if fingerprints is None:
            fingerprints = self._fingerprints_by_source(uow)
        used = {
            fingerprints[use.source_id]
            for use in uow.sources.used_by(record.id)
            if use.source_id in fingerprints
        }
        return to_channel(record, accounts=accounts, used_fingerprints=used)

    def __getitem__(self, channel_id: str) -> Channel:
        with self._uow() as uow:
            record = uow.channels.get(channel_id)
            if record is None:
                raise KeyError(channel_id)
            return self._hydrate(uow, record)

    def __setitem__(self, channel_id: str, channel: Channel) -> None:
        if channel_id != channel.channel_id:
            raise ValueError(
                f"key {channel_id!r} does not match channel {channel.channel_id!r}"
            )
        with self._uow() as uow:
            record = to_channel_record(
                channel, tenant_id=self.tenant_id, project_id=self.project_id
            )
            existing = uow.channels.get(channel_id)
            if existing is not None:
                record.created_at = existing.created_at
            uow.channels.save(record)

            # The account rows own the channel link, so writing the channel is
            # also where that link is kept true.
            for platform, account_id in channel.accounts.items():
                account = uow.accounts.get(account_id)
                if account is None:
                    account = SocialAccountRecord(
                        id=account_id,
                        tenant_id=self.tenant_id,
                        platform=platform.value,
                    )
                account.channel_id = channel_id
                uow.accounts.save(account)

            # Fingerprints the channel has consumed since it was last written.
            # Only those matching a known source can be recorded: the join
            # table is keyed by source, which is what stops it filling with
            # references to material nobody can look up.
            if channel.used_fingerprints:
                known = {s.fingerprint: s.id for s in uow.sources.all()}
                for fingerprint in channel.used_fingerprints:
                    source_id = known.get(fingerprint)
                    if source_id is not None:
                        uow.sources.mark_used(channel_id, source_id, utcnow())

    def __delitem__(self, channel_id: str) -> None:
        with self._uow() as uow:
            if not uow.channels.delete(channel_id):
                raise KeyError(channel_id)

    def __iter__(self) -> Iterator[str]:
        with self._uow() as uow:
            return iter([r.id for r in uow.channels.all()])

    def __len__(self) -> int:
        with self._uow() as uow:
            return uow.channels.count()

    def values(self):  # type: ignore[override]
        """One query for the rows, not one per key."""

        with self._uow() as uow:
            fingerprints = self._fingerprints_by_source(uow)
            return [
                self._hydrate(uow, r, fingerprints) for r in uow.channels.all()
            ]

    def items(self):  # type: ignore[override]
        return [(c.channel_id, c) for c in self.values()]


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

#: Where the analytics fields that have no column of their own ride. Namespaced
#: so a caller's own `extra` cannot collide with them.
_ANALYTICS = "_analytics"


class DurableAnalyticsStore(AnalyticsStore, _TenantBound):
    """`AnalyticsStore` whose readings are rows in `metric_snapshots`.

    Inherits `select`, `group` and `coverage` unchanged: those are the
    statistics, and they are the same statistics whether the records came from
    a dictionary or a table. What changes is where the records live.

    Snapshots are **append-only**, and not by convention — the application role
    holds no UPDATE or DELETE on `metric_snapshots`. Every finding the
    analytics engine reports rests on comparing posts at matched ages, and a
    reading rewritten after the fact makes those comparisons quietly wrong with
    no way to recover the original. A double collection at the same age is a
    constraint violation rather than a silently doubled view count.

    Ids for snapshot rows are derived from the post and the age, so a
    re-collection is caught by the primary key as well as by the unique index.
    """

    def __init__(self, database: Any, tenant_id: str) -> None:
        AnalyticsStore.__init__(self)
        _TenantBound.__init__(self, database, tenant_id)

    # -- write-through -----------------------------------------------------

    def add(self, record: PostRecord) -> None:
        super().add(record)
        self._save(record)

    def _save(self, record: PostRecord) -> None:
        with self._uow() as uow:
            upload = uow.uploads.get(record.post_id)
            if upload is None:
                # Analytics can legitimately arrive for a post this process did
                # not schedule — a backfill, or a post published before the
                # system took the channel over. Recording the reading matters
                # more than insisting the upload row already exists.
                return
            self._save_decisions(uow, upload, record)
            self._save_snapshots(uow, record)

    def _save_decisions(self, uow: Any, upload: Any, record: PostRecord) -> None:
        """Persist what the system decided, on the clip that carries it."""

        if not upload.clip_id:
            return
        clip = uow.clips.get(upload.clip_id)
        if clip is None:
            return
        clip.hook_text = record.hook_text
        clip.hook_type = record.hook_type
        clip.hook_rank = record.hook_rank
        clip.hook_explored = record.explored
        clip.predicted_lift = record.predicted_lift
        clip.virality_score = record.predicted_virality
        clip.topic = record.topic
        clip.weights_version = record.hook_weights_version
        clip.duration_s = record.clip_duration_s
        features = dict(clip.features or {})
        features[_ANALYTICS] = {
            "caption_style": record.caption_style,
            "gameplay_bed": record.gameplay_bed,
            "viral_weights_version": record.viral_weights_version,
            "extra": dict(record.extra),
        }
        clip.features = features
        uow.clips.save(clip)

    def _save_snapshots(self, uow: Any, record: PostRecord) -> None:
        existing = {s.age_hours for s in uow.metrics.for_upload(record.post_id)}
        for snapshot in record.metrics.snapshots:
            if snapshot.age_hours in existing:
                continue
            uow.metrics.append(
                MetricSnapshotRecord(
                    id=_snapshot_id(record.post_id, snapshot.age_hours),
                    tenant_id=self.tenant_id,
                    upload_id=record.post_id,
                    taken_at=snapshot.taken_at,
                    age_hours=snapshot.age_hours,
                    views=snapshot.views,
                    likes=snapshot.likes,
                    comments=snapshot.comments,
                    shares=snapshot.shares,
                    saves=snapshot.saves,
                    follows=snapshot.follows,
                    impressions=snapshot.impressions,
                    watch_time_s=snapshot.watch_time_s,
                    avg_watch_pct=snapshot.avg_watch_pct,
                    # Null, not an empty curve. Only YouTube reports one, and
                    # an imputed flat curve would be counted as a measured bad
                    # outcome rather than as a missing measurement.
                    retention_curve=(
                        [list(p) for p in snapshot.retention.points]
                        if snapshot.retention.points
                        else None
                    ),
                )
            )

    # -- rehydration -------------------------------------------------------

    def load(self, since: datetime | None = None) -> int:
        """Rebuild the records from the tables. Returns how many came back.

        Joins three tables by hand rather than in SQL: `uploads` for who and
        when, `clips` for what the system decided, `metric_snapshots` for what
        came of it. The channel and source lookups are read once into maps
        first — one query each, not one per post.
        """

        with self._uow() as uow:
            channels = {c.id: c for c in uow.channels.all()}
            sources = {s.id: s for s in uow.sources.all()}
            uploads = [
                u
                for u in uow.uploads.in_state("published", "awaiting_creator")
                if u.published_at is not None
                and (since is None or u.published_at >= ensure_utc(since))
            ]
            clips = {c.id: c for c in uow.clips.all()}
            readings: dict[str, list[Any]] = {}
            for upload in uploads:
                readings[upload.id] = list(uow.metrics.for_upload(upload.id))

        loaded = 0
        for upload in uploads:
            channel = channels.get(upload.channel_id)
            clip = clips.get(upload.clip_id) if upload.clip_id else None
            source = sources.get(clip.source_id) if clip and clip.source_id else None
            AnalyticsStore.add(
                self,
                _to_post_record(
                    upload,
                    channel=channel,
                    clip=clip,
                    source=source,
                    snapshots=readings.get(upload.id, ()),
                ),
            )
            loaded += 1
        return loaded


def _snapshot_id(post_id: str, age_hours: float) -> str:
    """Deterministic, so a repeat collection collides instead of duplicating."""

    return f"ms_{post_id}_{age_hours:g}"


def _to_post_record(
    upload: Any, *, channel: Any, clip: Any, source: Any, snapshots
) -> PostRecord:
    features = dict((clip.features if clip else None) or {})
    packed = features.get(_ANALYTICS, {}) or {}

    metrics = PostMetrics(
        post_id=upload.id,
        platform=Platform(upload.platform),
        published_at=upload.published_at,
        snapshots=[
            Snapshot(
                taken_at=row.taken_at,
                age_hours=row.age_hours,
                views=row.views,
                likes=row.likes,
                comments=row.comments,
                shares=row.shares,
                saves=row.saves,
                follows=row.follows,
                watch_time_s=row.watch_time_s,
                avg_watch_pct=row.avg_watch_pct,
                retention=RetentionCurve(
                    points=tuple(
                        (float(p[0]), float(p[1])) for p in (row.retention_curve or ())
                    )
                ),
                impressions=row.impressions,
            )
            for row in snapshots
        ],
    )
    return PostRecord(
        post_id=upload.id,
        metrics=metrics,
        channel_id=upload.channel_id,
        channel_name=channel.name if channel else "",
        niche=channel.niche if channel else "",
        account_id=upload.account_id,
        timezone=channel.timezone if channel else "UTC",
        hook_text=clip.hook_text if clip else "",
        hook_type=clip.hook_type if clip else "",
        predicted_lift=clip.predicted_lift if clip else 0.0,
        hook_rank=clip.hook_rank if clip else 0,
        explored=clip.hook_explored if clip else False,
        topic=clip.topic if clip else "",
        source_id=(clip.source_id or "") if clip else "",
        creator=source.creator if source else "",
        clip_duration_s=clip.duration_s if clip else 0.0,
        caption_style=packed.get("caption_style", ""),
        gameplay_bed=packed.get("gameplay_bed", ""),
        predicted_virality=clip.virality_score if clip else 0.0,
        hook_weights_version=clip.weights_version if clip else "",
        viral_weights_version=packed.get("viral_weights_version", ""),
        extra=dict(packed.get("extra", {})),
    )
