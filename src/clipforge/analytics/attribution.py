"""Joining outcomes to the decisions that produced them.

Every engine in this repository persists its feature vector and its weights
version with each decision, and this is the file those choices were made for.
A `PostRecord` is one published clip with both halves attached: what the system
decided, and what happened.

### Selection bias is the hard part, not the join

The factory publishes the *top-ranked* hook. So the only outcomes ever observed
for a hook type are outcomes for hooks the model already believed in. Measuring
"authority hooks perform well" on that data measures the model's preferences,
not the hooks — and the measurement will confirm the prior no matter what the
prior was, because the prior chose the sample.

This is the same failure the hook engine warned about: *a model trained only on
hooks that shipped learns which hooks get chosen, not which hooks work.*

The only fix is to sometimes publish something the model did not rank first.
`experiments.py` does that; `PostRecord.explored` records whether it happened;
and every comparison here reports whether it ran on explored data or on the
model's own choices. A confounded answer is still worth showing — it is what
the account actually experienced — but it must never be presented as evidence
that the prior was right.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from ..publish.types import Platform, ensure_utc
from .metrics import PostMetrics, PRIMARY_CHECKPOINT_H, Snapshot

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(slots=True)
class PostRecord:
    """One published clip: the decisions, and what came of them."""

    post_id: str
    metrics: PostMetrics

    # -- who -------------------------------------------------------------------
    channel_id: str = ""
    channel_name: str = ""
    niche: str = ""
    account_id: str = ""

    # -- when ------------------------------------------------------------------
    #: The channel's own timezone. "Best posting time" is a statement about
    #: local wall-clock time; expressed in UTC it is meaningless to the person
    #: acting on it, and wrong for half the year in any DST zone.
    timezone: str = "UTC"

    # -- what the system decided ------------------------------------------------
    hook_text: str = ""
    hook_type: str = ""
    predicted_lift: float = 0.0
    #: Which rank the published hook held in the generated set. 0 is the
    #: model's own pick; anything above is an exploration.
    hook_rank: int = 0
    explored: bool = False

    topic: str = ""
    source_id: str = ""
    creator: str = ""
    clip_duration_s: float = 0.0
    caption_style: str = ""
    gameplay_bed: str = ""
    predicted_virality: float = 0.0

    #: Versions of the priors in force when this was decided. Without them a
    #: retrained model's results get pooled with the old model's and neither
    #: can be evaluated.
    hook_weights_version: str = ""
    viral_weights_version: str = ""

    extra: dict[str, Any] = field(default_factory=dict)

    # -- derived ------------------------------------------------------------------

    @property
    def platform(self) -> Platform:
        return self.metrics.platform

    @property
    def published_at(self) -> datetime:
        return self.metrics.published_at

    @property
    def local_time(self) -> datetime:
        return self.published_at.astimezone(ZoneInfo(self.timezone))

    @property
    def local_hour(self) -> int:
        return self.local_time.hour

    @property
    def local_weekday(self) -> int:
        return self.local_time.weekday()

    @property
    def slot(self) -> str:
        """The posting slot, as a creator would name it."""
        return f"{WEEKDAYS[self.local_weekday]} {self.local_hour:02d}:00"

    @property
    def duration_bucket(self) -> str:
        """Clip length, bucketed.

        Buckets rather than raw seconds because no channel has enough posts to
        compare 28-second clips against 29-second ones, and pretending
        otherwise produces a hundred groups of one.
        """
        duration = self.clip_duration_s
        if duration <= 0:
            return "unknown"
        for low, high in ((0, 15), (15, 25), (25, 35), (35, 45), (45, 60)):
            if low <= duration < high:
                return f"{low}-{high}s"
        return "60s+"

    def value(self, metric: str, checkpoint_h: float = PRIMARY_CHECKPOINT_H
              ) -> float | None:
        """A metric at a matched age, or None if the post is too young.

        None rather than a substitute. A younger reading silently dragged into
        a comparison is how "recent posts are underperforming" gets reported
        when what has been measured is that recent posts are recent.
        """
        snapshot = self.metrics.at_age(checkpoint_h)
        if snapshot is None:
            return None
        return _extract(snapshot, metric)

    def mature(self, checkpoint_h: float = PRIMARY_CHECKPOINT_H) -> bool:
        return self.metrics.mature_at(checkpoint_h)

    def to_dict(self) -> dict[str, Any]:
        latest = self.metrics.latest
        return {
            "post_id": self.post_id,
            "channel": self.channel_name or self.channel_id,
            "niche": self.niche,
            "platform": self.platform.value,
            "published_at": self.published_at.isoformat(),
            "slot": self.slot,
            "hook_type": self.hook_type,
            "hook_rank": self.hook_rank,
            "explored": self.explored,
            "predicted_lift": round(self.predicted_lift, 4),
            "topic": self.topic,
            "creator": self.creator,
            "duration_bucket": self.duration_bucket,
            "caption_style": self.caption_style,
            "gameplay_bed": self.gameplay_bed,
            "views": latest.views if latest else 0,
            "engagement_rate": (
                round(latest.engagement_rate, 5) if latest else 0.0
            ),
            "hook_hold": (
                round(latest.retention.hook_hold, 4)
                if latest and latest.retention.available else None
            ),
        }


#: Metrics that can be compared. Rates rather than counts wherever possible:
#: a count is mostly a report on how much distribution the post received, which
#: is the thing being explained rather than the explanation.
METRICS: dict[str, str] = {
    "views": "views",
    "engagement_rate": "engagement rate",
    "like_rate": "like rate",
    "comment_rate": "comment rate",
    "share_rate": "share rate",
    "follow_rate": "follow rate",
    "avg_watch_pct": "average watch %",
    "hook_hold": "hook hold",
    "completion": "completion",
    "view_through_rate": "view-through rate",
    "follows": "subscribers gained",
}


def _extract(snapshot: Snapshot, metric: str) -> float | None:
    if metric == "hook_hold":
        if not snapshot.retention.available:
            return None
        return snapshot.retention.hook_hold
    if metric == "completion":
        if not snapshot.retention.available:
            return None
        return snapshot.retention.completion
    if metric == "views":
        return float(snapshot.views)
    if metric == "follows":
        return float(snapshot.follows)
    value = getattr(snapshot, metric, None)
    return float(value) if value is not None else None


class AnalyticsStore:
    """Every published post, with its decisions and its outcomes."""

    def __init__(self, records: Iterable[PostRecord] = ()) -> None:
        self._records: dict[str, PostRecord] = {}
        for record in records:
            self.add(record)

    def add(self, record: PostRecord) -> None:
        self._records[record.post_id] = record

    def get(self, post_id: str) -> PostRecord | None:
        return self._records.get(post_id)

    def __len__(self) -> int:
        return len(self._records)

    @property
    def records(self) -> tuple[PostRecord, ...]:
        return tuple(sorted(
            self._records.values(), key=lambda r: r.published_at
        ))

    def select(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        channel_id: str = "",
        platform: Platform | None = None,
        niche: str = "",
        explored_only: bool = False,
        checkpoint_h: float = PRIMARY_CHECKPOINT_H,
        require_mature: bool = True,
    ) -> tuple[PostRecord, ...]:
        """Posts matching a filter, mature enough to compare at `checkpoint_h`."""
        out = []
        for record in self.records:
            if since and record.published_at < ensure_utc(since):
                continue
            if until and record.published_at > ensure_utc(until):
                continue
            if channel_id and record.channel_id != channel_id:
                continue
            if platform is not None and record.platform is not platform:
                continue
            if niche and record.niche != niche:
                continue
            if explored_only and not record.explored:
                continue
            if require_mature and not record.mature(checkpoint_h):
                continue
            out.append(record)
        return tuple(out)

    def group(
        self,
        records: Sequence[PostRecord],
        dimension: str,
        metric: str,
        checkpoint_h: float = PRIMARY_CHECKPOINT_H,
    ) -> dict[str, list[float]]:
        """Bucket a metric by a dimension, dropping posts that cannot supply it.

        A post whose platform reports no retention curve is absent from a
        retention comparison rather than counted as zero. Counting a missing
        measurement as zero is not conservative — it invents a bad outcome.
        """
        groups: dict[str, list[float]] = {}
        for record in records:
            key = dimension_value(record, dimension)
            if not key:
                continue
            value = record.value(metric, checkpoint_h)
            if value is None:
                continue
            groups.setdefault(key, []).append(value)
        return groups

    def coverage(
        self, records: Sequence[PostRecord], metric: str,
        checkpoint_h: float = PRIMARY_CHECKPOINT_H,
    ) -> tuple[int, int]:
        """How many of these posts can actually supply this metric."""
        have = sum(
            1 for r in records if r.value(metric, checkpoint_h) is not None
        )
        return have, len(records)

    def to_dict(self) -> dict[str, Any]:
        return {
            "posts": len(self._records),
            "explored": sum(1 for r in self._records.values() if r.explored),
            "channels": len({r.channel_id for r in self._records.values()}),
            "platforms": sorted({
                r.platform.value for r in self._records.values()
            }),
        }


#: The dimensions the reports rank on.
DIMENSIONS: dict[str, str] = {
    "slot": "posting time",
    "hour": "hour of day",
    "weekday": "day of week",
    "hook_type": "hook type",
    "topic": "topic",
    "duration_bucket": "clip length",
    "creator": "source creator",
    "caption_style": "caption style",
    "gameplay_bed": "gameplay bed",
    "platform": "platform",
    "niche": "niche",
    "channel": "channel",
}


def dimension_value(record: PostRecord, dimension: str) -> str:
    if dimension == "slot":
        return record.slot
    if dimension == "hour":
        return f"{record.local_hour:02d}:00"
    if dimension == "weekday":
        return WEEKDAYS[record.local_weekday]
    if dimension == "duration_bucket":
        return record.duration_bucket
    if dimension == "platform":
        return record.platform.value
    if dimension == "channel":
        return record.channel_name or record.channel_id
    return str(getattr(record, dimension, "") or "")
