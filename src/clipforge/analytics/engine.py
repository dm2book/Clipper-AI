"""The analytics intelligence engine — orchestration.

    ingest snapshots from the platforms
      → learn this account's own baselines
      → join outcomes to the decisions that produced them
      → answer the five questions, with confidence attached
      → check the models' predictions against reality
      → build weekly reports on a schedule

Metric collection is a `MetricSource` protocol with no live implementation, for
the same reason the publishing system ships no HTTP client: the three platforms
expose completely different reporting APIs on different delays, and a fake one
here would be a liability rather than a convenience. `RecordedSource` replays
snapshots, which is what the tests and the demo use.

Reports are scheduled with the same `Recurrence` the publishing system uses, so
"every Monday at 9am in the creator's timezone" is DST-correct without a second
implementation of the hard part.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol, Sequence

from ..publish.schedule import Recurrence, weekly_on
from ..publish.types import Platform, ensure_utc, utcnow
from .attribution import AnalyticsStore, PostRecord
from .experiments import ExplorationPolicy, MIN_EXPLORED
from .insights import calibration, diagnose_retention
from .metrics import Baselines, CHECKPOINTS_H, PRIMARY_CHECKPOINT_H, Snapshot
from .report import WeeklyReport, build_weekly


class MetricSource(Protocol):
    """Fetches current counters for a published post."""

    def fetch(self, post_id: str, platform: Platform) -> Snapshot | None: ...


class RecordedSource:
    """Replays snapshots supplied up front.

    The default. There is no live implementation because the three platforms'
    reporting APIs differ in shape, granularity and delay — TikTok and
    Instagram report far less retention detail than YouTube, and all three lag
    by hours — and a stub that pretended otherwise would produce analyses whose
    limitations only appeared in production.
    """

    def __init__(self, snapshots: dict[str, list[Snapshot]] | None = None) -> None:
        self._snapshots = snapshots or {}
        self._served: set[tuple[str, float]] = set()

    def add(self, post_id: str, snapshot: Snapshot) -> None:
        self._snapshots.setdefault(post_id, []).append(snapshot)

    def fetch(self, post_id: str, platform: Platform) -> Snapshot | None:
        pending = [
            s for s in self._snapshots.get(post_id, [])
            if (post_id, s.age_hours) not in self._served
        ]
        if not pending:
            return None
        snapshot = min(pending, key=lambda s: s.age_hours)
        self._served.add((post_id, snapshot.age_hours))
        return snapshot


@dataclass(slots=True)
class AnalyticsConfig:
    #: Age at which posts are compared to each other.
    checkpoint_h: float = PRIMARY_CHECKPOINT_H
    #: How far back the five questions look. A week is never enough.
    lookback_weeks: int = 8
    #: When weekly reports fire. Monday morning, in the creator's timezone.
    schedule: Recurrence = field(
        default_factory=lambda: weekly_on([0], 9, 0, "UTC")
    )
    exploration: ExplorationPolicy = field(default_factory=ExplorationPolicy)
    seed: str = "clipforge"


class AnalyticsEngine:
    """Ingests metrics, answers the five questions, produces reports."""

    def __init__(
        self,
        config: AnalyticsConfig | None = None,
        store: AnalyticsStore | None = None,
    ) -> None:
        self.config = config or AnalyticsConfig()
        self.store = store or AnalyticsStore()
        self.baselines = Baselines()

    # -- ingest ------------------------------------------------------------------

    def track(self, record: PostRecord) -> None:
        self.store.add(record)

    def ingest(
        self, source: MetricSource, now: datetime | None = None
    ) -> dict[str, int]:
        """Pull one round of counters for every tracked post.

        Never raises for one post's sake — a platform returning nonsense for a
        single clip must not stop the collection run, or one bad post costs a
        whole week of data for every other.
        """
        now = ensure_utc(now or utcnow())
        collected = failed = skipped = 0

        for record in self.store.records:
            try:
                snapshot = source.fetch(record.post_id, record.platform)
            except Exception:                            # noqa: BLE001
                failed += 1
                continue
            if snapshot is None:
                skipped += 1
                continue
            record.metrics.record(snapshot)
            collected += 1

        self.baselines.learn(
            [r.metrics for r in self.store.records], self.config.checkpoint_h
        )
        return {
            "collected": collected, "skipped": skipped, "failed": failed,
            "tracked": len(self.store),
        }

    def due_checkpoints(self, now: datetime | None = None
                        ) -> list[tuple[str, float]]:
        """Posts that have passed a checkpoint without a reading near it.

        Drives the collection schedule. Sampling on a fixed cadence instead
        leaves posts with no reading near 24h, which is precisely the age every
        comparison is made at.
        """
        now = ensure_utc(now or utcnow())
        due: list[tuple[str, float]] = []

        for record in self.store.records:
            age = (now - record.published_at).total_seconds() / 3600.0
            for checkpoint in CHECKPOINTS_H:
                if age < checkpoint:
                    break
                if record.metrics.at_age(checkpoint) is None:
                    due.append((record.post_id, checkpoint))
        return due

    # -- analysis ------------------------------------------------------------------

    def report(
        self,
        week_end: datetime | None = None,
        scope: str = "all channels",
        channel_id: str = "",
        platform: Platform | None = None,
    ) -> WeeklyReport:
        return build_weekly(
            self.store,
            week_end=ensure_utc(week_end or utcnow()),
            scope=scope, channel_id=channel_id, platform=platform,
            checkpoint_h=self.config.checkpoint_h,
            lookback_weeks=self.config.lookback_weeks,
            seed=self.config.seed, baselines=self.baselines,
        )

    def reports_for_channels(
        self, week_end: datetime | None = None
    ) -> dict[str, WeeklyReport]:
        """One report per channel, plus one across all of them.

        Per channel because that is the unit someone acts on, and combined
        because a single channel rarely has the volume to answer anything —
        pooling seven channels is often the only way a question becomes
        answerable at all, at the cost of assuming they behave alike.
        """
        week_end = ensure_utc(week_end or utcnow())
        names = {
            r.channel_id: (r.channel_name or r.channel_id)
            for r in self.store.records if r.channel_id
        }

        out = {"__all__": self.report(week_end, "all channels")}
        for channel_id, name in sorted(names.items(), key=lambda kv: kv[1]):
            out[channel_id] = self.report(week_end, name, channel_id=channel_id)
        return out

    def next_report_at(self, after: datetime | None = None) -> datetime | None:
        return self.config.schedule.next_after(ensure_utc(after or utcnow()))

    # -- health ------------------------------------------------------------------

    def readiness(self) -> dict[str, Any]:
        """Whether this account has enough data to be told anything.

        Worth surfacing before the first report rather than after a creator
        has read four weeks of "not enough data" and stopped opening them.
        """
        records = self.store.records
        mature = [r for r in records if r.mature(self.config.checkpoint_h)]
        explored = [r for r in mature if r.explored]

        with_retention = sum(
            1 for r in mature
            if (s := r.metrics.at_age(self.config.checkpoint_h))
            and s.retention.available
        )

        return {
            "tracked": len(records),
            "mature": len(mature),
            "explored": len(explored),
            "explored_needed": max(0, MIN_EXPLORED - len(explored)),
            "hook_questions_causal": len(explored) >= MIN_EXPLORED,
            "with_retention_curve": with_retention,
            "baselines_observed": [
                p.value for p in Platform if self.baselines.is_observed(p)
            ],
            "exploration": self.config.exploration.to_dict(),
        }

    def status(self) -> dict[str, Any]:
        records = self.store.records
        return {
            "store": self.store.to_dict(),
            "readiness": self.readiness(),
            "baselines": self.baselines.to_dict(),
            "retention": diagnose_retention(
                [r for r in records if r.mature(self.config.checkpoint_h)],
                self.config.checkpoint_h,
            ).to_dict(),
            "calibration": calibration(
                [r for r in records if r.mature(self.config.checkpoint_h)],
                "predicted_lift", "view_through_rate", self.config.checkpoint_h,
            ).to_dict(),
        }
