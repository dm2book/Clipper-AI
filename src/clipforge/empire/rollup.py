"""Aggregating fifty channels into four numbers.

Total views, total uploads, total revenue, total growth — one line each, which
is the whole point of a dashboard and also where dashboards go wrong.

**A total hides the distribution, and at fifty channels the distribution is the
story.** "1.2M views this week" is the same number whether every channel
contributed evenly or one clip went viral and the other forty-nine did nothing.
Those are completely different businesses and they need opposite decisions, so
every total here carries a concentration measure alongside it.

**Growth needs a comparable denominator.** Week-on-week totals move when
channels are added, so a portfolio that grew from forty to fifty channels shows
25% "growth" from nothing but arithmetic. Growth is therefore reported twice:
raw, and same-channel — restricted to channels present in both periods, which
is the only version that says anything about whether the content improved.

**Every total is significance-tested.** Two weeks of a fifty-channel portfolio
differ by double digits on noise alone; the analytics engine's machinery
already knows how to say so, and this reuses it rather than reimplementing a
naive percentage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from ..analytics.attribution import AnalyticsStore, PostRecord
from ..analytics.metrics import PRIMARY_CHECKPOINT_H
from ..analytics.stats import MIN_MATERIAL_EFFECT, permutation_p, trimmed_mean
from ..publish.types import Platform, ensure_utc


@dataclass(frozen=True, slots=True)
class Concentration:
    """How much of a total comes from how little of the portfolio.

    Reported next to every headline number, because at fifty channels a total
    is a summary of a distribution and the distribution is usually extreme.
    """

    top_1_share: float
    top_10pct_share: float
    contributors: int
    #: Channels producing less than 1% of the leader. Not necessarily a
    #: problem — but if forty of fifty are here, the portfolio is one channel.
    dormant: int

    @property
    def verdict(self) -> str:
        if self.top_1_share >= 0.5:
            return (
                f"one channel is {self.top_1_share * 100:.0f}% of the total — "
                f"this is that channel's business with forty-nine hobbies"
            )
        if self.top_10pct_share >= 0.8:
            return (
                f"the top 10% carry {self.top_10pct_share * 100:.0f}% — the "
                f"portfolio total is a proxy for a handful of channels"
            )
        return "broadly distributed across the portfolio"

    def to_dict(self) -> dict[str, Any]:
        return {
            "top_1_share": round(self.top_1_share, 4),
            "top_10pct_share": round(self.top_10pct_share, 4),
            "contributors": self.contributors,
            "dormant": self.dormant,
            "verdict": self.verdict,
        }


def concentration(values: Sequence[float]) -> Concentration:
    live = [v for v in values if v > 0]
    if not live:
        return Concentration(0.0, 0.0, 0, len(values))

    ordered = sorted(live, reverse=True)
    total = sum(ordered)
    top_n = max(1, len(ordered) // 10)
    leader = ordered[0]

    return Concentration(
        top_1_share=leader / total if total else 0.0,
        top_10pct_share=sum(ordered[:top_n]) / total if total else 0.0,
        contributors=len(live),
        dormant=sum(1 for v in values if v < leader * 0.01),
    )


@dataclass(frozen=True, slots=True)
class Growth:
    """A change, with the two denominators that make it honest."""

    metric: str
    current: float
    previous: float
    #: Restricted to channels present in both periods.
    same_channel_current: float
    same_channel_previous: float
    channels_added: int
    p_value: float = 1.0
    significant: bool = False

    @property
    def raw_change(self) -> float:
        return (
            (self.current - self.previous) / self.previous
            if self.previous else 0.0
        )

    @property
    def same_channel_change(self) -> float:
        if not self.same_channel_previous:
            return 0.0
        return (
            (self.same_channel_current - self.same_channel_previous)
            / self.same_channel_previous
        )

    @property
    def from_expansion(self) -> float:
        """The part of raw growth explained by adding channels."""
        return self.raw_change - self.same_channel_change

    def describe(self) -> str:
        if not self.significant:
            return (
                f"{self.metric}: {self.raw_change * 100:+.0f}% raw — inside "
                f"the noise for a portfolio this size"
            )
        if self.channels_added and abs(self.from_expansion) > 0.05:
            return (
                f"{self.metric}: {self.raw_change * 100:+.0f}% raw, but "
                f"{self.same_channel_change * 100:+.0f}% on the same "
                f"channels — {self.channels_added} new channel(s) account for "
                f"most of the difference"
            )
        return (
            f"{self.metric}: {self.same_channel_change * 100:+.0f}% on the "
            f"same channels (p={self.p_value:.3f})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "current": round(self.current, 2),
            "previous": round(self.previous, 2),
            "raw_change": round(self.raw_change, 4),
            "same_channel_change": round(self.same_channel_change, 4),
            "from_expansion": round(self.from_expansion, 4),
            "channels_added": self.channels_added,
            "p_value": round(self.p_value, 4),
            "significant": self.significant,
            "description": self.describe(),
        }


def _sum_metric(records: Sequence[PostRecord], metric: str,
                checkpoint_h: float) -> float:
    total = 0.0
    for record in records:
        value = record.value(metric, checkpoint_h)
        if value is not None:
            total += value
    return total


def growth(
    metric: str,
    current: Sequence[PostRecord],
    previous: Sequence[PostRecord],
    checkpoint_h: float = PRIMARY_CHECKPOINT_H,
    seed: str = "empire",
) -> Growth:
    """Change between two periods, with the expansion effect separated out."""
    now_channels = {r.channel_id for r in current}
    was_channels = {r.channel_id for r in previous}
    shared = now_channels & was_channels

    same_now = [r for r in current if r.channel_id in shared]
    same_was = [r for r in previous if r.channel_id in shared]

    per_post_now = [
        v for r in same_now if (v := r.value(metric, checkpoint_h)) is not None
    ]
    per_post_was = [
        v for r in same_was if (v := r.value(metric, checkpoint_h)) is not None
    ]

    p_value, flagged = 1.0, False
    if len(per_post_now) >= 3 and len(per_post_was) >= 3:
        p_value = permutation_p(
            per_post_now, per_post_was, trimmed_mean, seed=f"{seed}|{metric}"
        )
        now_stat, was_stat = trimmed_mean(per_post_now), trimmed_mean(per_post_was)
        change = (now_stat - was_stat) / was_stat if was_stat else 0.0
        flagged = p_value < 0.05 and abs(change) >= MIN_MATERIAL_EFFECT

    return Growth(
        metric=metric,
        current=_sum_metric(current, metric, checkpoint_h),
        previous=_sum_metric(previous, metric, checkpoint_h),
        same_channel_current=_sum_metric(same_now, metric, checkpoint_h),
        same_channel_previous=_sum_metric(same_was, metric, checkpoint_h),
        channels_added=len(now_channels - was_channels),
        p_value=p_value, significant=flagged,
    )


@dataclass(frozen=True, slots=True)
class Totals:
    """The four headline numbers, each with its distribution attached."""

    uploads: int
    views: int
    likes: int
    comments: int
    shares: int
    subscribers: int
    channels: int
    brands: int

    views_concentration: Concentration | None = None
    uploads_concentration: Concentration | None = None
    views_by_platform: dict[str, int] = field(default_factory=dict)
    uploads_by_platform: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "uploads": self.uploads,
            "views": self.views,
            "likes": self.likes,
            "comments": self.comments,
            "shares": self.shares,
            "subscribers": self.subscribers,
            "channels": self.channels,
            "brands": self.brands,
            "views_by_platform": dict(sorted(self.views_by_platform.items())),
            "uploads_by_platform": dict(
                sorted(self.uploads_by_platform.items())
            ),
            "views_concentration": (
                self.views_concentration.to_dict()
                if self.views_concentration else None
            ),
            "uploads_concentration": (
                self.uploads_concentration.to_dict()
                if self.uploads_concentration else None
            ),
        }


def totals(
    records: Sequence[PostRecord],
    brands: int = 0,
    checkpoint_h: float = PRIMARY_CHECKPOINT_H,
) -> Totals:
    """Roll a set of posts up into the headline numbers."""
    views = likes = comments = shares = subscribers = 0
    per_channel_views: dict[str, float] = {}
    per_channel_uploads: dict[str, int] = {}
    views_platform: dict[str, int] = {}
    uploads_platform: dict[str, int] = {}

    for record in records:
        per_channel_uploads[record.channel_id] = (
            per_channel_uploads.get(record.channel_id, 0) + 1
        )
        uploads_platform[record.platform.value] = (
            uploads_platform.get(record.platform.value, 0) + 1
        )

        snapshot = record.metrics.at_age(checkpoint_h) or record.metrics.latest
        if snapshot is None:
            continue

        views += snapshot.views
        likes += snapshot.likes
        comments += snapshot.comments
        shares += snapshot.shares
        subscribers += snapshot.follows

        per_channel_views[record.channel_id] = (
            per_channel_views.get(record.channel_id, 0.0) + snapshot.views
        )
        views_platform[record.platform.value] = (
            views_platform.get(record.platform.value, 0) + snapshot.views
        )

    return Totals(
        uploads=len(records), views=views, likes=likes, comments=comments,
        shares=shares, subscribers=subscribers,
        channels=len(per_channel_uploads), brands=brands,
        views_concentration=concentration(list(per_channel_views.values())),
        uploads_concentration=concentration(
            [float(v) for v in per_channel_uploads.values()]
        ),
        views_by_platform=views_platform,
        uploads_by_platform=uploads_platform,
    )


@dataclass(frozen=True, slots=True)
class ChannelLine:
    """One row of the leaderboard."""

    channel_id: str
    channel_name: str
    brand_id: str
    brand_name: str
    uploads: int
    views: int
    subscribers: int
    #: Views per upload — the only column that compares a channel posting
    #: twice a day to one posting weekly.
    views_per_upload: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "channel": self.channel_name,
            "brand": self.brand_name,
            "uploads": self.uploads,
            "views": self.views,
            "subscribers": self.subscribers,
            "views_per_upload": round(self.views_per_upload, 1),
        }


def leaderboard(
    records: Sequence[PostRecord],
    names: dict[str, str] | None = None,
    brand_of: dict[str, str] | None = None,
    brand_names: dict[str, str] | None = None,
    checkpoint_h: float = PRIMARY_CHECKPOINT_H,
) -> list[ChannelLine]:
    """Per-channel rows, ranked by views.

    Sorted by total views because that is what an operator scans for, but
    `views_per_upload` is the column that actually compares channels — a
    channel posting four times a day will top a total-views ranking while
    being the worst performer in the portfolio.
    """
    names = names or {}
    brand_of = brand_of or {}
    brand_names = brand_names or {}

    grouped: dict[str, dict[str, float]] = {}
    for record in records:
        entry = grouped.setdefault(
            record.channel_id, {"uploads": 0.0, "views": 0.0, "subs": 0.0}
        )
        entry["uploads"] += 1
        snapshot = record.metrics.at_age(checkpoint_h) or record.metrics.latest
        if snapshot is not None:
            entry["views"] += snapshot.views
            entry["subs"] += snapshot.follows

    lines = [
        ChannelLine(
            channel_id=channel_id,
            channel_name=names.get(channel_id, channel_id),
            brand_id=brand_of.get(channel_id, ""),
            brand_name=brand_names.get(brand_of.get(channel_id, ""), ""),
            uploads=int(entry["uploads"]),
            views=int(entry["views"]),
            subscribers=int(entry["subs"]),
            views_per_upload=(
                entry["views"] / entry["uploads"] if entry["uploads"] else 0.0
            ),
        )
        for channel_id, entry in grouped.items()
    ]
    lines.sort(key=lambda line: -line.views)
    return lines
